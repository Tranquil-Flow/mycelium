# Mycelium A9 Platform-Neutral Capability and Accelerated Runtime Efficiency Specification

**Status:** `design_only`; dependency-ready contract and migration boundary
**Gate:** A9
**Parent:** `2026-08-11-mycelium-completion-plan.md`
**Depends on:** A8 internet-native control and activation physically closed
**Architecture:** Synthesized architecture sections 4.4, 4.5, 4.12, 4.14, and 4.15

## 1. Outcome and claim boundary

A9 replaces brand-shaped peer classes with one signed, versioned membership and
capability model. Identity kind, operating-system family, activation transport,
runtime backend, and observed resource/lifecycle capabilities become independent
facts. Eligibility is then derived from current signed evidence and an exact
class-specific qualification; it is never embedded in a device name or granted by
membership.

A9 also closes the performance prerequisite for A10. The ordinary product path gains
one physically qualified native accelerated Windows x86_64 runtime, efficient int8
weight execution, a parity-qualified reduced-precision activation path, component-aware
compute/serialization/transport observations, and a frozen single-request performance
floor. A fast backend is an executed capability claim, not an operating-system inference.

A9 does not qualify Android, iOS, iPadOS, a browser, or an unknown future platform for
model execution. It does not add a native mobile runtime, installer, public invite UX,
artifact permission, general placement authority, deployment registration, or model
selection. It does physically qualify one exact Windows runtime/stage class and the
component-cost inputs later consumed by the independent planner and deployment qualifier.
A12 owns mobile activation qualification and A13 owns ordinary installation and
invitation UX.

The current `pixel_http`, `android_termux_iroh`, `pixel-stdlib`, and similar compound
labels are migration inputs, not product architecture. Pixel remains one Android test
device. No vendor, product name, operating-system release, hostname, username, or
network location becomes a protocol or backend class.

## 2. Authority separation

The A9 model has seven independent authorities:

1. **Durable membership authority** owns node identity, public verification key,
   incarnation, membership generation, lease, invitation scope, and revocation.
2. **Peer-profile authority** is the member's signed declaration of identity kind,
   platform family, architecture, software build, installed transports, and installed
   runtime backends. A declaration is not proof that any capability works.
3. **Evidence authority** owns fresh observed memory, storage, lifecycle, power,
   thermal, network-loss, context, concurrency, and runtime-operation results.
4. **Class qualifier** decides which roles one exact platform/runtime/transport/build
   tuple has physically earned. It binds executed positive and negative evidence.
5. **Runtime-efficiency qualifier** owns exact provider/kernel/precision identity,
   persistent weight-materialization behavior, component and edge timings, activation
   codec parity, thermal stability, and the single-request performance decision. It
   cannot issue placement or deployment authority.
6. **Planner and Provisioner** independently decide placement and issue assignment-local
   artifact authority. Eligibility supplies candidates; it never selects them.
7. **Deployment qualifier and registry** remain the sole route-readiness and selection
   authorities. Peer qualification cannot qualify a deployment.

The Observatory and browser receive only privacy-reduced projections. Product actions
remain on explicitly authorized Device Lab, inference, preparation, activation, and
selection paths; the Observatory cannot mutate membership or eligibility.

## 3. Closed platform-neutral model

### 3.1 Identity and platform

`identity_kind` is one of:

- `native_peer` — an installed native member capable of authenticated product
  transports;
- `browser_peer` — a browser execution/probe member with browser lifecycle semantics;
- `artifact_source` — a member that may serve explicitly granted immutable chunks but
  cannot execute a model stage; or
- `probe_only` — an evidence contributor with no activation or artifact role.

`platform_family` is one of `macos`, `linux`, `windows`, `android`, `ios`, `ipados`,
`browser`, or `unknown`. An unrecognized future platform is represented as
`platform_family=unknown` with one optional bounded, privacy-reviewed
`platform_variant`; it remains ineligible. An unrecognized enum value or extra field is
rejected rather than treated as `unknown`.

`architecture` is a bounded registered product value such as `arm64`, `x86_64`,
`wasm32`, or `unknown`. Platform and architecture describe the execution environment;
they do not establish a runtime or role.

### 3.2 Transport and runtime

