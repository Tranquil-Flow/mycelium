# SPDX-License-Identifier: AGPL-3.0-only
"""Durable judge-owned authority and self-enrollment composition."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Any, TYPE_CHECKING
import uuid

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_node import NodeMembershipSession
from mycelium_node.membership import NodeMembershipError
from mycelium_node.durable_membership import (
    PROTOCOL as MEMBERSHIP_STATE_PROTOCOL,
    load_membership_state,
    next_incarnation,
    save_membership_state,
)
from mycelium_node.identity import load_node_signer, load_or_create_node_signer
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_seed import SeedCoordinator, SeedCoordinatorError
from mycelium_seed.http import SeedHTTPError
from mycelium_seed.authority import load_bound_seed_signer
from mycelium_seed.state import SqliteSeedState

from .pairing import (
    PairingError,
    approve_pairing_request,
    decrypt_invitation_handoff,
    new_pairing_request,
    validate_quota,
    verify_pairing_request,
)
from .tls import (
    LocalTLSSeedServer,
    ensure_local_tls_material,
    pinned_seed_client,
    pinned_seed_client_from_invite,
)

if TYPE_CHECKING:
    from .serving import PreparedSource, RegistryLaunch

_STATUS_PROTOCOL = "mycelium.demo_authority_status.v1"
_METADATA_PROTOCOL = "mycelium.demo_authority_state.v1"
_PENDING_PROTOCOL = "mycelium.demo_pairing_pending.v1"
_APPROVAL_PROTOCOL = "mycelium.demo_pairing_approval.v1"
_JOIN_PENDING_PROTOCOL = "mycelium.demo_join_pending.v1"
_ACCEPTED_PROTOCOL = "mycelium.demo_target_accepted.v1"
_MEMBERSHIP_RESULT_PROTOCOL = "mycelium.demo_membership_result.v1"
_REVOCATION_RESULT_PROTOCOL = "mycelium.demo_revocation_result.v1"
_SOFTWARE_VERSION = "mycelium-a13-macos-demo/1"
_PEER_CLASS = "mac_mlx_iroh"
_RUNTIME_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}
_CONSENT_FIELDS = frozenset(
    {
        "cooperative_peer_visibility_acknowledged",
        "resource_use_acknowledged",
        "contribution_enabled",
    }
)
_METADATA_FIELDS = frozenset(
    {
        "protocol",
        "swarm_id",
        "seed_node_id",
        "seed_incarnation",
        "target_node_id",
        "peer_class",
        "consent",
    }
)
_PENDING_FIELDS = frozenset(
    {"protocol", "request", "recipient_private_key"}
)
_ACCEPTED_FIELDS = frozenset(
    {"protocol", "handoff_digest", "quota", "accepted_at"}
)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PRIVATE_JSON_BYTES = 256 * 1024


class DemoAuthorityError(RuntimeError):
    """Stable composition error that never includes private source values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(document))).hexdigest()


def _win32_private_directory(absolute: Path, *, create: bool) -> Path:
    import mycelium_host_platform
    from mycelium_host_platform import HostPlatformError

    try:
        with mycelium_host_platform.private_directory(absolute, create=create) as root:
            if not root.exists:
                raise DemoAuthorityError("authority_state_path_invalid")
    except HostPlatformError as exc:
        raise DemoAuthorityError("authority_state_path_invalid") from exc
    return absolute


def _private_directory(path: Path, *, create: bool) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    import mycelium_host_platform

    if mycelium_host_platform.is_win32():
        return _win32_private_directory(absolute, create=create)
    try:
        if create and not absolute.exists() and not absolute.is_symlink():
            absolute.mkdir(parents=True, mode=0o700)
            absolute.chmod(0o700)
        metadata = absolute.lstat()
    except OSError as exc:
        raise DemoAuthorityError("authority_state_path_invalid") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DemoAuthorityError("authority_state_path_invalid")
    return absolute


