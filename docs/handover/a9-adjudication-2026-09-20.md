# A9 demotion adjudication at `7d0ffb6` + green baseline

**Date:** 2026-09-20
**Author:** delegated adjudication child (read-only against the frozen tree)
**Worktree:** `/Users/evinova-self/Documents/playground/mycelium-a9-adjudication-20260920`
**Branch:** `adjudication/a9-2026-09-20`
**Base sha:** `7d0ffb6f52b24bc00353aeed170a29acdabc49e2` (`integration/core-pilot`, HEAD of the frozen
checkout `/Users/evinova-self/Documents/playground/mycelium-core-pilot`)
**Head sha of this worktree at start:** `7d0ffb6f52b24bc00353aeed170a29acdabc49e2` (no commits added
before the adjudication commit)

**Scope discipline.** This adjudication performs **no rebase, no cherry-pick, no merge, no landing, no push**.
The frozen tree `mycelium-core-pilot` was read via the shared object database only (no `checkout`, `reset`,
`branch`, `commit` or `worktree` command ran in it; the one `git worktree add` ran *from* it and created this
worktree *outside* it). Nothing was written outside this worktree. `tests/a13_macos` was deliberately **not**
run — another child owns that suite on this host.

---

## 0. Headline

The 2026-09-16 demotion of A9 from `implemented` to `spec` is **correct and is still correct at `7d0ffb6`**.
Re-derived path by path rather than from the recorded count: of the 209 paths A9's cited candidate `ba6abbe`
changes relative to its own merge base, **0 carry the candidate's blob at HEAD** — 157 are **absent** at HEAD
(they are files the lane created) and 52 exist at HEAD with **different** content than the candidate's, and for
**none** of those 52 has the candidate's blob ever appeared in HEAD's history. No commit of the A9 lane is an
ancestor of HEAD, and no A9-lane commit is patch-identical to any commit in HEAD's history.

A9 is **not** unwritten, however: an *earlier, separate* A9 cycle (`9bbd1ec`, 2026-07-24) **is** an ancestor of
HEAD, and the only A9-named acceptance directory present at HEAD (`tests/a9_acceptance/`) was authored by the
**A4** commits `487a1b4` and `e61e780`, not by the A9 lane. So "A9 has zero integrated work" is too strong;
"A9's *cited evidence* has zero integrated work" is exactly right.

The baseline at `7d0ffb6` in this worktree is **green** for every suite that actually *completed*: 7 suite
invocations, **800 passed / 1 skipped / 0 failed**, all exit 0 (§5.1). One further suite,
`tests/seed_coordinator` as a whole directory, **did not complete** — it stalled on a single test with 0% CPU
for 11 minutes and was killed rather than reported as a pass; with that one test deselected the same
directory finished **397 passed in 10.72 s** (§5.3). No green is claimed for the stalled test.

---

## 1. Sources read (verbatim)

### 1.1 `PROGRESS_MATRIX.md:33` — the A9 register row (verbatim)

> | **A9** platform-neutral / runtime efficiency | ✔ | ✔ membership v2, native CPU provider, codecs, signed observations, planner | **committed** `ba6abbe` (27 commits, 209 files) | ✖; textual conflict manifest only **[F13]**; 6 semantic overlaps with core-since-base and 6 with A13 **[F14]** | ✔ **[F]** B1/B2 RED on parent (2 fail), GREEN on candidate (75 pass). Retained host suite 1,755 passed / 14 skipped **[R]**; intermittent identity observation unlocalised | ✖ | ✖ no Windows x86_64 | ✖ (frozen 2×/8 tok/s/1,500 ms floors unmeasured) | ✖ | **High** for committed state; full independent adjudication pending |

Related register rows:

> `PROGRESS_MATRIX.md:68` — | **P09** A9 reconcile | A9 | `pending` (**P07**, ledger:4238) | **Committed `ba6abbe`**; B1/B2 independently red/green **[F]**; full adjudication pending | Full independent adjudication; rebase over pilot; integration | No | High |
> `PROGRESS_MATRIX.md:69` — | **P10** A9 three physical desktops | A9 | `pending` (P09) | No Windows x86_64 / Linux / Mac matrix evidence | Hardware + native backends + frozen benchmark | No | High |
> `PROGRESS_MATRIX.md:45` — **Committed but unintegrated candidate:** 3 (A6, A9, A14a).
> `PROGRESS_MATRIX.md:48` — **Performance-promotion gates promoted:** 0 of 4 (A5, A9, A10, A11).

### 1.2 `REVIEW.md` — A9-relevant findings (verbatim)

