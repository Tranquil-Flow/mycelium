# Before / after dependency edges — A5 amendment draft

**Status: DRAFT. NOT APPROVED. NOT APPLIED.** Nothing in this file has been written to any canonical
source. This is an owner-facing plan change; the owner approves it, the integration owner applies it.

**Read with:** `01-AMENDMENT.md` (the amendment text), `04-EVIDENCE-AND-GAPS.md` (the citation log,
including what is *not* verified).

**Sources read for this file** (all read-only; digests in `04-EVIDENCE-AND-GAPS.md`):

| short name | path | protocol / identity |
|---|---|---|
| **ledger** | `/Users/evinova-self/Desktop/MYCELIUM_REMAINING_WORK_LEDGER_2026-09-07.json` | `mycelium.remaining_work_ledger.v1`, 5,710 lines, sha256 `117c106c…721e`, mtime 2026-09-10 09:44 (last write = the deferral event) |
| **completion-plan** | `docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md` @ pilot HEAD `9d68291` | — |
| **MASTER_PLAN** | `/Users/evinova-self/Desktop/MYCELIUM_REMAINING_WORK_MASTER_PLAN_2026-09-07.md` | 751 lines |
| **PLAN** | `/Users/evinova-self/Documents/mycelium-core-program/PLAN.md` | the program's only plan, in force |

---

## 0. Two structural facts that govern how the change must be expressed

1. **The ledger is append-only and the P-rows live *only* in it.** The task rows P00–P18 are **not** in
   `contracts/governance-ledger.v1.json` (that file is protocol `mycelium.governance_ledger.v1` and
   contains **zero** occurrences of `P05`, `P09` or `P03` — verified by grep). The P-rows live in the
   `mycelium.remaining_work_ledger.v1` snapshot named above, which is append-only and sits outside the
   orchestrator's write boundary (recorded in `status/decisions.log` line 15, 2026-09-15).
   **Consequence: the amendment is expressed as *additive coordination events*, never as row edits.**
   That is also the discipline the independent review's own plan-delta document states at its line 5.

2. **Two of the task ids the amendment needs do not exist yet.** The ledger's `tasks` array contains
   exactly `P00…P18` (verified by parsing the JSON). The review's proposed rows **P19** ("Baseline repair
   and checkout hygiene") and **P23** ("Full independent A9 adjudication") are *proposals* in
   `PROPOSED_PLAN_DELTA.md` D14 (lines 231–239); they are **not ledger rows today**. A dependency on a
   non-existent row cannot be encoded, so registering those two rows is a **precondition** of the
   amendment, carried in the same additive event. Flagged again in `01-AMENDMENT.md` §6 and
   `04-EVIDENCE-AND-GAPS.md` G2.

---

## 1. BEFORE — the edges as currently encoded (verbatim, with line numbers)

All quotes are from **ledger**, verbatim, one field per line as the file stores them.

| edge | id line | `dependencies` line | verbatim value | note |
|---|---|---|---|---|
| `P02 ← P01` | `3763: "id": "P02"` | `3767: "dependencies": [` | `3768: "P01"` | A5 chain |
| `P03 ← P02` | `3817: "id": "P03"` | `3821: "dependencies": [` | `3822: "P02"` | A5 chain; **P03 = "Close downstream product/browser gates and metadata"** |
| **`P05 ← P03`** | `4026: "id": "P05"` | `4030: "dependencies": [` | `4031: "P03"` | **REMOVE.** P05 = "Preserve and integrate existing replay implementation" (gate A6), status `pending` (`4029`) |
| `P06 ← P05` | `4078: "id": "P06"` | `4082: "dependencies": [` | `4083: "P05"` | keep — P06 = "Qualify replay recovery through ordinary product" (A6 **physical**) |
| `P07 ← P06, P04` | `4133: "id": "P07"` | `4137: "dependencies": [` | `4138: "P06"`, `4139: "P04"` | keep |
| `P08 ← P06` | `4183: "id": "P08"` | `4187: "dependencies": [` | `4188: "P06"` | keep (A7) |
| **`P09 ← P07`** | `4234: "id": "P09"` | `4238: "dependencies": [` | `4239: "P07"` | **REMOVE.** P09 = "Reconcile dirty platform-neutral implementation" (gate A9) |
| `P10 ← P09` | `4286: "id": "P10"` | `4290: "dependencies": [` | `4291: "P09"` | keep — P10 = "Qualify three physical desktop platforms" |
| `P13 ← P10` | `4444: "id": "P13"` | `4448: "dependencies": [` | `4449: "P10"` | keep (not in scope of this amendment) |
| `P14 ← P13` | `4496: "id": "P14"` | `4500: "dependencies": [` | `4501: "P13"` | keep (not in scope; review D6 handles it separately) |
| `P15 ← P07` | `4552: "id": "P15"` | `4556: "dependencies": [` | `4557: "P07"` | keep (not in scope; review D7 handles it separately) |

