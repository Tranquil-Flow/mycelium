# A13 integration handoff — current working source, not a release

## Parallel returns — parent reconciliation

All three assigned workstreams have returned; the active-assignment description
below is historical. The native worker now reuses PhysicalNodeProcess's existing
command demultiplexer and adds closed live-route operations, exact-command grant
checks, admitted-load checks, atomic reservations, send_before and request-scoped
interruption. Parent read both returned files and the native response validator.
The reconstructed envelope retains the native-validated result/error code;
transport/admission errors are not relabelled as native errors. The private
_RemoteNodeError dependency remains a compatibility coupling to this core version.

Parent ran test_worker.py, test_worker_limits.py, test_worker_concurrency.py,
test_control_pump.py and test_concurrent_pump.py together: **40 passed in 10.53s**,
exit 0 (`outputs/a13-macos-evidence/worker-parent-verification.log`). Worker,
new concurrency tests, canonical process boundary and concurrent pump hashes
were unchanged across execution. Worker SHA-256:
`b0227bedf109075cef133d971ba1958bc83a7fca1b75a22d5b20b809c9ce026a`;
new test SHA-256:
`8f3dc9bc24268ac75dbf60a74d8dbd1e2eca3b50837da6f49378bfea6f3e8671`.
The files remain untracked on demo/a13-macos at b9001e6ac3fc11dd9a16f451426453621b24852a;
no staged paths were present. git diff --check passed (not an untracked-file check).
The worker's original RED/native-demultiplexer counts are self-reported;
worker-concurrency-prefixed logs were not found in the evidence directory.

Disposition: locally exercised worker component, awaiting full managed serving
integration and physical acceptance. Real-native coverage is hello/pre-load
semantic rejection/cleanup; blocked-inference overlap tests use a labelled
in-memory session, not model execution or proof of loaded inference cancellation.
This return used gpt-5.6-sol; the provisioning/review batch reported MiniMax-M3.

Provisioning return hashes were verified and its focused suite independently
passed 29 tests; it is an admission helper, not implemented target staging.
The concurrency review's claimed defect list was not accepted wholesale: see
`outputs/a13-macos-evidence/concurrent-review-parent-adjudication.md` for source-
and-repro-based corrections. Existing six concurrency/recovery files separately
passed 26 tests. Do not add these overlapping selections into a total distinct
regression count. No new whole-suite/final-package qualification is claimed.

Remaining core work includes target-owned staging/run-profile production,
canonical controller/live-supervisor wiring, durable retention/quota recovery,
maintained client integration, final packaging and actual two-Mac acceptance.
A13 remains **INCOMPLETE — LOCAL WORK REMAINS**, with no physical/public activation.

## Active parallel continuation — not a frozen candidate

The owner authorized additional subagent help. Three bounded workstreams are
active: concurrent native worker implementation (`worker.py` and dedicated
tests), target-owned provisioning admission (new `managed_provisioning.py` and
dedicated tests), and read-only concurrency review. The parent owns mailbox,
concurrent pump, owner waiter/session integration and final acceptance. No shared
source file has multiple assigned writers; no model/fleet/public activation is
included in these assignments.

The parent has added opt-in signed poll v2 with bounded active claims (one work,
two control), cleanup/control priority, a durable concurrent target pump, and
per-command owner waiters with `send_before` and local interruption. Focused
model-free TLS/native-hello tests passed, including control progress while a
dispatch callback is blocked and late-result retirement. These do not prove
loaded inference cancellation or final integration. Native-worker and provisioning
subagent results still require parent inspection and combined verification.

The 500-pass regression and 69-file source index below belong to the preceding
recovery/stop candidate, not these active edits. The source is moving again;
do not treat older hashes or the preview package as current qualification.
Managed serving remains unavailable/unqualified, and the A13 goal is incomplete.

## Latest continuation — bounded recovery and stop repairs

Implementation resumed after the initial integration handoff below. The overall
verdict remains **INCOMPLETE / not_yet_serving**, and managed live serving must
not be activated from these results. This section supersedes the initial status
of the specifically repaired review cases; it does not erase the old report.

- Added `mycelium_a13_macos/durable_requests.py` and v2 owner/pump journals.
  Exact signed enqueue/poll/ack requests are durable before HTTP. Cache-cleared
  restart now recovers a pre-server enqueue crash and committed poll/ack response
  loss, with one observed native invocation and a subsequent command completing.
