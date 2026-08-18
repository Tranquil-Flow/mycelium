# Mycelium A10 Runtime Batching, Pipeline Overlap, and Target Verification Specification

**Status:** `design_only`; dependency-ready acceptance boundary; production integration
waits for A4 closure

**Gate:** A10

**Parent:** `2026-08-11-mycelium-completion-plan.md`

**Supersedes for this gate:** the runtime-batch exclusion in M16 and the unqualified
batching notes in `docs/router-phase-aware-batching.md`

**Depends on:** A3 qualified useful target; A4 concurrent dispatcher, interruptible
commands, scoped liveness, bounded cleanup, and ordinary browser request path

**Architecture:** Synthesized architecture sections 4.4 and 4.7; provides the verifier prerequisite for
A11

## 1. Outcome and claim boundary

A10 makes real backend microbatching, continuous-arrival scheduling, and pipeline
microbatch overlap observable on the ordinary qualified product path. It also adds one
bounded target-authoritative multi-position verification operation whose output and KV
effects match sequential target execution.

The runtime, not the queue or planner, is the authority for actual batch membership.
Concurrent admission, adjacent trace spans, a batch-size setting, a loop over singleton
runtime calls, or multiple requests completing together is not runtime batching.
Pipeline overlap exists only when physical stage traces prove that different
microbatches execute simultaneously on different ordered stages while preserving all
per-request dependencies.

A10 does **not** add stage replication, split one matrix operation between devices,
move KV between placements, recover a failed request, enable speculative decoding, or
claim transport-frame coalescing. A11 alone may use the target verifier for a draft
overlay. The existing qualified target-only path remains the incumbent until A10 passes
independently.

Planning, fixtures, deterministic fakes, direct runtime calls, qualification-only
scripts, and sealed traces may test shape and semantics, but cannot satisfy any
performance, overlap, runtime-batch, target-verifier, browser, or physical claim.

## 2. Retained authorities and integration ownership

- The qualifier remains the sole readiness authority and the registry remains the sole
  future-request selection authority.
- A4 owns admission, worker lifetime, interruptible commands, liveness, request-scoped
  failure, and exactly-once terminal cleanup. A10 cannot restore a route-global lock or
  broaden a batch failure into deployment-fatal state.
- The M16/A4 resource ledger and immutable `PathManifest` remain authoritative. Every
  batch member has an independently committed complete-path reservation before runtime
  execution.
- The scheduler owns bounded queueing, QoS, aging, collection deadlines, and candidate
  grouping. The stage runtime owns final membership, actual backend batch execution,
  per-member results, and physical timing.
- Each target stage owns only its request-local KV. The multi-position verifier owns a
  temporary target-KV transaction until commit or rollback.
- The request gateway owns prompts, decoded output, private token traffic, and bounded
  browser streams. Public evidence contains none of that private material.

A10 may reuse the pure `PhaseAwareBatchController` only after its decisions are wired
to A4's non-blocking dispatcher and a production `RuntimePort.execute_batch()` that
performs one real backend batch. The existing sequential fake remains test-only and
must always report `sequential_dispatch`.

## 3. Closed capability contracts

A10 introduces capability-named, closed, bounded, canonically digested records:

1. `mycelium.runtime_batch_policy.v1` — qualified future-request policy: count/byte
   limits, phase windows, compatibility key, QoS/aging, latency guards, slow-consumer
   bounds, measurement profile, and rollback policy.
2. `mycelium.runtime_batch_observation.v1` — runtime-owned batch membership, phase,
   stage, backend invocation identity, collection and execution intervals, per-member
   outcome, physical counters, and cleanup result.
3. `mycelium.pipeline_overlap_trace.v1` — monotonic, stage-clock-correlated intervals
   and causal edges sufficient to prove or disprove overlap without payload content.
4. `mycelium.target_verification_capability.v1` — exact runtime/backend/model/
   representation support, maximum positions and bytes, generation policy, KV
   transaction semantics, cancellation points, and qualification binding.
