# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runnable persistent Mycelium membership and physical-node service."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import stat
import sys
import tempfile
import threading
import time
from typing import Any, NoReturn, Sequence
from urllib.parse import urlsplit

from mycelium_invite import verify_invite_bundle
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed.http import (
    JOIN_ROUTE_ERROR_STATUSES,
    SeedHTTPClient,
    SeedHTTPError,
    _error_status,
    _validate_endpoint_url,
)

from . import membership as membership_module
from .identity import load_or_create_node_signer
from .durable_membership import (
    PROTOCOL as MEMBERSHIP_STATE_PROTOCOL,
    load_membership_state,
    next_incarnation,
    save_membership_state,
)
from .membership import NodeMembershipSession
from .reconnect import RenewalRetryPolicy, transient_renewal_failure
from .process import (
    _ExecutableIdentity,
    PhysicalNodeProcess,
    PrivateDirectoryLease,
    build_physical_node_command,
    capture_executable_identity,
    physical_service_interpreter_identity,
    private_directory_lease,
    private_directory_parent_fd,
    private_directory_path,
    validate_physical_node_launch_shape,
)


_STATUS_PROTOCOL = "mycelium.node_main_status.v1"
_PEER_CAPABILITIES = {
    "mac_mlx_iroh": {
        "runtime_backend": "mlx",
        "transport": "iroh",
        "activation_protocol": "mycelium.router_wire.v1",
    },
    "android_termux_iroh": {
        "runtime_backend": "pixel-stdlib",
        "transport": "iroh",
        "activation_protocol": "mycelium.router_wire.v1",
    },
    "linux_numpy_iroh": {
        "runtime_backend": "numpy",
        "transport": "iroh",
        "activation_protocol": "mycelium.router_wire.v1",
    },
    "linux_tbd": {
        "runtime_backend": "tbd",
        "transport": "none",
        "activation_protocol": None,
    },
    "artifact_source_https": {
        "runtime_backend": "artifact-source",
        "transport": "https",
        "activation_protocol": None,
    },
}
_DEFAULT_CAPABILITY = _PEER_CAPABILITIES["mac_mlx_iroh"]
_SOURCE_ONLY_PEER_CLASSES = frozenset({"artifact_source_https"})
_MAX_JOIN_BUNDLE_BYTES = 1024 * 1024
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)

# Stable process contract shared with the seed entrypoint.
EXIT_SUCCESS = 0
EXIT_PREFLIGHT_FAILURE = 2
EXIT_JOIN_REJECTION = 3
EXIT_RUNTIME_FAILURE = 4


class _EntrypointFailure(RuntimeError):
    def __init__(self, code: str, exit_status: int) -> None:
        self.code = code
        self.exit_status = exit_status
        super().__init__(code)


def _aggregate_cleanup_failures(
    failure: _EntrypointFailure | None,
    phases: Sequence[str],
) -> _EntrypointFailure:
    if failure is None:
        failure = _EntrypointFailure(
            "node_runtime_failed",
            EXIT_RUNTIME_FAILURE,
        )
    prior_phases = tuple(getattr(failure, "_cleanup_phases", ()))
    all_phases = (*prior_phases, *phases)
    failure._cleanup_phases = all_phases
    retained_notes = [
        note
        for note in getattr(failure, "__notes__", ())
        if not note.startswith(("cleanup_phase=", "cleanup_failure_count="))
    ]
    cleanup_notes = [
        *(f"cleanup_phase={phase}" for phase in all_phases),
        f"cleanup_failure_count={len(all_phases)}",
    ]
    failure.__notes__ = [*retained_notes, *cleanup_notes]
    return failure


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(EXIT_PREFLIGHT_FAILURE, "node_preflight_failed\n")


def _private_directory(value: str | Path, *, create: bool = True) -> Path:
    return private_directory_path(value, create=create)


