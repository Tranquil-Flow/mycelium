# macOS A13 hackathon demo

## Current acceptance status — round 12 continuation

**MACOS A13 DEMO INCOMPLETE — LOCAL WORK REMAINS.** This section supersedes
older implementation/readiness statements below, which remain historical records.
The owner's current scope is macOS-first core APIs/runtime, **no frontend and no
verifier prerequisite**. Subsequent full A13 approval authorized the scoped two-Mac
experiments and managed public relay; it does not authorize unrelated publishing,
paid services, protected checkout changes, or secret disclosure.

| Gate | Current evidence and remaining boundary |
|---|---|
| C1 | Packaged applications have run on the real arm64 macOS 26.5.1 owner and 15.6.1 laptop, with retained identities across restarts. Final clean-install acceptance remains open. Ad-hoc signed, not notarized. |
| C2 | Recipient-encrypted enrollment and retained-identity public-HTTPS reconnect exercised. SSH carries private operator input only, not core control/staging/inference. Different-network acceptance remains unproven. |
| C3 | Selected Qwen source acquired and integrity-verified via packaged application APIs: 999,604,126 bytes. Download interruption/resume has earlier evidence; final-candidate clean-state repetition remains open. |
| C4 | Packaged two-Mac native bootstrap, distinct host/boot observations, canonical compilation, and both exact work-consent previews verified. Workflow fails before work acceptance and requests cleanup. No successful native load/qualification claimed. |
| C5 | No verified distributed user inference, successful terminal receipt, or per-placement inference counters yet. Stream driver exists but has not reached execution. |
| C6 | Control-only stop/reconnect and extensive boundary tests exist. Loaded-worker cancellation/revocation/loss/recovery remain unexercised. |
| C7 | Adapter composes canonical compiler/controller/registry and existing APIs. Maintained request-gateway CLI/stream tests passed (7). Physical consumer and isolated integration acceptance remain open. |
| C8 | No final candidate closure. No staging, commits, pushes, or protected-checkout changes in this continuation. |

### Verified repairs and receipts

- Canonical assignments selected uniquely by target/deployment identity, not by
  assuming native IDs occur in compiler filenames. Ambiguity fails closed.
- Full normalized MLX runtime matches manifest architecture/configuration and
  selected backend/dtype/quantization pins. Corrected exact-file GLM review found
  no blocking bugs; the first mis-scoped review was discarded.
- Public staging chunk size is 512 KiB, retaining the relay's 1 MiB encoded frame
  limit. 1 MiB raw chunks were physically rejected; that failed attempt remains
  historical evidence, not a successful candidate.
- `parent-round12-regression.{json,log}`: **658 passed, 3 skipped**, exit 0, no
  source changes during the run. This predates the subsequent diagnostic edit.
- `preview22-package-receipt.json`: build/smoke exit 0. Deployed executable hash
  matched both Macs. Both exact work previews succeeded (owner 6.814 s; laptop
  39.3 s). The subsequent workflow failure is NOT qualification or inference.
- Added private original-exception retention for serving failures, after a failing
  regression. `round12-diagnostics-green.log`: **8 passed**.
- Preview.23 is a provisional diagnostic successor, built and deployed with matching
  executable hash `7c9eb85d44c415d6e85739b71b1cea17dad25d724916d45d133b4b54763919af`.
  Its next physical attempt is in progress; no outcome is credited here.

Evidence is under `outputs/a13-macos-evidence/`; private runtime and RPC records
remain in the lane-owned installation state, not committed/exported. Earlier
preview artifacts and failed runs are retained. Current branch is `demo/a13-macos`,
HEAD `b9001e6ac3fc11dd9a16f451426453621b24852a`, with uncommitted source/tests.


### Round 13 continuation — managed native boundary

Preview.25 reached the real registry API and both Macs accepted work offers and
started native workers. The registry remained unqualified; the stream driver
stopped before inference submission. Live status identified
`run_plan_endpoint_secret_file_invalid`, with zero applied operations/frames.

