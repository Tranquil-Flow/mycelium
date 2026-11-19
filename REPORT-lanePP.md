# Lane PP — prepare-honesty investigation (round 1)

Status: BLOCKED at the required RED gate. No production fix, no claimed release pass.

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