Transport declarations are a bounded set of independently versioned capabilities. A
transport record binds:

- transport family (`iroh`, `https`, `browser_https`, or `unknown`);
- implementation/build digest and supported wire-protocol versions;
- authenticated local EndpointID digest where applicable;
- supported connection roles, direct/relay observation support, and persistent reuse;
- lifecycle availability (`foreground_only`, `foreground_and_background`,
  `service_managed`, or `unknown`); and
- declared maximum frame and concurrency bounds.

Runtime declarations are likewise independent. A runtime record binds a stable,
vendor-neutral backend identifier, build digest, model-family/adaptor identifiers,
representation and precision support, decode operations, context and concurrency
bounds, KV schema identities, artifact formats, and cancellation/cleanup support.
Unknown runtime or transport identifiers remain visible but ineligible until their
contracts and qualification class are registered.

`mlx`, `numpy`, a native accelerated Windows provider, and future native Android or
Apple-mobile providers are runtime identities. A provider record is vendor-neutral but
exact: it binds implementation family, execution provider, build digest, device class,
driver/runtime identity, supported kernels and precisions, thread/queue limits, and the
qualification evidence that makes each operation eligible. `numpy` remains a portable
correctness/reference identity and cannot satisfy the accelerated Windows or
performance-ready class without separate executed acceleration evidence. `pixel-stdlib`
is not retained as a backend identity. Termux describes one development host
environment, not a runtime class.

### 3.3 Observed capability profile

The capability profile is signed by the member and reconciled with current product
observations. It contains only bounded, nullable fields:

- sustainable and currently available memory, assignment-local storage, runtime
  workspace and KV headroom;
- supported context, active-request, batch, prefill, decode, multi-position verify,
  and speculative-draft limits;
- thermal state, sustained thermal class, thermal observation duration, and throttle
  behavior;
- power source, battery class/level where user consent and platform APIs permit it,
  low-power state, minimum contribution policy, and drain deadline;
- lifecycle state, background execution guarantee, suspend/termination behavior,
  restart/reconnect semantics, and maximum background grace;
- activation protocol and transport observations, network-loss/reconnect behavior,
  link-measurement availability, and evidence freshness; and
- evidence source kind (`native_observed`, `synthetic_conformance`, `operator_declared`,
  or `unknown`) for every capability family.

Missing or unavailable observations are `null`/`unknown`, never zero, healthy,
supported, direct, background-capable, or eligible. Native observations and synthetic
conformance results are retained separately; one cannot satisfy the other's gate.

### 3.4 Accelerated runtime and activation-efficiency model

The Windows positive gate uses one native x86_64 member and the exact local
`Qwen/Qwen2.5-0.5B-Instruct` revision
`7ae557604adf67be50417f59c2c2f167def9a775` and int8-weight-only representation already
bound by the qualified route. The selected implementation may use DirectML, ONNX Runtime,
oneDNN, or another native provider, but A9 freezes the provider/build only after the
following physical evidence passes. Installing a library or naming an accelerator is
not qualification.

An accelerated backend must expose bounded operation evidence for embedding, decoder,
final normalization, vocabulary head, KV update, and token selection. For int8 weights,
load time may create persistent packed, transposed, or provider-owned kernel state.
Decode-time execution must prove that whole matrices are not recast or expanded for
each token. Materialization counters and bytes are monotonic and component-scoped;
after warmup, a decode window must show zero full-weight materializations and zero
unbounded packed-state growth. A backend that performs `int8 -> float32` whole-weight
conversion inside each linear operation remains correctness-only.

Every stage observation separates:

- embedding, decoder-range, final-normalization, vocabulary-head, and token-selection
  compute time for prefill and decode;
- quantization, packing, materialization, and device-transfer time and bytes;
- activation encode/decode, copy, queue, authenticated transport, and result-publication
  time and bytes; and
- warmup, steady-state, thermal/power state, concurrency, context/output bucket,
  uncertainty, and sample count.

The planner consumes confidence-bounded component service rates and directed edge costs.
The vocabulary head is an explicit component cost; it is never approximated as one
ordinary decoder layer. Predictions report compute, conversion, serialization,
transport, and queue contributions separately, and the physical gate records prediction
error. Unknown or stale component cost makes the affected placement performance-
ineligible rather than zero-cost.

