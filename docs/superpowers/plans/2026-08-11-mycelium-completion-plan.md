# Mycelium Completion Plan

**Status:** governing successor plan
**Date:** 2026-08-11
**Primary architecture:** `docs/superpowers/specs/2026-08-09-mycelium-architecture-product-design.md`
**Current-state ledger:** `docs/handover/CURRENT_AND_PLANNED_ARCHITECTURE.md`

## 1. Purpose and authority

This plan closes every part of the synthesized architecture that is not physically integrated
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
completion plan. The source synthesis required a separate future specification for tensor parallelism.
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

### 3.1 Accelerated execution discipline

Acceleration changes execution order and work-in-progress limits only. It does not
remove, merge, defer beyond A15, or weaken any capability, physical gate, negative
gate, UI projection, documentation obligation, regression, or release criterion in
this plan.

1. Maintain exactly one primary capability gate on the completion critical path. A
   gate remains primary until its specification, product integration, physical
   positive and negative evidence, all-eight-workspace UI verification, regressions,
   ledger/handover updates, and atomic feature commit are complete.
2. Freeze the primary gate to its written acceptance boundary. Do not add adjacent
   polish, future-gate behavior, or opportunistic architecture changes unless they are
   required to pass that boundary.
3. Long transfers, remote staging, model loading, qualification runs, and temporarily
   unavailable devices may be overlapped only with independent work that cannot alter
   the active physical run: the next dependency-ready specification, focused tests,
   reusable automation, or a shared foundation explicitly required by multiple named
   upcoming gates.
4. Shared foundations are implemented once at the earliest gate that needs them and
   must name every immediate consumer. They do not authorize claiming, committing, or
   presenting a later gate as integrated before its own physical and UI evidence
   passes.
5. Prefer repeatable product-path automation for evidence capture, staging,
   qualification, browser verification, and release assembly. Automation must retain
   the same fresh-source, authority, privacy, and fail-closed requirements as the
   manual gate.
6. A device-dependent delay does not broaden the gate or lower its evidence standard.
   Continue safe independent work, preserve the incumbent demo, and resume the exact
   physical gate when an eligible device returns.
7. Run focused tests throughout implementation. Run the gate's full required
   regression and audit set once the implementation and evidence shape are stable,
   then rerun only suites invalidated by subsequent fixes before the atomic commit.
8. Keep a live gate checklist mapped to the dependency graph and all eight product
   workspaces. Deferred items retain an explicit owner gate and cannot disappear into
   prose, a generic backlog, or a later release assertion.
9. After the primary gate's atomic commit, advance immediately to the next
   dependency-ready gate. Follow the dependency graph rather than internal milestone
   numbering or whichever implementation happens to be easiest.

The default critical path from the current checkpoint is A3, then A4, followed by the
dependency-ready branches shown below, with A15 remaining the executed closure over
every completed branch.

### 3.2 Parallel execution model

Parallel work is organized into isolated lanes. Dependency readiness permits work; it
does not permit two lanes to edit or exercise the same integration surface at once.

| Lane | Ordered gates | May proceed alongside | Integration constraint |
| --- | --- | --- | --- |
| Model qualification | A3 | dependency-ready design and preflight only | A3 owns the active physical route and model artifacts until its atomic close |
| Runtime resilience | A4 -> A6 -> A7 | A5, A10-A11, and the internet/platform lane after A4 | one integrator owns gateway, dispatcher, Router, route lifecycle, cancellation, and liveness wiring |
| Capacity | A5 | A6-A7, A10-A11, internet/platform | isolated planner/replica modules may build in parallel; shared route wiring and physical hosts are serialized |
| Scheduling | A9 -> A10 -> A11 | A5-A7 and internet/platform | A9 closes the accelerated-runtime and component-cost prerequisite; isolated scheduler/verifier modules may then build in parallel while dispatcher and stage-runtime wiring remain serialized |
| Internet/platform | A8 -> A9 -> A12/A13, plus A14 after A8 | A4-A11 runtime, capacity, and scheduling work | bootstrap/transport and platform packages stay isolated; A9 also owns the physically qualified accelerated Windows/runtime-efficiency boundary; supervisor, membership, contracts, and shared UI wiring are integrated by their designated owner |
| Release | A15 | none as a completion claim | begins only after every required predecessor has an atomic commit and fresh evidence |

At most one gate owns shared integration at a time. Up to three additional worktrees may
perform dependency-ready isolated implementation, tests, specifications, or preflight.
Before A4 closes, later runtime gates may write specifications, acceptance tests, and
new leaf modules, but may not integrate against guessed concurrency or liveness APIs.

The preferred execution waves are:

1. Close A3. In parallel, finish A4 acceptance tests and A8 infrastructure decisions.
2. Integrate and close A4. In parallel, build isolated A8 bootstrap/transport components
   and prepare A5, A6, and A10 test harnesses against the frozen A4 specification.
3. After A4, implement A5 and A6 in separate worktrees while A8 continues; A10 may
   prepare isolated harnesses but cannot integrate or qualify until A9 closes. Merge shared
   runtime wiring one gate at a time; reserve physical hosts for one gate run at a time.
   Start A7 only after A6 closes and A11 only after A10 closes.
