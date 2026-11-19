# Lane PP — prepare-honesty / retirement accounting investigation

Latest status (Round 2, final round): BLOCKED before RED execution by the repository's disk floor (24 GiB available; minimum 25 GiB). Packaged retirement/readiness mismatch and a separate owner-side 600-second serve-result tracking wall located statically; rv46's precise retirement cause/time remains unproven. No production fix, no new test pass, no claimed release pass. See Round 2 below.

Round 1 status: BLOCKED at the required RED gate. No production fix, no claimed release pass.

Branch: `lane/prepare-honesty`.
Base/production tested: `c2963eb46b886f2142d0d9fce1ead5a99f471389`.
All writes are in this worktree (build/runtime temporary files use the assigned Hermes scratch directory).
No push, merge, rebase, installed-app access, SSH, GPU/model execution, cloudflared, downloads or budget changes.

## Result and acceptance disposition

The supplied failure readback establishes an observed UI/chat disagreement, but its interpretation of the prepare logs is not supported by the log-emitting code. The requested missing/failed-prepare accounting regression has NOT been reproduced. A new deterministic diagnostic over the existing model-free owner-work harness instead passes on unchanged candidate production code.

This is an investigation/diagnostic handoff, not the requested RED-then-fix deliverable. There is no honest failing RED output or fix commit to supply. Production was deliberately left unchanged rather than weakening retained-result authority checks or treating a transient failed poll as a terminal operation refusal. The diagnostic and this report are suitable for review, not evidence that the physical defect is fixed.

Acceptance disposition:

- Missing/terminally failed prepare: the new controlled probe demonstrates the existing refusal gate, pending downstream steps and false `route_ready`. It does not reproduce the physical incident or prove all transport interleavings.
- Unaccepted work: production acceptance/proof gates traced; no new end-to-end unaccepted-offer regression demonstrated.
- Previous-run/authority and late completions: existing RRP and runner tests passed; no changes to their semantics. This is not a new exhaustive epoch regression.
- Repeated-failure automatic stop and physical UI/chat agreement: not newly verified or fixed.
- Required library commands: passed, including their explicit ignored-test counts below.
- Requested two commits (RED + fix): not produced. Do not count this lane as complete.

## Evidence that changes the diagnosis

Evidence directory (read-only):
`/Users/mycelium-dev/Projects/mycelium-m4-program/qualification/rv46-sessions/20261119T095945Z-s2/`.
The first evidence read was `run1-failure-readback.json`; the owner, watcher, chat and member logs were subsequently inspected. Private log contents/URLs are not copied into this report.

### 1. `outcome=finished failures=2` is not a terminal prepare failure

`a-run.log:4290-4291` records a `control_connect_timeout` on result poll attempt 23. `a-run.log:4483` later records:

    member result poll 3 (prepare_native) member=6caa2e417a outcome=finished failures=2 over_ms=4064

At `crates/api/src/joined_owner/remote.rs:305-327`, this exact `finished` outcome is emitted only after a result poll returns `result.finished == true`. The enclosed reply, not the repeat-summary counter, is then sent to the owner worker. A finished reply may itself be a refusal; `RemoteMember::finish` checks `ok` at `remote.rs:455-465` and propagates its code.

`attempt=23` counts all polls (`remote.rs:299-304`), not 23 failed attempts. `failures=2` records failed polls before the eventual finished-result observation. Transient result-poll transport errors stay pending within the existing wall; terminal errors follow the separate paths at `remote.rs:343-362`. The requested fix must not conflate these meanings or turn a successfully retrieved completion into a failure merely because an earlier poll timed out.

The finished reply's payload is not in these physical logs. Accordingly, this report does NOT claim a direct log proof that its native proof was valid, nor that a specific earlier reply was lost.

### 2. rv45 refused at a different control boundary

Read-only rv45 evidence:
`/Users/mycelium-dev/Projects/mycelium-m4-program/qualification/rv45-sessions/20261118T214742Z-s1/a-after-run1.log`.

- Lines 4893-4894: `member control call prepare_native (op Some(3)) ... control_connect_timeout`.
- Line 4934: `owner_work_failed: control_call_failed:control_connect_timeout`.

