# Mycelium / DDAI manual-driver handover

Status snapshot: 2026-07-18 10:04 CEST
Canonical repository: `/Users/evinova-self/Projects/mycelium`
Canonical branch: `main` at `f2bc55b62c5e3103eda584c1c68c222244bde489` (unchanged and clean)
Overnight source worktree: `/Users/evinova-self/Projects/mycelium-wt-overnight`
Overnight source code head: `d412777edb6099447dd6df3630e9addc883c6a42`
Overnight documentation head: `1d458f5474294f11ae3ff12cf333fe28a799931f`
Manual integration worktree: `/Users/evinova-self/Projects/mycelium-wt-manual-driver`
Integration branch: `integration/mycelium-manual-driver`
Verified pre-handover head: `9f2cc35b7e1fc977df00fbe73cbf39ed48d12504`
Authoritative architecture plan: `/Users/evinova-self/Projects/mycelium/.hermes/plans/2026-07-17_142630-mycelium-ddai-mvp-synthesis-plan.md`

## 0. Final manual reconciliation result

The requested reconciliation is complete on the isolated integration branch. Canonical `main`, old A/B/C worktrees, and the overnight source worktree were not edited, switched, reset, cleaned, or committed. Nothing was fetched, pulled, pushed, merged into `main`, installed, or run on a remote host.

Before this handover-only commit, `integration/mycelium-manual-driver` was 17 commits ahead of `main`. Integrated code commit `95c79ce51f027416164881ac7c654918fa1e2f34` and overnight code head `d412777edb6099447dd6df3630e9addc883c6a42` have the same tree `372c2dc56f2d2872d37b2498949e822b059472a1`. This exact tree match proves the skipped duplicate A1 commit and the reviewed journal conflict did not alter the intended overnight code/doc state through `d412777`.

### 0.1 Exact source-to-integration mapping

| Overnight source commit | Integration commit | Decision |
|---|---|---|
| `e9e6928436a71cf3eabd08953f22e6cd5f007eec` | `13d9a387245ecac54d8adede720672a3930b7d47` | Integrated; preserve automation journal. |
| `cd7d1b1966687ec973c991d89df62d229808f186` | `5d6c2e12cba4245aa79de8b75388d60b0199a466` | Integrated; preserve continuation provenance. |
| `ead89f20109ecda7cf8a21414a3d2504a99b135b` | none | Skipped because canonical `main` already contains its A1 implementation. Both production/test files were byte-identical to overnight: `two_process_inference_qualification.py` SHA-256 `1b35e62743a024fb57cbc7e73833d9ca389d39a654008e249252a1c71c4eef20`; `test_two_process_inference_qualification.py` SHA-256 `0264c24725b7ccf7b1906151abb99a11ad9e657ac442f08d4dced9b3f4fe29c6`. |
| `b759305d464856f1a74d36458d4cc93a4ef7a609` | `3b457a7735afe0acf1d01ad282397177ac6e2cf9` | Integrated. |
| `21f4ab8aac38e8311b4046f4fc0439a64d8b3ffb` | `a70f4a881331648aa16a3a9778cc66c892391575` | Integrated. |
| `f1e215253fb5a65cf3552b014ca4e6d3b37fbc81` | `8052024f18bd3f9b1ded6b16b7609f6a71cc7805` | Integrated. |
| `0cab4f23cacc262c17a91cbaa6db3fa509ccc084` | `ab8229f07b308bc6526167aed5c1bf66aa150392` | Integrated. |
| `f434b704bc53a76312bdb25dc488064ddaf6023a` | `39e8d760a3481a8f5d647b18c4b67973ef7c7729` | Integrated. |
| `e9edc4672b20022b1f21d7876f4a6993e0b1bd5` | `beb9754146c2f1828f50f1e0ca94b4309c063f9a` | Integrated. |
| `4bb65255ff24f0e0198a332f52a067e1da7e462d` | `f8bbfa1ddaa545d9834f689ca66b2a7a369c2a56` | Integrated. |
| `1de81d72a7e6e898014ff2e62313c109c5276250` | `dd99247c9657f189cf00676810ee281069c02364` | Integrated; preserve journal provenance. |
| `2e5da25631a728702320b5b04d72e5a4c42101103` | `2cb71b544a03acc5114c4937bbaca74ed12b0ece` | Integrated. |
| `fdf312af5cda322d1fd283954a20041e30a6fe0a` | `07b61ea64d8dbc372c28bf0ab5673511cf6e98c7` | Integrated; preserve journal provenance. |
| `160cd046585b709c61fd36ab701be848b46bc14d` | `5ca743c6d18e58b7f9bcb73d9dc577a8e8773a22` | Integrated. |
| `a5342393bcb94129c6f3b66e022afab86f0d1c20` | `10c8cf221db4ea76ab01486c7c9c4ba0133f0a90` | Integrated. |
| `a33844b3c296998f9e22fe0fd8778848ac2ab5fe` | `8629c64a411c7e5806ab65c24a6dca9a8d62f776` | Integrated. |
| `d412777edb6099447dd6df3630e9addc883c6a42` | `95c79ce51f027416164881ac7c654918fa1e2f34` | Integrated; final code-tree match verified. |