An edge may use float16 or bfloat16 activations only after exact shape/dtype/finite-value,
output-token parity, cancellation, replay, corruption, and byte-reduction gates pass for
the exact adjacent runtime builds. Negotiation is generation-bound and fail-closed.
Unsupported or withdrawn codecs use an independently qualified dtype; they are never
silently coerced. The product exposes the active dtype and privacy-reduced byte/timing
totals without exposing activations.

Before candidate measurements, a benchmark manifest freezes hardware, power/thermal
envelope, model/revision/representation, route and generations, reference and candidate
runtime builds, activation dtype, prompt/output buckets, arrival schedule, warmup,
repetitions, token limits, exclusions, and software digests. It contains at least three
paired alternating reference/candidate windows, 24 completed requests per mode, two
prompt buckets, two output buckets, and at least 512 generated tokens per mode. Failed,
cancelled, or timed-out requests remain in reliability accounting.

The accelerated candidate passes only when all completed outputs match the reference,
stage-local KV and exact cleanup remain proven, median decode rate is at least 2x the
reference with a paired 95% confidence lower bound above 1.5x, median decode reaches at
least 8 tokens/s, interactive p95 TTFT is at most 1,500 ms, and reliability/cleanup do
not regress. A miss is published honestly, keeps the candidate visible but performance-
ineligible, and blocks A10; thresholds are not weakened after measurement.

## 4. Versioned contracts

A9 introduces these capability-named contracts:

1. `mycelium.membership.peer_profile.v2` — the closed identity, platform, transport,
   runtime, build, incarnation, generation, and lease-bound declaration.
2. `mycelium.membership.capability_report.v2` — fresh signed observations and explicit
   source kind, bound to the exact profile digest and evidence generation.
3. `mycelium.peer_class_qualification.v1` — the class qualifier's role-by-role decision,
   evidence digests, validity interval, constraints, and negative-gate results.
4. `mycelium.peer_eligibility.v1` — privacy-reduced computed membership, evidence, and
   qualification ladder with blockers and no placement authority.
5. `mycelium.membership_contract_migration.v1` — owner-private executed migration
   record, with a privacy-reduced status projection.
6. `mycelium.runtime_acceleration_profile.v1` — exact backend/provider/build/device,
   supported operations, kernel/precision state, materialization policy, lifecycle,
   cancellation, and cleanup capabilities.
7. `mycelium.stage_performance_observation.v1` — signed component- and phase-scoped
   compute, conversion, serialization, queue, transport, publication, thermal, sample,
   and uncertainty evidence.
8. `mycelium.activation_codec_qualification.v1` — exact adjacent runtime/edge identity,
   dtype, shapes, parity, finite-value, byte reduction, cancellation/replay negatives,
   validity, and withdrawal state.
9. `mycelium.runtime_efficiency_qualification.v1` — reference/candidate benchmark
   binding, no-rematerialization proof, component-aware planner result, TTFT/TPOT/rate,
   confidence, reliability, cleanup, and performance-eligibility decision.

Every record is canonical, signed where authoritative, size/count bounded, and rejects
unknown fields, invalid nullability, duplicate identifiers, non-finite numbers, stale
incarnations/generations, unsupported protocol versions, or mismatched digests. The
closed contracts never contain invite secrets, signing keys, raw EndpointIDs, private
addresses, usernames, hostnames, private paths, prompts, outputs, token IDs, tensors,
activations, KV data, or unrestricted operating-system diagnostics.

The profile digest is included in capability evidence, class and runtime-efficiency
qualification, assignment eligibility, and any later load or deployment proof. A profile,
provider, driver/runtime, kernel/precision, activation codec, materialization policy, or
component-cost change does not rewrite past evidence: it increments the evidence
generation and makes dependent eligibility and A10 readiness stale until re-evaluated.

## 5. Eligibility ladder and fail-closed derivation

Each role is decided independently:

1. `signed_member` requires a current non-revoked membership generation and lease.
2. `probe_contributor` requires an approved identity kind, consent, a current capability
   profile, and the exact probe's integrity/freshness gate.
3. `artifact_source` requires a registered transport, source qualification, storage and
   thermal/power policy, plus a separate assignment-scoped A2 grant.
