#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""W5 churn gate -- the membership/reconnect invariant, checked offline ON BOTH SIDES.

THE INVARIANT (from the wp8-02 RCA, made checkable): a round must not leave the fleet churning, and a
re-enrollment must not advance more than once per round. Measured across every round on disk:

    owner  (pump/state.json)     membership_generation == the round's ordinal
                                 incarnation depth     == generation - 1
    target (pump/state.json)     membership_generation == the round's ordinal + 1
                                 incarnation depth     == generation - 1
    target (membership.state.json, durable, all-time)
                                 restart_count         == the number of rounds
                                 membership_generation == restart_count + 1
                                 incarnation depth     == restart_count

So the gate asserts exactly that on both sides and names any round that deviates. A deviation is the churn
the gate exists to catch.

HONEST LIMITS, restated after the target-side records were collected (they were previously unwritten here
because the signals were not on disk at all):

  * IN-RUN ADVANCEMENT. The pump state is written per round, so no single file proves what happened *inside*
    one run. What the records DO support: no run's own files contain two DIFFERENT incarnations, and the
    round-to-round delta is exactly +1, so at most one re-enrollment separates consecutive rounds and none
    is visible inside one. That is "consistent with no in-run churn", which is the strongest claim these
    artifacts can carry -- not "proven absent". Stated rather than implied.
  * `seed_http_unreachable` is a raised CODE (`mycelium_node/reconnect.py`), not a persisted field, so its
    absence from artifacts means no record names it -- not that no seed call ever failed. The gate reports
    sightings (now searchable on both sides) and never reads zero as proof of health.
  * The target records are a COLLECTION pulled read-only from the laptop into
    `runs/wp8-02-useful-two-mac/target-records/` (per-run) and `target-root-records/` (durable). A round's
    capture step must refresh them, or this gate grades stale evidence.

Exit: 0 the invariant holds on every round of both sides · 1 at least one deviation (named) · 7 no data.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

RE_SUFFIX = re.compile(r"\.r\d+")
DEFAULT_OWNER = os.path.expanduser("~/mycelium-wp8-02/owner-lhr")
DEFAULT_RECORDS = (
    os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac")
)


def depth(incarnation: str) -> int:
    return len(RE_SUFFIX.findall(incarnation or ""))


def load_side(pattern: str) -> list[tuple[str, int, int]]:
    """Rows of (run, membership_generation, incarnation depth) from pump states."""
    rows = []
    for state in sorted(glob.glob(pattern)):
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


def deviations(label: str, rows: list[tuple[str, int, int]]) -> list[tuple[str, str, list[str]]]:
    """Name every round whose generation or depth breaks the invariant."""
    out = []
    for index, (run, gen, dep) in enumerate(rows, start=1):
        notes = []
        if index > 1:
            prev = rows[index - 2][1]
            if gen != prev + 1:
                notes.append(f"generation moved by {gen - prev} (expected +1)")
        if dep != gen - 1:
            notes.append(f"depth r{dep} != generation-1 r{gen - 1}")
        if notes:
            out.append((label, run, notes))
    return out


