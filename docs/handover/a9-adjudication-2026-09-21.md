# Gate A9 adjudication at `349845b` — independent lane

**Date:** 2026-09-21 (UTC times stated inline).
**Adjudicating lane:** `deleg_8d41a96b`, worktree `/Users/evinova-self/Documents/playground/mycelium-a9-adjudication-lane`,
local branch `adjudication/a9-2026-09-21` whose parent is `349845ba646c75aefbcee2309a123c454de016ba`
(`integration/core-pilot`); all git reads against the pilot tree at that commit, which stayed clean throughout.
**Deliverable root:** `/Users/evinova-self/Documents/mycelium-core-program/runs/a9-adjudication/`
(this file plus `evidence-2026-09-21/`). Nothing in this directory was overwritten or deleted: the
2026-09-16 tranche-1/2 evidence (`ADJUDICATION.md`, `table.json`, `candidates.json`, …) is untouched.

**Scope discipline.** READ-ONLY on the pilot tree: every git read there used `--no-optional-locks`; no checkout,
reset, stash, branch, commit, merge, rebase, cherry-pick, worktree-add-in-pilot, or push ran **in the pilot tree
or against its refs** — its `HEAD` is still exactly `349845ba646c75aefbcee2309a123c454de016ba`. No gate
state, register, checklist, contract, manifest or record was edited; nothing was re-credited or promoted.
The only git writes anywhere were inside this lane's own worktree: one local commit on the local branch
`adjudication/a9-2026-09-21`, no push, no PR, no co-author or bot trailer.
`tests/a13_macos` and the demo suites were NOT run (authoritative A13 surface, owned elsewhere on this host).
`/Users/evinova-self/mycelium-wp8-02` was not touched; no preview was built or deployed; no round was launched.
This adjudication did NOT integrate anything: A9's cited bytes remain exactly as unintegrated as they were.

---

## 0. Headline

1. **The demotion's load-bearing fact reproduces, and is stronger than it was stated.**
   A9's cited candidate `ba6abbe` changes **209 paths** relative to its own merge base `abe291c`
   (157 added, 52 modified; `+27,506 / −592`). At HEAD `349845b`: **0 of the 209 paths carry the candidate's
   blob**, 157 do not exist at all, 52 exist with different content — and **for none of the 209 has the
   candidate's blob ever appeared anywhere in core's history** (not at HEAD, not replaced, not transiently).
   **0 of the 27 lane commits are ancestors of HEAD**, and **0 of the 27 are patch-identical to any commit in
   core's history**. Zero v2 protocol strings exist anywhere in the integrated tree outside `docs/`. So
   "209/209 paths unintegrated" is **confirmed by independent re-derivation at a head later than the one the
   demotion was measured at**.

2. **The demotion's *count* premise does NOT reproduce — and the number is a moving target.**
   `candidates_found = 101` was measured at `ba3595b` against the 2026-09-16 ref set. The same enumeration
   rule at `349845b` against the 2026-09-21 ref set gives **124** (124 unreachable across refs + 0 dangling
   doc-named + 2 already-integrated review-cited; 125 if the tranche-1 neighbour rule that also counts
   `f35ab91` is adopted). Within this lane's own three-minute measurement window the unreachable sub-count
   moved **122 → 123 → 124**, because a sibling lane (`refs/heads/policy/red-suite-collection`, committed
   00:25:50Z) was landing commits concurrently. The sub-count **97 is not reproducible as a rule**; it is
   reproducible only as a historical measurement.

3. **The demotion's prose overreaches in one sentence, and the disposition is nevertheless correct.**
   The register's stage vocabulary separates `implemented` (a lane-level stage) from `integrated` (on the
   branch), and the ledger row cited as A9's claim says in its own words "**not integrated** (not an ancestor
   of HEAD)". So "the 'implemented' stage was never supported by the integrated branch" is only true under the
   reading that a *verified* stage must be verifiable from the integrated tree. Under that reading the
   demotion is right and `verified_stage: spec` is the honest stage; under the looser reading A9's
   `implemented` tag was a correctly-labelled unintegrated candidate. Either way **nothing here re-credits
   A9**, and nothing here supports promotion.

