from __future__ import annotations

import argparse
import shutil
from collections.abc import Sequence
from pathlib import Path

from .doctor import DEFAULT_COMMANDS, canonical_json, local_tcp_port_available, run_preflight

ROOT = Path(__file__).resolve().parents[1]


def _port_value(value: str) -> int | str:
    if value.isascii() and value.isdecimal():
        significant = value.lstrip("0") or "0"
        if len(significant) <= 5:
            return int(significant, 10)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3.14 -m mycelium_demo",
        description=(
            "Mycelium local environment preflight "
            "(read-only; not release readiness or route qualification)."
        ),
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
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    which=shutil.which,
    port_available=local_tcp_port_available,
) -> int:
    args = build_parser().parse_args(argv)
    if args.action != "doctor":
        raise AssertionError(f"unsupported action: {args.action}")
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