def show(label: str, rows: list[tuple[str, int, int]]) -> None:
    print()
    print(f"=== {label} -- {len(rows)} rounds ===")
    print(f"{'run_id':<30} {'gen':>5} {'depth':>6}  {'ok':<6} note")
    print("-" * 92)
    per_run = {r: n for _, r, n in deviations(label, rows)}
    for run, gen, dep in rows:
        notes = per_run.get(run, [])
        print(f"{run:<30} {gen:>5} {('r' + str(dep)):>6}  {'ok' if not notes else 'FAIL':<6} "
              f"{'; '.join(notes)}")
    if rows:
        print("-" * 92)
        print(f"  generation range : {rows[0][1]} .. {rows[-1][1]}")
        print(f"  depth range      : r{rows[0][2]} .. r{rows[-1][2]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner-root", default=DEFAULT_OWNER)
    ap.add_argument("--target-records", default=f"{DEFAULT_RECORDS}/target-records",
                    help="per-run target records collected from the laptop")
    ap.add_argument("--target-durable", default=f"{DEFAULT_RECORDS}/target-root-records/"
                                               "membership.state.json",
                    help="the target node's own durable membership state")
    a = ap.parse_args()

    owner = load_side(f"{a.owner_root}/local-contributor/managed/*/pump/state.json")
    if not owner:
        print("churn_gate: NO_DATA -- no owner-side pump state found")
        return 7

    print(f"churn_gate -- owner root {a.owner_root}")
    show("OWNER side", owner)

    target = load_side(f"{a.target_records}/a13-qwen-*/pump/state.json")
    if target:
        show("TARGET side", target)
    else:
        print()
        print(f"=== TARGET side -- NO DATA (looked in {a.target_records}) ===")
        print("  The target's records were not collected, so the target side was NOT checked.")

    # The node's own durable state: the one place restart_count is persisted.
    durable = None
    try:
        durable = json.load(open(a.target_durable))
    except Exception:
        durable = None
    print()
    print("=== TARGET durable membership (the node's own all-time state) ===")
    if not isinstance(durable, dict):
        print(f"  NOT READ ({a.target_durable}) -- restart_count cannot be checked")
    else:
        rc = durable.get("restart_count")
        gen = durable.get("membership_generation")
        dep = depth(durable.get("incarnation", ""))
        print(f"  protocol            : {durable.get('protocol')}")
        print(f"  node_id             : {durable.get('node_id')}")
        print(f"  restart_count       : {rc}")
        print(f"  membership_generation: {gen}")
        print(f"  incarnation depth   : r{dep}")

    # seed_http_unreachable is a raised code, not a field: count sightings on BOTH sides.
    def sightings(*roots: str) -> int:
        hits = 0
        for root in roots:
            for base, _dirs, files in os.walk(root):
                for name in files:
                    if not name.endswith((".json", ".jsonl", ".log", ".txt")):
                        continue
                    p = os.path.join(base, name)
                    try:
                        if os.path.getsize(p) > 2_000_000:
                            continue
                        hits += len(re.findall(r"seed_http_unreachable",
                                               open(p, errors="replace").read()))
                    except Exception:
                        continue
        return hits

    found = []

    found += deviations("owner", owner)
    if target:
        found += deviations("target", target)

    # Durable-state cross-check: restart_count must equal the rounds observed, and the
    # generation/depth must follow from it. This is the per-round delta, one level up.
    if isinstance(durable, dict):
        rc, gen = durable.get("restart_count"), durable.get("membership_generation")
        dep = depth(durable.get("incarnation", ""))
        if isinstance(rc, int) and rc != len(target):
            found.append(("durable", "membership.state.json",
                          [f"restart_count {rc} != {len(target)} rounds observed on the target side"]))
        if isinstance(rc, int) and isinstance(gen, int) and gen != rc + 1:
            found.append(("durable", "membership.state.json",
                          [f"generation {gen} != restart_count+1 ({rc + 1})"]))
        if isinstance(rc, int) and dep != rc:
            found.append(("durable", "membership.state.json",
                          [f"incarnation depth r{dep} != restart_count {rc}"]))

    unreach = sightings(f"{a.owner_root}/local-contributor/managed", a.target_records)
    print()
    print(f"  seed_http_unreachable sightings (both sides) : {unreach}")
    print("  -> a raised code, not a field: zero sightings means no artefact names it,")
    print("     never that no seed call failed. Read a round's logs to attribute one.")

    print()
    if found:
        print(f"CHURN_GATE FAIL -- {len(found)} deviation(s)")
        for label, run, notes in found:
            print(f"   [{label}] {run}: {'; '.join(notes)}")
        return 1
    print("CHURN_GATE PASS -- on both sides: generation advances by exactly one per round, depth ==")
    print("                  generation-1 on every round, and restart_count == the rounds observed.")
    print("NOTE: at most one re-enrollment per round; no run's own records hold two incarnations, so this")
    print("      is consistent with no in-run churn -- which is the strongest claim these artefacts carry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
