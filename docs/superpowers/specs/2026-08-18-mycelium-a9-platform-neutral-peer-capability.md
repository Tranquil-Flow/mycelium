# Mycelium A9 Platform-Neutral Peer and Capability Specification

**Status:** `design_only`; dependency-ready contract and migration boundary
**Gate:** A9
**Parent:** `2026-08-11-mycelium-astra-completion-plan.md`
**Depends on:** A8 internet-native control and activation physically closed
**Architecture:** Astra sections 4.5, 4.12, 4.14, and 4.15

## 1. Outcome and claim boundary

A9 replaces brand-shaped peer classes with one signed, versioned membership and
capability model. Identity kind, operating-system family, activation transport,
runtime backend, and observed resource/lifecycle capabilities become independent
facts. Eligibility is then derived from current signed evidence and an exact
class-specific qualification; it is never embedded in a device name or granted by
membership.

A9 does not qualify Android, iOS, iPadOS, a browser, or an unknown future platform for
model execution. It does not add a native mobile runtime, installer, public invite UX,
artifact permission, placement, route activation, or model selection. A12 owns mobile
activation qualification and A13 owns ordinary installation and invitation UX.

The current `pixel_http`, `android_termux_iroh`, `pixel-stdlib`, and similar compound
labels are migration inputs, not product architecture. Pixel remains one Android test
device. No vendor, product name, operating-system release, hostname, username, or
network location becomes a protocol or backend class.

## 2. Authority separation

The A9 model has six independent authorities:

1. **Durable membership authority** owns node identity, public verification key,
   incarnation, membership generation, lease, invitation scope, and revocation.
2. **Peer-profile authority** is the member's signed declaration of identity kind,
   platform family, architecture, software build, installed transports, and installed
   runtime backends. A declaration is not proof that any capability works.
3. **Evidence authority** owns fresh observed memory, storage, lifecycle, power,
   thermal, network-loss, context, concurrency, and runtime-operation results.
4. **Class qualifier** decides which roles one exact platform/runtime/transport/build
   tuple has physically earned. It binds executed positive and negative evidence.
5. **Planner and Provisioner** independently decide placement and issue assignment-local
   artifact authority. Eligibility supplies candidates; it never selects them.
6. **Deployment qualifier and registry** remain the sole route-readiness and selection
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

`mlx`, `numpy`, and a future native Android or Apple-mobile backend are runtime
identities. `pixel-stdlib` is not retained as a backend identity. Termux describes one
development host environment, not a runtime class.

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

Every record is canonical, signed where authoritative, size/count bounded, and rejects
unknown fields, invalid nullability, duplicate identifiers, non-finite numbers, stale
incarnations/generations, unsupported protocol versions, or mismatched digests. The
closed contracts never contain invite secrets, signing keys, raw EndpointIDs, private
addresses, usernames, hostnames, private paths, prompts, outputs, token IDs, tensors,
activations, KV data, or unrestricted operating-system diagnostics.

The profile digest is included in capability evidence, class qualification, assignment
eligibility, and any later load or deployment proof. A profile change does not rewrite
past evidence: it increments the evidence generation and makes dependent eligibility
stale until re-evaluated.

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

An eligibility result records `eligible`, `ineligible`, or `unknown`, its current
blockers, authority and evidence generations, qualification ID/digest, constraints, and
expiry. It never contains a route-ready boolean authored by the member. Membership,
platform recognition, installed code, a successful synthetic fixture, or an accepted
Router frame alone cannot advance a role.

Unknown platform, architecture, runtime, transport, lifecycle semantics, source kind,
qualification class, or required measurement is `ineligible` for activation. The
system does not guess from operating-system strings, device brands, participation,
network location, or previous peer labels.

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
  roles; a member or installed runtime is never presented as selectable capacity.
- **Device Lab:** presents the ladder from signed member through probe, artifact, draft,
  and stage eligibility; each rung shows observed/synthetic source, constraints,
  blockers, consent, freshness, and qualification action.
- **Network:** shows only transport capabilities and current bound direct/relay/unknown
  observations; platform does not imply connectivity.
- **Nodes:** separately shows platform family, architecture, identity kind, transports,
  runtimes, signed membership/lease, observed capabilities, qualification, and current
  eligibility.
- **Plans:** displays which exact profile/evidence/qualification generations were used,
  and why an otherwise visible member was excluded. Placement remains planner intent.
- **Readiness:** separates member, profile, evidence, class qualification, artifact,
  load, deployment qualification, registration, and selection proofs.
- **Incidents:** records migration, stale profile, changed build, unsupported capability,
  lease/revocation, lifecycle drain, and eligibility withdrawal without private device
  details.
- **Settings:** shows contract versions, supported platform/runtime registries,
  contribution policy, privacy/retention, migration status, revocation, and advanced
  redacted diagnostics.

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
| `dynamic_ui_projection` | All eight workspaces consume the same live authority generation and registry-driven platform/transport/runtime/capability values. They expose source kind, freshness, blockers, constraints, migration state, and withdrawals without hard-coded device inventories or compound legacy labels. | Browser-local eligibility, brand-specific branches, static supported-device lists, sealed history as current, hidden unknowns, or stale values surviving reconnect. |

`tests/a9_acceptance/inventory.v1.json` is the closed machine-readable acceptance
inventory for these decisions, their fail-closed cases, and all-eight-workspace dynamic
projection requirements. Its protocol is `mycelium.a9_acceptance_inventory.v1`; its
claim boundary is frozen design and future acceptance input only. It contains no schema,
implementation, migration execution, device observation, evidence, readiness, or
completion claim. Repository-local tests enforce its exact decision/case/workspace sets,
bounded shape, privacy vocabulary, specification binding, and `design_only` state.

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

## 10. Physical and browser gates

### Physical positive

On the ordinary A8 product path, migrate current macOS and Linux members using their
existing durable identities. Prove node ID/key continuity, new incarnation and monotonic
generation, current v2 profiles, fresh native observations, and explicit class
re-evaluation. The incumbent remains recoverable and no member gains a role it did not
already physically qualify. Enroll or migrate one recognized but unqualified mobile or
browser member and show it at the exact lower rung it earned.

### Physical negative

Attempt stale-v1 renewal after cutover, changed-key migration, revoked-member migration,
unknown platform/runtime, unqualified capability claims, synthetic-as-native evidence,
expired profile evidence, and stage selection of a membership-only peer. Each fails
closed without route mutation, artifact disclosure, incumbent displacement, or a
fabricated peer-failure incident.

### Browser

All eight live workspaces expose the same v2 authority and human-readable separation.
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
the migration-only reader is removed, macOS/Linux continuity and unearned-eligibility
negatives pass physically, all eight workspaces pass live verification, full contracts,
privacy, governance, security, Python and frontend suites are green, and one atomic A9
feature commit exists. Until then this document remains `design_only`.
