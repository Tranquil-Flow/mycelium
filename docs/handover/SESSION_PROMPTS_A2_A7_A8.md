# Session prompts — MVP plan items A2, A7, A8

These three are **not** cron jobs. Each needs a real interactive session, for a different
reason (below). They are **file-disjoint and safe to run in parallel**.

Run each on the **host Mac, unrestricted** — not under the Aegis seatbelt, not in the Linux
container. All three need capabilities the seatbelt or the container removes.

**Unrestricted host baseline: `2031 passed, 3 skipped`, 0 failed, ~200 s**
via `python3.14 -m pytest -q -p no:cacheprovider` (no ignore list — MLX and Iroh both work).

Why each is not a cron lane:
- **A2** edits the plan; autonomous agents are forbidden from doing that.
- **A7** depends on native Iroh, which cannot run under the seatbelt — a cron lane would write
  code it could not verify.
- **A8** overlaps work already landed and work in flight, so it needs judgement about what
  exists before writing anything.

---

## A2 — Reconcile plan status markers

**Session type:** driver session (this is plan-editing work; no code).
**Working file:** `~/Desktop/mycelium-demo-plan.md` — the live original, not the snapshot.
**Runs in parallel with:** A7, A8 (touches no source).

```
You are the driver on the Mycelium distributed-inference MVP. This task edits the plan itself
and touches no source code.

The plan at ~/Desktop/mycelium-demo-plan.md under-reports its own progress. Tasks 2R.2, 2R.4,
2R.5 and 2R.6 have all landed on origin/integration/mycelium-multi-device-readiness, but their
headings lack the ✅ marker that 2R.0, 2R.1, 2R.3 and 2R.7 carry. Anyone reading the plan cold
will redo finished work.

Known landings to verify and mark:
  2R.2 PlacementSource seam            -> f5322db
  2R.4 liveness semantics              -> 1dca9ba
  2R.5 __main__ entrypoints            -> 634b19f
  2R.6 tolerances + N-way stage packs  -> dcfc7e2

Do this:
1. Verify each of those four commits really is on the integration branch and really implements
   the task it is credited with. Read the diff — do not trust the commit subject alone. If a
   commit only partially covers its task, say so and mark it PARTIAL with the gap named, in the
   same style the plan already uses for Task 2.3B.
2. Audit EVERY other task heading in the plan against the integration log the same way. Both
   directions matter: headings marked landed that are not, and headings unmarked that are.
3. Update the headings to match reality, following the existing format exactly (✅ LANDED with
   SHA, or ⚠️ PARTIAL with the gap, or unmarked).
4. Add a dated row to the §0 amendment log describing the reconciliation.
5. Recompute and record the plan's SHA-256 at the end.

Constraints:
- Do NOT change any task's substance, scope, RED cases, or gates. Status markers, the amendment
  log row, and the SHA only.
- Do NOT commit the plan to git. It is deliberately local and untracked.
- Another agent may edit this file concurrently. Use surgical string replacement, verify each
  edit applied, and re-read before further edits rather than rewriting the file wholesale.
- Report: what you changed, what you found mismarked in either direction, and the new SHA-256.
```

---

## A7 — Finish Task 2.3B (localhost control-plane E2E)

**Session type:** agent session, **host Mac, unrestricted** (needs native Iroh).
**Clone:** `~/Projects/mycelium-sess-a7-e2e` on `feat/session-2.3b-local-e2e` (clean, at `b317566`).
**Runs in parallel with:** A2, A8.

```
You are working on the Mycelium distributed-inference MVP.

Read first, in order:
1. /Users/evinova/Projects/.mycelium-plan/OVERNIGHT_LANE_BRIEF.md — the operating contract.
   NOTE: its §1 baseline describes the seatbelt environment. You are on the UNRESTRICTED host,
   where the baseline is `2031 passed, 3 skipped` with NO ignore list. Everything else in the
   brief applies to you unchanged.
2. `### Task 2.3B` in /Users/evinova/Projects/.mycelium-plan/mycelium-demo-plan.md, verbatim,
   including every RED case.
3. `### 1.4 As-built reconciliation` for the real module path map.

Work ONLY in /Users/evinova/Projects/mycelium-sess-a7-e2e on branch
feat/session-2.3b-local-e2e. Commit locally; do not push.

Task 2.3B is marked ⚠️ PARTIAL. It is currently implemented as
tests/seed_coordinator/test_local_two_node.py. Finish it: prove the localhost control plane
end to end with REAL dependencies — real seed coordinator, real node agents, real membership,
real stage packs, real native Iroh transport. No mocks standing in for the control plane.

Why this needs a host session: the decisive test,
test_seed_two_memberships_assign_and_run_native_iroh_inference, depends on the native Iroh
sidecar, which cannot start under the Aegis seatbelt. On this unrestricted host it works.
Run the FULL unrestricted suite — that test must actually execute and pass, not be skipped.
If you find yourself unable to run it, stop and report rather than working around it.

