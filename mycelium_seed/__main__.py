# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runnable persistent Mycelium seed service."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import signal
import sys
import threading
from typing import NoReturn, Sequence

from mycelium_invite import SqliteInviteRegistry
from mycelium_node.identity import load_or_create_node_signer
from mycelium_node.process import (
    PrivateDirectoryLease,
    private_directory_lease,
    private_directory_path,
)
from mycelium_qualification.evidence import canonical_json_bytes

from .coordinator import SeedCoordinator, _segment
from .http import (
    SeedHTTPServer,
    _validate_bind_address,
    _validate_endpoint_url,
)


_STATUS_PROTOCOL = "mycelium.seed_main_status.v1"

# Stable process contract shared with the node entrypoint.
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
        self.exit(EXIT_PREFLIGHT_FAILURE, "seed_preflight_failed\n")


def _private_directory(value: str | Path, *, create: bool = True) -> Path:
    return private_directory_path(value, create=create)


def _canonical_port(value: str) -> int:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"(?:0|[1-9][0-9]{0,4})", value) is None
    ):
        raise argparse.ArgumentTypeError("port is invalid")
    port = int(value)
    if port > 65535:
        raise argparse.ArgumentTypeError("port is invalid")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="python -m mycelium_seed")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=_canonical_port, default=8765)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--advertised-url")
    parser.add_argument("--swarm-id", default="mycelium-swarm")
    parser.add_argument("--seed-node-id", default="seed-node")
    parser.add_argument("--incarnation", default="seed-main")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _emit_status(status: dict[str, object]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(status) + b"\n")
    sys.stdout.buffer.flush()


def _preflight(args: argparse.Namespace) -> PrivateDirectoryLease:
    state_root: PrivateDirectoryLease | None = None
    try:
        _segment(args.swarm_id, "swarm_id")
        _segment(args.seed_node_id, "seed_node_id")
        _segment(args.incarnation, "incarnation")
        _validate_bind_address(args.bind, args.port)
        if args.bind in {"0.0.0.0", "::"} and args.advertised_url is None:
            raise ValueError("advertised URL is required for wildcard binds")
        if args.advertised_url is not None:
            if args.port == 0:
                raise ValueError("advertised URL requires a fixed port")
            _validate_endpoint_url(
                args.advertised_url,
                expected_scheme="http",
                expected_host=(None if args.bind in {"0.0.0.0", "::"} else args.bind),
                expected_port=args.port,
            )
        state_root = private_directory_lease(
            args.data_dir,
            create=not args.dry_run,
        )
        return state_root
    except Exception as exc:
        failure = exc
        if state_root is not None:
            try:
                state_root.close()
            except Exception as close_exc:
                failure = close_exc
        raise _EntrypointFailure(
            "seed_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        ) from failure


def _run_bound(
    args: argparse.Namespace,
    state_root: PrivateDirectoryLease,
) -> int:
    try:
        state_root.revalidate()
        signer = load_or_create_node_signer(Path("identity") / "seed.key")
        state_root.revalidate()
        database = Path("state.sqlite3")
        state_root.revalidate()
        invite_registry = SqliteInviteRegistry(database)
        state_root.revalidate()
        coordinator = SeedCoordinator(
            swarm_id=args.swarm_id,
            seed_node_id=args.seed_node_id,
            seed_url=None,
            signer=signer,
            invite_registry=invite_registry,
            incarnation=args.incarnation,
        )
        state_root.revalidate()
    except Exception as exc:
        raise _EntrypointFailure(
            "seed_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        ) from exc

    server: SeedHTTPServer | None = None
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    previous: dict[int, object] = {}
    failure: _EntrypointFailure | None = None
    try:
        state_root.revalidate()
        server = SeedHTTPServer(
            coordinator,
            host=args.bind,
            port=args.port,
            advertised_url=args.advertised_url,
        )
        state_root.revalidate()
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        server.start()
        _emit_status(
            {
                "protocol": _STATUS_PROTOCOL,
                "event": "seed_started",
                "seed_url": coordinator.seed_url,
                "seed_endpoint_id": signer.endpoint_id,
                "route_ready": False,
            }
        )
        while not stopping.wait(0.5):
            pass
    except _EntrypointFailure as exc:
        failure = exc
    except Exception:
        failure = _EntrypointFailure(
            "seed_runtime_failed",
            EXIT_RUNTIME_FAILURE,
        )
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                failure = _EntrypointFailure(
                    "seed_runtime_failed",
                    EXIT_RUNTIME_FAILURE,
                )
        for signum in reversed(tuple(previous)):
            try:
                signal.signal(signum, previous[signum])
            except Exception:
                failure = _EntrypointFailure(
                    "seed_runtime_failed",
                    EXIT_RUNTIME_FAILURE,
                )
    if failure is not None:
        raise failure from None
    return EXIT_SUCCESS


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state_root = _preflight(args)
    try:
        if args.dry_run:
            _emit_status(
                {
                    "protocol": _STATUS_PROTOCOL,
                    "event": "seed_dry_run",
                    "route_ready": False,
                }
            )
            return EXIT_SUCCESS
        with state_root.working_directory():
            return _run_bound(args, state_root)
    finally:
        state_root.close()


def main() -> None:
    try:
        raise SystemExit(run())
    except _EntrypointFailure as exc:
        print(exc.code, file=sys.stderr)
        raise SystemExit(exc.exit_status) from None
    except Exception:
        print("seed_runtime_failed", file=sys.stderr)
        raise SystemExit(EXIT_RUNTIME_FAILURE) from None


if __name__ == "__main__":
    main()
