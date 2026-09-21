# 04 — Evidence log, citation index, and honest gaps

> **Status: DRAFT. NOT APPROVED. NOT APPLIED.** This file exists so that a reviewer can falsify every claim
> in `01-AMENDMENT.md`, `02-OPTIONS.md` and `03-EDGES-BEFORE-AFTER.md` without re-deriving it — and so that
> the things I could **not** verify are as visible as the things I could.

**Verification labels used throughout**

- **[V]** — verified by me at source, this session, with the instrument named.
- **[R]** — a *record* I read (a file that states it), not something I re-executed. A record is evidence of a
  claim having been made, never proof the claim is true.
- **[U]** — unresolved / could not verify. Every **[U]** appears again in §8.

---

## 1. Provenance — what this draft is bound to

| item | value |
|---|---|
| drafting worktree | `/Users/evinova-self/Documents/playground/mycelium-a5-amendment-lane` (created by `git worktree add --detach … 9d68291` from `/Users/evinova-self/Projects/mycelium-astra-ready`) |
| commit read | `9d6829140921b806ee4b3d3e0e259657f7792615` — *"feat(a13-operator): preview.83 chain, round launcher and preflight, generated from the proven .82 set"* **[V]** (`git log -1`) |
| pilot tree (READ-ONLY) | `/Users/evinova-self/Documents/playground/mycelium-core-pilot` @ `9d68291` — read only, never written **[V]** |
| deliverable location | `/Users/evinova-self/Documents/mycelium-core-program/runs/a5-amendment-draft/` (outside any git repo) |
| branch state of the worktree | detached HEAD at `9d68291`, clean before and after **[V]** |

### Source files read, with digests

| file | sha256 | note |
|---|---|---|
| `docs/handover/a5-completion-record.v1.json` | `9aa287d2c7e35a1db36c01e4f2326891747b497d77b43285e8b3fbc2a2ea1628` | git blob `ddc8e57d…`, unchanged since it was added **[V]** |
| `docs/handover/mycelium-completion-checklist.v1.json` | `9aa2614696a2925eb0305713233393fe1491dfc17f2c609e21a4ca3ab48b7a0c` | `updated_on: 2026-09-20` **[V]** |
| `docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md` | `923f47d265b6a597604852b352ae5b2f78c98abefd546da4289697cc62db93de` | **equals** the digest the checklist pins at `:637` **[V]** |
| `docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md` | `12a742f7943b95f83c1b117c0637c9ea27aa1f14fe25bcd92319120b887ce30b` | — |
| `docs/superpowers/plans/2026-08-20-mycelium-a5-finish-plan.md` | `2973d21de089cb95936fc1ec6848cb69d714824b840fd842a367e902f4abf32a` | — |
| `contracts/governance-ledger.v1.json` | `dcf608d63e052ab186fbadaa013eb37a55e8219400d1f5408a8aa7a3ad273dd2` | protocol `mycelium.governance_ledger.v1`; **no P-rows** **[V]** |
| `docs/handover/a5-integration-source-manifest.v1.json` | `bf100e9cf26eab160ccb90a3db8649d8b7c8b98cce85da93c5960d8905d90a2f` | present; **contents not parsed by me** **[U→G7]** |
| `/Users/evinova-self/Desktop/MYCELIUM_REMAINING_WORK_LEDGER_2026-09-07.json` | `117c106cc2d21ae0b2837c410d6267a3953868e390652c8aeaaecf5a6b23721e` | 5,710 lines; protocol `mycelium.remaining_work_ledger.v1`; mtime 2026-09-10 09:44 **[V]** |
| `/Users/evinova-self/Desktop/MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md` | `5763a8a2791bbeecad85900106ab8311b2099d97559d8d1687ad5388633504ad` | **matches** the hash the ledger records at `:5460` **[V]** |
| `/Users/evinova-self/Desktop/MYCELIUM_REMAINING_WORK_MASTER_PLAN_2026-09-07.md` | `30e630e89af1eb9f7d897d584c66f0893c7b2a7c469d4a787f0ed7d6c4effb12` | 751 lines |
| `/Users/evinova-self/Desktop/A5_INTEGRATION_REQUALIFICATION_GOAL_2026-08-30.md` | not hashed **[U→G8]** | the `closing_commit` sentinel authority, §5 of `01-AMENDMENT.md` |
| `/Users/evinova-self/Documents/mycelium-core-program/PLAN.md` | not hashed **[U→G8]** | program plan in force |
| `/Users/evinova-self/Documents/mycelium-core-program/status/decisions.log` | 6,990 lines, 1,155,391 B **[V]** | read selectively: lines 1–103, plus targeted greps |
| `/Users/evinova-self/Documents/mycelium-core-program/status/status.json` | not hashed **[U→G8]** | roadmap gate weights |

