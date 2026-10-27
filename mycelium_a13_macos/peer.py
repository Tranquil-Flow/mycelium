# SPDX-License-Identifier: AGPL-3.0-only
"""Target-owned remote Mac enrollment and membership lifecycle.

The target owns only its identity, encrypted pairing recovery, consent, and
canonical durable membership state.  Seed authority and SQLite remain remote.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from mycelium_internet.bootstrap import canonical_https_origin
from mycelium_invite import InviteError, verify_invite_bundle
from mycelium_membership import CAPABILITY_REPORT_PROTOCOL
from mycelium_node import NodeMembershipSession
from mycelium_node.durable_membership import (
    DurableMembershipError,
    PROTOCOL as MEMBERSHIP_STATE_PROTOCOL,
    load_membership_state,
    next_incarnation,
    save_membership_state,
)
from mycelium_node.identity import (
    NodeIdentityError,
    load_node_signer,
    load_or_create_node_signer,
)
from mycelium_node.membership import NodeMembershipError
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError

from .authority import (
    DemoAuthorityError,
    _atomic_private_json,
    _b64_private,
    _consent,
    _decode_private,
    _digest,
    _load_private_json,
    _private_directory,
)
from .pairing import (
    PairingError,
    decrypt_invitation_handoff,
    new_pairing_request,
    verify_pairing_request,
)

_STATUS_PROTOCOL = "mycelium.remote_mac_member_status.v1"
_CONSENT_PROTOCOL = "mycelium.remote_mac_consent.v1"
_PENDING_PROTOCOL = "mycelium.remote_mac_pairing_pending.v1"
_JOIN_PENDING_PROTOCOL = "mycelium.remote_mac_join_pending.v1"
_RESUME_PENDING_PROTOCOL = "mycelium.remote_mac_resume_pending.v1"
_ACCEPTED_PROTOCOL = "mycelium.remote_mac_accepted.v1"
_REJOIN_PROTOCOL = "mycelium.remote_mac_rejoin_pending.v1"
_DISPOSITION_PROTOCOL = "mycelium.remote_mac_membership_disposition.v1"
_CAPABILITY_EVIDENCE_PROTOCOL = "mycelium.remote_mac_capability_evidence.v1"
_CAPABILITY_RESULT_PROTOCOL = "mycelium.remote_mac_capability_result.v1"
_MEMBERSHIP_RESULT_PROTOCOL = "mycelium.remote_mac_membership_result.v1"
_SOFTWARE_VERSION = "mycelium-a13-macos-demo/1"
_PEER_CLASS = "mac_mlx_iroh"
_RUNTIME_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}
_PENDING_FIELDS = frozenset(
    {"protocol", "request", "recipient_private_key", "consent_digest"}
)
_ACCEPTED_FIELDS = frozenset(
    {"protocol", "handoff_digest", "quota", "accepted_at"}
)
_TERMINAL_MEMBERSHIP_STATES = frozenset({"expired", "revoked", "stale", "unknown"})
#: Lapsed memberships a new, owner-approved invite may replace (identity and model cache stay).  A membership the
#: owner revoked is not among them: that is the owner's decision, not a lapse.
_REJOINABLE_STATES = frozenset({"expired", "stale", "unknown"})
_REJOIN_STATES_FOR_DISPOSITION = _REJOINABLE_STATES


class RemoteMacMemberError(RuntimeError):
    """Stable target-side error that never embeds private pairing material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


TestSeedClientFactory = Callable[..., Any]


