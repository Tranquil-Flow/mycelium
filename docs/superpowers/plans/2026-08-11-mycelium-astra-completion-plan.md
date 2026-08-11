# Mycelium Astra Completion Plan

**Status:** governing successor plan
**Date:** 2026-08-11
**Primary architecture:** `docs/superpowers/specs/2026-08-09-mycelium-astra-architecture-product-design.md`
**Current-state ledger:** `docs/handover/CURRENT_AND_PLANNED_ARCHITECTURE.md`

## 1. Purpose and authority

This plan closes every part of Astra's architecture that is not physically integrated
on the browser product path. It supersedes any linear successor sequence that assumes
M18-M22 are complete merely because a planner, contract, fixture, qualification script,
sealed JSON document, or UI panel exists.

The governing order of evidence is:

1. executing product-path behavior and freshly source-bound physical evidence;
2. closed runtime and qualification contracts;
3. tests against the product path;
4. this plan and capability specifications;
5. historical milestone prose and sealed evidence.

When two documents disagree, the narrower physically observed claim wins. Historical
evidence remains valuable, but it must never be projected as current runtime state.

Tensor parallelism and hybrid pipeline/tensor/data parallelism are not part of this
completion plan. Astra required a separate future specification for tensor parallelism.
They remain review-only future decisions until separately approved.

## 2. Claim vocabulary

Every capability is reported with exactly one of these states:

- `design_only`: a specification or contract exists;
- `implemented_unintegrated`: executable logic exists outside the browser product path;
- `integrated_unqualified`: the product path invokes it, but its physical gate is open;
- `physically_qualified`: fresh positive and negative physical gates passed;
- `registered`: a qualified deployment is present in the live registry;
- `selected`: the registry selected it for new requests;
- `observed`: a browser request exercised it and runtime evidence recorded the event.

No broader state may be inferred from a narrower one. A milestone is not `completed` if
its architectural capability remains design-only or unintegrated.

## 3. Global implementation rules

1. Write and approve the gate-specific implementation specification before changing
   production code.
2. Every cross-process, cross-host, evidence, or browser contract is closed, versioned,
   bounded, canonically digested, and pinned by contract governance.
3. Every accepted gate includes unit, integration, adversarial, unknown-field, stale-
   generation, physical-positive, and physical-negative tests.
4. Physical gates run through the same request gateway, Router port, dispatcher, stage
   runtimes, transport, registry, and browser path used by the product.
5. A qualification-only seam, direct method call, modeled projection, fixture, or
   operator-authored boolean cannot satisfy a product-path physical gate.
6. Live projections carry `source_kind`, `observed_at`, evidence generation, freshness,
   and authority. `source_kind` distinguishes at least `live` and `sealed_historical`.
7. Missing measurements remain `unknown`/`null`; they are never coerced to zero, direct,
   healthy, offline, external-network, ready, or successful.
8. Prompts, decoded text, token IDs, tensors, KV bytes, keys, credentials, raw EndpointIDs,
   private addresses, usernames, and private paths do not cross privacy-reduced evidence
   boundaries.
9. No model download, quantization, conversion, or representation substitution occurs
   without a fresh explicit owner authorization bound to the exact model and revision.
10. A representation decision binds feasibility, acquisition, assignments, stage packs,
    load proofs, execution graph, parity tolerance, qualification, registry, and history.
11. A candidate never displaces the incumbent until it independently qualifies. Model
    selection remains an explicit product action.
12. Each capability gate lands as its own atomic feature commit. Do not squash unrelated
    gates or physical evidence into one milestone commit.
13. Product UI uses human capability names, not internal milestone identifiers.
14. Fixture, replay, sealed history, degraded live, and fresh live modes cannot satisfy
    one another's gates.
15. Refresh, Back/Forward, workspace switching, reconnect, and a second browser session
    preserve or truthfully reconstruct evidence and terminal history.

## 4. Corrected Astra coverage baseline