That is the INITIAL command call failure, not the later result-poll summary. `remote.rs:285-293` immediately forwards such a failure as `ok:false`. `link.rs:73-74` propagates it, `Worker::work` cannot pass its prepare gate (`work.rs:1785-1787`), and `fail` records the open cause under the closed workflow failure (`work.rs:652`). Publish then refuses (`crates/owner-run/src/steps/publish.rs:118-123`). This explains rv45's loud refusal without inventing an accounting change.

rv46 instead reached the finished-result path. If that reply was successful, the same unchanged proof/step gates permit progress. The reviewed delta makes successful remote results rereadable within their verified authority lifetime; it does not set machine steps, `awaiting_activation`, startup qualification or `route_ready`.

The requested rv45-to-rv46 `git log --stat` and production diff were inspected. Relevant RRP changes are `crates/api/src/joined_member/work.rs:233-253` (scoped, non-destructive remote reads), `work.rs:296-316` (admission/cancellation-identity provenance), `work.rs:415-443` (unchanged authority refresh versus revocation), and `crates/node-service/src/packaged_work.rs:595-602` (advance accepted announcement lifetime). Retention is a plausible explanation for recovery of previously consumed completions, not proof that a lost completion caused this particular incident.

Thus the evidence does not support the proposed explanation “the same terminal prepare failure was ignored after RRP.” It supports different command/poll outcomes, plus a later UI/readiness disagreement still needing diagnosis.

### 3. The member card is not proof that the joined work path did nothing

B's log contains an artifact connection at `b-final.private.log:300` and its close at `:655`; router connections at `:951` and `:953`, closing at `:10634-10635`. These are contrary evidence to “no work/serve activity”; connection logs alone do not prove successful byte transfer or token computation.

The `work_offer_not_published` string is produced by `crates/api/src/local_agent.rs:563-588`, testing that agent's legacy offer ID/native state. The joined route pushes stage shards on another path (`crates/api/src/joined_owner/work.rs:1429-1441`). The packaged resident's work report instead comes from actual `native_owned`/`shard_owned` custody at `crates/node-service/src/packaged_work.rs:509-539`. The member agent runs the packaged work publication path separately at `crates/api/src/member_agent.rs:427-449`.

Consequently, that card cannot establish that the pushed joined work offer was never received or accepted. This does not excuse a misleading card; it limits what it proves about owner step accounting.

### 4. A separate live-readiness accounting mismatch is located, but not established as the physical root cause

- `crates/owner-run/src/machine.rs:1041`: `route_ready` is just `self.state == State::Serving`.
- `crates/api/src/joined_owner/work.rs:2023-2031`: the documented retirement behavior keeps the workflow `api_running` with `epoch_retired` until Stop, rollover, ceiling or monitor.
- `work.rs:2084-2090`: retire/close the route and record the retirement, without changing that workflow state.
- `work.rs:938-968`: monitor checks ceiling, FAILED/STOPPED and expired seed leases, not general epoch retirement.
- `crates/api/assets/desktop/run.js:94-108`: the owner page's Serving headline comes from the machine state.
- `crates/api/src/chat_service.rs:331-335`: the live qualification endpoint's 503 is mapped to `chat_route_not_qualified`.

A once-qualified route can therefore later refuse qualification while the machine still says Serving. Existing tests explicitly cover workflow survival after retirement (`crates/api/tests/owner_work.rs:2287`, `:2583`). This is a much better diagnostic lead for the later refused chat than converting a recovered prepare poll into a failure. Changing that behavior requires an explicit decision about the documented retirement semantics; it is not evidence of the requested missing-prepare hole.

The watcher reports `MONITOR_TIMEOUT` (`watch-run1.log:5`) and contains no authoritative owner-run/epoch snapshots for the disagreement. The chat log confirms a live refusal at its probe time, not whether startup qualification was absent from the beginning. The final repeated-failure stop observed after explicit Stop is not a timestamped proof that it had already been terminal throughout the preceding Serving display. Fresh raw owner-run, workflow, epoch-retirement and live qualification snapshots at the disagreement are needed to distinguish stale page/status publication from post-startup retirement/rollover.

## Existing gates traced