> `REVIEW.md:163` — Candidate `ba6abbe`: 27 commits, 209 files, +27.5k lines [F14].
> `REVIEW.md:171` — A9 shares 6 semantic files with A13: `route.py`, `supervisor.py`, `iroh.py`, `seed/http.py`, `physical_inference_node.py`, `physical_inference_qualification.py` [F14].
> `REVIEW.md:146` — **A6:** edits gateway `backend.py`/`service.py` [F14].
> `REVIEW.md:147` — **27B adapter** `22796ad`: … Overlaps A9 on 5 files [F14].

### 1.3 `NEXT_EXECUTION_BRIEF.md:59` — the A9 plan step T4 (verbatim)

> | **T4** | **A9** `ba6abbe` rebased onto T2 (P09) | S0.4 accept; T2 | 209 files; semantic overlap above; contract manifest conflict; product spine `app.py`/`projector.py` conflicts if A6 lands first | A9 owner or IO rebases in a fresh worktree; v1→v2 membership migration covers the Mac slice's `CAPABILITY_REPORT_PROTOCOL` consumer; full suites; host suite with documented interpreter; pilot regression run (local) to prove no Mac-path regression; physical A9 remains P10 |

T4's own entry conditions include **S0.4 = P23 A9 full adjudication** (`NEXT_EXECUTION_BRIEF.md:25`), i.e. this
document is one of T4's inputs, not a substitute for it.

### 1.4 The 2026-09-16 demotion (program decision log, read-only)

> `status/decisions.log:51` (2026-09-16, tranche-1 adjudication) — "…A9's own status line is updated: its recorded evidence ba6abbe is post-pilot with **209/209 paths unintegrated**, so its 'implemented' stage is unsupported by integration and needs re-review rather than promotion." … "spot-verification I ran myself: a65fd54/8141289/f35ab91 are ancestors of ba3595b (duplicate verdicts confirmed) and `git rev-list --all --not ba3595b` = 97, matching the child's count." … "`candidates_found=101` (97 commits unreachable from integration/core-pilot across refs + 1 dangling doc-named commit + 3 already-integrated review-cited commits)".
> `status/decisions.log:52` (2026-09-16) — "**A9 roadmap gate DEMOTED on evidence (reversible): verified_stage implemented -> spec, reported_stage left at implemented.** Reason: the adjudication shows A9's cited evidence ba6abbe is post-pilot with 209/209 paths unintegrated, so the 'implemented' stage was never supported by the integrated branch…"

**Metric note (do not conflate).** `candidates_found=101` was a *candidate-enumeration* count
(97 unreachable + 1 dangling + **3 already-integrated**). At `7d0ffb6`, `git rev-list --all --not 7d0ffb6` also
happens to return **101**, but that is a *different* metric (97 → 101 purely because the branch set grew).
This document does **not** treat the coincidence as a re-derivation of `candidates_found`.

### 1.5 `docs/handover/mycelium-completion-checklist.v1.json` — A9 row (verbatim fields)

- `gate_id`: `"A9"`, `name`: `"Platform-neutral capability and accelerated runtime efficiency"`, `dependencies`: `["A8"]`
- `specification`: `docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md`
- **`state`: `"design_only"`**
- `blockers` (4): `membership migration test window`; `native accelerated Windows x86_64 provider and device`; `qualified efficient int8 kernels and reduced-precision activation codec`; `frozen component-aware single-request benchmark`
- **`completed_requirements`: `["specification"]`**
- **`partial_requirements`: `[]`**
- **`pending_requirements` (12): `implementation`, `product_integration`, `deterministic_positive`, `deterministic_negative`, `physical_positive`, `physical_negative`, `ui_live_all_eight`, `browser_navigation_reconnect`, `browser_second_session`, `full_regressions_audits`, `architecture_handover`, `atomic_feature_commit`**
- `evidence_bindings`: exactly one, `requirement: "specification"`, `artifact_digest: sha256:65f2ebc829d64dfb3a466a5321be097a2078403f9077235c74bdcd08679e1d8e`
- `handover`: `null`

**Partition check, verified in the repo:** only `specification` is "completed". The other 12 requirements are
`pending`, and `partial` is empty. The bound spec digest matches the spec **at HEAD**
(`sha256:65f2ebc8…1d8e`, re-computed locally) — and that HEAD spec blob (`3de92cc9…`) is **byte-identical to
the merge base `abe291c`**, i.e. the A9 lane's own rewrite of its spec is *also* unintegrated (§3, row 12).

### 1.6 `docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md`