4. After A8, run A9 contract migration once, then close its accelerated Windows backend,
   activation-efficiency, component-cost, and single-request performance gates. A14a
   (accessible route table and logical
   graph) may then proceed immediately; A14b (coarse globe and region evidence) follows
   its region-vocabulary freeze. A12 waits on A4 and A9, and A13 waits on A12. Only the
   optional A12 speculative-draft-worker claim waits on physically closed A11. Build
   each package in isolation once its exact prerequisites close.
5. Run A15 only from the integrated branch after every required gate and external
   prerequisite is closed.

### 3.3 Worktree and integration protocol

Every parallel assignment starts from a recorded base commit and has a gate packet with:

- one gate identifier, frozen specification, and exact acceptance/negative gates;
- an allowed-path set, a prohibited shared-path set, and a named integration owner;
- private ports, run IDs, member identities, seed databases, cache/artifact roots, and
  evidence directories;
- focused test commands, contract/privacy checks, and the required physical devices;
- a list of assumptions that must be confirmed before shared wiring or physical claims.

Use a dedicated `codex/aNN-short-name` branch and worktree for each gate. A feature worker
adds leaf modules and focused tests without opportunistic cleanup. Only the current
integration owner edits shared entry points. The integrator reviews and applies one
atomic gate commit in prerequisite order, then runs the invalidated focused suites and
the gate regression. Generated fixtures are regenerated once by the integrator after
source contracts settle; parallel workers do not hand-edit them.

The following are exclusive integration surfaces and require an explicit reservation:

- request gateway, dispatcher, Router port, route/session lifecycle, cancellation,
  liveness, registry, activation, and supervisor wiring;
- `ui/web/src/App.tsx`, `LiveRouteWorkspace.tsx`, global navigation, route status, and
  cross-workspace live-source composition;
- membership schemas, contract manifests, compatibility fixtures, generators, and the
  governance ledger;
- the live device fleet, listening ports, seed/member databases, model/artifact caches,
  registry state, and physical evidence output roots;
- current-state ledgers, release handover, and any document that asserts completion.

Two lanes may not share a physical peer during destructive failure, thermal, memory,
network-loss, or performance qualification. Read-only observation may overlap only when
it cannot change load or measurements. A failed integration is fixed in its feature
worktree or reverted as one atomic commit; unrelated in-flight work is not patched into
the integration branch to rescue it.

### 3.4 Blocker preflight

Preflight is required before production integration begins. A failed preflight blocks
only the affected physical claim; specification, isolated tests, and unrelated lanes may
continue.

| Gate | Must be resolved before integration or physical qualification |
| --- | --- |
| A3 | exact local model/revision and authorized representation; resumable artifact path; stable source and target storage; reference backend; eligible route capacity |
| A4 | frozen lock/worker/cancellation/liveness ownership model; deterministic fault injector; latency budgets; no active A3 route mutation |
| A5 | enough independently eligible placements for two complete legal tracks with a replicated stage; identical workload; frozen material-throughput threshold |
| A6 | two compatible qualified immutable paths; controlled assigned-peer failure; committed-token authority and replay budget |
| A7 | standby memory and runtime compatibility for the exact layer/KV identity; watermark/fencing contract; proven A6 fallback |
| A8 | reachable authenticated membership bootstrap; domain/certificate and relay/rendezvous decisions; NAT/firewall test matrix; revocation authority; off-tailnet test peer |
| A9 | one versioned migration plan for every producer and consumer; unknown-field and downgrade policy; re-enrollment behavior; one native accelerated Windows x86_64 runtime candidate; exact backend/provider/build and int8-kernel evidence; float16/bfloat16 activation candidate; component-aware compute/serialization/transport instrumentation; frozen reference-versus-candidate single-request corpus and performance floor |
| A10 | dependencies A4 and A9 atomically closed; representative concurrent workload; frozen throughput and interactive tail-latency thresholds; deterministic slow-consumer/cancellation harness |
| A11 | locally present compatible draft candidate or an approved measured-disabled outcome; frozen gain/confidence threshold; target-only baseline |
| A12 | two Android device families for a general Android claim; an available iOS/iPadOS test device; native runtime, signing, lifecycle, thermal, and power test access |
| A13 | frozen supported-platform release matrix; signing/notarization/TestFlight/installer credentials and owners; clean install targets; update/revocation service |
| A14 | privacy-approved region evidence and accessible non-globe fallback; live A8 direct/relay/transition evidence |
| A15 | clean off-network reviewer/device scheduled; stable release matrix; fresh evidence runners; no required gate still relying on fixture or sealed history |

For A5, A10, and A11, the gate specification must define `material`, the measurement
window, repetitions, confidence calculation, allowed regression, and pass/fail treatment
before candidate measurements are seen. For A12-A13, unavailable hardware, accounts, or
signing authority must be reported as an explicit external blocker rather than absorbed
into implementation time or weakened into a simulated completion claim.

### 3.5 Gate flow and stop rules

Each gate uses the same short pipeline:

1. `ready_to_build`: dependencies, specification, contracts, thresholds, allowed paths,
   and deterministic test harness are frozen.
2. `ready_to_integrate`: isolated implementation and focused positive/negative tests pass;
   shared-surface conflicts are absent or assigned to the integrator.
3. `ready_to_qualify`: required devices, identities, storage, network, models, ports, and
   evidence roots pass preflight; the incumbent demo remains recoverable.
4. `ready_to_commit`: product-path positive and negative gates, all relevant UI views,
   refresh/reconnect, audits, and invalidated regressions pass from fresh sources.
