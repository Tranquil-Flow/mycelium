#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run the stream step, and fail it FAST when the route says why it withheld the terminal.

WHY THIS EXISTS (p74r1): the route printed

    live route terminal blocked request_id=084d931f-... reason=command_cleanup_receipt_missing

into the app daemon log AT THE MOMENT OF BLOCKING, while the stream step sat on a socket read for its
full timeout -- the information existed and nothing read it. This wrapper watches the daemon log while
the step runs, and aborts the step as soon as the route names its refusal, so a round costs seconds
instead of minutes and the failure signature carries the reason.

It is a HARNESS wrapper: it changes no product invariant, relaxes no check, and widens no budget. A
blocked terminal stays blocked; it is simply reported instead of waited out.

Usage:
  stream_step_watch.py --daemon-log <app daemon log> -- <step argv...>

Exit: the step's own status, or 3 when the route's refusal was seen first.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

BLOCKED = re.compile(r"live route terminal blocked request_id=(\S+) reason=(\S+)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon-log", required=True)
    ap.add_argument("--poll-seconds", type=float, default=1.0)
    ap.add_argument("--report-seconds", type=float, default=120.0,
                    help="if the step outlives this with no refusal seen, say so once and keep waiting")
    ap.add_argument("step", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    step = [a for a in args.step if a != "--"]
    if not step:
        print("stream_step_watch: no step argv", file=sys.stderr)
        return 2

    offset = 0
    if os.path.exists(args.daemon_log):
        offset = os.path.getsize(args.daemon_log)

    started = time.monotonic()
    reported_slow = False
    child = subprocess.Popen([sys.executable, "-B", *step])
    try:
        while child.poll() is None:
            time.sleep(max(0.05, args.poll_seconds))
            if os.path.exists(args.daemon_log):
                try:
                    with open(args.daemon_log, "r", errors="replace") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                        offset = handle.tell()
                except OSError:
                    chunk = ""
                found = BLOCKED.search(chunk)
                if found is not None:
                    print(
                        f"STREAM_BLOCKED_BY_ROUTE request_id={found.group(1)} "
                        f"reason={found.group(2)}",
                        flush=True,
                    )
                    print(
                        "stream_step_watch: the physical route withheld the terminal on purpose; "
                        "aborting the stream step instead of waiting out its socket timeout",
                        flush=True,
                    )
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                    return 3
            if (
                not reported_slow
                and time.monotonic() - started >= args.report_seconds
            ):
                reported_slow = True
                print(
                    f"stream_step_watch: step still running after {args.report_seconds:.0f}s "
                    "with no route refusal recorded",
                    flush=True,
                )
    except KeyboardInterrupt:
        child.terminate()
        raise
    return child.returncode if child.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
