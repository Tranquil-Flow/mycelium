# Mycelium A5 Multi-Stage Data-Parallel Replication Specification

**Status:** approved design boundary; implementation waits for A4 completion
**Gate:** A5
**Parent:** `2026-08-11-mycelium-astra-completion-plan.md`
**Supersedes for this gate:** `2026-08-11-mycelium-m18-replicated-throughput.md`
**Depends on:** A1 product authority; A2 assignment-local acquisition; A3 qualified
model; A4 concurrent dispatcher, interruptible commands, scoped liveness, and cleanup
**Architecture:** Astra section 4.8

## 1. Outcome and claim boundary

A5 adds request-level data-parallel replicas to a real model graph containing at least
two ordered pipeline stages. A replica is another qualified placement of one complete
contiguous stage range. Different requests may use different complete legal tracks;
one request remains pinned to exactly one track and keeps its stage-local KV there.

This is not whole-model data parallelism, tensor parallelism, a split matrix operation,
pipeline microbatch overlap, speculative decoding, in-flight migration, replay, or KV
successor recovery. Those names never appear as A5 claims. Historical single-stage
whole-model replica evidence is not accepted for this gate.

A5 may be planned and tested deterministically while later physical peers are absent,
but it remains `design_only` until the ordinary product gateway physically executes the
positive and negative gates. A fixture, planner-only result, qualification-only harness,
synthetic worker, concurrent count without runtime proof, or written throughput estimate
cannot promote a replica plan.

## 2. Frozen authority separation

The existing owner boundaries remain unchanged:

- **Planner:** proposes replica groups, placements, complete tracks, workload flow,
  marginal benefit, failure-domain constraints, and rejection reasons. It never reports
  a route ready or a replica qualified.
- **Provisioner:** issues assignment-scoped artifact grants, verifies stage packs, and
  atomically promotes only the exact replica placement artifact.
- **Runtime/loader:** owns placement-specific load proof and startup challenge.
- **Qualifier:** binds current resource/link evidence, artifact/load proof, graph,
  complete tracks, parity, workload envelope, and physical challenge. Only it may mark a
  replica track qualified.
- **Dispatcher/Router:** atomically admits each request to one currently qualified track,
  owns reservations and per-request work attribution, and applies A4 interruption,
  liveness, and exact cleanup semantics.
- **Observatory/UI:** projects bounded privacy-reduced records and cannot create a
  placement, track, flow, failure domain, readiness rung, or performance claim.

Selection of the base deployment does not select or promote a replica plan. Membership,
artifact presence, a load proof, a planner proposal, or another replica's qualification
does not make a placement usable. Replica activation cannot displace the incumbent until
the canary and rollback gates pass.

## 3. Immutable replica and track identity

A replica placement binds:

- swarm, deployment, epoch, model, immutable revision, serving representation,
  representation manifest, and base qualification;
- replica-group and placement IDs, node principal/incarnation, exact half-open layer
  range, component roles, assignment digest, stage-pack digest, load proof, runtime
  build, backend, and decode mode;
- context/concurrency envelope, static/resident/KV/workspace memory bounds, storage,
  power/thermal limits, evidence generation, and failure-domain facts; and
- every directed ingress, egress, local, and decode-closure edge used by a track.

All placements in one replica group own exactly the same layer range and component
roles. Ordered groups cover the model once without gaps or overlaps and contain at
least two pipeline stages. At least one group has two qualified placements for a
positive A5 gate.

A legal track selects exactly one placement from every ordered group. Its stage ranges
cover the graph once, all directed forward edges exist, and its decode mode's final-to-
first closure edge exists. The track digest binds the ordered placement and edge
identities. Mixed revisions, representations, epochs, evidence generations, decode
modes, component roles, or unqualified placements make a track illegal.

An admitted request captures the exact deployment, plan, track, placement sequence,
edge set, qualifier, workload, and authority generation. Prefill and every decode step
use that track. KV is keyed by request, attempt, track, placement, layer range,
deployment epoch, and generation. A5 never changes track or copies KV after admission.

