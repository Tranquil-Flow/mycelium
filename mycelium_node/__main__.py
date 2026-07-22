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
from typing import Any, Sequence

from mycelium_invite import verify_invite_bundle
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_seed.http import SeedHTTPClient

from .identity import load_or_create_node_signer
from .membership import NodeMembershipSession
from .process import PhysicalNodeProcess, build_physical_node_command


_STATUS_PROTOCOL = "mycelium.node_main_status.v1"
_DEFAULT_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}


def _private_directory(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(path))
    try:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError("data directory is invalid")
            if metadata.st_uid != os.getuid():
                raise ValueError("data directory owner is invalid")
        else:
            path.mkdir(mode=0o700, parents=True)
        path.chmod(0o700)
    except OSError as exc:
        raise ValueError("data directory is unavailable") from exc
    return path


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
        ):
            raise ValueError("seed invite file is invalid")
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("seed invite file is invalid") from exc
    if not isinstance(document, dict) or canonical_json_bytes(document) != raw:
        raise ValueError("seed invite file is invalid")
    return document


def _sidecar_path(value: str | None) -> Path:
    if value is not None:
        candidates = [Path(value).expanduser()]
    else:
        root = Path(__file__).resolve().parents[1]
        candidates = [
            root / "native" / "iroh_transport" / "target" / "release" / "mycelium-iroh-sidecar",
            root / "native" / "iroh_transport" / "target" / "debug" / "mycelium-iroh-sidecar",
        ]
        discovered = shutil.which("mycelium-iroh-sidecar")
        if discovered is not None:
            candidates.append(Path(discovered))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    raise ValueError("sidecar binary is unavailable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mycelium_node")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--seed-invite", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--advertise", action="append", required=True)
    parser.add_argument("--sidecar-path")
    parser.add_argument("--run-id", default="node-main-run")
    parser.add_argument("--deployment-id", default="node-main-unassigned")
    parser.add_argument("--incarnation", default="node-main")
    parser.add_argument("--heartbeat-interval", type=float, default=30.0)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        not math.isfinite(args.heartbeat_interval)
        or args.heartbeat_interval <= 0
    ):
        raise ValueError("heartbeat interval is invalid")
    data_dir = _private_directory(args.data_dir)
    bundle = _canonical_document(args.seed_invite)
    now = time.time()
    verified = verify_invite_bundle(bundle, now=now)
    client = SeedHTTPClient.from_invite_bundle(bundle, now=now)
    seed_identity = client.identity(now=time.time() + 1.0)
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
        service_script=Path(__file__).resolve().parents[1] / "physical_inference_node.py",
        run_id=args.run_id,
        deployment_id=args.deployment_id,
        node_id=args.node_id,
        artifact_root=artifact_root,
        socket_root=socket_root,
        sidecar_binary=_sidecar_path(args.sidecar_path),
        sidecar_local_only=False,
    )
    process: PhysicalNodeProcess | None = None
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    previous = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        process = PhysicalNodeProcess(
            command=command,
            node_id=args.node_id,
            run_id=args.run_id,
            deployment_id=args.deployment_id,
        )
        hello = process.command("hello")
        if hello.get("route_ready") is not False:
            raise RuntimeError("node_main_child_claim_invalid")
        request = session.join_request(
            invite_nonce=verified["payload"]["nonce"],
            endpoint_addrs=args.advertise,
        )
        acceptance = client.join(
            invite_token=bundle["token"],
            join_envelope=request,
        )
        session.accept_join(
            acceptance,
            seed_key_digest=verified["seed_key_digest"],
        )
        heartbeat = session.heartbeat(lifecycle_state="NEW", active_requests=0)
        renewal = client.send_member_message(heartbeat, now=time.time() + 1.0)
        session.accept_lease_renewal(
            renewal,
            heartbeat_message_id=heartbeat["message"]["message_id"],
        )
        status = {
            "protocol": _STATUS_PROTOCOL,
            "event": "node_started",
            "node_id": args.node_id,
            "node_endpoint_id": signer.endpoint_id,
            "membership_generation": session.generation,
            "seed_url": verified["payload"]["seed_url"],
            "node_process_pid": process.pid,
            "route_ready": False,
        }
        sys.stdout.buffer.write(canonical_json_bytes(status) + b"\n")
        sys.stdout.buffer.flush()
        while not stopping.wait(args.heartbeat_interval):
            heartbeat = session.heartbeat(lifecycle_state="NEW", active_requests=0)
            renewal = client.send_member_message(heartbeat, now=time.time() + 1.0)
            session.accept_lease_renewal(
                renewal,
                heartbeat_message_id=heartbeat["message"]["message_id"],
            )
    finally:
        if process is not None:
            process.close()
        shutil.rmtree(socket_root, ignore_errors=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (OSError, RuntimeError, ValueError) as exc:
        code = getattr(exc, "code", "node_main_start_failed")
        print(str(code), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