---

## 2. Method — and what was NOT done

**Instruments used [V]:** `git` plumbing only (`log`, `rev-parse`, `hash-object`, `cat-file -e`,
`merge-base --is-ancestor`, `diff`, `worktree add`), `grep`, `awk`, `sed -n`, `shasum -a 256`, `mdfind`,
and Python 3 `json` parsing. No network. No browser.

**Explicitly NOT done — and this matters for reading the draft:**

- **no test suite was run.** Not the canonical suite, not `tests/a5_acceptance`, not a single focused test.
  Every "green" statement in the draft is a **[R]** citation of a record, never a fresh measurement. → **G4**
- **no benchmark, no physical run, no fleet contact, no ssh.**
- **nothing was hashed inside the A5 sealed-evidence directory.** I confirmed the directory and its 20
  entries *exist* **[V]** (`ls /Users/evinova-self/mycelium-physical-run/a5-multistage-20260822/final-v190-r33/`
  lists `a5-benchmark.json`, `a5-browser-degraded.json`, `a5-browser-positive.json`,
  `a5-negative-illegal.json`, `a5-negative-replica-loss-browser-lifetime.json`,
  `a5-negative-replica-loss.json`, `a5-product-positive.json`, `concurrent-cancel-24x3/`,
  `source-manifest.v1.json`, and 11 more), but I did **not** recompute the digests the record binds them to.
  → **G7**
- **no file was written outside** `/Users/evinova-self/Documents/mycelium-core-program/runs/a5-amendment-draft/`
  and the drafting worktree. The pilot tree was never written to **[V]**.
- **no ledger event was appended**, and no attempt was made to append one — that file is append-only and
  outside the write boundary (§8 G3).

---

## 3. Citation log — the current dependency structure

Source: **ledger** = `/Users/evinova-self/Desktop/MYCELIUM_REMAINING_WORK_LEDGER_2026-09-07.json`.
All line numbers **[V]** by `awk 'NR==n'` on that exact file.