- Each production native prepare issues a fresh nonce and must finish/admit EVERY stage: `crates/api/src/joined_owner/link.rs:57-75`.
- Signed proof binds the offer, enrolled member, nonce and assigned range: `crates/api/src/joined_shard/native.rs:53-84`; one-use challenge ownership starts at `:88`.
- No `awaiting_activation` before `self.prepare(&offers)?`: `crates/api/src/joined_owner/work.rs:1785-1787`.
- Publish only returns success on `awaiting_activation`: `crates/owner-run/src/steps/publish.rs:118-123`.
- Targets needs all participant work reports and checks failed workflow: `crates/owner-run/src/steps/targets.rs:43-66`.
- Activation requires the same proposal and AWAITING state: `crates/api/src/joined_owner/work.rs:1421-1425`.
- Late/cancelled operation replies drained during exit do not prove work: `work.rs:1714-1715`.
- Replaced-context writes and serving publication are fenced: `crates/owner-run/src/runner.rs:270-290`, `:326-337`.

## New deterministic diagnostic — baseline GREEN, not RED

File: `crates/api/tests/support/owner_prepare_honesty.rs` (included by `support/owner_startup.rs`).

Uses the existing model-free swarm, real OwnerWork worker, real Publish step and sans-IO owner machine. The test-member admission seam is explicitly controlled; it does not pretend to be native computation or the physical remote-control transport. Earlier machine steps are explicitly modeled successes to isolate Publish.

A barrier holds one member's prepare result. Publish cannot finish while it is held. On release the test member answers `control_call_failed:control_connect_timeout`; Publish must refuse `managed_workflow_failed`, activation must refuse, no gateway/startup record may exist and no member may receive `serve`. The real step refusal is fed to the machine: targets, activate and serving remain pending, `route_ready` stays false.

First execution on unchanged c2963eb4 production:

    bash qualification/lanePP/run.sh prepare-honesty-baseline test -p api --test owner_work prepare_honesty -- --nocapture
    owner_work_failed: control_call_failed:control_connect_timeout
    test startup::prepare_honesty::a_missing_then_failed_prepare_cannot_complete_publish_or_qualify_the_run ... ok
    test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 46 filtered out

Evidence: `qualification/lanePP/prepare-honesty-baseline.log:5-10`. Final exact-source rerun: `prepare-honesty-final.log` (also passed). This is intentionally not relabeled RED. The initial dyld loader failure and a subsequently corrected test-only clippy conversion warning are not accounting REDs either.

## GREEN checks / neighboring tests actually executed

`qualification/lanePP/run.sh` pins this worktree's target, offline Cargo, clears PYTHONPATH, renices the shell and runs Cargo at nice 19. It refuses release busy markers/waiting parent wrappers. No `-j` override. MLX libraries are linked for feature tests only; no model is loaded. The loader path is set after the protected macOS launch utilities because exporting it before those utilities did not survive to the test binary. Bootstrap test config is supplied by path without printing it.

| Command (through wrapper) | Actual result | Log |
| --- | --- | --- |
| `cargo test -p node-service --lib --features "packaged desktop-shell"` | 142 passed, 0 failed, 6 ignored | `node-lib-final.log` |
| `cargo test -p api --lib` | 136 passed, 0 failed, 7 ignored | `api-lib-final.log` |
| `cargo test -p api --test owner_work` | 47 passed, 0 failed | `owner-work-final.log` |
| `cargo test -p owner-run` | 155 passed across test binaries, 0 failed | `owner-run.log` |
| `cargo test -p api --test owner_pin` | 18 passed, 0 failed | `owner-pin.log` |
| `cargo test -p node-service --features "packaged desktop-shell" --test packaged_work` | 9 passed, 0 failed | `packaged-result-lifetime.log` |
| `cargo test -p node-service --features "packaged desktop-shell" --test packaged_ten_rows` | 0 passed, 3 ignored; compile-only, NOT a ten-row run | `packaged-ten-rows.log` |
| `cargo test -p api --test owner_work prepare_honesty -- --nocapture` | 1 passed, 0 failed | `prepare-honesty-final.log` |
| `cargo clippy -p api --test owner_work -- -D warnings` | exit 0 (test-only redundant conversion corrected) | `api-test-clippy-final.log` |
| `cargo fmt --all -- --check` | exit 0 | `fmt-final.log` |

All logs above are under `qualification/lanePP/`. `git diff --check` passed. Production diff against the exact candidate is empty.