5. `closed`: one atomic feature commit exists and ledgers describe only observed scope.

Stop and return a gate to the prior state when an API is guessed, a threshold is chosen
after results are known, a physical resource is shared with another load-bearing run,
evidence provenance becomes ambiguous, or the implementation requires an unapproved
model representation, download, external service, credential, platform, or scope change.
Do not spend repeated days retrying an environmental failure: after one reproducible
failure and one controlled retry, record the blocker, preserve diagnostics, release the
fleet reservation, and advance another dependency-ready lane.

### 3.6 Long-operation liveness and work conservation

Conversion, sharding, hashing, acquisition, peer staging, model loading, physical
qualification, browser automation, and full regression are product operations rather
than opaque shell waits. Every remaining gate that launches one of these operations
must keep the operation recoverable, observable, and independent from the agent's
ability to continue safe dependency-ready work.

1. **Durable supervision.** Long work runs under a durable product supervisor with a
   stable operation identity, bounded cancellation, terminal status, private logs, and
   a read-only status surface. An interactive agent polls it briefly; the agent does
   not occupy its primary lane with a single blocking wait or depend on a terminal
   session remaining attached.
2. **Checkpoint before repeating bytes.** At minimum, applicable operations durably
   checkpoint `authority_frozen`, `source_verified`, every
   `representation_shard_verified`, `candidate_manifested`,
   `local_challenge_passed`, `current_membership_bound`, every
   `assignment_acquired`, every `peer_staged`, `parity_passed`,
   `activation_passed`, `physical_qualification_passed`,
   `browser_verification_passed`, and `evidence_sealed`. A checkpoint is canonical,
   private, atomically replaced, fsynced, and bound to the filesystem identity,
   immutable model/representation/assignment authority, and exact artifact digests.
   Restart validates the checkpoint and resumes after the latest completed phase; it
   does not reconvert, recopy, or retransmit already verified bytes.
3. **Warm-first execution.** Before cold work, the product searches authorized
   content-addressed stores for the exact representation and stage objects. Exact warm
   reacquisition invokes the ordinary local and remote acquisition paths and records a
   new terminal receipt for every stage with cached verified bytes equal to total
   bytes, zero transferred/origin/peer bytes, and the unchanged promotion digest.
   File presence, an earlier receipt, or a build report alone is insufficient.
4. **Physical preflight before expensive work.** Preflight verifies source digests,
   owner authority, peer reachability, sleep/power/thermal policy, current available
   memory, runtime/backend support, removable-volume identity and stability, negotiated
   storage-link rate, and free bytes for source, object, promotion, temporary, and
   safety-reserve footprints. Required capacity is calculated from the exact operation;
   no generic "disk available" boolean may launch a long cold path. A failed preflight
   publishes a bounded recovery action before model bytes are scanned or copied.
5. **Progress is product evidence.** The live API and relevant UI expose operation,
   phase, current shard/file or bounded work unit, completed and total bytes, cached
   and transferred bytes, rolling throughput, elapsed time, ETA when supportable,
   last-progress time, latest checkpoint, lease expiries and their exact scope, active
   blocker, retry count, and recommended recovery. Unknown values remain unknown.
   Refresh, reconnect, Back/Forward, and a clean second session reconstruct this state
   from durable product authority.
6. **Phase-aware stall detection.** Each long phase declares an expected byte/work
   budget, observed rate, progress heartbeat, no-progress threshold, bounded retry
   policy, and cleanup owner before launch. A watchdog diagnoses storage wait, peer
   loss, lease risk, process exit, memory pressure, thermal pressure, or genuine lack
   of progress. It does not kill a healthy hashing, compilation, or storage operation
   merely because no token or browser event has appeared.
7. **Resource-aware parallelism.** The primary gate reserves its physical peers,
   memory, storage bandwidth, ports, and evidence roots. Parallel lanes may use only
   non-overlapping resources and may not run a second preparation against the same
   disk, a heavy regression against a memory-bound inference host, or a destructive
   fault test against a participating peer. CPU-heavy verification should use an idle
   eligible host where practical. Process count is not a throughput metric.
8. **Work-conserving scheduling.** While the primary gate is legitimately waiting on
   physical I/O or an unavailable peer, the integration owner selects the highest
   dependency-ready non-conflicting item: next-gate specification/RED tests, focused
   regression, evidence review, browser automation preparation, or reusable preflight
   tooling. Parallel output cannot claim a later gate, edit a reserved integration
   surface, or invalidate the running physical operation.
9. **Just-in-time lease refresh.** Capacity leases remain short-lived safety evidence.
   A valid preparation start may freeze immutable deterministic build authority and a
   bounded acquisition grant may cover approved bytes, but warm reacquisition,
   activation, selection, and physical qualification recapture fresh authority at
   their owning boundary. Refresh adds eligibility; it never silently rewrites the
   frozen representation or placement. An incompatible refresh preserves completed
   immutable work, withholds publication, and returns an explicit replanning blocker.
10. **Verification economy.** Focused tests run during implementation. The complete
    gate regression/audit set runs after contracts and evidence shapes stabilize, then
    only invalidated suites rerun after bounded fixes before the atomic commit. Build,
    dependency, browser, and immutable artifact caches may be reused when their exact
    digests remain valid; physical and freshness claims may not be cached across their
    declared authority boundary.