Only one conflict occurred: `b759305` expected the skipped `ead89f2` journal row in `docs/automation/overnight-build.md`. The conflict was reviewed and resolved to the overnight source version so the automation journal retained both the historical A1 row and the Observatory row. No production-code conflict occurred. The resulting exact tree match against `d412777` verifies that resolution.

### 0.2 Clean-checkout documentation repair

`test_router_docs.py` initially exited 1 because two ignored authorities were absent from the integration checkout. No test was suppressed, deselected, or weakened.

Exact provenance established before tracking:

- Source repository: `/Users/evinova-self/Projects/mycelium-merge-20260716T194951Z`
- Source commit: `38508e5d0590da3c66619718c2a5c1c12f176c59` (`chore: capture verified M4 Pro canonical baseline`)
- `ROUTER_HANDOVER.md`: Git blob `6e5c6ccb111b865ae9824165d2d9b53b957b81ae`, SHA-256 `d7d42abf3505f6670f389092de05345f6ce8f230ff366ab765c38755259a8751`, 13,377 bytes. Canonical root copy and source blob were byte-identical.
- `docs/plans/2026-07-16-request-streaming-session-lifecycle-mvp.md`: Git blob `fff8ce9aeb2714ba1a45d1a09d21bb5da8942e4e`, SHA-256 `b709f6375165312b152003b57eff44a3177c3c6e45c6860d929eaae01471fb7e`, 28,504 bytes. This tracked source commit is the authority for the copy that existed only in the merge checkout.
- `test_router_docs.py` at that source commit and live integration tree were byte-identical (SHA-256 `5e4d1d008a3d303d50a08b92a463dd11eaecbbd773b1bbf7cd4c6ae13357cb14`).

Both exact source blobs are now tracked by integration commit `9f2cc35b7e1fc977df00fbe73cbf39ed48d12504`. Targeted regression result after repair: `4 passed` in 0.03 seconds, exit 0.

### 0.3 Final observed gates

All required final gates passed from the manual-driver worktree after the documentation repair:

| Command | Exit | Observed result |
|---|---:|---|
| `python3.14 -m pytest -q` | 0 | 693 passed, 2 skipped, 117 subtests passed in 90.13 seconds. |
| `python3.14 scripts/contract_audit.py` | 0 | `contract audit OK: 11 contracts`. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `git diff --check` and staged diff check | 0 | No whitespace errors. |
| `cargo fmt --check` | 0 | No diagnostics. |
| `cargo clippy --all-targets --all-features -- -D warnings` | 0 | Finished cleanly. |
| `cargo test` | 0 | 21 Rust tests passed, 0 failed. |
| `npm run check` | 0 | 70 Vitest tests and 3 Node contract tests passed; TypeScript typecheck and production build passed. |

The first UI attempt exited 127 because a fresh worktree has no `node_modules`; no install was performed. Canonical `package.json` and `package-lock.json` were byte-identical to integration (SHA-256 `c9b5843e7095533f777650256427f39ba6b7a8adeb0e23d0d647d7191df108b3` and `9e156296f89e577af801daed4538c8b1956b28225e184324f7e4f9da0b78ab99`). The passing run temporarily symlinked canonical's existing dependencies, then removed the symlink. Vite emitted only its non-blocking large-chunk warning.