The following repairs are implemented and locally verified, not yet credited as
physical serving:
- Installed-app registry startup explicitly omits source-checkout governance;
  the existing endpoint reports unavailable, never governance/release GREEN.
- Seed identity/receipt verification may sample a caller-supplied clock after
  transport. Fixed-time callers and signature/expiry verification stay unchanged.
- Managed HTTPS identity paths retain absolute/identity-directory constraints but
  do not inherit the raw SSH lowercase directory-prefix convention. The actual
  target-owned key selection/pin checks are unchanged; SSH guards remain intact.
- The factory conservatively caps canonical 930-second session requests at the
  existing managed 240-second maximum, rather than misreporting them as stale
  membership. The per-command cap was not extended.

`round13-native-adapter-green.log`: **111 passed** (canonical controller, managed
factory and control-session checks), following two reproduced regressions.
Preview.26 build/smoke succeeded and its executable hash matched on deployment:
`d38187cb7ee20480da3a9b65028b7906d17a0f97d33d9a03e716cacd85c49296`.
Its two-Mac load/qualification/stream attempt and independent bounded review are
in progress. No final source/artifact qualification is claimed.

### Round 14 continuation — native startup and journal bounds

Both real Macs returned native `configured` observations under preview.29.
`round14-two-mac-configured-verification.json` records verification of both
signatures, enrolled public-key matches, process/host bindings, and agreement
with the plan's Iroh activation identities. This is NOT full route qualification.

The native `start`/`rotate` consent grammar was corrected from scalar peer names
to canonical peer-binding objects. Graph membership, non-self/duplicate checks,
and exact local generation are enforced. The focused gate was **95 passed**.
Preview.30 then configured and started both workers and generated the first three
startup challenge tokens `[19202, 13, 151645]`; required output also includes
`198`. No successful startup qualification or user inference was credited.

The retained owner journal was 261,885 bytes, immediately below the generic
256-KiB state-file limit. Command journals now have a separate **4-MiB** bound
and **1,024** retained-command ceiling. Counts, sequence reservations, signed
intents and replay history are preserved; identity/configuration files retain
256-KiB defaults. Large signed-intent history save/reopen and adjacent tests:
**25 passed**. Safe wrapped native failure codes are retained in registry status
without exposing arbitrary exception text (**42 passed, 3 skipped**).

Preview.31 was built and deployed with matching executable hash
`bafca9c191a10722338fbe66bb7ca9fc9ff0cbd50949590db4e1e4077ca51b14`.
Its live attempt and fresh combined regression are in progress. The previous
stable combined gate was **826 passed, 3 skipped**, before the peer-grammar and
journal changes. Final C1–C8 closure remains open.

## Historical implementation checkpoints


## Current core-only integration checkpoint

**MACOS A13 DEMO INCOMPLETE — LOCAL WORK REMAINS.** Planning estimate: roughly
60–65% complete, subjective engineering estimate, not an acceptance percentage.
No frontend build/bundling or verifier integration is required from A13. The
ETHGlobal agent owns those in the consuming application. Older UI requirements
below are historical where they conflict with this owner correction.

Integrated and parent-exercised in the latest continuation:
- Managed builder build()/CLI entrypoint: 99 passed
  (`outputs/a13-macos-evidence/glm-round3/builder-parent-integration.log`).
- Target `start_work`: signed staging plus retained preposition binding and an
  explicit local per-command load authorizer. Activation remains not-loaded and
  not-qualified. Real native hello and pre-load denial exercised: 22 passed
  (`target-work-green-01.log`).
- Bounded signed-request retention with durable byte-identical replay: 50 passed
  (`retention-parent-green-01.log`, includes target activation regression).
- Explicit owner-local staging-offer retirement after target-confirmed cleanup,
  durable bounded receipt history, immediate next-offer admission: 18 passed
  (`offer-retirement-green-01.log`, includes adjacent activation/staging tests).
- API-only registry serving via `RegistryLaunch.run(..., api_only=True)`:
  61 passed, 3 skipped (`glm-round4/headless-parent-green.log`). Real loopback
  HTTP/ASGI composition; unavailable runtime intentionally remains unavailable.
