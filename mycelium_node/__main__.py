# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runnable persistent Mycelium membership and physical-node service."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from typing import Any, NoReturn, Sequence

from mycelium_invite import verify_invite_bundle
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError

from .identity import load_or_create_node_signer
from .membership import NodeMembershipSession
from .process import PhysicalNodeProcess, build_physical_node_command


_STATUS_PROTOCOL = "mycelium.node_main_status.v1"
_DEFAULT_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}
_MAX_JOIN_BUNDLE_BYTES = 1024 * 1024
_SHUTDOWN_DEADLINE_SECONDS = 5.0
_SHUTDOWN_REAP_GRACE_SECONDS = 1.0

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


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(EXIT_PREFLIGHT_FAILURE, "node_preflight_failed\n")


def _private_directory(value: str | Path, *, create: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(path))
    try:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ValueError("data directory is invalid")
        elif create:
            path.mkdir(mode=0o700, parents=True)
            path.chmod(0o700)
        else:
            parent = path.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            metadata = parent.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or not os.access(parent, os.W_OK | os.X_OK)
            ):
                raise ValueError("data directory is invalid")
    except OSError as exc:
        raise ValueError("data directory is unavailable") from exc
    return path


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
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_JOIN_BUNDLE_BYTES
        ):
            raise ValueError("join bundle file is invalid")
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("join bundle file is invalid") from exc
    return _canonical_document_bytes(raw)


def _stdin_document() -> dict[str, Any]:
    try:
        raw = sys.stdin.buffer.read(_MAX_JOIN_BUNDLE_BYTES + 1)
    except OSError as exc:
        raise ValueError("join bundle stdin is invalid") from exc
    return _canonical_document_bytes(raw)


def _sidecar_path(value: str | None) -> Path:
    if value is not None:
        candidates = [Path(value).expanduser()]
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
        discovered = shutil.which("mycelium-iroh-sidecar")
        if discovered is not None:
            candidates.append(Path(discovered))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise ValueError("sidecar binary is unavailable")


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


def _preflight(
    args: argparse.Namespace,
) -> tuple[Path, dict[str, Any], dict[str, Any], SeedHTTPClient, Path]:
    try:
        if (
            not math.isfinite(args.heartbeat_interval)
            or args.heartbeat_interval <= 0
        ):
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
        service_script = Path(__file__).resolve().parents[1] / "physical_inference_node.py"
        if not service_script.is_file():
            raise ValueError("node service is unavailable")
        return data_dir, bundle, verified, client, sidecar
    except _EntrypointFailure:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _EntrypointFailure(
            "node_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        ) from exc


def _close_process_with_deadline(process: PhysicalNodeProcess) -> None:
    closed = threading.Event()

    def close() -> None:
        try:
            process.close()
        except (OSError, RuntimeError, ValueError):
            pass
        finally:
            closed.set()

    closer = threading.Thread(target=close, name="mycelium-node-close", daemon=True)
    closer.start()
    if closed.wait(_SHUTDOWN_DEADLINE_SECONDS):
        closer.join()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    closed.wait(_SHUTDOWN_REAP_GRACE_SECONDS)
    closer.join(timeout=0)


def _join_rejected(exc: SeedHTTPError) -> bool:
    return exc.status is not None and 400 <= exc.status < 500


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    data_dir, bundle, verified, client, sidecar = _preflight(args)
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
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _EntrypointFailure(
            "node_runtime_failed",
            EXIT_RUNTIME_FAILURE,
        ) from exc

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
        artifact_root = data_dir / "artifacts"
        artifact_root.mkdir(mode=0o700, exist_ok=True)
        socket_root = Path(tempfile.mkdtemp(prefix="myc-node-", dir="/tmp"))
        command = build_physical_node_command(
            python_executable=Path(sys.executable),
            service_script=Path(__file__).resolve().parents[1]
            / "physical_inference_node.py",
            run_id=args.run_id,
            deployment_id=args.deployment_id,
            node_id=args.node_id,
            artifact_root=artifact_root,
            socket_root=socket_root,
            sidecar_binary=sidecar,
            sidecar_local_only=False,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _EntrypointFailure(
            "node_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        ) from exc

    process: PhysicalNodeProcess | None = None
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    previous: dict[int, object] = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        try:
            process = PhysicalNodeProcess(
                command=command,
                node_id=args.node_id,
                run_id=args.run_id,
                deployment_id=args.deployment_id,
            )
            hello = process.command("hello")
            if hello.get("route_ready") is not False:
                raise RuntimeError("node child claim is invalid")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _EntrypointFailure(
                "node_runtime_failed",
                EXIT_RUNTIME_FAILURE,
            ) from exc

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
        except (RuntimeError, TypeError, ValueError) as exc:
            raise _EntrypointFailure(
                "node_join_rejected",
                EXIT_JOIN_REJECTION,
            ) from exc

        try:
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
        except _EntrypointFailure:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _EntrypointFailure(
                "node_runtime_failed",
                EXIT_RUNTIME_FAILURE,
            ) from exc
    finally:
        if process is not None:
            _close_process_with_deadline(process)
        shutil.rmtree(socket_root, ignore_errors=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return EXIT_SUCCESS


def main() -> None:
    try:
        raise SystemExit(run())
    except _EntrypointFailure as exc:
        print(exc.code, file=sys.stderr)
        raise SystemExit(exc.exit_status) from None
    except (OSError, RuntimeError, TypeError, ValueError):
        print("node_runtime_failed", file=sys.stderr)
        raise SystemExit(EXIT_RUNTIME_FAILURE) from None


if __name__ == "__main__":
    main()
