#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Did A9's lane content EVER exist in core's history for the same path?

For each of the 209 paths the A9 lane changes (base abe291c -> tip ba6abbe),
compare the lane's blob for that path against every blob core ever had for that
same path. This is the "stale" test from the marker's method: a path whose A9
content core once had and has since replaced is still unintegrated NOW, but the
distinction matters for an honest verdict.

READ-ONLY: --no-optional-locks on every git call.
"""
from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path

PILOT = Path(os.path.expanduser("~/Documents/playground/mycelium-core-pilot"))
OUT = Path(os.path.expanduser("~/Documents/mycelium-core-program/runs/a9-adjudication/evidence-2026-09-21"))
CORE = "integration/core-pilot"
TIP, BASE = "ba6abbe", "abe291c"


def git(*args: str) -> str:
    r = subprocess.run(["git", "--no-optional-locks", "--no-pager", *args],
                       cwd=str(PILOT), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r.stdout


paths = [l.split("\t", 1)[1] for l in
         git("diff", "--name-status", "--no-renames", BASE, TIP).splitlines()]
tip_blobs = {p: git("rev-parse", f"{TIP}:{p}") for p in paths}

identical, stale, divergent, absent = [], [], [], []
core_commits_for_path: dict[str, int] = {}
for p in paths:
    hb = subprocess.run(["git", "--no-optional-locks", "rev-parse", f"{CORE}:{p}"],
                        cwd=str(PILOT), capture_output=True, text=True)
    head_blob = hb.stdout.strip() if hb.returncode == 0 else None
    if head_blob == tip_blobs[p]:
        identical.append(p)
        continue
    # every core commit that touched this path
    revs = git("rev-list", CORE, "--", p).split()
    core_commits_for_path[p] = len(revs)
    hist = set()
    for c in revs[:400]:
        b = subprocess.run(["git", "--no-optional-locks", "rev-parse", f"{c}:{p}"],
                           cwd=str(PILOT), capture_output=True, text=True)
        if b.returncode == 0:
            hist.add(b.stdout.strip())
    if tip_blobs[p] in hist:
        stale.append(p)
    elif head_blob is None:
        absent.append(p)
    else:
        divergent.append(p)

res = {
    "paths_total": len(paths),
    "identical_at_core_head": len(identical),
    "core_once_had_this_blob_then_replaced": len(stale),
    "present_at_head_but_never_this_blob": len(divergent),
    "absent_at_core_head_and_never_in_history": len(absent),
    "stale_paths": stale,
    "divergent_paths": divergent[:60],
    "core_commits_touching_lane_paths_max": max(core_commits_for_path.values()) if core_commits_for_path else 0,
}
(OUT / "blob-history-2026-09-21.json").write_text(json.dumps(res, indent=1))
for k in list(res)[:6]:
    print(f"{k} = {res[k]}")
print("stale_paths =", stale)