def _canonical_document_bytes(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > _MAX_JOIN_BUNDLE_BYTES:
        raise ValueError("join bundle is invalid")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("join bundle is invalid") from exc
    if not isinstance(document, dict) or canonical_json_bytes(document) != raw:
        raise ValueError("join bundle is invalid")
    return document


def _canonical_document(path_value: str | Path) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(path))
    descriptor: int | None = None
    parent: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent = private_directory_parent_fd(path)
        descriptor = os.open(path.name, flags, dir_fd=parent)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size <= 0
            or before.st_size > _MAX_JOIN_BUNDLE_BYTES
        ):
            raise ValueError("join bundle file is invalid")
        chunks: list[bytes] = []
        remaining = _MAX_JOIN_BUNDLE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != before.st_size
            or len(raw) > _MAX_JOIN_BUNDLE_BYTES
            or any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            )
        ):
            raise ValueError("join bundle file is invalid")
    except OSError as exc:
        raise ValueError("join bundle file is invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)
    return _canonical_document_bytes(raw)


def _stdin_document() -> dict[str, Any]:
    try:
        raw = sys.stdin.buffer.read(_MAX_JOIN_BUNDLE_BYTES + 1)
    except OSError as exc:
        raise ValueError("join bundle stdin is invalid") from exc
    return _canonical_document_bytes(raw)


def _sidecar_path(value: str | None) -> Path:
    if value is not None:
        supplied = Path(value).expanduser()
        if not supplied.is_absolute() or supplied != Path(os.path.abspath(supplied)):
            raise ValueError("sidecar binary is unavailable")
        candidates = [supplied]
    else:
        root = Path(__file__).resolve().parents[1]
        candidates = [
            root
            / "native"
            / "iroh_transport"
            / "target"
            / "release"
            / "mycelium-iroh-sidecar",
            root
            / "native"
            / "iroh_transport"
            / "target"
            / "debug"
            / "mycelium-iroh-sidecar",
        ]
    for candidate in candidates:
        try:
            identity = capture_executable_identity(
                candidate,
                require_canonical=True,
                require_private_owner=True,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        return Path(identity.path)
    raise ValueError("sidecar binary is unavailable")


def _service_interpreter() -> Path:
    return Path(physical_service_interpreter_identity().path)


def _validate_advertised_endpoint(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
    ):
        raise ValueError("advertised endpoint is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or (parsed.path and not parsed.path.startswith("/"))
    ):
        raise ValueError("advertised endpoint is invalid")
    origin = _validate_endpoint_url(f"{parsed.scheme}://{parsed.netloc}")
    if value != origin + parsed.path:
        raise ValueError("advertised endpoint is invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="python -m mycelium_node")
    parser.add_argument("--data-dir", required=True)
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument(
        "--join-bundle-file",
        "--seed-invite",
        dest="join_bundle_file",
    )
    source.add_argument("--join-bundle-stdin", action="store_true")
    parser.add_argument("--node-id", required=True)
    parser.add_argument(
        "--peer-class",
        choices=tuple(_PEER_CAPABILITIES),
        default="mac_mlx_iroh",
    )
    parser.add_argument("--membership-endpoint-id")
    parser.add_argument("--advertise", action="append", required=True)
    parser.add_argument(
        "--sidecar-path",
        help="required for inference peers and forbidden for source-only peers",
    )
    parser.add_argument(
        "--membership-control-only",
        action="store_true",
        help=(
            "renew the inference peer membership lease while the product route owns "
            "the physical stage runtime"
        ),
    )
    parser.add_argument("--run-id", default="node-main-run")
    parser.add_argument("--deployment-id", default="node-main-unassigned")
    parser.add_argument("--incarnation", default="node-main")
    parser.add_argument(
        "--lifecycle-state",
        choices=("NEW", "CONFIGURED", "RUNNING", "DRAINING"),
        default="NEW",
    )
    parser.add_argument("--heartbeat-interval", type=float, default=30.0)
    parser.add_argument("--renewal-jitter-fraction", type=float, default=0.15)
    parser.add_argument("--reconnect-base-seconds", type=float, default=0.5)
    parser.add_argument("--reconnect-max-seconds", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _emit_status(status: dict[str, object]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(status) + b"\n")
    sys.stdout.buffer.flush()


@dataclass(frozen=True)
class _TemporaryRoot:
    path: Path
    device: int
    inode: int


def _temporary_root() -> _TemporaryRoot:
    trusted_root = Path(tempfile.gettempdir()).resolve(strict=True)
    path = Path(tempfile.mkdtemp(prefix="myc-node-", dir=trusted_root))
    parent = private_directory_parent_fd(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(path.name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ValueError("temporary root is invalid")
        return _TemporaryRoot(path, metadata.st_dev, metadata.st_ino)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _clear_directory(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise RuntimeError("temporary root cleanup failed")
                _clear_directory(child)
            finally:
                os.close(child)
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise RuntimeError("temporary root cleanup failed")
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _remove_temporary_root(root: _TemporaryRoot) -> None:
    parent = private_directory_parent_fd(root.path)
    descriptor: int | None = None
    try:
        descriptor = os.open(root.path.name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (root.device, root.inode):
            raise RuntimeError("temporary root cleanup failed")
        _clear_directory(descriptor)
        current = os.stat(
            root.path.name,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (root.device, root.inode):
            raise RuntimeError("temporary root cleanup failed")
        os.rmdir(root.path.name, dir_fd=parent)
    except FileNotFoundError:
        raise RuntimeError("temporary root cleanup failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _preflight(
    args: argparse.Namespace,
) -> tuple[
    PrivateDirectoryLease,
    dict[str, Any],
    dict[str, Any],
    SeedHTTPClient,
    Path | None,
    tuple[_ExecutableIdentity, _ExecutableIdentity, _ExecutableIdentity] | None,
    dict[str, Any] | None,
]:
    state_root: PrivateDirectoryLease | None = None
    try:
        RenewalRetryPolicy(
            heartbeat_interval_seconds=args.heartbeat_interval,
            jitter_fraction=args.renewal_jitter_fraction,
            reconnect_base_seconds=args.reconnect_base_seconds,
            reconnect_max_seconds=args.reconnect_max_seconds,
            lease_risk_window_seconds=max(args.heartbeat_interval, 1.0),
        )
        state_root = private_directory_lease(
            args.data_dir,
            create=not args.dry_run,
        )
        now = time.time()
        persisted = load_membership_state(state_root.path)
        if persisted is None:
            if args.join_bundle_stdin:
                bundle = _stdin_document()
            elif args.join_bundle_file is not None:
                bundle = _canonical_document(args.join_bundle_file)
            else:
                raise ValueError("join bundle is required for first enrollment")
            verified = verify_invite_bundle(bundle, now=now)
            client = SeedHTTPClient.from_invite_bundle(bundle, now=now)
        else:
            if (
                persisted["node_id"] != args.node_id
                or (
                    args.membership_endpoint_id is not None
                    and persisted["endpoint_id"] != args.membership_endpoint_id
                )
            ):
                raise ValueError("persisted membership identity mismatch")
            bundle = {}
            verified = {
                "payload": {
                    "swarm_id": persisted["swarm_id"],
                    "seed_url": persisted["seed_url"],
                    "nonce": "persisted-resume",
                },
                "seed_key_digest": persisted["seed_key_digest"],
                "seed_key_records": list(persisted["seed_key_records"]),
            }
            client = SeedHTTPClient(
                seed_url=persisted["seed_url"],
                swarm_id=persisted["swarm_id"],
                seed_key_digest=persisted["seed_key_digest"],
                seed_key_records=list(persisted["seed_key_records"]),
            )
        source_only = args.peer_class in _SOURCE_ONLY_PEER_CLASSES
        membership_control_only = args.membership_control_only
        if source_only and membership_control_only:
            raise ValueError("source-only peer has no product-route runtime ownership")
        if source_only or membership_control_only:
            if args.sidecar_path is not None:
                raise ValueError("control-only peer cannot accept a sidecar")
            sidecar = None
        else:
            sidecar = _sidecar_path(args.sidecar_path)
        advertised_endpoints = [
            _validate_advertised_endpoint(value) for value in args.advertise
        ]
        identities = None
        if not source_only and not membership_control_only:
            service_script = (
                Path(__file__).resolve().parents[1] / "physical_inference_node.py"
            )
            interpreter = _service_interpreter()
            assert sidecar is not None
            identities = validate_physical_node_launch_shape(
                python_executable=interpreter,
                service_script=service_script,
                run_id=args.run_id,
                deployment_id=args.deployment_id,
                node_id=args.node_id,
                sidecar_binary=sidecar,
                sidecar_local_only=False,
            )
        validation_signer = generate_ed25519_signer(
            endpoint_id=args.membership_endpoint_id or "node-preflight-endpoint"
        )
        validation_session = NodeMembershipSession(
            node_id=args.node_id,
            swarm_id=verified["payload"]["swarm_id"],
            seed_node_id="seed-preflight-node",
            signer=validation_signer,
            incarnation=(
                args.incarnation
                if persisted is None
                else next_incarnation(
                    args.incarnation,
                    int(persisted["restart_count"]) + 1,
                )
            ),
            software_version="mycelium-node-main",
            peer_class=args.peer_class,
            runtime_capability=_PEER_CAPABILITIES[args.peer_class],
        )
        if persisted is None:
            validation_session.join_request(
                invite_nonce=verified["payload"]["nonce"],
                endpoint_addrs=advertised_endpoints,
            )
        else:
            validation_session.resume_request(
                previous_generation=int(persisted["membership_generation"]),
                previous_incarnation=persisted["incarnation"],
                endpoint_addrs=advertised_endpoints,
            )
        membership_module.validate_heartbeat_shape(
            lifecycle_state="NEW",
            active_requests=0,
            route_ready=False,
            liveness_source="scheduled_heartbeat",
            activity_receipt_digest=None,
            activity_peer_node_id=None,
        )
        return state_root, bundle, verified, client, sidecar, identities, persisted
    except _EntrypointFailure as failure:
        cleanup_phases: list[str] = []
        if state_root is not None:
            try:
                state_root.close()
            except Exception:
                cleanup_phases.append("state_root")
        if cleanup_phases:
            _aggregate_cleanup_failures(failure, cleanup_phases)
        raise
    except Exception as exc:
        failure = _EntrypointFailure(
            "node_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        )
        cleanup_phases: list[str] = []
        if state_root is not None:
            try:
                state_root.close()
            except Exception:
                cleanup_phases.append("state_root")
        if cleanup_phases:
            _aggregate_cleanup_failures(failure, cleanup_phases)
        raise failure from exc


def _join_rejected(exc: SeedHTTPError) -> bool:
    authoritative_status = JOIN_ROUTE_ERROR_STATUSES.get(exc.code)
    return (
        authoritative_status is not None
        and exc.status == authoritative_status
        and exc.status == _error_status(exc.code)
    )


def _endpoint_identity_digest(endpoint_id: str) -> str:
    return "sha256:" + hashlib.sha256(endpoint_id.encode("utf-8")).hexdigest()


def _renew_lease(
    *,
    client: SeedHTTPClient,
    session: NodeMembershipSession,
    lifecycle_state: str,
    stopping: threading.Event,
    policy: RenewalRetryPolicy,
) -> bool:
    """Renew one exact signed heartbeat with bounded idempotent retry."""

    heartbeat = session.heartbeat(
        lifecycle_state=lifecycle_state,
        active_requests=0,
        force=True,
    )
    if heartbeat is None:  # pragma: no cover - force=True makes this unreachable.
        raise RuntimeError("membership_heartbeat_missing")
    message = heartbeat.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("message_id"), str):
        raise RuntimeError("membership_heartbeat_invalid")
    attempt = 0
    disconnected = False
    while not stopping.is_set():
        try:
            renewal = client.send_member_message(
                heartbeat,
                now=time.time() + 1.0,
            )
            session.accept_lease_renewal(
                renewal,
                heartbeat_message_id=message["message_id"],
            )
        except BaseException as exc:
            lease_expires_at = session.lease_expires_at
            if lease_expires_at is None:
                raise RuntimeError("membership_lease_missing") from exc
            now = time.time()
            state = policy.state(
                now=now,
                lease_expires_at=lease_expires_at,
                disconnected=True,
            )
            _emit_status(
                {
                    "protocol": _STATUS_PROTOCOL,
                    "event": "membership_connectivity_changed",
                    "node_id": session.node_id,
                    "membership_generation": session.generation,
                    "connectivity_state": state.value,
                    "last_signed_observation_unix_ms": int(now * 1_000),
                    "renewal_deadline_unix_ms": int(lease_expires_at * 1_000),
                    "reconnect_action": "bounded_exponential_retry",
                    "placement_impact": "route_eligibility_requires_fresh_qualification",
                    "route_ready": False,
                }
            )
            if not transient_renewal_failure(exc) or state.value == "expired":
                raise
            delay = policy.reconnect_delay(
                attempt=attempt,
                random_value=random.random(),
                now=now,
                lease_expires_at=lease_expires_at,
            )
            if delay <= 0 or stopping.wait(delay):
                return False
            disconnected = True
            attempt += 1
            continue
        if disconnected:
            lease_expires_at = session.lease_expires_at
            assert lease_expires_at is not None
            now = time.time()
            _emit_status(
                {
                    "protocol": _STATUS_PROTOCOL,
                    "event": "membership_connectivity_changed",
                    "node_id": session.node_id,
                    "membership_generation": session.generation,
                    "connectivity_state": "online",
                    "last_signed_observation_unix_ms": int(now * 1_000),
                    "renewal_deadline_unix_ms": int(lease_expires_at * 1_000),
                    "reconnect_action": "reconnected",
                    "placement_impact": "qualification_still_required",
                    "route_ready": False,
                }
            )
        return True
    return False


def _run_bound(
    args: argparse.Namespace,
    state_root: PrivateDirectoryLease,
    bundle: dict[str, Any],
    verified: dict[str, Any],
    client: SeedHTTPClient,
    sidecar: Path | None,
    identities: tuple[
        _ExecutableIdentity,
        _ExecutableIdentity,
        _ExecutableIdentity,
    ] | None,
    persisted: dict[str, Any] | None = None,
) -> int:
    membership_control_only = bool(
        getattr(args, "membership_control_only", False)
    )
    try:
        rotation_method = getattr(client, "rotation", None)
        if callable(rotation_method):
            rotation_method(now=time.time())
        seed_identity = client.identity(now=time.time() + 1.0)
    except Exception as exc:
        raise _EntrypointFailure(
            "node_runtime_failed",
            EXIT_RUNTIME_FAILURE,
        ) from exc

    temporary_root: _TemporaryRoot | None = None
    artifact_root: PrivateDirectoryLease | None = None
    try:
        state_root.revalidate()
        signer = load_or_create_node_signer(
            Path("identity") / "node.key",
            endpoint_id=args.membership_endpoint_id,
        )
        state_root.revalidate()
        runtime_incarnation = (
            args.incarnation
            if persisted is None
            else next_incarnation(
                args.incarnation,
                int(persisted["restart_count"]) + 1,
            )
        )
        if persisted is not None and signer.endpoint_id != persisted["endpoint_id"]:
            raise ValueError("persisted membership identity mismatch")
        session = NodeMembershipSession(
            node_id=args.node_id,
            swarm_id=verified["payload"]["swarm_id"],
            seed_node_id=seed_identity["seed_node_id"],
            signer=signer,
            incarnation=runtime_incarnation,
            software_version="mycelium-node-main",
            peer_class=args.peer_class,
            runtime_capability=_PEER_CAPABILITIES[args.peer_class],
        )
        command = None
        if (
            args.peer_class not in _SOURCE_ONLY_PEER_CLASSES
            and not membership_control_only
        ):
            assert sidecar is not None and identities is not None
            artifact_root = state_root.private_subdirectory("artifacts")
            state_root.revalidate()
            artifact_root.revalidate()
            temporary_root = _temporary_root()
            with state_root.working_directory():
                command = build_physical_node_command(
                    python_executable=_service_interpreter(),
                    service_script=Path(__file__).resolve().parents[1]
                    / "physical_inference_node.py",
                    run_id=args.run_id,
                    deployment_id=args.deployment_id,
                    node_id=args.node_id,
                    artifact_root=Path("artifacts"),
                    socket_root=temporary_root.path,
                    sidecar_binary=sidecar,
                    sidecar_local_only=False,
                    descriptor_relative_artifact_root=True,
                )
            state_root.revalidate()
            artifact_root.revalidate()
    except Exception as exc:
        failure = _EntrypointFailure(
            "node_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        )
        cleanup_phases: list[str] = []
        if temporary_root is not None:
            try:
                _remove_temporary_root(temporary_root)
            except Exception:
                cleanup_phases.append("temporary_root")
        if artifact_root is not None:
            try:
                artifact_root.close()
            except Exception:
                cleanup_phases.append("artifact_root")
        if cleanup_phases:
            _aggregate_cleanup_failures(failure, cleanup_phases)
        raise failure from exc

    process: PhysicalNodeProcess | None = None
    stopping = threading.Event()
    acknowledged_authority_generation: int | None = None
    previous: dict[int, object] = {}
    failure: _EntrypointFailure | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        # The child resolves its descriptor-relative artifact directory during
        # startup, so pin its inherited cwd to the retained state-root fd.
        if command is not None:
            assert identities is not None
            with state_root.working_directory():
                process = PhysicalNodeProcess(
                    command=command,
                    node_id=args.node_id,
                    run_id=args.run_id,
                    deployment_id=args.deployment_id,
                    expected_executables=identities,
                )
            hello = process.command("hello")
            if not isinstance(hello, dict) or hello.get("route_ready") is not False:
                raise RuntimeError("node child claim is invalid")

        try:
            rotation = client.rotation(now=time.time())
            if persisted is None:
                request = session.join_request(
                    invite_nonce=verified["payload"]["nonce"],
                    endpoint_addrs=args.advertise,
                )
                acceptance = client.join(
                    invite_token=bundle["token"],
                    join_envelope=request,
                )
                accepted_digest = client.accepted_seed_key_digest(
                    acceptance,
                    now=time.time(),
                )
                session.accept_join(
                    acceptance,
                    seed_key_digest=accepted_digest,
                )
                restart_count = 0
            else:
                request = session.resume_request(
                    previous_generation=int(persisted["membership_generation"]),
                    previous_incarnation=persisted["incarnation"],
                    endpoint_addrs=args.advertise,
                )
                acceptance = client.resume(resume_envelope=request)
                accepted_digest = client.accepted_seed_key_digest(
                    acceptance,
                    now=time.time(),
                )
                session.accept_resume(
                    acceptance,
                    seed_key_digest=accepted_digest,
                )
                restart_count = int(persisted["restart_count"]) + 1
            acceptance_record = acceptance.get("verification_key")
            if not isinstance(acceptance_record, dict):
                records = verified.get("seed_key_records")
                acceptance_record = (
                    dict(records[0])
                    if isinstance(records, (list, tuple))
                    and len(records) == 1
                    and isinstance(records[0], dict)
                    else None
                )
            if not isinstance(acceptance_record, dict):
                raise ValueError("membership acceptance key is invalid")
            save_membership_state(
                state_root.path,
                {
                    "protocol": MEMBERSHIP_STATE_PROTOCOL,
                    "node_id": args.node_id,
                    "swarm_id": verified["payload"]["swarm_id"],
                    "seed_node_id": seed_identity["seed_node_id"],
                    "seed_url": verified["payload"]["seed_url"],
                    "seed_key_digest": accepted_digest,
                    "seed_key_records": [dict(acceptance_record)],
                    "endpoint_id": signer.endpoint_id,
                    "incarnation": runtime_incarnation,
                    "membership_generation": session.generation,
                    "restart_count": restart_count,
                },
            )
            state_root.revalidate()
            if (
                rotation is not None
                and session.seed_key_digest
                == rotation["transition"]["old_seed_key_digest"]
            ):
                session.trust_seed_rotation(
                    rotation["transition"],
                    old_endpoint_id=rotation["old_signature"]["signer_endpoint_id"],
                    new_endpoint_id=rotation["new_signature"]["signer_endpoint_id"],
                )
                acknowledgement = session.seed_rotation_acknowledgement()
                client.send_member_message(acknowledgement, now=time.time() + 1.0)
                acknowledged_authority_generation = int(
                    rotation["transition"]["authority_generation"]
                )
        except SeedHTTPError as exc:
            if _join_rejected(exc):
                raise _EntrypointFailure(
                    "node_join_rejected",
                    EXIT_JOIN_REJECTION,
                ) from exc
            raise _EntrypointFailure(
                "node_runtime_failed",
                EXIT_RUNTIME_FAILURE,
            ) from exc
        except Exception as exc:
            raise _EntrypointFailure(
                "node_runtime_failed",
                EXIT_RUNTIME_FAILURE,
            ) from exc

        renewal_policy = RenewalRetryPolicy(
            heartbeat_interval_seconds=args.heartbeat_interval,
            jitter_fraction=args.renewal_jitter_fraction,
            reconnect_base_seconds=args.reconnect_base_seconds,
            reconnect_max_seconds=args.reconnect_max_seconds,
            lease_risk_window_seconds=max(args.heartbeat_interval, 1.0),
        )
        if not _renew_lease(
            client=client,
            session=session,
            lifecycle_state=args.lifecycle_state,
            stopping=stopping,
            policy=renewal_policy,
        ):
            return EXIT_SUCCESS
        lease_expires_at = session.lease_expires_at
        assert lease_expires_at is not None
        observed_at = time.time()
        status: dict[str, object] = {
            "protocol": _STATUS_PROTOCOL,
            "event": "node_started",
            "node_id": args.node_id,
            "node_endpoint_identity_digest": _endpoint_identity_digest(
                signer.endpoint_id
            ),
            "membership_generation": session.generation,
            "connectivity_state": "online",
            "last_signed_observation_unix_ms": int(observed_at * 1_000),
            "renewal_deadline_unix_ms": int(lease_expires_at * 1_000),
            "reconnect_action": "none",
            "placement_impact": "qualification_still_required",
            "route_ready": False,
        }
        if process is not None:
            status["node_process_pid"] = process.pid
        elif membership_control_only:
            status["runtime_ownership"] = "product_route"
        else:
            status["source_only"] = True
        if persisted is not None:
            status["membership_resumed"] = True
        _emit_status(status)
        while not stopping.wait(renewal_policy.heartbeat_delay(random.random())):
            rotation = client.rotation(now=time.time())
            if (
                rotation is not None
                and session.seed_key_digest
                == rotation["transition"]["old_seed_key_digest"]
            ):
                session.trust_seed_rotation(
                    rotation["transition"],
                    old_endpoint_id=rotation["old_signature"]["signer_endpoint_id"],
                    new_endpoint_id=rotation["new_signature"]["signer_endpoint_id"],
                )
                authority_generation = int(
                    rotation["transition"]["authority_generation"]
                )
                if acknowledged_authority_generation != authority_generation:
                    acknowledgement = session.seed_rotation_acknowledgement()
                    client.send_member_message(
                        acknowledgement,
                        now=time.time() + 1.0,
                    )
                    acknowledged_authority_generation = authority_generation
            if not _renew_lease(
                client=client,
                session=session,
                lifecycle_state=args.lifecycle_state,
                stopping=stopping,
                policy=renewal_policy,
            ):
                break
    except _EntrypointFailure as exc:
        failure = exc
    except Exception:
        failure = _EntrypointFailure(
            "node_runtime_failed",
            EXIT_RUNTIME_FAILURE,
        )
    finally:
        cleanup_phases = []
        if process is not None:
            try:
                process.close()
            except Exception:
                cleanup_phases.append("process")
        if temporary_root is not None:
            try:
                _remove_temporary_root(temporary_root)
            except Exception:
                cleanup_phases.append("temporary_root")
        if artifact_root is not None:
            try:
                artifact_root.close()
            except Exception:
                cleanup_phases.append("artifact_root")
        for signum in reversed(tuple(previous)):
            try:
                signal.signal(signum, previous[signum])
            except Exception:
                cleanup_phases.append("signal_restoration")
        if cleanup_phases:
            failure = _aggregate_cleanup_failures(failure, cleanup_phases)
    if failure is not None:
        raise failure from None
    return EXIT_SUCCESS


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state_root, bundle, verified, client, sidecar, identities, persisted = _preflight(args)
    failure: _EntrypointFailure | None = None
    result = EXIT_SUCCESS
    try:
        if args.dry_run:
            _emit_status(
                {
                    "protocol": _STATUS_PROTOCOL,
                    "event": "node_dry_run",
                    "route_ready": False,
                }
            )
        else:
            with state_root.working_directory():
                result = _run_bound(
                    args,
                    state_root,
                    bundle,
                    verified,
                    client,
                    sidecar,
                    identities,
                    persisted,
                )
    except _EntrypointFailure as exc:
        failure = exc
    except Exception:
        failure = _EntrypointFailure(
            "node_runtime_failed",
            EXIT_RUNTIME_FAILURE,
        )
    finally:
        try:
            state_root.close()
        except Exception:
            failure = _aggregate_cleanup_failures(
                failure,
                ("state_root",),
            )
    if failure is not None:
        raise failure from None
    return result


def main() -> None:
    try:
        raise SystemExit(run())
    except _EntrypointFailure as exc:
        print(exc.code, file=sys.stderr)
        raise SystemExit(exc.exit_status) from None
    except Exception:
        print("node_runtime_failed", file=sys.stderr)
        raise SystemExit(EXIT_RUNTIME_FAILURE) from None


if __name__ == "__main__":
    main()