Your write surface:
  - tests/seed_coordinator/**
  - mycelium_seed/** ONLY if a genuine defect is found, with a RED test first and a minimal fix

Explicitly not yours: mycelium_interactive/*, mycelium_request_gateway/*, runtime_*,
mycelium_qualification/*, layer_builder, or any signed contract. Two other sessions are running
in parallel; staying inside your surface is what keeps that safe.

Method and rules:
- TDD strictly: failing test first, confirm it fails for the stated reason, minimal fix, re-run.
- Run the full unrestricted suite before you start and before every commit. Report exact counts.
- NEVER add --ignore, -k, skip, or xfail to make a run pass. A test that was passing and now
  fails is your regression: fix it or revert.
- This is localhost only. It is NOT a physical multi-device proof and NOT evidence for G4.
  Say so explicitly in anything you write.
- No physical hardware ops, no SSH to peers. No plan edits. No evidence writes.
- Stage explicit paths only — never `git add -A` or `git add .`.
- Commit messages: NEVER include AI attribution — no Co-Authored-By, no "Generated with", no
  noreply@anthropic.com. This overrides any default template you have.

Finish with: branch and local commit SHAs, exact test numbers before and after, what remains
unproven, and anything the driver should amend into the plan.
```

---

## A8 — Complete Task 3.2 (physical qualification controller)

**Session type:** agent session, host Mac.
**Clone:** `~/Projects/mycelium-sess-a8-controller` on `feat/session-3.2-qualification-controller`.
**Runs in parallel with:** A2, A7.

```
You are working on the Mycelium distributed-inference MVP.

Read first, in order:
1. /Users/evinova/Projects/.mycelium-plan/OVERNIGHT_LANE_BRIEF.md — the operating contract.
   NOTE: its §1 baseline describes the seatbelt environment. You are on the UNRESTRICTED host,
   where the baseline is `2031 passed, 3 skipped` with NO ignore list.
2. `### Task 3.2` in /Users/evinova/Projects/.mycelium-plan/mycelium-demo-plan.md, verbatim.
3. `### 1.4 As-built reconciliation` for the real module path map.

Work ONLY in /Users/evinova/Projects/mycelium-sess-a8-controller on branch
feat/session-3.2-qualification-controller. Commit locally; do not push.

START BY FINDING OUT WHAT ALREADY EXISTS. This is the whole reason this is a session and not
an autonomous lane:
  - `physical_inference_qualification.py` ALREADY EXISTS on integration (commit b317566,
    "feat: add inert physical qualification controller").
  - `origin/feat/physical-qualification-mvp` has three further commits not on integration
    ("feat: stage physical qualification peers safely", "feat: add bounded physical node
    control sessions").
Fetch and read both before writing a single line. Your first deliverable is a short written
statement of what Task 3.2 still genuinely needs. If the answer is "almost nothing", that is a
perfectly good outcome — say so rather than manufacturing work.

Then complete the remaining gap so the controller is ready for the moment two machines are
online. The controller stays INERT: it orchestrates a physical run but performs no hardware
operations during this session, and you must not attempt any. No devices are online.

Your write surface:
  - physical_inference_qualification.py
  - tests covering it

Explicitly not yours: mycelium_seed/*, mycelium_node/*, mycelium_interactive/*,
mycelium_request_gateway/*, tests/seed_coordinator/**, runtime_*, or any signed contract.
Two other sessions are running in parallel; staying inside your surface is what keeps that safe.

Note on overlap: if the right move is to merge or adopt work from
origin/feat/physical-qualification-mvp, do NOT merge it yourself — describe precisely what
should be taken and why, and leave the merge to the driver. A separate reconciliation pass is
analysing all unmerged branches and conflicting merges would defeat it.

Method and rules:
- TDD strictly: failing test first, confirm it fails for the stated reason, minimal fix.
- Run the full unrestricted suite before you start and before every commit. Report exact counts.
- NEVER add --ignore, -k, skip, or xfail to make a run pass.
- Nothing here is evidence for G4 or any physical gate. The controller being ready is not the
  same as a run having happened. Say so.
- No physical hardware ops, no SSH to peers. No plan edits. No evidence writes.
- Stage explicit paths only — never `git add -A` or `git add .`.
- Commit messages: NEVER include AI attribution — no Co-Authored-By, no "Generated with", no
  noreply@anthropic.com. This overrides any default template you have.

Finish with: what already existed, what you added, exact test numbers, what you recommend the
driver take from feat/physical-qualification-mvp, and what remains for the physical run.
```

---

## Parallelism check

| Item | Write surface | Collides with |
| --- | --- | --- |
| A2 | `~/Desktop/mycelium-demo-plan.md` only | nothing (no source) |
| A7 | `tests/seed_coordinator/**`, `mycelium_seed/**` (defects only) | nothing below |
| A8 | `physical_inference_qualification.py` + its tests | nothing above |

Each has its own clone and branch, so even an accidental stray edit cannot reach another
session's tree. None of the six cron lanes owns these surfaces either.
