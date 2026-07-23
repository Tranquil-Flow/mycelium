# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runnable persistent Mycelium membership and physical-node service."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
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
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError, _validate_endpoint_url

from .identity import load_or_create_node_signer
from .membership import NodeMembershipSession
from .process import (
    _ExecutableIdentity,
    PhysicalNodeProcess,
    build_physical_node_command,
    capture_executable_identity,
    physical_service_interpreter_identity,
    private_directory_parent_fd,
    private_directory_path,
    validate_physical_node_launch_shape,
)


_STATUS_PROTOCOL = "mycelium.node_main_status.v1"
_DEFAULT_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}
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
_JOIN_REJECTION_BAD_REQUEST_CODES = frozenset(
    {
        "invite_expired",
        "invite_field_invalid",
        "invite_malformed",
        "invite_protocol_invalid",
        "join_request_protocol_required",
        "membership_endpoint_addr_invalid",
        "membership_endpoint_id_mismatch",
        "membership_envelope_invalid",
        "membership_field_unusable",
        "membership_fields_invalid",
        "membership_generation_invalid",
        "membership_identifier_invalid",
        "membership_integer_invalid",
        "membership_join_generation_invalid",
        "membership_message_expired",
        "membership_message_from_future",
        "membership_message_invalid",
        "membership_peer_class_invalid",
        "membership_protocol_invalid",
        "membership_runtime_capability_invalid",
        "membership_runtime_capability_mismatch",
        "membership_sender_endpoint_mismatch",
        "membership_signature_invalid",
        "membership_signer_endpoint_mismatch",
        "membership_swarm_mismatch",
        "membership_text_invalid",
        "membership_time_invalid",
        "membership_ttl_invalid",
        "membership_verifier_invalid",
        "seed_join_key_invalid",
        "seed_join_mismatch",
        "seed_join_retry_mismatch",
        "seed_member_identity_reused",
        "seed_node_endpoint_conflict",
    }
)
_JOIN_REJECTION_UNAUTHORIZED_CODES = frozenset(
    {
        "invite_signature_invalid",
        "membership_key_pin_mismatch",
        "membership_signature_invalid",
    }
)
_JOIN_REJECTION_CONFLICT_CODES = frozenset(
    {
        "invite_replayed",
        "seed_join_invite_replayed",
        "seed_node_key_conflict",
    }
)