| # | claim | citation | verbatim |
|---|---|---|---|
| 1 | the ledger's task array is exactly `P00…P18` — **no P19–P23** | JSON parse **[V]** | `['P00','P01','P02','P03','P04','P05','P06','P07','P08','P09','P10','P11','P12','P13','P14','P15','P16','P17','P18']` |
| 2 | `P05` is gate A6, status pending | `4026`, `4027`, `4029` | `"id": "P05"` / `"gate": "A6"` / `"status": "pending"` |
| 3 | **`P05` depends on `P03`** | `4030`, `4031` | `"dependencies": [` / `"P03"` |
| 4 | `P06` = A6 physical, depends on `P05` | `4078`, `4080`, `4082`, `4083` | `"id": "P06"` / `"title": "Qualify replay recovery through ordinary product"` / `"dependencies": [` / `"P05"` |
| 5 | `P07` depends on `P06`, `P04` | `4133`, `4137`, `4138`, `4139` | `"id": "P07"` / `"dependencies": [` / `"P06"` / `"P04"` |
| 6 | **`P09` depends on `P07`** | `4234`, `4238`, `4239` | `"id": "P09"` / `"dependencies": [` / `"P07"` |
| 7 | `P10` depends on `P09` | `4286`, `4290`, `4291` | `"id": "P10"` / `"dependencies": [` / `"P09"` |
| 8 | `P03` = A5 downstream closeout, depends on `P02` | `3817`, `3819`, `3821`, `3822` | `"id": "P03"` / `"title": "Close downstream product/browser gates and metadata"` / `"dependencies": [` / `"P02"` |
| 9 | `P00` has no dependencies; `P01` depends on `P00`; `P02` depends on `P01` | `3527`,`3531`; `3642`,`3646`,`3647`; `3763`,`3767`,`3768` | `"id": "P00"` / `"dependencies": []` / `"dependencies": [` / `"P00"` / `"P01"` |
| 10 | `P08` depends on `P06` | `4183`, `4187`, `4188` | — |
| 11 | `P13` depends on `P10`; `P14` on `P13` | `4444`,`4448`,`4449`; `4496`,`4500`,`4501` | — |
| 12 | `P15` depends on `P07` | `4552`, `4556`, `4557` | — |
| 13 | encoded critical path | `4764`–`4780` | `"critical_path": [` … `"P00","P01","P02","P03","P05","P06","P07","P09","P10","P11","P12","P16","P17","P18"` |
| 14 | no calendar estimate | `5106` | `"calendar_estimate": null` |
| 15 | the A5 scored cycle was **deferred**, not completed | `5692`, `5695` | `"event_id": "mvp-v3-priority-20260910-lane2"` / `"disposition": "A5_scored_replica_cycle_deferred_not_complete"` |
| 16 | A5 recorded not complete; A6 condition unsatisfied | `5697`, `5698` | `"A5_complete": false` / `"A6_scheduling_condition_satisfied": false` |
| 17 | **five separate events** record Stage C unauthorised | `5592` (event `5565`), `5627` (event `5604`), `5659` (event `5638`), `5688` (event `5672`), `5704` (event `5692`) | `"stage_C_authorized": false` |
| 18 | the owner's recorded rule | `5455`–`5469` (event `session7-i4o3rh0h-owner-decision`) | `5462`: `"decision": "After a fresh valid non-promoting A5 disposition, its scheduling gate may close to permit later controlled A6 integration while A5 remains design_only/incomplete/non-promoting"` |
| 19 | its condition is unsatisfied | `5463` | `"fresh_valid_result_condition_satisfied": false` |
| 20 | no identical retry; the downstream functional run class explicitly **not** authorised by that decision | `5465`, `5466` | `"identical_scored_retry_authorized": false` / `"additional_downstream_functional_browser_execution_authorized": false` |
| 21 | thresholds/stopping rules recorded unchanged | `5464` | `"thresholds_and_stopping_rules_unchanged": true` |
| 22 | the decision's authority file + hash | `5459`, `5460` | path = the Desktop file; `"sha256": "5763a8a2791bbeecad85900106ab8311b2099d97559d8d1687ad5388633504ad"` — **equals the file's digest today [V]** |
| 23 | identity drift unresolved | `5686`, `5702` | `"identity_drift_resolved": false` |
| 24 | push / a6 seal false | `5451`, `5452` (first coordination event, lines `5111`–`5453`) | `"push": false` / `"a6_seal": false` |
| 25 | A6/A9/A14 fan-in false | `5557`, `5597`, `5699` | `"A6_A9_A14_fan_in": false` |
| 26 | the ledger's first coordination event has **no** `event_id` | `5111`–`5453` **[V]** | first `event_id` appears at `5455`; `coordination_updates` opens at `5110`; 8 further events follow |
| 27 | **no P-rows in the governance ledger** | `grep -c` on `contracts/governance-ledger.v1.json` returned `0` **[V]** | that file's keys contain no `tasks` array |

---

## 4. Citation log — the authority documents