4. **Exactly one A9 claim verifies at HEAD, and it is the smallest one.** `A9:specification` is supported
   byte-exactly: the checklist binding `sha256:65f2ebc829d64dfb3a466a5321be097a2078403f9077235c74bdcd08679e1d8e`
   is the sha256 of the spec file at HEAD (computed). The integrated A9 acceptance inventory is
   `state: design_only` and says in its own claim boundary that it contains "no schema, implementation,
   migration or benchmark execution, device observation, evidence, readiness, or completion claim".

**Name-collision warning (unchanged from the record).** `docs/handover/MVP_COMPLETION_PLAN.md:98` uses "A9" for a
*different, older* item — "A9 — Task 3B.3 offline half", the cycle-search audit. The A9 adjudicated here is
"Platform-neutral / runtime efficiency" (checklist `gate_id: A9`, `docs/handover/mycelium-completion-checklist.v1.json:1309`).

---

## 1. What A9 claims, and what evidence each claim cites

Five records speak for A9. Quoted with `file:line`.

### 1.1 The specification — `docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md`

- `:3` — `**Status:** \`design_only\`; dependency-ready contract and migration boundary`
- `:382-389` — the acceptance inventory's own claim boundary: *"Its protocol is
  `mycelium.a9_acceptance_inventory.v1`; its claim boundary is frozen design and future acceptance input only.
  It contains no schema, implementation, migration or benchmark execution, device observation, evidence,
  readiness, or completion claim."*
- `:478-494` — §11 Completion, the closure criteria (paraphrased faithfully): an executed
  `mycelium.membership_contract_migration.v1` artifact; A8 physically closed; the one-time v2 migration
  complete; the migration-only reader removed; macOS/Linux/Windows continuity and unearned-eligibility
  negatives passing physically; the accelerated Windows provider and the exact Qwen2.5-0.5B route passing the
  no-rematerialization, activation-codec, component-cost, planner, single-request-performance, cleanup,
  browser and negative gates; all eight workspaces passing live verification; full contracts, privacy,
  governance, security, Python and frontend suites green; **"and one atomic A9 feature commit exists"**.
  The spec closes: *"Until then this document remains `design_only`."*

**The specification itself claims no implementation.** It is a design boundary by its own first line and last.

### 1.2 The in-repo completion record — `docs/handover/mycelium-completion-checklist.v1.json:1308-1372`

This is A9's completion record in the repository (there is **no** `docs/handover/a9-completion-record.v1.json`;
only `a3`, `a4`, `a5`, `a8` exist).

- `:1315` — `"state": "design_only"`
- `:1322-1324` — `"completed_requirements": ["specification"]` — **one** requirement completed.
- `:1326-1333` — exactly one evidence binding: `requirement: "specification"`,
  `artifact_reference: docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md`,
  `artifact_digest: sha256:65f2ebc829d64dfb3a466a5321be097a2078403f9077235c74bdcd08679e1d8e`,
  `provenance: "source"`, `subject: "A9:specification"`, `observed_at: "2026-08-18T00:00:00Z"`,
  and every cross-binding (`contract_digest`, `model_digest`, `representation_digest`, `runtime_digest`,
  `environment_digest`, `authority_generation`) is `null`.
- `:1347-1360` — `pending_requirements` (12): `implementation`, `product_integration`,
  `deterministic_positive`, `deterministic_negative`, `physical_positive`, `physical_negative`,
  `ui_live_all_eight`, `browser_navigation_reconnect`, `browser_second_session`, `full_regressions_audits`,
  `architecture_handover`, `atomic_feature_commit`.
- `:1316-1321` — blockers: membership migration test window; native accelerated Windows x86_64 provider and
  device; qualified efficient int8 kernels and reduced-precision activation codec; frozen component-aware
  single-request benchmark.

**The repository's own completion record claims `design_only` + `specification`, and lists implementation as
PENDING.** It is not a record of an implemented gate.

### 1.3 The programme register — `/Users/evinova-self/Documents/mycelium-core-program/status/status.json:601-611`

- `:608` — `"verified_stage": "spec"` (post-demotion value; decisions.log `:52` records the change from
  `implemented`)
- `:609` — `"verifier": "orchestrator vs runs/a9-adjudication (object-level, spot-verified)"`
- `:610` — `"evidence": "runs/a9-adjudication/ADJUDICATION.md + table.json (independent, 101 candidates). The
  cited evidence ba6abbe adjudicates **post-pilot**: 27 commits, 209/209 paths unintegrated, 15 paths also
  changed in core since abe291c. So 'implemented' is a REPORTED claim whose evidence is not in the integrated
  branch - demoted to spec pending the owner naming A9's real evidence (decisions.log 2026-09-16)."`