class _EntrypointFailure(RuntimeError):
    def __init__(self, code: str, exit_status: int) -> None:
        self.code = code
        self.exit_status = exit_status
        super().__init__(code)


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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--join-bundle-file",
        "--seed-invite",
        dest="join_bundle_file",
    )
    source.add_argument("--join-bundle-stdin", action="store_true")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--advertise", action="append", required=True)
    parser.add_argument("--sidecar-path")
    parser.add_argument("--run-id", default="node-main-run")
    parser.add_argument("--deployment-id", default="node-main-unassigned")
    parser.add_argument("--incarnation", default="node-main")
    parser.add_argument("--heartbeat-interval", type=float, default=30.0)
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
    Path,
    dict[str, Any],
    dict[str, Any],
    SeedHTTPClient,
    Path,
    tuple[_ExecutableIdentity, _ExecutableIdentity, _ExecutableIdentity],
]:
    try:
        if not math.isfinite(args.heartbeat_interval) or args.heartbeat_interval <= 0:
            raise ValueError("heartbeat interval is invalid")
        data_dir = _private_directory(args.data_dir, create=not args.dry_run)
        if args.join_bundle_stdin:
            bundle = _stdin_document()
        else:
            bundle = _canonical_document(args.join_bundle_file)
        now = time.time()
        verified = verify_invite_bundle(bundle, now=now)
        client = SeedHTTPClient.from_invite_bundle(bundle, now=now)
        sidecar = _sidecar_path(args.sidecar_path)
        advertised_endpoints = [
            _validate_advertised_endpoint(value) for value in args.advertise
        ]
        service_script = (
            Path(__file__).resolve().parents[1] / "physical_inference_node.py"
        )
        interpreter = _service_interpreter()
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
            endpoint_id="node-preflight-endpoint"
        )
        validation_session = NodeMembershipSession(
            node_id=args.node_id,
            swarm_id=verified["payload"]["swarm_id"],
            seed_node_id="seed-preflight-node",
            signer=validation_signer,
            incarnation=args.incarnation,
            software_version="mycelium-node-main",
            peer_class="mac_mlx_iroh",
            runtime_capability=_DEFAULT_CAPABILITY,
        )
        validation_session.join_request(
            invite_nonce=verified["payload"]["nonce"],
            endpoint_addrs=advertised_endpoints,
        )
        return data_dir, bundle, verified, client, sidecar, identities
    except _EntrypointFailure:
        raise
    except Exception as exc:
        raise _EntrypointFailure(
            "node_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        ) from exc


def _join_rejected(exc: SeedHTTPError) -> bool:
    if exc.status == 400:
        return exc.code in _JOIN_REJECTION_BAD_REQUEST_CODES
    if exc.status == 401:
        return exc.code in _JOIN_REJECTION_UNAUTHORIZED_CODES
    if exc.status == 409:
        return exc.code in _JOIN_REJECTION_CONFLICT_CODES
    return False


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    data_dir, bundle, verified, client, sidecar, identities = _preflight(args)
    if args.dry_run:
        _emit_status(
            {
                "protocol": _STATUS_PROTOCOL,
                "event": "node_dry_run",
                "route_ready": False,
            }
        )
        return EXIT_SUCCESS

    try:
        seed_identity = client.identity(now=time.time() + 1.0)
    except Exception as exc:
        raise _EntrypointFailure(
            "node_runtime_failed",
            EXIT_RUNTIME_FAILURE,
        ) from exc

    temporary_root: _TemporaryRoot | None = None
    try:
        signer = load_or_create_node_signer(data_dir / "identity" / "node.key")
        session = NodeMembershipSession(
            node_id=args.node_id,
            swarm_id=verified["payload"]["swarm_id"],
            seed_node_id=seed_identity["seed_node_id"],
            signer=signer,
            incarnation=args.incarnation,
            software_version="mycelium-node-main",
            peer_class="mac_mlx_iroh",
            runtime_capability=_DEFAULT_CAPABILITY,
        )
        artifact_root = _private_directory(data_dir / "artifacts")
        temporary_root = _temporary_root()
        command = build_physical_node_command(
            python_executable=_service_interpreter(),
            service_script=Path(__file__).resolve().parents[1]
            / "physical_inference_node.py",
            run_id=args.run_id,
            deployment_id=args.deployment_id,
            node_id=args.node_id,
            artifact_root=artifact_root,
            socket_root=temporary_root.path,
            sidecar_binary=sidecar,
            sidecar_local_only=False,
        )
    except Exception as exc:
        if temporary_root is not None:
            try:
                _remove_temporary_root(temporary_root)
            except Exception:
                raise _EntrypointFailure(
                    "node_runtime_failed",
                    EXIT_RUNTIME_FAILURE,
                ) from None
        raise _EntrypointFailure(
            "node_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        ) from exc

    process: PhysicalNodeProcess | None = None
    stopping = threading.Event()
    previous: dict[int, object] = {}
    failure: _EntrypointFailure | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
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

        request = session.join_request(
            invite_nonce=verified["payload"]["nonce"],
            endpoint_addrs=args.advertise,
        )
        try:
            acceptance = client.join(
                invite_token=bundle["token"],
                join_envelope=request,
            )
            session.accept_join(
                acceptance,
                seed_key_digest=verified["seed_key_digest"],
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

        heartbeat = session.heartbeat(lifecycle_state="NEW", active_requests=0)
        renewal = client.send_member_message(
            heartbeat,
            now=time.time() + 1.0,
        )
        session.accept_lease_renewal(
            renewal,
            heartbeat_message_id=heartbeat["message"]["message_id"],
        )
        _emit_status(
            {
                "protocol": _STATUS_PROTOCOL,
                "event": "node_started",
                "node_id": args.node_id,
                "node_endpoint_id": signer.endpoint_id,
                "membership_generation": session.generation,
                "seed_url": verified["payload"]["seed_url"],
                "node_process_pid": process.pid,
                "route_ready": False,
            }
        )
        while not stopping.wait(args.heartbeat_interval):
            heartbeat = session.heartbeat(
                lifecycle_state="NEW",
                active_requests=0,
            )
            renewal = client.send_member_message(
                heartbeat,
                now=time.time() + 1.0,
            )
            session.accept_lease_renewal(
                renewal,
                heartbeat_message_id=heartbeat["message"]["message_id"],
            )
    except _EntrypointFailure as exc:
        failure = exc
    except Exception:
        failure = _EntrypointFailure(
            "node_runtime_failed",
            EXIT_RUNTIME_FAILURE,
        )
    finally:
        if process is not None:
            try:
                process.close()
            except Exception:
                failure = _EntrypointFailure(
                    "node_runtime_failed",
                    EXIT_RUNTIME_FAILURE,
                )
        try:
            assert temporary_root is not None
            _remove_temporary_root(temporary_root)
        except Exception:
            failure = _EntrypointFailure(
                "node_runtime_failed",
                EXIT_RUNTIME_FAILURE,
            )
        for signum in reversed(tuple(previous)):
            try:
                signal.signal(signum, previous[signum])
            except Exception:
                failure = _EntrypointFailure(
                    "node_runtime_failed",
                    EXIT_RUNTIME_FAILURE,
                )
    if failure is not None:
        raise failure from None
    return EXIT_SUCCESS


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