A claim-boundary scan found no production `route_ready=true` assignment. Observatory gateway/UI production source contains no mutating HTTP method or browser write transport; its Python tests also reject `POST`, `PUT`, `PATCH`, and `DELETE` on read-only endpoints.

### 0.4 Handover boundary and remaining work

This is local evidence only. `route_ready=false` remains mandatory. The branch does not prove physical two-host inference, stage-local KV reuse, a production Router-to-iroh adapter, authenticated two-Mac dispatch, accepted RouteQualificationV1 evidence, an inference request/token-stream gateway, recovery after real route loss, or release-grade bootstrap/doctor behavior. Observatory remains read-only.

Feature lanes must base new work on the final `integration/mycelium-manual-driver` tip after this handover commit, not on `automation/mycelium-overnight`. Recommended dependency order remains: stage-local KV semantics; production Router-to-iroh adapter; qualification/evidence authority rebased across both; explicitly authorized physical qualification; separate request/token-stream gateway; read-only Observatory event consumption; recovery; release orchestration.

## 1. Source handover executive state (historical snapshot)

The overnight automation ran successfully, was canceled after the requested overnight window, and no Mycelium cron job remains registered. Work is isolated on `automation/mycelium-overnight`; it is not yet folded into canonical `main`.

Git topology at this snapshot:

- `main` has one commit absent from the overnight branch: `f2bc55b`.
- `automation/mycelium-overnight` has 17 commits absent from `main` through code head `d412777`.
- `git diff main..automation/mycelium-overnight`: 37 files changed, 12,026 insertions, 759 deletions.
- Canonical `main` is 20 commits ahead of `origin/main`; do not fetch, push, or rewrite it during reconciliation.

Strongest honest claim:

- Real local two-process routed MLX inference qualification exists and matches an independent concatenated local reference under the deliberately narrowed full-context-replay MVP semantics.
- Authenticated local iroh sidecar behavior exists across local processes, but no production Router-to-iroh adapter or two-host authenticated data path has been accepted.
- Read-only semantic Observatory projection and UI exist and pass their local gates.
- Router mutating receive paths have substantially stronger hop/source/provenance fencing.
- `route_ready` remains false. No accepted two-Mac distributed-inference run exists yet.
- No physical iroh path, stage-local KV continuity proof, request token stream, recovery proof, or release-grade evidence seal has been completed.

Do not reuse the old planning estimate (`94% component`, `35% verified MVP`) as a completion claim. Overnight work advanced local implementation and security tranches, but it did not close the physical acceptance gates that dominate verified MVP readiness.

## 2. Were Agents A, B, and C folded in?

Yes for selected A1, B, and C work. A2 was deliberately rejected rather than blindly merged.

### Agent A / Phase 6

- Selected lane A1: `phase6/two-process-router-exec`, source commit `f2bc55b`.
- Overnight integration commit: `ead89f2`.
- The two production/test files are byte-identical between canonical A1/main and the overnight branch:
  - `two_process_inference_qualification.py`
  - `test_two_process_inference_qualification.py`
- Competing A2 lane: `phase6/local-exec`, dirty two-file snapshot.
- A2 was reviewed and rejected because it used drifted APIs, `InProcessMesh`, and an invalid `route_ready=true` claim. Both A2 path names exist in the selected implementation, but A2 bytes were not merged by design.

Conclusion: A1 is fully represented; A2 is intentionally superseded, not lost accidentally.

### Agent B / Phase 7 sidecar

- Source lane: `phase7/iroh-sidecar` at base `dc9ca4c`, reviewed quiescent 13-file snapshot.
- Overnight integration commit: `21f4ab8`.
- All 13 changed source-lane files exist in the overnight branch.
- Ten remain byte-identical to B.
- Three differ because overnight automation hardened them with additional tests/fixes:
  - `mycelium_iroh_sidecar/client.py`
  - `native/iroh_transport/src/sidecar.rs`
  - `test_iroh_sidecar_cross_language.py`
- Added hardening includes explicit cancellation, active-ID retention, malformed-hello handling, and endpoint-generation fencing.

