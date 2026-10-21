# SPDX-License-Identifier: AGPL-3.0-only
"""Runnable persistent Mycelium seed service."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import stat
from typing import Any, NoReturn, Sequence
from urllib.parse import urlsplit

from mycelium_host_platform import StopRequest
from mycelium_internet.bootstrap import PublicBootstrapPolicy, canonical_https_origin
from mycelium_invite import SqliteInviteRegistry
from mycelium_mobile.android_onboarding import validate_android_quota
from mycelium_node.identity import load_or_create_node_signer
from mycelium_node.process import (
    PrivateDirectoryLease,
    private_directory_lease,
    private_directory_path,
)
from mycelium_platform_neutral import network_mode as network_mode_module
from mycelium_platform_neutral.network_mode import NetworkMode
from mycelium_platform_neutral.offline import add_offline_argument, apply_offline
from mycelium_qualification.evidence import canonical_json_bytes

from .authority import load_bound_seed_signer
from .coordinator import SeedCoordinator, _segment
from .http import (
    SeedHTTPServer,
    _validate_bind_address,
    _validate_endpoint_url,
)
from .operator import verify_seed_key_transition
from .pairing import MAX_APPROVALS, MAX_WINDOW_SECONDS, AndroidPairingChannel
from .state import SqliteSeedState


_STATUS_PROTOCOL = "mycelium.seed_main_status.v1"
_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})

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


def _aggregate_cleanup_failures(
    failure: _EntrypointFailure | None,
    phases: Sequence[str],
) -> _EntrypointFailure:
    if failure is None:
        failure = _EntrypointFailure(
            "seed_runtime_failed",
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
    # Default 127.0.0.1 (or the private interface in --network-mode lan),
    # applied by _resolve_network.
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", type=_canonical_port, default=8765)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--advertised-url")
    parser.add_argument(
        "--trusted-https-proxy",
        action="store_true",
        help="advertise an https URL served by a loopback HTTPS proxy",
    )
    parser.add_argument("--pairing-port", type=_canonical_port)
    parser.add_argument("--pairing-url")
    parser.add_argument("--pairing-quota-file")
    parser.add_argument("--pairing-window-seconds", type=int, default=600)
    parser.add_argument("--pairing-max-approvals", type=int, default=1)
    parser.add_argument("--swarm-id", default="mycelium-swarm")
    parser.add_argument("--seed-node-id", default="seed-node")
    parser.add_argument("--incarnation", default="seed-main")
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    network_mode_module.add_network_mode_argument(parser)
    add_offline_argument(parser)
    return parser


_STATUS_LOCK = threading.Lock()


def _emit_status(status: dict[str, object]) -> None:
    with _STATUS_LOCK:
        sys.stdout.buffer.write(canonical_json_bytes(status) + b"\n")
        sys.stdout.buffer.flush()


def _pairing_preflight(args: argparse.Namespace) -> None:
    args.pairing_quota = None
    if args.pairing_port is None:
        if args.pairing_quota_file is not None or args.pairing_url is not None:
            raise ValueError("pairing options require --pairing-port")
        return
    if args.pairing_quota_file is None:
        raise ValueError("pairing requires an owner quota")
    if args.pairing_port != 0 and args.pairing_port == args.port:
        raise ValueError("pairing port must differ from the seed port")
    if not 1 <= args.pairing_window_seconds <= MAX_WINDOW_SECONDS:
        raise ValueError("pairing window is invalid")
    if not 1 <= args.pairing_max_approvals <= MAX_APPROVALS:
        raise ValueError("pairing approvals are invalid")
    if args.pairing_url is not None:
        scheme = args.pairing_url.split(":", 1)[0]
        if scheme != ("https" if args.trusted_https_proxy else "http"):
            raise ValueError("pairing URL scheme is invalid")
    elif args.trusted_https_proxy:
        raise ValueError("pairing URL is required behind a proxy")
    raw = Path(args.pairing_quota_file).read_bytes()
    args.pairing_quota = validate_android_quota(json.loads(raw))


def _resolve_network(
    args: argparse.Namespace,
    *,
    environ: Any = None,
    lan_address: Any = None,
) -> NetworkMode:
    """Apply the network mode to bind/advertise; internet keeps today's values."""
    mode = network_mode_module.resolve_network_mode(args.network_mode, environ)
    # lan/local imply offline unless overridden; internet leaves the environment alone.
    apply_offline(args.offline, mode, os.environ if environ is None else environ)
    if mode is NetworkMode.INTERNET:
        if args.bind is None:
            args.bind = "127.0.0.1"
        return mode
    if mode is NetworkMode.LOCAL:
        if args.bind is None:
            args.bind = "127.0.0.1"
        if args.bind not in _LOOPBACK_BINDS:
            raise ValueError("local mode binds loopback only")
        return mode
    lookup = lan_address or network_mode_module.first_private_lan_address
    if args.trusted_https_proxy:
        raise ValueError("lan mode has no hosted proxy")
    if args.bind is None:
        address = lookup()
        if address is None:
            raise ValueError("no private LAN address is available")
        args.bind = address
    elif args.bind != "0.0.0.0" and not network_mode_module.is_rfc1918(args.bind):
        raise ValueError("lan mode binds a private interface")
    if args.advertised_url is None:
        if args.bind == "0.0.0.0":
            address = lookup()
            if address is None or args.port == 0:
                raise ValueError("advertised URL cannot be derived")
            args.advertised_url = f"http://{address}:{args.port}"
    else:
        host = urlsplit(args.advertised_url).hostname
        try:
            literal = ipaddress.ip_address(host) if host else None
        except ValueError:
            literal = None
        if literal is not None and not network_mode_module.is_rfc1918(str(literal)):
            raise ValueError("lan mode advertises a private address")
    return mode