- Native hello retains the canonical derived run-scoped boot ID without
  broadening signed event schemas: 21 passed (`native-boot-green.log`).

The logs above are all under `outputs/a13-macos-evidence/` and overlap; do not
sum them as a unique full-suite count. They do not establish real model load,
physical distinct-host qualification, two-Mac inference, or final-package readiness.
The prior packaged preview is stale relative to these source changes.

Open core work: fresh authenticated hello producer (incomplete first GLM draft,
not imported; narrower completion worker active); target native endpoint identity
binding; final owner-run orchestration/provision/load/challenge/qualification/
selection; loaded-worker lifecycle acceptance; exact physical/network approval
and two-Mac execution; final package and reviewed portable handoff. The source
regression for this checkpoint excludes test_package.py intentionally; packaging
is an explicit remaining gate, not a skip hidden inside completion claims.

GLM-5.3 workers own isolated snapshots. Parent alone integrates after verifying
termination, actual changes and tests. No commits/pushes/public exposure/physical
model work are authorized by this checkpoint. No protected checkout was edited.

## Active continuation — finish core Mycelium A13

Authority: updated `~/Desktop/ethonline-A13-execution-goal.md`, including its
**Latest owner correction — core versus hackathon** and C1–C8 gates.
A13 is core Mycelium work. Verifier endpoint/authentication/audit logic, policy,
consent and UI belong to the separate hackathon application built on top of core.
They are not A13 dependencies or completion gates. Preserve generic native
streaming and evidence interfaces; do not add a verifier plug-in framework.

The earlier preview below is retained as historical evidence. Verifier-specific
placeholders have now been removed from core source/UI and the locally tested
0.1.0-preview.2 successor app. The original delivered preview remains historical.
No protected hackathon workbench was modified. New managed-control/worker source
postdates preview.2 and is not qualified by that package.

**Current verdict: INCOMPLETE.** Continue in this same lane until the managed
core path is built, physically exercised and integration-ready. The main
bottleneck is provisioning/load/qualification/browser and ordinary two-Mac control,
not optional verifier availability. Component tests and disabled controls are not
completion; no new single-device path or broadened cross-platform campaign.

| Gate | Current status / missing evidence |
|---|---|
| C1 — Install and authority | Partial: real local authority and extracted-package owner/target tests on arm64 macOS 26.5.1; second-Mac package installation/start unverified. |
| C2 — Normal-user two-Mac pairing | Partial: real loopback HTTPS/SQLite with independent contexts; managed unrelated-network pairing/bootstrap and actual two-host exercise remain. |
| C3 — Download and provision | Partial: real browser-driven public Hugging Face acquisition completed (999,604,126 repository bytes; pinned revision and hashes); native source manifest compiled without tensor loading. UI consent/progress/cancel, capacity/integrity/resume negatives exercised. Installed second-Mac acquisition and managed stage provisioning remain. |
| C4 — Managed native serving | Partial: same canonical TLS listener now serves signed target-initiated control; a durable target pump executes a real native hello and signed ack, with uncertain ack replay fenced. Canonical managed-transport/producer integration, target staging, actual authorized load/challenge, distinct-host qualification and registry still remain. |
| C5 — Real user inference | Not exercised: fresh two-Mac native work, ordinary Mycelium browser stream, successful terminal job, request receipts and placement counters. |
| C6 — Enforced safety and recovery | Partial: real native no-model child is stopped on revoked membership, unsafe policy, measured RSS excess, disk growth and independent elapsed-time watchdog; process-group cleanup observed. Load/start requires an explicit grant callback. Loaded-worker qualification, durable aggregate run grants, complete storage/network accounting, stale-route/drain and UI recovery remain. |
| C7 — Core integration | Partial boundary: verifier-absent core UI tests pass; canonical A8 join/renewal and new control coexist on the same TLS listener. Maintained Mycelium browser/client/runtime consumer integration and native controller adaptation remain unfinished. |
| C8 — Final candidate and handoff | Preview only: final core artifact, supported-platform evidence, complete C1–C8 matrix, blocking-review closure and reviewed staged/portable integration handoff remain. |

