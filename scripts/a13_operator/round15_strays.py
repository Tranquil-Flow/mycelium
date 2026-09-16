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
LOCAL_PS = "ps -Ao pid=,comm=,args= | grep -Ei '%s' | grep -v grep || true" % "|".join(PATTERNS)


def _strays(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pid = line.split(None, 1)[0]
        if pid.isdigit():
            rows.append({"pid": int(pid), "cmd": line[:200]})
    return rows


def local_sweep():
    done = subprocess.run(["/bin/sh", "-c", LOCAL_PS], capture_output=True, text=True, timeout=60)
    return _strays(done.stdout)


def remote_sweep():
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", REMOTE_HOST, "/bin/sh -c " + json.dumps(LOCAL_PS)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"error": "ssh_timeout", "strays": []}
    if done.returncode:
        return {"error": (done.stderr or done.stdout)[-300:].strip(), "strays": []}
    return {"error": None, "strays": _strays(done.stdout)}


def sweep():
    """Both Macs, both surfaces. `resume_ready` is the precondition for --resume."""
    report = {
        "owner": {"root": str(OWNER), "surface": local_state(OWNER), "strays": local_sweep()},
        "target": {
            "root": str(REMOTE) + "/target",
            "host": REMOTE_HOST,
            "surface": remote_state(REMOTE, REMOTE_HOST),
            "strays": remote_sweep(),
        },
    }
    report["stray_count"] = len(report["owner"]["strays"]) + len(report["target"]["strays"])
    report["resume_ready"] = {
        label: report[label]["surface"].get("present") is False for label in ("owner", "target")
    }
    for label in ("owner", "target"):
        part = report[label]
        print(
            "%s root=%s surface_present=%s strays=%d"
            % (label.upper(), part["root"], part["surface"].get("present"), len(part["strays"])),
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
