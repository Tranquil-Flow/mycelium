#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fixture test for stream_step_watch.py -- the wrapper must both fire and NOT fire.

Both directions matter: a wrapper that always aborts would look helpful and would wreck every future
round, and one that never fires is the blindness it exists to fix.

Run: env -u PYTHONPATH /opt/homebrew/bin/python3 -B scripts/a13_operator/test_stream_step_watch.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WATCH = os.path.join(HERE, "stream_step_watch.py")

FAILS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS += 1


def write(path: str, body: str) -> str:
    with open(path, "w") as handle:
        handle.write(body)
    return path


def run_watch(daemon_log: str, step: str, extra=()):
    return subprocess.run(
        [sys.executable, "-B", WATCH, "--daemon-log", daemon_log,
         "--poll-seconds", "0.1", *extra, "--", step],
        capture_output=True, text=True, timeout=60,
    )


print("A) the route names its refusal while the step is still waiting -> fast abort")
with tempfile.TemporaryDirectory() as td:
    log = write(os.path.join(td, "daemon.log"), "launcher_started\n")
    sleeper = write(os.path.join(td, "sleeper.py"), "import time\ntime.sleep(30)\n")

    def append():
        time.sleep(1.0)
        with open(log, "a") as handle:
            handle.write(
                "live route terminal blocked request_id=084d931f-9d1b-4c2e-8f0a-0123456789ab "
                "reason=command_cleanup_receipt_missing\n"
            )

    threading.Thread(target=append, daemon=True).start()
    started = time.monotonic()
    result = run_watch(log, sleeper)
    elapsed = time.monotonic() - started
    check("exit 3", result.returncode == 3, f"got {result.returncode}")
    check("reason reported", "STREAM_BLOCKED_BY_ROUTE" in result.stdout
          and "command_cleanup_receipt_missing" in result.stdout)
    check("aborted fast, not after the child's 30 s", elapsed < 15,
          f"{elapsed:.1f}s")

print()
print("B) a refusal that predates the wrapper must NOT abort a healthy step (offset semantics)")
with tempfile.TemporaryDirectory() as td:
    log = write(os.path.join(td, "daemon.log"),
                "live route terminal blocked request_id=old reason=old_reason\n")
    quick = write(os.path.join(td, "quick.py"), "print('ok')\n")
    result = run_watch(log, quick)
    check("exit 0 despite a stale refusal in the log", result.returncode == 0,
          f"got {result.returncode}")
    check("no false STREAM_BLOCKED_BY_ROUTE", "STREAM_BLOCKED_BY_ROUTE" not in result.stdout)

print()
print("C) the step's own status is preserved when no refusal appears")
with tempfile.TemporaryDirectory() as td:
    log = write(os.path.join(td, "daemon.log"), "nothing\n")
    ok = write(os.path.join(td, "ok.py"), "raise SystemExit(0)\n")
    bad = write(os.path.join(td, "bad.py"), "raise SystemExit(2)\n")
    r_ok = run_watch(log, ok)
    r_bad = run_watch(log, bad)
    check("a passing step stays 0", r_ok.returncode == 0, f"got {r_ok.returncode}")
    check("a failing step stays 2 (so STEP_FAILED still fires)", r_bad.returncode == 2,
          f"got {r_bad.returncode}")

print()
print("D) a missing daemon log is not fatal (the wrapper still runs the step)")
with tempfile.TemporaryDirectory() as td:
    ok = write(os.path.join(td, "ok.py"), "raise SystemExit(0)\n")
    result = run_watch(os.path.join(td, "absent.log"), ok)
    check("exit 0 with no daemon log", result.returncode == 0, f"got {result.returncode}")

print()
print(f"FAILURES: {FAILS}")
sys.exit(1 if FAILS else 0)
