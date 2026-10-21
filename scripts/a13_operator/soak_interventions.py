#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The C5 interventions' read-only instrument: locate, then verify.

Subcommands (ALL read-only -- nothing here changes state, ever):
  where   find every membership.state.json on both Macs and print its counters
  list    list candidate worker processes on both Macs with full command lines
  read    re-print the counters at a labelled moment (before/after an intervention)

The 2026-09-20 incident is the reason this file exists in this shape: a state-changing command was
used as a probe and took the laptop's Wi-Fi off.  Every call here is a `-get`/list/find.
"""
import os
import json
from pathlib import Path
import subprocess
import sys

OWNER = "local"
REMOTE = "member-mac"
OWNER_ROOT = os.path.expanduser("~/mycelium-wp8-02")
REMOTE_ROOT = "/Users/member/mycelium-wp8-02"


def run(cmd, host=None):
    if host:
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, cmd]
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def state_files(root, host=None):
    # remote shell is zsh: use find, never globs
    out = run(f"find {root} -name membership.state.json -maxdepth 9 2>/dev/null", host=host)
    return [line.strip() for line in out.splitlines() if line.strip().endswith(".json")]


def where():
    for label, root, host in (("OWNER (local)", OWNER_ROOT, None),
                              ("TARGET (laptop)", REMOTE_ROOT, REMOTE)):
        print(f"=== {label} ===")
        files = state_files(root, host)
        if not files:
            print("  no membership.state.json found (node not running yet?)")
            continue
        for path in files:
            if host:
                text = run(f"cat '{path}'", host=host)
            else:
                try:
                    text = Path(path).read_text()
                except OSError as exc:
                    text = f"<unreadable: {exc}>"
            try:
                doc = json.loads(text)
            except Exception:
                print(f"  {path}  <not json>")
                continue
            keys = ("node_id", "restart_count", "membership_generation",
                    "incarnation", "generation", "revision")
            flat = {k: doc.get(k) for k in keys if k in doc}
            print(f"  {path}")
            print(f"     {json.dumps(flat, sort_keys=True)}")
            # one level down, in case the counters live under a member/state key
            for k, v in doc.items():
                if isinstance(v, dict):
                    inner = {kk: v.get(kk) for kk in keys if kk in v}
                    if inner:
                        print(f"     {k}: {json.dumps(inner, sort_keys=True)}")


def list_workers():
    pat = "physical_inference_node|mycelium_a13|worker|iroh-sidecar"
    for label, host in (("OWNER (local)", None), ("TARGET (laptop)", REMOTE)):
        print(f"=== {label} ===")
        out = run(f"ps -Ao pid,ppid,etime,command | grep -E '{pat}' | grep -v grep", host=host)
        print(out.strip() or "  none")
        print()


def read(label):
    print(f"=== counters at {label} ===")
    where()


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "where"
    if what == "where":
        where()
    elif what == "list":
        list_workers()
    elif what == "read":
        read(" ".join(sys.argv[2:]) or "unlabelled")
    else:
        print(__doc__)
        raise SystemExit(2)
