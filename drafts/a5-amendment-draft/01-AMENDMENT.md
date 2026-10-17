# 01 — Amendment draft: decouple A6 source integration from A5, and re-point P09

> ## STATUS: DRAFT — NOT APPROVED — NOT APPLIED
>
> This document is **paper**. Nothing in it has been written to any canonical source. No file that this
> amendment would touch has been modified. The pilot tree at `/Users/evinova-self/Documents/playground/mycelium-core-pilot`
> (HEAD `9d68291`) was **read-only** throughout; this draft was composed in the separate lane worktree
> `/Users/evinova-self/Documents/playground/mycelium-a5-amendment-lane`, also at `9d68291`.
>
> **The owner approves plan changes.** Every numbered item below is **independently refusable**; refusing
> one does not refuse the others. If nothing is approved, nothing changes and the serialisation described
> in §0 persists.

**Amendment id:** `a5-amendment-2026-09-21-decouple-a6-repoint-p09`
**Prospective:** yes — it changes the *future* routing of work. It changes no past measurement, no past
decision record, and no gate's state.
**Evidence:** every claim below is read from a named file at a named line. The full log, including the
things I could **not** verify, is `04-EVIDENCE-AND-GAPS.md`.

---

## 0. Why this amendment exists (two sentences, both cited)

Gate A5 (multi-stage replicas, roadmap weight **6**) is **terminal and fully executed but not promoted**:
all **13** closure requirements are bound to evidence (`docs/handover/mycelium-completion-checklist.v1.json:617-631`)
and the frozen benchmark still concluded `inconclusive` — point estimate `0.16710745942796965`,
paired 95 % lower bound `-0.4196995766436599`, materiality threshold `0.1`, `promotion_authorized: false`
(`docs/handover/a5-completion-record.v1.json:4-13`). **That is a correct, honest result and this amendment
does not disturb it in any way.**

The defect is structural, not statistical: the execution DAG still routes unrelated work **through** that
deferred cycle. `P05` — A6 *source integration* — depends on `P03` — A5's downstream closeout
(`ledger:4030-4031`), and `P09` — A9 reconcile — depends on `P07`, which depends on `P06`, which is A6
*physical* qualification (`ledger:4238-4239`, `:4137-4139`). The A5 scored cycle was **deferred**
(`ledger:5692-5695`) and Stage C was **never authorised** (`ledger:5592`, `:5627`, `:5659`, `:5688`,
`:5704`), so work the owner has already decided is not blocked is formally blocked. The program's own plan
already records the correction in words (`PLAN.md:32` D2: *"A6 source integration no longer waits on any A5
result"*); what is missing is the amendment that puts those words into the sources the ledger reads.
The independent review states the same finding as F1 and recommends exactly these two changes
(`mycelium-core-plan-review-20260915/REVIEW.md:37-62`); the review's proposed texts are in its
`PROPOSED_PLAN_DELTA.md` D3–D5 and D11, and this draft reuses them rather than re-inventing them.

---

## 1. The amendment, in one table

| # | Change | Class | Independently refusable |
|---|---|---|---|
| **C1** | A6 *source integration* no longer requires **any** A5 disposition | OWNER, prospective | yes |
| **C2** | `P09` re-pointed from `P07` to **A9 independent adjudication + a green baseline** | OWNER, prospective | yes |
| **C3** | A5's standing disposition recorded (a note; **no** state, threshold or protocol change) | OWNER | yes |
| **C4** | A5's `closing_commit` sentinel — **WITHDRAWN, recommend REFUSE.** A standing written instruction says *do not* replace it | — (see §5) | yes |
| **C0** | **Precondition:** register rows `P19` and `P23` so C1/C2 have something to point at | FACT (registration) + OWNER (sequencing) | yes |

---

## 2. C1 — A6 *source integration* decoupled from any A5 disposition

**Edge changed:** `P05 ← P03` → `P05 ← P19`.
Verbatim before-state: `ledger:4026 "id": "P05"`, `ledger:4030 "dependencies": [`, `ledger:4031 "P03"`.
Full before/after edge table: `03-EDGES-BEFORE-AFTER.md` §1–§2.

**Scope of the decoupling — stated narrowly, on purpose:**

| decoupled | still serialised / still required |
|---|---|
| A6 *source integration* — the controlled fan-in of an accepted A6 candidate with its deterministic, contract, governance and composition checks. It now requires **no** A5 disposition of any kind: not a fresh valid non-promoting one, not a downstream closeout, not a benchmark result. | A6 ***physical*** qualification (`P06`) remains serialised against other fleet use, keeps its own separately-scoped grant, and is **not** authorised by this amendment. A6 sealing is not authorised. |
| | The accepted candidate itself, controlled integration checks, and applicable post-integration verification — unchanged (`MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md:20`). |
| | `git commit` / `git push` / PR remain owner-gated (`PLAN.md:41` D11). |

**Why this is not a weakening of the A5 rule.** The rule's stated purpose was avoiding source/fleet
collisions (`MASTER_PLAN:34`), and it was already relaxed by the owner on 2026-09-07 to a *"fresh valid
non-promoting A5 disposition"* (`MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md:9`, sha256 `5763a8a2…`,
recorded in the ledger at `:5455-5469`). Source integration no longer shares the fleet, so the source half
of the rule has no remaining referent; only the fleet half survives, and it survives intact in `P06`.
The normative plan never made A5 an A6 prerequisite in the first place
(`docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md:436`: *"A5 is not a prerequisite for A6."*;
and its own dependency table puts A6 under A4 at `:425`).