Conclusion: B is fully folded in and then hardened.

### Agent C / semantic Observatory

- Source lane: `phase9/semantic-observatory` at base `dc9ca4c`, reviewed quiescent 14-file snapshot.
- Overnight integration commit: `b759305`.
- All 14 changed source-lane files exist in the overnight branch.
- Twelve remain byte-identical to C.
- Two differ because overnight automation added an SSE reconnection/live-qualification regression and fix:
  - `ui/web/src/data/observatorySource.ts`
  - `ui/web/src/data/observatorySource.test.ts`

Conclusion: C is fully folded in and then hardened.

## 3. Overnight commit inventory

Integration and automation setup:

- `e9e6928` Add isolated overnight build queue
- `cd7d1b1` Coordinate overnight session continuation
- `ead89f2` Integrate two-process routed inference qualification
- `b759305` Integrate semantic observatory live qualification
- `21f4ab8` Integrate authenticated iroh sidecar

Router hardening:

- `f1e2152` Harden decode failure recovery token fence
- `0cab4f2` Reject off-path failure identities
- `f434b70` Bind failure reports to source peers
- `e9edc46` Bind token events to final hop
- `4bb6525` Bind prefill completions to final hop
- `2e5da25` Bind manifest locks to final hop
- `160cd04` Bind participant registration provenance
- `a534239` Harden Router hop source provenance
- `a33844b` Bind locked hop zero to admitted entry
- `d412777` Validate route-building hop provenance

Journal-only provenance commits:

- `1de81d7` Record prefill provenance tranche
- `fdf312a` Record manifest provenance tranche

## 4. Source-branch verification before reconciliation (historical)

Commands were rerun in `/Users/evinova-self/Projects/mycelium-wt-overnight` after cron cancellation.

### Python

Use `python3.14`, not macOS `python3` (3.9.6). Python 3.9 cannot evaluate current union annotations such as `RuntimeBatchKey | None`.

Raw branch result:

- 691 passed
- 2 failed
- 2 skipped
- 117 subtests passed

Both failures are inherited missing documentation authorities, not production-code failures:

- `ROUTER_HANDOVER.md`
- `docs/plans/2026-07-16-request-streaming-session-lifecycle-mvp.md`

A full rerun with temporary, test-only copies of those exact documents produced:

- 693 passed
- 2 skipped
- 117 subtests passed

The copies were removed after the run. This workaround proves the executable tree is green, but the missing tracked-document defect remains a real integration task. `ROUTER_HANDOVER.md` currently exists in canonical root; the request lifecycle plan was found only under `/Users/evinova-self/Projects/mycelium-merge-20260716T194951Z/`. Review provenance before tracking it.

### Rust

From `native/iroh_transport`:

- `cargo fmt --check`: pass
- `cargo clippy --all-targets --all-features -- -D warnings`: pass
- `cargo test`: 21 passed, 0 failed

### UI

The overnight worktree has no installed `node_modules`. Its `package-lock.json` is byte-identical to canonical main's lockfile, so verification temporarily symlinked canonical's installed modules and removed the symlink afterward.

`npm run check` result:

- Vitest: 70 passed
- Node contract tests: 3 passed
- TypeScript typecheck: pass
- Production build: pass
- Non-blocking warning: generated JS/ELK chunks exceed Vite's 500 kB warning threshold

### Contract/integrity

- `python3 scripts/contract_audit.py`: 11 contracts, no drift
- `python3 -m compileall -q .`: pass
- `git diff --check`: pass
- Overnight worktree clean after verification

## 5. Phase/gate map

This map uses the authoritative 14-phase synthesis plan, not older handover numbering.

