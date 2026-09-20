# A6 integration report — candidate `7ddd74d` BLOCKED at base `7d0ffb6`

**Status: NOT INTEGRATED. No candidate content landed. Textual merge at this HEAD is NOT clean.**
Reported by the A6 integration child (packet `a6-1-integrate-candidate.md`), 2026-09-20.
Nothing in this file claims the A6 gate is closed; see §9.

| item | value |
| --- | --- |
| Worktree | `/Users/evinova-self/Documents/playground/mycelium-a6-integrate` (branch `wp/a6-integration`) |
| Base (`git rev-parse HEAD` at start, clean) | `7d0ffb6f52b24bc00353aeed170a29acdabc49e2` |
| Candidate (verified object in this repo) | `7ddd74dad61ddc0aa6cd06f67ddc1e13aed3e638` |
| Candidate parent / merge-base with HEAD | `f156ba08621e6bc2351e98f9052b433e065b7b68` |
| Commits on HEAD since `f156ba0` | 138 |
| Integration commit | **none — blocked** |
| HEAD after this report | report-only commit; `7ddd74d` is **not** an ancestor |

---

## 1. Candidate verification (real, run by me at this HEAD)

```
$ git cat-file -e 7ddd74d^{commit}          # exit 0, object present
$ git log --oneline -1 7ddd74d
7ddd74d Preserve A6 integration candidate with blocked external acceptance

$ git show --name-only --format="%H%n%an <%ae>%n%ad%n%s" 7ddd74d | head -4
7ddd74dad61ddc0aa6cd06f67ddc1e13aed3e638
Tranquil-Flow <tranquil_flow@protonmail.com>
Mon Sep 7 18:35:09 2026 +0200
Preserve A6 integration candidate with blocked external acceptance

$ git diff --shortstat f156ba0 7ddd74d
 65 files changed, 12113 insertions(+), 70 deletions(-)      # 35 added (A), 30 modified (M), 0 deleted

$ git diff --stat f156ba0 7ddd74d -- mycelium_request_gateway/backend.py \
      mycelium_request_gateway/service.py mycelium_live/router_port.py
 mycelium_live/router_port.py        | 626 +++++++++++++++++++++++++++++++++++-
 mycelium_request_gateway/backend.py |  58 +++-
 mycelium_request_gateway/service.py | 334 ++++++++++++++++++-
 3 files changed, 1004 insertions(+), 14 deletions(-)
```

The candidate touches **no** register/checklist file and **no** spec file:
`git diff --name-only f156ba0 7ddd74d | grep -E "completion-checklist|docs/superpowers"` → empty
(only `docs/handover/A6_SESSION5B_CANDIDATE_2026-09-07.md` is added under `docs/`).

## 2. Textual-merge verdict at THIS HEAD: **RED (4 files conflict)**

`FRESH_CHECKS.md` F13 recorded the merge as *clean*, but that probe ran against an older base.
At `7d0ffb6` it is not clean — the 138 intervening commits are the reason.

```
$ MB=$(git merge-base 7ddd74d HEAD); echo $MB
f156ba08621e6bc2351e98f9052b433e065b7b68            # = the candidate's own parent

$ git merge-tree $(git merge-base 7ddd74d HEAD) HEAD 7ddd74d > /tmp/mt.txt; echo $?
0                                                   # legacy form always exits 0
$ grep -c "^<<<<<<<\|changed in both" /tmp/mt.txt
14                                                  # conflict-marker lines emitted

$ git merge-tree --write-tree HEAD 7ddd74d ; echo $?
577d66d0071c7a776de48d7354f9ab93095033d9
CONFLICT (content): Merge conflict in contracts/contract-manifest.v1.json
CONFLICT (content): Merge conflict in mycelium_live/router_port.py
CONFLICT (content): Merge conflict in mycelium_request_gateway/backend.py
CONFLICT (content): Merge conflict in mycelium_request_gateway/service.py
1                                                   # exit 1 = conflicts

$ git cherry-pick -n 7ddd74d ; echo $?
... same 4 CONFLICT (content) lines ...
error: could not apply 7ddd74d... Preserve A6 integration candidate with blocked external acceptance

$ git diff --name-only --diff-filter=U
contracts/contract-manifest.v1.json
mycelium_live/router_port.py
mycelium_request_gateway/backend.py
mycelium_request_gateway/service.py
```

The cherry-pick has the same three inputs as the merge (`base = f156ba0`, ours = HEAD,
theirs = `7ddd74d`), so cherry-pick and merge conflict identically. There is no
"different vehicle" that avoids these hunks. Per the packet I resolved nothing and stopped.

