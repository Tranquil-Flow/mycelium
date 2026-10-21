# SPDX-License-Identifier: AGPL-3.0-only
"""Console entry point.

``--dry-run`` is the acceptance path: it runs the whole machine against mocks and
writes ``DRY-RUN.json``.  A live run requires ``--arm`` *and* ``--source-commit``
(so no physical round is ever charged to an undeclared candidate); without them
the CLI refuses and exits 3.  Loading the LaunchAgent is the owner's step.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .commands import DEFAULT_H4_RENEWAL_WATCH_DIR, PYTHON, default_recovery_commands
from .driver import RecoveryDriver
from .dryrun import run_dry_run
from .ledger import RoundLedger
from .probes import HTTPHealthProbe, ProcessProbe, SSHProbe, private_capability_headers
from .watchdog import (
    DEFAULT_CAPABILITY_PATH,
    DEFAULT_STATE_DIR,
    DemoWatchdog,
    ObserverRunner,
    WatchdogConfig,
)

REFUSE_WITHOUT_ARM = 3
DEFAULT_LEDGER_PATH = (
    DEFAULT_STATE_DIR.parent
    / "mycelium-core-program/runs/v2/takeover-20260925-1501/live-h4/ATTEMPT-LEDGER.json"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mycelium_demo_watchdog",
        description="Always-on, self-healing owner of the mycelium.now demo.",
    )
    parser.add_argument("--dry-run", action="store_true", help="exercise the machine against mocks and exit")
    parser.add_argument("--out", type=Path, default=None, help="dry-run output directory")
    parser.add_argument(
        "--arm",
        action="store_true",
        help="arm the LIVE watchdog; deploy-gated on the device being present",
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument(
        "--source-commit",
        default=None,
        help="resolved candidate commit charged by any physical round this run spends",
    )
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--capability", type=Path, default=DEFAULT_CAPABILITY_PATH)
    parser.add_argument("--pid", type=int, default=None, help="local node/seed pid to probe")
    parser.add_argument("--max-ticks", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.dry_run:
        return run_dry_run(out_dir=args.out or (args.state_dir / "dry-run"))

    if not args.arm:
        print(
            "refused_without_arm: a live run is deploy-gated on the device returning; "
            "pass --arm together with --source-commit, or use --dry-run.",
            file=sys.stderr,
        )
        return REFUSE_WITHOUT_ARM

    if not args.source_commit:
        print(
            "refused_without_source_commit: a physical round may not be charged to an "
            "undeclared candidate.",
            file=sys.stderr,
        )
        return REFUSE_WITHOUT_ARM

    return run_live(args)


def run_live(args: argparse.Namespace) -> int:
    """The armed loop.  Owner-gated; not exercised by this lane (no hardware)."""

    config = WatchdogConfig(
        origin_headers=private_capability_headers(args.capability),
        source_commit=args.source_commit,
        state_dir=args.state_dir,
        ledger_path=args.ledger,
    )
    commands = default_recovery_commands()
    driver = RecoveryDriver(
        commands,
        RoundLedger(args.ledger),
        source_commit=args.source_commit,
        spend_receipt=str(args.state_dir / "PROSPECTIVE-ROUND.json"),
        log=lambda message: print(f"[driver] {message}", flush=True),
    )
    watchdog = DemoWatchdog(
        config,
        device_probe=SSHProbe(
            config.laptop_target,
            config.laptop_key_path,
            connect_timeout=config.ssh_connect_timeout,
            total_timeout=config.ssh_total_timeout,
        ),
        process_probe=ProcessProbe(args.pid, timeout=config.probe_process_timeout_seconds),
        origin_probe=HTTPHealthProbe(config.origin_url, config.origin_headers, timeout=5.0),
        driver=driver,
        observer=ObserverRunner(
            (PYTHON, "-B", str(DEFAULT_H4_RENEWAL_WATCH_DIR / "observer.py"), "--observe"),
            timeout=config.observer_timeout_seconds,
        ),
        log=lambda message: print(f"[watchdog] {message}", flush=True),
    )
    watchdog.run(max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
