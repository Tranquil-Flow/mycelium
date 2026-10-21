#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Did the live boot ids reach the artifact the plan was compiled from?

If operator-plan.json / build-report.json carry the two boot ids measured in observations.json,
then the boot-id fix is not merely "running" -- its data flowed end to end for this round.
Read-only.
"""
import glob
import json
import os

OWNER = os.path.expanduser("~/mycelium-wp8-02/owner-lhr")
RUN = "a13-qwen-20260919T120607Z"
BOOT_OWNER = "boot-3fb2b21ca45a18c169661a2a6ad016b1"
BOOT_TARGET = "boot-db09cba0d4af694793f97234f7e551f2"
HOST_OWNER = "host-7fa49fb6b98c5360d25ad92d93aece66"
HOST_TARGET = "host-a0d776f04e286957bc33d58b6f721472"

CANDS = [
    f"{OWNER}/chain-runs/{RUN}/compiled/operator-plan.json",
    f"{OWNER}/chain-runs/{RUN}/compiled/build-report.json",
    f"{OWNER}/chain-runs/{RUN}/compiled/local-challenge-checkpoint.json",
    f"{OWNER}/chain-runs/{RUN}/topology.json",
    f"{OWNER}/chain-runs/{RUN}/template.json",
    f"{OWNER}/rpc-round74/stream-{RUN}/startup.json",
    f"{OWNER}/rpc-round74/stream-{RUN}/qualification.json",
    f"{OWNER}/rpc-round74/stream-{RUN}/runtime-after.json",
    f"{OWNER}/rpc-round74/stream-{RUN}/deployments-after.json",
]

for p in CANDS:
    if not os.path.exists(p):
        print(f"ABSENT  {p}")
        continue
    text = open(p, errors="replace").read()
    print(f"\n{'=' * 74}")
    print(f"{os.path.basename(p)}   {len(text)} bytes")
    for label, needle in [("boot_id key", '"boot_id"'),
                          ("boot_id_bindings key", "boot_id_bindings"),
                          ("OWNER boot id value", BOOT_OWNER),
                          ("TARGET boot id value", BOOT_TARGET),
                          ("OWNER host id", HOST_OWNER),
                          ("TARGET host id", HOST_TARGET),
                          ("observations ref", "observations")]:
        n = text.count(needle)
        mark = "" if n == 0 else "   <-- present"
        print(f"   {label:<22} occurrences: {n}{mark}")

print()
print("=" * 74)
print("RAW boot-related excerpts from operator-plan.json")
print("=" * 74)
p = f"{OWNER}/chain-runs/{RUN}/compiled/operator-plan.json"
try:
    d = json.load(open(p))

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if "boot" in k.lower() or "host_id" in k.lower():
                    s = json.dumps(v)
                    print(f"   {path}/{k} = {s[:160]}")
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(d)
except Exception as e:
    print("   parse failed:", e)
    print(open(p, errors="replace").read()[:1200])