| Phase | Current state | Honest boundary / next gate |
|---|---|---|
| 0 Provenance/baseline | Complete on integration branch | Ordered reconciliation, exact tree match, source-to-integration mapping, and tracked documentation provenance are recorded above. |
| 1 Green baseline/contracts | Complete on integration branch | Clean-checkout Python, contract, compile, diff, Rust, and UI gates all pass. |
| 2 Gossip/Planner/assignment | Implemented and contract-audited | Preserve frozen contracts; do not let parallel lanes invent replacements. |
| 3 Assignment provisioning/evidence | Implemented locally | Physical cold-cache peer-direct rerun remains outside current evidence. |
| 4 MLX runtime/load proof | Implemented locally | Assignment-bound split runtime exists; stage-local KV continuity under routed decode remains unproven. |
| 5 Layer Builder | Implemented locally | Structural graph is not readiness authority. |
| 6 Real local Router ports | Strong local tranche complete | Two-process TCP Router parity passes under complete-context replay. Original stage-local KV/progressive semantics remain a gap. |
| 7 Native iroh | Partial | Wire codec and authenticated local sidecar pass. Missing production Router TransportPort, authenticated two-host dispatch, persistent path evidence, and crash-durability proof. |
| 8 Physical qualification | Not completed | Requires explicit current authorization for both Macs, real stage compute on each, cold cache, at least eight decode steps, negative runs, and evidence seal. |
| 9 Request gateway/streaming | Not completed | Observatory gateway is not the inference request gateway. Build a separate authenticated request module per D7. |
| 10 Live Observatory | Strong local tranche complete | Semantic/read-only UI passes local gates. It still lacks accepted live physical route/request data and browser-level physical-demo verification. |
| 11 Recovery | Not integrated | Router failure fencing improved, but sealed recovery source and true KV-loss outcomes remain unintegrated. Defer until base physical route is stable. |
| 12 Pixel/mobile | Deferred | External mobile evidence is eligibility evidence, not exact-stage execution. Do not put Pixel on first two-Mac critical path. |
| 13 Demo/release | Not completed | Doctor/orchestration, fresh bootstrap, immutable evidence verification, and complete release gate remain. |

## 6. Critical claim boundaries

Preserve these in every new session:

1. Never set or imply `route_ready: true` from provisioning, loading, graph construction, local process tests, or synthetic fixtures.
2. The accepted route qualifier alone may emit readiness after real two-host compute/transport/parity gates pass.
3. Local loopback TCP and local iroh sidecar evidence are not physical distributed-inference evidence.
4. Full-context replay is the completed local Phase 6 tranche. It is not stage-local KV reuse.
5. The Observatory remains GET/SSE/read-only. Prompt submission belongs in a separately authenticated request module.
6. No full-model fallback, fixture RuntimePort, simulator transport, or synthetic compute may participate in an accepted run.
7. Gossip carries control-plane metadata only. Never send model tensors, protected edits, prompts, activations, KV state, or secrets over Gossip.
8. Keep Mycelium implementation isolated from other distributed-inference repositories.
9. Do not mutate or clean the old A/B/C/overnight worktrees; they remain provenance inputs after reconciliation.
10. No push, PR, remote-host action, package install, or physical qualification without explicit scope.

## 7. Completed serial integration recipe (historical)

Steps 1 through 9 below are complete on `integration/mycelium-manual-driver`; do not rerun them against the overnight source branch. Step 10 is now the active feature-lane handoff.

1. Create a fresh integration worktree from `main`; do not edit canonical main in place.
2. Confirm `main=f2bc55b` and overnight code head includes `d412777`. If state changed, re-audit before applying this recipe.
3. Verify the A1 qualification files are byte-identical between main and overnight.
4. Bring in automation provenance commits `e9e6928` and `cd7d1b1` if retaining the overnight journal.
5. Skip `ead89f2` because A1's two files already exist byte-identically on main.
6. Cherry-pick and review `b759305` through `d412777` in order.
7. Resolve the two missing documentation authorities with explicit source/provenance review; do not hide the test failure with a permanent ad hoc copy.
8. Run Python 3.14, Rust, UI, contract, compile, and diff gates.
9. Commit on the integration branch. Do not merge into main or push until Evi reviews the report.
10. Rebase/cherry-pick parallel lane commits onto the verified integration head in dependency order.

## 8. Archived primary manual-driver prompt (completed)

This prompt produced the verified result in Section 0. Preserve it as provenance; do not launch another integration driver from it.