| # | claim | citation | verbatim |
|---|---|---|---|
| 28 | **the normative plan never made A5 an A6 prerequisite** | completion-plan `436` | `A5 is not a prerequisite for A6. Replay may use any newly qualified compatible path;` |
| 29 | A6's own prerequisite is A4 | completion-plan `425` | `\| A6 \| A4 \|` |
| 30 | A9's prerequisite is A8 | completion-plan **`428`** | `\| A9 \| A8 \|` — see **G1** (the review cites `:427`) |
| 31 | A14's prerequisite is A8 | completion-plan `433` | `\| A14 \| A8; A1 is already transitive \|` |
| 32 | A13's prerequisite is A12 | completion-plan `432` | `\| A13 \| A12 \|` |
| 33 | the A6/A7 rationale paragraph | completion-plan `436`–`438` | `…A6 is a prerequisite for A7 because truthful replay is the fallback when compatible KV cannot be resumed.` |
| 34 | A9's physical requirement (incl. component-aware instrumentation) | completion-plan `210` | `…component-aware compute/serialization/transport instrumentation; frozen reference-versus-candidate single-request corpus and performance floor` |
| 35 | **the rule is scheduling, not technical dependency** | MASTER_PLAN `34` | `There are **technical dependencies** and **this program's integration/fleet ordering**. A6 technically depends on A4, not A5; scheduling its integration after A5 release avoids source/fleet collisions.` |
| 36 | the program's in-force decision (D2) | `PLAN.md:32` | `**A6 source integration no longer waits on any A5 result.** A6 physical work stays serialised against other fleet use.` |
| 37 | the program's in-force A5 posture (D3) | `PLAN.md:33` | `**A5 is off the pilot path.** Replicas are disabled by default. No scored runs. Any future A5 run declares its purpose in advance. Thresholds stay unchanged.` |
| 38 | integration order (D4) | `PLAN.md:34` | `**Pilot-first integration order:** B1 baseline → A14a → A13 → pilot qualification → A9 (rebased) → A6 → 27B / C-UC1 v3.` |
| 39 | pre-authorisation boundary (D11) | `PLAN.md:41` | `**Still human-only:** \`git commit\` (unless granted in that session), \`git push\`/PR/merge to main, …` |
| 40 | stage-credit vocabulary | `PLAN.md:165` | `Stage credits: spec 10%, implemented (committed candidate with its own tests) 35%, independently accepted 50%, integrated on green branch 70%, qualified 100%.` |
| 41 | evidence lives outside the repo | `PLAN.md:154` | `**Evidence and outputs live outside the repo**` |
| 42 | attempts bound | `PLAN.md:119` | `**At most 2 physical attempts per hypothesis.**` |
| 43 | RED reproduction required before a fix | `PLAN.md:91` | `**A local reproduction test that is RED on the failing bytes** before any fix.` |
| 44 | the owner's rule on prior relaxation | `MYCELIUM_A5_DOWNSTREAM_OWNER_DECISION.md:9` | `After a **fresh, valid but non-promoting A5 result**, A5's scheduling gate may close and permit **later controlled A6 integration**, while A5 remains explicitly \`design_only\` / incomplete and non-promoting.` |
| 45 | that decision does **not** grant integration/physical/push | same file `:15`–`:22` | `A6 integration still requires the accepted candidate, controlled integration checks and applicable post-integration verification. A6 physical qualification/sealing is not authorized.` / `No push or PR is authorized.` |
| 46 | the review's finding F1 and its recommendation | `REVIEW.md:37`–`62` | — |
| 47 | the review's F7 recommendation set | `REVIEW.md:180`–`194` | — |
| 48 | the review's F10 (the `closing_commit` "defect") | `REVIEW.md:225` | `A5 record \`closing_commit\` is still a placeholder (\`assigned_by_git…\`).` — **refuted, see §7** |
| 49 | the review's proposed texts D3–D5, D11 | `PROPOSED_PLAN_DELTA.md:75`–`130`, `:192`–`203` | — |
| 50 | the ledger is append-only; corrections are additive events | `PROPOSED_PLAN_DELTA.md:5` | `The ledger is append-only: task-row corrections are expressed as **additive coordination events** that supersede stale rows, never as rewrites.` |
| 51 | the orchestrator cannot write the ledger | `status/decisions.log:15` | `The review's P05-P15 rows are NOT in contracts/governance-ledger.v1.json - they live in mycelium.remaining_work_ledger.v1, which exists only as snapshots outside this session's write boundary and are append-only; left untouched, listed as a blocker needing an owner-authorised event.` |
| 52 | requalification needed after A5/A6/A13 lifecycle edits | `PROGRESS_MATRIX.md:28` | `**High** retained; requalification needed after A5/A6/A13 lifecycle edits` |
| 53 | 21 A5 integration candidate roots | `REVIEW.md:213` **[R]** | counted **independently** by me from the ledger: **21** **[V]** |

---

## 5. Citation log — the A5 record, spec, checklist, finish plan