4. `speculative_draft_worker` requires a compatible registered runtime and exact draft
   class qualification; it remains ineligible until A11 is physically closed and bound
   to the exact target workload. This optional restriction does not block ordinary
   probe, artifact, stage, or generic A12 mobile participation.
5. `qualified_model_stage` requires exact backend/build, representation, operation,
   lifecycle, resource, thermal/power, network-loss, cancellation, cleanup, and physical
   class qualification. Planner placement and deployment qualification remain later
   independent gates.
6. `performance_ready_model_stage` additionally requires a current
   `mycelium.runtime_efficiency_qualification.v1` for the exact provider/device/build,
   model representation, component roles, activation codec, workload envelope, and
   thermal state. On Windows, a portable NumPy-only or per-decode whole-weight-
   materializing runtime cannot earn this role. A10 admits only complete tracks whose
   participating placements and edges hold the exact current performance-ready and
   activation-codec evidence required by its benchmark manifest.

An eligibility result records `eligible`, `ineligible`, or `unknown`, its current
blockers, authority and evidence generations, qualification ID/digest, constraints, and
expiry. It never contains a route-ready boolean authored by the member. Membership,
platform recognition, installed code, a successful synthetic fixture, or an accepted
Router frame alone cannot advance a role.

Unknown platform, architecture, runtime, transport, lifecycle semantics, source kind,
qualification class, or required measurement is `ineligible` for activation. The
system does not guess from operating-system strings, device brands, participation,
network location, or previous peer labels.

Correctness eligibility and performance readiness remain distinct. A physically correct
reference backend stays available for parity, diagnosis, or explicit owner-authorized
fallback, but it cannot be presented as accelerated capacity or satisfy the A10
prerequisite. A failed performance gate never revokes unrelated membership, probe, or
artifact roles.

## 6. One-time migration from membership v1

Migration is a coordinated product operation, not perpetual compatibility behavior.
Before production edits, enumerate and update every producer and consumer: seed join,
resume and message validation; node agent and durable state; external-participant
policy; planner evidence adapter; artifact source/admission; activation admission;
qualifier; registry; product gateway; all eight UI workspaces; fixtures, generators,
contract manifest, audits, and runbooks.

The migration sequence is:

1. Freeze the contract/source digests and full producer/consumer inventory. Preserve a
   recoverable qualified incumbent, but drain shared membership writes for the bounded
   cutover.
2. Convert no member by string substitution. Each current member signs a v2 peer profile
   with its existing durable node key and a new incarnation. The seed verifies the old
   current generation, profile, key continuity, revocation state, and software build,
   then atomically commits generation `n+1`.
3. Preserve node ID and verification key only when cryptographic continuity succeeds.
   An absent, stale, revoked, changed-key, or ambiguous legacy identity must re-enroll
   through a fresh invite. No operator-authored identity mapping is accepted.
4. Legacy `mac_mlx_iroh` and `linux_numpy_iroh` members become separate native platform,
   Iroh transport, and runtime declarations. Legacy Pixel/Android/browser labels become
   declared but ineligible profiles. Their historical evidence remains labelled with
   its original contract and cannot qualify v2.
5. Do not copy legacy activation eligibility. Re-evaluate existing macOS/Linux class
   qualifications against the exact v2 profile and evidence bindings. Until accepted,
   the member remains visible and ineligible; the existing incumbent is not silently
   rebound or displaced.
6. During one bounded migration window, v1 inputs may be read only to authenticate the
   transition. They cannot renew a v2 lease, change capability, receive a new
   assignment, or satisfy readiness. After the recorded cutover, every v1 write and
   stale v1 process fails with `membership_contract_upgrade_required`.
7. Reconcile the complete member inventory, prove every producer and consumer uses v2,
   rotate the public product generation, and remove the migration-only reader before A9
   closure. There is no indefinite dual-write or compound-class compatibility layer.

Restart at any point resumes from a private, canonical, fsynced checkpoint. A partial
member transition is never visible. A failed migration preserves the prior durable
membership database and incumbent deployment, records a bounded recovery action, and
does not broaden eligibility.

## 7. Product and eight-workspace behavior