- An unknown native effect (crash after invocation before output persistence)
  remains fenced; tampered persisted signatures are rejected before transport.
  Recovery is bounded by the original signed freshness window. Legacy journal
  migration is conservative and has source review, not dedicated runtime proof.
- Relative owner deadlines now begin before the session lock, bound signer-lock
  acquisition, and are rechecked after membership lookup and persistence before
  enqueue. Remaining budget caps HTTPS open/read timeouts and response-body reads.
  A real committed TLS response deliberately stalled mid-body is cut short.
  This is not proof of cancellable DNS, every public TLS phase, or live-priority
  cancellation; `send_before`/interrupt/concurrent inference work still remains.
- Concurrent close calls now share one stop intent; `stop_command_id` and
  `ManagedControlOwner.reconcile(command_id)` allow a lost stop result to be
  retrieved after owner restart without a second native stop. Local handle
  closure still does not claim an observed remote process exit.

Final parent regression: **500 passed, 3 skipped**, exit 0. JUnit independently
reconciles 503 testcase elements, zero failures/errors and three
`staged deployment absent` skips. Selection includes A13 (excluding package
execution), request gateway, selected live suites, physical-runner config and
physical-controller tests. Evidence:

- `outputs/a13-macos-evidence/recovery-stop-parent-regression.log`
- `outputs/a13-macos-evidence/recovery-stop-parent-regression.xml`
- `outputs/a13-macos-evidence/integration-handoff-recovery-source-index.json`
  — 69 changed/untracked source/test/build inputs; successor to the original
  65-file handoff index, which remains historical.
- `outputs/a13-macos-evidence/control-recovery-review.md` — independent scoped
  acceptance of the three recovery windows and two negative controls. It is not
  a whole-production review; later stop changes have parent regression evidence.

**Still unresolved:** replay-cache/SQLite retention and lifetime quota exhaustion;
concurrent target claims/native demultiplexing/reserved cleanup lane; the full
native inference/control command surface; target-owned stage/profile production;
provider threading into the maintained live supervisor; public bootstrap;
aggregate model/network/run grants; packaged two-Mac acceptance and final release
handoff. Preview.2 remains older than this source. No conversion, model load,
inference, physical two-Mac run, public activation, staging, commit or push was
performed in this continuation. A combined source regression does not close C1–C8.

## Initial integration decision — retained handoff history

**MACOS A13 DEMO INCOMPLETE — LOCAL WORK REMAINS.** Role: `not_yet_serving`.

You can review and selectively integrate the generic token-ID/representation changes and the locally exercised onboarding/acquisition components now. **Do not enable or advertise the new mailbox/worker bridge as a production serving path.** The newest review identifies unresolved crash-recovery, deadline, retention and cleanup defects, and the live inference/cancellation contract is not implemented by that bridge.

This handoff was requested by the owner when implementation was interrupted. It is context for coordination across the A13, inference-verification and general demo sessions, not authorization to merge into protected checkouts or activate infrastructure. No further feature implementation is implied by this document. The existing `A13-MACOS-DEMO.md` remains the C1–C8 evidence ledger; this document explains integration boundaries and links to it.

## 1. Authority, location and ownership

Read `/Users/evinova-self/Desktop/ethonline-A13-execution-goal.md` in full before changing scope. Its latest core-versus-hackathon correction is binding.

- Owned lane: `/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo`.
- Branch: `demo/a13-macos`.
- Base and current HEAD: `b9001e6ac3fc11dd9a16f451426453621b24852a`.
- All implementation remains **uncommitted**. The latest `git diff --cached --stat` was empty; nothing is staged. No commit/push/PR was made.
- Much of the implementation is **untracked**, including the entire `mycelium_a13_macos/` package, A13 tests, `mycelium_live/representation.py` and build files. `git diff` alone does NOT export this work.
- `outputs/a13-macos-evidence/integration-handoff-source-index.json` records 65 changed/untracked source, test and build-input files with hashes. It is a working-source inventory, NOT a complete dependency closure, committed snapshot, signed release or acceptance seal. Existing tracked dependencies remain supplied by the baseline checkout.
- Read-only original target: `/Users/evinova-self/Documents/playground/mycelium-wave8-integration`. Earlier inspection found separate dirty A13-lite work there; recheck live state before any owner-controlled integration. This session did not modify it.
- Protected hackathon workbench: `/Users/evinova-self/Documents/playground/mycelium-parallel-prompts-3zwvxhg7/foundation/continuations/hackathon-app-03/workbench`.
- Protected W6 runtime: `/Users/evinova-self/mycelium-physical-run/w6-ethonline-20260912T090309Z`; ports 8791, 8876, 8877, 4350, 4351, 4352; public origin `https://m4pro.tail53d0d3.ts.net`.
- Do not touch Mycelium A/B/C research sessions/worktrees. Do not borrow W6 membership, weights, plans, profiles, credentials, signatures or qualification receipts.