```text
You are the primary integration driver for the Mycelium/DDAI MVP.

Repository: /Users/evinova-self/Projects/mycelium
Canonical main snapshot: f2bc55b62c5e3103eda584c1c68c222244bde489
Overnight source branch/code head: automation/mycelium-overnight at d412777edb6099447dd6df3630e9addc883c6a42 (a later documentation-only handover commit may now sit above it)
Read first:
- /Users/evinova-self/Projects/mycelium-wt-overnight/docs/automation/2026-07-18-manual-driver-handover.md
- /Users/evinova-self/Projects/mycelium/.hermes/plans/2026-07-17_142630-mycelium-ddai-mvp-synthesis-plan.md
- /Users/evinova-self/Projects/mycelium-wt-overnight/docs/automation/overnight-build.md

Goal: reconcile the verified overnight work into one clean integration branch, repair the clean-checkout documentation test defect, run every gate, and leave a precise handover for feature lanes. Do not merge into main or push.

Safety:
- Create or use an isolated worktree such as /Users/evinova-self/Projects/mycelium-wt-manual-driver on branch integration/mycelium-manual-driver from main.
- Never edit, reset, clean, commit, or switch canonical main or old A/B/C/overnight source worktrees.
- No fetch/pull/push/PR, package install, remote host, phone, credentials, or physical qualification.
- Use python3.14. Keep route_ready=false. Keep Observatory read-only. Do not import code or protected edits from other distributed-inference repositories.
- Stage explicit files only; never git add -A or git add .

Required reconciliation:
1. Re-probe all SHAs and worktree statuses. Stop and report if the topology differs materially.
2. Verify main and overnight copies of two_process_inference_qualification.py and its test are byte-identical.
3. Preserve e9e6928 and cd7d1b1 if retaining the automation journal.
4. Skip ead89f2 because its A1 implementation already exists on main.
5. Integrate b759305 through d412777 in order, reviewing every conflict and claim boundary.
6. Fix the inherited clean-checkout failures for ROUTER_HANDOVER.md and docs/plans/2026-07-16-request-streaming-session-lifecycle-mvp.md. Establish exact source provenance before tracking them; the request plan currently exists only in /Users/evinova-self/Projects/mycelium-merge-20260716T194951Z/. Do not merely suppress or deselect the tests.
7. Update the current handover only after observed gates pass.

Verification:
- python3.14 -m pytest -q
- python3.14 scripts/contract_audit.py
- python3.14 -m compileall -q .
- git diff --check
- native/iroh_transport: cargo fmt --check; cargo clippy --all-targets --all-features -- -D warnings; cargo test
- ui/web: npm run check using the existing lockfile/dependencies only; no install

Deliver:
- clean integration-branch commit(s)
- exact commits integrated/skipped and why
- test counts and command exits
- remaining physical/semantic gaps
- claim boundary: local evidence only, route_ready=false
- no merge to main and no push
```

## 9. Parallel lane prompt A — stage-local KV and progressive local execution

Start this lane only from the final reviewed `integration/mycelium-manual-driver` tip. The overnight branch is now a provenance source, not a feature base.

```text
Build the next local execution tranche for Mycelium: stage-local KV-backed prefill/decode semantics with deterministic parity. Work only in a new isolated worktree/branch based on the final reviewed integration/mycelium-manual-driver tip; suggested branch feature/stage-local-kv. Read docs/automation/2026-07-18-manual-driver-handover.md and the authoritative synthesis plan first.

Own primarily:
- mycelium_router/mlx_runtime.py
- two_process_inference_qualification.py
- test_router_mlx_runtime.py
- test_two_process_inference_qualification.py
- narrowly necessary runtime-port tests/contracts
Do not edit iroh sidecar/transport, Observatory UI/gateway, qualification package, or request gateway files.

Use strict TDD. First prove RED for:
1. prefill establishes request/path/manifest/epoch-bound KV state on each assigned stage;
2. each decode step consumes only the new token/position instead of replaying the full prefix;
3. at least eight greedy decode steps match the independent monolithic/concatenated reference token-for-token and within declared numeric tolerance;
4. duplicate/replayed sequence is idempotent or rejected exactly once;
5. wrong request, path, assignment, manifest, epoch, position, or sequence fails closed;
6. cancellation, lease expiry, worker crash, and normal completion free KV/capacity state without cross-request leakage;
7. each child still loads only its assigned tensors and no full-model fallback exists.

Implement the smallest production change that makes those tests green. Preserve current complete-context replay as an explicit compatibility mode only if needed; never silently relabel it as KV-backed decode. Keep route_ready=false and make no physical-host claim.

Run focused tests, full python3.14 -m pytest -q, contract audit, compileall, and git diff --check. Commit explicit files only. Report RED-to-GREEN evidence, token/parity output, memory/KV lifecycle evidence, exact commit, and unresolved gap. No network, package install, push, PR, remote host, or changes to other worktrees.
```