Next implementation order: finish the mailbox/controller and producer/staging
connection in the managed application (not an SSH workaround); add explicit
reconciliation/cancellation UI for uncertain control receipts and durable aggregate
execution grants; finish supported public-origin bootstrap; obtain the exact
dependency-closed two-Mac execution/network grant; run real packaged two-host
inference, safety/recovery and maintained-client integration gates. Independent safe source work proceeds while genuine
external approvals are pending. No model/fleet/publication grant is implied.

Later representation reconciliation evidence:
`representation-parent-adjacent-green.log` records **87 passed, 3 skipped**
(staged deployments absent); only five outdated supervisor unit seams were updated
and two missing-authority entrypoint controls added. Production and delivered ZIP
were unchanged. This is local regression evidence, not physical serving.

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

## Initial integration handoff and review hold — historical checkpoint

Owner requested an integration context handoff and paused implementation. Read
`A13-MACOS-INTEGRATION-HANDOFF.md` for the complete source/package boundaries,
review-now versus hold guidance, consumer contract, and remaining C1–C8 path.

Since the acquisition checkpoint, signed owner result retrieval, durable owner
session intent, and explicit canonical `managed_https` peer/plan selection were
added. A real native hello was consumed through the canonical controller factory
and hello verifier; the single-host qualification gate still rejects. Latest
parent selection `managed-transport-green-02.log`: **146 passed** (managed
transport/session, physical-runner config, physical-controller tests). This is
not the full application suite or a physical serving result.

**Managed serving remains blocked for production integration.** The returned
`outputs/a13-macos-evidence/owner-control-review.md` matches the current reviewed
source hashes and identifies unresolved owner pre-enqueue crash recovery, target
lost poll/ack recovery, absolute-deadline handling, unbounded replay retention /
lifetime quota exhaustion, and concurrent/uncertain stop defects. Happy-path
GREEN and no-reexecution tests do not close these findings. No remediation was
performed after the review returned during handoff preparation.

`outputs/a13-macos-evidence/owner-session-integration-map.md` also identifies the
missing live inference/control command surface, concurrent target demultiplexing,
reserved cancellation lane, and real target-owned staging/profile producer.
Its earlier parser-discovery prose predates the new managed mode; use current
source and the handoff's reconciliation. Factory threading into the live
supervisor has intentionally not been performed before those contracts close.

Preview.2 remains older than the worker/control/session changes. The existing
quickstart describes historical preview.1, not the current core boundary or a
finished download/serving runbook. No model load, conversion, two-host execution,
public activation, stage, commit or push was performed in this continuation.

## Acquisition and native-control continuation receipts

All paths below are relative to `outputs/a13-macos-evidence/`. These are scoped
observations, not C1–C8 closure or physical two-Mac qualification.

- `public-source-acquisition.json` / `.log`: actual Chromium → private app →
  public pinned Hugging Face repository → digest verification → native immutable
  source-manifest compilation. **999,604,126** repository bytes, including
  **988,097,824** weight-file bytes; no conversion, tensor load or inference.
  Observed external power, nominal thermal state and unmetered network.
  `public-source-acquisition-desktop.png` preserves the real UI result.
- `browser-acquisition-green-02.log`: **16 passed**. Real small local HTTPS origin,
  app/browser acquisition, consent, state recovery and cancellation boundaries.
- `acquisition-parent-regression.log` / `.xml`: **310 passed, 3 skipped** against
  the earlier acquisition candidate, before the new managed-worker/control edits.
  Skips require staged physical deployments; this is not final-candidate proof.
- `package-acquisition-build.log` and `package-acquisition-green.log`: successor
  app in `dist/0.1.0-preview.2/` built and exercised, including its real bundled
  native resource helper. This app predates managed-control/worker source and is
  not the final requested delivery ZIP; a new package and archive replay remain.