| Astra section | Accepted baseline | Closing gate |
| --- | --- | --- |
| 4.1 Evidence-driven planning | Qualified, subject to live provenance audit | A0-A1 |
| 4.2 Capability-aware contiguous allocation | Qualified; re-prove memory-tier sensitivity for larger models | A2-A3 |
| 4.3 Directed cyclic topology | Qualified on direct shared-network paths | A8 re-proves unrelated-network operation |
| 4.4 Phase/workload objectives | Partial; concurrency and batching remain modeled | A10 |
| 4.5 Assignment-local artifacts/admission | Partial | A2-A3 |
| 4.6 Progressive routing/immutable paths | Qualified; interruption must preserve it | A4 |
| 4.7 Batching/scheduling/backpressure | Partial; admission is real, runtime batching/overlap are not | A10 |
| 4.8 Data-parallel stage replication | Implemented but not integrated on a compliant product path | A5 |
| 4.9 Speculative decoding | Design-only | A11 |
| 4.10 KV ownership/fault tolerance | Stage-local KV qualified; recovery unintegrated | A6-A7 |
| 4.11 Deterministic scoped replanning | Implemented but unintegrated | A4, A6 |
| 4.12 Signed heterogeneous membership | Partial | A9, A12-A13 |
| 4.13 Traffic-aware liveness | Implemented but unintegrated | A4 |
| 4.14 Authenticated direct/relay transport | Partial; direct observed, relay/off-tailnet incomplete | A8 |
| 4.15 Privacy/authority/qualification | Partial until audits and live truth boundary close | A0-A1, A15 |

M18, M19, and M20 remain open capability gates. M21 is partial. M22 release evidence is
historical until the final executed-artifact closure in A15. M23's heterogeneous stage-
local KV result remains valid within its exact physical and representation boundary.

## 5. Dependency graph

```text
A0 ledger/audits
  -> A1 live evidence mechanism
      -> A2 artifact + representation authority
          -> A3 larger-model qualification
      -> A4 concurrency foundation + traffic liveness
          -> A5 multi-stage replicas
          -> A6 replay recovery
              -> A7 fenced KV recovery
          -> A10 real batching + batched target verification
              -> A11 speculative decoding
      -> A8 off-tailnet control + direct/relay data plane
          -> A9 platform-neutral peer capabilities
              -> A12 Android/iOS activation
              -> A13 user installation and invitation UX
          -> A14 globe/route explorer

A15 release closure depends on every required Astra gate above.
```

A5 is not a prerequisite for A6. Replay may use any newly qualified compatible path;
replicas are one useful successor source, not the definition of recovery. A6 is a
prerequisite for A7 because truthful replay is the fallback when compatible KV cannot
be resumed. A10 precedes A11 because useful speculation requires a real multi-position
target-verification primitive.

## 6. Gates

### A0 — Reconcile claims and restore governance

**Outcome:** one consistent ledger and green boundary/contract audits.

**Observed opening state (2026-08-11):** `scripts/claim_boundary_audit.py` rejects
`deploymentActivation.ts`, `modelCapacityRefresh.ts`, and `modelPreparation.ts` as
unapproved Observatory-side write surfaces. `scripts/contract_audit.py` reports pinned
source digest/size drift for `mycelium_live/registry.py` and
`mycelium_live/supervisor.py`. These remain blockers until the action-authority decision
and contract ownership review are completed; refreshing an allow-list or digest alone
does not close the gate.

- Reconcile milestone rows with the capability matrix. M18/M19/M20 cannot say
  `completed` while their capabilities are unintegrated or design-only.
- Run claim-boundary and contract audits from a clean tree and make both mandatory CI
  inputs.
- Add a consistency check that fails when a milestone state exceeds its capability
  state.
- Pin every protocol crossing a process, host, evidence, or browser boundary.
- Remove references to governing plans that are absent from the repository, or add the
  exact governing plan to source control.
- Resolve every browser write surface as either an explicitly authorized product action
  or a prohibited Observatory mutation.

**Negative gate:** an unpinned boundary protocol, unsupported milestone promotion, or
unapproved UI write surface fails the audit.

**UI:** no new product feature; Settings/Readiness expose contract/source versions and
the current release exclusions.

### A1 — Replace sealed-document-as-live projection