11. **Measured retrospective.** Every atomic gate close records cold and warm duration,
    materialized/hashed/transferred/reused bytes, time waiting on storage/network/
    leases/peers/tests/browser work, retries, checkpoint recoveries, duplicate bytes,
    and the next gate's approved throughput improvements. The following gate's
    preflight incorporates those observed bottlenecks rather than relying on prose.

The long-operation requirements are acceptance criteria wherever applicable. A gate is
not `ready_to_commit` when its happy path completes only while one terminal, peer,
volume, browser session, or unexpired short observation lease remains continuously
available.

## 4. Synthesized architecture coverage baseline

| Architecture section | Accepted baseline | Closing gate |
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

### Current execution checkpoint — 2026-08-15

A2 is complete and A3 is the next active gate. The live
Qwen2.5-0.5B representation is freshly qualified
for the Lenovo-independent M4 Pro -> Surface route. Planner-v2 independently selected
the contiguous `node-0 [0,23) -> node-2 [23,24)` allocation from fresh signed resource
and activation-plane evidence. Capacity refresh exposes the exact immutable
`int8-weight-only` replacement operation and its assignment-local artifact bytes without
silently authorizing a conversion; signed member inventory discovery expands the
catalogue without granting selection authority. A generic mobile HTTPS artifact-source
member is current and may source assigned chunks, but remains permanently ineligible
for model-layer placement.

The A2 artifact protocol is physically proven for cold two-source acquisition, warm
zero-byte reuse, serving-traffic pacing, source loss/rotation, corrupt-cache quarantine,
interrupted-process recovery, persistent signed-grant replay rejection, disk admission,
manifest substitution, concurrent staging, cancellation, no-source failure, membership
drift, representation/authorization drift, stale feasibility, and unassigned requests.
Every authorized source holds every exact chunk, while transfer grants remain scoped to
the recipient's current assignment. No model download, conversion, quantization, or
representation substitution was used.

The owner authorized only the exact `int8-weight-only` representation of existing local
revision `7ae557604adf67be50417f59c2c2f167def9a775`; no download or model
substitution was authorized or performed. The ordinary product path then recorded a
302,097,376-byte two-source cold Surface acquisition with zero origin and exact warm
reuse with zero transfer. Executed artifact
`a2-product-gate-20260815-after-warm.json` passed every check and binds those records to
a fresh physical inference returning `Paris`, stable route identity, and positive
counters on both stages. The prepared candidate is visible to activation authority with
zero invalid candidates. All eight UI workspaces passed live browser checks, including
refresh, navigation, Back/Forward, fail-closed outage, automatic session renewal on
reconnect, and an independent second session. Lenovo restoration remains explicitly
outside the close gate. The final full Python/frontend/governance/contract/security
regression is green and the single atomic A2 commit is present. A3 work must remain in
its own milestone commit.

## 5. Dependency graph

```text
A0 -> A1 -> A2 -> A3
A3 -> A4
A3 -> A8
A4 -> A5
A4 -> A6 -> A7
A4 -> A10 -> A11
A8 -> A9
A9 -> A10
A8 -> A14
A4 -> A12
A9 -> A12
A11 -[optional mobile speculative-draft-worker claim]-> A12
A12 -> A13

A15 release closure depends on every required architecture gate above.
```

The diagram is sequencing authority, not only architectural ancestry. A4 and A8 do not
enter production integration until A3 has its separate atomic close. The exact direct
prerequisites are:

| Gate | Direct prerequisite(s) |
| --- | --- |
| A0 | none |
| A1 | A0 |
| A2 | A1 |
| A3 | A2 |
| A4 | A3 |
| A5 | A4 |
| A6 | A4 |
| A7 | A6 |
| A8 | A3 |
| A9 | A8 |
| A10 | A4; A9 |
| A11 | A10 |
| A12 | A4; A9 (see the resolved A12 owner decision below) |
| A13 | A12 |
| A14 | A8; A1 is already transitive |
| A15 | A0-A14 required gates |

A5 is not a prerequisite for A6. Replay may use any newly qualified compatible path;
replicas are one useful successor source, not the definition of recovery. A6 is a
prerequisite for A7 because truthful replay is the fallback when compatible KV cannot
be resumed. A9 is a direct prerequisite for A10 because batching measurements are not
meaningful on a reference-only Windows backend or a route whose component, conversion,
serialization, and transport costs are not separately observed. A10 precedes A11 because
useful speculation requires a real multi-position target-verification primitive.

**A12 and A13 corrections (2026-08-18).** The earlier rows `A12 | A9` and `A13 | A9` did
not fully express the mobile lifecycle and onboarding sequence.

`docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md` declares
"**Depends on:** A4 scoped lifecycle and A9 membership/capability v2 physically closed;
A11 physically closed before a [draft role]". A4 and A9 are the exact direct A12 gate
prerequisites. A8 remains transitive through A9. A11 is not a blanket gate prerequisite.

`docs/superpowers/specs/2026-08-18-mycelium-a13-cross-platform-onboarding.md` declares
"implementation waits for A8, A9, and A12" and "**Depends on:** A8 Internet-native
bootstrap; A9 capability membership; A12 qualified platform activation levels". A13 is
downstream of A12, not a sibling of it, which makes A13 the last gate before A15.