- Header: `**Status:** design_only; dependency-ready contract and migration boundary` — consistent with the checklist.
- §6 "One-time migration from membership v1": "Migration is a coordinated product operation, not perpetual compatibility behavior." … "6. During one bounded migration window, v1 inputs may be read only to authenticate the…" (the spec itself carries the migration as a *boundary*, not as executed work).
- The **literal name `CAPABILITY_REPORT_PROTOCOL` does not appear in the A9 spec at all** — the T4 requirement to cover "the Mac slice's `CAPABILITY_REPORT_PROTOCOL` consumer" comes from the execution brief, not from the spec. At HEAD that consumer is `mycelium_a13_macos/peer.py:21` (`from mycelium_membership import CAPABILITY_REPORT_PROTOCOL`) and `:1051` (protocol check).
- At HEAD the symbol resolves to `mycelium_membership/contracts.py:20` — `CAPABILITY_REPORT_PROTOCOL = "mycelium.membership.capability_report.v1"` — i.e. the **legacy v1** protocol. `mycelium_membership/` at HEAD contains only `__init__.py` and `contracts.py`; there is **no `v2.py`, `capability_v2.py`, `control_v2.py`, `declarations_v2.py`, `migration.py`, `observation_windows.py` or `stage_observations.py`**, and a repo-wide search for the v2 protocol strings (`membership.control.v2`, `capability_publication_v2`, `membership_join_request_v2`) outside `docs/` returns **0 hits**. So T4's migration requirement is not merely unproven at this head — **there is nothing at this head for it to migrate through**.

---

## 2. What `ba6abbe` is (git, not prose)

```
git cat-file -e ba6abbe^{commit}                     → exit 0 (EXISTS)
git show -s --format='%H %an %ad %s' ba6abbe         → ba6abbe3a953a755c493f5c56025adeb1f91a35e
                                                       Tranquil-Flow  Wed Sep 9 22:41:11 2026 +0200
                                                       "Fix Goal B observation lifecycle and signed window provenance"
git rev-list --parents -n1 ba6abbe                   → ba6abbe… e007e8d…   (single parent: e007e8d)
git merge-base ba6abbe 7d0ffb6                       → abe291c3ae856bef60394f528f1f86bddf78b2ad
git rev-list --count abe291c..ba6abbe                → 27
git diff --name-only abe291c ba6abbe | wc -l         → 209
git diff --name-status abe291c ba6abbe               → 157 A, 52 M   (0 D, 0 R)
git merge-base --is-ancestor ba6abbe 7d0ffb6         → exit 1  (NOT an ancestor)
```

So "27 commits / 209 files" (REVIEW.md:163) is **confirmed literally**: 27 commits from the merge base
`abe291c`, 209 changed paths, all of them either **added** (157) or **modified** (52).

The lane's own head commit `ba6abbe` is a normal 21-file commit (`21 files changed, 1102 insertions(+), 97 deletions(-)`)
— the "209 files" figure belongs to the **27-commit range**, not to that commit; the two numbers are used
consistently here.

---

## 3. The per-path verdict table (sampled, per path, with the deciding command)

**Method for the sample.** Each row was decided by two independent reads, not by a count:

```
# presence at HEAD
git cat-file -e 7d0ffb6:<path>            # exit 0 = present, exit 128 = absent
# identity at HEAD
git rev-parse ba6abbe:<path>   vs   git rev-parse 7d0ffb6:<path>
```

**Sampling rule and why.** I did *not* trust the recorded 209/209. I ran the two commands above over **all 209
paths mechanically** (result in §3.3) *and* hand-printed a **named sample of 12** chosen to span every
category the demotion's claim depends on: the membership-v2 modules, the v1→v2 migration module, the v2
contract fixture, the native CPU provider, path admission, the cross-platform benchmark gate, the A9
acceptance suite, the `CAPABILITY_REPORT_PROTOCOL` consumer, and the two highest-churn core files.

### 3.1 Named sample — 12 paths, individual verdicts

