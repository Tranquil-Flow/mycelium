#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Establish the p74r1 timeline around the boot-id collection and the worker failure.

Question: observations.json for this run holds BOTH nodes' boot_id -- so did the collection
actually succeed? If it did, my recorded E verdict ("the producer refused, nothing was
collected") is wrong and needs an ERRATA. If it succeeded EARLY and a LATER refresh timed out
after the worker died, the verdict stands but for a different mechanism.

Read-only.
"""
import datetime
import glob
import json
import os
import re

OWNER = os.path.expanduser("~/mycelium-wp8-02/owner-lhr")
RUN = "a13-qwen-20260919T120607Z"
LOGS = os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac")


def iso(ms_or_s):
    v = float(ms_or_s)
    if v > 1e11:  # milliseconds
        v /= 1000.0
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ")


def iso_mtime(path):
    return datetime.datetime.fromtimestamp(
        os.path.getmtime(path), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


print("=" * 78)
print("1. THE OBSERVATIONS ARTIFACT")
print("=" * 78)
obs = f"{OWNER}/chain-runs/{RUN}/observations.json"
d = json.load(open(obs))
print(f"  path        : chain-runs/{RUN}/observations.json")
print(f"  mtime       : {iso_mtime(obs)}")
print(f"  captured_at : {iso(d['captured_at_unix_ms'])}   (raw {d['captured_at_unix_ms']})")
print(f"  protocol    : {d['protocol']}")
print(f"  nodes       : {len(d['observations'])}")
for o in d["observations"]:
    print(f"     {o['node_id']}  boot_id={o['boot_id']}  host_id={o['host_id']}")

# is there an OLDER observations file anywhere for this run (an earlier attempt)?
print()
print("  any other observations.json under the owner root for this run?")
hits = glob.glob(f"{OWNER}/**/observations*.json", recursive=False)
for h in glob.glob(f"{OWNER}/chain-runs/*/observations.json"):
    print(f"     {h}  ({iso_mtime(h)})")
found_deep = []
for root, dirs, files in os.walk(f"{OWNER}/local-contributor/managed/{RUN}"):
    for f in files:
        if "observation" in f or "hello" in f:
            found_deep.append(os.path.join(root, f))
print(f"  inside the managed dir: {found_deep if found_deep else 'none'}")

print()
print("=" * 78)
print("2. WHAT THE APP SAID, WITH TIMESTAMPS")
print("=" * 78)
pat = re.compile(r"boot_id|ManagedObservations|observation_unavailable|refreshed",
                 re.IGNORECASE)
for lg in sorted(glob.glob(f"{LOGS}/roundp74r1-*.log")):
    lines = open(lg, errors="replace").read().splitlines()
    hits = [l for l in lines if pat.search(l)]
    print(f"\n  {os.path.basename(lg)}   ({len(lines)} lines, mtime {iso_mtime(lg)})")
    for l in hits[:12]:
        print(f"     {l.strip()[:200]}")
    if not hits:
        print("     (no boot-id lines)")

print()
print("=" * 78)
print("3. THE FAILURE / EXIT RECORDS")
print("=" * 78)
md = f"{OWNER}/local-contributor/managed/{RUN}"
for f in ["native-failure.private.json", "native-exit.private.json", "pump/state.json",
          "pump/concurrent.json"]:
    p = os.path.join(md, f)
    print(f"  {iso_mtime(p)}  {f}")
print()
print("  pump/state.json:")
try:
    st = json.load(open(os.path.join(md, "pump/state.json")))
    print(json.dumps(st, indent=4)[:1500])
except Exception as e:
    print("   could not parse:", e)

print()
print("=" * 78)
print("4. STREAM EVIDENCE TIMES")
print("=" * 78)
sdir = glob.glob(f"{OWNER}/rpc-round74/stream-{RUN}")
for sd in sdir:
    for f in sorted(os.listdir(sd)):
        print(f"  {iso_mtime(os.path.join(sd, f))}  {f}")