No public listener/tunnel/DNS/publication, signing-account submission, model conversion/load, fleet reservation, paid inference or funds movement has been authorized by the A13 model selection. No secrets in chat/logs/evidence. Owner commits; no agent/co-author trailers. Stage only explicitly reviewed paths when authorized.

## 2. Boundary with verification and the existing demo

**A13 is core Mycelium, not the verification application.**

Core owns installation-local identities/authority, safe membership pairing, consent/resource policy, model acquisition/provisioning, native worker lifecycle, qualification/selection, inference streaming and generic request-bound receipts/work evidence.

The separate hackathon application owns verifier endpoints/authentication, scoring/ensembles/banks, audit dispatch, verifier consent/UI and checker policy. None of those are A13 dependencies. This session has NOT implemented or qualified the inference-verification system; its other owner must provide that status. No verifier URL/auth configuration/plugin framework should be reintroduced into core.

Useful shared surface delivered here: real native token IDs can propagate through request-event v2, and representation identity is derived from canonical authority instead of a hard-coded quantization label. Neither surface establishes output correctness or proves a particular checker works. A checker consuming token IDs must require genuine native IDs from the appropriate backend; the additive field can be absent on other producers.

The A13 route is NOT the existing hosted W6 route. Existing demo-serving evidence from another session remains separate; do not call it fresh A13 two-Mac qualification. Other independently ready demo/checker work need not wait for A13 completion, but that is not permission for this lane to mutate those runtimes.

## 3. Exact model contract and actual acquisition

- Model: `Qwen/Qwen2.5-0.5B-Instruct`.
- Revision: `7ae557604adf67be50417f59c2c2f167def9a775`.
- Upstream source: original BF16 Safetensors.
- Runtime: MLX float32 arithmetic, `int8-weight-only`.
- Decode contract: `quantized_greedy_token_id`, sampling seed 0.
- Two separate real Macs are required. Same-host processes must continue to fail distinct-host qualification.

Owner chose **Hugging Face download per Mac**, not a peer-to-peer model-weight distribution network. Coordination of assignments/worker commands is a separate problem; existing Iroh carries native inference traffic. HF source acquisition does not perform the BF16-to-int8 conversion or produce per-stage runtime packs.

Actual browser-driven public acquisition completed:

