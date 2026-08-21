#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Execute the A4 shutdown-negative gate against a live product serve.

SIGTERM to the serve must produce a bounded exit (<= 4,000 ms), the remote
node processes must retire, and nothing may be left half-alive. The serve PID
is required explicitly: the operator owns the lifecycle of the singleton.

The output is a bounded privacy-reduced observation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

SHUTDOWN_BOUND_MS = 4000.0
DEFAULT_SSH_HOST = "astra@100.125.181.68"
DEFAULT_SSH_KEY = "/Users/evinova-self/.ssh/id_ed25519_mycelium_linux"

REMOTE_NODE_SCAN = (
    "import os\n"
    "out=[]\n"
    "for p in os.listdir('/proc'):\n"
    "    if not p.isdigit(): continue\n"
    "    if p == str(os.getpid()): continue\n"
    "    try: cmd=open('/proc/'+p+'/cmdline','rb').read().replace(b'\\0',b' ').decode('utf-8','replace')\n"
    "    except Exception: continue\n"
    "    first=cmd.strip().split()[0] if cmd.strip() else ''\n"
    "    if first.endswith('python3') and 'physical_inference_node.py' in cmd and '--run-id' in cmd: out.append(int(p))\n"
    "print(out)\n"
)


def _ssh_prefix(args: argparse.Namespace) -> list[str]:
    return [
        "ssh",
        "-i",
        args.ssh_key,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        args.ssh_host,
    ]


def _remote_nodes(args: argparse.Namespace) -> list[int]:
    result = subprocess.run(
        _ssh_prefix(args) + ["python3 -c " + shlex.quote(REMOTE_NODE_SCAN)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout.strip() or "[]")


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    before_pids = _remote_nodes(args)
    start = time.monotonic()
    subprocess.run(
        ["kill", "-TERM", str(args.serve_pid)], capture_output=True, timeout=10
    )

    exited = False
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            os.kill(args.serve_pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            exited = True
            break
        except PermissionError:
            time.sleep(0.05)
    shutdown_ms = (time.monotonic() - start) * 1000.0 if exited else None

    time.sleep(1.0)
    # Poll remote retirement: the ssh session teardown for remote node
    # processes outlives the serve exit by seconds, not milliseconds. A
    # fixed short sleep mis-attributes lingering ssh wrappers and a still-
    # draining node as "not retired". Poll with a bounded deadline and
    # record the measured retirement latency.
    after_pids: list[int] = []
    retirement_ms = None
    if exited:
        deadline_retire = time.monotonic() + 30.0
        while time.monotonic() < deadline_retire:
            after_pids = _remote_nodes(args)
            if not after_pids:
                break
            time.sleep(0.5)
        else:
            after_pids = _remote_nodes(args)
        retirement_ms = (time.monotonic() - start) * 1000.0

    report: dict[str, Any] = {
        "protocol": "mycelium.a4_product_negative_shutdown_observation.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "passed": False,
        "simulated": False,
        "claim_boundary": "SIGTERM shutdown bounded with remote node retirement",
        "signal": "SIGTERM",
        "serve_exit_observed": exited,
        "shutdown_ms": shutdown_ms,
        "shutdown_bound_ms": SHUTDOWN_BOUND_MS,
        "remote_retirement_ms": retirement_ms,
        "remote_node_processes_before": before_pids,
        "remote_node_processes_after": after_pids,
    }
    checks = {
        "serve_exited": exited is True,
        "shutdown_bounded": (
            shutdown_ms is not None and shutdown_ms <= SHUTDOWN_BOUND_MS
        ),
        "remote_nodes_retired": bool(before_pids) and not after_pids,
        "not_simulated": report["simulated"] is False,
    }
    report["passed"] = all(checks.values())
    report["checks"] = checks
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A4 shutdown-negative gate against a live product serve."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve-pid", type=int, required=True)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    args = parser.parse_args(argv)
    report = run_gate(args)
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "passed": report["passed"],
                "checks": report["checks"],
                "shutdown_ms": report["shutdown_ms"],
                "digest": digest,
            },
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
