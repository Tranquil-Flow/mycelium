#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Two questions, read-only, each with a CONTROL NEEDLE so the comparison is provably able
to detect a difference and provably able to report a match.

Q1 DIRECTION: is the demo worktree's content strictly BEHIND pilot HEAD (an ancestor state),
   or is it a divergent fork carrying work pilot does not have?
Q2 THE UNTRACKED ADAPTER: demo's `mycelium_a13_macos/` is untracked in the demo repo. Does it
   match pilot's copy module-by-module -- including the two files this session fixed
   (workflow.py, plan_binding.py is under mycelium_seed/)?
"""
import hashlib
import os
import subprocess
import sys

DEMO = os.path.expanduser("~/Documents/playground/mycelium-a13-macos-demo")
PILOT = os.path.expanduser("~/Documents/playground/mycelium-core-pilot")


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        return None
    return r.stdout.strip()


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


print("=" * 78)
print("Q1  DIRECTION: is demo content behind pilot HEAD, or a divergent fork?")
print("=" * 78)
head = git(PILOT, "rev-parse", "HEAD")
for label, rev in [("demo HEAD b9001e6", git(DEMO, "rev-parse", "HEAD")),
                   ("e72d2b0", git(PILOT, "rev-parse", "e72d2b0"))]:
    anc = subprocess.run(["git", "-C", PILOT, "merge-base", "--is-ancestor", rev, head],
                         capture_output=True)
    print(f"  {label:<22} ancestor-of-pilot-HEAD : {anc.returncode == 0}")

print()
print("  commits pilot HEAD has that e72d2b0 does not:")
log = git(PILOT, "log", "--oneline", "--no-decorate", "e72d2b0..HEAD")
lines = log.splitlines() if log else []
print(f"    count = {len(lines)}")
for l in lines[:6]:
    print("     ", l)
if len(lines) > 6:
    print(f"      ... and {len(lines) - 6} more")

print()
print("  did any of those commits touch the 8 demo-stale files?")
STALE = ["mycelium_live/route.py", "mycelium_live/supervisor.py", "mycelium_node/process.py",
         "mycelium_request_gateway/service.py", "mycelium_seed/http.py",
         "physical_inference_node.py", "physical_inference_qualification.py",
         "scripts/build_qwen_live_route.py"]
for p in STALE:
    n = git(PILOT, "rev-list", "--count", f"e72d2b0..HEAD", "--", p) or "0"
    print(f"    {n:>3} commits touch  {p}")

print()
print("=" * 78)
print("Q2  THE UNTRACKED ADAPTER vs PILOT, with control needles")
print("=" * 78)
demo_adapter = os.path.join(DEMO, "mycelium_a13_macos")
pilot_adapter = os.path.join(PILOT, "mycelium_a13_macos")
if not os.path.isdir(demo_adapter):
    print("  demo adapter dir absent -> nothing to compare")
else:
    names = sorted(f for f in os.listdir(demo_adapter) if f.endswith(".py"))
    print(f"  demo adapter modules: {len(names)}   pilot adapter modules: "
          f"{len([f for f in os.listdir(pilot_adapter) if f.endswith('.py')])}")

    same, differ, missing = [], [], []
    for f in names:
        d = os.path.join(demo_adapter, f)
        p = os.path.join(pilot_adapter, f)
        if not os.path.exists(p):
            missing.append(f)
            continue
        (same if sha(d) == sha(p) else differ).append(f)

    print()
    print(f"  IDENTICAL to pilot : {len(same)}")
    print(f"  DIFFERENT          : {len(differ)}")
    for f in differ:
        print("     DIFFERS", f)
    print(f"  ONLY IN DEMO       : {len(missing)}")
    for f in missing:
        print("     ONLY-IN-DEMO", f)

    # ---- CONTROL NEEDLES -------------------------------------------------
    print()
    print("  CONTROL NEEDLES (the comparator must be provably able to do both):")
    ctl_diff = os.path.join(DEMO, "mycelium_live/route.py")
    ctl_diff_p = os.path.join(PILOT, "mycelium_live/route.py")
    verdict = "DIFFERS" if sha(ctl_diff) != sha(ctl_diff_p) else "IDENTICAL"
    ok = (verdict == "DIFFERS")
    print(f"    positive control (a known-stale file): {verdict}  -> {'PASS' if ok else 'FAIL'}"
          f"   [mycelium_live/route.py]")
    ctl_same = os.path.join(DEMO, "mycelium_router/transports/iroh.py")
    ctl_same_p = os.path.join(PILOT, "mycelium_router/transports/iroh.py")
    verdict2 = "IDENTICAL" if sha(ctl_same) == sha(ctl_same_p) else "DIFFERS"
    ok2 = (verdict2 == "IDENTICAL")
    print(f"    negative control (a known-equal file): {verdict2}  -> {'PASS' if ok2 else 'FAIL'}"
          f"   [mycelium_router/transports/iroh.py]")
    print()
    print(f"    comparator trustworthy: {ok and ok2}")
print()
print("READ-ONLY: nothing written to either tree.")