---

## 3. C2 — `P09` re-pointed to A9 adjudication plus a green baseline

**Edge changed:** `P09 ← P07` → `P09 ← P19, P23`.
Verbatim before-state: `ledger:4234 "id": "P09"`, `ledger:4238 "dependencies": [`, `ledger:4239 "P07"`.

- `P19` — **green baseline.** Concretely: the committed B1 baseline `39f631e` on `integration/core-pilot`,
  recorded green at `status/decisions.log:11`, and **re-earned at whatever commit the amendment is applied
  to**. `39f631e` is verified here as an ancestor of pilot HEAD `9d68291`. `P19` is a *condition*, not a
  satisfied checkbox — see `03-EDGES-BEFORE-AFTER.md` §3 and gap **G4** in `04-EVIDENCE-AND-GAPS.md`.
- `P23` — **bounded full independent A9 adjudication.** Read-only, local, parallel-safe: it needs no
  hardware and no fleet, which is precisely why it can run ahead of `P07`. A9's technical prerequisite is
  A8, which is complete (`completion-plan.md:428`).

**Retained, explicitly:** `P10 ← P09` is unchanged. A9's *physical* gate still needs the three-platform
matrix and the frozen performance floors; C2 makes A9 **adjudicable**, it does not make A9 *accepted*, and
it does not integrate anything.

**A second, smaller consequence worth naming:** with `P09 ← P07` gone, the A5 and A6 branches stop being on
the critical path. The recomputed longest chain is derived in `03-EDGES-BEFORE-AFTER.md` §2 and is labelled
there as *derived*, not owner-authored. This amendment does **not** rewrite the ledger's `critical_path`
field; that is a separate decision.

---

## 4. C3 — A5's standing disposition, recorded

Append a dated **disposition note** to two documents — the completion plan's A5 section and the A5
specification's completion section. **No threshold, protocol, accounting, invalidation or stopping-rule
text is changed.** The text is in §8 below.

The recorded posture, exactly:

- A5 remains `design_only`, not promoted (`docs/handover/a5-completion-record.v1.json:4`; `:11`).
- It is excluded from the core pilot view; replica tracks are **disabled by default**.
- A future scored cycle requires a prospectively declared purpose — one of exactly two kinds (§7 of this
  file, and `02-OPTIONS.md`). It never reruns an already-scored workload to seek a different bound, and it
  never changes a threshold.
- The A5 scoring candidate is an **immutable commit** and need not pin the integration branch head; Stage
  B's preparation, bound to `b9001e6`, **fails closed** at any admission against a different candidate.

---

## 5. C4 — **WITHDRAWN**: the A5 record's `closing_commit` sentinel is deliberate