Product copy uses human role and capability names, never A9/M21 labels or compound
internal class strings.

- **Inference:** identifies the selected deployment and participating peers' qualified
  roles, backend/provider, activation precision, TTFT, TPOT, and tokens/second; a member,
  installed runtime, or correctness-only backend is never presented as accelerated or
  selectable performance capacity.
- **Device Lab:** presents the ladder from signed member through probe, artifact, draft,
  stage, and performance-ready eligibility; each rung shows observed/synthetic source,
  provider/kernel/precision evidence, constraints, blockers, consent, freshness, and
  qualification action.
- **Network:** shows only transport capabilities and current bound direct/relay/unknown
  observations plus privacy-reduced activation dtype/bytes and compute/serialization/
  transport timing; platform does not imply connectivity.
- **Nodes:** separately shows platform family, architecture, identity kind, transports,
  runtimes/providers, signed membership/lease, observed capabilities, component service
  rates, materialization state, qualification, and current eligibility.
- **Plans:** displays which exact profile/evidence/qualification generations were used,
  component-aware predicted versus observed costs including the vocabulary head and edge
  overhead, and why an otherwise visible member was excluded. Placement remains planner
  intent.
- **Readiness:** separates member, profile, evidence, class qualification, artifact,
  load, accelerated Windows, int8 rematerialization, activation codec, component timing,
  single-request performance, deployment qualification, registration, and selection
  proofs. A10 remains blocked until every A9 performance prerequisite is current.
- **Incidents:** records migration, stale profile, changed build, unsupported capability,
  provider fallback, rematerialization, precision withdrawal, prediction drift,
  performance-floor failure, lease/revocation, lifecycle drain, and eligibility
  withdrawal without private device details.
- **Settings:** shows contract versions, supported platform/runtime registries,
  qualified precision/performance policy, contribution policy, privacy/retention,
  migration status, revocation, and advanced redacted diagnostics. Unqualified settings
  are disabled with their reason.

Refresh, direct navigation, Back/Forward, workspace switching, reconnect, stale/degraded
sources, terminal migration history, and a clean second session reconstruct the same
authority generation. Browser-local state cannot grant or preserve eligibility.

## 8. Frozen acceptance decisions and inventory

These decisions are implementation-binding while remaining `design_only`. Changing one
requires an A9 specification revision and acceptance-inventory update before membership
schema or shared product integration begins.