| # | path | `cat-file -e 7d0ffb6:<path>` | cand blob | HEAD blob | **verdict** |
|---|---|---|---|---|---|
| 1 | `mycelium_membership/v2.py` | **absent** (128) | `4ffe98d1` | — | **ABSENT at HEAD** (A9 created it; never landed) |
| 2 | `mycelium_membership/capability_v2.py` | **absent** (128) | `b2b62766` | — | **ABSENT at HEAD** |
| 3 | `mycelium_membership/migration.py` | **absent** (128) | `c8b6a399` | — | **ABSENT at HEAD** (the T4 v1→v2 migration module) |
| 4 | `contracts/compatibility-fixtures/membership-join-request-v2.json` | **absent** (128) | `bbd28cd4` | — | **ABSENT at HEAD** |
| 5 | `mycelium_router/native_cpu_provider.py` | **absent** (128) | `672fccfe` | — | **ABSENT at HEAD** (native provider) |
| 6 | `mycelium_router/path_admission.py` | **absent** (128) | `6977b869` | — | **ABSENT at HEAD** (signed path admission) |
| 7 | `mycelium_qualification/cross_platform_benchmark.py` | **absent** (128) | `1475b726` | — | **ABSENT at HEAD** (frozen benchmark) |
| 8 | `tests/a9_acceptance/test_inventory.py` | present (0) | `d9140dce` | `4b74486f` | **DIVERGENT** — HEAD's copy is A4's, not A9's |
| 9 | `mycelium_node/membership.py` | present (0) | `7a14fd91` | `320e0d04` | **DIVERGENT** (lanes differ; candidate blob never in HEAD history) |
| 10 | `mycelium_live/route.py` | present (0) | `c3b95966` | `15bd5fec` | **DIVERGENT** (8 core commits touched it since base) |
| 11 | `contracts/contract-manifest.v1.json` | present (0) | `4ec91fa9` | `5aa0a911` | **DIVERGENT** (17 core commits touched it since base) |
| 12 | `docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md` | present (0) | `b7afb2f6` | `3de92cc9` | **DIVERGENT** — HEAD's spec `== abe291c`'s spec, i.e. **A9's own spec rewrite did not land either** |

**Zero of the 12 sampled paths carries the candidate's content at HEAD.** Categories 1–7 (A9's new modules and
fixtures) are simply not in the tree; categories 8–12 exist but hold someone else's bytes.

### 3.2 Staleness check on the "divergent" category

For every one of the 52 modified paths I also asked whether HEAD had *ever* held the candidate's blob:

```
# per path: list every blob HEAD's history has ever had for the path, look for the candidate's blob
git rev-list 7d0ffb6 -- <path>   → git rev-parse <commit>:<path> for each   → set membership test
```

- modified paths where the candidate's blob **appears somewhere in HEAD's history**: **0**
- modified paths where the candidate's blob **never** appears in HEAD's history: **52**

So the 52 are not "A9's edit landed earlier and was later superseded" — A9's edit **never** landed.

### 3.3 Full-population result (all 209 paths, mechanical)

```
git diff --name-status abe291c ba6abbe   → 157 A + 52 M = 209 paths
per path: git cat-file -e 7d0ffb6:<path> ; git rev-parse ba6abbe:<path> vs 7d0ffb6:<path>

ABSENT-at-HEAD ................ 157   (all 157 are status A)
DIVERGENT-present-different ... 52   (all 52 are status M)
INTEGRATED-identical ..........   0
                                ---
                                209
```

**`209/209 unintegrated` is re-confirmed at `7d0ffb6`, path by path.** The demotion's number survives a change
of head; the *sample above is what establishes it*, not the recorded count.

### 3.4 Commit-level cross-checks (independent of the path table)

```
# every A9-lane commit tested for ancestry at HEAD — none matches
for c in $(git rev-list abe291c..ba6abbe); do git merge-base --is-ancestor $c 7d0ffb6 && echo ANCESTOR $c; done
  → (no output: 0 of 27 are ancestors of 7d0ffb6)

# patch-id equality against 386 commits of HEAD's recent history — no match
git log -p abe291c..ba6abbe   | git patch-id --stable | awk '{print $1}' | sort  → 27 pids
git log -p 7d0ffb6 -n 400     | git patch-id --stable | awk '{print $1}' | sort  → 386 pids
comm -12 → 0 overlapping patch-ids
```

Two independent readings (blob identity and commit identity) agree: **A9's lane content is not in
`integration/core-pilot` in any form.**

### 3.5 Rebase-surface measurement (context, not a conflict census)

```
git rev-list --count abe291c..7d0ffb6                → 137 core commits since A9's base
git diff --name-only abe291c 7d0ffb6 | wc -l         → 437 paths changed in core since that base
A9's 209 paths ∩ core's 437 paths                    → 18 shared paths
```

The 18 shared paths (with the number of core commits touching each since the base):