**Resolved owner decision — A12 A11 scope (2026-08-18).** The owner resolves the former
§4.3/§12 inconsistency by making A4 and A9 the exact direct prerequisites for generic
A12 closure. A general Android or exact iOS/iPadOS activation claim may close after its
full A12 matrix with A4 and A9 closed while A11 remains incomplete. The optional
`speculative_draft_worker` claim retains a separate, explicit, fail-closed prerequisite:
A11 must be physically closed and bound to the exact target workload. Missing, stale,
disabled, or mismatched A11 authority rejects only that optional claim; it neither
blocks generic A12 closure nor becomes draft authority through an A12 gate state.

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
planner, and assigned artifacts can be acquired efficiently from the authorized swarm
without giving a peer unassigned model content.

Before production work, write and approve a closed
`mycelium.swarm_artifact_acquisition.v1` specification. Petals is an architectural
reference for decentralized block availability and capacity-aware serving, not evidence
that its model-weight acquisition is itself a BitTorrent protocol. Mycelium's extension
is an explicit content-addressed, multi-source acquisition protocol owned by the
Provisioner. Availability evidence never grants placement, changes an assignment, or
makes an artifact executable.

Implement A2 in this order:

1. **Authority and immutable artifact manifests.** Freeze the owner-approved model,
   revision, serving representation, assignment, tensor/component scope, and feasibility
   generation. Build assignment-local stage packs with fixed-size content chunks, a
   canonical Merkle/digest manifest, exact byte bounds, and signatures or equivalent
   authenticated owner provenance. Peers are never authorized for model layers or
   shared components outside their current assignment allow-list.
2. **Privacy-reduced availability discovery.** Authorized peers advertise immutable
   content/chunk digests, verified byte availability, freshness, serving limits, and
   transfer health. They do not advertise local paths, credentials, raw addresses, or
   model bytes. The Planner may consider availability and redundancy as cost evidence;
   only the Planner chooses placement, and only the Provisioner issues acquisition
   grants.
   Model and artifact discovery must be member-driven rather than coordinator-cache-
   fixed: a newly enrolled current member may add immutable identities and signed chunk
   availability to the private coordinator inventory. The UI consumes the reconciled
   aggregate, displays discovery scope and blockers, and never hard-codes a model or
   device count. Discovery alone never makes a model selectable.
3. **Swarm-assisted acquisition.** Fetch missing chunks concurrently from multiple
   authorized peers when at least two useful sources exist, with the operator-owned
   source/cache as fallback. Support resumable range/chunk transfer, bounded retry,
   source rotation, duplicate suppression, disk reservation, concurrent staging locks,
   corruption quarantine, and bandwidth/concurrency budgets. Prioritize the chunks on
   the route-readiness critical path; use rare-chunk preference only as a secondary
   redundancy policy. Below a measured size/source/link threshold, select a simpler
   single-source transfer to avoid coordination and hashing overhead.
4. **Verification and atomic promotion.** Verify each chunk before reuse, then verify
   the complete stage-pack digest, artifact size, model revision, representation digest,
   tensor ownership/scope, assignment digest, and feasibility generation. Only a fully
   verified pack is atomically promoted and eligible for load proof. Interrupted or
   corrupt partial state is never runtime-visible.

- Add resumable assignment-local transfers, corruption quarantine, bounded retry,
  concurrent staging locks, disk reservation, eviction policy, atomic promotion, and
  zero-duplicate warm reuse across both origin and peer sources.
- Require an owner authorization choosing an existing immutable representation or an
  explicitly named derived representation.
- Reject preparation when authorization, feasibility, builder output, assignment,
  stage pack, or graph representation digests differ.
- Recompute feasibility whenever representation, host set, evidence generation, or
  workload changes.
- Keep newly enrolled peers outside preparation until current capability and directed-
  link evidence includes them.
- Keep artifact acquisition subordinate to serving traffic: use separate bounded queues,
  bandwidth reservations, cancellation, and thermal/power policies so staging cannot
  congest activation links or starve an admitted inference request.
- Rebalance availability toward under-replicated or bottleneck stage packs only through
  explicit planner/provisioner intent. Cached content is evidence and a performance
  opportunity, never permission to auto-place a peer or silently replicate a model.

**Physical positive:** add one freshly assigned peer whose stage pack is obtained from
at least two existing authorized peers, with exact per-source/chunk byte accounting,
digest verification, no unassigned-layer transfer, and no duplicate origin fetch. Then
repeat the same preparation from a warm cache and transfer zero duplicate bytes while
loading the exact authorized representation. A serving-traffic A/B must show acquisition
budgets preserve the frozen activation latency/goodput envelope.

**Negative gate:** corrupt reuse, insufficient disk, representation drift, authorization
drift, stale feasibility, interrupted transfer, source disappearance, unauthorized or
unassigned chunk requests, replayed grants, manifest substitution, and concurrent staging
fail safely. An interrupted multi-source transfer resumes from another authorized peer;
if no source remains, it falls back to the approved origin or stays explicitly
unprepared. It never widens the assignment or downloads an unapproved representation.

**UI:** Models/Settings show representation, lifecycle, acquisition policy, and owner
authority; Nodes shows each placement's assigned layers, cached/missing/verified bytes,
source count, transfer rate, ETA, and artifact state; Plans shows allocation,
representation binding, artifact sources, redundancy, and origin fallback; Readiness
shows manifest, authorization, transfer, verification, and load blockers; Incidents
records source loss, resume, corruption, quarantine, fallback, and terminal acquisition
failure without private paths. Refresh/reconnect reconstructs progress and terminal
history from the Provisioner's live authority rather than browser-local state.