**Encoded critical path** (ledger, verbatim, `critical_path[0]`):

```
4764:  "critical_path": [
4765:    [
4766:      "P00",
4767:      "P01",
4768:      "P02",
4769:      "P03",
4770:      "P05",
4771:      "P06",
4772:      "P07",
4773:      "P09",
4774:      "P10",
4775:      "P11",
4776:      "P12",
4777:      "P16",
4778:      "P17",
4779:      "P18"
4780:    ],
```

So the encoded path is `P00→P01→P02→P03→P05→P06→P07→P09→P10→P11→P12→P16→P17→P18`, and **A5's
`P03` sits on it, ahead of everything downstream including A9.** This is the structural problem.

### 1.1 The state that makes the A5 condition unsatisfiable-by-intent

| fact | citation | verbatim |
|---|---|---|
| the scored cycle was **deferred**, not completed | ledger `5692` (event_id), `5695` | `"disposition": "A5_scored_replica_cycle_deferred_not_complete"` |
| A6's scheduling condition is recorded **unsatisfied** | ledger `5698`, and again at `5556` | `"A6_scheduling_condition_satisfied": false` |
| A5 is recorded **not complete** | ledger `5697` | `"A5_complete": false` |
| Stage C was **never authorised** | ledger `5592`, `5627`, `5659`, `5688`, `5704` | `"stage_C_authorized": false` |
| the owner's own rule, as recorded | ledger `5462` | `"decision": "After a fresh valid non-promoting A5 disposition, its scheduling gate may close to permit later controlled A6 integration while A5 remains design_only/incomplete/non-promoting"` |
| and its condition is likewise unsatisfied | ledger `5463` | `"fresh_valid_result_condition_satisfied": false` |
| no calendar estimate exists | ledger `5106` | `"calendar_estimate": null` |

### 1.2 The authorities that already contradict the encoded edge

| authority | citation | verbatim |
|---|---|---|
| **normative plan** — A5 is not an A6 prerequisite | completion-plan `436` | `A5 is not a prerequisite for A6. Replay may use any newly qualified compatible path;` |
| normative plan — A6's own prerequisite is **A4** | completion-plan `425` | `\| A6 \| A4 \|` |
| normative plan — A9's prerequisite is **A8** | completion-plan **`428`** | `\| A9 \| A8 \|` |
| normative plan — A14's prerequisite is **A8** | completion-plan `433` | `\| A14 \| A8; A1 is already transitive \|` |
| **MASTER_PLAN** — the rule is scheduling, not technical | MASTER_PLAN `34` | `There are **technical dependencies** and **this program's integration/fleet ordering**. A6 technically depends on A4, not A5; scheduling its integration after A5 release avoids source/fleet collisions.` |
| **PLAN.md (in force)** — the decision is already made | PLAN `32` (D2) | `**A6 source integration no longer waits on any A5 result.** A6 physical work stays serialised against other fleet use.` |
| PLAN.md (in force) — A5 posture | PLAN `33` (D3) | `**A5 is off the pilot path.** Replicas are disabled by default. No scored runs. Any future A5 run declares its purpose in advance. Thresholds stay unchanged.` |
| owner decision file | `/Users/evinova-self/Desktop/MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md` line `9`, sha256 `5763a8a2…` | `After a **fresh, valid but non-promoting A5 result**, A5's scheduling gate may close and permit **later controlled A6 integration**, while A5 remains explicitly `design_only` / incomplete and non-promoting.` |

> **Citation discrepancy, stated rather than smoothed over.** The review cites `completion-plan.md:427`
> for "A9's technical prerequisite is A8". In the copy at pilot HEAD `9d68291` that row is on **line
> 428**; line 427 is `| A8 | A3 |`. I use the observed line (428). The A6 row (425) and the A14 row
> (433) match the review's citations exactly, and the prose citation `:436` matches exactly.

---

## 2. AFTER — the proposed effective edges

Expressed as **additive coordination events** in the ledger (never as row edits). The event payloads are
in `01-AMENDMENT.md` §8.