| Decision ID | Frozen selection | Rejected shortcut |
| --- | --- | --- |
| `separated_capability_authority` | Identity kind, platform family, architecture, installed transports, installed runtimes, observed capability families, class qualification, eligibility, placement, and deployment readiness remain separately authored and generation-bound. A declaration can name installed support but cannot prove operation or advance another authority. | Compound device classes, brand-derived backends, participation-derived capability, or one field transitively granting eligibility. |
| `signed_capability_binding` | The durable member key signs the v2 profile and each capability report. Reports bind membership generation, incarnation, profile digest, evidence generation, issue/expiry, source kind per capability family, and exact bounded observations before class evaluation. | Unsigned claims, seed/operator-authored member capability, stale profile reuse, mixed generations, or an unbound declaration treated as evidence. |
| `unknown_remains_ineligible` | Registered `unknown` sentinels and nullable missing observations remain visible with explicit blockers but are ineligible for activation. Unrecognized enum values, fields, protocols, or classes are rejected rather than converted to `unknown`. | Default-zero/healthy/supported coercion, guessing from platform or brand text, prior-label fallback, or unknown satisfying a required objective. |
| `native_synthetic_evidence_separation` | Every capability family carries its own source kind. Native-observed, synthetic-conformance, operator-declared, and unknown evidence remain distinct across storage, qualification, projection, and history; only the exact required source kind can close a class gate. | Synthetic-as-native promotion, source-kind inheritance across families, relabelling historical fixtures, or UI copy hiding the source. |
| `identity_preserving_v2_migration` | Migration preserves node ID and verification key only after cryptographic continuity, creates a new incarnation and monotonic generation, retains legacy history under its original contract, and re-evaluates every role without eligibility carry-over. V1 becomes a bounded read-only transition input and is removed after cutover. | String substitution, operator identity mapping, dual-write, indefinite compatibility, changed-key continuity, incumbent rebinding, or copied eligibility. |
| `class_specific_qualification` | Qualification binds the exact platform/runtime/transport/build tuple, role, profile/evidence generations, physical positive and negative evidence, constraints, and expiry. Each role is independent; planner, artifact grant, deployment qualifier, registry, and selection remain later authorities. | Platform-wide qualification, one role granting another, membership-only placement, artifact access from eligibility alone, or peer qualification setting route readiness. |
| `accelerated_windows_backend` | A9 closes only with one physically qualified native accelerated Windows x86_64 provider on the ordinary product route. Provider, device, build, driver/runtime, model adapter, operation, precision, lifecycle, cancellation, cleanup, and thermal evidence are exact and current. | Treating Windows, an installed package, NumPy correctness, a synthetic kernel probe, or a provider name as acceleration evidence. |
| `persistent_int8_execution` | A performance-ready int8 backend may create bounded persistent packed state at load/warmup, then proves zero decode-time full-weight materializations and bounded stable workspace throughout the benchmark. | Per-linear or per-token whole-weight `int8 -> float32` conversion, hidden repacking, unbounded cache growth, or storage quantization presented as compute acceleration. |
| `qualified_activation_precision` | Float16/bfloat16 activation transport is exact-edge/build/dtype qualified with token parity, shape/finite-value validation, byte reduction, replay/cancellation/corruption negatives, generation fencing, and explicit fallback to another independently qualified codec. | Silent dtype coercion, precision inferred from weights, unqualified compression, stale codec reuse, or byte savings asserted without physical counters. |
| `component_aware_performance_authority` | Signed observations separate embedding, decoder range, final norm, vocabulary head, token selection, conversion, serialization, queue, transport, and publication costs by phase. The planner consumes confidence-bounded component and edge costs, and the frozen physical benchmark enforces the single-request floor before A10. | Equal-layer cost assumptions, hiding the vocabulary head inside one layer, modeled timing as execution evidence, aggregate throughput standing in for TPOT, or post-measurement threshold changes. |
| `dynamic_ui_projection` | All eight workspaces consume the same live authority generation and registry-driven platform/transport/runtime/capability values. They expose source kind, freshness, blockers, constraints, migration state, and withdrawals without hard-coded device inventories or compound legacy labels. | Browser-local eligibility, brand-specific branches, static supported-device lists, sealed history as current, hidden unknowns, or stale values surviving reconnect. |

`tests/a9_acceptance/inventory.v1.json` is the closed machine-readable acceptance
inventory for these decisions, their fail-closed cases, the accelerated Windows/runtime-
efficiency benchmark boundary, and all-eight-workspace dynamic projection requirements.
Its protocol is `mycelium.a9_acceptance_inventory.v1`; its claim boundary is frozen
design and future acceptance input only. It contains no schema, implementation,
migration or benchmark execution, device observation, evidence, readiness, or completion
claim. Repository-local tests enforce its exact decision/case/workspace sets, bounded
shape, privacy vocabulary, specification binding, and `design_only` state.

## 9. Deterministic and adversarial verification

Contract tests cover canonical encoding, size/count/time bounds, duplicate entries,
unknown fields/enums, `unknown` platform representation, nullability, source-kind
separation, signature/key/profile/generation mismatches, stale evidence, changed build,
unsupported runtime/transport, role independence, and privacy scanning.

Migration tests cover every legacy class, existing-key continuity, `n+1` generation,
restart at each checkpoint, exact retry, changed retry, duplicate identity, concurrent
resume, revoked/stale members, changed keys, unmapped values, rollback before commit,
v1 write rejection after cutover, and removal of the migration reader. A generated
producer/consumer inventory test fails if any membership, eligibility, planner,
artifact, activation, qualifier, registry, gateway, fixture, or UI consumer still reads
compound legacy classes as authority.

Eligibility tests prove:

- a current member with no evidence is ineligible;
- synthetic conformance cannot satisfy a native-observed gate;
- class qualification for one role cannot grant another role;
- platform, transport, runtime, and capability changes withdraw dependent eligibility;
- missing thermal, power, lifecycle, memory, or network-loss evidence remains unknown;
- artifact eligibility cannot grant chunks without an A2 assignment grant;
- stage eligibility cannot grant placement, route readiness, registration, or selection;
  and
