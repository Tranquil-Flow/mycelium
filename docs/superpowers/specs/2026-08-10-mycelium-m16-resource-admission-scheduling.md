# Mycelium M16 Resource Admission, Scheduling, and Backpressure Specification

**Status:** qualified implementation
**Milestone:** M16
**Parent architecture:** `2026-08-09-mycelium-astra-architecture-product-design.md`

## 1. Outcome and boundary

M16 promotes the existing standalone Router reservation, progressive-path,
priority-aging, bounded-queue, and runtime-batch mechanisms into the qualified live
request path. A request is admitted only after every selected placement can reserve
its memory/KV/workspace requirement. One topology version is pinned per attempt and
decode may begin only after the complete reservation set is atomically committed into
one immutable `PathManifest`.

The physical three-device deployment remains a single legal track in M16. The live
scheduler may accept concurrent requests but the physical runtime may report
`sequential_dispatch` when no compatible runtime microbatch is observed. M16 never
calls queued sequential execution a microbatch and never claims continuous batching
or pipeline overlap without trace evidence. Replica tracks remain M17.

## 2. Authorities and compatibility

- Qualification remains the sole deployment/readiness admission authority.
- The M15 workload projection remains the allowlist for workload profile and QoS.
- The Router owns request-scoped resource reservations, path construction, path lock,
  scheduling, batching, cancellation, and cleanup.
- Node/runtime observations own free/reserved memory, queue depth, batch membership,
  and execution timings.
- The product gateway exposes privacy-reduced state only.

M16 adds new versions instead of widening frozen M12-M15 contracts:

1. `mycelium.request_gateway.v2` adds qualified `workload_profile_id` and `qos_class`
   to request submission. V1 remains readable and maps to the qualified interactive
   default only while compatibility is enabled.
2. `mycelium.request_event.v2` adds bounded lifecycle events for `admission`, `queue`,
   `prefill`, `first_token`, `decode`, and `completion`, without resource secrets or
   prompt/output in non-token events.
3. `mycelium.m16_runtime_status.v1` projects deployment-bound resource limits,
   reservation/path state, queue state, privacy-reduced request lifecycle, observed
   batch decisions, overload/backpressure incidents, and budget results.
4. `mycelium.performance_budget.v3` closes M15's admission-latency, queueing,
   concurrency, and batch-shape deferrals with observed bounds. V2 remains readable
   and unchanged.

All shapes are closed, bounded, finite, detached, and reject unknown protocols,
unknown fields, duplicate IDs, stale generations, private content, private paths or
addresses, prompt/response text, token IDs, activations, tensors, and KV content.

## 3. Resource ledger and reservation lifecycle

Each placement publishes immutable capacity limits for:

- fast memory available to the deployment;
- model/static bytes already assigned;
- request workspace bytes;
- KV bytes per context token and maximum reserved KV;
- maximum concurrent reservations and queue depth;
- optional power/thermal state with evidence freshness.

A tentative reservation binds request ID, deployment ID/epoch, graph digest,
topology version, path ID/attempt, stage/placement, workload/QoS, context/output
bounds, memory/KV/workspace bytes, reservation epoch, issue time, and expiry. The
admission controller reserves every hop or releases the complete partial set. A
reservation can be committed once. Duplicate identical operations are idempotent;
conflicting duplicates, stale attempts, expired leases, generation mismatch, and
out-of-order commit fail closed.

Cancel, timeout, admission rejection, path-build failure, runtime failure, and normal
completion release every request-owned resource. The ledger must prove that reserved
totals return to baseline and that no queue item, idempotency entry, runtime state, or
reservation remains reachable.

## 4. Progressive path and immutable decode

The Router evaluates complete legal continuations before selecting each progressive
prefill hop. Every decision consumes one coherent current device/load/link snapshot
and the attempt's pinned topology version. A later load update may affect another
request, but cannot mutate a locked path.