### A3 — Qualify useful local models

**Outcome:** Qwen2.5-7B and Qwen3-8B are honest catalog operations, with only physically
qualified representations selectable.

**Frozen specification:**
`docs/superpowers/specs/2026-08-15-mycelium-a3-useful-local-model-qualification.md`.
A3 remains the sole primary gate until its physical runtime, selector, live browser,
regression, handover, and atomic commit requirements pass. Deterministic parity and an
interrupted candidate preparation do not qualify, register, or select the 7B deployment.

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
- Complete the active exact 7B preparation without retroactively invalidating its
  valid-at-start authorization. Before any subsequent cold start, require a stable fast
  preparation root: prefer an internal SSD with the exact calculated footprint plus at
  least 20 GB operating headroom, otherwise a directly connected SSD or storage link
  negotiated at 5 Gbps or faster. A slower explicitly accepted recovery run remains
  visibly degraded and cannot hide its measured rate or ETA.
- Separate `representation_shard_verified` and `candidate_manifested` checkpoints from
  the bounded startup challenge so a process, storage, or lease interruption after
  materialization cannot force another full representation copy. Preserve the source
  checkpoint and owner authorization unchanged.
- Show preparation phase, current shard, completed/total/cached/transferred bytes,
  rolling rate, ETA, last progress, checkpoint, and lease scope in the Models/Settings,
  Plans, Readiness, and Incidents projections before A3 closes.
- After the cold candidate is published, execute exact warm reacquisition with fresh
  eligibility and prove one new zero-transfer terminal receipt per stage before
  activation. Recapture capacity and physical route health just in time for activation
  and qualification rather than rebuilding the completed immutable candidate.
- Capture memory-pressure A/B evidence first, then remove artificial pressure, connect
  participating laptops to stable power, prevent sleep for the bounded gate, and
  recapture clean capacity before the final serving allocation. Do not mutate the
  assignment of an in-flight preparation.

**Negative gate:** an infeasible, stale, incomplete, mismatched, unqualified, or dead
deployment cannot enter the selector.

**UI:** Inference model selector; Settings catalog/lifecycle; Plans allocation and
representation; Network proposed/selected/observed topology; Nodes layer ranges and
resident bytes; Readiness parity/qualification; Incidents preparation or load failure.

### A4 — Product-path concurrency, interruptible commands, and scoped liveness

**Outcome:** live traffic detects failure promptly without unnecessarily killing the
entire deployment.

**Frozen specification:**
`docs/superpowers/specs/2026-08-17-mycelium-a4-product-concurrency-liveness.md`.
It was written as dependency-ready design work while A3 awaited removable storage;
A4 remains `design_only` and no A4 production or completion claim may begin before the
separate atomic A3 close.

- Decompose the generation-long route-global lock into bounded per-request, per-session,
  per-stage, and authority locks.
- Replace the single dispatch thread with a bounded dispatcher/worker pool that preserves
  admission, QoS, ordering, backpressure, cancellation, and exact cleanup.
- Use cooperative, request-scoped cancellation at bounded prefill and decode work-unit
  boundaries. Every command consumes M16's canonical `PathManifest` identity and carries
  deployment epoch, qualification digest, request ID/attempt, path ID/digest/attempt,
  topology generation, command ID, cancellation generation, and publisher generation.
- A backend is A4-ineligible unless it proves both interruption and exact request-owned
  cleanup, backend release, and terminal publication within one original absolute
  2,000 ms deadline. No layer may restart that budget. Terminating a shared node process
  cannot satisfy request-scoped cancellation.
- Terminal and cleanup mutations use expected-revision, owner-scoped, generation-fenced
  compare-and-swap receipts. Cleanup is request/attempt/path-specific and must remain
  provable while unrelated requests are active; node-global zero counters are insufficient.
- Reconnect advances publisher generation through gateway, Router, physical command,
  event/cursor persistence, and all UI reducers. Late command results are discarded without
  failing unrelated waiters, stopping the response reader, or terminating a shared node.
- Integrate traffic-aware receipts, receipt suppression, idle keepalive, active failure,
  suspect/quarantine/recovery, and generation conflict into the live route.
- Replace route-global fatal latching with request/edge/placement/peer/deployment scope.
- A non-participating peer failure must not affect the active route. A participating peer
  affects only tracks containing it unless a deployment-wide invariant is actually lost.
- Enumerate and justify the few errors that remain deployment-fatal.
- Keep A5 physical execution and A8 shared integration/contract regeneration blocked until
  the atomic A4 close. Rebase and redesign A6/A7 after A4 rather than integrating their
  pre-A4 Router-owned generation and cleanup behavior unchanged.

**Physical positive:** measured active-disconnect and idle-staleness detection on a
browser request, including interruption before the old command timeout.

**Negative gate:** one missed receipt enters `suspect` only; a non-participating peer exit
leaves admission available; cleanup and immutable-path reservations remain exact.

**UI:** Inference waiting/interruption state; Network edge health; Nodes detector state;
Plans freshness; Readiness budgets; Incidents scoped detector source and action; Settings
detector policy.

### A5 — Multi-stage data-parallel stage replication