- unknown future platforms remain visible and safely ineligible.

Runtime-efficiency tests prove:

- provider/build/device/driver and precision changes withdraw performance readiness;
- NumPy correctness, package presence, synthetic probes, and platform family cannot
  mint accelerated Windows eligibility;
- every warm decode operation leaves full-weight materialization counters unchanged;
- bounded packed state is created only at declared load/warmup boundaries and cannot
  grow with output tokens;
- float16/bfloat16 activation negotiation rejects unsupported, stale, corrupted,
  non-finite, wrong-shape, wrong-dtype, replayed, or generation-mismatched frames;
- reference and reduced-precision paths produce exact committed token parity and exact
  request-scoped cleanup;
- component timing totals reconcile within declared uncertainty with end-to-end TTFT and
  TPOT, while compute, conversion, serialization, queue, transport, and publication
  remain separately attributable;
- the planner assigns an explicit vocabulary-head cost, rejects unknown component costs,
  and reports prediction error without rewriting evidence; and
- aggregate throughput, a short output, thermal drift, excluded failures, or a post-hoc
  threshold cannot satisfy the single-request performance gate.

## 10. Physical and browser gates

### Physical positive

On the ordinary A8 product path, migrate current macOS, Linux, and Windows members using
their existing durable identities. Prove node ID/key continuity, new incarnation and
monotonic generation, current v2 profiles, fresh native observations, and explicit class
re-evaluation. The incumbent remains recoverable and no member gains a role it did not
already physically qualify. Enroll or migrate one recognized but unqualified mobile or
browser member and show it at the exact lower rung it earned.

On the exact Qwen2.5-0.5B int8-weight-only two-host M4 Pro to Windows route, execute the
frozen reference/candidate corpus through the product browser. Prove the exact accelerated
provider/build, zero warm decode-time full-weight materialization, bounded workspace,
stage-local KV, qualified reduced-precision activation bytes, component and edge timing,
component-aware planner choice, token parity, performance thresholds, request cleanup,
thermal stability, and fresh evidence. The UI reconstructs the same result after refresh,
navigation, reconnect, and a clean second session.

### Physical negative

Attempt stale-v1 renewal after cutover, changed-key migration, revoked-member migration,
unknown platform/runtime, unqualified capability claims, synthetic-as-native evidence,
expired profile evidence, and stage selection of a membership-only peer. Also attempt to
qualify Windows from NumPy/package presence, a changed provider/driver, per-token full-
weight conversion, unqualified or corrupted float16 activation, missing vocabulary-head
timing, modeled-only component costs, thermally drifted samples, and a result below the
frozen performance floor. Each fails closed without route mutation, artifact disclosure,
incumbent displacement, A10 readiness, or a fabricated peer-failure incident.

### Browser

All eight live workspaces expose the same v2 and runtime-efficiency authority and human-
readable separation.
Verify direct navigation, refresh, Back/Forward, reconnect, stale/degraded evidence,
migration terminal history, privacy redaction, keyboard and responsive operation,
reduced motion, and a clean second session. Injected compound legacy classes,
unknown fields, raw EndpointIDs, host details, or synthetic qualification must be
rejected rather than rendered as current truth.

## 11. Completion

The executed `mycelium.membership_contract_migration.v1` artifact binds source and
contract digests, every producer/consumer version, pre/post member generations, identity
continuity proofs, profile and qualification digests, physical positive and negative
results, browser checks, and regression/audit outputs. Its public projection is
privacy-reduced and cannot replay membership credentials.

A9 closes only after A8 is physically closed, the one-time v2 migration is complete,
the migration-only reader is removed, macOS/Linux/Windows continuity and unearned-
eligibility negatives pass physically, the accelerated Windows provider and exact
Qwen2.5-0.5B route pass the no-rematerialization, activation-codec, component-cost,
planner, single-request performance, cleanup, browser, and negative gates, all eight
workspaces pass live verification, full contracts, privacy, governance, security,
Python and frontend suites are green, and one atomic A9 feature commit exists. A10 has
direct dependencies on both A4 and this atomic A9 close. Until then this document remains
`design_only`.