## 4. Deterministic placement and workload flow

Planning starts from one qualified primary multi-stage track and a bounded declared
workload. Candidates are enumerated in stable identity order and accepted iteratively
only when they improve the robust bottleneck objective after uncertainty, latency,
fairness, failure-domain, artifact, and resource costs.

Every accepted placement must:

- fit current static, KV, workspace, storage, backend, representation, thermal, power,
  and concurrency bounds without borrowing another placement's reservation;
- participate in at least one complete legal track with positive workload flow;
- preserve declared TTFT, TPOT, queue, fairness, and reservation budgets;
- obey per-node, per-group, total-replica, artifact-transfer, and failure-domain bounds;
  and
- improve robust admitted goodput by more than the frozen material-gain threshold.

The flow solver consumes measured placement service rates and directed-edge capacities.
It reports requested, admitted, and unmet demand; complete tracks; traffic fractions;
before/after bottlenecks; uncertainty; candidate marginal gain; and every rejection.
Fractions are bounded, finite, deterministic, and sum to one within a fixed tolerance.
They allocate requests across tracks; they never split one request across tracks.

A non-primary placement with zero final flow is removed from the candidate and cannot
be provisioned or rendered active. Unknown failure-domain information stays unknown and
cannot support a diversity claim. A replica on the same physical host or correlated
domain may provide measured capacity, but is not described as failure isolation.

## 5. Provisioning, activation, and ordinary product dispatch

Only accepted placements receive A2 assignment-local acquisition grants. Artifact
scope is the exact stage range and shared components required by the representation.
Warm reuse follows the same digest and receipt rules; cached data grants no placement
authority. Each placement independently passes artifact verification, load proof,
startup challenge, parity, memory, cleanup, and current directed-link qualification.

The A4 dispatcher holds no generation-long route-global lock. Admission chooses one
qualified track under bounded authority and reservation locks, then releases admission
state before execution. A request owns only its queue entry, track reservation, stage
commands, Router streams, receipts, and KV. Cancellation, timeout, transport failure,
worker exit, and shutdown use A4's narrow failure scopes and idempotent cleanup.

Scheduling follows the qualified traffic fractions subject to current track capacity,
QoS, aging, and backpressure. It never routes around an unqualified or saturated track
by constructing a new placement combination. If every legal track is full, admission
queues or fails with the bounded product reason; it does not overcommit.

Per-request runtime evidence binds track ID and every placement that applied work.
Per-placement counters distinguish admitted requests, prefill/decode work, frames,
queue/reservation occupancy, memory/KV ownership, cancellation, cleanup, and health.
Runtime batch mode is reported from the runtime and is not inferred from overlapping
requests.

## 6. Degradation and successor boundary

Removing, draining, or losing a replica immediately blocks new admission to tracks that
contain it. Complete surviving qualified tracks remain admissible with their newly
measured capacity. Losing a non-participating replica cannot fail an active request.
Losing a placement on an admitted track terminates that request under A4 unless a later
A6 recovery capability is separately qualified.

A5 performs no in-flight migration, replay, KV handoff, or automatic successor claim.
The planner may publish candidate intent for a future generation, but provisioning,
load proof, qualification, epoch fencing, and stable activation must finish before new
admission. Old and new generations remain distinct in evidence and the UI.

Replica-plan failure never poisons the base deployment. Corrupt artifacts, stale
evidence, illegal tracks, failed challenge, neutral throughput, or regression roll back
the candidate and preserve the incumbent selector and admissions.

## 7. Closed product contracts

A5 uses capability-named, closed, bounded, canonically digest-bound records:

1. `mycelium.replica_plan.v1` — immutable base binding, groups, candidates, complete
   tracks, directed edges, workload flow, predicted gain, uncertainty, failure-domain
   facts, zero-flow removals, and rejections. It always reports `route_ready=false`.
2. `mycelium.replica_qualification.v1` — exact placement/track lifecycle authorities,
   current evidence, parity, resource and physical challenge results, workload envelope,
   and expiry.