## 10. Parallel lane prompt B — production Router-to-iroh adapter

Start this lane only from the final reviewed `integration/mycelium-manual-driver` tip. It remains disjoint from the KV lane if file ownership is respected.

```text
Build the production Router TransportPort adapter over the existing authenticated Mycelium iroh sidecar. Work only in a new isolated worktree/branch based on the final reviewed integration/mycelium-manual-driver tip; suggested branch feature/router-iroh-adapter. Read docs/automation/2026-07-18-manual-driver-handover.md, contracts/iroh-sidecar-v1.md, and the authoritative synthesis plan first.

Own primarily:
- new mycelium_router/transports/iroh.py
- mycelium_router/transports/__init__.py only if required
- mycelium_iroh_sidecar/client.py only for adapter-required, tested behavior
- new focused transport/integration tests
- native sidecar files only when a failing cross-language test proves a native defect
Do not edit MLX/KV runtime, qualification, request gateway, or Observatory files. Avoid core Router contract changes; if unavoidable, stop and report the exact contract conflict instead of inventing a replacement.

Use TDD. Require RED then GREEN for:
1. Router dispatch actually traverses the sidecar adapter; no loopback/simulator fallback;
2. Mycelium node ID binds to exact authenticated EndpointID/generation before Router frame delivery;
3. canonical mycelium.router_wire.v1 framing remains the sole Router envelope authority;
4. bounded queue/backpressure, deadline, cancellation, ACK, reconnect, duplicate/replay, sequence-gap, malformed frame, and clean shutdown behavior;
5. endpoint rotation fences stale connections and in-flight frames;
6. sidecar crash cannot produce false delivery success; delivery semantics and any process-lifetime limitation are explicit;
7. local three-process prefill/decode traverses the adapter and preserves activation/token digests.

Do not use remote hosts yet and do not claim physical iroh qualification. Keep route_ready=false. Use existing local Cargo/Python caches only; no package install or network fetch. Run Rust fmt/clippy/test, focused Python, full python3.14 suite, contract audit, compileall, and diff check. Commit explicit files only and report exact transport evidence and remaining two-host blockers. No push/PR.
```

## 11. Parallel lane prompt C — qualification/evidence authority

This lane can build fail-closed contracts in parallel from the final reviewed integration tip but must not claim current readiness.

```text
Build the Mycelium RouteQualificationV1 authority and immutable evidence validator without running a physical qualification. Work only in a new isolated worktree/branch based on the final reviewed integration/mycelium-manual-driver tip; suggested branch feature/route-qualification-authority. Read docs/automation/2026-07-18-manual-driver-handover.md and Phase 8 plus Sections 11-14 of the authoritative synthesis plan first.

Own only new/narrow files:
- mycelium_qualification/__init__.py
- mycelium_qualification/contracts.py
- mycelium_qualification/qualifier.py
- mycelium_qualification/evidence.py
- tests/qualification/*
- contract fixture/manifest entries required for this versioned schema
Do not edit Router execution, MLX runtime, iroh sidecar/adapter, request gateway, or Observatory UI.

Use strict TDD. Define one canonical, versioned RouteQualificationV1 contract and fail closed on every identity/gate mismatch. Tests must cover at least:
- source/provenance and dependency-lock digests;
- signed Gossip snapshot and Planner/assignment/provision/load/graph chain coherence;
- deployment ID/epoch, topology, model revision, manifest, stage signature, load-proof digest, path, reservation, EndpointID, process, tensor-scope, transport, token, numeric-parity, and evidence-manifest binding;
- stale proof, wrong endpoint, missing tensor, expired reservation, sequence replay, dropped peer, full-model fallback, simulator/fixture participation, synthetic timing, and missing negative-run evidence;
- canonical serialization and immutable SHA256 evidence manifest;
- only the qualifier can express route_ready=true.

A synthetic happy-path fixture may test schema logic only if it is prominently labeled synthetic_test_fixture and impossible to confuse with accepted run evidence. Current repository/runtime state must remain route_ready=false. Do not generate or commit a real-looking successful physical run.

Run focused tests, full python3.14 suite, contract fixture generation/audit, compileall, and diff check. Commit explicit files only. Report the frozen schema, RED-to-GREEN negatives, exact commit, and all physical inputs still missing. No network, remote host, package install, push, or PR.
```