**Outcome:** Architecture section 4.8 is proven through the normal product path.

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md`.
It reconciles the earlier M18 baseline with A1–A4 authority and runtime boundaries while
physical peers are unavailable. A5 remains `design_only`; no planner fixture, written
benchmark, or historical whole-model replica run satisfies its product or physical gate.

- Re-specify replication as replicas of one or more contiguous stages inside a graph
  containing at least two pipeline stages.
- Planner produces complete legal request tracks, failure-domain constraints, flow
  assignment, marginal benefit, and zero-flow removal.
- Browser requests traverse the ordinary gateway, bounded dispatcher, Router port, and
  live stage runtimes; delete the qualification-only throughput seam.
- Compare primary-only and replicated configurations at identical offered concurrency
  and workload.
- Relabel the existing single-stage whole-model replica observation as historical whole-
  model replication or remove it from the architecture section 4.8 claim.

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

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a6-full-context-replay-recovery.md`.
It freezes gateway-owned committed-delivery authority, private prompt/token-prefix
replay, request-scoped cutover and generation fencing, independently qualified
successors, exact cleanup, explicit no-successor abort, and ordinary browser-path proof.
A6 remains `design_only`; a scripted failover or authored sealing record cannot satisfy
its continuity gate.

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

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a7-fenced-kv-successor-recovery.md`.
It permits no-replay continuation only from a route-wide acknowledged KV watermark that
exactly equals the gateway committed watermark, with byte-exact v1 backend/runtime/cache
compatibility. Every lag or mismatch falls back through qualified A6 or aborts honestly.
A7 remains `design_only`; standby capacity is not A5 replica eligibility.

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

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a8-internet-native-control.md`.
It was written as dependency-ready infrastructure design while A3 awaited its physical
peers. A8 remains `design_only`; no public listener, production integration, or completion
claim exists until its separate integration and physical gates execute.

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

### A9 — Platform-neutral capability and accelerated runtime efficiency

**Outcome:** eligibility derives from signed capabilities and class-specific
qualification, not device brands, and the ordinary product path has a physically
qualified accelerated Windows backend plus an efficient, component-aware distributed
execution baseline before A10 changes scheduling.

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md`.
It freezes separated platform/transport/runtime/capability authority, unknown-ineligible
handling, native-versus-synthetic truth, one-time identity-preserving migration without
eligibility carry-over, and class-specific qualification. A9 remains `design_only`.

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
- Implement and physically qualify at least one native accelerated Windows x86_64
  runtime provider for the exact Qwen2.5-0.5B serving representation. Portable NumPy
  remains a correctness/reference backend and cannot satisfy the accelerated Windows
  or performance-ready stage class by itself.
- Bind provider, device, driver/runtime build, kernel/precision support, model adapter,
  cancellation/cleanup behavior, thermal envelope, and evidence freshness. DirectML,
  ONNX Runtime, oneDNN, or another provider may be selected only through measured
  qualification; the product contract remains vendor-neutral.
- Prohibit decode-time whole-weight materialization for a performance-qualified int8
  backend. Loading may create bounded persistent packed/kernel-owned state, but each
  decode operation must prove it does not cast or expand the full int8 matrices again.
- Qualify float16 or bfloat16 activation transport where both adjacent stages support it,
  with exact output parity, finite-value, shape/dtype, cancellation, replay, and byte-
  reduction evidence. Unsupported edges stay on their qualified dtype; there is no
  silent coercion or precision downgrade.
- Measure compute, quantization/materialization, serialization, queue, transport, and
  result/token-selection time separately for prefill and decode. Record component costs
  for embeddings, decoder ranges, final normalization, and the vocabulary head; feed
  confidence-bounded component and edge costs into placement rather than treating equal
  layer counts as equal service cost.
- Freeze a same-hardware reference-versus-accelerated single-request benchmark before
  observing the candidate. For the exact two-host Qwen2.5-0.5B route, A9 requires output
  parity, stage-local KV, at least a 2x median decode-rate improvement whose paired 95%
  confidence lower bound exceeds 1.5x, median decode at or above 8 tokens/s, interactive
  p95 TTFT at or below 1,500 ms, and no cleanup/reliability regression. A documented
  miss keeps the Windows backend visible but performance-ineligible and blocks A10.

**Physical positive:** existing macOS/Linux/Windows members preserve identity and
re-enroll or upgrade across the version transition without gaining unearned eligibility;
the Windows member then completes the accelerated-backend, efficient-int8, activation-
precision, component-timing, planner, browser-inference, and single-request performance
gates through the ordinary product path.

**Negative gate:** unknown fields/classes fail closed; membership alone never grants
placement; claimed capabilities without class qualification remain ineligible; NumPy-
only, unaccelerated, silently dequantizing, falsely compressed, stale, thermally drifted,
or below-floor Windows candidates cannot satisfy performance-ready eligibility or unblock
A10.

**UI:** Nodes separately shows platform, transport, runtime/provider, capabilities,
membership, eligibility, precision, and measured component rates; Device Lab shows each
qualification gate; Network shows activation dtype/bytes and compute-versus-transport
timing; Plans shows component-aware predicted/observed placement and vocabulary-head
bottlenecks; Inference shows TTFT/TPOT/tokens-per-second and the qualified backend without
internal milestone labels; Readiness distinguishes member, correctness-only stage, and
performance-ready stage; Incidents and Settings expose bounded fallback reasons and only
qualified future-request policies.

### A10 — Real continuous batching, pipeline overlap, and target batch verification

**Outcome:** Architecture section 4.7 is physically complete and speculation has a real verifier.

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a10-runtime-batching-overlap-target-verification.md`.
It freezes actual runtime batch membership, continuous arrival, causal pipeline-overlap
evidence, bounded QoS/queues/slow-consumer behavior, transactional target verification,
and a same-route confidence-bound benchmark. A10 remains `design_only`.

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

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a11-target-authoritative-speculative-decoding.md`.
It freezes exact target/draft identity and separate KV, off-by-default preference,
adaptive deadlines and circuit breaking, target-authoritative commit/rollback/fallback,
desktop-first qualification, and a confidence-bound same-session benchmark. A11 remains
`design_only`; a neutral result produces a measured disabled decision.

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

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md`.
It freezes a generic native Android/Apple-mobile architecture, the four independent
eligibility rungs, two-family Android proof, foreground-only initial Apple eligibility,
and lifecycle/thermal/power/memory/network/artifact/parity gates. A12 remains
`design_only`; Termux, ADB, and SSH are development tools, not the user product path.