- `:611` — `"reported_stage": "implemented"` — the lane's claim is *preserved*, not deleted.

### 1.4 The claim's own evidence basis — the 2026-09-15 review

`/Users/evinova-self/Documents/mycelium-core-plan-review-20260915/PROGRESS_MATRIX.md:33` (A9 row), which the
register's `verifier` line treats as the "full adjudication pending":

> ✔ | ✔ membership v2, native CPU provider, codecs, signed observations, planner | **committed** `ba6abbe`
> (27 commits, 209 files) | ✖; textual conflict manifest only **[F13]**; 6 semantic overlaps with
> core-since-base and 6 with A13 **[F14]** | ✔ **[F]** B1/B2 RED on parent (2 fail), GREEN on candidate
> (75 pass). Retained host suite 1,755 passed / 14 skipped **[R]**; intermittent identity observation
> unlocalised | ✖ | ✖ no Windows x86_64 | ✖ (frozen 2×/8 tok/s/1,500 ms floors unmeasured) | ✖ | **High**
> for committed state; full independent adjudication pending

and its own supporting checks, `FRESH_CHECKS.md:27-28`:

> `F13` … core+A9: conflict `contracts/contract-manifest.v1.json` only …
> `F14` … A9: 209 files, +27,506/−592 (base `abe291c`) …

Row-level context: `PROGRESS_MATRIX.md:45` "Committed but unintegrated candidate: 3 (A6, A9, A14a)";
`:48` "Performance-promotion gates promoted: 0 of 4 (A5, A9, A10, A11)";
`:68` P09 — "Committed `ba6abbe`; B1/B2 independently red/green **[F]**; full adjudication pending";
`:69` P10 — "No Windows x86_64 / Linux / Mac matrix evidence".

### 1.5 The ledger's P09 row — `runs/wp9-1-draft/proposal/remaining-work-ledger-P05-P15-truth-proposal.md:28`

The source of the `implemented` tag:

> **Committed candidate** `ba6abbe` (27 commits, 209 files) with its own evidence: B1/B2 RED on parent →
> GREEN on candidate (75 passed), retained host suite 1,755 passed / 14 skipped. Full independent adjudication
> **pending**; **not integrated (not an ancestor of HEAD)**; no Windows x86_64. | **implemented** (35%);
> adjudication pending

### 1.6 The demotion — `/Users/evinova-self/Documents/mycelium-core-program/status/decisions.log:51-52`

- `:51` — "…**candidates_found=101** (97 commits unreachable from integration/core-pilot across refs + 1
  dangling doc-named commit + 3 already-integrated review-cited commits) … Spot-verification I ran myself:
  a65fd54/8141289/f35ab91 are ancestors of ba3595b (duplicate verdicts confirmed) and
  `git rev-list --all --not ba3595b` = 97 … A9's own status line is updated: its recorded evidence ba6abbe is
  post-pilot with **209/209 paths unintegrated** …"
- `:52` — "A9 roadmap gate DEMOTED on evidence (reversible): verified_stage implemented -> spec, reported_stage
  left at implemented. Reason: the adjudication shows A9's cited evidence ba6abbe is post-pilot with 209/209
  paths unintegrated, so the 'implemented' stage was never supported by the integrated branch …"

### 1.7 Prior adjudication at `7d0ffb6` — a *prior record*, not my measurement

`status/decisions.log:6884` records that a previous lane reached the same structural conclusion on 2026-09-20
and committed it as `07ea9ee` on `adjudication/a9-2026-09-20`
(`docs/handover/a9-adjudication-2026-09-20.md`, 416 lines). This report does **not** inherit its numbers; every
number below was re-measured by this lane at `349845b`. Where the two agree, that is independent corroboration;
where they differ, §2.3 says so.

---

## 2. Re-measurement of the demotion's premise