3. `mycelium.replica_runtime.v1` — admitted request-to-track bindings, per-placement
   work, queues, reservations, frames, health, cleanup, and observed performance.
4. `mycelium.replica_benchmark.v1` — frozen primary-only and replicated workloads,
   samples, throughput/latency/fairness/resource results, prediction error, material-gain
   decision, and provenance.

If implementation shows an existing version cannot represent these closed shapes
without ambiguity, it is replaced rather than kept as a compatibility layer. Projection
records contain no prompts, output text, token IDs, logits, activations, KV contents,
credentials, private paths, raw addresses, or unbounded traces.

## 8. Eight-workspace product behavior

Product copy uses capability language and contains no internal milestone labels.

- **Inference:** exact immutable request track, placement sequence, queue state, progress,
  cancellation, terminal result, and history labelled request-level stage replication.
- **Device Lab:** capability and qualification requirements for replica placements;
  synthetic browser workers remain visibly ineligible for model stages.
- **Network:** primary, candidate, qualified, active, surviving, and failed tracks;
  directed forward/closure edges and per-request physical trace remain separable.
- **Nodes:** replica group/range, primary or replica role, artifact/load/qualification,
  capacity, reservations, request count, applied work, traffic share, and health.
- **Plans:** candidates, legal tracks, traffic fractions, predicted/measured marginal
  gain, bottlenecks, uncertainty, failure-domain facts, zero-flow removals, and reasons.
- **Readiness:** independent artifact, load, graph, edge, parity, resource, cleanup,
  qualification, benchmark, and promotion rungs per placement and track.
- **Incidents:** artifact/load/qualification failure, saturation, replica loss, drain,
  zero-flow removal, rollback, affected tracks/requests, narrow action, and outcome.
- **Settings:** bounded replica count, per-node placement, material-gain, failure-domain,
  QoS, queue, and benchmark policy. Changes affect future plans, not admitted requests.

All workspaces consume one current public generation and truthfully render stale,
degraded, unavailable, unknown, and historical state. Refresh, direct navigation,
Back/Forward, reconnect, terminal history, and a clean second browser session reconstruct
the same public plan and runtime evidence. Tab-private prompt/output remains isolated.

## 9. Frozen benchmark materiality protocol

Before any A5 candidate measurement is inspected, the immutable
`tests/a5_acceptance/benchmark_protocol.v1.json` manifest freezes the metric,
threshold, order, pairing, confidence calculation, warmup, minimum window size,
bindings, invalidations, and decision vocabulary. Changing that manifest creates a new
benchmark protocol and invalidates measurements captured under the earlier digest.

The primary metric is completed generated tokens per wall-clock second. For each paired
baseline/replicated window, fractional improvement is
`(replicated_rate / baseline_rate) - 1`. The point estimate is the arithmetic mean of
the six paired improvements. Material gain requires both the point estimate and the
paired 95% bootstrap lower confidence bound to be at least **10%**.

The measured order is exactly `ABBA` repeated three times: 12 measured windows forming
six explicit baseline/replicated pairs. It follows exactly one unscored full warmup
window per mode. Every warmup and measured window contains at least 60 terminal requests.
Completed, error, timeout, cancellation, rejected-admission, and cleanup-
failure outcomes remain in the window accounting; only completed generated tokens
contribute to the throughput numerator. No outcome is silently excluded.

Confidence uses a deterministic paired non-parametric bootstrap over the six fractional
improvements: 10,000 resamples of six pairs with replacement, seed `0xA5`, arithmetic-
mean statistic, and the nearest-rank ceiling at the 2.5th percentile. It never
resamples tokens or individual requests. This calculation is machine-checked by the
test-only A5 acceptance harness and is not runtime or qualification evidence.