**Outcome:** the UI consumes genuine runtime state; sealed files are clearly historical.

- Replace the one-time loading and repeated serving of M18-M23 JSON documents as if they
  were changing live sources.
- Runtime-owned state is updated by runtime events and observations. Planner intent,
  qualifier decisions, and sealed physical evidence remain separately owned sources.
- Every projection carries capture time, observed time, source kind, generation,
  freshness, and authority.
- Retain sealed artifacts in an explicitly historical/replay register.
- Delete fabricated physical facts, including hard-coded detection success, duplicate-
  delivery success, cleanup success, invented negative recovery, participation-derived
  connectivity, and OS-derived external-network status.
- Replace the fixture-only live workspace fallback. Network, Plans, Readiness, and
  Incidents become real live components using the shared evidence spine rather than a
  stack of milestone panels.
- Rename browser-facing capability endpoints away from milestone numbering.

**Physical positive:** a live value changes during a browser request and retains its
terminal history after refresh. A sealed value is visibly labelled with capture time.

**Negative gate:** stale sealed evidence cannot render as current or satisfy a live gate.

**UI:** all eight workspaces distinguish fresh live, degraded live, sealed history,
replay, and fixture sources.

### A2 — Assignment lifecycle and representation authority

**Outcome:** model preparation uses the exact representation approved and priced by the
planner.

- Add resumable assignment-local transfers, corruption quarantine, bounded retry,
  concurrent staging locks, disk reservation, eviction policy, atomic promotion, and
  zero-duplicate warm reuse.
- Require an owner authorization choosing an existing immutable representation or an
  explicitly named derived representation.
- Reject preparation when authorization, feasibility, builder output, assignment,
  stage pack, or graph representation digests differ.
- Recompute feasibility whenever representation, host set, evidence generation, or
  workload changes.
- Keep newly enrolled peers outside preparation until current capability and directed-
  link evidence includes them.

**Physical positive:** one cold preparation followed by a warm preparation transfers
zero duplicate bytes and loads the exact authorized representation.

**Negative gate:** corrupt reuse, insufficient disk, representation drift, authorization
drift, stale feasibility, interrupted transfer, and concurrent staging fail safely.

**UI:** Models/Settings show representation and lifecycle; Nodes shows per-placement
artifact state; Plans shows allocation and representation binding; Readiness shows exact
blockers; Incidents records acquisition failures without private paths.

### A3 — Qualify useful local models

**Outcome:** Qwen2.5-7B and Qwen3-8B are honest catalog operations, with only physically
qualified representations selectable.

- Qualify `Qwen/Qwen2.5-7B-Instruct` first using one explicit representation decision.
- Run a memory-tier A/B, not only a compute-coefficient A/B, and require a traceable
  allocation change or an evidence-backed explanation for stability.
- Prove independent reference parity, stage load, prefill/decode, stage-local KV,
  terminal cleanup, Router frame movement, browser streaming, and history persistence.
- For `Qwen/Qwen3-8B`, retain its complete compatible catalog identity. If its existing
  representation is infeasible, keep it visibly blocked. Any quantized representation
  requires separate owner approval, immutable identity, feasibility, parity tolerance,
  preparation, and physical qualification; it is never a hidden builder default.
- Scope every qualification to one model revision, representation, evidence generation,
  workload, host set, topology, and runtime set.

**Negative gate:** an infeasible, stale, incomplete, mismatched, unqualified, or dead
deployment cannot enter the selector.

**UI:** Inference model selector; Settings catalog/lifecycle; Plans allocation and
representation; Network proposed/selected/observed topology; Nodes layer ranges and
resident bytes; Readiness parity/qualification; Incidents preparation or load failure.

### A4 — Product-path concurrency, interruptible commands, and scoped liveness

**Outcome:** live traffic detects failure promptly without unnecessarily killing the
entire deployment.

- Decompose the generation-long route-global lock into bounded per-request, per-session,
  per-stage, and authority locks.
- Replace the single dispatch thread with a bounded dispatcher/worker pool that preserves
  admission, QoS, ordering, backpressure, cancellation, and exact cleanup.
