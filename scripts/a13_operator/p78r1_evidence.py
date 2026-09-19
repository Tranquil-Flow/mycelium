#!/usr/bin/env python3
"""Collect p78r1's evidence from ARTIFACTS -- never from a console line.

Usage: p78r1_evidence.py pre|post [label]

`pre`  records what the round starts from: strays, disk, the deployed exe digests on both Macs, and
       the membership counters on the owner side.
`post` assembles the C5 claim: the smoke verdict, the soak's own counters, the restart_count readings
       on both sides, the churn gate, the network-change log, and the post-round sweep.

Every number printed here is read from a file or a `-get`/list command.  Nothing changes state: the
2026-09-20 incident (a state-changing command used as a probe took the laptop off-network) is the
reason this script is read-only by construction.
"""
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

LABEL = sys.argv[2] if len(sys.argv) > 2 else "p78r1"
RUN = Path("/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac")
PROG = Path("/Users/evinova-self/Documents/mycelium-core-program")
TREE = Path("/Users/evinova-self/Documents/playground/mycelium-core-pilot")
OWNER_ROOT = "/Users/evinova-self/mycelium-wp8-02"
DECISIONS = PROG / "status/decisions.log"


def sh(cmd, host=None, timeout=25):
    if host:
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout=8", host, cmd]
    else:
        cmd = ["bash", "-lc", cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return "<timeout>"


def counters(root, host=None):
    out = {}
    find = sh(f"find {root} -name membership.state.json -maxdepth 9 2>/dev/null", host=host)
    for path in [x for x in find.splitlines() if x.strip()]:
        text = (sh(f"cat '{path}'", host=host) if host else Path(path).read_text())
        try:
            doc = json.loads(text)
        except Exception:
            continue
        out[path] = {k: doc.get(k) for k in ("node_id", "restart_count", "membership_generation")}
    return out


def pre():
    print("=== git ===")
    print("  head:", sh(f"git -C {TREE} rev-parse --short HEAD"),
          "| dirty files:", sh(f"git -C {TREE} status --porcelain | wc -l").strip())
    print("=== disk ===")
    print("  local:", sh("df -h /System/Volumes/Data | tail -1"))
    print("  laptop:", sh("df -h /System/Volumes/Data | tail -1", host="mycelium-laptop") or "<unreachable>")
    print("=== membership counters (owner side) ===")
    for path, c in counters(OWNER_ROOT).items():
        print(f"  {path}\n     {json.dumps(c, sort_keys=True)}")
    print("=== membership counters (laptop) ===")
    for path, c in counters("/Users/evinova/mycelium-wp8-02", host="mycelium-laptop").items():
        print(f"  {path}\n     {json.dumps(c, sort_keys=True)}")


def post():
    print(f"=== SMOKE ({LABEL}) ===")
    smoke = RUN / f"round{LABEL}-smoke.json"
    if smoke.exists():
        doc = json.loads(smoke.read_text())
        print("  " + json.dumps({k: doc.get(k) for k in
                                 ("ok", "succeeded", "terminal", "terminal_code", "has_terminal")},
                                sort_keys=True))
        print("  -> soak was", "SPENT" if doc.get("succeeded") else "REFUSED (correctly)")
    else:
        print(f"  MISSING {smoke} -- the round did not reach the smoke")

    print(f"=== SOAK COUNTERS ===")
    soaks = sorted(RUN.glob(f"round{LABEL}*soak*.json")) + sorted(RUN.glob(f"*{LABEL}*soak*.json"))
    seen = set()
    for path in soaks:
        if path.name in seen:
            continue
        seen.add(path.name)
        doc = json.loads(path.read_text())
        interesting = {k: v for k, v in doc.items()
                       if any(t in k.lower() for t in ("request", "cancel", "stuck", "unproven",
                                                       "terminal", "concurrent", "sequential"))}
        print(f"  {path.name}: {json.dumps(interesting, sort_keys=True)[:600]}")
    if not soaks:
        print("  no soak artifact found (the soak did not run)")

    print(f"=== ROUTE WITHHELD TERMINALS ({LABEL}) ===")
    # ERRATA-23: the reason a terminal was withheld used to die with the app.  The route now writes one
    # bounded line per refusal; with the command-id prefix it joins against managed_commands, which is what
    # splits "the proof command was issued and went unclaimed" from "no proof command was ever minted".
    blocked = Path(os.environ.get("MYCELIUM_LIVE_BLOCKED_TERMINAL_LOG")
                   or (RUN / f"route-blocked-terminals-{LABEL}.jsonl"))
    if blocked.exists():
        rows = [json.loads(line) for line in blocked.read_text().splitlines() if line.strip()]
        print(f"  {blocked.name}: {len(rows)} withheld terminal(s)")
        for reason, count in Counter(row.get("reason") for row in rows).most_common():
            print(f"    reason={reason}  n={count}")
        print("    execution_started="
              f"{sum(1 for row in rows if row.get('execution_started'))}"
              f"  with_command_prefix={sum(1 for row in rows if row.get('command_id_prefix'))}"
              f"  cleanup_owner_bound={sum(1 for row in rows if row.get('cleanup_owner_bound'))}")
        if rows:
            print(f"    last: {json.dumps(rows[-1], sort_keys=True)[:240]}")
    else:
        print(f"  MISSING {blocked}")
        print("    -> if this round withheld terminals, the ABSENCE is an instrument finding: the env")
        print("       MYCELIUM_LIVE_BLOCKED_TERMINAL_LOG must be exported into the app's environment.")

    print(f"=== restart_count (the C5 restart evidence) ===")
    for label, root, host in (("owner", OWNER_ROOT, None), ("laptop", "/Users/evinova/mycelium-wp8-02", "mycelium-laptop")):
        got = counters(root, host=host)
        if not got:
            print(f"  {label}: <no artifact>")
        for path, c in got.items():
            print(f"  {label}: {path} -> restart_count={c.get('restart_count')} "
                  f"generation={c.get('membership_generation')} node={c.get('node_id')}")

    print("=== network change log (laptop) ===")
    print("  " + (sh("cat /tmp/net-toggle.log 2>/dev/null || echo '<no toggle log>'",
                    host="mycelium-laptop") or "<unreachable>"))

    print("=== churn gate ===")
    print(sh(f"/opt/homebrew/bin/python3 {TREE}/scripts/a13_operator/churn_gate.py "
             f"--owner-root {OWNER_ROOT} 2>&1 | tail -8"))

    print("=== post-round sweep ===")
    print("  local strays:", sh(f"/opt/homebrew/bin/python3 {TREE}/scripts/a13_operator/"
                               "round15_strays.py 2>&1 | tail -2"))
    print("=== evidence files written by this round ===")
    for path in sorted(RUN.glob(f"round{LABEL}*")):
        print(f"  {path.name}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "pre"
    (pre if what == "pre" else post)()
