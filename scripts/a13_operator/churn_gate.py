#!/usr/bin/env python3
"""W5 churn gate -- the membership/reconnect invariant, checked offline.

THE INVARIANT (from the wp8-02 RCA, made checkable): a round must not leave the fleet churning. The
owner-side records show one observation per round, and across all 23 rounds on disk the values are exact:

    membership_generation == the round's ordinal
    incarnation depth     == generation - 1     (i.e. ONE re-incarnation per round, no more)

So the gate asserts precisely that, and flags any round that deviates. A deviation is the churn the gate
exists to catch.

HONEST LIMITS, stated because they bound what this gate can claim:
  * The owner-side pump state is written once per round (at bind time), so it CANNOT show advancement
    *within* a run. Proving "no advancement inside the run" needs a second in-round observation.
  * `restart_count` and `seed_http_unreachable` appear NOWHERE in the owner tree -- they belong to the
    target (laptop) side. This gate reports their absence rather than silently implying they were checked.
    A round must capture the target's last-action-error for the gate to be complete.

Exit: 0 the invariant holds on every round · 1 at least one deviation (named) · 7 no data.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

RE_SUFFIX = re.compile(r"\.r\d+")
DEFAULT_OWNER = "/Users/evinova-self/mycelium-wp8-02/owner-lhr"


def depth(incarnation: str) -> int:
    return len(RE_SUFFIX.findall(incarnation or ""))


def load(owner: str) -> list[tuple[str, int, int]]:
    rows = []
    for state in sorted(glob.glob(f"{owner}/local-contributor/managed/*/pump/state.json")):
        run = os.path.basename(os.path.dirname(os.path.dirname(state)))
        try:
            binding = (json.load(open(state)) or {}).get("binding") or {}
        except Exception:
            continue
        gen = binding.get("membership_generation")
        if not isinstance(gen, int):
            continue
        rows.append((run, gen, depth(binding.get("member_incarnation", ""))))
    rows.sort()  # run ids are timestamps: lexicographic == chronological
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner-root", default=DEFAULT_OWNER)
    a = ap.parse_args()

    rows = load(a.owner_root)
    if not rows:
        print("churn_gate: NO_DATA -- no owner-side pump state found")
        return 7

    print(f"churn_gate -- {len(rows)} rounds, owner root {a.owner_root}")
    print(f"{'run_id':<30} {'gen':>5} {'depth':>6}  {'ok':<6} note")
    print("-" * 92)

    deviations = []
    for index, (run, gen, dep) in enumerate(rows, start=1):
        notes = []
        if index > 1:
            prev_gen = rows[index - 2][1]
            if gen != prev_gen + 1:
                notes.append(f"generation moved by {gen - prev_gen} (expected +1)")
        if dep != gen - 1:
            notes.append(f"depth r{dep} != generation-1 r{gen - 1}")
        ok = not notes
        if not ok:
            deviations.append((run, notes))
        print(f"{run:<30} {gen:>5} {('r' + str(dep)):>6}  {'ok' if ok else 'FAIL':<6} "
              f"{'; '.join(notes)}")

    print("-" * 92)
    print(f"  generation range : {rows[0][1]} .. {rows[-1][1]}")
    print(f"  depth range      : r{rows[0][2]} .. r{rows[-1][2]}")
    print()

    # The target-side signals, reported as ABSENT rather than assumed checked.
    restart_hits, unreach_hits = 0, 0
    for root, dirs, files in os.walk(f"{a.owner_root}/local-contributor/managed"):
        for name in files:
            if not name.endswith((".json", ".jsonl", ".log")):
                continue
            p = os.path.join(root, name)
            try:
                if os.path.getsize(p) > 2_000_000:
                    continue
                text = open(p, errors="replace").read()
            except Exception:
                continue
            restart_hits += len(re.findall(r'"restart_count"', text))
            unreach_hits += len(re.findall(r"seed_http_unreachable", text))
    print(f"  restart_count sightings in the owner tree      : {restart_hits}")
    print(f"  seed_http_unreachable sightings                : {unreach_hits}")
    if restart_hits == 0 and unreach_hits == 0:
        print("  -> both live on the TARGET (laptop) side. This gate cannot see them: a round must")
        print("     capture the target's last-action-error, or the churn claim stays half-measured.")

    print()
    if deviations:
        print(f"CHURN_GATE FAIL -- {len(deviations)} round(s) deviate")
        for run, notes in deviations:
            print(f"   {run}: {'; '.join(notes)}")
        return 1
    print("CHURN_GATE PASS -- generation advances by exactly one per round and depth == generation-1 "
          "on every round on disk")
    print("NOTE: owner-side only. Within-run advancement is not observable here, and the target-side")
    print("      signals above were not found locally, so they were NOT checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