- Make blocked stage/transport commands interruptible within declared budgets.
- Integrate traffic-aware receipts, receipt suppression, idle keepalive, active failure,
  suspect/quarantine/recovery, and generation conflict into the live route.
- Replace route-global fatal latching with request/edge/placement/peer/deployment scope.
- A non-participating peer failure must not affect the active route. A participating peer
  affects only tracks containing it unless a deployment-wide invariant is actually lost.
- Enumerate and justify the few errors that remain deployment-fatal.

**Physical positive:** measured active-disconnect and idle-staleness detection on a
browser request, including interruption before the old command timeout.

**Negative gate:** one missed receipt enters `suspect` only; a non-participating peer exit
leaves admission available; cleanup and immutable-path reservations remain exact.

**UI:** Inference waiting/interruption state; Network edge health; Nodes detector state;
Plans freshness; Readiness budgets; Incidents scoped detector source and action; Settings
detector policy.

### A5 — Multi-stage data-parallel stage replication

**Outcome:** Astra 4.8 is proven through the normal product path.

- Re-specify replication as replicas of one or more contiguous stages inside a graph
  containing at least two pipeline stages.
- Planner produces complete legal request tracks, failure-domain constraints, flow
  assignment, marginal benefit, and zero-flow removal.
- Browser requests traverse the ordinary gateway, bounded dispatcher, Router port, and
  live stage runtimes; delete the qualification-only throughput seam.
- Compare primary-only and replicated configurations at identical offered concurrency
  and workload.
- Relabel the existing single-stage whole-model replica observation as historical whole-
  model replication or remove it from the Astra 4.8 claim.

**Physical positive:** at least two concurrent browser requests use distinct legal tracks
on a multi-stage pipeline with a replicated stage, per-placement work counters, parity,
and material throughput gain.

**Negative gate:** removing or failing a replica reduces measured capacity and blocks only
new admissions to affected tracks; surviving tracks remain legal.

**UI:** Inference request track; Network replica tracks; Nodes replica group/range/work;
Plans flows and marginal gain; Readiness per-replica qualification; Incidents zero-flow,
loss, and drain actions.

**Boundary:** request-level data parallelism only; not full-model data parallelism and not
tensor/hybrid parallelism.

### A6 — End-to-end full-context replay recovery

**Outcome:** a real browser request continues after an assigned peer/path failure without
duplicate output.

- Store the original encoded prompt, committed-token count/digest, attempt, path,
  generation, and terminal authority in the live request lifecycle rather than a script.
- A successor is any newly qualified compatible immutable path; it need not be a replica
  created by A5.
- Reconstruct state from the prompt plus committed generated tokens. Only tokens after
  the committed watermark may reach the browser.
- Integrate scoped candidate intent, hysteresis, bounded attempts, circuit breakers,
  reservations, cutover, cleanup, and restart reconciliation.

**Physical positive:** kill an assigned peer during a browser request, qualify/use a
successor, continue with reference parity, zero duplicated delivery, one terminal result,
and exact cleanup.

**Physical negative:** no compatible successor produces an explicit abort and no
continuity/KV-transfer claim. Both positive and negative cases must be observed, not
authored by a sealing script.

**UI:** Inference attempt/committed count/recovery mode; Network ghosted old and active
successor routes; Plans retained tracks and candidate hysteresis; Readiness successor
gates; Incidents complete timeline and breaker state.

### A7 — Fenced KV-successor recovery

**Outcome:** a compatible standby resumes from acknowledged KV without stale publication.

- Bind source and successor to identical model representation, layer range, KV schema,
  attention layout, dtype, RoPE, decode mode, checkpoint digest, attempt, and generation.
- Replicate monotonic acknowledged watermarks and fence old processes/generations.
- Exercise rollback, skipped watermark, repeated watermark, cross-generation, stale
  attempt, incompatible layout, and duplicate-terminal rejection before successor work.
- Physically exercise fallback to A6 replay when KV is incompatible or unavailable.

**Physical positive:** mid-request compatible successor cutover continues without replay,
duplicate output, or stale publication.