| # | claim | citation | verbatim |
|---|---|---|---|
| 54 | record state | `a5-completion-record.v1.json:4` | `"state": "design_only"` |
| 55 | decision + gate state | `:6`, `:7` | `"decision": "inconclusive"` / `"gate_state": "design_only"` |
| 56 | the numbers | `:8`, `:9`, `:10`, `:11`, `:12`, `:13` | `0.16710745942796965` / `-0.4196995766436599` / `0.1` / `"promotion_authorized": false` / `"qualification_claim": false` / `"reason": "paired_confidence_lower_bound_below_material_threshold"` |
| 57 | the claim boundary defines the closing commit | `:15` | `The single atomic A5 feature commit is the closing commit that contains this record; its SHA is assigned by Git at commit time.` |
| 58 | the sentinel | `:16` | `"closing_commit": "assigned_by_git_to_the_atomic_commit_containing_this_record"` |
| 59 | the record's source binding | `:55`–`:63` | base_commit `1fd3a6f8733fea52e55e0fbcbf46e86fa7bed03f`; entry_count `101`; total_size_bytes `3008354` |
| 60 | the seven functional evidence shapes | `:72`–`:136` | `concurrent_cancellation_repeat`, `physical_positive`, `frozen_benchmark`, `illegal_replica_negative`, `replica_loss_negative`, `browser_positive`, `browser_lifetime_replica_loss`, `browser_degraded_survivor` |
| 61 | the benchmark detail line | `:95` | `+16.710745942796965% point estimate and -41.96995766436599% paired 95% lower bound; promotion false` |
| 62 | evidence is retained but not current admission authority | `:156` | `The foreground physical route was intentionally stopped after evidence capture, so retained evidence is not current admission authority.` |
| 63 | the threshold | spec `:211` | `Material gain requires both the point estimate and the paired 95% bootstrap lower confidence bound to be at least **10%**.` |
| 64 | changing the manifest creates a **new** protocol | spec `:201`–`:205` | `Changing that manifest creates a new benchmark protocol and invalidates measurements captured under the earlier digest.` |
| 65 | protocol order / window size | spec `:213`–`:215` | `The measured order is exactly `ABBA` repeated three times: 12 measured windows forming six explicit baseline/replicated pairs. … at least 60 terminal requests.` |
| 66 | bootstrap | spec `:220`–`:224` | `10,000 resamples of six pairs with replacement, seed `0xA5`, arithmetic-mean statistic, and the nearest-rank ceiling at the 2.5th percentile` |
| 67 | invalidations | spec `:226`–`:235` | `…invalidates the benchmark instead of becoming a zero-valued sample.` |
| 68 | decision vocabulary | spec `:239`–`:243` | `material` / `not_material` / `inconclusive` |
| 69 | results always refuse promotion | spec `:245`–`:247` | `Every result retains its reasons and reports `qualification_claim=false` and `promotion_authorized=false`.` |
| 70 | **completion requires material measured gain** | spec `:305`–`:308` | `A5 is complete only when a multi-stage physical graph serves concurrent ordinary browser requests on distinct qualified legal tracks, produces material measured gain, survives the scoped replica-loss gate, passes all-eight-workspace verification, and is committed in one atomic A5 feature commit. Until then it remains `design_only`.` |
| 71 | checklist A5 state | `checklist:613` | `"state": "design_only"` |
| 72 | checklist A5 blocker | `checklist:615` | `frozen materiality benchmark concluded inconclusive; paired 95% lower bound -0.4196995766436599 is below the 0.10 promotion threshold` |
| 73 | the **13** completed requirements | `checklist:617`–`631` **[V]** (counted) | `specification, implementation, product_integration, deterministic_positive, deterministic_negative, physical_positive, physical_negative, ui_live_all_eight, browser_navigation_reconnect, browser_second_session, full_regressions_audits, architecture_handover, atomic_feature_commit` |
| 74 | the spec digest the checklist pins | `checklist:637` | `sha256:923f47d265b6a597604852b352ae5b2f78c98abefd546da4289697cc62db93de` — **matches the file on disk [V]** |
| 75 | the finish-plan lock | `2026-08-20-mycelium-a5-finish-plan.md:17` | `\| Inconclusive material-gain outcome? \| **Stay `design_only` with all artifacts shipped** \|` |
| 76 | no re-ask, no different workload | `:185`, `:189` | `## Open questions — none` / `Do not re-ask whether to try a different model variant or workload — the answer is already no.` |
| 77 | the lock is non-negotiable | `:224` | `- Inconclusive material-gain → stay `design_only` with all artifacts shipped` |
| 78 | roadmap weight of A5 | `status/status.json` → `roadmap.gates` **[V]** | `{"id": "A5", "title": "Multi-stage replicas", "weight": 6, … "verified_stage": "integrated", … "evidence": "a5-completion-record: inconclusive, not promoted"}` |

---

## 6. Citation log — git facts (pilot repository @ `9d68291`) [all V]