Neighbor coverage includes refused prepare cleanup; never claiming an unrun startup challenge; startup failure before teardown; retired-epoch refusal; same endpoint with changed signed run/deployment; late completion after repin and A-B-A; replacement service isolation; and replaced-run publication/write fencing. These are existing GREEN controls, not new RED evidence. Ignored native/physical tests were not enabled.

## Blocking handoff

Do not merge this as a fix for rv46. Request either:

1. raw timestamped owner-run/workflow/epoch/qualification snapshots demonstrating failed current prepare while the same run advances; or
2. explicit rescope to the located post-startup retirement / stale readiness and member-card accounting issues, with a deterministic RED at that actual boundary.

The physical failure remains valid release-blocking evidence. Its proposed prepare-causality explanation is the blocked prerequisite, not a reason to invent a fix or claim success.

## Round 3 — sanctioned convergence fix round (in progress)

Base: `85dc0af6`, production behavior unchanged from `c2963eb4`. Both required REDs
executed before any production fix: `qualification/lanePP/round3-red-executed.log`.
Command: `bash qualification/lanePP/run.sh round3-red-executed test -p api --test owner_work startup::retirement:: -- --nocapture`.
Result: **0 passed, 2 failed**, 47 filtered out; test 6.08 s, command 8.56 s.

- RED-A: `red_a_tracking_timeout_does_not_retire_a_live_serving_epoch_or_refuse_chat`:
  expected null retirement, actual `joined_route_member_ended`. The signed route wall
  exceeds MemberChosenWall, startup and an initial chat succeed, the actual remote
  driver polls after an injected tracking-age advance, and a later chat is attempted.
- RED-B: `red_b_retired_epoch_clears_run_readiness_without_ending_api_running`:
  actual member consent exhaustion ends serve; workflow remains api_running and
  the next chat refuses, but the run snapshot's route_ready is true, expected false.
  Completed checklist and Serving state are explicitly preserved.
- Harness: real OwnerWork, epoch/carrier/stage servers, gateway, startup challenge,
  serving step, Runner and snapshot. Existing model-free controlled stage computation
  and admission; remote call transport is controlled. The actual remote.rs is compiled
  by path, not copied. Added only cfg(test) call/age hooks to that file; ordinary library
  behavior and all timeout branches are unchanged at this RED commit. Native proof,
  signed-control authentication and physical inference are NOT simulated evidence.
- Initial compile attempt (`round3-red.log`) used a nonexistent test wait helper;
  corrected to the existing snapshot interface. This is not counted as RED.
- Preflight: 24 GiB available, user-authorized floor 22 GiB. Pruned only this lane's
  stale target/debug/incremental (3.3 GiB logical); df then 26 GiB, after RED 25 GiB.
  No models, installed app, SSH, cloudflared, network, pushes or other worktrees touched.
  Wrapper now enforces 22 GiB and adopted four-job/nonincremental/line-table settings.

## Round 2 — final round: packaged epoch retirement / readiness accounting

### Scope, execution blocker and disposition

The owner withdrew the failed-prepare hypothesis and authorized retirement/readiness investigation. That supersedes Round 1's rescope request. This round starts at `1f81f44e64905681475c5ac1c3f10c5626628c01` on `lane/prepare-honesty`; its production code is still unchanged from `c2963eb4`. `git diff --stat c2963eb4 1f81f44e -- crates` lists only the diagnostic test and its module inclusion.

Read the adopted process rules and `REVIEW-laneEP.md` in the M4 program root. The old core-program and m4-program `status/V02-PLAN.md` paths are absent; `LANE-COMMON.md:5-7` explicitly acknowledges that stale context. Only this worktree was written. No installed-app access, GPU/model execution, SSH, cloudflared, external network, downloads, push or merge.

Before any edit to code or Cargo execution, `df -g /System/Volumes/Data` reported **24 GiB available**, below the **25 GiB mandatory stop floor** (`plans/LANE-BRIEF-COMMON.md:108-110,143-146`). Stopped implementation/build work; did not delete caches, override the gate or try a different build route. The read-only trace was completed and this handoff recorded. Exact command output is retained in `qualification/lanePP/round2-preflight.log` (2026-11-19T11:25:39Z). This is a resource-blocked diagnostic, not a completed fix.