| task | before (encoded) | after (effective) | delta |
|---|---|---|---|
| **P05** A6 preserve/integrate | `["P03"]` | `["P19"]` | **`P03` removed entirely.** No A5 disposition of any kind — not a fresh valid non-promoting one, not a downstream closeout, not a benchmark result. `P19` = the green baseline (see §3). |
| **P06** A6 physical qualification | `["P05"]` | `["P05"]` | **unchanged.** A6 *physical* work stays serialised against fleet use; the amendment decouples *source integration* only. |
| **P07** converge A3+A4+A5+A6+A8 | `["P06","P04"]` | `["P06","P04"]` | **unchanged.** |
| **P09** A9 reconcile | `["P07"]` | `["P19","P23"]` | **`P07` removed.** `P19` = green baseline; `P23` = bounded full independent A9 adjudication of the A9 candidate (read-only, local, parallel-safe). |
| **P10** A9 three physical desktops | `["P09"]` | `["P09"]` | **unchanged.** A9's physical gate (hardware + frozen thresholds) is untouched. |
| **P03** A5 downstream closeout | `["P02"]` | `["P02"]` | **unchanged as a row; it simply stops gating anything downstream.** The A5 chain `P00→P01→P02→P03` becomes a dormant branch. |
| P02, P08, P13, P14, P15 | as listed in §1 | as listed in §1 | **unchanged** (out of scope of this amendment). |

**New effective edge list (derived view — this is what the ledger should *mean* after the events land):**

```
P02 ← P01
P03 ← P02                     (dormant: no downstream depends on P03 any more)
P05 ← P19                     (was: P03)
P06 ← P05
P07 ← P06, P04
P08 ← P06
P09 ← P19, P23                (was: P07)
P10 ← P09
P13 ← P10
P14 ← P13
P15 ← P07
```

**Critical path, recomputed from the after-edges** (derived, by hand from the edge list above — *not* an
owner-authored value, and **not** a repair to the ledger's `critical_path` field, which is a separate
decision the owner may or may not take):

- before: `P00→P01→P02→P03→P05→P06→P07→P09→P10→P11→P12→P16→P17→P18` (15 nodes)
- after, longest remaining chain: **`P19→P09→P10→{P11→P12 | P13→P14}→P16→P17→P18`** (8 nodes)
  — with the A5 branch `P00→P01→P02→P03` hanging off the critical path entirely, and the A6 branch
  `P19→P05→P06→P07→P16→P17→P18` (7 nodes) as the second-longest.

**What the amendment does *not* do to the edges, deliberately:**

- It does not delete or re-title `P03`. The A5 downstream closeout remains a real, owner-decidable piece
  of work; it stops being a *gate on other people's work*.
- It does not write a scheduling position into the `dependencies` array. PLAN `34` (D4) sets the
  integration order (`… → pilot qualification → A9 (rebased) → A6 → 27B / C-UC1 v3`). That is an
  **ordering statement, not a hard edge**, and mixing the two is exactly what produced the stale edge
  being corrected here. The amendment keeps the order in PLAN and keeps the ledger's `dependencies` to
  hard prerequisites only.
- It does not touch `P14 ← P13` or `P15 ← P07`. The review treats those in D6/D7 and they are outside
  this amendment's stated purpose; bundling them would make this change harder to refuse cleanly.

---

## 3. What "a green baseline" names concretely

`P19` is shorthand. The concrete referent the amendment should bind to is the **B1 baseline**: commit
`39f631e` on `integration/core-pilot`, whose green run is recorded in `status/decisions.log` line 11
(2026-09-15): `full canonical suite on 39f631e = 5198 passed, 13 skipped, 121 subtests passed (471 s);
audits exit 0 (contract 79, claim boundary 502 files, governance gate, completion audit); frontend 630
passed, typecheck and build clean. WP1 closed 5/5 verified.`

**Verified here:** `39f631e` **is an ancestor of pilot HEAD `9d68291`** (`git merge-base --is-ancestor
39f631e HEAD` → true), as are the later fan-ins `435b857` (A14a) and `1a022c9` (A13 lane).

**Not verified here, and it matters (see `04-EVIDENCE-AND-GAPS.md` G4):** that the *current* HEAD is
green. The B1 green run predates the A13 lane fan-in and later commits, and inherited failures were
recorded at that fan-in (`2 tests fail on the lane commit in isolation … tests/a13_macos 16 failed /
689 passed`, later re-classified to `5 failed, 700 passed` in a build-inclusive run —
`status/decisions.log` lines 62 and 65). **Therefore `P19` is a *condition to be re-earned at the
application commit*, not a satisfied checkbox.** The amendment must not be read as asserting HEAD is
green, and this draft does not re-run any suite.