All warmup and measured windows bind the identical model, immutable revision,
representation, base deployment, route generation, workload, offered-arrival schedule,
prompt/output buckets, QoS mix, concurrency, token limits, product benchmark session,
software, configuration, and instrumentation. Only the declared mode and qualified
track-policy binding may differ. Server restart, route reselection, workload or session
change, missing/duplicate/reordered window, fewer than 60 requests, incomplete outcome
accounting, stale authority, member/runtime incarnation change, thermal or power drift,
software/configuration/instrumentation drift, non-finite measurement, post-hoc
exclusion, or any threshold/workload change invalidates the benchmark instead of
becoming a zero-valued sample.

The frozen decision vocabulary is deliberately honest:

- `material` only when the valid point estimate and paired 95% lower bound are both at
  least 10%;
- `not_material` when a valid point estimate is below 10%; and
- `inconclusive` when the point estimate reaches 10% but the lower bound does not, or
  when any required input or binding is missing, invalid, or invalidated.

Every result retains its reasons and reports `qualification_claim=false` and
`promotion_authorized=false`. A `material` test-harness result freezes acceptance math;
it does not satisfy the physical gate or change A5 from `design_only`.

## 10. Verification matrix

### Contract and deterministic gates

- Closed-shape, count/size, canonical digest, privacy, unknown-field, duplicate identity,
  mixed generation, stale authority, and illegal transition tests for every contract.
- Planner tests cover exact multi-stage ranges, legal-track enumeration, missing/reversed
  edges, stable ties, finite bounded flow, resource fit, positive/neutral/negative gain,
  uncertainty, failure-domain warnings, zero-flow removal, and no per-request splitting.
- Lifecycle tests cover assignment-local acquisition, warm reuse, independent load and
  qualification, partial-candidate rollback, incumbent preservation, generation
  fencing, and stale/mismatched placement rejection.
- Dispatcher interleavings cover admission/cancel, saturation/release, replica loss,
  non-participating loss, shutdown, projection during mutation, and exact cleanup with
  no route-global serialization or cross-request ownership leak.

### Physical positive gates

1. Start from one qualified model graph with at least two physical pipeline stages.
2. Provision and qualify at least one replica of a contiguous stage on another eligible
   physical placement, yielding at least two complete legal qualified tracks.
3. Through the ordinary browser gateway, overlap at least two requests and prove that
   distinct requests use distinct complete tracks with exact per-placement work,
   Router-frame movement, parity, stage-local KV, and zero cleanup delta.
4. Compare primary-only and replicated configurations using the same model,
   representation, workload, offered concurrency, sessions, route generation, and
   measurement method. Replication must exceed the predeclared material throughput
   threshold without violating correctness, latency, fairness, memory, or cleanup.

### Physical negative gates

- Remove or fail one replica and prove affected tracks reject new admission while a
  complete surviving track remains usable at truthfully reduced capacity.
- A request using the lost placement terminates explicitly without migration or
  continuity claim; an unaffected admitted request completes.
- Exercise illegal range, incomplete/reversed track, stale evidence, mixed identity,
  unqualified placement, saturation, zero-flow candidate, artifact/load failure, and
  neutral benchmark. Each fails closed and preserves the incumbent.

### Browser and regression gates

All eight live workspaces are checked through concurrent submission, cancellation,
replica degradation, direct navigation, refresh, Back/Forward, reconnect, terminal
history, and a clean second session. Accessibility, privacy, contracts, governance,
release security, frontend, full Python, acquisition, dispatcher, liveness, and cleanup
suites pass after the evidence shape stabilizes.

## 11. Completion

The owner-private A5 qualification bundle binds source/specification digests; base and
replica identities; assignments and artifacts; load and qualification proofs; graph,
groups, complete tracks, flow, failure-domain facts, and workload; browser request/track
bindings; per-placement work and frame counters; stage-local KV and cleanup facts;
primary-only and replicated benchmark samples; replica-loss negative results; UI checks;
and regression/audit outputs.

A5 is complete only when a multi-stage physical graph serves concurrent ordinary
browser requests on distinct qualified legal tracks, produces material measured gain,
survives the scoped replica-loss gate, passes all-eight-workspace verification, and is
committed in one atomic A5 feature commit. Until then it remains `design_only`.