- `worker-review-green.log`: **19 passed**. Actual native child hello/shutdown,
  measured-RSS refusal, retained over-limit storage, policy/membership changes,
  post-grant membership drift, and watchdog independence from a blocked observer.
  Policy fault injection is labelled; no model was configured or loaded.
- `managed-local-composition.log`: **39 passed** across the combined canonical
  TLS authority/control, real native target pump, worker guards, app acquisition
  and browser acquisition. Lost ack after actual success never re-executes the
  native command after pump restart. The production controller/registry/browser
  serving chain is still unfinished; all hosts in this test are the same Mac.

Private installation/pump state and credentials are not evidence exports. Keep
those directories private; do not include them in a source patch or release ZIP.
Only explicitly selected sanitized logs, receipts and screenshots may be handed
out. No model/fleet/public-exposure grant, staging, commit, push or publication
was performed by these continuation checks.

## Historical owner-contract preview — 2026-09-12

**MACOS A13 DEMO INCOMPLETE — LOCAL WORK REMAINS.** Serving role is
`not_yet_serving`. The versioned package below is an onboarding preview, not
completion of the judge-owned inference goal. Earlier checkpoints below retain
historical evidence, not current model selection or fresh serving qualification.

The owner-selected contract is now **Qwen/Qwen2.5-0.5B-Instruct**, revision
`7ae557604adf67be50417f59c2c2f167def9a775`, original BF16 Safetensors source,
MLX float32 arithmetic with **int8-weight-only**, **quantized_greedy_token_id**,
seed **0**. Launcher preflight/start, application status and the browser show
this contract. Native plan preparation rejects wrong/non-integer sampling seeds.
This supersedes Base/unquantized preparation, without adopting any W6 artifacts,
credentials, signatures or historical qualification as this demo's evidence.

Fresh canonical layer assignments can now be compiled for the owner and one
current enrolled Mac. The private proposal binds membership identities,
generations, leases, seed identity and the prepared manifest. It explicitly
remains `assignments_compiled_not_provisioned`, with `route_ready=false`.
Synthetic model metadata plus real local TLS/SQLite membership exercises this
compiler; it is not weight verification, resource admission or physical work.

Gateway v2 token events now retain real router `token_id` alongside unchanged
text; v1 wire shape remains unchanged. Different IDs decoding to identical text
cannot pass as the same replay. The exported patch also includes the strict
StreamEvent contract and regression tests, not only backend/service changes.
`outputs/a13-macos-evidence/gateway-v2-token-id.patch` passes read-only
`git apply --check` against wave8. Wave8 was not changed; application by its owner
and cross-tree byte equality are **not claimed**.

### Executed current evidence

- `contract-preview-local-gate.log`: **241 passed** — all A13 source tests except
  packaged tests, gateway suite, physical-runner config and durable-seed tests.
- `contract-preview-build.log`: self-contained arm64 app rebuild, exit 0.
- `contract-preview-extracted-package.log`: **2 passed**, owner/target browser
  lifecycle driven against the actual executable extracted from the delivery ZIP.
- Mobile screenshot: `contract-preview-browser/macos-a13-mobile.png`; identity
  wraps without horizontal overflow; unavailable features are visibly disabled.
- Tested package platform: Apple Silicon macOS **26.5.1 (25F80)** only. The
  admitted macOS 15.6.1 target remains untested; no continuous range is qualified.

### Versioned preview delivery

`dist/Mycelium-A13-0.1.0-preview.1-arm64.zip` — **41,546,242 bytes**.
SHA-256: `7dd2ca404592fa220105eedf74f11bba0d1d0000334c7d9755ff028f2fd34657`.
The archive includes the application, exact preflight artifact identity and
`QUICKSTART.md`; its byte integrity and extracted executable were exercised.
It is ad-hoc/unsigned development software, **not notarized**. Gatekeeper approval
was not exercised or bypassed. No public package hosting/publication occurred.