Eligibility progresses independently through:

1. signed member;
2. probe/evidence contributor;
3. speculative draft worker;
4. qualified model stage.

A12 directly depends on A4 and A9. Ordinary qualified mobile participation may close
without A11. The optional speculative-draft-worker rung remains visibly ineligible until
A11 physically closes and the exact mobile peer passes the additional draft gates.

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
or CLI knowledge. The product remains an open-ended private swarm rather than a fixed
demo inventory: authorized users can invite additional members, observe their evidence
and eligibility, and use newly qualified capacity without changing frontend code.

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a13-cross-platform-onboarding.md`.
It freezes target-owned identity, encrypted recipient-bound QR/deep-link handoff,
platform-signed packages, explicit consent, eligibility ladders, managed lifecycle, and
dynamic live-authority UI. A13 remains `design_only`; development tools, package
metadata, or a pre-enrolled device cannot satisfy its normal-user physical gate.

- Provide signed packages/apps for supported platforms and durable managed services where
  the OS permits them.
- Use a short-lived, single-use QR/deep-link invitation. The target device creates and
  retains its own identity.
- Show explicit consent: assigned peers may see assigned weights, activations, timing,
  and network metadata.
- Automate bootstrap reachability, identity creation, capability probe, lease renewal,
  reconnect, update, and revocation while retaining advanced diagnostics.
- Joining never modifies active Router state or deployment selection.
- Drive the entire join -> measure -> qualify -> acquire assigned artifacts -> load ->
  serve lifecycle from live authorities surfaced in the frontend. Do not encode a fixed
  member, model, stage, platform, or topology list in either the UI or coordinator.
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

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a14-route-explorer.md`.
It freezes coarse-region authority, an explicit unknown-region tray, independent path
and transport dimensions, accessible table/2D alternatives, reduced-motion/low-power
behavior, and a read-only projection boundary. A map or animation never becomes route,
qualification, or physical evidence.

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

**Frozen specification:**
`docs/superpowers/specs/2026-08-18-mycelium-a15-executed-release-closure.md`.
It replaces manual release booleans with a verified content-addressed result graph,
strict live/replay/fixture provenance, bounded signed exclusions, reproducible package
and SBOM bindings, a clean external-reviewer path, and fail-closed release revocation.
A15 cannot pass before current A3–A14 atomic commits and required executions validate.

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
- Seal exact exclusions. No public or complete-architecture claim survives an unapproved required
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

The synthesized architecture is complete only when:

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
8. assignment-local stage packs can be acquired from multiple authorized swarm peers,
   resume after source loss, reject corrupt or unauthorized chunks, and warm-reuse exact
   verified content without duplicate transfer or interference with admitted traffic;
9. speculative decoding is either physically beneficial under its frozen gate or visibly
   disabled with measured reasons;
10. all eight workspaces consume genuine live authorities and label sealed history;
11. the release decision is derived from executed artifacts rather than assertions;
12. a reviewer can add another supported device and select another locally discovered,
    physically qualified model through the frontend without editing a fixture, plan,
    source file, or hard-coded inventory; unsafe or oversized choices remain visible
    with their real blocker and cannot be forced into service;
13. every applicable long product operation survives bounded process, storage, network,
    peer, and lease interruption from its latest verified checkpoint, exposes truthful
    progress and recovery in the UI, and proves exact warm reuse without duplicate
    transfer.
14. before A10 changes scheduling, A9 physically qualifies one accelerated Windows
    x86_64 backend, proves persistent int8 execution without warm decode-time whole-weight
    materialization, qualifies reduced-precision activation transport, feeds component-
    and edge-specific observations including vocabulary-head cost into placement, and
    passes its frozen single-request TTFT/TPOT/tokens-per-second floor through the browser.

## 9. Separately reviewed future program

The following are not added by this plan: tensor parallelism, hybrid pipeline/tensor/data
parallelism, full-model data parallelism, cross-backend KV migration, prefix caching,
disaggregated prefill/decode, expert parallelism, and sequence parallelism. Each requires
its own approved architecture, correctness model, planner/runtime contracts, physical
positive and negative gates, UI truth boundary, and resource/failure analysis.