5. `mycelium.target_verification_runtime.v1` — private request operation plus a
   privacy-reduced projection of prefix position, proposed-position count, verified
   count, accepted-prefix count, correction count, commit/rollback, timing, resources,
   and terminal cleanup.
6. `mycelium.runtime_batch_qualification.v1` — owner-private executed gate artifact
   binding policy, benchmark, parity, physical traces, browser evidence, negative gates,
   and regression results.

All cross-process shapes reject unknown fields/protocols, duplicates, non-finite or
negative values, excess counts/bytes, stale deployment or membership generation,
mixed model/representation/path identity, impossible intervals, invalid state
transitions, and mismatched canonical digests. Bounded public histories carry opaque or
pseudonymized request identities.

No public record contains prompt or output text, raw token IDs, logits, tensors,
activations, KV bytes, credentials, keys, raw EndpointIDs, addresses, usernames,
hostnames, private paths, command lines, or exception strings. Missing observations are
`unknown`/`null`, never zero or success.

## 4. Runtime batch compatibility and continuous arrivals

Two work items may be offered to the same runtime batch only when all of these match:

- deployment ID/epoch, qualification, model/revision/manifest/representation;
- graph, immutable path generation, placement, assignment, layer range, stage
  signature, load proof, backend, precision, and runtime incarnation;
- phase, hidden/activation shape, token span, position semantics, generation policy,
  and target-versus-verifier role; and
- resource class and any backend-specific layout needed for isolated per-member KV.

QoS need not match, but interactive deadlines always constrain collection. Missing
identity or compatibility evidence forces singleton execution; it never guesses a key.
The target runtime may reject any proposed member or split a candidate group into
smaller physical batches, and must report what it actually executed.

Continuous batching is iteration-boundary admission of newly compatible work into an
already active decode scheduler. Late arrivals do not mutate another request's
committed prefix or reorder its events. Each decode iteration selects a bounded set of
ready members, performs one true backend batch, scatters exactly one isolated result per
member, and then reevaluates new arrivals, cancellations, deadlines, output pressure,
and QoS.

The following bounds are mandatory policy inputs and never scale from peer count:

- maximum batch members and encoded bytes;
- maximum per-phase collection window and deadline guard;
- maximum queued members/bytes and active batches per placement;
- maximum runtime result bytes and per-member browser event bytes;
- maximum retained decisions, observations, intervals, and terminal history; and
- maximum consecutive interactive selections before an aged eligible batch item wins.

Queue overflow rejects before runtime dispatch with a bounded retry hint. A slow
consumer is detached from future scheduling when its bounded gateway buffer fills; its
request is cancelled or terminated by declared policy without blocking batch peers.
Backpressure cannot create unbounded stage output, transport, SSE, replay, or evidence
buffers.

## 5. QoS, cancellation, failure, and KV isolation

Interactive work has a lower collection delay and higher base priority. Batch work
accumulates bounded aging/deficit so it cannot starve. Ties are deterministic and every
decision records candidates, exclusions, selected membership, reason, and deadline
without private payloads.

Every member has separate request, attempt, path, reservation, stream, event sequence,
and stage-local KV identity. Backend packing may share temporary tensor storage, but no
member may address, retain, commit, or roll back another member's KV. Padding and shape
metadata cannot become generated positions.

Cancellation is checked before collection, before backend launch, after backend return,
before forwarding, and at A4 command cancellation points. A member cancelled before
launch is removed. If the backend cannot remove a member after launch, its bounded
computation may finish but its result is discarded and only that member's KV transaction
is rolled back and cleaned. Remaining members continue and receive exactly one result.

A per-member validation or forwarding failure fails only that member. A shared backend
failure may fail the members of that invocation, but does not poison unrelated batches,
queues, paths, or the deployment unless it independently satisfies A4's fatal allowlist.
Wrong result count/order, cross-member identity, partial KV commit, non-finite output,
or stale incarnation fails closed and rolls every uncommitted member of that invocation
back to its previously committed prefix.

