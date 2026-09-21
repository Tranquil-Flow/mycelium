# A5 amendment draft — index

> ## DRAFT — NOT APPROVED — NOT APPLIED
>
> This directory contains an **owner-facing plan change** written as paper. **Nothing here has been applied.**
> No canonical source, record, ledger, checklist, contract, manifest or plan was modified. The pilot tree
> `/Users/evinova-self/Documents/playground/mycelium-core-pilot` was read-only throughout.
>
> **The owner approves plan changes.** This is a proposal addressed to the owner. If the owner does nothing,
> nothing changes — and that outcome is stated explicitly in `01-AMENDMENT.md` §11.

**Amendment id:** `a5-amendment-2026-09-21-decouple-a6-repoint-p09`
**Drafted:** 2026-09-21, by a bounded subagent, in the lane worktree
`/Users/evinova-self/Documents/playground/mycelium-a5-amendment-lane` at commit
`9d6829140921b806ee4b3d3e0e259657f7792615` (detached; created for this purpose only).
**Not reviewed by anyone else** — see gap **G12**.

---

## What is here

| file | what it is | read it if you want… |
|---|---|---|
| **`01-AMENDMENT.md`** | the amendment itself: the operative changes **C0–C4**, the exact documents/records it would touch with anchors, the verbatim insertable texts, the **invariants that must not change**, and an explicit list of **what the draft does NOT establish** | to decide. Start here. |
| **`02-OPTIONS.md`** | the **two drafted options** for any future A5 run — (a) a functional non-promoting requalification bound to an immutable commit, and (b) a new performance protocol motivated by a declared architecture or variance change — with the recommendation and its trade-offs | to decide *what a future A5 run would even be*. |
| **`03-EDGES-BEFORE-AFTER.md`** | the **before/after dependency edges**, every current edge quoted verbatim with `file:line` | to check the graph claims. |
| **`04-EVIDENCE-AND-GAPS.md`** | the full citation index (102 entries) and **every gap I could not close** | to falsify this draft. |

## The change in one paragraph

Gate A5 is terminal and fully executed but **honestly not promoted** — all 13 closure requirements are bound
to evidence and the frozen benchmark still concluded `inconclusive` (+16.71 % point estimate, paired 95 %
lower bound −0.4197 against the 10 % threshold, `promotion_authorized: false`). That result is left exactly
as it is. The defect this draft addresses is **structural**: the execution DAG still routes unrelated work
*through* that deferred cycle — `P05` (A6 **source integration**) depends on `P03` (A5's downstream closeout)
at `ledger:4030-4031`, and `P09` (A9 reconcile) depends on `P07`, which depends on A6 *physical*
(`ledger:4238-4239`, `:4137-4139`). The amendment removes those two edges **prospectively**: A6 source
integration no longer waits on any A5 disposition, and `P09` re-points to A9 independent adjudication plus a
green baseline. The ledger is append-only, so the change is expressed as **additive coordination events**,
never as row edits. A5's thresholds, protocol, state, history and default-off replicas are untouched.

## The three things worth knowing before reading further

1. **The invariants are load-bearing.** Replicas stay **disabled by default**; the **10 % threshold is not
   relaxed**; **no existing record is rewritten** (the amendment changes the future, never the past); no
   identical scored retry; no push, no PR, no sealing, no stage credit. `01-AMENDMENT.md` §9.
2. **Neither option is ready to run, and the draft says so.** Option (b)'s motivating change does not exist
   yet (the one candidate — A9's component-aware instrumentation — is not integrated; `ba6abbe` is not an
   ancestor of HEAD, verified). Option (a) is admissible in principle but **the same run shape already failed
   closed 21 times on this fleet**, with the `gate_state_timeout` cleanup invariant's root cause still
   unlocalised — so (a) carries a **local RED→GREEN pre-condition** before any window is opened.
   `02-OPTIONS.md` §1.6.
3. **One item was withdrawn on authority, and the withdrawal is the most useful part of the draft.** The
   independent review (F10) and the earlier draft in `runs/a5-disposition/` both wanted A5's
   `closing_commit` filled in. It must **not** be: the programme's own integration goal states the sentinel
   is *intentional* — *"This avoids an impossible self-referential commit SHA. … **Do not replace the sentinel
   with a guessed or circular SHA**"* (`A5_INTEGRATION_REQUALIFICATION_GOAL_2026-08-30.md:124-130`). The
   verification that instruction *does* require — that the standalone A5 commit contains the exact record
   bytes — was performed read-only and **passes**. `01-AMENDMENT.md` §5.

## Relationship to the earlier draft

`runs/a5-disposition/2026-09-20-a5-prospective-amendment-DRAFT.md` already exists and covers overlapping
ground (its Q1–Q3 map to this draft's C1–C3). This draft **supersedes nothing** — it is a separate,
independent draft that: adds the before/after edge quotes at `file:line`; drafts both future-purpose options
in full rather than naming them; adds the invariants section and the non-establishment list; and **withdraws**
that draft's Q4 (`closing_commit` = `b9001e6`) on the authority quoted above. The two drafts disagree on
exactly two points, both stated in the open:

- **the `closing_commit` item** — that draft proposes `b9001e6`; this one withdraws the item entirely
  (`01-AMENDMENT.md` §5);
- **the width of the `P05` decoupling** — that draft keeps a *"fresh valid non-promoting A5 disposition"*
  condition (following the review's D3 text); this one removes the A5 condition **entirely**, per the task's
  instruction that A6 source integration proceed *independently of any A5 disposition* (`01-AMENDMENT.md`
  §2, `02-OPTIONS.md` §4). **The owner should choose between these two widths deliberately** — the wider
  decoupling is the one the program plan already states in words (`PLAN.md:32`, D2).

## Provenance of the drafting work

- source tree read: pilot repository at `9d68291`, read-only;
- any file kept in the drafting worktree is committed **locally only** — no push, no PR, explicit paths,
  no co-author or bot trailers;
- the ledger, the Desktop owner-decision file, the MASTER_PLAN and the requalification handovers live
  **outside** any git repository and were read but never written;
- no test suite, benchmark or physical run was executed while preparing this draft. Every "green" statement
  in it is a citation of a record, never a fresh measurement. See `04-EVIDENCE-AND-GAPS.md` §2 and **G4**.
