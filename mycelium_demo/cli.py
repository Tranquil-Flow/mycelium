from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from .device_lab import DeviceLabError, prepare_device_lab
from .doctor import DEFAULT_COMMANDS, canonical_json, local_tcp_port_available, run_preflight

ROOT = Path(__file__).resolve().parents[1]


def _port_value(value: str) -> int | str:
    if value.isascii() and value.isdecimal():
        significant = value.lstrip("0") or "0"
        if len(significant) <= 5:
            return int(significant, 10)
    return value


def _serve_port(value: str) -> int:
    parsed = _port_value(value)
    if isinstance(parsed, int) and 1 <= parsed <= 65_535:
        return parsed
    raise argparse.ArgumentTypeError("port must be an integer from 1 through 65535")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3.14 -m mycelium_demo",
        description="Mycelium local preflight and explicitly bounded demo launchers.",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    doctor = commands.add_parser("doctor", help="run local non-mutating prerequisite checks")
    doctor.add_argument("--repo-root", type=Path, default=ROOT)
    doctor.add_argument("--state-dir", type=Path, required=True)
    doctor.add_argument(
        "--port",
        type=_port_value,
        action="append",
        default=[],
        help="intended local TCP listener port to probe; repeat for multiple ports",
    )

    device_lab = commands.add_parser(
        "device-lab",
        help="prepare trusted LAN HTTPS and launch a bounded physical-browser swarm",
    )
    device_lab.add_argument(
        "--advertise-host",
        required=True,
        help="non-loopback LAN IP address or DNS name reachable by every test device",
    )
    device_lab.add_argument("--bind-host", choices=("0.0.0.0",), default="0.0.0.0")
    device_lab.add_argument("--port", type=_serve_port, default=8787)
    device_lab.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".mycelium" / "device-lab",
    )

    serve = commands.add_parser(
        "serve",
        help="launch bundled fixture UI or genuine local browser/MLX inference",
    )
    serve.add_argument(
        "--mode",
        choices=("fixture", "live"),
        required=True,
        help=(
            "fixture serves bundled synthetic product data; live serves genuine local "
            "browser-stage inference and never changes route readiness"
        ),
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_serve_port)
    serve.add_argument("--state-root", type=Path)
    serve.add_argument("--operator-token-file", type=Path)
    serve.add_argument("--public-origin")
    serve.add_argument("--static-root", type=Path)
    serve.add_argument("--tls-cert", type=Path)
    serve.add_argument("--tls-key", type=Path)
    return parser


def _live_arguments(args: argparse.Namespace) -> list[str]:
    result = [
        "--host",
        args.host,
        "--port",
        str(args.port if args.port is not None else 8787),
    ]
    optional_paths = (
        ("--state-root", args.state_root),
        ("--operator-token-file", args.operator_token_file),
        ("--static-root", args.static_root),
        ("--tls-cert", args.tls_cert),
        ("--tls-key", args.tls_key),
    )
    if args.public_origin is not None:
        result.extend(("--public-origin", args.public_origin))
    for flag, value in optional_paths:
        if value is not None:
            result.extend((flag, str(value)))
    return result


def _fixture_has_live_only_arguments(args: argparse.Namespace) -> bool:
    return any(
        value is not None
        for value in (
            args.state_root,
            args.operator_token_file,
            args.public_origin,
            args.static_root,
            args.tls_cert,
            args.tls_key,
        )
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    which=shutil.which,
    port_available=local_tcp_port_available,
    process_runner=subprocess.run,
    live_server_main=None,
    environ: Mapping[str, str] | None = None,
    fixture_runtime_available=lambda path: path.is_file(),
    device_lab_prepare=prepare_device_lab,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action == "doctor":
        report = run_preflight(
            repo_root=args.repo_root,
            state_dir=args.state_dir,
            commands=DEFAULT_COMMANDS,
            ports=args.port,
            which=which,
            port_available=port_available,
        )
        print(canonical_json(report))
        return 0 if report["local_preflight_ok"] else 1

    if args.action == "device-lab":
        openssl = which("openssl")
        if openssl is None:
            parser.error("device-lab requires OpenSSL")
        try:
            bundle = device_lab_prepare(
                state_root=args.state_root,
                advertise_host=args.advertise_host,
                port=args.port,
                openssl=openssl,
            )
        except DeviceLabError as exc:
            parser.error(str(exc))
        print(
            canonical_json(
                {
                    "protocol": "mycelium.device_lab_prepared.v1",
                    "advertise_host": bundle.advertise_host,
                    "port": bundle.port,
                    "public_origin": bundle.public_origin,
                    "ca_cert": str(bundle.ca_cert),
                    "device_setup": [
                        "transfer_and_trust_ca",
                        "open_unique_join_link_per_device",
                        "wait_until_each_device_is_ready",
                        "run_bounded_test_inference",
                    ],
                    "route_ready": False,
                    "local_evidence_only": True,
                    "tailscale_required": False,
                }
            ),
            flush=True,
        )
        if live_server_main is None:
            from mycelium_interactive.server import main as live_server_main

        return int(
            live_server_main(
                [
                    "--host",
                    args.bind_host,
                    "--port",
                    str(bundle.port),
                    "--state-root",
                    str(bundle.runtime_root),
                    "--public-origin",
                    bundle.public_origin,
                    "--tls-cert",
                    str(bundle.tls_cert),
                    "--tls-key",
                    str(bundle.tls_key),
                ]
            )
        )

    if args.action != "serve":
        raise AssertionError(f"unsupported action: {args.action}")
    if args.mode == "live":
        if live_server_main is None:
            from mycelium_interactive.server import main as live_server_main

        return int(live_server_main(_live_arguments(args)))
    if _fixture_has_live_only_arguments(args):
        parser.error("fixture mode does not accept live-runtime arguments")

    fixture_environment = dict(os.environ if environ is None else environ)
    fixture_environment["VITE_OBSERVATORY_SOURCE_MODE"] = "fixture"
    fixture_root = ROOT / "ui" / "web"
    fixture_vite = fixture_root / "node_modules" / ".bin" / "vite"
    if not fixture_runtime_available(fixture_vite):
        bootstrap = process_runner(
            ["npm", "ci"],
            cwd=fixture_root,
            env=fixture_environment,
            check=False,
        )
        bootstrap_exit = int(bootstrap.returncode)
        if bootstrap_exit != 0:
            return bootstrap_exit
    result = process_runner(
        [
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            args.host,
            "--port",
            str(args.port if args.port is not None else 5173),
            "--strictPort",
        ],
        cwd=fixture_root,
        env=fixture_environment,
        check=False,
    )
    return int(result.returncode)