def _atomic_private_json(path: Path, document: Mapping[str, Any], *, max_bytes: int = _MAX_PRIVATE_JSON_BYTES) -> None:
    if type(max_bytes) is not int or not 0 < max_bytes <= 4*1024*1024:
        raise DemoAuthorityError("authority_state_size_limit_invalid")
    raw = canonical_json_bytes(dict(document))
    if not raw or len(raw) > max_bytes:
        raise DemoAuthorityError("authority_state_write_failed")
    import mycelium_host_platform

    if mycelium_host_platform.is_win32():
        from mycelium_host_platform import HostPlatformError

        try:
            with mycelium_host_platform.private_directory(path.parent, create=False) as root:
                root.atomic_replace(path.name, raw)
        except HostPlatformError as exc:
            raise DemoAuthorityError("authority_state_write_failed") from exc
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short private state write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        parent = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError as exc:
        raise DemoAuthorityError("authority_state_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _win32_load_private_json(path: Path, *, max_bytes: int) -> dict[str, Any] | None:
    import mycelium_host_platform
    from mycelium_host_platform import HostPlatformError

    try:
        with mycelium_host_platform.private_directory(path.parent, create=False) as root:
            raw = root.read_file(path.name, max_bytes=max_bytes)
    except HostPlatformError as exc:
        if exc.code == "host_file_missing":
            return None
        raise DemoAuthorityError("authority_state_invalid") from exc
    if not raw:
        raise DemoAuthorityError("authority_state_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise ValueError("noncanonical")
    except (UnicodeDecodeError, ValueError) as exc:
        raise DemoAuthorityError("authority_state_invalid") from exc
    return value


def _load_private_json(path: Path, *, max_bytes: int = _MAX_PRIVATE_JSON_BYTES) -> dict[str, Any] | None:
    if type(max_bytes) is not int or not 0 < max_bytes <= 4*1024*1024:
        raise DemoAuthorityError("authority_state_size_limit_invalid")
    import mycelium_host_platform

    if mycelium_host_platform.is_win32():
        return _win32_load_private_json(path, max_bytes=max_bytes)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DemoAuthorityError("authority_state_invalid") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 0 < metadata.st_size <= max_bytes
    ):
        raise DemoAuthorityError("authority_state_invalid")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise DemoAuthorityError("authority_state_invalid")
        raw = os.read(descriptor, max_bytes + 1)
    except DemoAuthorityError:
        raise
    except OSError as exc:
        raise DemoAuthorityError("authority_state_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not raw or len(raw)>max_bytes:
        raise DemoAuthorityError("authority_state_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise ValueError("noncanonical")
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DemoAuthorityError("authority_state_invalid") from exc
    return value


def _consent(value: Mapping[str, Any]) -> dict[str, bool]:
    if not isinstance(value, Mapping) or set(value) != _CONSENT_FIELDS:
        raise DemoAuthorityError("authority_consent_invalid")
    result = dict(value)
    if any(type(result[field]) is not bool for field in _CONSENT_FIELDS):
        raise DemoAuthorityError("authority_consent_invalid")
    if not (
        result["cooperative_peer_visibility_acknowledged"]
        and result["resource_use_acknowledged"]
    ):
        raise DemoAuthorityError("authority_consent_invalid")
    return result


def _b64_private(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_private(value: Any) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise DemoAuthorityError("authority_pairing_state_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001 - private state boundary
        raise DemoAuthorityError("authority_pairing_state_invalid") from exc
    if len(raw) != 32 or _b64_private(raw) != value:
        raise DemoAuthorityError("authority_pairing_state_invalid")
    return raw


class DemoAuthority:
    """One installation's durable canonical seed and local target member."""

    def __init__(self, state_root: Path | str) -> None:
        self.state_root = Path(os.path.abspath(Path(state_root).expanduser()))
        self._lock = threading.RLock()
        self._started = False
        self._metadata: dict[str, Any] | None = None
        self._coordinator: SeedCoordinator | None = None
        self._control_mailbox = None
        self._member: NodeMembershipSession | None = None
        self._seed_server: LocalTLSSeedServer | None = None
        self._seed_client = None
        # Only a verified canonical HTTP receipt populates this observation.
        # Stop/resume and generation changes invalidate it.
        self._capability_report: dict[str, Any] | None = None

    @property
    def coordinator(self) -> SeedCoordinator | None:
        return self._coordinator

    @property
    def member(self) -> NodeMembershipSession | None:
        return self._member

    @property
    def seed_server(self) -> LocalTLSSeedServer | None:
        return self._seed_server

    @property
    def seed_client(self):
        return self._seed_client

    def _clock(self) -> float:
        value = float(time.time())
        if not math.isfinite(value):
            raise DemoAuthorityError("authority_clock_invalid")
        return value

    @property
    def _metadata_path(self) -> Path:
        return self.state_root / "authority.json"

    @property
    def _seed_root(self) -> Path:
        return self.state_root / "seed"

    @property
    def _target_root(self) -> Path:
        return self.state_root / "target"

    def _require_started(self) -> None:
        if not self._started:
            raise DemoAuthorityError("authority_not_started")

    def _load_metadata(self) -> dict[str, Any] | None:
        value = _load_private_json(self._metadata_path)
        if value is None:
            return None
        if (
            set(value) != _METADATA_FIELDS
            or value.get("protocol") != _METADATA_PROTOCOL
            or value.get("peer_class") != _PEER_CLASS
            or any(
                not isinstance(value.get(field), str)
                or _SEGMENT_RE.fullmatch(value[field]) is None
                for field in (
                    "swarm_id",
                    "seed_node_id",
                    "seed_incarnation",
                    "target_node_id",
                )
            )
        ):
            raise DemoAuthorityError("authority_state_invalid")
        _consent(value.get("consent"))
        return value

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._started:
                return self.status()
            _private_directory(self.state_root, create=True)
            metadata = self._load_metadata()
            self._network_configuration()
            if metadata is None and any(p.name!='network.json' for p in self.state_root.iterdir()):
                raise DemoAuthorityError("authority_partial_state")
            self._started = True
            self._metadata = metadata
            if metadata is None:
                return self.status()
            try:
                self._start_runtime()
            except Exception:
                self._started = False
                self._metadata = None
                self._close_runtime()
                raise
            return self.status()

    def create_swarm(self, consent: dict) -> dict[str, Any]:
        with self._lock:
            self._require_started()
            approved_consent = _consent(consent)
            self._network_configuration()
            if self._metadata is not None or any(p.name!='network.json' for p in self.state_root.iterdir()):
                raise DemoAuthorityError("authority_swarm_exists")
            seed_root = _private_directory(self._seed_root, create=True)
            target_root = _private_directory(self._target_root, create=True)
            seed_signer = load_or_create_node_signer(
                seed_root / "identity" / "seed.key"
            )
            # Permanent target identity is durably created before any invite path exists.
            target_signer = load_or_create_node_signer(
                target_root / "identity" / "node.key"
            )
            ensure_local_tls_material(self.state_root / "tls")
            metadata = {
                "protocol": _METADATA_PROTOCOL,
                "swarm_id": "swarm-" + secrets.token_hex(12),
                "seed_node_id": "seed-" + secrets.token_hex(8),
                "seed_incarnation": "seed-incarnation-" + secrets.token_hex(8),
                "target_node_id": "judge-mac-" + secrets.token_hex(8),
                "peer_class": _PEER_CLASS,
                "consent": approved_consent,
            }
            database = seed_root / "state.sqlite3"
            state = SqliteSeedState(database)
            # The canonical coordinator transactionally binds fresh SQLite metadata.
            SeedCoordinator(
                swarm_id=metadata["swarm_id"],
                seed_node_id=metadata["seed_node_id"],
                seed_url=None,
                signer=seed_signer,
                invite_registry=SqliteInviteRegistry(database),
                incarnation=metadata["seed_incarnation"],
                state=state,
            )
            # Prove target key creation completed before publishing owner state.
            if load_node_signer(
                target_root / "identity" / "node.key"
            ).verification_key_digest != target_signer.verification_key_digest:
                raise DemoAuthorityError("authority_target_identity_invalid")
            _atomic_private_json(self._metadata_path, metadata)
            self._metadata = metadata
            try:
                self._start_runtime()
            except Exception:
                self._close_runtime()
                raise
            return self.status()

    def _network_configuration(self):
        from mycelium_seed.http import canonical_https_origin
        value=_load_private_json(self.state_root/'network.json')
        if value is None:return None
        try:
            if (set(value)!={'protocol','public_origin','port'}
                    or value['protocol']!='mycelium.a13.public_endpoint.v1'
                    or canonical_https_origin(value['public_origin'])!=value['public_origin']
                    or type(value['port']) is not int or not 49152<=value['port']<=65535):
                raise ValueError()
        except (TypeError,ValueError):
            raise DemoAuthorityError('network_configuration_invalid') from None
        return value

    def configure_public_endpoint(self, *, public_origin, port, acknowledged):
        """Store an explicitly approved relay endpoint before swarm creation.

        This configures the canonical identity; it does not open a public
        listener or start a tunnel. The managed relay owner supplies the values.
        """
        from mycelium_seed.http import canonical_https_origin
        with self._lock:
            self._require_started()
            if self._metadata is not None or self._coordinator is not None:
                raise DemoAuthorityError('network_configuration_requires_empty_authority')
            try:
                origin=canonical_https_origin(public_origin)
                if acknowledged is not True or type(port) is not int or not 49152<=port<=65535:
                    raise ValueError()
            except (TypeError,ValueError):
                raise DemoAuthorityError('network_configuration_invalid') from None
            _atomic_private_json(self.state_root/'network.json',{
                'protocol':'mycelium.a13.public_endpoint.v1','public_origin':origin,'port':port})

    def _start_runtime(self) -> None:
        metadata = self._metadata
        if metadata is None:
            raise DemoAuthorityError("authority_swarm_missing")
        database = self._seed_root / "state.sqlite3"
        state = SqliteSeedState(database)
        binding = state.identity_binding()
        if binding != {
            "swarm_id": metadata["swarm_id"],
            "seed_node_id": metadata["seed_node_id"],
            "seed_key_digest": binding["seed_key_digest"],
        }:
            raise DemoAuthorityError("authority_seed_binding_invalid")
        signer = load_bound_seed_signer(
            self._seed_root / "identity",
            expected_digest=binding["seed_key_digest"],
        )
        # Existing metadata makes missing TLS material a fail-closed identity loss.
        tls_root = self.state_root / "tls"
        if not tls_root.is_dir():
            raise DemoAuthorityError("authority_tls_identity_missing")
        material = ensure_local_tls_material(tls_root)
        coordinator = SeedCoordinator(
            swarm_id=binding["swarm_id"],
            seed_node_id=binding["seed_node_id"],
            seed_url=None,
            signer=signer,
            invite_registry=SqliteInviteRegistry(database),
            incarnation=metadata["seed_incarnation"],
            state=state,
        )
        from .managed_offers import ManagedProvisioningMailbox
        control_mailbox=ManagedProvisioningMailbox(self.state_root/'managed-control'/'mailbox.sqlite3',coordinator=coordinator)
        self._control_mailbox=control_mailbox
        network=self._network_configuration()
        server = LocalTLSSeedServer(coordinator, material=material,control_mailbox=control_mailbox,
            **({} if network is None else {'public_origin':network['public_origin'],'port':network['port']})).start()
        self._coordinator = coordinator
        self._seed_server = server
        membership = load_membership_state(self._target_root)
        accepted_state = _load_private_json(self._accepted_path())
        if accepted_state is not None and (
            set(accepted_state) != _ACCEPTED_FIELDS
            or accepted_state.get("protocol") != _ACCEPTED_PROTOCOL
        ):
            raise DemoAuthorityError("authority_state_invalid")
        if accepted_state is not None and membership is None:
            raise DemoAuthorityError("authority_membership_state_invalid")
        # A joined SQLite row without accepted.json is recoverable by replaying
        # the exact persisted join; do not resume and advance its generation yet.
        if membership is None or accepted_state is None:
            self._member = None
            self._seed_client = None
            return
        self._restore_member(membership, material.ca_certificate)

    def _restore_member(self, membership: Mapping[str, Any], ca_certificate: Path) -> None:
        assert self._coordinator is not None and self._seed_server is not None
        metadata = self._metadata
        assert metadata is not None
        if (
            membership["node_id"] != metadata["target_node_id"]
            or membership["swarm_id"] != metadata["swarm_id"]
            or membership["seed_node_id"] != metadata["seed_node_id"]
        ):
            raise DemoAuthorityError("authority_membership_state_invalid")
        signer = load_node_signer(
            self._target_root / "identity" / "node.key",
            endpoint_id=membership["endpoint_id"],
        )
        if signer.verification_key_digest != self._coordinator.member(
            membership["node_id"]
        )["verification_key_digest"]:
            raise DemoAuthorityError("authority_target_identity_invalid")
        canonical_member = self._coordinator.member(membership["node_id"])
        if canonical_member["lifecycle_state"] in {"STOPPING", "STOPPED"}:
            self._member = None
            self._seed_client = pinned_seed_client(
                seed_url=self._coordinator.seed_url,
                transport_url=self._seed_server.base_url,
                swarm_id=membership["swarm_id"],
                seed_key_digest=membership["seed_key_digest"],
                seed_key_records=list(membership["seed_key_records"]),
                ca_certificate=ca_certificate,
                timeout=2,
            )
            return
        restart_count = int(membership["restart_count"]) + 1
        incarnation = next_incarnation(membership["incarnation"], restart_count)
        session = self._new_member_session(signer=signer, incarnation=incarnation)
        client = pinned_seed_client(
            seed_url=self._coordinator.seed_url,
                transport_url=self._seed_server.base_url,
            swarm_id=membership["swarm_id"],
            seed_key_digest=membership["seed_key_digest"],
            seed_key_records=list(membership["seed_key_records"]),
            ca_certificate=ca_certificate,
            timeout=2,
        )
        client.identity(now=self._clock)
        request = session.resume_request(
            previous_generation=membership["membership_generation"],
            previous_incarnation=membership["incarnation"],
            endpoint_addrs=[self._endpoint_address(signer.endpoint_id)],
        )
        acceptance = client.resume(resume_envelope=request)
        accepted = session.accept_resume(
            acceptance,
            seed_key_digest=membership["seed_key_digest"],
        )
        save_membership_state(
            self._target_root,
            {
                **dict(membership),
                "seed_url": self._coordinator.seed_url,
                "incarnation": incarnation,
                "membership_generation": accepted["membership_generation"],
                "restart_count": restart_count,
            },
        )
        self._member = session
        self._seed_client = client
        self._renew_locked()

    @property
    def control_mailbox(self):
        return self._control_mailbox

    def _close_runtime(self) -> None:
        if self._control_mailbox is not None:
            self._control_mailbox.close()
            self._control_mailbox=None
        self._capability_report = None
        server = self._seed_server
        self._seed_server = None
        self._seed_client = None
        self._member = None
        self._coordinator = None
        if server is not None:
            server.close()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._close_runtime()
            self._started = False
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            coordinator = self._coordinator
            metadata = self._metadata
            membership_state = "absent" if metadata is None else "unenrolled"
            membership_generation: int | None = None
            lease_state = "none"
            member_count = 0
            if coordinator is not None:
                members = coordinator.members()
                member_count = len(members)
                if metadata is not None:
                    current = next(
                        (
                            member
                            for member in members
                            if member["node_id"] == metadata["target_node_id"]
                        ),
                        None,
                    )
                    if current is not None:
                        membership_generation = int(current["generation"])
                        if current["lifecycle_state"] in {"STOPPING", "STOPPED"}:
                            membership_state = "revoked"
                        else:
                            membership_state = "current"
                        lease_state = (
                            "fresh"
                            if float(current["lease_expires_at"]) > self._clock()
                            else "expired"
                        )
            elif metadata is not None:
                membership = load_membership_state(self._target_root)
                if membership is not None:
                    membership_state = "retained"
                    membership_generation = membership["membership_generation"]
                    lease_state = "unknown"
            capability = self._capability_report
            capability_state = "not_reported"
            if capability is not None:
                current_report = (
                    membership_state == "current"
                    and lease_state == "fresh"
                    and capability["generation"] == membership_generation
                    and capability["expires_at"] > self._clock()
                )
                capability_state = "self_report_accepted" if current_report else "stale"
                if not current_report:
                    capability = None
            return {
                "protocol": _STATUS_PROTOCOL,
                "started": self._started,
                "swarm_created": metadata is not None,
                "local_tls": (
                    "pinned_loopback" if self._seed_server is not None else "inactive"
                ),
                "membership_state": membership_state,
                "member_count": member_count,
                "capability_state": capability_state,
                "capability": dict(capability) if capability is not None else None,
                "membership_generation": membership_generation,
                "lease_state": lease_state,
                "contribution_enabled": (
                    False
                    if metadata is None
                    else bool(metadata["consent"]["contribution_enabled"])
                ),
                "route_ready": False,
            }

    def plan_assignments(
        self, *, prepared: PreparedSource, node_ids: tuple[str, str],
        split_layer: int, cache_roots: Mapping[str, str],
    ) -> dict[str, Any]:
        """Compile source-only assignments, not resources, load proofs or offers."""
        from layer_assignment import compile_layer_assignments
        import model_manifest as mm

        with self._lock:
            self._require_started()
            if self._coordinator is None or self._metadata is None:
                raise DemoAuthorityError('authority_swarm_missing')
            now = self._clock()
            try:
                manifest = json.loads(json.dumps(dict(prepared.manifest), allow_nan=False))
                if (
                    not isinstance(node_ids, (list, tuple)) or len(node_ids) != 2
                    or len(set(node_ids)) != 2
                    or self._metadata['target_node_id'] not in node_ids
                    or set(cache_roots) != set(node_ids)
                    or type(split_layer) is not int
                    or not 0 < split_layer < manifest['num_layers']
                    or not mm.verify_manifest_digest(manifest)
                    or mm.manifest_digest_ref(manifest) != prepared.manifest_digest
                    or manifest['model_id'] != prepared.model.model_id
                    or manifest['resolved_commit'] != prepared.model.resolved_commit
                    or prepared.model.runtime_backend != 'mlx'
                ):
                    raise ValueError('invalid_preparation')
                roster = {m['node_id']:m for m in self._coordinator.members()}
                members = []
                for node in node_ids:
                    member = roster[node]
                    if (
                        member['peer_class'] != 'mac_mlx_iroh'
                        or member['lifecycle_state'] not in {'NEW','CONFIGURED','RUNNING'}
                        or member['lease_expires_at'] <= now
                    ):
                        raise ValueError('member_not_current')
                    members.append({key:member[key] for key in (
                        'node_id','endpoint_id','generation','lease_expires_at')})
                route = {
                    'protocol':'mycelium.manual_provisioning_route.v1', 'ok':True,
                    'claim_boundary':'Source-only two-member assignment proposal; not physical host or serving evidence.',
                    'model':{key:manifest[key] for key in ('model_id','resolved_commit','num_layers')},
                    'route':[], 'node_order':list(node_ids),
                }
                route['model']['manifest_digest'] = prepared.manifest_digest
                for node, start, end in ((node_ids[0],0,split_layer),
                                          (node_ids[1],split_layer,manifest['num_layers'])):
                    route['route'].append({'node_id':node,'range':{
                        'start_layer':start,'end_layer_exclusive':end,'layer_count':end-start}})
                deployment_id = str(uuid.uuid4())
                assignments = compile_layer_assignments(
                    route_plan=route, manifest=manifest, deployment_id=deployment_id,
                    deployment_epoch=1, cache_roots=dict(cache_roots),
                    runtime_by_node={node:{'backend':prepared.model.runtime_backend,
                        'dtype':prepared.model.runtime_dtype,
                        'quantization':prepared.model.runtime_quantization} for node in node_ids},
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise DemoAuthorityError('assignment_preparation_invalid') from exc
            proposal = {
                'protocol':'mycelium.a13_assignment_preparation.v1',
                'state':'assignments_compiled_not_provisioned','route_ready':False,
                'swarm_id':self._coordinator.swarm_id,
                'seed_key_digest':self._coordinator.signer.verification_key_digest,
                'deployment_id':deployment_id, 'created_at':now,
                'members':members,'manual_route':route,'assignments':assignments,
            }
            root = _private_directory(self.state_root/'preparations',create=True)
            path = root/(deployment_id+'.json')
            _atomic_private_json(path,proposal)
            return {'plan_path':str(path),'state':proposal['state'],'route_ready':False}

    def prepare_serving(
        self, *, prepared: PreparedSource, operator_plan: Path | str,
        session_factory=None,
    ) -> RegistryLaunch:
        """Bind source-only preparation to this owner; never load or select a route.

        The complete native plan still needs to be constructed from enrolled
        identities and authorized artifacts. Execution has a separate grant and
        physical qualification boundary; this method establishes neither.
        """
        from .serving import prepare_registry_launch

        with self._lock:
            self._require_started()
            status = self.status()
            if status['membership_state'] != 'current' or status['lease_state'] != 'fresh':
                raise DemoAuthorityError('authority_member_not_current')
            return prepare_registry_launch(
                prepared=prepared,
                operator_plan=operator_plan,
                private_state_root=self.state_root / 'serving',
                seed_state_root=self._seed_root,
                session_factory=session_factory,
            )

    def _target_signer(self):
        metadata = self._metadata
        if metadata is None:
            raise DemoAuthorityError("authority_swarm_missing")
        return load_node_signer(self._target_root / "identity" / "node.key")

    def _pending_path(self) -> Path:
        return self._target_root / "pairing.pending.json"

    def _handoff_path(self) -> Path:
        return self._target_root / "pairing.handoff.json"

    def _join_pending_path(self) -> Path:
        return self._target_root / "join.pending.json"

    def _accepted_path(self) -> Path:
        return self._target_root / "accepted.json"

    def _load_pending_pairing(self) -> dict[str, Any]:
        value = _load_private_json(self._pending_path())
        if value is None or set(value) != _PENDING_FIELDS or value.get("protocol") != _PENDING_PROTOCOL:
            raise DemoAuthorityError("authority_pairing_state_invalid")
        try:
            request = verify_pairing_request(value["request"], now=self._clock())
        except PairingError as exc:
            if exc.code != "pairing_request_expired":
                raise DemoAuthorityError("authority_pairing_state_invalid") from exc
            request = dict(value["request"])
        return {
            "request": request,
            "recipient_private_key": _decode_private(value["recipient_private_key"]),
        }

    def pair_request(self) -> dict[str, Any]:
        with self._lock:
            self._require_started()
            if self._coordinator is None or self._metadata is None:
                raise DemoAuthorityError("authority_swarm_missing")
            if load_membership_state(self._target_root) is not None:
                raise DemoAuthorityError("authority_target_already_enrolled")
            existing = _load_private_json(self._pending_path())
            if existing is not None:
                pending = self._load_pending_pairing()
                request = pending["request"]
                if float(request["expires_at"]) >= self._clock():
                    return dict(request)
                self._pending_path().unlink()
            signer = self._target_signer()
            request, private_key = new_pairing_request(
                signer=signer,
                node_id=self._metadata["target_node_id"],
                peer_class=self._metadata["peer_class"],
                now=self._clock(),
                ttl_seconds=300,
            )
            _atomic_private_json(
                self._pending_path(),
                {
                    "protocol": _PENDING_PROTOCOL,
                    "request": request,
                    "recipient_private_key": _b64_private(private_key),
                },
            )
            return request

    def _approval_path(self, request_digest: str) -> Path:
        approvals = _private_directory(self._seed_root / "approvals", create=True)
        return approvals / f"{request_digest.removeprefix('sha256:')}.json"

    def approve_pairing(
        self,
        request: Mapping[str, Any],
        ttl_seconds: int,
        quota: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self._require_started()
            if self._coordinator is None:
                raise DemoAuthorityError("authority_swarm_missing")
            now = self._clock()
            try:
                verified_request = verify_pairing_request(request, now=now)
                approved_quota = validate_quota(quota)
            except PairingError as exc:
                raise DemoAuthorityError(exc.code) from exc
            if (
                isinstance(ttl_seconds, bool)
                or not isinstance(ttl_seconds, int)
                or ttl_seconds <= 0
                or now + ttl_seconds > float(verified_request["expires_at"])
            ):
                raise DemoAuthorityError("pairing_approval_ttl_invalid")
            request_digest = _digest(verified_request)
            approval_path = self._approval_path(request_digest)
            quota_digest = _digest(approved_quota)
            existing = _load_private_json(approval_path)
            if existing is not None:
                if (
                    existing.get("protocol") != _APPROVAL_PROTOCOL
                    or existing.get("request_digest") != request_digest
                ):
                    raise DemoAuthorityError("authority_state_invalid")
                if (
                    existing.get("ttl_seconds") != ttl_seconds
                    or existing.get("quota_digest") != quota_digest
                ):
                    raise DemoAuthorityError("pairing_approval_changed")
                handoff = existing.get("handoff")
                if not isinstance(handoff, dict):
                    raise DemoAuthorityError("authority_state_invalid")
                return handoff
            bundle = self._coordinator.mint_invite(
                nonce=verified_request["pairing_nonce"],
                ttl_seconds=ttl_seconds,
            )
            try:
                handoff = approve_pairing_request(
                    request=verified_request,
                    invite_bundle=bundle,
                    quota=approved_quota,
                    now=now,
                )
            except PairingError as exc:
                raise DemoAuthorityError(exc.code) from exc
            _atomic_private_json(
                approval_path,
                {
                    "protocol": _APPROVAL_PROTOCOL,
                    "request_digest": request_digest,
                    "ttl_seconds": ttl_seconds,
                    "quota_digest": quota_digest,
                    "handoff": handoff,
                },
            )
            return handoff

    def _new_member_session(
        self,
        *,
        signer,
        incarnation: str,
        first_message_id: str | None = None,
    ) -> NodeMembershipSession:
        metadata = self._metadata
        if metadata is None:
            raise DemoAuthorityError("authority_swarm_missing")
        first = [first_message_id] if first_message_id is not None else []

        def identifiers() -> str:
            if first:
                return str(first.pop(0))
            return "member-message-" + uuid.uuid4().hex

        return NodeMembershipSession(
            node_id=metadata["target_node_id"],
            swarm_id=metadata["swarm_id"],
            seed_node_id=metadata["seed_node_id"],
            signer=signer,
            incarnation=incarnation,
            software_version=_SOFTWARE_VERSION,
            peer_class=metadata["peer_class"],
            runtime_capability=_RUNTIME_CAPABILITY,
            clock=self._clock,
            id_source=identifiers,
        )

    @staticmethod
    def _endpoint_address(endpoint_id: str) -> str:
        return f"https://{endpoint_id}.invalid/control"

    def _membership_result(self, member: Mapping[str, Any]) -> dict[str, Any]:
        state = (
            "revoked"
            if member["lifecycle_state"] in {"STOPPING", "STOPPED"}
            else "current"
        )
        return {
            "protocol": _MEMBERSHIP_RESULT_PROTOCOL,
            "membership_state": state,
            "membership_generation": int(member["generation"]),
            "lifecycle_state": member["lifecycle_state"],
            "route_ready": False,
        }

    def accept_pairing(self, handoff: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_started()
            if self._coordinator is None or self._seed_server is None:
                raise DemoAuthorityError("authority_swarm_missing")
            if not isinstance(handoff, Mapping):
                raise DemoAuthorityError("pairing_handoff_invalid")
            handoff_value = dict(handoff)
            handoff_digest = _digest(handoff_value)
            accepted_state = _load_private_json(self._accepted_path())
            if accepted_state is not None:
                if (
                    set(accepted_state) != _ACCEPTED_FIELDS
                    or accepted_state.get("protocol") != _ACCEPTED_PROTOCOL
                ):
                    raise DemoAuthorityError("authority_state_invalid")
                if accepted_state.get("handoff_digest") != handoff_digest:
                    raise DemoAuthorityError("pairing_handoff_changed")
                if load_membership_state(self._target_root) is None:
                    raise DemoAuthorityError("authority_membership_state_invalid")
                metadata = self._metadata
                assert metadata is not None
                return self._membership_result(
                    self._coordinator.member(metadata["target_node_id"])
                )
            _atomic_private_json(self._handoff_path(), handoff_value)
            pending = self._load_pending_pairing()
            request = pending["request"]
            try:
                recovered = decrypt_invitation_handoff(
                    request=request,
                    handoff=handoff_value,
                    recipient_private_key=pending["recipient_private_key"],
                    now=self._clock(),
                )
            except PairingError as exc:
                raise DemoAuthorityError(exc.code) from exc
            bundle = recovered["invite_bundle"]
            verified = verify_invite_bundle(bundle, now=self._clock())
            signer = self._target_signer()
            if signer.verification_key_digest != recovered["identity_key_digest"]:
                raise DemoAuthorityError("authority_target_identity_invalid")
            client = pinned_seed_client_from_invite(
                bundle,
                now=self._clock(),
                ca_certificate=self._seed_server.material.ca_certificate,
                transport_url=self._seed_server.base_url,
                timeout=2,
            )
            identity = client.identity(now=self._clock)
            if identity["seed_url"] != self._coordinator.seed_url:
                raise DemoAuthorityError("authority_seed_binding_invalid")
            checkpoint = _load_private_json(self._join_pending_path())
            if checkpoint is None:
                incarnation = "target-incarnation-" + secrets.token_hex(8)
                join_message_id = "join-message-" + uuid.uuid4().hex
                session = self._new_member_session(
                    signer=signer,
                    incarnation=incarnation,
                    first_message_id=join_message_id,
                )
                join_envelope = session.join_request(
                    invite_nonce=recovered["pairing_nonce"],
                    endpoint_addrs=[self._endpoint_address(signer.endpoint_id)],
                )
                checkpoint = {
                    "protocol": _JOIN_PENDING_PROTOCOL,
                    "handoff_digest": handoff_digest,
                    "incarnation": incarnation,
                    "join_message_id": join_message_id,
                    "join_envelope": join_envelope,
                }
                _atomic_private_json(self._join_pending_path(), checkpoint)
            else:
                if (
                    checkpoint.get("protocol") != _JOIN_PENDING_PROTOCOL
                    or checkpoint.get("handoff_digest") != handoff_digest
                    or not isinstance(checkpoint.get("incarnation"), str)
                    or not isinstance(checkpoint.get("join_message_id"), str)
                    or not isinstance(checkpoint.get("join_envelope"), dict)
                ):
                    raise DemoAuthorityError("authority_join_recovery_invalid")
                session = self._new_member_session(
                    signer=signer,
                    incarnation=checkpoint["incarnation"],
                    first_message_id=checkpoint["join_message_id"],
                )
                reproduced = session.join_request(
                    invite_nonce=recovered["pairing_nonce"],
                    endpoint_addrs=[self._endpoint_address(signer.endpoint_id)],
                )
                if canonical_json_bytes(reproduced) != canonical_json_bytes(
                    checkpoint["join_envelope"]
                ):
                    raise DemoAuthorityError("authority_join_recovery_invalid")
                join_envelope = reproduced
            acceptance = client.join(
                invite_token=bundle["token"],
                join_envelope=join_envelope,
            )
            accepted = session.accept_join(
                acceptance,
                seed_key_digest=verified["seed_key_digest"],
            )
            # Persist canonical member recovery before publishing accepted.json.
            # start() deliberately does not resume the generation in this narrow
            # crash window, so the exact join checkpoint remains replayable.
            save_membership_state(
                self._target_root,
                {
                    "protocol": MEMBERSHIP_STATE_PROTOCOL,
                    "node_id": recovered["node_id"],
                    "swarm_id": verified["payload"]["swarm_id"],
                    "seed_node_id": identity["seed_node_id"],
                    "seed_url": self._coordinator.seed_url,
                    "seed_key_digest": verified["seed_key_digest"],
                    "seed_key_records": list(verified["seed_key_records"]),
                    "endpoint_id": signer.endpoint_id,
                    "incarnation": checkpoint["incarnation"],
                    "membership_generation": accepted["membership_generation"],
                    "restart_count": 0,
                },
            )
            _atomic_private_json(
                self._accepted_path(),
                {
                    "protocol": _ACCEPTED_PROTOCOL,
                    "handoff_digest": handoff_digest,
                    "quota": recovered["quota"],
                    "accepted_at": self._clock(),
                },
            )
            for path in (
                self._pending_path(),
                self._handoff_path(),
                self._join_pending_path(),
            ):
                path.unlink(missing_ok=True)
            self._member = session
            self._seed_client = client
            try:
                return self._renew_locked()
            except (DemoAuthorityError, NodeMembershipError, SeedHTTPError):
                # Enrollment is already durably accepted; callers can renew later.
                return self._membership_result(
                    self._coordinator.member(recovered["node_id"])
                )

    def _renew_locked(self) -> dict[str, Any]:
        metadata = self._metadata
        if self._coordinator is not None and metadata is not None:
            try:
                canonical_member = self._coordinator.member(metadata["target_node_id"])
            except SeedCoordinatorError:
                canonical_member = None
            if canonical_member is not None and canonical_member["lifecycle_state"] in {
                "STOPPING",
                "STOPPED",
            }:
                raise DemoAuthorityError("authority_member_revoked")
        if self._member is None or self._seed_client is None or self._coordinator is None:
            if metadata is not None:
                try:
                    current = self._coordinator.member(metadata["target_node_id"])
                except SeedCoordinatorError:
                    current = None
                if current is not None and current["lifecycle_state"] in {
                    "STOPPING",
                    "STOPPED",
                }:
                    raise DemoAuthorityError("authority_member_revoked")
            raise DemoAuthorityError("authority_member_missing")
        heartbeat = self._member.heartbeat(lifecycle_state="RUNNING", active_requests=0)
        if heartbeat is None:
            raise DemoAuthorityError("authority_renewal_unavailable")
        renewal = self._seed_client.send_member_message(
            heartbeat,
            now=self._clock,
        )
        self._member.accept_lease_renewal(
            renewal,
            heartbeat_message_id=heartbeat["message"]["message_id"],
        )
        metadata = self._metadata
        assert metadata is not None
        return self._membership_result(
            self._coordinator.member(metadata["target_node_id"])
        )

    def renew(self) -> dict[str, Any]:
        with self._lock:
            self._require_started()
            return self._renew_locked()

    def report_capability(self) -> dict[str, Any]:
        """Observe this Mac and obtain a verified seed receipt; never qualify it."""
        from .capability import signed_mac_report

        with self._lock:
            self._require_started()
            if self._member is None or self._seed_client is None:
                raise DemoAuthorityError("authority_member_missing")
            status = self.status()
            if status["membership_state"] != "current" or status["lease_state"] != "fresh":
                raise DemoAuthorityError("authority_member_not_current")
            envelope = signed_mac_report(self._member, self.state_root)
            receipt = self._seed_client.send_member_message(envelope, now=self._clock)
            message = envelope["message"]
            # The canonical client verifies the pinned seed signature and exact ID.
            reports = _private_directory(self._target_root / "capability-reports", create=True)
            _atomic_private_json(reports / f"{uuid.uuid4().hex}.json", {
                "protocol": "mycelium.demo_capability_evidence.v1",
                "report": envelope, "receipt": receipt,
            })
            self._capability_report = {
                "state": "self_report_accepted",
                "generation": message["generation"],
                "backends": list(message["backends"]),
                "precisions": list(message["precisions"]),
                "memory_bytes": message["memory_bytes"],
                "available_storage_bytes": message["available_storage_bytes"],
                "expires_at": message["expires_at"],
                "independently_qualified": False,
            }
            return dict(self._capability_report)

    def revoke(self, node_id: str, generation: int) -> dict[str, Any]:
        with self._lock:
            self._require_started()
            if self._coordinator is None:
                raise DemoAuthorityError("authority_swarm_missing")
            member = self._coordinator.advance_member_generation(
                node_id=node_id,
                expected_generation=generation,
                lifecycle_state="STOPPED",
            )
            return {
                "protocol": _REVOCATION_RESULT_PROTOCOL,
                "previous_generation": generation,
                "generation": int(member["generation"]),
                "lifecycle_state": member["lifecycle_state"],
                "route_ready": False,
            }


__all__ = ["DemoAuthority", "DemoAuthorityError"]