| path | core commits since base |
|---|---|
| `contracts/contract-manifest.v1.json` | 17 |
| `mycelium_live/route.py` | 8 |
| `physical_inference_node.py` | 4 |
| `physical_inference_qualification.py` | 4 |
| `mycelium_router/transports/iroh.py` | 3 |
| `scripts/generate_contract_fixtures.py` | 3 |
| `mycelium_live/supervisor.py` | 2 |
| `mycelium_product_spine/projector.py` | 2 |
| `mycelium_seed/http.py` | 2 |
| `docs/handover/mycelium-completion-checklist.v1.json` | 1 |
| `mycelium_product_spine/contracts.py` | 1 |
| `mycelium_router/mlx_runtime.py` | 1 |
| `mycelium_router/numpy_runtime.py` | 1 |
| `scripts/contract_registry.py` | 1 |
| `tests/contracts/test_contract_fixtures.py` | 1 |
| `tests/product_spine/test_application.py` | 1 |
| `ui/web/src/App.tsx` | 1 |
| `ui/web/src/features/productEvidence/contracts.ts` | 1 |

The review counted "15 paths also changed in core since the base" and "6 semantic overlaps with core-since-base
… and 6 with A13" (`PROGRESS_MATRIX.md:33`) — the 15 was measured at `ba3595b`; at `7d0ffb6` the same
intersection is **18**, consistent with 137 core commits having landed since. The 6 A13 files named in T4
(`route.py`, `supervisor.py`, `iroh.py`, `seed/http.py`, `physical_inference_node.py`,
`physical_inference_qualification.py`) are **all** inside the 18. **This is a shared-path count, not a conflict
count** — see §6.

---

## 4. Reachable vs stranded

**Reachable (integrated at `7d0ffb6`):**
- A9's **base** `abe291c` is an ancestor of HEAD (`git merge-base --is-ancestor abe291c 7d0ffb6` → 0). The lane forked from integrated bytes.
- An **earlier A9 cycle** `9bbd1ec` (*"integration: merge A9 cycle (two-stage approved)"*, 2026-07-24) **is** an ancestor of HEAD.
- `tests/a9_acceptance/` **exists** at HEAD (4 tests: `inventory.v1.json` + `test_inventory.py`), but it was authored by **`487a1b4` (A4)** and **`e61e780`**, not by the A9 lane. Today's inventory declares `"state": "design_only"` and its `claim_boundary` reads: *"frozen authority, migration, runtime efficiency, qualification, benchmark, and projection acceptance inputs only; no schema, implementation, migration or benchmark execution, device evidence, readiness, or completion claim"* — i.e. the integrated A9 acceptance surface is an **inventory of intents**, self-labelled design-only.
- The three review-cited "already integrated" commits are still ancestors of `7d0ffb6`: `a65fd54` ✔, `8141289` ✔, `f35ab91` ✔ (spot-check re-run today). This corroborates the 2026-09-16 spot-verification.
- `docs/handover/mycelium-completion-checklist.v1.json` (A9 row) and the A9 spec are at HEAD with the checklist's own bound digest — the **bookkeeping** side of A9 is integrated; the **capability** side is not.

**Stranded (reachable only from non-integrated refs):**
- `ba6abbe` itself: `git for-each-ref --contains ba6abbe` → exactly **one** ref, `refs/heads/work/mycelium-session8b-5_ry7xyo` (worktree `…/mycelium-session8b-5_ry7xyo`) plus a detached review checkout at `…/a9-cuc1-review/a9`. No remote, no integration branch.
- Its parent `e007e8d` — **not** an ancestor of HEAD.
- All 27 lane commits and all 209 lane paths (§3).
- The lane's **tests**: `tests/a9_acceptance/acceptance_bindings.v1.json`, `reconciled_scope.v1.json`, `test_acceptance_bindings.py`, `test_scope_reconciliation.py`; `tests/live/test_a9_*`; `tests/membership/test_control_v2*.py`; `tests/node_agent/test_membership_v2_session.py`; `tests/product_spine/test_a9_*.py`; `tests/qualification/test_a9_three_platform_gate*.py`; `tests/qualification/test_cross_platform_*.py`; `tests/runtime/test_a9_path_admission.py` and the rest of the `tests/runtime/test_admission_*` family — all **absent** at HEAD.
- The lane's spec rewrite (`b7afb2f6` vs HEAD's `3de92cc9`) — stranded.
- Adjacent A9-lane refs that are **not** integrated: `codex/a9-capability-acceptance` = `0f79a2b` (*"docs: freeze A9 capability acceptance inventory"*). Its same-subject twin `9e68ad5` is **also not** an ancestor of HEAD (`git merge-base --is-ancestor 9e68ad5 7d0ffb6` → non-zero). Note `efcdf44` (`codex/a9-platform-neutral-from-a8`) **and** `1fd3a6f` (`codex/a9-cross-platform-plan`) **are** ancestors — those two A9-labelled branches are already in core.

---

## 5. Green baseline at `7d0ffb6` in this worktree

Interpreter/toolchain used verbatim as instructed:

```
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  /opt/homebrew/bin/python3 -B -m pytest -q -p no:cacheprovider <paths>
```

### 5.1 Suites run — literal counts and exit codes

| # | suite(s) | why it is genuinely A9-relevant | literal result | exit |
|---|---|---|---|---|
| 1 | `tests/a9_acceptance` | the only A9-named acceptance suite at HEAD (integrated copy, A4-authored) | **4 passed** in 0.03s | **0** |
| 2 | `tests/membership` | membership contracts — the surface A9's v2 work rewrites | **46 passed** in 0.20s | **0** |
| 3 | `tests/conformance` | production/router conformance + trace generator | **35 passed** in 1.62s | **0** |
| 4 | `tests/a9_acceptance tests/membership tests/conformance` (same three, one invocation) | cross-check that the totals compose | **85 passed** in 1.88s | **0** |
| 5 | `tests/iroh_conformance tests/request_conformance` | transport + request conformance (A9 touches `iroh.py`/`seed/http.py`) | **127 passed** in 24.84s | **0** |
| 6 | `tests/node_agent` | contains `test_membership.py`, i.e. the **`CAPABILITY_REPORT_PROTOCOL` consumer** side | **150 passed, 1 skipped** in 25.64s | **0** |
| 7 | `tests/node_agent/test_membership.py tests/seed_coordinator/test_coordinator_core.py` | the two consumer-facing tests named in T4's migration requirement | **41 passed** in 0.63s | **0** |
| 8 | `tests/seed_coordinator` (whole directory) | seed-side membership/coordinator consumers | **DID NOT COMPLETE** — killed at 11 m 01 s wall, **0.0 % CPU**, 260 of 398 collected tests emitted (65 %), 0 failures/errors in the partial output. **Not counted as green.** | **killed** |
| 9 | `tests/seed_coordinator` **deselected** `test_local_two_node.py::test_local_two_node_native_process_e2e` | same coverage minus the one test that stalls on this host | **397 passed, 1 deselected** in 10.72s | **0** |
| 10 | `tests/seed_coordinator/test_local_two_node.py::test_local_two_node_native_process_e2e` alone, under `timeout 240` | to characterise the stall | **did not return within 240 s** (and the run held the pipe open past 300 s) | **n/a / 124-class** |

**Green totals over the completed, mutually disjoint invocations (rows 1, 2, 3, 5, 6, 7, 9):**
`4 + 46 + 35 + 127 + 150 + 41 + 397` = **800 passed, 1 skipped, 0 failed, 0 errors — every exit code 0.**
Row 4 is the same three suites as rows 1–3 in a single invocation and is *not* additive (it reproduces 85
passed exactly). Rows 8 and 10 are **not** green and are not included in the total.

### 5.2 Deliberately NOT run (and why)