## 12. Queued lane prompt D — separate request gateway and token stream

Start implementation once the qualification contract is frozen or provide that commit to this agent. It can be prepared in parallel, but it must not invent a competing readiness schema. Base it on the final reviewed integration tip plus the frozen qualifier commit.

```text
Build Mycelium's separately authenticated inference request gateway and token-stream lifecycle. Base a new isolated worktree/branch on the final reviewed integration/mycelium-manual-driver tip plus the frozen RouteQualificationV1 commit. Suggested branch feature/request-token-stream. Read docs/automation/2026-07-18-manual-driver-handover.md, D7, and Phase 9 of the authoritative synthesis plan first.

Preserve the existing mycelium_gateway Observatory as strictly read-only. Put prompt submission/control in a separate package, preferably mycelium_request_gateway/, unless an already frozen architecture authority specifies another isolated module. Do not add POST/control methods to the Observatory ASGI app.

Own primarily:
- new mycelium_request_gateway/*
- tests/request_gateway/*
- separate CLI/API contract documentation
Do not edit iroh transport, MLX/KV runtime, qualifier internals, or Observatory UI except a versioned read-only event consumer fixture if strictly necessary.

Use TDD for:
- health/current qualification endpoints;
- inference admission only with an exact current RouteQualificationV1 match;
- prompt submission, deterministic token event ordering, terminal event exactly once, cancellation, disconnect/reconnect/resume without duplicate output, bounded backpressure, and capacity/KV cleanup;
- explicit rejection for stale/mismatched qualification, epoch/path change, dropped route, or revoked readiness;
- default logs and metrics contain no prompt text, token IDs/content, activations, KV, credentials, or raw private endpoints;
- CLI receives streamed tokens through the same production session interface, not a fixture-only shortcut.

If the qualifier contract is absent or unstable, stop before inventing a schema and report the exact required interface. Local fixture tests are allowed but must be labeled; no distributed or route_ready claim follows from them. Run focused and full Python 3.14 gates, privacy/log scans, contract audit, compileall, and diff check. Commit explicit files only. No network, remote host, package install, push, or PR.
```

## 13. Merge/dependency order for parallel lanes

Recommended order after the primary integration branch is green:

1. Stage-local KV/local execution tranche.
2. Router-to-iroh adapter.
3. Qualification/evidence authority, rebased against the final execution/transport contracts.
4. Physical two-Mac qualification only after explicit authorization and a written staging/cleanup plan.
5. Request gateway/token stream against the frozen qualifier.
6. Feed accepted request/qualification events into the existing read-only Observatory.
7. Recovery integration only after stable physical base route.
8. Demo/release orchestration, then optional Pixel exact-stage work.

KV and iroh implementation agents may run concurrently if they obey file ownership. Qualification can develop new contracts/tests concurrently but should rebase and run all gates after those two lanes land. Request-gateway scaffolding can run concurrently only if it consumes rather than replaces the qualification authority.

## 14. Explicit blockers before physical proof

- Integration branch awaits Evi review; canonical `main` remains intentionally unchanged.
- Clean-checkout documentation defect is fixed on the integration branch with exact provenance.
- Stage-local KV-backed routed decode not proven.
- Router does not yet dispatch through a production iroh TransportPort.
- No authorized authenticated two-Mac runtime/transport run.
- No accepted RouteQualificationV1/evidence seal.
- No inference request gateway/token stream.
- No release doctor/fresh-bootstrap proof.

The next moonlit step is no longer reconciliation: review the clean integration tip, then close local KV and Router-to-iroh gaps with measured RED-to-GREEN evidence.