- Complete repository bytes recorded in the acquisition ledger: **999,604,126**.
- Weight/source bytes: **988,097,824** (the receipt's `source_bytes` field).
- Manifest: `sha256:8f0f4a5de5449a5ebf071db0d7ad30a267e7b9d94844ac847f8b48ba2cdcd656`.
- Receipt: `outputs/a13-macos-evidence/public-source-acquisition.json` and `.log`.
- Receipt explicitly states `model_execution=false`, `conversion=false`, `physical_two_mac=false`, `launcher_closed=true`.
- Cached public source: `outputs/a13-macos-evidence/public-source-installation/model-source/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/7ae557604adf67be50417f59c2c2f167def9a775`.

The parent installation directory also contains private application state. **Do not export that directory or all of `outputs/`.** Cached public model files are not private W6 artifacts, but their presence is not authorization to load/convert/run them.

## 4. What exists and what it actually proves

### Installation, authority and pairing

Files: `mycelium_a13_macos/{__main__,app,authority,tls,pairing,peer,target_app,capability}.py`, `web/{index.html,app.js,style.css}`.

- A self-contained Mac launcher opens a private loopback browser application via installation-owned launch/session authorization.
- Fresh durable seed/owner and target identities; target role does not create a seed.
- Explicit cooperative-peer/resource consent, membership lifecycle, revoke, retained private state, restart/reconnect and capability reporting.
- Recipient-encrypted pairing: target identity/request first, owner review/approval, encrypted return, canonical authenticated membership join. Plaintext invites are not UI/URL/log artifacts.
- Real canonical TLS/SQLite signatures and separate browser storage contexts have been exercised.
- Capability reporting includes a real tiny MLX probe; this is not model serving or physical qualification.
- Verifier-specific placeholders are removed from current core source and preview.2.

Limit: these are installation-local/loopback compositions, not a managed cross-network two-physical-Mac onboarding result. Supported normal-user public bootstrap remains missing.

### Acquisition and source/assignment preparation

Files: `acquisition.py`, `resources.py`, `native/resource_probe.m`, `serving.py`, acquisition wiring in `app.py` and web files.

- Pinned HF metadata/file acquisition, visible size/progress, consent, integrity/capacity checks, cancellation/interruption/resume and bounded failure behavior.
- Resource probe uses actual macOS observations; UI and worker safety tests also contain explicitly labelled injected policy failures.
- `prepare_local_source` uses the real immutable manifest compiler without loading tensors.
- Authority assignment planning produces fresh canonical assignments bound to membership generations/incarnations, seed and prepared manifest.
- Assignment tests use synthetic model metadata with real compiler/membership services. Verdict remains `assignments_compiled_not_provisioned`, `route_ready=false`.
- `prepare_registry_launch` / `RegistryLaunch.run` point toward the actual native supervisor and enforce model/topology approval checks. They do not implement the missing application-owned producer chain, and `run` has not been authorized/exercised with a model here.

### Native worker and authenticated lifecycle bridge — newest, not release-ready

Files: `worker.py`, `managed_control.py`, `control_pump.py`, `control_session.py`; composition in `authority.py` / `tls.py`.

- `ManagedWorker` owns a real fixed native subprocess, observes membership/policy/RSS/storage, enforces command/wall limits and has an independent elapsed-time watchdog with cleanup reserve. Configure/start require an explicit callback grant.
- Actual native no-model hello/stop and process-group cleanup have been exercised. This is not loaded-model resource qualification or a durable aggregate execution grant.
- `ManagedControlMailbox` supports signed `/managed-control/enqueue`, `/poll`, `/ack`, `/result` on the canonical seed TLS listener.
- Requests bind owner/target key identity, generation/incarnation, run, deployment and exact command. Canonical signatures, replay guards, SQLite persistence, freshness and current-member checks are present.
- `TargetControlPump` can execute a claimed lifecycle command on the real native worker and acknowledge its actual native output. No invented native failure receipt is emitted for a pre-execution admission refusal.
- `ManagedControlOwner` / `ManagedNodeSession` implement durable owner intent/sequence plus a native `send`/`close` adapter; exact repeat commands query results rather than blindly re-executing.
- Real TLS tests cover native output returned to the owner, one post-acceptance lost-enqueue recovery, conflicting retry refusal, storage failure before enqueue, exclusive owner lock and revocation during result retrieval.
- A real native hello passes the canonical `QualificationController._hello_identity` consumer through the injected factory; the one-host physical-distinctness gate still rejects it.

**These passing slices do not close recovery or live-session semantics. See the blocking review below.**

### Canonical transport selection — newest source-only integration foundation

Files: `physical_inference_qualification.py`, `mycelium_physical_runner/config.py`, `tests/physical_runner/test_config.py`, `tests/a13_macos/test_managed_transport.py`.

- Explicit `managed_https` peer/plan mode now exists.
- Managed peers require null SSH target/key; mixed managed/legacy plans are rejected. Existing local-entry/SSH-other legacy rules remain.
- `_peer_process_argv` returns an empty tuple for managed peers; only an explicitly supplied managed factory accepts it. Default native subprocess construction rejects it. No shell/SSH command is sent through the mailbox.
- Physical controller `prepare`/`cleanup` explicitly reject managed peers with `managed_artifact_lifecycle_requires_target_owner`. This is an honest missing-target-provisioner boundary, NOT implemented managed staging.
- `ManagedControlOwner.session_factory` matches the canonical constructor keyword shape and rejects nonempty launch argv.
- The factory is **not threaded through** `PhysicalLiveRoute.from_operator_plan`, supervisor entrypoints or the Mac application's real serving composition. It would be premature to do that before the live-session contract below is implemented.

## 5. Review blockers that the integrating agent must know

Read both:

- `outputs/a13-macos-evidence/owner-control-review.md` — risk review, exact source hashes and reproduction recipes.
- `outputs/a13-macos-evidence/owner-session-integration-map.md` — consumer methods, required native commands, 15 insertion points and target-stage boundary.

The parent independently checked that the four reviewed production-file hashes match current bytes. The reviewer ran the focused tests; several findings are source-traced reproduction recipes, not executed adversarial regressions. They remain unresolved. Implementation was paused for this handoff, not repaired after receipt of the review.

### Concrete unresolved bridge defects

1. **Pre-enqueue crash gap:** owner journal retains only binding/digest. A crash before the server receives enqueue leaves an intent whose retries query an unknown command forever. Exact request identity/signed bytes and recoverable phases are not durable.
2. **Lost poll/ack wedges the target:** a committed claim whose poll response is lost cannot be recovered by the next fresh poll; a lost terminal ack leaves `pending` blocking every subsequent step. Existing ack-loss test proves no re-execution but intentionally remains fenced — it does NOT prove usable recovery.
3. **Not an absolute end-to-end deadline:** signer-lock waits and HTTP calls can exceed the nominal session timeout; a call queued behind the lock can enqueue after its own deadline. The live adapter must preserve caller-owned absolute cancellation deadlines and cap/cancel transport work.
4. **Unbounded retention / lifetime quota exhaustion:** client prepared-request cache and SQLite authentication rows have no retention policy. Owner command/request caps count installation-lifetime records rather than outstanding work; restart does not restore service capacity. Retain durable anti-replay high-water state separately from bounded retry caches and compact terminal work without weakening replay protection.
5. **Concurrent close/uncertain stop:** close is not one lock-linearized operation with one durable stop ID; concurrent close calls can submit separate stops, and an uncertain stop cannot be reconciled through the current API.

### Missing live inference semantics, distinct from those defects

Current mailbox/worker allowlist is only `hello/configure/start/health/snapshot/cancel/stop`. Canonical consumers also need:

- `bind_request_control`, `update_request_control`;
- `infer_start`, `infer_decode`;
- `infer_cancel`, `infer_cancel_wait`;
- `rotate` for recovery.

Mirror native closed payload schemas; do not add generic exec, Python scripts or arbitrary argv.

The current pump, worker and owner session serialize work; the mailbox permits only a single outstanding claim for a binding. A long inference command would therefore block polling/executing cancellation and receipt snapshots. Required next work is concurrent native command demultiplexing and a bounded reserved cleanup/control lane, not simply expanding an allowlist.

The live route session contract also requires `send_before` with an absolute monotonic deadline, `interrupt_command`, honest session-liveness/`returncode` behavior, bounded sanitized `stderr`, and idempotent close that does not fabricate target exit. These are not all provided by `ManagedNodeSession`.

Some integration-map discovery text predates the latest enum/parser edits. In particular, its claim that the parser accepts only local/SSH is historical now. The current implementation uses all-managed mode and empty argv as a fail-closed factory sentinel; the map recommends branching before argv construction instead. That design recommendation has NOT been adjudicated as a final integration contract. Use current source symbols, not stale line numbers.

## 6. Generic native deltas worth reviewing separately now

### Token IDs

Files:

- `mycelium_request_gateway/{backend,contracts,service}.py`;
- `tests/request_gateway/test_physical_backend.py`;
- **untracked** `tests/request_gateway/test_token_ids.py`.

Actual router token IDs flow through `_GatewayTokenSink` → `TokenEmitter` → service → optional `StreamEvent.token_id` on request-event v2. V1 wire shape remains unchanged. Replay hashes bind token ID plus text, so equal decoded text with different token IDs is not accepted as identical replay.

Existing export: `outputs/a13-macos-evidence/gateway-v2-token-id.patch`, SHA-256 `63abe4a64dc615bddbfaa65795fa1b92d130e29045259b644a57ad4e16ce0c1d`.

**Export caveat:** its diff headers cover the three production gateway files and `test_physical_backend.py`; it does NOT include untracked `test_token_ids.py`. Carry that test separately or regenerate a complete reviewed export. Earlier read-only apply-check against wave8 is not target application, merged-byte equality or final combined regression proof. Callback consumers expecting exactly two positional emitter arguments must be reconciled with the new optional third argument.

### Representation authority

- New **untracked** `mycelium_live/representation.py` derives canonical deployment/model/revision/quantization projection and rejects absent/mismatched authority.
- `mycelium_live/supervisor.py` uses this before startup, validates runtime identity, and passes the actual quantization into qualification/registry projection.
- `mycelium_live/route.py` no longer silently defaults qualification quantization to int8; `set_deployment_qualification` requires explicit quantization.
- Consumers must take these together. Do not copy the supervisor change without `representation.py`, or update only one caller of the now-required argument.

### macOS native process cleanup

`mycelium_node/process.py` handles framework Python's legitimate second exec by retaining unchanged process-incarnation ownership and pre-pinned executables rather than rejecting a transient post-handshake executable change. This is a sensitive cleanup boundary; review with `tests/a13_macos/test_native_cleanup.py` and `A13-MACOS-NATIVE-CLEANUP.md`, not as an incidental UI change.

## 7. Package and platform status

Current tested preview directory: `dist/0.1.0-preview.2/`.

Actual executable rehashed for this handoff:

`dist/0.1.0-preview.2/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo`

SHA-256: `6a6f335e659524631e6afa38b45644cde59ca9966c9c53949cb7f0f18389c179`.

This is an **executable digest**, not a ZIP digest. Artifact identity also records resource-probe SHA-256 `f5fefbbdeb836b78c7e0a1737742a957d8015e8585eeae29f80eab02c68ee6c7`.

- Apple Silicon arm64/macOS 26.5.1 exercised.
- Ad-hoc signed only; no publisher certificate; NOT notarized.
- Contains acquisition and verifier-free UI.
- **Predates the worker/control/session/managed-transport changes.** No package receipt qualifies those new modules.
- Earlier delivered preview.1 and its receipts remain historical.
- `A13-MACOS-QUICKSTART.md` still describes preview.1, disabled acquisition and old verifier placeholders. It is NOT a current runbook; do not copy its verifier requirements into core. It must be superseded for the finished artifact without changing historical package receipts.
- Second-Mac package startup/install is unverified. Earlier read-only hardware discovery identified the laptop as macOS 15.6.1/16 GiB; this is a compatibility target, not qualified support. Recheck resources before proposing a run.
- Build inputs: `scripts/build_a13_macos.py`, `Mycelium A13 Demo.spec`, native resource helper source. Do not distribute pycache, private state or the entire evidence/install tree.

## 8. Evidence actually retained

Paths below are relative to `outputs/a13-macos-evidence/`. Counts overlap and are NOT additive; no single final combined test run covers all current source/package bytes.

| Evidence | Actual result | Scope / limit |
|---|---:|---|
| `acquisition-parent-regression.log` / `.xml` | 310 passed, 3 skipped | Acquisition-era A13 + gateway + selected live regression. Before worker/control/session edits; physical staged-deployment prerequisites account for skips. |
| `browser-acquisition-green-02.log` | 16 passed | Browser/app acquisition with real small local HTTPS origin; distinct from public HF run. |
| `public-source-acquisition.json` / `.log` | status passed | Actual browser/public HF acquisition and manifest; no conversion/load/inference. |
| `package-acquisition-green.log` | 2 passed | Built preview.2 package path; not current worker/control code. |
| `worker-review-green.log` | 19 passed | Real no-model native child plus labelled policy fault injection and cleanup. |
| `managed-local-composition.log` | 39 passed | Earlier combined TLS/pump/worker/acquisition composition on one Mac. |
| `control-result-green-02.log` | 15 passed | Owner-result endpoint, mailbox, pump and canonical authority/control tests. |
| `control-session-green-02.log` | 7 passed | Owner/result slice before the later canonical-consumer test. |
| `managed-transport-green-02.log` | 146 passed | Latest parent run: managed transport, owner session, physical-runner config and physical-controller tests. Does not include all A13/browser/package tests. |
| `owner-control-review.md` | reviewer reports 8 passed | Five owner-session + three result tests on matching reviewed source; unresolved review defects remain. |

Latest parent 146-test selection:

```sh
cd /Users/evinova-self/Documents/playground/mycelium-a13-macos-demo
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -B -m pytest -q \
  tests/a13_macos/test_managed_transport.py \
  tests/a13_macos/test_control_session.py \
  tests/physical_runner/test_config.py \
  tests/physical_qualification/test_controller.py
```

Retain RED and intermediate failure logs. A fixture revocation call initially omitted its generation, and a canonical-consumer fixture initially used an unsupported `inert` mode instead of `dry-run`; these were fixture errors, corrected without weakening production validators. The response-time revocation regression was a real behavioral RED and was fixed. None of that closes the newly returned broader recovery/deadline review.

The terminal tool's returned `cwd` field has sometimes named another concurrent session's directory even when explicit `pwd`, imported tests and written logs were in this lane. Use explicit `cd` plus absolute evidence paths and verify actual command output; do not infer protected-checkout mutation solely from that field.

## 9. Remaining critical path / expected completed A13 handoff

1. Repair the reviewed durable request/response state machines, bounded retention and stop reconciliation.
2. Implement the actual concurrent live-session contract, native inference/control commands, caller-owned deadlines and reserved cancellation/receipt lane; prove with inert fault tests plus real model-free process composition.
3. Implement target-owned, digest-bound staging/provisioning and persisted run-profile admission. HF download is only source acquisition. Preserve canonical markers/acknowledgements, stage/graph/load-proof chains, target-local roots and identities outside wiped staging roots. No arbitrary remote staging scripts through the native mailbox.
4. Thread the qualified provider through `RegistryLaunch` → supervisor `_qualified_runtime` / `run_registry_server` → `PhysicalLiveRoute.from_operator_plan` → existing route/session factory; preserve the maintained runtime/browser rather than creating a duplicate inference UI.
5. Finish managed public bootstrap and normal-user pairing, plus complete resource/network/run accounting and recovery UI.
6. Once an actually dependency-closed physical tranche exists, obtain exact model/revision/representation, both hosts, attempts/loads/generation counts, RSS/disk/network/time/cost limits, evidence destination and cleanup allowance. Conversion, target preparation and startup challenges must be inside that approval, not hidden prerequisites. Current `RESOURCE_PROPOSAL` is a proposal, not a grant or measured complete budget.
7. Run fresh packaged two-physical-Mac pairing/acquisition/staging/load/startup challenge/qualification/selection, then ordinary browser inference with successful terminal output and request-bound per-placement work on both Macs. Exercise cancellation, revocation/expiry/stale qualification and retained-identity recovery.
8. Rebuild the final package, verify exact final source/artifact bytes, run combined regression and isolated maintained-consumer integration, close review findings, and deliver updated runbook plus reviewed explicit staged paths or a complete portable patch and suggested owner commit message. Owner retains publication/merge authority.

Expected completed delivery to the integration agent is therefore not merely a new ZIP: it must include a usable managed lifecycle/provider composition, native authority-bound model/representation and generic streaming/receipt interfaces, actual two-host evidence, supported-platform/package identity, complete source export including untracked files, and C1–C8 closure. It must work with no verifier configured or reachable. None of those unrun outcomes should be pre-announced as successful.

### C1–C8 right now

- C1 partial: first-Mac packaged onboarding/restart exercised; second-Mac install and final package pending.
- C2 partial: canonical local pairing exercised; normal-user different-network two-Mac path missing/unrun.
- C3 partial: real HF acquisition and source preparation; managed stage provisioning and second-Mac acceptance pending.
- C4 incomplete: lifecycle/control building blocks and canonical hello consumer exist; live contract/producer/load/qualification/registry composition missing.
- C5 unrun: no fresh A13 two-Mac inference or browser terminal job.
- C6 partial: model-free worker guards exercised; reviewed bridge recovery/deadline defects and full loaded-worker/network/run-budget acceptance pending.
- C7 incomplete: verifier coupling removed and selected generic seams tested; maintained end-to-end consumer integration not qualified.
- C8 incomplete: previews/historical receipts only; no final reviewed release/integration artifact.

**Integration recommendation:** review small generic deltas independently now; consume onboarding/acquisition as explicitly scoped preview functionality if useful; keep managed serving disabled until its real contract and review blockers are closed. Do not wait on verifier availability, and do not describe this lane as merely waiting for approval while substantial local implementation remains.