The quickstart is also in `A13-MACOS-QUICKSTART.md`. Model download/progress,
managed serving and optional verifier connection are **unimplemented and disabled**,
not simulated. Pinned upstream metadata gives 988,097,824 weight bytes and
999,604,126 full-repository bytes; no weights were downloaded. The proposed
verifier hostname failed DNS lookup from this machine; this does not establish a
global outage. Its authentication/audit protocol and configurable URL integration
remain unknown/unimplemented. No verifier data, payment or inference was sent.

Remaining critical paths: managed provisioning/load/challenge/qualification/
registry/browser inference, resource enforcement, download/progress/recovery,
normal-user cross-network pairing, verifier protocol integration and second-Mac
package/physical execution. No heavy execution or remote provisioning occurred.
No commits, push or staging occurred in this checkpoint.

## Frozen bounded acceptance (before implementation)

Authority: `~/Desktop/ethonline-A13-execution-goal.md`. This standalone lane is
`demo/a13-macos`, based on `b9001e6ac3fc11dd9a16f451426453621b24852a`.
The source checkout and all W6/application/A/B/C state remain protected.
The normative cross-platform A13 specification/inventory remain unchanged and unclaimed.

### Platform, installation and trust

Target: Apple Silicon arm64 macOS: **26.5.1 on m4pro and 15.6.1 on the separate
Evis-MacBook-Pro**. The preflight admits these two exact compatibility targets;
only the earlier 26.5.1 package was exercised. The 15.6.1 package remains unverified
until actual deployment/testing on that device. Other OS releases and Intel are
not qualified. Deliver an explicitly
unsigned development launcher/application with bundled Python/dependencies where
practical, normal Start/Stop controls and browser UI. No security prompt bypass,
notarization claim, hidden download, organizer credential or pre-enrolled identity.
All listeners are dynamically allocated loopback ports; installation state is
owner-private and separate from source. No fleet/resource ownership is inherited.

For the single-installation path, use canonical signed A8 join/member wire contracts
through locally pinned HTTPS; disclose this as **local TLS, not public Web-PKI or
unrelated-network A8 qualification**. No global A8 TLS policy weakening. External
Mac enrollment retains public HTTPS and explicit owner/target approval prerequisites;
a public listener/tunnel must never be launched implicitly.

### Acceptance table (evidence populated after execution)

| ID | Required observed outcome | Evidence initially |
|---|---|---|
| M1 | Artifact installs/starts without source edits, seed URLs, endpoint IDs or manual sidecars; deterministic unsupported-platform preflight | pending |
| M2 | Fresh owner/seed and target identities; target precedes mint; atomic join; durable identities and exact retry/restart | pending |
| M3 | Recipient-encrypted request/handoff; identity/nonce/seed/class/expiry/quota checks; reject tampering, replay and nonprivate recovery | pending |
| M4 | Explicit cooperative-peer visibility/resource consent; membership distinct from capability/assignment/load/qualification/serving | pending |
| M5 | Canonical compiler, authorized provisioner, loader/challenge, fresh physical qualification and registry selection | pending, exact model execution approval required |
| M6 | Judge-controlled Mac real assigned work through ordinary browser/client inference; visible output, terminal successful job, request-bound receipt and per-placement counters | pending, never substitute W6 or synthetic output |
| M7 | Bounded lease renewal/reconnect, restart, graceful drain/stop, revoke; no implicit identity/payment/evidence deletion | pending |
| M8 | Missing resources, stale qualification, interrupted acquisition/start and revoked/drained worker fail closed at production boundaries | pending |
| M9 | Actual UI refresh/reconnect, responsive/keyboard controls and clean-second-browser privacy isolation | pending |
| M10 | Final-byte focused/applicable regressions, built-artifact identity, explicit staged paths, cleanup/runbook | pending |

Owner correction after the initial freeze: **two separate physical Macs are the
required target**, using the existing distributed-serving and distinct-host
qualification contracts. No new single-device profile will be built, and multiple
processes on one Mac will never be represented as separate physical devices.
Intended role: `judge_mac_serving` in a real two-Mac topology. Current role remains
`not_yet_serving`. Exact device/resource, model-execution and network-exposure
approvals remain separate from this architecture decision.
No delegated inference is silently selected. Output assessment, payment, publication
and receipt integrity remain independent claims.