class RemoteMacMember:
    """A target-only Mac member using recipient pairing and canonical HTTPS.

    ``_test_seed_client_factory`` is solely for deterministic local TLS tests.
    When it is absent, the ordinary :class:`SeedHTTPClient` uses the system
    Web-PKI trust store and its no-redirect canonical JSON transport.
    """

    #: The registered peer class and runtime this member declares.  A managed
    #: host for another platform subclasses and overrides these three.
    PEER_CLASS = _PEER_CLASS
    RUNTIME_CAPABILITY = _RUNTIME_CAPABILITY
    SOFTWARE_VERSION = _SOFTWARE_VERSION

    def __init__(
        self,
        state_root: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        timeout: float = 10.0,
        _test_seed_client_factory: TestSeedClientFactory | None = None,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock is invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("timeout is invalid")
        if _test_seed_client_factory is not None and not callable(
            _test_seed_client_factory
        ):
            raise ValueError("test seed client factory is invalid")
        self.state_root = Path(os.path.abspath(Path(state_root).expanduser()))
        self._clock_source = clock
        self._timeout = float(timeout)
        self._test_seed_client_factory = _test_seed_client_factory
        self._lock = threading.RLock()
        self._started = False
        self._signer = None
        self._member: NodeMembershipSession | None = None
        self._seed_client: Any | None = None

    @property
    def member(self) -> NodeMembershipSession | None:
        return self._member

    @property
    def seed_client(self) -> Any | None:
        return self._seed_client

    @property
    def _identity_path(self) -> Path:
        return self.state_root / "identity" / "node.key"

    @property
    def _consent_path(self) -> Path:
        return self.state_root / "consent.json"

    @property
    def _pending_path(self) -> Path:
        return self.state_root / "pairing.pending.json"

    @property
    def _handoff_path(self) -> Path:
        return self.state_root / "pairing.handoff.json"

    @property
    def _join_pending_path(self) -> Path:
        return self.state_root / "join.pending.json"

    @property
    def _resume_pending_path(self) -> Path:
        return self.state_root / "resume.pending.json"

    @property
    def _rejoin_path(self) -> Path:
        return self.state_root / "rejoin.pending.json"

    @property
    def _accepted_path(self) -> Path:
        return self.state_root / "accepted.json"

    @property
    def _disposition_path(self) -> Path:
        return self.state_root / "membership.disposition.json"

    @property
    def _capability_path(self) -> Path:
        return self.state_root / "capability.evidence.json"

    def _clock(self) -> float:
        value = self._clock_source()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise RemoteMacMemberError("peer_clock_invalid")
        return float(value)

    def _require_started(self) -> None:
        if not self._started:
            raise RemoteMacMemberError("peer_not_started")

    def _node_id(self) -> str:
        if self._signer is None:
            raise RemoteMacMemberError("peer_identity_missing")
        digest = self._signer.verification_key_digest.removeprefix("sha256:")
        return "remote-mac-" + digest[:24]

    def _endpoint_address(self) -> str:
        if self._signer is None:
            raise RemoteMacMemberError("peer_identity_missing")
        return f"https://{self._signer.endpoint_id}.invalid/control"

    def _has_post_identity_state(self) -> bool:
        paths = (
            self._consent_path,
            self._pending_path,
            self._handoff_path,
            self._join_pending_path,
            self._resume_pending_path,
            self._accepted_path,
            self._disposition_path,
            self._capability_path,
            self._rejoin_path,
            self.state_root / "membership.state.json",
        )
        return any(path.exists() or path.is_symlink() for path in paths)

    def _load_consent(self) -> dict[str, bool] | None:
        value = _load_private_json(self._consent_path)
        if value is None:
            return None
        if set(value) != {"protocol", "consent"} or value.get(
            "protocol"
        ) != _CONSENT_PROTOCOL:
            raise RemoteMacMemberError("peer_state_invalid")
        try:
            return _consent(value["consent"])
        except (DemoAuthorityError, KeyError, TypeError) as exc:
            raise RemoteMacMemberError("peer_state_invalid") from exc

    def _validated_consent(self, value: Mapping[str, Any]) -> dict[str, bool]:
        try:
            return _consent(value)
        except DemoAuthorityError as exc:
            raise RemoteMacMemberError("peer_consent_invalid") from exc

    def _save_consent(self, consent: Mapping[str, Any]) -> dict[str, bool]:
        approved = self._validated_consent(consent)
        existing = self._load_consent()
        if existing is not None:
            if existing != approved:
                raise RemoteMacMemberError("peer_consent_changed")
            return existing
        _atomic_private_json(
            self._consent_path,
            {"protocol": _CONSENT_PROTOCOL, "consent": approved},
        )
        return approved

    def _load_accepted(self) -> dict[str, Any] | None:
        value = _load_private_json(self._accepted_path)
        if value is None:
            return None
        if set(value) != _ACCEPTED_FIELDS or value.get("protocol") != _ACCEPTED_PROTOCOL:
            raise RemoteMacMemberError("peer_state_invalid")
        return value

    def _load_disposition(self) -> str | None:
        value = _load_private_json(self._disposition_path)
        if value is None:
            return None
        if (
            set(value) != {"protocol", "membership_state", "observed_at"}
            or value.get("protocol") != _DISPOSITION_PROTOCOL
            or value.get("membership_state") not in _TERMINAL_MEMBERSHIP_STATES
            or isinstance(value.get("observed_at"), bool)
            or not isinstance(value.get("observed_at"), (int, float))
        ):
            raise RemoteMacMemberError("peer_state_invalid")
        return str(value["membership_state"])

    def _record_disposition(self, state: str) -> None:
        if state not in _TERMINAL_MEMBERSHIP_STATES:
            return
        _atomic_private_json(
            self._disposition_path,
            {
                "protocol": _DISPOSITION_PROTOCOL,
                "membership_state": state,
                "observed_at": self._clock(),
            },
        )

    def _clear_disposition(self) -> None:
        self._disposition_path.unlink(missing_ok=True)

    def _translate(self, exc: BaseException) -> RemoteMacMemberError:
        code = getattr(exc, "code", "peer_operation_failed")
        mapping = {
            "seed_http_unreachable": "peer_transport_unreachable",
            "bootstrap_unavailable": "peer_transport_unreachable",
            "seed_resume_member_revoked": "peer_member_revoked",
            "seed_member_lifecycle_ineligible": "peer_member_revoked",
            "revoked": "peer_member_revoked",
            "seed_resume_generation_stale": "peer_generation_stale",
            "seed_member_generation_stale": "peer_generation_stale",
            "generation_stale": "peer_generation_stale",
            "seed_member_unknown": "peer_member_unknown",
            "seed_member_lease_expired": "peer_member_expired",
            "membership_lease_expired": "peer_member_expired",
            "lease_expired": "peer_member_expired",
        }
        translated = mapping.get(str(code), str(code))
        disposition = {
            "peer_member_revoked": "revoked",
            "peer_generation_stale": "stale",
            "peer_member_unknown": "unknown",
            "peer_member_expired": "expired",
        }.get(translated)
        if disposition is not None:
            self._record_disposition(disposition)
        return RemoteMacMemberError(translated)

    def start(self) -> dict[str, Any]:
        """Create/load target identity, then resume accepted membership if present."""

        with self._lock:
            if self._started:
                return self.status()
            try:
                _private_directory(self.state_root, create=True)
                if (
                    self._has_post_identity_state()
                    and not self._identity_path.exists()
                ):
                    raise RemoteMacMemberError("peer_identity_missing")
                self._signer = load_or_create_node_signer(self._identity_path)
                membership = load_membership_state(self.state_root)
                accepted = self._load_accepted()
                join_pending = _load_private_json(self._join_pending_path)
                if accepted is not None and membership is None:
                    raise RemoteMacMemberError("peer_membership_state_invalid")
                if membership is not None and accepted is None and join_pending is None:
                    raise RemoteMacMemberError("peer_membership_state_invalid")
                self._started = True
                if membership is not None and accepted is not None:
                    # Durable acceptance makes pairing recovery unnecessary.
                    # Complete cleanup after a crash between accepted.json's
                    # atomic replace and the ordinary unlink sequence.  A re-join in flight keeps its files.
                    if not self._rejoin_path.exists():
                        for path in (
                            self._pending_path,
                            self._handoff_path,
                            self._join_pending_path,
                        ):
                            path.unlink(missing_ok=True)
                    self._resume(membership)
                return self.status()
            except RemoteMacMemberError as exc:
                # A lapsed membership keeps the peer started (identity loaded, no member): the only thing it may do
                # is ask for a new pairing, which the owner still has to approve (``pair_request``).
                self._started = (
                    exc.code in {"peer_member_expired", "peer_generation_stale", "peer_member_unknown"}
                    and self._signer is not None
                )
                self._member = None
                self._seed_client = None
                raise
            except (
                DemoAuthorityError,
                DurableMembershipError,
                NodeIdentityError,
                NodeMembershipError,
                SeedHTTPError,
                ValueError,
            ) as exc:
                self._started = False
                self._member = None
                self._seed_client = None
                raise self._translate(exc) from exc

    def stop(self) -> dict[str, Any]:
        """Stop network membership activity while retaining identity and evidence."""

        with self._lock:
            self._member = None
            self._seed_client = None
            self._started = False
            return self.status()

    def status(self) -> dict[str, Any]:
        """Return a closed projection with membership distinct from serving."""

        with self._lock:
            identity_present = self._identity_path.is_file()
            consent = self._load_consent() if self.state_root.exists() else None
            membership = (
                load_membership_state(self.state_root)
                if self.state_root.exists()
                else None
            )
            accepted = self._load_accepted() if self.state_root.exists() else None
            disposition = (
                self._load_disposition() if self.state_root.exists() else None
            )
            pairing_state = "not_requested"
            if accepted is not None:
                pairing_state = "accepted"
            elif self._handoff_path.exists():
                pairing_state = "handoff_retained"
            elif self._pending_path.exists():
                pairing_state = "pending"
            membership_state = "absent"
            generation = None
            lease_state = "none"
            if membership is not None:
                generation = int(membership["membership_generation"])
                membership_state = "retained"
                lease_state = "unknown"
            if self._member is not None:
                generation = self._member.generation
                lease = self._member.lease_expires_at
                if lease is not None and self._clock() >= lease:
                    membership_state = "expired"
                    lease_state = "expired"
                else:
                    membership_state = "current"
                    lease_state = "fresh"
            if disposition is not None:
                membership_state = disposition
                if disposition == "expired":
                    lease_state = "expired"
            return {
                "protocol": _STATUS_PROTOCOL,
                "started": self._started,
                "identity_state": "present" if identity_present else "absent",
                "pairing_state": pairing_state,
                "membership_state": membership_state,
                "membership_generation": generation,
                "lease_state": lease_state,
                "cooperative_peer_visibility_acknowledged": bool(
                    consent
                    and consent["cooperative_peer_visibility_acknowledged"]
                ),
                "resource_use_acknowledged": bool(
                    consent and consent["resource_use_acknowledged"]
                ),
                "contribution_enabled": bool(
                    consent and consent["contribution_enabled"]
                ),
                "capability_state": (
                    "reported" if self._capability_path.is_file() else "absent"
                ),
                "serving_state": "not_yet_serving",
                "route_ready": False,
            }

    def _load_pending(self) -> dict[str, Any]:
        value = _load_private_json(self._pending_path)
        if (
            value is None
            or set(value) != _PENDING_FIELDS
            or value.get("protocol") != _PENDING_PROTOCOL
        ):
            raise RemoteMacMemberError("peer_pairing_state_invalid")
        try:
            request = verify_pairing_request(value["request"], now=self._clock())
        except PairingError as exc:
            if exc.code != "pairing_request_expired":
                raise RemoteMacMemberError("peer_pairing_state_invalid") from exc
            request = dict(value["request"])
        private_key = _decode_private(value.get("recipient_private_key"))
        if value.get("consent_digest") != _digest(self._load_consent() or {}):
            raise RemoteMacMemberError("peer_pairing_state_invalid")
        return {"request": request, "recipient_private_key": private_key}

    def pair_request(
        self,
        consent: Mapping[str, Any],
        *,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Return one exact retryable target request after explicit consent."""

        with self._lock:
            self._require_started()
            rejoining = load_membership_state(self.state_root) is not None
            if rejoining and not self._rejoin_allowed():
                raise RemoteMacMemberError("peer_already_enrolled")
            approved = self._validated_consent(consent)
            existing_consent = self._load_consent()
            if existing_consent is not None and existing_consent != approved:
                raise RemoteMacMemberError("peer_consent_changed")
            existing = _load_private_json(self._pending_path)
            if existing is not None:
                pending = self._load_pending()
                request = pending["request"]
                if float(request["expires_at"]) >= self._clock():
                    return dict(request)
                self._pending_path.unlink()
            approved = self._save_consent(approved)
            assert self._signer is not None
            if rejoining and not self._rejoin_path.exists():
                _atomic_private_json(self._rejoin_path, {"protocol": _REJOIN_PROTOCOL, "requested_at": self._clock()})
            try:
                request, private_key = new_pairing_request(
                    signer=self._signer,
                    node_id=self._node_id(),
                    peer_class=self.PEER_CLASS,
                    now=self._clock(),
                    ttl_seconds=ttl_seconds,
                    nonce="pairing-" + secrets.token_hex(24),
                )
            except PairingError as exc:
                raise self._translate(exc) from exc
            _atomic_private_json(
                self._pending_path,
                {
                    "protocol": _PENDING_PROTOCOL,
                    "request": request,
                    "recipient_private_key": _b64_private(private_key),
                    "consent_digest": _digest(approved),
                },
            )
            return request

    def _rejoin_allowed(self) -> bool:
        """A lapsed membership (lease ran out, or the owner no longer accepts this generation) may be replaced."""

        if self._rejoin_path.exists():
            return True  # a re-join already requested (a retry returns the same request)
        disposition = self._load_disposition()
        if disposition is not None:
            return disposition in _REJOIN_STATES_FOR_DISPOSITION
        member = self._member
        if member is None:
            return False
        lease = member.lease_expires_at
        return lease is not None and self._clock() >= lease

    def _make_client(
        self,
        *,
        bundle: Mapping[str, Any] | None = None,
        membership: Mapping[str, Any] | None = None,
    ) -> Any:
        if (bundle is None) == (membership is None):
            raise RemoteMacMemberError("peer_state_invalid")
        now = self._clock()
        if bundle is not None:
            try:
                verified = verify_invite_bundle(bundle, now=now)
                origin = canonical_https_origin(verified["payload"]["seed_url"])
            except (InviteError, KeyError, TypeError, ValueError) as exc:
                raise self._translate(exc) from exc
            if self._test_seed_client_factory is None:
                client: Any = SeedHTTPClient.from_invite_bundle(
                    bundle,
                    now=now,
                    timeout=self._timeout,
                )
            else:
                client = self._test_seed_client_factory(
                    bundle=dict(bundle),
                    membership=None,
                    now=now,
                    timeout=self._timeout,
                )
        else:
            assert membership is not None
            try:
                origin = canonical_https_origin(str(membership["seed_url"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise RemoteMacMemberError("peer_seed_origin_invalid") from exc
            if self._test_seed_client_factory is None:
                client = SeedHTTPClient(
                    seed_url=origin,
                    swarm_id=str(membership["swarm_id"]),
                    seed_key_digest=str(membership["seed_key_digest"]),
                    seed_key_records=list(membership["seed_key_records"]),
                    timeout=self._timeout,
                )
            else:
                client = self._test_seed_client_factory(
                    bundle=None,
                    membership=dict(membership),
                    now=now,
                    timeout=self._timeout,
                )
        if getattr(client, "seed_url", None) != origin:
            raise RemoteMacMemberError("peer_seed_origin_invalid")
        if self._test_seed_client_factory is None:
            from .public_https import configure_seed_client
            configure_seed_client(client)
        return client

    def set_contribution_enabled(self, *, enabled, expected_generation):
        """Explicit local consent transition; never a seed or load-approval change."""
        if type(enabled) is not bool or type(expected_generation) is not int:
            raise RemoteMacMemberError('peer_consent_invalid')
        with self._lock:
            current=self._control_snapshot(contribution_required=False)
            if current['generation']!=expected_generation:
                raise RemoteMacMemberError('peer_member_binding_mismatch')
            consent=self._load_consent()
            approved=self._validated_consent({**consent,'contribution_enabled':enabled})
            _atomic_private_json(self._consent_path,{'protocol':_CONSENT_PROTOCOL,'consent':approved})
            return dict(approved)

    def managed_control_snapshot(self) -> dict[str, Any]:
        return self._control_snapshot(contribution_required=True)

    def staging_control_snapshot(self) -> dict[str, Any]:
        return self._control_snapshot(contribution_required=False)

    def _control_snapshot(self, *, contribution_required) -> dict[str, Any]:
        """Current target-owned binding, not a serving/physical qualification.

        The same callback is checked before every runtime/pump operation. A
        stopped, expired or locally observed revoked member cannot keep working
        from a detached copy of an earlier accepted generation.
        """
        with self._lock:
            self._require_started()
            consent = self._load_consent()
            member = self._member
            if (member is None or self._signer is None or not consent
                or (contribution_required and not consent['contribution_enabled']) or self._load_disposition() is not None
                or member.lease_expires_at is None or self._clock() >= member.lease_expires_at):
                raise RemoteMacMemberError('peer_member_not_current')
            retained = load_membership_state(self.state_root)
            if (retained is None or retained['node_id'] != member.node_id
                or retained['swarm_id'] != member.swarm_id
                or retained['seed_node_id'] != member.seed_node_id
                or retained['seed_key_digest'] != member.seed_key_digest
                or retained['membership_generation'] != member.generation
                or retained['incarnation'] != member.incarnation
                or retained['endpoint_id'] != self._signer.endpoint_id):
                raise RemoteMacMemberError('peer_member_binding_mismatch')
            return {
                'node_id': member.node_id,
                'endpoint_id': self._signer.endpoint_id,
                'verification_key_digest': self._signer.verification_key_digest,
                'generation': member.generation,
                'incarnation': member.incarnation,
                'lease_expires_at': member.lease_expires_at,
            }

    def managed_control_client(self, *, _test_ca_certificate: Path | None = None):
        return self._control_client(contribution_required=True,_test_ca_certificate=_test_ca_certificate)

    def staging_control_client(self, *, _test_ca_certificate: Path | None = None):
        return self._control_client(contribution_required=False,_test_ca_certificate=_test_ca_certificate)

    def _control_client(self, *, contribution_required, _test_ca_certificate=None):
        """Internal application client from the accepted handoff's origin/pin.

        Callers cannot substitute a signer, origin, swarm or authority key.
        Local-CA injection is an explicit test seam, never inferred from pairing.
        """
        from .managed_control import ManagedControlClient
        with self._lock:
            self._control_snapshot(contribution_required=contribution_required)
            retained = load_membership_state(self.state_root)
            assert retained is not None and self._member is not None
            records = [record for record in retained['seed_key_records']
                if record.get('verification_key_digest') == self._member.seed_key_digest]
            if len(records) != 1:
                raise RemoteMacMemberError('peer_seed_key_invalid')
            kwargs: dict[str, Any] = dict(base_url=canonical_https_origin(retained['seed_url']),
                signer=self._signer, trusted_owner_key_record=records[0],
                trusted_owner_endpoint_id=self._member.seed_endpoint_id,
                swarm_id=self._member.swarm_id, owner_node_id=self._member.seed_node_id,
                timeout=self._timeout, clock=self._clock)
            if _test_ca_certificate is not None:
                return ManagedControlClient.for_local_tls(ca_certificate=_test_ca_certificate, **kwargs)
            return ManagedControlClient(**kwargs)

    def _new_session(
        self,
        *,
        node_id: str,
        swarm_id: str,
        seed_node_id: str,
        incarnation: str,
        first_message_id: str,
        replay_issued_at: float | None = None,
    ) -> tuple[NodeMembershipSession, list[float | None]]:
        assert self._signer is not None
        first = [first_message_id]
        replay = [replay_issued_at]

        def identifiers() -> str:
            if first:
                return first.pop(0)
            return "member-message-" + uuid.uuid4().hex

        def clock() -> float:
            return self._clock() if replay[0] is None else float(replay[0])

        session = NodeMembershipSession(
            node_id=node_id,
            swarm_id=swarm_id,
            seed_node_id=seed_node_id,
            signer=self._signer,
            incarnation=incarnation,
            software_version=self.SOFTWARE_VERSION,
            peer_class=self.PEER_CLASS,
            runtime_capability=dict(self.RUNTIME_CAPABILITY),
            clock=clock,
            id_source=identifiers,
        )
        return session, replay

    @staticmethod
    def _checkpoint_issued_at(checkpoint: Mapping[str, Any], field: str) -> float:
        try:
            envelope = checkpoint[field]
            issued = envelope["message"]["issued_at"]
            if isinstance(issued, bool) or not isinstance(issued, (int, float)):
                raise ValueError("issued_at")
            return float(issued)
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteMacMemberError("peer_recovery_invalid") from exc

    def _membership_result(self) -> dict[str, Any]:
        status = self.status()
        return {
            "protocol": _MEMBERSHIP_RESULT_PROTOCOL,
            "membership_state": status["membership_state"],
            "membership_generation": status["membership_generation"],
            "lease_state": status["lease_state"],
            "serving_state": "not_yet_serving",
            "route_ready": False,
        }

    def accept_handoff(self, handoff: Mapping[str, Any]) -> dict[str, Any]:
        """Decrypt locally, pin the seed, and submit one checkpointed HTTPS join."""

        with self._lock:
            self._require_started()
            if not isinstance(handoff, Mapping):
                raise RemoteMacMemberError("pairing_handoff_invalid")
            handoff_value = dict(handoff)
            handoff_digest = _digest(handoff_value)
            accepted = self._load_accepted()
            rejoin = self._rejoin_path.exists()
            if accepted is not None and rejoin and accepted["handoff_digest"] != handoff_digest:
                accepted = None  # a new invite replaces the lapsed membership; the old one stays until this one commits
            if accepted is not None:
                if accepted["handoff_digest"] != handoff_digest:
                    raise RemoteMacMemberError("pairing_handoff_changed")
                if load_membership_state(self.state_root) is None:
                    raise RemoteMacMemberError("peer_membership_state_invalid")
                self._rejoin_path.unlink(missing_ok=True)
                return self._membership_result()
            checkpoint = _load_private_json(self._join_pending_path)
            if checkpoint is not None and checkpoint.get(
                "handoff_digest"
            ) != handoff_digest:
                raise RemoteMacMemberError("pairing_handoff_changed")
            _atomic_private_json(self._handoff_path, handoff_value)
            pending = self._load_pending()
            try:
                recovered = decrypt_invitation_handoff(
                    request=pending["request"],
                    handoff=handoff_value,
                    recipient_private_key=pending["recipient_private_key"],
                    now=self._clock(),
                )
                verified = verify_invite_bundle(
                    recovered["invite_bundle"], now=self._clock()
                )
            except (PairingError, InviteError) as exc:
                raise self._translate(exc) from exc
            if self._signer is None or (
                self._signer.verification_key_digest
                != recovered["identity_key_digest"]
            ):
                raise RemoteMacMemberError("peer_identity_mismatch")
            bundle = recovered["invite_bundle"]
            client = self._make_client(bundle=bundle)
            try:
                identity = client.identity(now=self._clock)
            except SeedHTTPError as exc:
                raise self._translate(exc) from exc
            if (
                identity.get("seed_url") != verified["payload"]["seed_url"]
                or identity.get("swarm_id") != verified["payload"]["swarm_id"]
            ):
                raise RemoteMacMemberError("peer_seed_identity_mismatch")
            if checkpoint is None:
                incarnation = "remote-incarnation-" + secrets.token_hex(8)
                message_id = "join-message-" + uuid.uuid4().hex
                session, replay = self._new_session(
                    node_id=recovered["node_id"],
                    swarm_id=verified["payload"]["swarm_id"],
                    seed_node_id=identity["seed_node_id"],
                    incarnation=incarnation,
                    first_message_id=message_id,
                )
                join_envelope = session.join_request(
                    invite_nonce=recovered["pairing_nonce"],
                    endpoint_addrs=[self._endpoint_address()],
                )
                replay[0] = None
                checkpoint = {
                    "protocol": _JOIN_PENDING_PROTOCOL,
                    "handoff_digest": handoff_digest,
                    "incarnation": incarnation,
                    "join_message_id": message_id,
                    "join_envelope": join_envelope,
                }
                _atomic_private_json(self._join_pending_path, checkpoint)
            else:
                if (
                    checkpoint.get("protocol") != _JOIN_PENDING_PROTOCOL
                    or not isinstance(checkpoint.get("incarnation"), str)
                    or not isinstance(checkpoint.get("join_message_id"), str)
                    or not isinstance(checkpoint.get("join_envelope"), Mapping)
                ):
                    raise RemoteMacMemberError("peer_recovery_invalid")
                session, replay = self._new_session(
                    node_id=recovered["node_id"],
                    swarm_id=verified["payload"]["swarm_id"],
                    seed_node_id=identity["seed_node_id"],
                    incarnation=checkpoint["incarnation"],
                    first_message_id=checkpoint["join_message_id"],
                    replay_issued_at=self._checkpoint_issued_at(
                        checkpoint, "join_envelope"
                    ),
                )
                reproduced = session.join_request(
                    invite_nonce=recovered["pairing_nonce"],
                    endpoint_addrs=[self._endpoint_address()],
                )
                replay[0] = None
                if canonical_json_bytes(reproduced) != canonical_json_bytes(
                    checkpoint["join_envelope"]
                ):
                    raise RemoteMacMemberError("peer_recovery_invalid")
                join_envelope = reproduced
            try:
                acceptance = client.join(
                    invite_token=bundle["token"],
                    join_envelope=join_envelope,
                )
                accepted_message = session.accept_join(
                    acceptance,
                    seed_key_digest=verified["seed_key_digest"],
                )
            except (KeyError, NodeMembershipError, SeedHTTPError) as exc:
                raise self._translate(exc) from exc
            save_membership_state(
                self.state_root,
                {
                    "protocol": MEMBERSHIP_STATE_PROTOCOL,
                    "node_id": recovered["node_id"],
                    "swarm_id": verified["payload"]["swarm_id"],
                    "seed_node_id": identity["seed_node_id"],
                    "seed_url": verified["payload"]["seed_url"],
                    "seed_key_digest": verified["seed_key_digest"],
                    "seed_key_records": list(verified["seed_key_records"]),
                    "endpoint_id": self._signer.endpoint_id,
                    "incarnation": checkpoint["incarnation"],
                    "membership_generation": accepted_message[
                        "membership_generation"
                    ],
                    "restart_count": 0,
                },
            )
            _atomic_private_json(
                self._accepted_path,
                {
                    "protocol": _ACCEPTED_PROTOCOL,
                    "handoff_digest": handoff_digest,
                    "quota": recovered["quota"],
                    "accepted_at": self._clock(),
                },
            )
            for path in (
                self._pending_path,
                self._handoff_path,
                self._join_pending_path,
                self._rejoin_path,
            ):
                path.unlink(missing_ok=True)
            self._clear_disposition()
            self._member = session
            self._seed_client = client
            return self._membership_result()

    def _load_resume_checkpoint(
        self, membership: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        checkpoint = _load_private_json(self._resume_pending_path)
        if checkpoint is None:
            return None
        if (
            checkpoint.get("protocol") != _RESUME_PENDING_PROTOCOL
            or not isinstance(checkpoint.get("previous_generation"), int)
            or isinstance(checkpoint.get("previous_generation"), bool)
            or not isinstance(checkpoint.get("previous_incarnation"), str)
            or not isinstance(checkpoint.get("incarnation"), str)
            or not isinstance(checkpoint.get("restart_count"), int)
            or isinstance(checkpoint.get("restart_count"), bool)
            or not isinstance(checkpoint.get("resume_message_id"), str)
            or not isinstance(checkpoint.get("resume_envelope"), Mapping)
        ):
            raise RemoteMacMemberError("peer_recovery_invalid")
        locally_committed = (
            membership["membership_generation"]
            == checkpoint["previous_generation"] + 1
            and membership["incarnation"] == checkpoint["incarnation"]
            and membership["restart_count"] == checkpoint["restart_count"]
        )
        if locally_committed:
            self._resume_pending_path.unlink()
            return None
        if (
            membership["membership_generation"]
            != checkpoint["previous_generation"]
            or membership["incarnation"] != checkpoint["previous_incarnation"]
            or membership["restart_count"] + 1 != checkpoint["restart_count"]
        ):
            raise RemoteMacMemberError("peer_recovery_invalid")
        return checkpoint

    def _resume(self, membership: Mapping[str, Any]) -> None:
        assert self._signer is not None
        if (
            membership["node_id"] != self._node_id()
            or membership["endpoint_id"] != self._signer.endpoint_id
        ):
            raise RemoteMacMemberError("peer_identity_mismatch")
        client = self._make_client(membership=membership)
        try:
            identity = client.identity(now=self._clock)
        except SeedHTTPError as exc:
            raise self._translate(exc) from exc
        if (
            identity.get("seed_url") != membership["seed_url"]
            or identity.get("swarm_id") != membership["swarm_id"]
            or identity.get("seed_node_id") != membership["seed_node_id"]
        ):
            raise RemoteMacMemberError("peer_seed_identity_mismatch")
        checkpoint = self._load_resume_checkpoint(membership)
        if checkpoint is None:
            restart_count = int(membership["restart_count"]) + 1
            incarnation = next_incarnation(membership["incarnation"], restart_count)
            message_id = "resume-message-" + uuid.uuid4().hex
            session, replay = self._new_session(
                node_id=membership["node_id"],
                swarm_id=membership["swarm_id"],
                seed_node_id=membership["seed_node_id"],
                incarnation=incarnation,
                first_message_id=message_id,
            )
            resume_envelope = session.resume_request(
                previous_generation=membership["membership_generation"],
                previous_incarnation=membership["incarnation"],
                endpoint_addrs=[self._endpoint_address()],
            )
            replay[0] = None
            checkpoint = {
                "protocol": _RESUME_PENDING_PROTOCOL,
                "previous_generation": membership["membership_generation"],
                "previous_incarnation": membership["incarnation"],
                "incarnation": incarnation,
                "restart_count": restart_count,
                "resume_message_id": message_id,
                "resume_envelope": resume_envelope,
            }
            _atomic_private_json(self._resume_pending_path, checkpoint)
        else:
            session, replay = self._new_session(
                node_id=membership["node_id"],
                swarm_id=membership["swarm_id"],
                seed_node_id=membership["seed_node_id"],
                incarnation=checkpoint["incarnation"],
                first_message_id=checkpoint["resume_message_id"],
                replay_issued_at=self._checkpoint_issued_at(
                    checkpoint, "resume_envelope"
                ),
            )
            reproduced = session.resume_request(
                previous_generation=checkpoint["previous_generation"],
                previous_incarnation=checkpoint["previous_incarnation"],
                endpoint_addrs=[self._endpoint_address()],
            )
            replay[0] = None
            if canonical_json_bytes(reproduced) != canonical_json_bytes(
                checkpoint["resume_envelope"]
            ):
                raise RemoteMacMemberError("peer_recovery_invalid")
            resume_envelope = reproduced
        try:
            acceptance = client.resume(resume_envelope=resume_envelope)
            accepted = session.accept_resume(
                acceptance,
                seed_key_digest=membership["seed_key_digest"],
            )
        except (NodeMembershipError, SeedHTTPError) as exc:
            raise self._translate(exc) from exc
        save_membership_state(
            self.state_root,
            {
                **dict(membership),
                "incarnation": checkpoint["incarnation"],
                "membership_generation": accepted["membership_generation"],
                "restart_count": checkpoint["restart_count"],
            },
        )
        self._resume_pending_path.unlink(missing_ok=True)
        self._clear_disposition()
        self._member = session
        self._seed_client = client

    def _require_current(self) -> tuple[NodeMembershipSession, Any]:
        self._require_started()
        if self._member is None or self._seed_client is None:
            disposition = self._load_disposition()
            if disposition is not None:
                code = {
                    "expired": "peer_member_expired",
                    "revoked": "peer_member_revoked",
                    "stale": "peer_generation_stale",
                    "unknown": "peer_member_unknown",
                }[disposition]
                raise RemoteMacMemberError(code)
            raise RemoteMacMemberError("peer_membership_missing")
        return self._member, self._seed_client

    def renew(self) -> dict[str, Any]:
        """Renew the current canonical lease without advancing generation."""

        with self._lock:
            member, client = self._require_current()
            try:
                heartbeat = member.heartbeat(
                    lifecycle_state="RUNNING", active_requests=0, force=True
                )
                if heartbeat is None:
                    raise RemoteMacMemberError("peer_renewal_unavailable")
                renewal = client.send_member_message(
                    heartbeat, now=self._clock
                )
                member.accept_lease_renewal(
                    renewal,
                    heartbeat_message_id=heartbeat["message"]["message_id"],
                )
            except RemoteMacMemberError:
                raise
            except (NodeMembershipError, SeedHTTPError) as exc:
                raise self._translate(exc) from exc
            self._clear_disposition()
            return self._membership_result()

    def capability_report(
        self,
        *,
        platform: str,
        architecture: str,
        memory_bytes: int,
        available_storage_bytes: int,
        backends: list[str],
        precisions: list[str],
    ) -> dict[str, Any]:
        """Sign, submit, and privately retain one canonical capability report."""

        with self._lock:
            member, client = self._require_current()
            consent = self._load_consent()
            if consent is None or not (
                consent["cooperative_peer_visibility_acknowledged"]
                and consent["resource_use_acknowledged"]
            ):
                raise RemoteMacMemberError("peer_consent_invalid")
            try:
                report = member.capability_report(
                    platform=platform,
                    architecture=architecture,
                    memory_bytes=memory_bytes,
                    available_storage_bytes=available_storage_bytes,
                    backends=backends,
                    precisions=precisions,
                )
                receipt = client.send_member_message(
                    report, now=self._clock
                )
            except (NodeMembershipError, SeedHTTPError, ValueError) as exc:
                raise self._translate(exc) from exc
            if report.get("message", {}).get("protocol") != CAPABILITY_REPORT_PROTOCOL:
                raise RemoteMacMemberError("peer_capability_invalid")
            _atomic_private_json(
                self._capability_path,
                {
                    "protocol": _CAPABILITY_EVIDENCE_PROTOCOL,
                    "report": report,
                    "receipt": receipt,
                    "accepted_at": self._clock(),
                },
            )
            return {
                "protocol": _CAPABILITY_RESULT_PROTOCOL,
                "accepted": True,
                "membership_generation": member.generation,
                "report": report,
                "serving_state": "not_yet_serving",
                "route_ready": False,
            }

    # -- A13 join flow helpers (pairing window / automatic report) ---------------

    def signed_capability_link_report(
        self, *, purpose: str, device: Mapping[str, Any], link: Mapping[str, Any], caps=None
    ) -> dict[str, Any]:
        """Sign one self-reported device + relay-link report with the durable key.

        ``join`` reports precede membership (no generation); every later report
        is bound to the current membership generation.
        """

        from .capability import signed_capability_link_report

        with self._lock:
            self._require_started()
            generation = None
            if purpose != "join" or self._member is not None:
                member, _client = self._require_current()
                generation = member.generation
            assert self._signer is not None
            return signed_capability_link_report(
                signer=self._signer, node_id=self._node_id(), purpose=purpose,
                generation=generation, device=device, link=link, now=self._clock(), caps=caps)

    def handoff_seed_key_digest(self, handoff: Mapping[str, Any]) -> str:
        """Recipient-decrypt a handoff WITHOUT joining, to check a join-link pin."""

        with self._lock:
            self._require_started()
            pending = self._load_pending()
            try:
                payload = decrypt_invitation_handoff(
                    request=pending["request"], handoff=dict(handoff),
                    recipient_private_key=pending["recipient_private_key"],
                    now=self._clock())
            except PairingError as exc:
                raise self._translate(exc) from exc
            return str(payload["seed_key_digest"])

    def membership_binding(self) -> dict[str, str] | None:
        """Owner binding for standing consent: swarm, node and owner seed key."""

        with self._lock:
            member = self._member
            if member is None:
                return None
            return {"swarm_id": member.swarm_id, "node_id": member.node_id,
                    "seed_key_digest": member.seed_key_digest}

    def owner_origin(self) -> str | None:
        """The accepted owner's public HTTPS origin (the relay), if joined."""

        retained = load_membership_state(self.state_root) if self.state_root.exists() else None
        if retained is None:
            return None
        try:
            return canonical_https_origin(str(retained["seed_url"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteMacMemberError("peer_seed_origin_invalid") from exc


__all__ = ["RemoteMacMember", "RemoteMacMemberError"]