The final hop commits all reservations and emits the immutable `PathManifest` plus a
privacy-reduced digest. Decode and loopback follow that exact manifest. A topology or
deployment change after lock is visible as churn but does not reroute the request.
Only explicit M18 recovery may replace a path generation.

## 5. QoS scheduling, aging, batching, and backpressure

The live queue has hard item and byte limits. Interactive work has a higher base
priority; batch work accumulates bounded age/deficit priority so it cannot starve.
Priority ties are deterministic. A full queue rejects before physical dispatch with a
bounded retry hint and records an overload incident.

Only work with the same deployment epoch, model/manifest, placement, assignment,
stage signature, load proof, backend, phase, tensor shape, and speculative role may
share a runtime batch. The runtime owns the final batch decision and reports member
request IDs, phase, size, payload bytes, collection window, execution time, and result.
Absent physical batch evidence is reported as `sequential_dispatch`; it is not a
failed batch and does not satisfy the batch-shape budget.

Slow consumers are bounded at the gateway replay/SSE queue. Backpressure propagates
as a request-scoped public state or terminal overload outcome; it never creates an
unbounded token/event buffer.

## 6. Performance budgets

M16 freezes and evaluates at least:

- admission latency p50/p95;
- queue wait p50/p95 by QoS;
- maximum queue depth and bytes;
- minimum completed interactive and batch requests under concurrent load;
- maximum interactive latency regression versus the sequential M15 baseline;
- maximum batch starvation interval;
- required cancellation/resource-release latency;
- observed runtime batch-size range and membership integrity;
- backpressure rejection and retry-hint bounds.

Each dimension is `met`, `failed`, or `approved_exclusion`. Batch shape can be an
approved exclusion only when the physical runtime reports sequential dispatch; it
cannot be `met` from modeled or fixture evidence. A qualified M16 gate requires every
non-excluded dimension to pass and the exclusion to remain visible in UI and handover.

## 7. UI contract

- **Inference:** shows admission, queued, prefill, first-token, decode, cancelling,
  cleanup, and terminal phases; workload/QoS and locked path survive refresh.
- **Network:** shows progressive candidate hops separately from the immutable locked
  path and pins the topology version used by the request.
- **Nodes:** shows deployment capacity, assigned/reserved/free memory and KV, active
  reservations, queue depth/bounds, and freshness without private endpoints.
- **Incidents:** records overload, backpressure, reservation expiry, timeout, and
  cancellation cleanup with request/placement/deployment scope.
- **Plans/Readiness:** show M16 budget state, physical batching status, exclusions, and
  the exact authority withholding readiness.

All views derive from the same status generation. Navigation, refresh, reconnect, and
Back/Forward preserve the current privacy-reduced lifecycle and terminal history.

## 8. Verification gate

1. Contract RED tests precede implementation and cover closed shapes, privacy,
   duplicate/conflicting reservation operations, stale attempt/generation, expiry,
   out-of-order commit, partial cleanup, and bounded status history.
2. Pure/in-process Router tests prove complete-path reservation before dispatch,
   topology pinning, immutable decode, priority protection, batch aging, compatible
   microbatch membership, incompatible separation, queue/byte backpressure, slow
   consumer bounds, and cleanup after every terminal path.
3. Concurrent browser requests include both qualified profiles. Interactive work is
   selected ahead of already-queued batch work; aged batch work still completes.
4. Physical before/after evidence binds request IDs, paths, placements, reservations,
   phases, queue timings, frame counters, runtime batch or sequential-dispatch state,
   cancellation, and zero residual resources.
5. Plans, Inference, Network, Nodes, Incidents, and Readiness render the same M16
   generation, remain correct after refresh/reconnect, and make no M17 replica claim.

The M16 handover records the focused spec digest, contract manifest digest, RED and
GREEN commands, physical hosts/runtimes, request/path/reservation bindings, queue and
batch trace, budgets, before/after counters, browser routes, negative proofs,
approved exclusions, and the remaining M17 boundary.
