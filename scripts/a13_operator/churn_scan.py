#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""W5 baseline: every place a run records membership churn, and whether it can be observed IN-RUN.

The gate W5 asks for is "membership_generation and restart_count must stop advancing WITHIN a run".
Measuring that needs at least two observations inside one run's window; this scan finds out whether
such observations exist on disk at all, before a gate is written on top of them.
Read-only.
"""
import glob
import json
import os
import re
import datetime

OWNER = os.path.expanduser("~/mycelium-wp8-02/owner-lhr")
MANAGED = f"{OWNER}/local-contributor/managed"

RE_GEN = re.compile(r'"membership_generation"\s*:\s*(\d+)')
RE_INC = re.compile(r"remote-incarnation-([0-9a-f]+)((?:\.r\d+)*)")
RE_RESTART = re.compile(r'"restart_count"\s*:\s*(\d+)')
RE_UNREACH = re.compile(r"seed_http_unreachable")


def iso(p):
    return datetime.datetime.fromtimestamp(
        os.path.getmtime(p), datetime.timezone.utc).strftime("%H:%M:%SZ")


def suffix_depth(inc_suffix: str) -> int:
    return len(re.findall(r"\.r\d+", inc_suffix))


print("=" * 100)
print("1. IN-RUN OBSERVATIONS: every file in a run's managed dir that mentions a generation or incarnation")
print("=" * 100)
run_dirs = sorted(d for d in glob.glob(f"{MANAGED}/*") if os.path.isdir(d))
print(f"   managed run dirs: {len(run_dirs)}")
for run_dir in run_dirs:
    run = os.path.basename(run_dir)
    files = []
    for root, dirs, names in os.walk(run_dir):
        for n in names:
            if n.endswith((".json", ".jsonl", ".log", ".txt")):
                p = os.path.join(root, n)
                try:
                    if os.path.getsize(p) > 2_000_000:
                        continue
                    t = open(p, errors="replace").read()
                except Exception:
                    continue
                gens = RE_GEN.findall(t)
                incs = RE_INC.findall(t)
                rest = RE_RESTART.findall(t)
                if gens or incs or rest:
                    depth = max((suffix_depth(s) for _, s in incs), default=0)
                    files.append((os.path.relpath(p, run_dir), iso(p), gens[-1] if gens else "-",
                                  depth, rest[-1] if rest else "-", len(RE_UNREACH.findall(t))))
    if files:
        print(f"\n   {run}")
        for rel, mtime, gen, depth, restart, unreach in files:
            print(f"      {mtime}  {rel:<34} gen={gen:<5} incarn_depth=r{depth:<3} "
                  f"restart_count={restart:<4} seed_http_unreachable={unreach}")

print()
print("=" * 100)
print("2. DOES THE INCARNATION SUFFIX ADVANCE WITHIN A SINGLE RUN?")
print("=" * 100)
for run_dir in run_dirs:
    run = os.path.basename(run_dir)
    seen = {}
    for root, dirs, names in os.walk(run_dir):
        for n in names:
            p = os.path.join(root, n)
            if not n.endswith((".json", ".jsonl", ".log", ".txt")):
                continue
            try:
                if os.path.getsize(p) > 2_000_000:
                    continue
                t = open(p, errors="replace").read()
            except Exception:
                continue
            for _, suffix in RE_INC.findall(t):
                seen.setdefault(suffix, []).append((iso(p), os.path.relpath(p, run_dir)))
    if len(seen) > 1:
        print(f"   {run}: {len(seen)} DISTINCT incarnations in-run")
        for suffix, where in sorted(seen.items(), key=lambda kv: len(kv[0])):
            print(f"      ...{suffix or '(none)'}   first seen in {where[0][1]} at {where[0][0]}")
    elif len(seen) == 1:
        deep = list(seen)[0]
        if suffix_depth(deep) > 1:
            print(f"   {run}: ONE incarnation, but already ...{deep} "
                  f"({suffix_depth(deep)} re-incarnations) at first sight -> churn predates this run's records")

print()
print("=" * 100)
print("3. HISTORY: generation and incarnation depth, run by run (chronological)")
print("=" * 100)
rows = []
for run_dir in run_dirs:
    run = os.path.basename(run_dir)
    sp = os.path.join(run_dir, "pump/state.json")
    if not os.path.exists(sp):
        continue
    try:
        d = json.load(open(sp))
    except Exception:
        continue
    b = d.get("binding") or {}
    rows.append((run, b.get("membership_generation"), suffix_depth(b.get("member_incarnation", "")),
                 iso(sp)))
print(f"{'run_id':<30} {'generation':>10} {'incarn_depth':>13}  state.json mtime")
for run, gen, depth, mtime in sorted(rows):
    print(f"{run:<30} {str(gen):>10} {'r' + str(depth):>13}  {mtime}")