Timeout, overflow, slow consumer, cancellation, worker exit, runtime exit, partial
batch failure, and shutdown must return queue entries, reservations, command handles,
temporary packed buffers, target-KV transactions, receipts, and gateway buffers to the
measured baseline within A4's cleanup budgets.

## 6. Pipeline overlap semantics

Pipeline overlap uses multiple request microbatches on one qualified ordered stage
graph. For a request and decode position, stage `n + 1` begins only after stage `n`
produces the bound activation. The final-to-entry loopback for the next token begins
only after the target-owned final result for the current token commits.

An overlap trace records synchronized monotonic intervals for queue, runtime compute,
send, receive, and bounded wait at each placement; request/microbatch identity; stage
and phase; predecessor/successor causal edges; and clock-correlation uncertainty.
Overlap is `observed` only when, after accounting for timestamp uncertainty, at least
two physical stage-compute intervals for different microbatches intersect for a
positive duration. A trace that only overlaps queueing, serialization, or UI animation
does not pass.

The trace validator rejects reversed intervals, missing causal predecessors, same-
request dependency overlap, impossible clocks, duplicated stage application, skipped
stages, activation/attempt mismatch, and final-loopback before commit. Each request
still follows one immutable complete path; overlap never splices placements from
different paths.

## 7. Multi-position target-verification operation

The verifier is a target-runtime operation, not a planner flag. It is advertised only
after the exact selected target runtime physically passes this gate. A script,
configuration field, or draft-model compatibility claim cannot advertise it.

The private operation binds deployment/qualification, target model/revision/
representation, graph/path/attempt, placement and runtime incarnation, request and
generation policy, target-KV identity, committed prefix position and digest, bounded
candidate-position count and bytes, deadline, cancellation generation, reservation,
and idempotency digest. Raw candidate token IDs stay only on the authenticated private
runtime path.

Execution creates a target-owned transaction from the exact committed prefix. One real
backend call evaluates all bounded candidate positions with causal target semantics and
returns the target-authoritative per-position decisions needed to compare with
sequential execution. The operation may then:

- roll back completely to the supplied prefix;
- commit a declared verified prefix; or
- commit the verified prefix plus one target-authoritative correction position.

Commit is monotonic, idempotent for the identical operation digest, and atomic for the
declared position range. Conflicting duplicate, stale prefix, excess width, wrong
generation policy, cancellation, timeout, backend error, or parity failure rolls back
to the original prefix. Neither tentative verifier KV nor uncommitted results may enter
ordinary decode, browser output, or another request.

The gate initially qualifies deterministic greedy target verification. Sampling remains
unsupported unless a later gate binds the exact target sampler, seed/counter state,
distribution transformation, and sequential parity. Unsupported generation policies
fail admission; they never silently switch to greedy.

## 8. Frozen benchmark and materiality rule

Before any candidate measurement, an immutable benchmark manifest binds target,
representation, route/session/generation, stage runtimes, workload inputs, offered
arrival schedule, prompt/output-length buckets, QoS mix, concurrency, token limits,
batch bounds, warmup, sample exclusions, and software/configuration digests.

The A10 benchmark uses three paired repetitions with alternating pair order: `AB`,
`BA`, `AB` (six measured windows total), after one unscored warmup per mode. Here `A`
is the target-only baseline and `B` is the batching/overlap candidate. Each measured
window contains at least 60 completed requests, including at least 20 interactive
requests, at least 20 late arrivals after decode has begun, and at least two prompt-
length and two output-length buckets. Failed, cancelled, overflowed, and slow-consumer
requests remain in reliability/latency accounting and are never silently dropped from
the denominator.

The primary material threshold is a **10% increase in completed generated tokens per
wall-clock second**, with the paired 95% bootstrap lower confidence bound also at or
above 10%. The candidate must simultaneously satisfy:

