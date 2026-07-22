# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runnable persistent Mycelium seed service."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import stat
import sys
import threading
from typing import Sequence

from mycelium_invite import SqliteInviteRegistry
from mycelium_node.identity import load_or_create_node_signer
from mycelium_qualification.evidence import canonical_json_bytes

from .coordinator import SeedCoordinator
from .http import SeedHTTPServer


_STATUS_PROTOCOL = "mycelium.seed_main_status.v1"


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mycelium_seed")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--advertised-url")
    parser.add_argument("--swarm-id", default="mycelium-swarm")
    parser.add_argument("--seed-node-id", default="seed-node")
    parser.add_argument("--incarnation", default="seed-main")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.port < 0 or args.port > 65535:
        raise ValueError("port is invalid")
    data_dir = _private_directory(args.data_dir)
    signer = load_or_create_node_signer(data_dir / "identity" / "seed.key")
    database = data_dir / "state.sqlite3"
    coordinator = SeedCoordinator(
        swarm_id=args.swarm_id,
        seed_node_id=args.seed_node_id,
        seed_url=None,
        signer=signer,
        invite_registry=SqliteInviteRegistry(database),
        incarnation=args.incarnation,
    )
    server = SeedHTTPServer(
        coordinator,
        host=args.bind,
        port=args.port,
        advertised_url=args.advertised_url,
    )
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    previous = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        server.start()
        status = {
            "protocol": _STATUS_PROTOCOL,
            "event": "seed_started",
            "seed_url": coordinator.seed_url,
            "seed_endpoint_id": signer.endpoint_id,
            "route_ready": False,
        }
        sys.stdout.buffer.write(canonical_json_bytes(status) + b"\n")
        sys.stdout.buffer.flush()
        while not stopping.wait(0.5):
            pass
    finally:
        server.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (OSError, RuntimeError, ValueError) as exc:
        code = getattr(exc, "code", "seed_main_start_failed")
        print(str(code), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