### Two-Mac local integration checkpoint (not physical serving evidence)

Read-only SSH verified that `m4pro` and `Evis-MacBook-Pro` are reachable, separate
named systems with 48 GiB and 16 GiB RAM respectively; the laptop reports macOS
15.6.1 and 7,841,476 KiB available disk. No remote provisioning, reservation,
model execution or public network exposure was performed by this check. Runtime
physical qualification remains required; SSH host labels alone do not qualify it.

The browser's explicit capability action now performs a tiny real MLX float32
arithmetic self-probe, signs the canonical capability report, sends it through
pinned HTTPS, verifies the seed's message-bound receipt, and retains private
report/receipt evidence. Empty/unavailable backends cannot be fabricated or
signed. The report does not establish model support, physical qualification or
serving. The UI invalidates it on expiry, membership change, revocation and
restart. This source checkpoint is exercised on m4pro only.

Evidence: `outputs/a13-macos-evidence/two-mac-local-gate-03.log` records **42 passed**
for `tests/a13_macos`, excluding the previously built package and the independently
in-progress `test_peer.py` / `test_representation.py` components. Actual browser
screenshots are under `outputs/a13-macos-evidence/two-mac-browser/`. Earlier failing
logs are retained. This is neither final-byte package evidence nor two-Mac model
execution evidence. No commits or staging have been performed at this checkpoint.

### Application-owned target pairing and package checkpoint

The application now selects **owner** or **target** explicitly. Target selection
is persisted before generating the target identity; restart reopens that role,
and missing/conflicting role state fails closed. A target never creates a seed or
SQLite membership database. The browser exposes public-request creation, verified
identity/class review, explicit expiry/quota approval, recipient-encrypted return,
and canonical HTTPS acceptance. Owner and target roles cannot silently replace
one another. Saved resource policies are displayed read-only after selection.

Local tests exercise two independent application instances and browser contexts
against a real installation-local TLS seed. This proves application/HTTP/TLS/
SQLite composition, not two physical hosts or public Web-PKI. The default remote
client still uses Web-PKI; only the tests inject the installation-local CA.

Observed evidence under `outputs/a13-macos-evidence/`:
- `target-combined-02.log`: **109 passed**, selecting all `tests/a13_macos`
  except `test_package.py`, plus `tests/physical_runner/test_config.py`.
- `browser-target-green-01.log`: owner/target Chromium pairing and existing
  lifecycle browser test passed; fresh final screenshots are in
  `target-browser-final/` from the later combined run.
- `package-build-target-01.log`: actual self-contained arm64 package rebuilt.
- `package-target-green-01.log`: built executable owner/target clean-state
  startup, browser interaction, stop/reconnect and retained private identity.
- `two-host-launch-red.log` / `two-host-launch-green-01.log`: canonical operator
  plan parsing, two distinct declared hosts, exact plan/host approval matching,
  and changed-plan refusal before runner invocation. Declared host IDs do not
  replace native physical qualification.
- `pairing-nonce-red.log`: reproduced generated nonce alphabet mismatch and
  invalid explicit nonce fallback; the final combined run verifies the fix.
- `browser-target-red.log` / `browser-target-red-02.log` were browser-context
  fixture failures: localhost cookies are not port-isolated. The corrected
  separate-context `browser-target-red-03.log` demonstrates the missing target
  action. `package-target-stale-red.log` demonstrates the old package lacked it.

Current package: `dist/Mycelium A13 Demo.app`, launched by
`dist/Open Mycelium.command`. It is ad-hoc/unsigned development software, **not
notarized**. Only arm64 macOS 26.5.1 was exercised; 15.6.1 remains unverified.
`dist/artifact-identity.json` identifies the rebuilt executable; the checkpoint
manifest separately binds bundle files and current source bytes.