- **`tests/a13_macos` — NOT run.** Explicitly out of scope: another child owns that suite on this host and concurrent runs stall each other. A9's physical/Mac-path claims therefore have **no** coverage from this document.
- **The full canonical suite** (`pytest tests/`) — not run. An attempt to run seven directories in one invocation (`tests/a9_acceptance tests/membership tests/conformance tests/iroh_conformance tests/request_conformance tests/node_agent tests/seed_coordinator`) **timed out at 180 s** (exit 124, `tests/seed_coordinator` still running); the suites above were then run in smaller groups so that every reported number is a *completed* run rather than a truncated one. No green full-suite claim is made here.
- **`ui/web` vitest and Playwright e2e** — not run (node/Playwright lane; `ui_live_all_eight`, `browser_*` remain `pending` in the checklist, and the A9 lane's `ui/web` changes are absent at HEAD anyway, so there is nothing A9-specific to exercise).
- **Physical/hardware gates (Windows x86_64, Linux, second physical Mac), the frozen 2×/8 tok/s/1,500 ms benchmark floors, package build/signing** — not run; no hardware in scope.
- **The A9 lane's own tests** — impossible to run at HEAD: they are among the 157 absent paths (§3.3). Running them would require materialising the lane, which this task forbids.

### 5.3 `tests/seed_coordinator` — the one suite that did not go green, and what to make of it

Three separate runs, in this order:

1. **Whole directory, backgrounded.** `398 tests collected` (`--collect-only`). The run emitted 260 dots
   (65 %) and then stopped making progress: **0.0 % CPU, no child processes, no listening sockets**, at
   11 m 01 s wall. It was **killed**. The partial output contained **0 failures and 0 errors** — only dots.
   **This is not a green result and is not counted as one.**
2. **Locating the stall.** `--collect-only -q | sed -n '258,264p'` maps the 261st collected test — the one in
   flight when the dots stopped — to
   `tests/seed_coordinator/test_local_two_node.py::test_local_two_node_native_process_e2e`. So the stall is
   attributable to **one test**, not to the suite.
3. **Same directory, that one test deselected:** `397 passed, 1 deselected in 10.72s` — **exit 0**. So every
   other test in `tests/seed_coordinator` is green, and quickly.
4. **That one test alone, under `timeout 240`:** **did not return within 240 s** (the pipeline held the pipe
   open past 300 s). It is a two-node *native process* e2e test, i.e. the class of test that spawns processes
   and binds ports.

**Host-contention context (recorded, not used as an excuse).** At the time of runs 1 and 4, sibling delegated
children were running other load-bearing suites on this same host — observed via `ps ax`:
`tests/a13_macos/test_app_authority.py` (from a worktree belonging to another child) and
`tests/live/test_a4_runtime_interruption.py` under `timeout 900`. That is the configuration the `pytest`-parallel
port-conflict pitfall describes. **I did not re-run it on a quiet host**, so I cannot say whether the stall is
contention-induced or intrinsic. I record only what was observed: on this host, under this load, that test did
not complete in 11 minutes (and did not complete in 240 s when run alone under the same load); everything else
in the directory passed in 10.72 s. The one thing that *is* established: the 11-minute stall is **not** a
general `tests/seed_coordinator` failure and **not** an A9 regression (A9 is absent from this head — §3).

---

## 6. Verdict on 2026-09-16 demotion

### 6.1 Per-claim verdicts

| # | claim (as recorded) | verdict | deciding evidence |
|---|---|---|---|
| A | `ba6abbe` exists and is a commit | **CONFIRMED** | `git cat-file -e ba6abbe^{commit}` → 0; subject/date read from `git show -s` |
| B | `ba6abbe` = 27 commits / 209 files (REVIEW.md:163) | **CONFIRMED** | `git rev-list --count abe291c..ba6abbe` = 27; `git diff --name-only abe291c ba6abbe \| wc -l` = 209 |
| C | `ba6abbe` is post-pilot / not integrated | **CONFIRMED** | `git merge-base --is-ancestor ba6abbe 7d0ffb6` → exit 1; `for-each-ref --contains ba6abbe` → 1 non-integrated ref |
| D | "209/209 paths unintegrated" | **CONFIRMED at `7d0ffb6`** (independently re-derived path by path: 157 absent + 52 divergent, 0 identical; 0 of 52 ever had the candidate blob) | §3.3, §3.2, §3.1 |
| E | "the 'implemented' stage was never supported by the integrated branch" | **CONFIRMED** | 0 of 27 lane commits are ancestors of HEAD; 0/27 lane patch-ids match any of 386 HEAD commits; the 7 new A9 modules and 3 A9-specific test families are absent |
| F | A9's cited evidence is the only thing demoted — the gate's `reported_stage` stayed `implemented` and the move is reversible | **CONFIRMED as an accurate characterisation of the move**; nothing in this head re-instates the `implemented` verified stage | checklist A9 row `state: design_only`, `completed_requirements: ["specification"]`, `partial: []`, 12 pending |
| G | `candidates_found = 101` (97 unreachable + 1 dangling + 3 already-integrated) | **Not re-derived** (different metric, and the sub-counts were measured against `ba3595b`, not `7d0ffb6`); the *checkable* half is **CONFIRMED**: the three "already-integrated" commits `a65fd54`, `8141289`, `f35ab91` **are** ancestors of `7d0ffb6`, and `git rev-list --all --not 7d0ffb6` = 101 (a coincidence of a different metric, explicitly **not** used here as corroboration) | §1.4, §4 |
| H | A9 has "zero integrated work" (a claim the demotion's prose *could* be over-read as) | **REFUTED** | `9bbd1ec` ("integration: merge A9 cycle") **is** an ancestor of HEAD; `tests/a9_acceptance/` exists at HEAD (A4-authored); `efcdf44` and `1fd3a6f` A9-labelled branches are integrated |
| I | The 15-path / 6-overlap numbers in `PROGRESS_MATRIX.md:33` | **STALE BUT NOT WRONG** — they were measured at `ba3595b`; at `7d0ffb6` the same intersection is 18 paths (all 6 A13 files still inside it) | §3.5 |

### 6.2 Verdict

**The demotion was right on 2026-09-16, and it is still right today.** Its load-bearing claim — that A9's cited
evidence commit `ba6abbe` carries no integrated content — survives both an independent per-path re-derivation
at a *later* head (`7d0ffb6`, 137 core commits after A9's base) and a commit-level cross-check. A gate whose
only integrated artefact is its own `design_only` spec, and whose cited candidate's 209 paths are 0-for-209 at
HEAD, cannot honestly hold a verified stage of `implemented`.

**No part of the demotion is now wrong.** Two of its supporting numbers are **age-stale, not incorrect**: the
"15 paths also changed in core since the base" and the `candidates_found` sub-counts were measured at
`ba3595b`; the path-overlap figure has since grown to 18 (§3.5), which *strengthens* rather than weakens the
"post-pilot" conclusion. One sentence in the demotion's surrounding prose is **too coarse if read literally** —
A9 has *some* integrated work (`9bbd1ec`, `tests/a9_acceptance/`, two A9-labelled integrated branches); what is
stripped is the **cited evidence**, not the gate's entire history.

**Consequence for T4, stated as evidence and not as advice:** T4's premise ("A9 `ba6abbe` rebased onto T2") is
still exactly the right step for A9 — nothing else can move these 209 paths into the product — and the
reasons to schedule it deliberately rather than opportunistically are now measured: 18 of the 209 paths are
shared with 137 commits of core drift, `contracts/contract-manifest.v1.json` alone has been rewritten 17
times since A9's base, and A6's attempt to merge into this same head produced four content conflicts
(ERRATA-31, `status/decisions.log:6864`). **This document does not attempt, recommend a date for, or predict
the outcome of that rebase**; it records only that the rebase's input is unchanged and its drift surface is
non-empty.

---

## 7. What this does NOT prove

1. **No rebase was attempted.** Not one lane commit was replayed onto any base; nothing was cherry-picked,
   merged, or landed. "The 209 paths are unintegrated" is a statement about `7d0ffb6` *as it stands*, not about
   how hard the rebase is or whether it succeeds.
2. **The rebase's conflict surface is unmeasured.** §3.5 counts *shared paths* (18) and the core commits
   touching them. It is **not** a conflict census: no 3-way merge, `merge-tree`, or `cherry-pick -n` was run
   against `ba6abbe`. A shared path may merge cleanly; an unshared path may still conflict on a rename or a
   generated-fixture hash. The count 18 must not be quoted as "18 conflicts".
3. **No physical A9.** Nothing here touches Windows x86_64, Linux, a second physical Mac, or any device matrix
   (P10). Zero hardware was exercised.
4. **No membership migration was proven.** The v1→v2 migration was not implemented, not executed, and not
   tested. At this head there is **no v2 code to migrate** (0 hits for the v2 protocol strings outside `docs/`),
   and the Mac slice's `CAPABILITY_REPORT_PROTOCOL` consumer at HEAD resolves to the **v1** constant
   `mycelium.membership.capability_report.v1`. T4's migration requirement remains entirely open.
5. **No performance claim.** The frozen 2×/8 tok/s/1,500 ms floors and the component-aware single-request
   benchmark were not run; `mycelium_a9_benchmark.py` and `mycelium_qualification/cross_platform_benchmark.py`
   do not exist at HEAD.
6. **No `tests/a13_macos` coverage, and therefore no Mac-path regression claim.** That suite was deliberately
   left to its concurrent owner; the A9 physical/Mac-path question is untouched.
7. **No full-suite green.** The numbers in §5 are bounded suite invocations, not `pytest tests/`. A
   seven-directory combined invocation **timed out at 180 s (exit 124)** and is reported as such, not as a
   pass. `tests/seed_coordinator` as a whole directory **stalled and was killed**; its 397-test deselected form
   is green, but the single deselected test
   (`test_local_two_node.py::test_local_two_node_native_process_e2e`) has **no** verdict from this document —
   neither pass nor fail. That test's behaviour on a quiet host is unknown.
8. **No green claim for A9 itself.** These suites pass at `7d0ffb6` *because* A9's work is absent from
   `7d0ffb6`. Green here means "the integrated tree is not broken and the legacy v1 membership/consumer paths
   work"; it is **not** evidence that A9's capability works, nor that A9's own tests would pass on a rebase.
9. **No change to any record.** The register, the checklist, the spec, the decision log and the frozen tree were
   read, never written. A9 is **not** re-credited, re-staged, promoted or integrated by this document; its
   verified stage remains `spec`.
10. **The `candidates_found = 101` enumeration was not re-derived.** Only its three "already-integrated"
    members were re-checked (`a65fd54`, `8141289`, `f35ab91` — all still ancestors of `7d0ffb6`). The 97
    unreachable + 1 dangling split was not recomputed at this head.
11. **Nothing was written outside this worktree**; the single commit carrying this file is on
    `adjudication/a9-2026-09-20` in this worktree only. No push, no PR, no merge.
