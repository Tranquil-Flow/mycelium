#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Enumerate EVERY run's boot-id observations across the owner tree.

This is the baseline the guard v2 must judge against: which runs collected observations, how many
nodes each, and whether the compiled plan bound them. Gives real fixtures instead of expectations.
Read-only.
"""
import glob
import json
import os

OWNER = os.path.expanduser("~/mycelium-wp8-02/owner-lhr")

rows = []
for obs_path in sorted(glob.glob(f"{OWNER}/chain-runs/*/observations.json")):
    run = os.path.basename(os.path.dirname(obs_path))
    try:
        d = json.load(open(obs_path))
        nodes = len(d.get("observations", []))
        proto = d.get("protocol", "?")
        boot_ids = [o.get("boot_id") for o in d.get("observations", [])]
    except Exception as e:
        rows.append((run, "PARSE_FAIL", 0, str(e), []))
        continue

    plan_path = f"{OWNER}/chain-runs/{run}/compiled/operator-plan.json"
    bound = 0
    plan_missing = not os.path.exists(plan_path)
    if not plan_missing:
        try:
            plan_text = open(plan_path, errors="replace").read()
            bound = sum(1 for b in boot_ids if b and b in plan_text)
        except Exception:
            bound = -1

    rows.append((run, proto, nodes, ("plan_missing" if plan_missing else "ok"), bound))

print(f"{'run_id':<30} {'nodes':>5} {'boot_ids_in_plan':>16}  protocol / note")
print("-" * 96)
for run, proto, nodes, note, bound in rows:
    print(f"{run:<30} {nodes:>5} {str(bound):>16}  {proto} {'' if note == 'ok' else '[' + note + ']'}")

print()
print(f"total runs with an observations.json : {len(rows)}")
n_ge1 = sum(1 for r in rows if isinstance(r[2], int) and r[2] >= 1)
print(f"runs with nodes >= 1                  : {n_ge1}")
n_bound = sum(1 for r in rows if isinstance(r[4], int) and r[4] >= 1)
print(f"runs whose plan carries a boot id      : {n_bound}")
print()
print("runs with nodes >= 1 AND plan bound:")
for run, proto, nodes, note, bound in rows:
    if isinstance(nodes, int) and nodes >= 1 and isinstance(bound, int) and bound >= 1:
        print(f"   {run}   nodes={nodes} bound={bound}")

print()
print("=" * 96)
print("soak04 specifically: which run id, and what does it have?")
print("=" * 96)
import subprocess
lg = os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac/roundsoak04-attempt.log")
if os.path.exists(lg):
    txt = open(lg, errors="replace").read()
    import re
    runs = sorted(set(re.findall(r"a13-qwen-\d{8}T\d{6}Z", txt)))
    print("run ids mentioned in the soak04 attempt log:", runs)
    for r in runs:
        print(f"   chain-runs/{r}/observations.json exists: "
              f"{os.path.exists(f'{OWNER}/chain-runs/{r}/observations.json')}")
else:
    print("attempt log not found at", lg)
    print("candidates:")
    for p in glob.glob(os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac/*soak04*")):
        print("   ", os.path.basename(p))