Acceptance remains partial: M1/M2/M3/M4/M7/M9 have the local evidence above;
M5/M6 (native model-stage work, qualification, selection, browser inference,
terminal receipt and per-placement counters) remain unexecuted and incompletely
wired. M8's real loaded-worker/download/drain negatives and M10's final closure
are not claimed. This is **not_yet_serving**, not an externally-blocked-only build.

Next acceptance-critical local work:
1. Generate a fresh complete two-host plan using enrolled identities. Existing
   judge-owned seed binding is implemented in the source checkpoint below; the
   plan producer and managed browser invocation remain unfinished.
2. Connect managed assignment/artifact/load/challenge/qualification/selection and
   browser request/SSE/terminal evidence, including actual resource-limit and
   owned-process cancellation enforcement. The present wrapper checks approval
   values but does not itself enforce the declared aggregate run/resource limits.
3. Resolve the ordinary remote launcher/control path: current native runner plans
   require a local entry and SSH remote peer; this is not yet the no-operator-
   knowledge product experience. Public HTTPS enrollment needs separate approved
   publication; the owner seed currently listens on loopback only.
4. Finish local wiring before requesting the exact physical model/host/network
   grant. Rebuild/requalify final bytes afterward. No staging, commits, pushes,
   physical execution, remote provisioning, or public exposure in this checkpoint.

### Existing-seed source checkpoint

`prepare_registry_launch` now requires an existing canonical seed root rather than
creating an empty one. It uses the native load-only SQLite/identity validator,
checks the plan's swarm and seed-key binding, and rechecks identity and authority
generation before calling the runner. `DemoAuthority.prepare_serving` supplies
its own installation seed path and refuses stopped, revoked or stale membership.
Preparation does not enable contribution, qualify a route, or execute a model.
The HTTP/browser managed serving caller still needs implementation.

Evidence in `outputs/a13-macos-evidence/`:
- `existing-seed-red.log`: missing seed argument and absent rejection reproduced.
- `existing-seed-green-01.log`: 12 focused tests passed before owner integration.
- `owner-serving-preparation-red.log`: missing owner preparation method reproduced.
- `existing-seed-local-gate-01.log`: **119 passed** for `tests/a13_macos`
  excluding `test_package.py`, plus `tests/physical_runner/test_config.py` and
  `tests/live/test_durable_seed_identity.py`.

These checks use real disposable TLS-enrolled owners and native read-only seed
loading, including unchanged seed-file bytes, wrong swarm/key rejection, missing
seed rejection without creating state, and lost-key rejection before runner
invocation. Plan graph/model contents remain fixtures and the successful runner
call is intercepted: this is not model, assignment or physical-serving evidence.
The previously tested package has **not** been rebuilt for this source change.
Its earlier package receipt is historical, not current-source package coverage.

### Resource/external gates

Observed host: m4pro, arm64, macOS 26.5.1 build 25F80, 48 GiB RAM; only ~18 GiB
free disk at initial inspection. Source/local tests and small dependency setup are
authorized; no heavy model load/conversion, private model reuse, >1 GB download,
physical fleet reservation or external compute is authorized by this freeze.
Investigate the already-supported Qwen2.5-0.5B path and publish an exact source,
revision/representation, host, run-count, memory/disk/network/cost/time and evidence
proposal before executing it. Do not change model fidelity to evade the gate.
Missing signing/public exposure is disclosed, not bypassed. Public distribution,
accounts, DNS, paid inference, ENS and testnet use require fresh approval.

### Implementation boundaries

Use canonical seed coordinator/SQLite APIs, signatures and membership protocols;
never edit a seed database or set route readiness from setup intent. Recompute the
whole topology/artifact/qualification binding chain. Keep plaintext invitations out
of browser/URLs/argv/clipboard/logs/evidence. Use recipient-bound encrypted private
recovery files. Identity loss fails closed instead of silently regenerating.

Start is explicit; contribution is off by default. Stop retains identity and evidence.
Removal must be a distinct confirmed owner action and must not claim secure erasure
or offline coordinator revocation. The runbook/evidence will identify all unbuilt and
unverified acceptance rows separately from actual external approvals.

No commits, pushes, PRs or release actions. Stage reviewed explicit paths only.