**Physical negative:** every mismatch rejects KV continuity and either replays through A6
or terminates honestly.

**UI:** recovery mode, KV outcome, watermark count, source/successor generation, fallback,
cutover, cleanup, and terminal result without exposing token IDs or KV data.

### A8 — Internet-native control and activation planes

**Outcome:** a device can join, be measured, qualify, and serve across unrelated networks
without Tailscale as a product dependency.

- Make the membership/control endpoint safely reachable off-tailnet through an approved
  authenticated bootstrap design; do not solve only the activation data plane.
- Iroh remains the EndpointID-authenticated activation transport.
- Report missing RTT/goodput/jitter/loss as unknown, never zero.
- Replace raw relay address projection with a privacy-safe stable relay identity and
  region when available.
- Delete participation-derived connectivity and OS-derived external-network claims.
- Confine SSH to explicitly labelled operator staging on owner-controlled hosts. It is
  absent from the external reviewer/user path.

**Physical positive:** an off-tailnet join, one direct route, one forced-relay route, one
observed direct/relay transition, persistent connection reuse, reconnect, and a completed
browser inference.

**Physical negative:** with Tailscale disabled, join, measurement, qualification, and
serving still work; unauthorized bootstrap and stale/revoked identities fail closed.

**UI:** Device Lab reachability; Network direct/relay/unknown and measurements; Nodes
selected path; Plans path costs; Readiness transport proof; Incidents transitions;
Settings bootstrap/relay policy.

### A9 — Platform-neutral peer and capability contract

**Outcome:** eligibility derives from signed capabilities and class-specific
qualification, not device brands.

- Version the membership contract before adding more mobile platforms.
- Separate identity class, platform, transport, runtime backend, and capability profile.
- Support platform values including macOS, Linux, Windows, Android, iOS, iPadOS, browser,
  and future unknown-but-ineligible values.
- Capability profile covers activation protocol, decode modes, context/concurrency,
  sustainable memory, thermal/power class, lifecycle/background semantics, and network-
  loss behavior.
- Rename `pixel_http`/`pixel-stdlib` product identities to generic Android/mobile
  capability values. Pixel remains a test device, not a protocol or backend class.
- Update external-participant policy and every backend/eligibility/UI consumer once.

**Physical positive:** existing macOS/Linux members preserve identity and re-enroll or
upgrade across the version transition without gaining unearned eligibility.

**Negative gate:** unknown fields/classes fail closed; membership alone never grants
placement; claimed capabilities without class qualification remain ineligible.

**UI:** Nodes separately shows platform, transport, runtime, capabilities, membership,
and eligibility; Device Lab shows each qualification gate; Readiness distinguishes member
from stage-ready.

### A10 — Real continuous batching, pipeline overlap, and target batch verification

**Outcome:** Astra 4.7 is physically complete and speculation has a real verifier.

- Integrate the batching policy into the live physical path.
- Add a versioned bounded multi-position target verification operation with explicit KV
  prefix, rollback-to-prefix, parity, cancellation, and resource semantics.
- Implement runtime microbatching, continuous arrival handling, pipeline overlap, bounded
  queues, slow-consumer protection, QoS/aging, and per-request attribution.
- Baselines and candidates use the same route, session, workload, offered concurrency,
  token limits, and measurement window.
- Never infer a batch from concurrent admission; record actual batch membership.

**Physical positive:** concurrent and late-arriving browser requests preserve exact output
while materially improving throughput; interactive tail latency remains within its frozen
budget. One real multi-position verification call matches sequential target authority.

**Negative gate:** cancellation, timeout, queue overflow, slow consumer, partial batch
failure, and rollback clean up every request independently.

**UI:** Inference batch attribution; Network overlap trace; Nodes runtime batch; Plans
observed versus modeled workload; Readiness throughput/parity; Incidents backpressure;
Settings QoS/batch policy.

### A11 — Speculative decoding with measured benefit

**Outcome:** optional target-authoritative speculation improves a useful workload or is
honestly disabled.

- Use A10's runtime-advertised multi-position verification capability; delete phantom
  capability fields inferred only by a qualification script.