- exact output parity against target-only execution for every completed request;
- no more than 10% regression in interactive p95 TTFT;
- no more than 10% regression in interactive p95 TPOT;
- no regression in terminal error, timeout, cancellation, or cleanup success rate;
- bounded batch starvation and queue/byte limits; and
- zero residual resources and zero cross-request KV/result contamination.

The same target route, product sessions, workload, arrival schedule, token limits,
measurement window, and instrumentation are used for baseline and candidate. Only the
declared batching/overlap policy changes. Route churn, thermal/power-limit drift, stale
evidence, incompatible software, or missing measurements invalidates the pair rather
than becoming a zero. Confidence is computed over paired windows, not individual
tokens. No post-hoc threshold or exclusion change is allowed.

The target verifier has a separate correctness gate: at least 100 bounded operations
covering widths `1`, the qualified default, and the maximum qualified width across at
least two prefix-length buckets. Every per-position target decision and final committed
KV position must match sequential target execution. Verifier speed is reported but is
not an A10 materiality requirement.

## 9. Eight-workspace product contract

Product copy uses human capability names and contains no internal A/M labels.

- **Inference:** real batch ID/size and per-request position, QoS, queue/collection
  reason, active/slow-consumer/cancel state, target-verification use, and immutable
  terminal attribution without exposing peer prompts or tokens.
- **Device Lab:** backend batch and verifier capability limits, parity/cleanup gates,
  incarnation, and qualification freshness; synthetic workers are visibly non-
  qualifying.
- **Network:** physical per-stage batch flow and uncertainty-aware overlap trace,
  separated from modeled flow, queueing, and transport-only concurrency.
- **Nodes:** runtime batch mode, actual size/bytes, active limits, queue/reservation/KV
  occupancy, stage utilization, verifier support, and freshness.
- **Plans:** modeled batch choice beside observed membership, workload assumptions,
  exclusions, bottleneck, predicted/observed gain, and baseline comparison.
- **Readiness:** independent backend-batch, continuous-arrival, overlap-causality,
  parity, verifier, latency, throughput, cleanup, and freshness proofs.
- **Incidents:** overflow, starvation, slow consumer, member cancellation, partial
  batch failure, verifier rollback, stale result, and bounded cleanup with narrow scope.
- **Settings:** qualified future-request QoS, batch limits/windows, slow-consumer policy,
  and target-verification limits. Unsafe or unqualified values are disabled with reason.

All views consume one current public generation and distinguish live, stale,
historical, fixture, modeled, unknown, disabled, and failed state. Direct navigation,
refresh, workspace switching, Back/Forward, gateway reconnect, bounded terminal
history, and a clean second session reconstruct the same public evidence while keeping
tab-private prompt/output isolated. Keyboard, responsive layout, accessible names, and
reduced motion are required.

## 10. Verification matrix

The closed machine-checked acceptance inventory is
`tests/a10_acceptance/scenarios.v1.json`. It freezes runtime-batch truth, verifier and
benchmark constants, negative gates, cleanup/privacy boundaries, and workspace
mappings. Passing `tests/a10_acceptance/test_scenario_manifest.py` proves only that the
acceptance inputs remain closed and complete. It does not satisfy a product, runtime,
physical, overlap, verifier, performance, browser, qualification, or completion claim.

| Acceptance family | Required inventory scenarios |
| --- | --- |
| Runtime batch truth and continuous arrivals | `real_runtime_batch_membership`, `continuous_late_arrival` |
| Causal physical overlap | `causal_pipeline_overlap`, `overlap_trace_rejection` |
| Bounded queues, QoS, and consumer isolation | `bounded_queue_qos_aging`, `slow_consumer_isolation` |
| Cancellation and invocation failure | `cancellation_boundary_isolation`, `batch_fail_closed_rollback` |
| Member-local KV semantics | `cross_member_kv_isolation` |
| Transactional target verification | `target_verifier_transaction`, `target_verifier_rollback_to_prefix`, `target_verifier_sampling_rejection` |
| Same-route materiality | `same_route_confidence_benchmark`, `invalid_benchmark_pair` |
| Exit, shutdown, and exact cleanup | `runtime_exit_shutdown_cleanup` |
| Browser reconstruction and privacy | `all_workspace_reconstruction_privacy` |