### Q1 — packaged wall computation and retirement producers (file:line, then test)

1. **The signed route wall starts at Publish, not first visible Serving.**
   - `crates/planner/src/chain_compile.rs:30-36,716-724`: compiled proposal wall is 1,800 seconds; maximum is 3,600; unknown-member fallback is 60.
   - `chain_compile.rs:791-815`: each participant gets `min(proposed, caps.max_wall_seconds)` after the proposal maximum clamp. The owner without a cap gets the proposal, NOT the unknown-member fallback. An unknown joined member gets the fallback.
   - `crates/owner-run/src/steps/compile.rs:258` stores these worker walls. `crates/node-service/src/packaged_run/work.rs:1003,1156-1188` supplies them to the packaged `OwnerWork` and builds the real `RemoteMember` links.
   - `crates/api/src/joined_owner/work.rs:1377-1390`: `expires_at = wall_clock_at_start_serving + min(worker_walls)`, queued in `Command::Prepare`. Native prepare follows at `:1779-1787`; activation/route opening and the startup challenge come later at `:1795-1870`.
   - `work.rs:1799-1807` passes that already-fixed expiry to the run's single route (`epoch: 1`); `Proven::issue`, `:267-282`, calls `route::issue_epoch_until` for v2. `crates/api/src/joined_shard/route.rs:137-193` signs that expiry unchanged. This is NOT the rehearsal `open_epoch(..., expires_at: Option)` path.
   - Consequently prepare/startup time consumes the signed route lifetime. There is no independent "three minutes after Serving" wall in this computation. The run's rollover/ceiling similarly use the Publish execution clock: `work.rs:2138-2173` and `:2177` onward. No wall, lease or consent limit was changed.

2. **The member's serve does NOT retain the generic 600-second operation deadline.**
   - The generic dispatcher sets `MemberChosenWall` at admission (`crates/api/src/joined_member/work.rs:147-153,224-229`); `crates/budgets/src/table.rs:499` defines it as 600,000 ms. The packaged operations loop checks it before calling serve (`crates/node-service/src/packaged_work.rs:881-905`).
   - But `crates/api/src/joined_member/work.rs:1404-1448` verifies the signed route, derives a monotonic bound from its remaining lifetime plus `RouterClose`, and calls stage serve with `deadline: expiry.min(consented)`. It does NOT pass `operation.deadline` to the stage serve. `consented` was captured from this member's first prepare (`:1310-1312`); the native worker also checks elapsed consent wall, remaining commands and unchanged consent (`crates/api/src/joined_shard/native.rs:275-279,364-370`). The 600-second dispatcher default is therefore not itself the member's resident serve wall.
   - Actual stage serve checks expiry during setup (`crates/api/src/joined_shard/serve.rs:288-295`) and on every v2 loop pass (`:530-588`): expiry, cancellation/deadline, or consent failure ends compute. Note that this latter path labels `s.check()` failure `operator_cancelled`, so that report alone does not distinguish cancellation from its local deadline.
   - Existing test: `crates/api/tests/member_work.rs:762`, `a_member_serves_its_epoch_until_the_routes_signed_expiry_not_one_operation_wall`; setup/consent boundary tests at `:944,948`. NOT RUN in Round 2.