- Bind target/draft revisions, tokenization/vocabulary/special-token/position semantics,
  proposal width, target/draft KV ownership, transfer, verification, rollback, and cleanup.
- Capture target-only and speculative baselines on the same route/session/workload.
- Add an adaptive bounded proposal deadline; a slow or thermally throttled draft cannot
  make the target wait indefinitely.
- Prove desktop draft operation before Android/iOS draft tests.
- Draft loss falls back target-only. Target/path loss enters A6/A7 recovery.
- Promote only with parity and the frozen material-gain threshold, including its required
  confidence bound. Otherwise publish a measured disabled decision.

**Physical positive:** measured acceptance, transfer, batch verification, end-to-end
wall-clock gain, fallback, and output parity on a useful larger target.

**Physical negative:** incompatible draft, low acceptance, slow draft, draft loss, target
loss, rollback, and cancellation behave without target-KV corruption.

**UI:** Inference optional overlay/counters; Nodes draft role; Plans compatibility and
observed gain; Readiness promotion decision; Incidents fallback; Settings preference and
disabled reason.

### A12 — Generic Android and iOS/iPadOS activation

**Outcome:** mobile participation is capability-based, staged, and physically qualified.

Eligibility progresses independently through:

1. signed member;
2. probe/evidence contributor;
3. speculative draft worker;
4. qualified model stage.

Android requirements:

- retain Termux as a development/conformance route, not the final installation model;
- qualify a second non-Pixel Android device before claiming general Android support;
- exercise parity, sustainable memory, thermal drain, battery/power policy, suspend,
  network loss/reconnect, cancellation, artifact integrity, and cleanup.

iOS/iPadOS requirements:

- native signed application with embedded authenticated transport and a separately
  qualified native model runtime; no Termux/Python/exec assumption;
- initial inference eligibility is foreground-active only;
- suspension is a normal drain/termination outcome, not automatically a peer-failure
  incident;
- reconnect after suspension is normal and generation-fenced;
- parity, available-memory pressure, thermal state, power, lifecycle, and network-loss
  gates are re-proven for each backend;
- mobile draft qualification may precede full-stage qualification.

**Physical positive:** generic Android proof on two device families and iOS/iPadOS proof
on an available test device, each at the exact eligibility level claimed.

**Negative gate:** background/suspend, thermal pressure, low memory, revoked membership,
network loss, incompatible runtime, and unqualified stage selection fail or drain safely.

**UI:** Device Lab qualification ladder/consent; Network mobile edges; Nodes platform,
capabilities and current eligibility; Plans mobile placement; Readiness class gates;
Incidents thermal/network events; Settings contribution and revocation controls.

### A13 — Cross-platform installation and invitation UX

**Outcome:** normal users join safely without filesystem, seed URL, EndpointID, sidecar,
or CLI knowledge.

- Provide signed packages/apps for supported platforms and durable managed services where
  the OS permits them.
- Use a short-lived, single-use QR/deep-link invitation. The target device creates and
  retains its own identity.
- Show explicit consent: assigned peers may see assigned weights, activations, timing,
  and network metadata.
- Automate bootstrap reachability, identity creation, capability probe, lease renewal,
  reconnect, update, and revocation while retaining advanced diagnostics.
- Joining never modifies active Router state or deployment selection.
- Suggested distribution targets: signed macOS package, Linux package/service, Windows
  installer/service, signed Android app, TestFlight/native iOS/iPadOS app, and a clearly
  limited browser member.

**Physical positive:** a clean device installs, scans/opens an invitation, joins off-
tailnet, appears in Device Lab, runs preflight, and remains ineligible until qualified.

**Negative gate:** expired/reused/forged invite, duplicate identity, revoked device,
unsupported platform, failed update, and interrupted onboarding remain recoverable and
do not alter inference.

**UI:** Device Lab wizard and consent; Nodes enrollment/lease; Readiness onboarding proof;
Incidents join failures; Settings invite, quota, audit, update, and revocation controls.

### A14 — Privacy-preserving globe and route explorer