### 2.1 Conflict census (authoritative, from conflict markers in the conflicted worktree)

```
contracts/contract-manifest.v1.json : 12 conflict hunk(s)
mycelium_live/router_port.py        :  1 conflict hunk(s)
mycelium_request_gateway/backend.py :  1 conflict hunk(s)
mycelium_request_gateway/service.py :  4 conflict hunk(s)
```

Raw conflicted bytes of all four files: `git diff --diff-filter=U` →
`…/mycelium-a6-integrate` run copy saved at
`/Users/evinova-self/Documents/mycelium-core-program/runs/a6-integration-20260920/conflicted-merge.patch`
(363 lines).

### 2.2 Each conflicting hunk, in HEAD terms

**`mycelium_request_gateway/backend.py` — 1 hunk, inside `_GatewayTokenSink.emit` (HEAD `def` at L71, conflict at L81). FOUR-WRITER FILE.**
HEAD side (`e72d2b0`, A13 macOS demo lane — the only commit since `f156ba0` touching this file):

```python
        self._emit_token(token_index, text, token_id)
```

Candidate side (`7ddd74d`):

```python
        private_emit = getattr(self._emit_token, "emit_router_token", None)
        if callable(private_emit):
            private_emit(token_index, token_id, text)
        else:
            self._emit_token(token_index, text)
```

Adjacent (non-conflicting, candidate wins) change: `__init__` at L68 annotates
`emit_token: TokenEmitter` (HEAD) vs `emit_token: Callable[[int, str], None]` (candidate).

**`mycelium_request_gateway/service.py` — 4 hunks. FOUR-WRITER FILE.**
1. Module import block, HEAD L24–30: HEAD adds `import mycelium_deadlines`; candidate adds
   ```python
   if TYPE_CHECKING:
       from mycelium_a6_recovery import ReplayCheckpoint
   ```
2. `RequestGatewayService._run` (HEAD `def` L461), HEAD L484–495 — the token-emitter argument to
   `self._backend.run(...)`:
   HEAD: `lambda token_index, token_text, token_id=None: self._accept_token(session, token_index, token_text, token_id),`
   candidate: `_GatewayEmission(self, session),`
3. `RequestGatewayService._accept_token` signature region, HEAD L541–552 — candidate inserts a new
   method `_bind_encoded_prompt(...)` **and** changes the signature to keyword-only:
   HEAD `def _accept_token(self, session, token_index, token_text, token_id=None)`
   candidate `def _accept_token(self, session, token_index, token_text, *, token_id: int | None = None)`
4. `RequestGatewayService._accept_token` body, HEAD L554–560 — the `token_id` validation expression
   (semantically equal, textually different).
   The remaining `_run` change (checkpoint discard in `finally`, `mycelium_a6_recovery.CheckpointStore`)
   auto-merged.

**`mycelium_live/router_port.py` — 1 hunk, module import block, HEAD L10–21** (not a four-writer file,
but contract-pinned and touched by 3 commits since `f156ba0` — `90381bc`, `15af3c8`, `185b17f`):
HEAD adds `from pathlib import Path` + `import mycelium_deadlines`; candidate adds `Callable` and
`from mycelium_a6_m16 import M16RecoveryAuthority` / `from mycelium_a6_recovery import ReplayCheckpoint`
(same hunk, both sides rewrote the import tail). A second combined-diff hunk at HEAD L854
(`class LiveRouterPort`) auto-merged with no markers.

**`contracts/contract-manifest.v1.json` — 12 hunks** at HEAD L115, 180, 220, 255, 290, 420, 495, 1195,
1205, 1225, 1240, 1425. All are generated owner-source pins (`sha256` + `size_bytes`), e.g.
`mycelium_live/router_port.py` 58917/`4ac737…` (HEAD) vs 79261/`f8cbbc…` (candidate),
`mycelium_live/supervisor.py` 115366 (HEAD) vs 116929 (candidate). This file is touched by 17 commits
since `f156ba0`. It can be re-pinned non-creatively (`scripts/generate_contract_manifest.py`), but only
*after* the three code conflicts above are decided — the pins follow the winning bytes.

### 2.3 Why this is the four-writer hazard, not an accident