### Contract and deterministic gates

- Closed-shape, bound, digest, privacy, unknown-field, mixed-generation, duplicate,
  stale-prefix, illegal-transition, invalid-timing, and unbounded-history tests for all
  contracts.
- Deterministic scheduler tests cover compatibility, continuous late arrival, QoS and
  aging, deadlines, count/byte limits, backpressure, slow consumers, stable ties,
  singleton fallback, and actual-versus-proposed membership.
- Runtime tests use a real batch-capable backend for integration claims and cover
  scatter order, per-member identity, shape/padding isolation, KV independence,
  cancellation at every boundary, partial/shared failure, stale incarnation, wrong
  result count, and exact cleanup.
- Trace tests prove valid pipeline overlap and reject causal, clock, stage, request,
  path, and loopback violations.
- Verifier tests cover every commit mode, sequential parity, prefix/position bounds,
  idempotent and conflicting duplicates, rollback, cancellation, timeout, sampling
  rejection, and resource release.

### Physical positive gates

1. At least two clean browser sessions submit the frozen concurrent and late-arrival
   workload through the ordinary gateway on one qualified multi-stage route. Physical
   runtimes execute true batches larger than one and record exact per-member work.
2. Trace evidence proves continuous admission and positive pipeline stage-compute
   overlap for different microbatches without causal violation.
3. The same-route paired benchmark passes the frozen throughput, confidence, TTFT,
   TPOT, correctness, reliability, and cleanup thresholds.
4. One exact target runtime performs the multi-position verifier matrix through the
   product dispatcher and matches sequential target authority and KV state.

### Physical negative gates

- Cancellation before collection, after collection, during backend execution, before
  forwarding, and during verification affects only the owning request.
- Timeout, queue/byte overflow, slow consumer, incompatible member, stale prefix,
  partial member failure, shared backend failure, wrong result count, runtime exit,
  shutdown, and verifier rollback all fail within their declared scope and leave zero
  residue.
- A sequential loop, concurrent admission, modeled schedule, fixture trace, direct
  method call, qualification script, or UI-authored batch state cannot satisfy a
  runtime batch, overlap, verifier, or performance gate.

### Browser and regression gates

All eight live workspaces pass direct navigation, refresh, Back/Forward, workspace
switching, reconnect, stale/degraded source, live batch progress, cancellation,
terminal history, and independent second-session checks. Accessibility, privacy,
contracts, governance, release security, frontend, full Python, dispatcher, liveness,
reservation, stage-runtime, transport, and cleanup suites pass after the executed
evidence shape is stable.

## 11. Completion and next boundary

The owner-private qualification bundle binds source/specification digests; exact target
and representation; route/session/workload; policy and limits; baseline/candidate
samples; actual batch membership; runtime and Router counters; overlap and causal
traces; target-verifier operations and sequential oracle; cancellations, failures,
rollbacks, and cleanup; the acceptance-inventory digest; all-eight browser results; and
regression/audit output.

A10 becomes `physically_qualified`, then `registered`/`selected`/`observed` only through
their existing authorities. It is complete only after the ordinary browser product
path passes every physical positive and negative gate, the all-eight UI verification,
and one atomic A10 feature commit. Until then it remains `design_only` or the narrower
truthful intermediate state.

A10 advertises the target verifier capability only for the exact qualified target
runtime binding. It does not enable speculation. A11 must independently bind a draft,
prove target parity and rollback/fallback, measure end-to-end benefit on the same route,
and remain off by default.
