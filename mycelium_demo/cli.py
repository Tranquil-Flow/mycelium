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
    device_lab.add_argument(
        "--static-root",
        type=Path,
        help="product operator SPA root; use with --worker-static-root for split rich UI",
    )
    device_lab.add_argument(
        "--worker-static-root",
        type=Path,
        help="browser-worker static root mounted at /device",
    )

    serve = commands.add_parser(
        "serve",
        help="launch bundled fixture UI or a qualified physical inference route",
    )
    serve.add_argument(
        "--mode",
        choices=("fixture", "live"),
        required=True,
        help=(
            "fixture serves bundled synthetic product data; live serves only a "
            "qualified physical route"
        ),
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_serve_port)
    serve.add_argument("--state-root", type=Path)
    serve.add_argument("--operator-token-file", type=Path)
    serve.add_argument("--public-origin")
    serve.add_argument("--static-root", type=Path)
    serve.add_argument("--worker-static-root", type=Path)
    serve.add_argument("--tls-cert", type=Path)
    serve.add_argument("--tls-key", type=Path)
    serve.add_argument("--operator-plan", type=Path, action="append")
    serve.add_argument("--deployment-dir", type=Path)
    serve.add_argument("--model-operation-file", type=Path)
    serve.add_argument("--registry-state", type=Path)
    serve.add_argument("--seed-state-root", type=Path)
    serve.add_argument("--seed-url")
    return parser


def _live_arguments(args: argparse.Namespace) -> list[str]:
    result = [
        "--host",
        args.host,
        "--port",
        str(args.port if args.port is not None else 8787),
    ]
    for operator_plan in args.operator_plan or ():
        result.extend(("--operator-plan", str(operator_plan)))
    optional_paths = (
        ("--deployment-dir", args.deployment_dir),
        ("--model-operation-file", args.model_operation_file),
        ("--static-root", args.static_root),
        ("--registry-state", args.registry_state),
        ("--seed-state-root", args.seed_state_root),
    )
    for flag, value in optional_paths:
        if value is not None:
            result.extend((flag, str(value)))
    if args.seed_url is not None:
        result.extend(("--seed-url", args.seed_url))
    return result


def _fixture_has_live_only_arguments(args: argparse.Namespace) -> bool:
    return any(
        value is not None
        for value in (
            args.state_root,
            args.operator_token_file,
            args.public_origin,
            args.static_root,
            args.worker_static_root,
            args.tls_cert,
            args.tls_key,
            args.operator_plan,
            args.seed_state_root,
            args.deployment_dir,
            args.model_operation_file,
            args.registry_state,
            args.seed_url,
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

        live_arguments = [
            "--host",
            args.bind_host,
            "--port",
            str(bundle.port),
            "--state-root",
            str(bundle.runtime_root),
        ]
        for flag, value in (
            ("--static-root", args.static_root),
            ("--worker-static-root", args.worker_static_root),
        ):
            if value is not None:
                live_arguments.extend((flag, str(value)))
        live_arguments.extend(
            [
                "--public-origin",
                bundle.public_origin,
                "--tls-cert",
                str(bundle.tls_cert),
                "--tls-key",
                str(bundle.tls_key),
            ]
        )
        return int(live_server_main(live_arguments))

    if args.action != "serve":
        raise AssertionError(f"unsupported action: {args.action}")
    if args.mode == "live":
        if args.operator_plan is None:
            parser.error("live mode requires --operator-plan")
        if args.seed_state_root is None:
            parser.error("live mode requires --seed-state-root")
        if any(
            value is not None
            for value in (
                args.state_root,
                args.operator_token_file,
                args.public_origin,
                args.worker_static_root,
                args.tls_cert,
                args.tls_key,
            )
        ):
            parser.error("physical live mode is loopback-only and rejects interactive-runtime options")
        if live_server_main is None:
            from mycelium_live.supervisor import main as live_server_main

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