`git log --oneline f156ba0..HEAD -- <file>` (writers of the overlapping files since the candidate's base):

| file | commits since `f156ba0` | writers |
| --- | --- | --- |
| `mycelium_request_gateway/backend.py` | 1 | `e72d2b0` (A13 macOS demo lane) |
| `mycelium_request_gateway/service.py` | 5 | `827b2ad`, `e72d2b0`, `1a022c9`, `1704ac2`, `185b17f` (deadlines registry / A13 / gateway terminal) |
| `mycelium_live/router_port.py` | 3 | `185b17f`, `15af3c8`, `90381bc` |
| `contracts/contract-manifest.v1.json` | 17 | generated re-pins |

Per the packet's forbidden list I did **not** reconcile `backend.py`/`service.py` to make anything pass.

## 3. A6's own symbols in the four-writer files (what the orchestrator must reconcile)

Candidate-side symbols added/changed by `7ddd74d` in the two four-writer files
(enclosing symbols of every added hunk, from `git diff -U0 f156ba0 7ddd74d -- <file>`):

* `mycelium_request_gateway/backend.py` (+58/-…): `_GatewayTokenSink.emit` (the private-emitter probe),
  `RouterSessionBackend` (`_run`), `recovery_public_projection`, module imports.
* `mycelium_request_gateway/service.py` (+334/-…): new `_GatewayEmission` class (`__init__`, `__call__`),
  `_Session` (`bind_encoded_prompt`, `emit_router_token`, `freeze_replay_checkpoint`),
  `RequestGatewayService` (`startup_recovery_reconciliations`, `recovery_public_projection`, `_run`,
  new `_bind_encoded_prompt`, `_accept_token`, `_append_terminal`, `_next_event`), module imports.

The four exact overlap regions needing a deliberate decision are §2.2 items for `backend.py` and
`service.py` (5 hunks total: backend L81; service L24–30, L484–495, L541–552, L554–560).

## 4. Acceptance numbers — literal

Interpreter `/opt/homebrew/bin/python3` for all runs, env stripped
(`env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 … -B -m pytest -q -p no:cacheprovider`).

### 4.1 RED-before, on the base `7d0ffb6` (this worktree, before any candidate bytes)

```
$ … pytest -q -p no:cacheprovider tests/a6_acceptance
........                                                                 [100%]
8 passed in 0.03s                                                        EXIT=0
```
(`tests/a6_acceptance` is the design-only inventory test; it asserts
`gate_state == "design_only"` and `qualification_claim == false` and is **unchanged** by the
candidate, so it is green on both trees. It is not evidence of implementation.)

```
$ … pytest -q -p no:cacheprovider <the 20 delta files, candidate list>   # exit 4
ERROR: file or directory not found: tests/contracts/test_a6_intent_bounds.py
no tests ran in 0.00s                                                    EXIT=4
```
12 of the 20 candidate delta files **do not exist** at this HEAD (they are `A` in the candidate):
`tests/contracts/test_a6_intent_bounds.py`, `tests/contracts/test_a6_qualification_claims.py`,
`tests/governance/test_a6_source_manifest.py`, `tests/layer_planner/test_a6_recovery_intent.py`,
`tests/live/test_a6_integration_boundaries.py`, `tests/live/test_a6_live_router_recovery.py`,
`tests/live/test_a6_m16_recovery.py`, `tests/live/test_a6_physical_qualification.py`,
`tests/live/test_a6_production_composition.py`, `tests/live/test_a6_recovery_gate.py`,
`tests/product_spine/test_a6_recovery_projection.py`, `tests/request_gateway/test_a6_checkpoint_restart.py`,
`tests/request_gateway/test_a6_gateway.py`, `tests/request_gateway/test_a6_recovery_product_path.py`.

The 6 delta files that do exist at HEAD (their pre-candidate versions):
```
$ … pytest -q -p no:cacheprovider tests/contracts/test_contract_fixtures.py \
    tests/demo/test_product_physical_composition.py tests/live/test_supervisor.py \
    tests/request_gateway/test_backend_cli.py tests/ui_gateway/test_inference.py \
    tests/ui_gateway/test_session_security.py
85 passed, 3 skipped in 9.61s                                            EXIT=0
```
(Not comparable to the candidate's totals — different file revisions and 14 missing files.)

### 4.2 Candidate's OWN bytes `7ddd74d` (reproduces F9 — NOT an integration measurement)

Extracted with `git archive 7ddd74d | tar -x -C /tmp/a6-cand` (no `.git`, so nothing in this run
touched the source repo; two delta tests shell out but do not require git).

```
$ … pytest -q -p no:cacheprovider tests/a6_acceptance
8 passed in 0.03s                                                        EXIT=0

$ … pytest -q -p no:cacheprovider <the 20 delta test files>
........................................................................ [ 42%]
.....ss.s..s............................................................ [ 85%]
........................                                                 [100%]
164 passed, 4 skipped in 25.43s                                          EXIT=0
```
**8 + 164 = 172 passed, 4 skipped — F9's number reproduced exactly, on the candidate's own bytes.**

### 4.3 GREEN-after on integrated bytes: **NOT MEASURED — no integration exists**

There is no merged tree, so the packet's "re-measure on the merged bytes" step is impossible until
the §2.2 hunks are reconciled. I did not run the A6 acceptance against a partially integrated tree,
because no partial tree was created.

### 4.4 Registers/audits at the base (packet commands, read-only)

```
$ … python3 -B scripts/mycelium_completion_audit.py --repo-root .
Mycelium completion audit OK: 13 gates, primary A6                    EXIT=0

$ … python3 -B scripts/governance_gate.py --repo-root .
governance gate OK: ledger, claim boundary, and contracts             EXIT=0
```
`git status --porcelain` empty after both.

**Register interaction is INAPPLICABLE here (packet's hard requirement not met).** The
`blocker_checks` machinery (`e45cc42`, `wp/a0-claims-audit`) is **not** on this head:
`git merge-base --is-ancestor e45cc42 HEAD` → *not an ancestor* (the object exists), and
`grep -rn "blocker_checks" scripts/ docs/` → no matches. The A6 register row
(`docs/handover/mycelium-completion-checklist.v1.json`, `gate_id: A6`) is the pre-`blocker_checks`
form: `state: "design_only"`, `completed_requirements: ["specification"]` (1 of 13),
blockers = `["A6 full-context replay implementation is committed as candidate 7ddd74d (1 commit on
f156ba0; 172 passed / 4 skipped), but it is not integrated on the pilot branch",
"qualified successor replay path has not been executed"]`, with no `kind`/`operand` fields.
Per the packet I therefore edited **nothing** in the register rather than editing it blindly.

## 5. Commands run — summary with exit codes

| # | command (all with `/opt/homebrew/bin/python3`, env stripped, cwd = worktree unless noted) | literal result | exit |
| --- | --- | --- | --- |
| 1 | `git worktree add ../mycelium-a6-integrate -b wp/a6-integration 7d0ffb6…` | `Preparing worktree (new branch 'wp/a6-integration')` / `HEAD is now at 7d0ffb6` | 0 |
| 2 | `git status --porcelain` (worktree, at start) | *(empty)* | 0 |
| 3 | `git cat-file -e 7ddd74d^{commit}` | *(silent)* | 0 |
| 4 | `git diff --shortstat f156ba0 7ddd74d` | `65 files changed, 12113 insertions(+), 70 deletions(-)` | 0 |
| 5 | `git merge-tree $(git merge-base 7ddd74d HEAD) HEAD 7ddd74d` | 14 marker lines; 4 files `changed in both` | 0 |
| 6 | `git merge-tree --write-tree HEAD 7ddd74d` | 4 × `CONFLICT (content)` | **1** |
| 7 | `git cherry-pick -n 7ddd74d` | 4 × `CONFLICT (content)`; `error: could not apply 7ddd74d…` | 1 |
| 8 | `git diff --name-only --diff-filter=U` | 4 paths (§2) | 0 |
| 9 | `git reset --hard HEAD` | tree restored to `7d0ffb6`, `git status --porcelain` empty | 0 |
| 10 | `pytest tests/a6_acceptance` @ base | `8 passed in 0.03s` | 0 |
| 11 | `pytest <20 delta files>` @ base | `ERROR: file or directory not found:` … `no tests ran in 0.00s` | **4** |
| 12 | `pytest <6 existing delta files>` @ base | `85 passed, 3 skipped in 9.61s` | 0 |
| 13 | `pytest tests/a6_acceptance` @ candidate bytes | `8 passed in 0.03s` | 0 |
| 14 | `pytest <20 delta files>` @ candidate bytes | `164 passed, 4 skipped in 25.43s` | 0 |
| 15 | `scripts/mycelium_completion_audit.py --repo-root .` @ base | `Mycelium completion audit OK: 13 gates, primary A6` | 0 |
| 16 | `scripts/governance_gate.py --repo-root .` @ base | `governance gate OK: ledger, claim boundary, and contracts` | 0 |
| 17 | `git merge-base --is-ancestor 7ddd74d HEAD` | *not an ancestor* (after everything) | 1 |

Pitfall recorded: `git cherry-pick -n` leaves **no** sequencer state, so
`git cherry-pick --abort` fails with `error: no cherry-pick or revert in progress`; the staged
conflicted index must be dropped with `git reset --hard HEAD` instead.

## 6. Frozen-tree discipline

* All work in `/Users/evinova-self/Documents/playground/mycelium-a6-integrate` (worktree created from
  the pilot repo, as the packet instructs).
* `/Users/evinova-self/Documents/playground/mycelium-core-pilot`: working tree read only
  (`git worktree add` writes only `.git/worktrees` metadata, which the packet requires).
* `/Users/evinova-self/Documents/playground/mycelium-core-pilot-wt-p81r2`: never entered.
* `/Users/evinova-self/Documents/playground/mycelium-5b-ynqf0bos` (candidate worktree): never entered;
  the candidate bytes were read via `git archive` instead.
* No `git push`, no PR, no merge into `integration/core-pilot`.

## 7. What must happen next (for the orchestrator / owner)

1. Decide the four overlap regions with the writers present (A6 + A13) — §2.2 items 1–4 and the
   `router_port.py` import block. Each is a real both-sides-changed-the-same-region conflict:
   * `_GatewayTokenSink.emit` routing contract (private-emitter capability probe vs plain call);
   * `RequestGatewayService._run`'s emitter argument (`_GatewayEmission` vs inline lambda);
   * `_accept_token` signature (keyword-only `token_id` + new `_bind_encoded_prompt`) — a **caller
     contract change**, so all call sites must move together;
   * `service.py`/`router_port.py` import blocks (`mycelium_deadlines` vs a6 modules — additive union,
     but deliberate).
2. Then re-pin `contracts/contract-manifest.v1.json` with `scripts/generate_contract_manifest.py` and
   confirm `scripts/contract_audit.py` reports 0 drift.
3. Only then re-run `tests/a6_acceptance` + the 20 delta files **on the merged bytes** and compare with
   the 172/4 baseline recorded here.
4. Update the register **in the same change** once `blocker_checks` (or its successor) is on the branch —
   it is absent here, so a `commit_integrated` claim cannot be machine-checked yet.

## 8. Skipped / not run

* Full core `tests/` suite — not run (packet: not required; the tree is unmodified from `7d0ffb6`, which
  I verified with `git diff 7d0ffb6 --stat` = empty).
* Any subset of the A6 acceptance on merged bytes — impossible, no merge exists.
* Frontend/Vitest and the A6 browser harness (`ui/web/scripts/a6-product-browser-e2e.mjs`) — not run.
* `scripts/contract_audit.py` — not run (no contract source or manifest changed in this run).

## 9. What this run does NOT prove (explicit)

* **No physical run.** No fleet window, no two-Mac route, no node sidecar, no fault injection.
* **No successor-route replay.** The qualified-successor replay path was not executed; the register
  blocker "qualified successor replay path has not been executed" stands unchanged.
* **No browser matrix.** No browser was opened; the A6 browser product E2E was not run.
* **No integration.** Candidate `7ddd74d` is not an ancestor of `wp/a6-integration`; the 172 passed /
  4 skipped figure is measured on the candidate's **own** bytes (`git archive` extraction), i.e. it
  re-confirms F9 and says nothing about the pilot branch.
* **The 5 model-dependent cases remain blocked** (classification `BLOCKED_EXTERNAL_MODEL_LOADING`,
  `mycelium-session6-oqst1yd0/execution-exclusions.json:blocked_original_cases`) — all five are
  unexecuted here, and the candidate itself records 16 A6 physical cases as pending/false:
  1. `tests/physical_qualification/test_endpoint_secret_conduit.py::test_host_node_service_reuses_endpoint_identity_without_public_leakage` (failure)
  2. `tests/physical_qualification/test_node_service.py::test_two_node_subprocesses_run_distributed_inference_over_native_iroh[mlx]` (failure)
  3. `tests/physical_qualification/test_node_service.py::test_two_node_subprocesses_run_distributed_inference_over_native_iroh[numpy]` (failure)
  4. `tests/physical_qualification/test_node_service.py::test_two_node_subprocesses_run_distributed_inference_over_native_iroh[mixed-mlx-numpy]` (failure)
  5. `tests/seed_coordinator/test_local_two_node.py::test_local_two_node_native_process_e2e` (error: `FileNotFoundError: 'cargo'`)
  Also unaddressed: the 133 explicit selectors / 212 parametrized deselections and the 3 native-integration
  omissions recorded by Session 6, and the candidate's own recorded broader-suite run (5158 passed / 21 failed /
  8 errors / 14 skipped / exit 124) which it explicitly says is **not** a green canonical gate.
* **No gate state change is asserted.** At most this run's evidence supports `integrated_unqualified`
  — and since integration itself is blocked, not even that. A6 remains `design_only` (1 of 13).
* **No claim about A13/A13-lite/C-UC1 correctness.** The HEAD-side bytes in the conflict regions belong
  to those lanes and were only read, never evaluated.
