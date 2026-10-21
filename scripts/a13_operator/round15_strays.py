# SPDX-License-Identifier: AGPL-3.0-only
"""Sweep BOTH Macs for round strays and report each control surface.

Why both: a daemon behind `ssh` survives the local ssh client dying. On 2026-09-16 the
laptop app (pids 12931/12932) was still running at 19:51, fifteen minutes after the chain
died at 19:36, while the owner daemon - a child of the orchestrator's chat session - was
gone (runs/wp8-02-useful-two-mac/ERRATA-1.md). A stray also blocks the next `--resume`,
because `packaged_app_daemon.py` asserts that `native-control.json` is absent at launch.

Usage: round15_strays.py [--fail-on-strays]
Exit 0 = report written. Exit 1 only with --fail-on-strays when a stray is found.
"""

import json
import subprocess
import sys
from pathlib import Path

from round14_rpc import OWNER, REMOTE, REMOTE_HOST
from round14_surface import local_state, remote_state

PATTERNS = ("packaged_app_daemon", "packaged_owner_daemon", "packaged_target_daemon", "Mycelium A13 Demo")
# The sweep must never look like a stray: its own command line carries PATTERNS, and the first
# version of this tool reported 2 phantom strays on the laptop because an `sh -c` wrapper and a
# `grep` matched their own pattern. So: plain `ps`, filter in Python, and exclude ourselves.
SELF_MARKERS = ("round15_strays", "grep -Ei", "ps -Ao")
PS_CMD = "ps -Ao pid=,args="


def _strays(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _sep, _rest = line.partition(" ")
        if not pid.isdigit():
            continue
        low = line.lower()
        if any(marker.lower() in low for marker in SELF_MARKERS):
            continue
        if not any(pattern.lower() in low for pattern in PATTERNS):
            continue
        rows.append({"pid": int(pid), "cmd": line[:200]})
    return rows


def local_sweep():
    """Returns (rows, transport_error)."""
    done = subprocess.run(["/bin/sh", "-c", PS_CMD], capture_output=True, text=True, timeout=60)
    return _strays(done.stdout), None


def remote_sweep():
    """Returns (rows, transport_error).

    Both sweeps must return the SAME shape. The first version returned a dict here and a list
    there, so `stray_count` counted the dict's two keys and every report of a clean laptop
    showed `strays=2` - a false alarm in the very evidence the round's hygiene is judged on.
    """
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", REMOTE_HOST, "/bin/sh -c " + json.dumps(PS_CMD)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return [], "ssh_timeout"
    if done.returncode:
        return [], (done.stderr or done.stdout)[-300:].strip()
    return _strays(done.stdout), None


def sweep():
    """Both Macs, both surfaces. `resume_ready` is the precondition for --resume."""
    owner_rows, owner_err = local_sweep()
    target_rows, target_err = remote_sweep()
    report: dict = {
        "owner": {"root": str(OWNER), "surface": local_state(OWNER), "strays": owner_rows,
                  "sweep_error": owner_err},
        "target": {"root": str(REMOTE) + "/target", "host": REMOTE_HOST,
                   "surface": remote_state(REMOTE, REMOTE_HOST), "strays": target_rows,
                   "sweep_error": target_err},
    }
    report["stray_count"] = len(owner_rows) + len(target_rows)
    report["sweep_errors"] = {
        label: report[label]["sweep_error"]
        for label in ("owner", "target")
        if report[label]["sweep_error"]
    }
    report["resume_ready"] = {
        label: report[label]["surface"].get("present") is False for label in ("owner", "target")
    }
    for label in ("owner", "target"):
        part = report[label]
        print(
            "%s root=%s surface_present=%s strays=%d%s"
            % (label.upper(), part["root"], part["surface"].get("present"), len(part["strays"]),
               "" if not part["sweep_error"] else " SWEEP_ERROR=" + part["sweep_error"]),
            flush=True,
        )
    return report


def main():
    report = sweep()
    print(json.dumps(report, sort_keys=True, default=str), flush=True)
    if "--fail-on-strays" in sys.argv and report["stray_count"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