**Dependencies:** A1 and A8. Do not begin while sealed artifacts are presented as live,
missing metrics become zero, relay identity leaks an address, or the live Network/Plans/
Readiness/Incidents workspaces remain fixture-only fallbacks.

- Define a coarse-region evidence contract using operator-declared region, privacy-safe
  relay region, or a labelled latency-distance class. Never infer or expose exact peer
  coordinates from IP data.
- Unknown-region peers appear in an explicit tray/logical layout, never at a fabricated
  coordinate.
- Keep proposed, selected, observed, failed, and recovered paths independent.
- Keep direct, relay, and unknown transport independent.
- Show eligible/ineligible members, forward activation, decode loopback, replica tracks,
  recovery cutover, and optional speculative draft paths.
- Provide accessible table/2D alternatives, reduced motion, and a low-power mode.

**Physical positive:** a proposed path, actual direct path, forced relay path, route
transition, and recovered route update from bound live evidence and persist on refresh.

**Negative gate:** absent region/link evidence renders unknown; historical routes remain
labelled historical; the globe cannot become planner or qualification authority.

### A15 — Executed-artifact release closure

**Outcome:** release readiness is computed from executions, not an operator affidavit.

- Replace hand-written release booleans with digests and results from executed tests,
  audits, physical runs, browser runs, source revisions, model/representation manifests,
  and reviewer evidence.
- UI audit distinguishes `verified_live`, `verified_replay`, and `verified_fixture`; one
  cannot satisfy another.
- Run clean Python, Rust, browser-engine, contract, privacy, security, accessibility,
  performance, cold-bootstrap, restart, corruption, mobile, unrelated-network, recovery,
  replica, batching, and model-selection gates.
- A clean external reviewer joins without Tailscale, contributes only after qualification,
  runs a real prompt, observes route evidence in the UI, and reproduces the negative case.
- Seal exact exclusions. No public or complete-Astra claim survives an unapproved required
  exclusion.

**Negative gate:** altered, absent, stale, unsigned, manually asserted, fixture-only, or
source-mismatched evidence withholds release.

**UI:** Readiness shows executed gate provenance; Settings shows source/contracts and
approved exclusions; Incidents retains negative runs; every workspace exposes its relevant
live closure evidence in product language.

## 7. UI verification rule

Every gate must visibly and testably advance the existing eight workspaces where relevant:
Inference, Device Lab, Network, Nodes, Plans, Readiness, Incidents, and Settings. A backend
feature with no truthful UI projection is not product-complete; a UI projection with no
runtime authority is not implementation evidence.

At every gate, browser tests cover direct navigation, refresh, Back/Forward, workspace
switching, reconnect, degraded source, stale source, sealed history, a second session,
responsive layouts, keyboard use, reduced motion, privacy redaction, and accessible names.

## 8. Completion definition

The Astra architecture is complete only when:

1. every Section 4 capability is physically qualified on the product path and visible in
   the UI, or the owner separately approves an explicit rejection;
2. A0-A15 required gates pass at their exact boundaries;
3. an assigned peer can fail mid-request and the request either recovers by A6/A7 or
   terminates explicitly without corruption, duplication, or stale publication;
4. multiple browser requests exercise compliant multi-stage replicas and real runtime
   batching;
5. unrelated-network peers can join and serve through Iroh direct/relay operation without
   Tailscale as a product dependency;
6. Android and iOS/iPadOS claims never exceed their independently qualified eligibility
   level;
7. Qwen2.5-7B and any serving Qwen3-8B representation are selected only after exact
   representation-bound physical qualification;
8. speculative decoding is either physically beneficial under its frozen gate or visibly
   disabled with measured reasons;
9. all eight workspaces consume genuine live authorities and label sealed history;
10. the release decision is derived from executed artifacts rather than assertions.

## 9. Separately reviewed future program

The following are not added by this plan: tensor parallelism, hybrid pipeline/tensor/data
parallelism, full-model data parallelism, cross-backend KV migration, prefix caching,
disaggregated prefill/decode, expert parallelism, and sequence parallelism. Each requires
its own approved architecture, correctness model, planner/runtime contracts, physical
positive and negative gates, UI truth boundary, and resource/failure analysis.