3. **A distinct shorter owner-side result-tracking wall remains: 600 seconds, including serve. This is the strongest newly located premature-retirement candidate.**
   - `crates/api/src/joined_owner/remote.rs:66-70`: the packaged remote driver's `budgets.wall` is unconditionally `MemberChosenWall`, independently of the signed route or consent. The run uses this driver (`crates/node-service/src/packaged_run/work.rs:1158-1169`).
   - `crates/api/src/joined_owner/work.rs:192-203`: `serve` is begun before opening/dialing the epoch and before the startup challenge. `remote.rs:281-283` starts the pending operation clock once its initial command was accepted.
   - Even if every result poll succeeds with `finished:false`, `remote.rs:353-362` removes that pending operation at `began.elapsed() >= budgets.wall` and sends a synthetic `ok:false, code:member_operation_wall_exceeded`. This is not a member-signed completion proving that its serve ended. Failed polls take the parallel bounded-error path at `:343-348`.
   - `work.rs:2033-2039` treats ANY taken serve result, including that local tracking refusal, as `member_ended`. When idle, `:2077-2090` sets `joined_route_member_ended`, closes the carrier, and publishes retirement. Thus the owner-side 600-second wall can retire a route whose member was allowed to serve longer. Time spent opening/qualifying consumes that owner tracking window too.
   - The normal timeout can be absent from logs: `remote.rs:239-248,356-362` routes it through `Repeats::outcome_line`; `crates/runtime/src/repeats.rs:79-84,113-122` produces a line only if that operation had previously recorded failed polls. Successful pending polls do not create that history. Absence of a serve-timeout log is not contrary evidence.
   - This is a concrete static path, NOT an executed RED and NOT proof that rv46 took it. An appropriate deterministic test must hold a real remote serve result pending, advance an injected tracking clock past `MemberChosenWall` while signed route/consent remain valid, and distinguish owner tracking expiry from a member terminal completion. The existing member-only test above does not exercise this owner driver. No such test was implemented or run under the disk blocker.

4. **Other producers and their public accounting:**
   - `crates/api/src/m16_route.rs:826-905`: lease expiry (current row, same generation, lease plus clock skew) or changed generation retires the epoch with `joined_route_member_stale` / `joined_route_generation_changed`. This is independent of the signed expiry computation; the signed route is not set to the seed lease wall.
   - `m16_route.rs:1623-1667`: on Start, retired state, generation change, signed expiry or closed carrier connections refuses/ends the epoch. `Observed::retired`, `:180-187`, also covers setup/carrier failure, closed state, death evidence and unsettled teardown. No separate three-minute idle timer was located in this carrier path.
   - `work.rs:2023-2090`: the idle owner watcher publishes the existing epoch failure, else `route_dropped` for an already-retired carrier, else `joined_route_member_ended` for an answered serve. It preserves `api_running`.
   - `m16_route.rs:2172-2183`: live qualification checks both carrier refusal and current signed-route verification. An expired/stale route can produce `stale_qualification` even without an observed `epoch_retired` snapshot. `crates/api/src/chat_service.rs:332-335` maps ANY inner 503 to `chat_route_not_qualified`; that code alone does not identify retirement.
   - Existing tests: `crates/api/tests/owner_work.rs:2287,2583,2698` cover continued workflow ownership after retirement, completed-then-retired qualification refusal, and publishing consent-exhaustion retirement. NOT RUN in Round 2; Round 1 results above are historical only.

**Physical timeline limit:** neither 10:35:07 (page observed Serving) nor 10:38:40 (first recorded refused chat) establishes the instant serving began or retirement occurred. The supplied B `phase=serving` observation at 10:14:11 makes "only 3–4 minutes into serving" particularly unsafe to infer. This round's targeted search of the evidence logs found the prepare completion (`a-run.log:4483`) and final gateway/carrier close lines (`:42001-42003`), but no recorded route expiry, epoch-retirement transition, or serve result/timeout outcome. The 10:41:23 close messages and later owner-pin loss do not identify which earlier producer fired. Repeated direct-dial timeouts explain transport delay; they do not themselves prove a terminal prepare refusal or determine the route wall. Do not promote either the 1,800-second expiry or the 600-second tracking candidate to the rv46 root cause without the missing observations below.

### Q2 — finished prepare payload and current-run identity (file:line, then test)

**A finished poll is not sufficient to satisfy prepare; a stale-result bypass was not located.** The legacy card can disagree with the joined path. If B genuinely never had the current shard, the native prepare path has no success payload to produce.