def _preflight(args: argparse.Namespace) -> PrivateDirectoryLease:
    state_root: PrivateDirectoryLease | None = None
    try:
        _resolve_network(args)
        _segment(args.swarm_id, "swarm_id")
        _segment(args.seed_node_id, "seed_node_id")
        _segment(args.incarnation, "incarnation")
        _validate_bind_address(args.bind, args.port)
        if args.bind in {"0.0.0.0", "::"} and args.advertised_url is None:
            raise ValueError("advertised URL is required for wildcard binds")
        if args.trusted_https_proxy:
            if args.bind not in _LOOPBACK_BINDS or args.advertised_url is None:
                raise ValueError("trusted https proxy requires a loopback bind")
            canonical = canonical_https_origin(args.advertised_url)
            if canonical != args.advertised_url:
                raise ValueError("advertised https URL is not canonical")
            _validate_endpoint_url(args.advertised_url, expected_scheme="https")
        elif args.advertised_url is not None:
            _validate_endpoint_url(
                args.advertised_url,
                expected_scheme="http",
                expected_host=(None if args.bind in {"0.0.0.0", "::"} else args.bind),
                expected_port=None if args.port == 0 else args.port,
            )
        _pairing_preflight(args)
        if args.init_only and args.dry_run:
            raise ValueError("init-only and dry-run are mutually exclusive")
        state_root = private_directory_lease(args.data_dir, create=args.init_only)
        if not state_root.exists and not args.dry_run:
            raise ValueError("seed state root is missing")
        return state_root
    except Exception as exc:
        failure = _EntrypointFailure(
            "seed_preflight_failed",
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


def _run_bound(
    args: argparse.Namespace,
    state_root: PrivateDirectoryLease,
) -> int:
    try:
        state_root.revalidate()
        key_path = Path("identity") / "seed.key"
        database = Path("state.sqlite3")
        if args.init_only:
            if key_path.exists() or key_path.is_symlink() or database.exists():
                raise ValueError("seed state is already initialized")
            signer = load_or_create_node_signer(key_path)
        state_root.revalidate()
        if not args.init_only and not database.is_file():
            raise ValueError("seed database is missing")
        state_root.revalidate()
        invite_registry = SqliteInviteRegistry(database)
        state = SqliteSeedState(database)
        if not args.init_only:
            binding = state.identity_binding()
            signer = load_bound_seed_signer(
                Path("identity"),
                expected_digest=binding["seed_key_digest"],
            )
        state_root.revalidate()
        coordinator = SeedCoordinator(
            swarm_id=args.swarm_id,
            seed_node_id=args.seed_node_id,
            seed_url=None,
            signer=signer,
            invite_registry=invite_registry,
            incarnation=args.incarnation,
            state=state,
        )
        rotation_envelope = None
        rotation_path = Path("identity") / "seed.rotation.json"
        if rotation_path.exists() or rotation_path.is_symlink():
            metadata = rotation_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError("seed rotation record is unsafe")
            raw_rotation = rotation_path.read_bytes()
            rotation_envelope = json.loads(raw_rotation)
            if canonical_json_bytes(rotation_envelope) != raw_rotation:
                raise ValueError("seed rotation record is noncanonical")
            transition = verify_seed_key_transition(
                rotation_envelope,
                now=0.0,
            )
            if signer.verification_key_digest not in {
                transition["old_seed_key_digest"],
                transition["new_seed_key_digest"],
            }:
                raise ValueError("seed rotation record is not bound")
        state_root.revalidate()
    except Exception as exc:
        raise _EntrypointFailure(
            "seed_preflight_failed",
            EXIT_PREFLIGHT_FAILURE,
        ) from exc

    if args.init_only:
        _emit_status(
            {
                "protocol": _STATUS_PROTOCOL,
                "event": "seed_initialized",
                "seed_endpoint_id": signer.endpoint_id,
                "seed_key_digest": signer.verification_key_digest,
                "route_ready": False,
            }
        )
        return EXIT_SUCCESS

    server: SeedHTTPServer | None = None
    pairing: AndroidPairingChannel | None = None
    # Set from the SIGINT/SIGTERM handler: never a threading.Event, whose
    # set() takes the lock its own wait() holds on this (the main) thread.
    stopping = StopRequest()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    previous: dict[int, object] = {}
    failure: _EntrypointFailure | None = None
    try:
        state_root.revalidate()
        if args.trusted_https_proxy:
            server = SeedHTTPServer(
                coordinator,
                host=args.bind,
                port=args.port,
                rotation_envelope=rotation_envelope,
                public_seed_url=args.advertised_url,
                policy=PublicBootstrapPolicy(canonical_origin=args.advertised_url),
            )
        else:
            server = SeedHTTPServer(
                coordinator,
                host=args.bind,
                port=args.port,
                advertised_url=args.advertised_url,
                rotation_envelope=rotation_envelope,
            )
        state_root.revalidate()
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        server.start()
        started = {
            "protocol": _STATUS_PROTOCOL,
            "event": "seed_started",
            "seed_url": coordinator.seed_url,
            "seed_endpoint_id": signer.endpoint_id,
            "route_ready": False,
        }
        if args.trusted_https_proxy:
            started["local_url"] = server.base_url
        _emit_status(started)
        if args.pairing_port is not None:
            pairing = AndroidPairingChannel(
                coordinator,
                quota=args.pairing_quota,
                host=args.bind,
                port=args.pairing_port,
                pairing_url=args.pairing_url,
                trusted_https_proxy=args.trusted_https_proxy,
                window_seconds=args.pairing_window_seconds,
                max_approvals=args.pairing_max_approvals,
                on_event=lambda event: _emit_status(
                    {"protocol": _STATUS_PROTOCOL, **event}
                ),
            )
            pairing.start()
            _emit_status(
                {
                    "protocol": _STATUS_PROTOCOL,
                    "event": "pairing_channel_open",
                    "local_url": pairing.local_url,
                    "pairing_url": pairing.pairing_url,
                    "invite_qr": pairing.channel_qr,
                    "expires_at": pairing.expires_at,
                    "max_approvals": pairing.max_approvals,
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
        cleanup_phases = []
        if pairing is not None:
            try:
                pairing.close()
            except Exception:
                cleanup_phases.append("pairing")
        if server is not None:
            try:
                server.close()
            except Exception:
                cleanup_phases.append("server")
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
    state_root = _preflight(args)
    failure: _EntrypointFailure | None = None
    result = EXIT_SUCCESS
    try:
        if args.dry_run:
            _emit_status(
                {
                    "protocol": _STATUS_PROTOCOL,
                    "event": "seed_dry_run",
                    "route_ready": False,
                }
            )
        else:
            with state_root.working_directory():
                result = _run_bound(args, state_root)
    except _EntrypointFailure as exc:
        failure = exc
    except Exception:
        failure = _EntrypointFailure(
            "seed_runtime_failed",
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
        print("seed_runtime_failed", file=sys.stderr)
        raise SystemExit(EXIT_RUNTIME_FAILURE) from None


if __name__ == "__main__":
    main()