| # | fact | command | result |
|---|---|---|---|
| 79 | ancestry of the green baseline | `git merge-base --is-ancestor 39f631e HEAD` | **true** |
| 80 | ancestry of the A14a fan-in | `git merge-base --is-ancestor 435b857 HEAD` | true |
| 81 | ancestry of the A13 lane fan-in | `git merge-base --is-ancestor 1a022c9 HEAD` | true |
| 82 | the A6 candidate is **not** integrated | `git merge-base --is-ancestor 7ddd74d HEAD` | **false** |
| 83 | the A9 candidate is **not** integrated | `git merge-base --is-ancestor ba6abbe HEAD` | **false** |
| 84 | the record was **added** by `0a83a2d` in the integration line | `git log --full-history --diff-filter=A --format=%H -- docs/handover/a5-completion-record.v1.json` | `0a83a2db2312fa0e76f6027b1d45c989bd925922` — *"integration: fan in A5 candidate"*, 2026-09-03, parent `efcdf44d…` |
| 85 | `0a83a2d` descends into `b9001e6` | `git merge-base --is-ancestor 0a83a2d b9001e6` | true (merge-base is exactly `0a83a2d`) |
| 86 | the record blob never changed | `git rev-parse {0a83a2d,b9001e6}:<path>`; `git log --full-history 0a83a2d..HEAD -- <path>` | all `ddc8e57dd55deee76e99490dc966ee7990068dcd`; the log is **empty** |
| 87 | the record on disk **is** that blob | `git hash-object docs/handover/a5-completion-record.v1.json` | `ddc8e57dd55deee76e99490dc966ee7990068dcd` |
| 88 | the A5 lane's atomic commit is `44d5b38` | `git log --full-history --diff-filter=A --date=short --format='%H %ad %s' codex/a5-multistage-replication -- <path>` | `44d5b38aa0d0dfc7ad06f4891ec39851f0cb6c2b` — *"feat: complete A5 multistage replication"*, 2026-08-30 |
| 89 | its parent is the record's base_commit | `git log -1 --format=%P 44d5b38` | `1fd3a6f8733fea52e55e0fbcbf46e86fa7bed03f` — **equals** `:59` of the record |
| 90 | `44d5b38` is **not** an ancestor of HEAD | `git merge-base --is-ancestor 44d5b38 HEAD` | **false** (reachable as tip of `codex/a5-multistage-replication`) |
| 91 | the A5 sealed-evidence directory exists | `ls /Users/evinova-self/mycelium-physical-run/a5-multistage-20260822/final-v190-r33/` | 20 entries, including all seven functional evidence artifacts named in the record |

---

## 7. Citation log — the sentinel, and the requalification history