**I am withdrawing this item, and I recommend the owner refuse it if it is ever raised again.**
The independent review lists it as a defect (F10: *"A5 record `closing_commit` is still a placeholder
(`assigned_by_git…`)"*, `REVIEW.md:225`), and the earlier draft in `runs/a5-disposition/` proposed filling
it (`:40`, proposed value `b9001e6`). **Both readings are wrong, and here is the authority that settles it.**

The programme's own A5 integration goal — `/Users/evinova-self/Desktop/A5_INTEGRATION_REQUALIFICATION_GOAL_2026-08-30.md`
— says the sentinel is **intentional and is not to be replaced**. Verbatim, lines `124`–`130`:

```
124|Its `closing_commit` intentionally says:
126|```text
127|assigned_by_git_to_the_atomic_commit_containing_this_record
128|```
130|This avoids an impossible self-referential commit SHA. Verify that standalone commit `44d5b38...` contains the exact record bytes. Do not replace the sentinel with a guessed or circular SHA unless repository protocol and validators explicitly require a different non-circular binding mechanism.
```

So: **the sentinel is the correct value.** It exists because a record cannot name the commit that contains
it without a self-reference; the reproducibility is supplied instead by the *verification step* the same
instruction names — *verify that standalone commit `44d5b38…` contains the exact record bytes*.
**That step is discharged below, and it passes.**

### 5.1 The verification the goal asked for, performed (read-only)

| step | result |
|---|---|
| the goal names the standalone A5 commit | `A5_INTEGRATION_REQUALIFICATION_GOAL_2026-08-30.md:5` — *"Integrate completed standalone A5 commit `44d5b38aa0d0dfc7ad06f4891ec39851f0cb6c2b`"*; repeated at `:60` (`commit:   44d5b38aa…`) and `:173` (`tree(44d5b38aa…)`) |
| it also names the exact-parent criterion | `:201` — *"Verify A5 commit `44d5b38...`, exact parent `1fd3a6f...`, one-commit distance, tree cleanliness, author, subject, and containment."* |
| **does `44d5b38` contain the exact record bytes?** | **yes.** `git rev-parse 44d5b38:docs/handover/a5-completion-record.v1.json` → blob `ddc8e57dd55deee76e99490dc966ee7990068dcd`, which is **byte-identical** to the record on disk at pilot HEAD `9d68291` (`git hash-object` → same blob; sha256 `9aa287d2c7e35a1db36c01e4f2326891747b497d77b43285e8b3fbc2a2ea1628`). |
| **exact parent / one-commit distance** | **yes.** `44d5b38` has exactly one parent: `1fd3a6f8733fea52e55e0fbcbf46e86fa7bed03f` — which is **exactly** the record's own `source_binding.base_commit` (`a5-completion-record.v1.json:59`). |
| **was it ever modified afterwards?** | **no.** `git log --full-history --format=%H 0a83a2d..HEAD -- <path>` is empty: the record has not been touched since the fan-in, and the blob is unchanged at every commit that contains it. |
| the record is present in the integration line | via `0a83a2d` (*"integration: fan in A5 candidate"*, 2026-09-03, parent `efcdf44d…`), identical blob. `0a83a2d` is an ancestor of `b9001e6`, and of pilot HEAD. |
| the goal's own binding agrees with the record's | goal `:110-116` states the same artifact path, SHA-256 `d3748976…`, protocol `mycelium.candidate_source_manifest.v1`, base `1fd3a6f8…`, `entries: 101`, `total size: 3,008,354 bytes` — matching `a5-completion-record.v1.json:55-63` field for field. |

**Conclusion.** `44d5b38aa0d0dfc7ad06f4891ec39851f0cb6c2b` is the commit the record's `claim_boundary`
describes, and the record bytes inside it are the bytes on disk today. The sentinel stays. **Nothing in this
amendment depends on C4; it is retained in this document only so that the next session does not "fix" it.**

> Two secondary notes for honesty. (i) `44d5b38` is **not** an ancestor of pilot HEAD `9d68291` — it is
> reachable as the tip of ref `codex/a5-multistage-replication`; the integration line carries the identical
> content via `0a83a2d`. (ii) I did **not** inspect the A5 lane's own repository
> (`/Users/evinova-self/Documents/playground/mycelium-wave8-a5-build`) beyond confirming the ref, so I
> cannot exclude that some *other* repository holds a differently-defined "atomic A5 feature commit".
> The goal document quoted above is the authority for the value, not my archaeology.

---

## 6. C0 — precondition: two task rows that do not exist yet

The ledger's `tasks` array contains **exactly `P00…P18`** (verified by parsing the JSON: `['P00','P01',…,
'P18']`). The ids `P19` and `P23` come from the review's *proposal* list
(`mycelium-core-plan-review-20260915/PROPOSED_PLAN_DELTA.md:231-239`, D14) and are **not ledger rows today**.

**Therefore the amendment's additive event must register them in the same change.** Until they exist, a
`"dependencies": ["P19","P23"]` value would point at nothing — an encoded edge that cannot be evaluated is
exactly the class of defect this amendment exists to remove, so it must not be introduced.
Definitions to register (from the review's D14, unchanged):

- **P19 — Baseline repair and checkout hygiene.** Fix the canonical failing test with RED/GREEN; owner
  disposition of the dirty/staged A13-lite bytes; canonical suite green on a new committed baseline.
- **P23 — Full independent A9 adjudication** of the A9 candidate. Read-only reviewer; dependencies: none.

---

## 7. What a *future* A5 scored cycle would have to be (so the decoupling cannot be read as a door left open)

Only **two** purposes are admissible, and **both must be declared in writing before the run**. They are
drafted in full in `02-OPTIONS.md`:

- **(a) a functional NON-PROMOTING requalification** of a named, immutable integrated candidate; or
- **(b) a NEW frozen performance protocol** motivated by a declared architecture change or a declared
  variance change.

Neither is authorised by this amendment. Both would need their own separately-scoped owner grant, and both
leave A5 `design_only` unless a *material* result is measured under a valid frozen protocol — which (b)
could in principle produce and (a) cannot, by construction.

---

## 8. Documents and records this amendment would touch (and what each change is)

Nothing in this table has been applied. Each row names the exact anchor so the integration owner can apply
it in one change.

| # | target | exact anchor | change | class |
|---|---|---|---|---|
| 1 | `/Users/evinova-self/Desktop/MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md` (28 lines, sha256 `5763a8a2…`) | append after line `28` | new **dated addendum** (text §8.1); the original 28 lines are **not** edited | OWNER, prospective |
| 2 | `/Users/evinova-self/Desktop/MYCELIUM_REMAINING_WORK_MASTER_PLAN_2026-09-07.md` | beside line `34` | inline annotation (text §8.2) | OWNER |
| 3 | `/Users/evinova-self/Desktop/MYCELIUM_REMAINING_WORK_LEDGER_2026-09-07.json` (append-only; `.lock` present) | `coordination_updates` array | three **additive events** (§8.3): register `P19`/`P23`; supersede `P05.dependencies`; supersede `P09.dependencies`. **No row is edited.** | C0 FACT + C1/C2 OWNER |
| 4 | `docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md` §6 A5 | after `:746` (`**Boundary:** …`) | disposition note (§8.4); no threshold or protocol text touched | OWNER |
| 5 | `docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md` §11 | append after `:308` | disposition note (§8.5); **no text in §1–§10 is changed** | OWNER |
| 6 | `docs/handover/a5-completion-record.v1.json` | `:16` | **NOT TOUCHED.** The `closing_commit` sentinel is deliberate and a standing instruction forbids replacing it; the verification it does require has been discharged and passes (§5) | — (C4 withdrawn) |

**Explicitly NOT touched by this amendment:** the A5 record's `state`, `outcome`, `bindings`,
`sealed_evidence`, `verification` or `limitations`; the checklist's A5 row (`state: design_only`,
blocker string, the 13 `completed_requirements`, all `evidence_bindings`); any file under `contracts/`;
any manifest; any test; any threshold; any timeout, gate, guard or identity check. Not one byte of the
pilot tree.

---

### 8.1 Insertable text — addendum to `MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md`

> ## Prospective amendment (2026-09-2x) — A6 source integration decoupled from any A5 disposition
>
> The rule in **Approved rule** above is amended **prospectively, for the future only**. A6 *source
> integration* — the controlled fan-in of an accepted A6 candidate with its deterministic, contract,
> governance and composition checks — **no longer requires any A5 disposition of any kind**: not a fresh
> valid non-promoting disposition, not a downstream closeout, not a benchmark result.
>
> Authority: the normative completion plan never made A5 an A6 prerequisite
> (`docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md:436` — *"A5 is not a prerequisite for
> A6"*; its dependency table places A6 under A4 at `:425`), and the program plan already records the
> decision (`PLAN.md:32` D2 — *"A6 source integration no longer waits on any A5 result. A6 physical work
> stays serialised against other fleet use."*). The historical scheduling rule existed to avoid
> source/fleet collisions (`MYCELIUM_REMAINING_WORK_MASTER_PLAN_2026-09-07.md:34`); source integration no
> longer shares the fleet, so only the fleet half of that rule remains, and it remains intact.
>
> **Unchanged, and reaffirmed by this addendum:**
> - A6 ***physical*** qualification remains serialised against any A5 fleet window and still requires its
>   own separately scoped grant. A6 sealing is not authorised here.
> - A5's performance thresholds, measurement protocol, accounting, invalidation rules and statistical
>   stopping rules, and the no-identical-retry rule.
> - A5 remains `design_only`, non-promoting, and off the pilot path. Replica tracks remain **disabled by
>   default**.
> - V23 remains historical invalid/no-result; its accepted operational recovery with intentional retention
>   is not reopened or upgraded. Stage B's acceptance with retained procedural non-compliance is not
>   upgraded; Stage C remains unauthorised.
> - The A9 work item is re-pointed to A9 independent adjudication plus a green baseline; A9's physical
>   gate is untouched.
> - No push, no PR, no release claim, no new physical authorisation is granted by this addendum.
>
> This addendum does not retroactively validate any historical attempt, does not alter any record written
> before its date, and does not advance any gate's stage credit.

### 8.2 Insertable text — annotation beside `MASTER_PLAN:34`

> *[Prospectively superseded 2026-09-2x — see the dated addendum in
> `MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md`. The "after A5 release" half of this sentence was a
> scheduling rule to avoid source/fleet collisions; A6 **source integration** no longer waits on any A5
> disposition. A6 **physical** work remains serialised against fleet use. The technical statement
> "A6 technically depends on A4, not A5" is unchanged.]*

### 8.3 Insertable events — ledger `coordination_updates` (append-only)

```json
[
  {
    "recorded_at": "<integration-owner timestamp>",
    "event_id": "a5-amendment-20260921-register-p19-p23",
    "disposition": "new_task_rows_registered",
    "rows": [
      {
        "id": "P19",
        "gate": "integration",
        "title": "Baseline repair and checkout hygiene",
        "status": "pending",
        "dependencies": []
      },
      {
        "id": "P23",
        "gate": "A9",
        "title": "Full independent A9 adjudication of the A9 candidate",
        "status": "pending",
        "dependencies": []
      }
    ],
    "note": "rows proposed by the independent plan review, PROPOSED_PLAN_DELTA.md D14; registering them grants no stage credit and authorises no integration."
  },
  {
    "recorded_at": "<integration-owner timestamp>",
    "event_id": "a5-amendment-20260921-p05-a5-condition-removed",
    "task": "P05",
    "disposition": "dependency_superseded_prospectively_a5_condition_removed",
    "supersedes_field": "tasks[P05].dependencies",
    "removed_dependency": "P03",
    "effective_dependencies": ["P19"],
    "authority": "MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md dated addendum; PLAN.md D2; completion-plan.md:436",
    "a5_disposition_required": false,
    "a6_source_integration_requires_accepted_candidate_and_integration_checks": true,
    "a6_physical_qualification_authorized_by_this_event": false,
    "a6_sealing_authorized_by_this_event": false,
    "a5_thresholds_unchanged": true,
    "a5_protocol_unchanged": true,
    "a5_state": "design_only",
    "a5_promotion_authorized": false,
    "identical_scored_retry_authorized": false,
    "push": false,
    "pr": false
  },
  {
    "recorded_at": "<integration-owner timestamp>",
    "event_id": "a5-amendment-20260921-p09-repointed",
    "task": "P09",
    "disposition": "dependency_superseded_prospectively",
    "supersedes_field": "tasks[P09].dependencies",
    "removed_dependency": "P07",
    "effective_dependencies": ["P19", "P23"],
    "retained": "A9 physical qualification (P10) still requires the physical platform matrix and the frozen performance floors",
    "review_note": "the independent review reproduced the A9 candidate's B1/B2 RED-on-parent / GREEN-on-candidate locally; that is a review observation, not full acceptance",
    "push": false,
    "pr": false
  }
]
```

### 8.4 Insertable text — completion plan §6 A5, after `:746`

> **Disposition note (2026-09-2x).** A5 is terminal and fully executed but **not promoted**: all 13 closure
> requirements are bound to evidence, and the frozen benchmark concluded `inconclusive` (+16.71 % point
> estimate; paired 95 % lower bound −0.4197 against the +0.10 materiality threshold). It is excluded from
> the core pilot view; replica tracks are **disabled by default** in pilot builds. A future scored cycle
> requires a prospectively declared purpose, either (a) a functional non-promoting requalification of a
> named immutable candidate, or (b) a new frozen protocol justified by a declared architecture or variance
> change; it never reruns an already-scored workload to seek a different bound and never changes a
> threshold. The A5 scoring candidate is an immutable commit and need not pin the integration branch head.
> Stage B's preparation, bound to `b9001e6`, fails closed at any admission against a different candidate.
> **No threshold or protocol text above is changed by this note.**

### 8.5 Insertable text — A5 spec §11, appended after `:308`

> **Disposition note (2026-09-2x).** A5 is terminal and fully executed but **not promoted**; this
> specification is unchanged and remains the frozen authority for any future A5 work. All 13 closure
> requirements are bound to evidence and the frozen benchmark concluded `inconclusive` (+16.71 % point
> estimate; paired 95 % lower bound −0.4197 against the +0.10 materiality threshold), so A5 remains
> `design_only` per §11 above. Replica tracks are **disabled by default**. A future scored cycle requires a
> prospectively declared purpose — (a) a functional non-promoting requalification of a named immutable
> candidate, or (b) a new frozen protocol justified by a declared architecture or variance change — and it
> never reruns the §9 workload expecting a different bound. **No text in §1–§10 is changed by this note;
> §9's threshold, protocol, invalidation rules and decision vocabulary stand exactly as frozen.**

---

## 9. What must NOT change (invariants this amendment is bound by)

These are not options and not negotiable inside this amendment. If any of them would have to move to make
the amendment "work", the amendment is wrong, not the invariant.

| # | invariant | authority |
|---|---|---|
| **N1** | **Replica tracks stay DISABLED BY DEFAULT.** An unpromoted capability must never be a default, and no exception is created anywhere. | `PLAN.md:33` (D3); review F7 (`REVIEW.md:191`); this draft creates none. |
| **N2** | **The materiality threshold is not relaxed, re-derived, or moved.** 10 % point estimate **and** paired 95 % lower bound, exactly as frozen. This includes Option (b) in `02-OPTIONS.md`, which keeps the same threshold. | spec §9, `:211` (`Material gain requires both the point estimate and the paired 95% bootstrap lower confidence bound to be at least **10%**`), `:239-243` (decision vocabulary) |
| **N3** | **The measurement protocol is not changed in place.** Order `ABBA`×3, 12 windows / 6 pairs, ≥60 terminal requests per window, deterministic paired bootstrap 10,000 resamples seed `0xA5` at the 2.5th percentile, identical bindings across windows. *Changing the manifest creates a new protocol and invalidates the old measurements* — it is never an edit. | spec §9, `:201-205`, `:213-224`, `:226-235` |
| **N4** | **No existing record is rewritten.** The amendment is prospective: it changes the future routing of work, never the past. Specifically unchanged: the A5 record's `state: design_only` and its whole `outcome` block; the checklist's A5 blocker string; V23's invalid/no-result status; Stage B's acceptance with retained procedural non-compliance; the ledger's historical rows. The ledger is **append-only** — task-row corrections are expressed as additive events that supersede, never as rewrites. | `a5-completion-record.v1.json:4-13`; checklist `:613-615`; `PROPOSED_PLAN_DELTA.md:5`; `MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md:14-22` |
| **N5** | **No identical scored retry, ever.** No retry-until-green, no "rerun the same workload and hope for a better bound". | `docs/superpowers/plans/2026-08-20-mycelium-a5-finish-plan.md:189`; `ledger:5465` (`"identical_scored_retry_authorized": false`); spec §9 |
| **N6** | **A5 stays `design_only` and off the pilot path.** No scored cycle runs on the pilot path. | `PLAN.md:33`; `a5-completion-record.v1.json:4`; `checklist:613` |
| **N7** | **A6 *physical* stays serialised against fleet use.** This amendment decouples *source integration* only. | `PLAN.md:32` (D2, second sentence) |
| **N8** | **A9's physical gate is untouched.** `P10` still needs the three-platform matrix and the frozen floors. | `completion-plan.md:210`; `ledger:4290-4291` |
| **N9** | **A5's Stage B admission fails closed** against any candidate other than the one it was bound to (`b9001e6`). | `ledger:5672-5688`; review F7 `REVIEW.md:185` |
| **N10** | **No push, no PR, no sealing, no release or promotion claim.** | `ledger:5451-5452` (`"push": false`, `"a6_seal": false`); `PLAN.md:41` (D11) |
| **N11** | **No gate's stage credit moves because of this amendment.** Stage credits are `spec 10 % / implemented 35 % / independently accepted 50 % / integrated 70 % / qualified 100 %`. | `PLAN.md:165`; and see §10 below |

---

## 10. What this draft does NOT establish

Stated plainly and exhaustively, because a draft that overclaims is worse than no draft.

1. **It is not approved.** It is a proposal addressed to the owner. The owner approves plan changes; no
   part of this document is in force.
2. **It is not applied.** Nothing has been written to `PLAN.md`, the ledger, any completion record, the
   checklist, `contracts/`, any spec, any plan, or any manifest. The pilot tree at `9d68291` is untouched
   (verified clean before and after).
3. **It does not advance any gate's stage credit — not one.** A5 stays `integrated` (70 %) on the roadmap
   with its honest `inconclusive` disposition; A6 stays `implemented` (35 %); A9 stays `spec` (10 %).
   *Removing a scheduling blocker is not evidence of any gate's work.* The amendment moves **zero** credit.
   (`status/status.json` → `roadmap.gates`; `PLAN.md:165`.)
4. **It does not promote A5, and creates no path to promotion by itself.** `promotion_authorized` stays
   `false`; A5 stays `design_only`.
5. **It does not authorise any A5 run** — not a scored cycle, not a functional requalification, not a new
   protocol, not a browser pass. Options (a) and (b) in `02-OPTIONS.md` are *conditions*, and both need
   their own grant. Note in particular that the 2026-09-07 owner decision left
   `additional_downstream_functional_browser_execution_authorized: false` (`ledger:5466`) — this draft does
   **not** flip that.
6. **It does not authorise A6 integration, A6 physical qualification, or A6 sealing.** It removes a
   blocker; the fan-in still needs the accepted candidate, the controlled integration checks and the
   applicable post-integration verification, and commit/push remain owner-gated.
7. **It does not establish that A6 integrates cleanly on the current head.** The review's "clean textual
   merge" was an observation bound to a specific candidate/parent pair, not a merge result on `9d68291`.
   No merge was performed while preparing this draft.
8. **It does not establish that the current HEAD is green.** The green baseline run cited
   (`status/decisions.log:11`, on `39f631e`) predates the A13 lane fan-in and later commits. `P19` is a
   *condition to be re-earned*, and this draft re-ran nothing.
9. **It does not validate, re-open, relabel or re-score V23 or Stage B**, and it does not resolve
   `identity_drift_resolved: false` (`ledger:5690` region).
10. **It does not adjudicate A9.** It makes `P09` *adjudicable*; the adjudication itself is `P23`'s work
    and has not been performed here. The review's B1/B2 RED→GREEN reproduction is a review observation,
    never full acceptance.
11. **It does not resolve the review's other stale edges.** `P14 ← P13` (`ledger:4500-4501`) and
    `P15 ← P07` (`ledger:4556-4557`) are out of this amendment's scope; they are handled in the review's
    D6/D7 and are deliberately left alone so this change can be approved or refused cleanly on its own.
12. **It does not bind `P03`'s remaining content.** The A5 downstream closeout stays a real, open,
    owner-decidable item; the amendment only stops it from gating other work.
13. **It does not touch the ledger file itself.** That file is outside the orchestrator's write boundary
    and append-only; the events in §8.3 must be appended by whoever holds that authority, under owner
    authorisation.
14. **It makes no calendar or effort claim.** The ledger records `calendar_estimate: null`
    (`ledger:5106`) and explicitly low-confidence effort ranges; nothing here changes that.
15. **It does not establish that P19 and P23 are the *complete* dependency set** for the re-pointed rows.
    They are the two the owner's instruction and the review name; the owner may name more, and §8.3 is
    written so an additional edge can be added without re-approving this text.
16. **It does not establish that a functional requalification is currently *feasible*.** That route has a
    history: the programme ran an "A5 integration requalification" cycle repeatedly and **every attempt
    failed closed or was consumed** — `gate_state_timeout` with `64` nonterminal records retaining `128`
    placement reservations (*"a product cleanup-invariant failure, not an external network failure. The exact
    source-level root cause is not yet established"*, `A5_INTEGRATION_REQUALIFICATION_V16_BLOCKED_HANDOVER_2026-09-03.md:41,50-52`),
    `route_not_alive` / `route_peer_process_lost` (`…V15_BLOCKED_2026-09-03.md:13-19`), and a terminal
    failed closure dated 2026-09-02 (`A5_INTEGRATION_REQUALIFICATION_BLOCKED_2026-09-02.md:1-20`). §7 of
    this file registers Option (a) as an *admissible purpose*, not as a runnable plan. See `02-OPTIONS.md`
    §1.5.
17. **It does not "fix" the A5 record's `closing_commit`.** Item C4 is **withdrawn**: the sentinel is
    deliberate and a standing written instruction forbids replacing it. What the record *does* require —
    verification that the standalone A5 commit contains the exact record bytes — has been performed
    read-only and **passes** (§5). The review's F10 and the earlier draft's proposed `b9001e6` are both
    withdrawn on that authority.
18. **It does not claim that its own citations are complete.** `04-EVIDENCE-AND-GAPS.md` lists, by name,
    every gap I could not close — including the citation-line discrepancy in the review, the ledger
    snapshot's staleness relative to later decisions, and the artefacts I read about but did not hash.

---

## 11. Approval block (owner)

Tick, strike, or ignore — each line stands alone. **Not deciding is a real option with a real cost**: with
nothing approved, A6 source integration keeps waiting for a "fresh valid non-promoting A5 disposition"
and `P09` keeps waiting on `P07`. That is the current state.

| item | decision | date | notes |
|---|---|---|---|
| C0 register `P19` + `P23` rows | ☐ approve ☐ refuse | | required before C1/C2 can point anywhere |
| C1 decouple A6 source integration from A5 | ☐ approve ☐ refuse | | leaves A6 *physical* serialised and unauthorised |
| C2 re-point `P09` to adjudication + green baseline | ☐ approve ☐ refuse | | leaves A9 physical and acceptance untouched |
| C3 record A5's disposition note | ☐ approve ☐ refuse | | changes no threshold, no protocol, no state |
| C4 assign `closing_commit` | **WITHDRAWN — recommended REFUSE** (standing instruction: do not replace the sentinel; the required verification is discharged and passes, §5) | — | — |