**Method.** Enumeration rules and the per-path probe were re-implemented from the packet's description rather
than replayed from the tranche-1 scripts; the tranche-1 JSON artifacts were not read to produce any number
below. Scripts: `scripts/adjudication/remeasure_a9_premise.py`, `scripts/adjudication/blob_history_check.py`
(kept in this lane's worktree). Raw output: `evidence-2026-09-21/remeasure-2026-09-21.json`,
`evidence-2026-09-21/blob-history-2026-09-21.json`.

### 2.1 Part A — "cited evidence `ba6abbe` has 209/209 paths unintegrated": **REPRODUCES**

Commands and raw results (all at `349845b`, 2026-09-21T00:26-00:30Z):

```
$ git -C <pilot> --no-optional-locks merge-base integration/core-pilot ba6abbe
abe291c3ae856bef60394f528f1f86bddf78b2ad          # matches the base the record names

$ git --no-optional-locks rev-list --count abe291c..ba6abbe
27                                                 # 27 commits, matches

$ git --no-optional-locks diff --shortstat abe291c ba6abbe
 209 files changed, 27506 insertions(+), 592 deletions(-)   # matches F14's "209 files, +27,506/−592"

$ git --no-optional-locks diff --name-status --no-renames abe291c ba6abbe | awk '{print $1}' | sort | uniq -c
 157 A
  52 M
```

Per-path probe (this lane's own loop over all 209 paths, comparing the candidate's blob against the blob at
core HEAD **and against every blob core ever had for that same path**):

| state | count |
|---|---|
| identical at core HEAD (content is integrated) | **0** |
| core once had this blob, then replaced it | **0** |
| present at HEAD but never the candidate's blob (divergent) | **52** |
| absent at HEAD and never in that path's history | **157** |

Commit-level probes:

```
$ git merge-base --is-ancestor <each of the 27 commits> integration/core-pilot   # 27/27
NOT ancestors                                                 # 0 of 27 integrated
$ git patch-id --stable over each lane commit vs over all core commits          # 484 core commits indexed
0 lane commits have a patch-id present in core's history
```

Named sample paths, checked one by one rather than trusted from a count (all four named by the prior lane
plus the lane's own spec rewrite and the contract manifest):

| path | at HEAD | in lane tip | integrated? |
|---|---|---|---|
| `mycelium_membership/v2.py` | absent | present | no |
| `mycelium_membership/capability_v2.py` | absent | present | no |
| `mycelium_membership/migration.py` | absent | present | no |
| `mycelium_router/native_cpu_provider.py` | absent | present | no |
| `docs/superpowers/specs/mycelium-membership-control-v2.md` | absent | present | no |
| `contracts/contract-manifest.v1.json` | present | present (different) | no — divergent |
| `mycelium_a13_macos/peer.py` | present | (not in lane's set) | n/a — lane does not touch it |

Surface absence confirmed at HEAD: `mycelium_membership/` contains only `__init__.py` + `contracts.py` (v1);
the Mac consumer `mycelium_a13_macos/peer.py:21` imports `CAPABILITY_REPORT_PROTOCOL`, whose value at
`mycelium_membership/contracts.py:20` is `"mycelium.membership.capability_report.v1"` — the **v1** protocol.
A grep for the nine v2/runtime-efficiency protocol strings (`mycelium.membership.peer_profile.v2`,
`…capability_report.v2`, `mycelium.peer_class_qualification.v1`, `mycelium.peer_eligibility.v1`,
`mycelium.runtime_acceleration_profile.v1`, `mycelium.stage_performance_observation.v1`,
`mycelium.activation_codec_qualification.v1`, `mycelium.runtime_efficiency_qualification.v1`,
`mycelium.membership_contract_migration.v1`) across `*.py *.json *.ts *.tsx` **outside `docs/` and outside
`tests/a9_acceptance/` returns nothing.** (The design-only inventory names some of them; the inventory is
excluded here because it is the spec's own design input, not an implementation.)

**Verdict on Part A: reproduces exactly, at a later head, and the "unintegrated" is stronger than a HEAD
comparison — no candidate blob for any of the 209 paths has ever existed in core's history.**

*What is integrated under the A9 name (so the finding is not over-read):* the A9-labelled branches
`9bbd1ec`, `efcdf44`, `1fd3a6f` **are** ancestors of HEAD (verified individually), and
`tests/a9_acceptance/` is integrated but was authored by A4's commits `487a1b4` and `e61e780`
(`git log --follow` on `tests/a9_acceptance/inventory.v1.json`). "A9 has zero integrated work" is too coarse;
"A9's *cited evidence* has zero integrated work" is exactly right.

### 2.2 Part B — "candidate count 101": **DOES NOT REPRODUCE**

```
$ git --no-optional-locks rev-list --all --not integration/core-pilot --count
124                        # measured 2026-09-21T00:28:43Z and again at 00:30:59Z
$ git --no-optional-locks for-each-ref | wc -l
86 refs total; 54 of them not ancestors of integration/core-pilot at 00:28Z, 55 at 00:30:59Z
$ git --no-optional-locks rev-list --count integration/core-pilot
484                        # the core history the patch-id index was built from
```

Reproducing the tranche-1 rule (`unreachable-across-refs ∪ doc-named ∪ review-cited`):

| component | demotion (at `ba3595b`, 2026-09-16 refs) | this lane (at `349845b`, 2026-09-21 refs) |
|---|---|---|
| commits unreachable across refs | 97 | **124** |
| dangling doc-named commits | 1 (`2cabd0ee`) | **0** — `2cabd0ee` no longer resolves as a commit object (pruned) |
| already-integrated review-cited commits | 3 (`f35ab91`, `a65fd54`, `8141289`) | **2** (`a65fd54`, `8141289`) by this packet's 6-hash rule; **3** if the tranche-1 neighbour rule that adds `f35ab91` is used |
| **union (candidates_found)** | **101** | **124** (125 under the neighbour rule) |

Why it moved, measured rather than asserted:

- **27 commits of the unreachable set are dated after 2026-09-16** (`git log --no-walk --format=%cd` over the
  unreachable set at 00:30:59Z: 27 rows fall in 2026-09-20/2026-09-21) — i.e. lanes created since the demotion
  (a7/a10/a11/a12/a14b scaffolds, `lane/pyz-ledger-coverage`, `r1/*`, `wp/p83-chain`,
  `policy/red-suite-collection`, …). Per-ref contribution at 00:28:46Z includes
  `work/mycelium-session8b-5_ry7xyo` = 27 (the A9 lane itself), `codex/astra-acceptance-hardened-review` = 18,
  `codex/a4-implementation-packet-v2-final` = 16, and 51 further non-ancestor refs.
- **The count is not stable even inside one lane's window.** My first enumeration read **122**; a re-read
  minutes later read **123**; the final read is **124**. The driver is visible: `refs/heads/policy/red-suite-collection`
  was created/updated at `2026-09-21 02:25:50 +0200` (= 00:25:50Z) by a sibling lane that is landing commits
  tonight. I have recorded the full ref set as it stood (`evidence-2026-09-21/refs-2026-09-21.txt`,
  sha256 `dc37e8e539ef20a19c3f0944ab46cb38ad54b2412acd1b40174c9e728ed7538b`) so the number can be attributed.

**Verdict on Part B: the "101" does not reproduce as a current number.** It was a correct measurement of a
different, earlier state. It cannot be carried forward as if it described today, and `git rev-list --all --not
<core>` is a *different metric* from `candidates_found` — the prior lane correctly refused to use it as
corroboration, and this lane does too.

### 2.3 The demotion's third number — "15 paths also changed in core since the base": **age-stale; it is 18**

```
$ git --no-optional-locks diff --name-only --no-renames abe291c integration/core-pilot | wc -l
$ git --no-optional-locks rev-list --count abe291c..integration/core-pilot
$ (intersection with the lane's 209 paths) -> 18
```

18, not 15. (The prior lane measured 18 at `7d0ffb6`; it is still 18 at `349845b`, though core has moved
further.) **This is a shared-path count, not a conflict census** — no `merge-tree`/cherry-pick was run for A9
by this lane, and this number must not be quoted as "18 conflicts".

### 2.4 Verdict on the premise

| premise element | verdict |
|---|---|
| "cited evidence `ba6abbe` has 209/209 paths unintegrated" | **REPRODUCES** — and strengthened (no blob ever in core history for any of the 209) |
| "candidate count was 101" | **DOES NOT REPRODUCE** — 124 at `349845b`/2026-09-21 refs; 122→123→124 within the measurement window |
| "15 paths also changed in core since the base" | **age-stale** — 18 |

**The demotion still stands on its load-bearing fact.** The count it also quoted is a snapshot of a different
state, and a reader today must not treat 101 as current. Neither correction re-credits A9.

---

## 3. Claim-by-claim support status

| # | claim (source) | evidence it cites | what this adjudication measured | status |
|---|---|---|---|---|
| 1 | `A9:specification` completed (`checklist:1322-1333`) | the spec file, digest `sha256:65f2ebc8…` | sha256 of the spec at HEAD = `65f2ebc829d64dfb3a466a5321be097a2078403f9077235c74bdcd08679e1d8e` — **byte-exact match** to the binding; `tests/a9_acceptance` (the spec's enforcement tests) pass | **SUPPORTED** |
| 2 | A9 `state: design_only` (`checklist:1315`; arch doc `CURRENT_AND_PLANNED_ARCHITECTURE.md:526`; spec `:3`) | the spec + inventory | inventory `protocol: mycelium.a9_acceptance_inventory.v1`, `state: design_only`, claim boundary excludes implementation/evidence/completion; its tests pass | **SUPPORTED** |
| 3 | A9 implementation exists — "membership v2, native CPU provider, codecs, signed observations, planner" (`PROGRESS_MATRIX.md:33`) | committed candidate `ba6abbe`, 27 commits / 209 files | the candidate exists as objects and is unchanged: 209 paths, `+27,506/−592`, 27-commit linear chain off `abe291c`. **On the integrated branch it does not exist**: 0/209 blobs at HEAD or ever in history, 0/27 ancestors, 0/27 patch-id matches, 0 v2 protocol strings outside `docs/` | **SUPPORTED ONLY AS AN UNINTEGRATED CANDIDATE** — it is a true statement about the lane and a false one about the branch |
| 4 | "B1/B2 RED on parent (2 fail), GREEN on candidate (75 pass)" (`PROGRESS_MATRIX.md:33`, `[F]`) | the reviewer's own fresh execution on 2026-09-15 | **not re-measured by this lane.** Re-running it means running the lane's own unintegrated tree, which this adjudication did not do (read-only, no integration, no other lane's tree) | **MERELY UNMEASURED HERE** (and, even if reproduced, it would qualify the lane, not the branch) |
| 5 | "Retained host suite 1,755 passed / 14 skipped" (`PROGRESS_MATRIX.md:33`, `[R]`; ledger `:28`) | retained record, never re-executed even by the review | same as #4: not re-measured here; the objects cannot establish runtime quality of an unintegrated tree | **MERELY UNMEASURED HERE** |
| 6 | Integration of A9 (`PROGRESS_MATRIX.md:33` marks it ✖; ledger `:28` states "not integrated") | the row itself | measured: `git merge-base --is-ancestor ba6abbe HEAD` → false; all 209 paths unintegrated | **SUPPORTED (as a negative) — the records are honest here** |
| 7 | Deterministic / physical / UI / performance closure (`spec:478-494`; `PROGRESS_MATRIX.md:33` ✖ columns) | nothing | nothing found; `checklist:1347-1360` lists all 12 requirements pending, `:1316-1321` lists 4 blockers; no Windows x86_64 anywhere | **UNSUPPORTED / UNMET** |
| 8 | "one atomic A9 feature commit exists" (`spec:493`) | nothing | 0 of the 27 commits are ancestors of HEAD | **UNMET** |
| 9 | Register `reported_stage: implemented` (`status.json:611`) | preserved by the demotion as the lane's claim | the lane's claim is correctly carried as *reported*; the *verified* stage is `spec` | **CORRECTLY LABELLED AS REPORTED-ONLY** |

**On the demotion's reasoning, said plainly.** The demotion's load-bearing fact holds (#3, #6). Its sentence
"the 'implemented' stage was never supported by the integrated branch" reads more broadly than the artifacts:
the register's own vocabulary separates `implemented` from `integrated`, and ledger `:28` already said
"not integrated (not an ancestor of HEAD)". The *disposition* — `verified_stage: spec` with
`reported_stage: implemented` left in place, and no promotion — is the honest reading of the integrated branch
and is **not** disturbed by anything measured here.

---

## 4. GREEN baseline from real suite runs

Run in **this lane's worktree** at `349845ba646c75aefbcee2309a123c454de016ba` (clean), one suite at a time —
never in parallel, because sibling lanes on this host have caused port conflicts and stalls. Interpreter and
runner:

```
$ env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
    /opt/homebrew/bin/python3 -B -m pytest -q -p no:cacheprovider <suite>
Python 3.14.4 (main, Apr  7 2026) / pytest 9.0.2
```

Runner: `scripts/adjudication/run_baseline_suites.sh`; raw log:
`evidence-2026-09-21/suites/baseline-2026-09-21.txt` (every line below is copied from it).

| # | invocation | pytest's own last line | exit |
|---|---|---|---|
| 1 | `tests/a9_acceptance` | `4 passed in 0.03s` | 0 |
| 2 | `tests/membership` | `46 passed in 0.18s` | 0 |
| 3 | `tests/conformance` | `35 passed in 1.69s` | 0 |
| 4 | `tests/iroh_conformance tests/request_conformance` | `127 passed in 25.61s` | 0 |
| 5 | `tests/node_agent` | `150 passed, 1 skipped in 77.78s (0:01:17)` | 0 |
| 6 | `tests/contracts` | `29 passed in 2.40s` | 0 |
| 7 | `tests/product_spine` | `27 passed in 0.19s` | 0 |
| 8 | `tests/governance` | `20 passed in 3.01s` | 0 |
| 9 | `tests/security` | `1 failed, 9 passed in 3.05s` | **1** |
| 10 | `tests/seed_coordinator` (whole directory, **no deselect**) | `398 passed in 13.03s` | 0 |
| 11 | `tests/lane_audit` | `9 passed in 2.02s` | 0 |

**Totals: 854 passed / 1 skipped / 1 failed across 11 invocations; 10 of 11 exit 0.**

The single failure is `tests/security/test_release_security_audit.py::test_current_checkout_and_runbook_preserve_claim_boundary`,
and it is **a known, pre-existing false positive documented by the programme itself** —
`status/decisions.log:6123-6128`: *"finding secret_cli_argument at mycelium_testbed/emulation.py:4020 (subject
--token-steps). The audit walks EVERY tracked .py add_argument call, so this predates the edit that moved the
line; it is a heuristic false positive on the word "token" (LLM tokens, not a credential) and it fails at
baseline for the same reason. NOT silenced here…"*. Verified intrinsic rather than caused by this lane:
`mycelium_testbed/emulation.py` is unmodified vs HEAD (`git diff --stat HEAD -- …` empty), this worktree's only
untracked path is `scripts/adjudication/`, and the failure reproduces when the test is run alone.

### 4.1 Honest caveats about this baseline

- **One of my own instrument labels was wrong, and I am recording it rather than hiding it.** My runner labelled
  invocation #10 "seed_coordinator_deselected" and passed
  `--deselect tests/seed_coordinator/test_local_two_node_native_process_e2e.py::…`. That node id does **not**
  exist — the test lives in `tests/seed_coordinator/test_local_two_node.py::test_local_two_node_native_process_e2e`
  (collection confirms the file). pytest therefore ignored the deselect and ran **all 398** tests, which all
  passed in 13.03 s. **The consequence is a stronger result than the prior record's**: the directory that
  stalled for 11 minutes at `7d0ffb6` on 2026-09-20 (`decisions.log:6884`) completed here, whole and green, with
  no deselect applied. Confirmed twice more: `398 passed in 12.97s` and `398 passed in 12.89s`, the last with a
  captured `EXIT=0` (`evidence-2026-09-21/suites/seed_coordinator-full-2026-09-21.txt`). It does **not**
  establish that the 2026-09-20 stall is fixed — only that it did not reproduce in this lane's three runs.
- **A concurrent sibling lane was committing into the shared object database during this baseline**
  (`refs/heads/policy/red-suite-collection` at 00:25:50Z and later). That does not change the commit under test
  (my worktree is pinned at `349845b` and stayed clean), but it does mean the host was loaded, and it is the
  same class of condition that produced the earlier stall. Nothing was run in parallel by *this* lane: the
  suites ran strictly one at a time.
- **`tests/security` is RED at HEAD and I am not calling the baseline green.** 10 of 11 invocations exit 0 and
  every A9-surface suite is green; the security suite has one documented pre-existing failure whose owner is
  named in the programme record, not by me.

### 4.2 What the baseline is, and is not

These suites pass **because A9's cited work is absent**. A green run over the v1 surface says the integrated
tree is not broken; it says nothing in favour of the candidate's correctness, and it is not evidence that A9
is implemented. `tests/a9_acceptance` passing means the *design inventory* is internally consistent and
`design_only`; it does not mean any A9 capability executes.

---

## 5. What this adjudication does NOT establish

- **I did not integrate anything.** No rebase, cherry-pick, merge, landing or push was performed; A9's 209
  paths are exactly as unintegrated as they were. `git merge-base --is-ancestor ba6abbe integration/core-pilot`
  is still false.
- **I did not change any gate state, and did not re-credit anything.** A9 remains `verified_stage: spec` /
  `reported_stage: implemented` / `state: design_only`; no register, checklist, contract, manifest, ledger or
  decision-log entry was edited by this lane. Nothing here supports promotion to `implemented`, `accepted`,
  `integrated` or `qualified`.
- **A green baseline is not a closure record.** §4 is a set of suite invocations over the *v1* surface. It
  contains no A9 acceptance result, no physical result, no browser result, no Windows result and no
  performance measurement. It is not a completion artifact and must not be cited as one.
- **I did not re-run the candidate's own suites.** Claims #4 and #5 in §3 (the lane's 75-pass and
  1,755-pass/14-skip numbers) remain the author's/reviewer's recorded numbers; this lane did not reproduce
  them, and reproducing them would still only qualify the unintegrated lane.
- **The "18" is not a conflict count.** It is a shared-path count from `git diff --name-only`. No
  `merge-tree`, cherry-pick or three-way probe for A9 was run here; the rebase/conflict surface of A9 is
  **unmeasured** by this report.
- **Correctness of the candidate is not adjudicated.** As the 2026-09-16 record itself says, the objects
  cannot establish quality. Nothing here says A9's code is right or wrong — only where it is and is not.
- **The candidate-count number is not final.** It moved 122→123→124 inside this lane's window while a sibling
  lane committed (and the non-ancestor ref count moved 54→55). The number is stamped with a measurement time
  and a ref-set digest precisely because it is not a constant; any later reader must re-measure rather than
  quote this report's figure. **This lane's own commit adds to it**: committing this adjudication on
  `adjudication/a9-2026-09-21` (as the programme's convention for adjudication deliverables requires, and as
  the 2026-09-20 lane did) creates one more commit reachable only from a non-ancestor ref, so the next reader's
  unreachable count will be one higher than the figure reported here. That is a fact about the measurement, not
  about A9.
- **Content-equivalence across renamed paths is not tested.** The 209-path probe compares the same path. A
  hypothetical case where A9's content landed at a *different* path under a different name is outside the
  probe; the patch-id check (0 matches) and the protocol-string grep reduce but do not eliminate that space.
- **No physical, hardware, Windows, mobile or UI verification of any kind was performed.**
- **`tests/a13_macos` and the demo suites were not run** — by instruction, and because another lane owns that
  suite on this host; nothing here says anything about the A13 surface.

---

## 6. Evidence index (all under `runs/a9-adjudication/` unless stated)

| artifact | what it is |
|---|---|
| `ADJUDICATION-2026-09-21.md` | this report |
| `evidence-2026-09-21/remeasure-2026-09-21.json` | full enumeration + 209-path probe output |
| `evidence-2026-09-21/blob-history-2026-09-21.json` | per-path history probe (0 stale, 52 divergent, 157 absent) |
| `evidence-2026-09-21/refs-2026-09-21.txt` | the 86-ref snapshot the counts were measured against (sha256 `dc37e8e5…`) |
| `evidence-2026-09-21/suites/baseline-2026-09-21.txt` | raw pytest output for every suite in §4 |
| `ADJUDICATION.md`, `table.json`, `candidates.json`, `tranche2.json`, … | 2026-09-16 tranche-1/2 evidence — **untouched by this lane** |

Scripts kept in this lane's worktree (`mycelium-a9-adjudication-lane/scripts/adjudication/`):
`remeasure_a9_premise.py`, `blob_history_check.py`, `run_baseline_suites.sh`.

---

## 7. Postscript — the report's own prediction, verified

§5 says the next reader's unreachable count will be one higher because this lane's commit adds to it. That was
checked rather than assumed, at `2026-09-21T00:31:49Z`:

```
$ git --no-optional-locks rev-list --all --not integration/core-pilot --count
125        # was 124 at 00:28:43Z; +1 is this lane's own adjudication commit on
           # adjudication/a9-2026-09-21 (its hash is deliberately not pinned here:
           # the commit contains this report, so any amendment renames it)
$ git merge-base --is-ancestor ba6abbe integration/core-pilot ; echo $?
1          # still unintegrated, after everything in this report
```

The load-bearing facts re-verified at the same instant: `merge-base(HEAD, ba6abbe) = abe291c3…`, 27 lane
commits, 209 changed paths, `209 files changed, 27506 insertions(+), 592 deletions(-)`, **0** lane commits
ancestors of HEAD, **0** of the 209 paths carrying the candidate's blob at HEAD, and `mycelium_membership/`
at HEAD still reading `__init__.py  contracts.py` (v1 only).