- Packaged producer: `crates/node-service/src/packaged_work.rs:874-923` reads the current verified announcement, checks live authority and operation admission, calls `Work::prepare_native`, records its result and invokes `Dispatcher::complete`. The member's `Work::prepare_native` needs native/shard custody and the current announced share before preparing (`crates/api/src/joined_member/work.rs:1260-1330`); its return is the signed resident proof, not a status card.
- Retention: `joined_member/work.rs:298-315` stores the completion with its ADMISSION scope, recovered through both operation id and the exact cancellation-Arc identity, never today's pin. `:242-254` returns `finished:true` only for the matching result id and scope. Scope matches both live authority token and control-service identity (`:81-84`); new control loops have a fresh service scope (`:605`).
- Authority replacement: `joined_member/work.rs:415-440` preserves a token only for unchanged pin, run_id, deployment_id and owner_incarnation. Replacing/clearing it permanently revokes the previous token (`:403-406`). `crates/node-service/src/packaged_work.rs:575-602` closes on changed run/deployment/pin, cancels on changed limits, and advances the accepted announcement pin. A numeric operation-id collision alone does not cross these lifetimes; ids also strictly increase within one dispatcher (`joined_member/work.rs:196-209`).
- Packaged owner binding: `crates/node-service/src/packaged_run/work.rs:1115-1127` obtains member work registrations for the CURRENT run and deployment before constructing `RemoteMember::already_joined` (`:1158`). That constructor verifies current registration and adopts the enrolled signer (`crates/api/src/joined_owner/remote.rs:383-409`).
- Poll consumption: `remote.rs:305-327` sends the enclosed reply after `finished:true`; `:455-465` checks `ok` before yielding the result. It does NOT itself compare a run_id/epoch tuple or explicitly recompare the inner result's operation_id. The signed control envelope is checked against member/owner endpoints, sequence, exact command digest and enrolled key (`crates/api/src/member_control.rs:140-162`); the request digest covers the result poll's requested operation id. The dispatcher chooses the retained record by that id and authority scope.
- Prepare admission is the second, essential boundary: `crates/api/src/joined_owner/link.rs:57-75` creates fresh single-use stage challenges and admits EVERY returned proof against the current stage offer. `crates/api/src/joined_shard/native.rs:53-84,143-169` checks current member, offer digest, assigned range, nonce and requested capacity. `work.rs:88-89,2100-2111` derives each transfer from this run's id; a previous run's offer/proof is not the current offer. The route epoch is assigned only AFTER successful prepare (`work.rs:1785-1807`), not a literal field the prepare poll compares. In this packaged path there is one route epoch per run, with fresh proof challenges. Do not invent an explicit prepare-time epoch check that the code does not have.
- Card distinction: `crates/api/src/local_agent.rs:563-588` emits `work_offer_not_published` from the legacy offer/native view, while joined slices push directly through `OwnerWork::node_slice_plans` (`work.rs:1429-1441`). The separate packaged report uses actual `native_owned` / `shard_owned` custody (`crates/node-service/src/packaged_work.rs:509-539`). Missing legacy offer publication/log entries therefore cannot establish absent joined custody. Conversely, a `finished` line alone cannot establish a successful proof either.

Existing identity tests: `crates/api/tests/owner_pin/result_retention_round2.rs:154,162,166,170,174,178,216` cover late completion, A→B→A, same-endpoint changed run/deployment, clearing, owner-process change and service replacement; `:207,211` preserve valid unchanged-authority retries. These are model-free result-retention controls (not a physical native-prepare run). NOT RUN in Round 2. No new adversarial previous-epoch prepare test or exhaustive race proof is claimed.

### Q3 — readiness defect, reviewed delta, RED/fix/GREEN disposition

**Located static mismatch:** `crates/owner-run/src/machine.rs:1041` sets `route_ready` solely from `State::Serving`. Retirement intentionally preserves that workflow/run continuation (`crates/api/src/joined_owner/work.rs:2023-2031,2084-2090`), and its monitor does not fail on general retirement (`:938-968`). The live gateway already refuses. The packaged page's status is the machine snapshot: `crates/node-service/src/packaged_run.rs:852-867` → `crates/api/src/owner_driver.rs:68-70,114-143`. Its headline uses `run.state`, not live route readiness (`crates/api/assets/desktop/run.js:94-108`); completed checklist details are rendered unchanged (`:110-113`). The packaged chat's snapshot uses the same DemoView (`crates/node-service/src/packaged_run/chat.rs:49-55`), while its gateway still obtains the retained gateway reader (`:60-76`; `work.rs:909-918`). This is the cross-boundary accounting defect, not a reason to loosen prepare checks.