| # | claim | citation | verbatim |
|---|---|---|---|
| 92 | **the sentinel is intentional and must not be replaced** | `A5_INTEGRATION_REQUALIFICATION_GOAL_2026-08-30.md:124`–`130` | `Its `closing_commit` intentionally says:` … `This avoids an impossible self-referential commit SHA. Verify that standalone commit `44d5b38...` contains the exact record bytes. Do not replace the sentinel with a guessed or circular SHA unless repository protocol and validators explicitly require a different non-circular binding mechanism.` |
| 93 | the goal names `44d5b38` as *"completed standalone A5 commit"* | same file `:5`, `:60`, `:173` | `Integrate completed standalone A5 commit `44d5b38aa0d0dfc7ad06f4891ec39851f0cb6c2b` into …` |
| 94 | the goal names the exact-parent / one-commit criterion | same file `:201` | `Verify A5 commit `44d5b38...`, exact parent `1fd3a6f...`, one-commit distance, tree cleanliness, author, subject, and containment.` |
| 95 | the goal's binding block matches the record field-for-field | same file `:110`–`116` | artifact path, SHA-256 `d3748976…`, protocol `mycelium.candidate_source_manifest.v1`, base `1fd3a6f8…`, `entries: 101`, `total size: 3,008,354 bytes` |
| 96 | the program already required A5's non-promoted outcome be preserved | same file `:5` | `…preserve A5's truthful terminal non-promoted `design_only` outcome…` |
| 97 | **the requalification failed closed, terminally** | `A5_INTEGRATION_REQUALIFICATION_BLOCKED_2026-09-02.md:1`–`20` | `> **NOT A SUCCESS HANDOVER.**` … `goal_complete=false`, `qualification_claim=false`, `promotion_authorized=false`, `integration_commit_created=false`, `push_performed=false` |
| 98 | the v15 functional failure | `A5_INTEGRATION_REQUALIFICATION_V15_BLOCKED_2026-09-03.md:11`, `:13`, `:14`, `:20` | `executed 395 requests` / `- `reason_code`: `route_not_alive`;` / `- route incident: `route_peer_process_lost`;` / `The benchmark attempt is consumed. No v15 rerun or downstream gate is authorized.` |
| 99 | the v16 cleanup-invariant failure, unlocalised | `A5_INTEGRATION_REQUALIFICATION_V16_BLOCKED_HANDOVER_2026-09-03.md:41`, `:46`, `:47`, `:48`, `:50`, `:52` | `- failure reason: `gate_state_timeout`` / `- nonterminal request records: `64`` / `- active reservations: `128`` / `- zero-resource wait bound: `240 seconds`` / `The failure occurred while waiting for the route to return to zero live resources. The queue itself was empty, but 64 nonterminal records retained 128 placement reservations. The benchmark therefore did not emit a valid materiality result.` / `This is a product cleanup-invariant failure, not an external network failure. The exact source-level root cause is not yet established.` |
| 100 | 21 candidate roots; six distinct reason codes | ledger, counted by regex **[V]** | roots `candidate-0958ac8d23cdc10c … candidate-fdccada793e5a20d` (21); reason codes `gate_state_timeout`, `route_not_alive`, `qualification_install_http_403`, `staged_sidecar_not_executable`, `operator_authorization_missing`, `test_process_umask_contamination` |
| 101 | `gate_state_timeout` appears repeatedly in the ledger | ledger `1212`, `1264`, `1472` | `"reason_code": "gate_state_timeout"` |
| 102 | the earlier draft's C4 proposal | `runs/a5-disposition/2026-09-20-a5-prospective-amendment-DRAFT.md:40` | that draft proposed `b9001e6` for `closing_commit` — **withdrawn by this draft on authority `:124`–`130` of the goal above** |

---

## 8. Gaps — what I could **not** verify

Each gap names what would close it. None of these is smoothed over in the draft.

**G1 — a citation-line discrepancy in the review.** `REVIEW.md:49` says *"A9's technical prerequisite is A8 (`completion-plan.md:427`)"*. In the copy at pilot HEAD `9d68291` the row `| A9 | A8 |` is on **line 428**; line 427 is `| A8 | A3 |`. The A6 row (`:425`) and A14 row (`:433`) match the review exactly, as does the prose citation `:436`. **Closed by:** whoever applies the amendment should cite the observed line. A different tree would explain it — I did not chase that.

**G2 — `P19` and `P23` are not ledger rows.** Verified: the ledger's `tasks` array is exactly `P00…P18`. They are proposals in `PROPOSED_PLAN_DELTA.md` D14 (`:231`–`239`). **Consequence:** the amendment *cannot* reference them until they are registered, which is why `C0` exists. **Closed by:** `C0`'s additive event.

**G3 — the ledger is a stale, append-only snapshot outside my write boundary.** Dated 2026-09-07 in its filename; last written 2026-09-10 09:44 (the deferral event). It therefore does **not** contain the 2026-09-15 `PLAN.md` D1–D11, nor the 2026-09-16 A9 stage demotion, nor anything after 2026-09-10. "The current encoded edges" in this draft means *"as encoded in that snapshot"*. `mdfind` found **only** that file and its `.lock` on this machine — no newer `remaining_work_ledger` snapshot exists here. **Closed by:** the owner-authorised appender, who may also decide whether later decisions should be folded in.

**G4 — I ran no test suite, so I cannot say the current HEAD is green.** The green baseline is a **record** (`status/decisions.log:11`, on `39f631e`), not a fresh measurement, and it predates the A13 lane fan-in (`1a022c9`) and every later commit. Later green signals exist only as *readiness-gate* GO receipts on named heads (e.g. `status/decisions.log:55`, `:58`, `:64`, `:80`, `:89`, `:95`, `:100`), whose check (a) is "canonical suites + audits" — I did not read those receipts to establish their exact scope. **Consequence:** `P19` in this draft is a *condition*, never a satisfied checkbox. **Closed by:** running the canonical suite at whichever commit the amendment is applied to.

**G5 — `44d5b38` is not an ancestor of the pilot HEAD.** It is the tip of ref `codex/a5-multistage-replication`, whose worktree is `mycelium-wave8-a5-build`. I confirmed the ref and the blob, and the goal document names it explicitly — but I did not inspect that lane's repository history, nor the A5 lane's own "one-commit distance" claim beyond checking the parent. **Closed by:** the owner (or the A5 lane owner) confirming the lane's atomic commit independently. Note this affects only C4, which is **withdrawn**; nothing else in the amendment depends on it.

**G6 — I did not recompute any evidence digest in the A5 record.** The record binds eight sealed evidence artifacts under `/Users/evinova-self/mycelium-physical-run/a5-multistage-20260822/final-v190-r33/`; I verified the directory exists and lists matching filenames, but I did not re-hash a single artifact. Same for the checklist's `evidence_bindings` digests. **Consequence:** *"all 13 closure requirements are bound to evidence"* is a **[R]** claim from the checklist (and from the task statement), **not** my verification. **Closed by:** a bounded re-hash of the eight artifacts against their recorded digests.

**G7 — `docs/handover/a5-integration-source-manifest.v1.json` (sha256 `bf100e9c…`) was not parsed.** It may carry the entry-level detail behind the record's `source_binding` block, and `status/decisions.log:15` records a deliberate decision to keep a pre-correction digest inside it as a frozen candidate-time record. **Closed by:** reading it, if the owner wants entry-level sourcing for the amendment.

**G8 — three source documents were read but not hashed:** `PLAN.md`, `status/status.json`, and the Desktop requalification handovers. They are cited by line, which is sufficient for a draft, but a reviewer binding this amendment to exact bytes would need the digests. **Closed by:** hashing at application time.

**G9 — the review's snapshot (2026-09-15) is stale in ways I had to correct by hand.** Its F1 calls A6/A9/A14a "committed but unintegrated" — true for A6 (`7ddd74d`) and A9 (`ba6abbe`), **false for A14a** (`435b857` is an ancestor of HEAD, verified). Its "last integrated core commit is `b9001e6`" is a pre-fan-in snapshot. **Consequence:** every edge claim in this draft was re-verified against the ledger and against `git` at `9d68291` rather than copied from the review. **Closed by:** nothing further — this is recorded so the reader knows the draft did not inherit the staleness.

**G10 — I could not establish whether `P03`'s remaining content has been partly done elsewhere.** The review's `PROGRESS_MATRIX.md:62` states it is "Not started; blocked on P02", but the A5 downstream closeout overlaps the same surface as the pilot's WP9 claim work and the A13 lane's C1–C8 tables. **Consequence:** the amendment leaves `P03` intact precisely because I cannot bound it. **Closed by:** whoever owns the A5 closeout deciding its residual scope.

**G11 — the requalification history is cited from handover documents, not from the evidence roots.** I read four desktop handovers and counted roots/reason codes from the ledger; I did **not** open
`/Users/evinova-self/mycelium-physical-run/a5-integration-requalification-20260831/run-091603-ce31/phase5/`
and read the failure artifacts directly. **Consequence:** §1.6 of `02-OPTIONS.md` is **[R]**, not [V], for the failure *mechanisms* — though the root count and the reason-code set are [V] from the ledger. **Closed by:** reading the named failure artifacts at the paths the ledger gives (e.g. `a5-benchmark-v16-r1.failure.v1.json`).

**G12 — no reviewer has checked this draft.** It is a first-party draft produced by one subagent under the parent's instruction; it has had no independent read. **Consequence:** treat every judgement (particularly the §4 recommendation in `02-OPTIONS.md`) as a proposal, not a finding. **Closed by:** an independent review pass before the owner acts on it.

**G13 — this draft and the earlier draft in `runs/a5-disposition/` differ on the *width* of the `P05` decoupling, and the owner is choosing between them.** The review's own D3 event text keeps a condition in `P05`'s effective dependencies — `["A5 fresh valid non-promoting disposition per session7-i4o3rh0h-owner-decision (or any later owner amendment)", "P19"]` (`PROPOSED_PLAN_DELTA.md:86`) — whereas this draft's `C1` removes the A5 condition **entirely**, per the instruction that A6 source integration proceed *independently of any A5 disposition*. I did **not** silently resolve this in favour of the review's text, and the reason is stated: the task's instruction is explicit, and the program plan already states the wider rule in words (`PLAN.md:32`, D2 — *"A6 source integration no longer waits on **any** A5 result"*). The narrower option is nonetheless defensible and is closer to the review's D3. **Closed by:** the owner's answer to `C1`; both widths are drafted, and `01-AMENDMENT.md` §8.3's event text is the place to switch.

---

## 9. Self-check against the hard rules of the task

| rule | status |
|---|---|
| draft only; amendment not applied | **met** — no canonical source was written; the amendment is text in this directory **[V]** |
| do not edit `PLAN.md`, the ledger, any record, `contracts/`, the checklist, any manifest | **met** — none of those files was opened for writing **[V]** |
| do not touch the pilot tree | **met** — read-only; a separate worktree was created from `9d68291` for any work of mine **[V]** |
| commit anything kept in the worktree only, local, no push/PR, no co-author or bot trailers, explicit paths, prefer `git commit -F <file>` | **met** — see the commit recorded in `00-README.md` |
| do not change any threshold, timeout, gate, guard or identity check | **met** — nothing was changed anywhere; the draft *preserves* the 10 % threshold and the frozen protocol, and says so in three places |
| verify before reporting; quote `file:line` for every claim about the current dependency structure | **met** — §3 above; and where a citation did not match (G1) it is stated rather than smoothed |
| if the P05/P03/P09 edges cannot be found, say so plainly | **not needed** — all three were found and are quoted verbatim (`4030`–`4031`, `4238`–`4239`, and the P03 row at `3817`–`3822`) |
