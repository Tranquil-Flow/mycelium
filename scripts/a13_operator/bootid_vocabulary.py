#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The app's actual boot-id vocabulary, across every round log, in full (no truncation).

Decisive question: does `boot_id_observations_collected nodes=N` EVER appear? The guard v1 keys on
that string; v2 must not repeat a wrong assumption.
Read-only.
"""
import glob
import os
import re

LOGS = os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac")
PATTERNS = {
    "collected": r"boot_id_observations_collected",
    "unavailable": r"boot_id_observation_unavailable",
    "refreshed": r"boot_id_refreshed",
    "OBSERVED": r"OBSERVED \{",
    "UnboundLocalError": r"UnboundLocalError",
    "managed_control_response_timeout": r"managed_control_response_timeout",
}

print("=" * 100)
print("1. ROUND LOGS: which boot-id vocabulary appears, per log")
print("=" * 100)
logs = sorted(glob.glob(f"{LOGS}/round*.log"))
print(f"{'log':<44} " + " ".join(f"{k[:11]:>12}" for k in PATTERNS))
for lg in logs:
    txt = open(lg, errors="replace").read()
    counts = {k: len(re.findall(p, txt)) for k, p in PATTERNS.items()}
    if not any(counts.values()):
        continue
    name = os.path.basename(lg)
    print(f"{name:<44} " + " ".join(f"{counts[k]:>12}" for k in PATTERNS))

print()
print("=" * 100)
print("2. ANY log anywhere in the program dir with `boot_id_observations_collected`")
print("=" * 100)
hits = []
for root, dirs, files in os.walk(LOGS):
    for f in files:
        if not f.endswith((".log", ".json", ".txt", ".md")):
            continue
        p = os.path.join(root, f)
        try:
            t = open(p, errors="replace").read()
        except Exception:
            continue
        if "boot_id_observations_collected" in t:
            hits.append(p)
for h in hits:
    print("   ", h.replace(LOGS + "/", ""))
print(f"   total: {len(hits)}")

print()
print("=" * 100)
print("3. p74r1: every boot-id line IN FULL")
print("=" * 100)
for f in ["roundp74r1-owner-daemon.log", "roundp74r1-attempt.log",
          "roundp74r1-supervisor.log", "roundp74r1-target-daemon.log"]:
    p = os.path.join(LOGS, f)
    print(f"\n----- {f} -----")
    if not os.path.exists(p):
        print("   (absent)")
        continue
    for i, line in enumerate(open(p, errors="replace").read().splitlines(), 1):
        if re.search(r"boot_id|OBSERVED|managed_control_response_timeout|UnboundLocalError", line):
            print(f"   {i:>4}| {line}")

print()
print("=" * 100)
print("4. THE APP SOURCE: what it prints, and where the plan's boot ids come from")
print("=" * 100)
PILOT = os.path.expanduser("~/Documents/playground/mycelium-core-pilot")
for rel, pat in [("mycelium_a13_macos/workflow.py", r"boot_id_observations_collected|boot_id_observation_unavailable|boot_id_refreshed"),
                 ("mycelium_seed/plan_binding.py", r"boot_id"),
                 ("mycelium_a13_macos/managed_observations.py", r"def collect_managed_hello_observations|managed_control_response_timeout|raise ManagedObservationsError")]:
    p = os.path.join(PILOT, rel)
    print(f"\n----- {rel} -----")
    if not os.path.exists(p):
        print("   (absent)")
        continue
    for i, line in enumerate(open(p, errors="replace").read().splitlines(), 1):
        if re.search(pat, line):
            print(f"   {i:>5}| {line.strip()[:160]}")