**Review checked:** `/Users/mycelium-dev/Projects/mycelium-m4-program/REVIEW-laneEP.md:76-77,84-91` names F3 (idle-ended member still looks ready) and F9 (completed-then-retired readiness still OK). `work.rs:2023-2031` explicitly adopts those fixes while retaining `api_running, epoch_retired`. The authorized delta is consistent: preserve ownership, cleanup, Stop/rollover/ceiling semantics and historical completed steps; remove the CURRENT readiness/route-ready claim once retirement is observed. Do not turn retirement into automatic reopen, rerun prepare, or force a workflow failure. UI must distinguish "run still active, route unavailable" from a ready route rather than equating Serving with readiness.

**RED evidence:** NONE in Round 2. No new assertion was executed on `c2963eb4` production; a source trace is not a RED. The deterministic regression required is an actually-qualified model-free OwnerWork epoch, then a controlled member-end or carrier retirement, with `api_running` preserved and the packaged owner-run/page status becoming `route_ready:false` without Stop. A positive pre-retirement check and an unchanged completed-checklist check must prevent an always-false or forced-failure workaround; a replacement-run control must prevent old retirement contaminating a new run. Existing `owner_work.rs:2583` stops at the gateway and does not test the packaged machine/page projection. Existing packaged chat tests explicitly use a serving stand-in (`crates/node-service/src/packaged_run/work/tests/chat_route.rs:14-21`); they are not evidence of native qualification.

**Fix:** NONE. No production or test source changed. No new callback/state/wire field, no budget change, no alteration of RRP lifetime checks. There is no RED commit and no GREEN fix commit; only this diagnostic handoff is committed. No third round was started.

**GREEN evidence:** NONE newly run. Required `cargo test -p node-service --lib --features "packaged desktop-shell"`, `cargo test -p api --lib`, focused retirement/retention tests, fmt and targeted clippy are all **NOT_RUN — disk floor**. Do not carry Round 1's green counts forward as Round 2 verification. Documentation checks only: retained preflight output, source-reference audit and `git diff --check`; no code artifact or physical release pass.

### Exact missing instrumentation / visibility (not implemented under the stop gate)

Log-only, bounded records on existing transitions are sufficient; no polling additions, sleeps, HTTP probes, payload dumps or wire/state changes are needed. Records must include both event wall time and monotonic age, safe run/deployment/epoch/operation identifiers, and fixed outcome codes, with no bearer, key, grant, prompt, token, native-control document or full signed payload.

1. At `OwnerWork::start_serving` (`work.rs:1377-1390`): Publish wall, minimum planned worker wall, resulting signed expiry, run/deployment. At route delivery/startup completion (`:1799-1870`): epoch, delivery/startup timestamps and remaining signed lifetime.
2. At owner remote initial acceptance and terminal delivery (`remote.rs:281-283,305-327,343-362`): command/operation/member, tracking budget and elapsed age, and provenance **member completion vs local poll-wall expiry**. A local terminal timeout must emit a transition record even when every preceding poll succeeded; the existing repeat logger suppresses precisely that case. Do not print the reply/proof.
3. At member serve entry/exit (`joined_member/work.rs:1391-1448`, packaged result production `packaged_work.rs:910-923`): verified run/epoch/operation, remaining signed lifetime, remaining consent lifetime, and teardown/refusal category. Separate deadline from explicit cancel; existing `operator_cancelled` reporting collapses them.
4. At watcher retirement (`work.rs:2033-2090`): initiating member/operation result category, carrier retirement code, whether a request was idle/settled, and the exact published `epoch_retired` transition. At qualification refusal (`m16_route.rs:2172-2183`): inner closed refusal code, before chat maps it to generic 503.
5. At the SAME observation instant: sanitized owner-run `{state, route_ready, run_id, deployment_id}`, workflow `{state, epoch, epoch_retired, route_expires_at}`, and the qualification verdict. The existing general workflow snapshot contains gateway credentials; never dump it wholesale. These fields distinguish a stale page from a live stale status, a true member end from the synthetic owner tracking timeout, and retirement from expired signed qualification.

Until those observations (or the deterministic owner-driver/retirement REDs) exist, rv46's exact initiating event remains unassigned. The located readiness defect and owner tracking-wall mismatch are actionable findings, but neither is mislabeled as a verified physical root cause or a landed repair. Tier-escape disposition: physical UI/chat disagreement exposed a cross-package lifecycle/status seam that model-free owner-work + packaged status tests should cover; the existing gateway-only retirement tests leave that seam uncovered.
