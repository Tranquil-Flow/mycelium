# Mycelium M18 Replicated Throughput Specification

**Status:** implementation baseline
**Milestone:** M18
**Parent architecture:** `2026-08-09-mycelium-astra-architecture-product-design.md`

## 1. Outcome

M18 turns the existing planner replication primitives into qualified, physically
measured data-parallel stage replication. A replica owns the same complete contiguous
layer range and component roles as its replica group. Requests are assigned to complete
legal tracks, remain track-pinned for their lifetime, and retain KV only on that track.

This is request-level data parallelism. It is not tensor parallelism, layer splitting
within a stage, speculative decoding, recovery, or migration of an in-flight KV cache.
M18 starts only from an M17-qualified immutable model representation and deployment.

## 2. Authorities and closed projections

The Planner owns `mycelium.replica_plan.v1`. It binds the primary deployment and
qualification, workload, signed resource/link evidence, immutable replica groups,
candidate placements, legal directed edges, complete tracks, flow allocation,
rejections, and predicted gain. It always reports `route_ready=false`.

The provisioner and runtime retain their M17 authorities for assignment-local artifact
integrity and load proofs. The qualifier alone promotes a replica track after every
placement and edge has current evidence and an independent distributed challenge.

The Router owns `mycelium.replica_runtime.v1`: admitted request-to-track bindings,
per-placement work, queue and reservation state, traffic counters, and track health.
Observatory projects those records read-only and never invents replicas, tracks, flow,
readiness, failure domains, or tensor-parallel claims.

Both projections are closed, bounded, privacy-reduced, digest-bound, and contain no
prompts, output text, token IDs, activation payloads, KV contents, credentials, private
cache paths, or raw network addresses.

## 3. Replica identity and eligibility

A replica placement binds:

- deployment ID/epoch, model ID, immutable revision, representation, manifest, and
  primary qualification;
- replica-group ID, placement ID, node principal/incarnation, exact half-open layer
  range, component roles, assignment, stage pack, load proof, and runtime build;
- backend, quantization, decode mode, context/concurrency envelope, and failure-domain
  labels supported by signed evidence;
- the resource and link evidence generation used for admission.

All placements in one group have identical layer and component ownership. A node may
host at most one placement for a request's track and cannot host two adjacent placements
on the same track unless the plan explicitly models the local edge. Replicas require
fresh M17 feasibility, assignment-local artifact acquisition, load proof, and stage
challenge. Membership, artifact presence, or a planner proposal never implies replica
readiness.

Failure-domain labels are evidence, not host-name inference. Unknown remains unknown
and produces a warning; the planner may not claim correlated-failure tolerance without
distinct supplied domains.

## 4. Legal tracks and KV locality

A legal track selects exactly one placement from every ordered replica group, covers
the model once without gaps or overlap, has every directed forward edge, and has the
final-to-first decode closure required by its decode mode. Track identity is the digest
of the ordered placement identities and their bound edges.

Admission atomically captures one qualified track with the existing model, deployment,
qualification, topology, assignment, and workload binding. Prefill and every decode
step use that exact track. Stage-local KV is keyed by request, track, placement,
deployment epoch, and generation. A request never mixes replica choices, migrates KV,
or silently falls back to another track. M19 separately owns recovery and replay.

Removing a zero-flow non-primary replica is mandatory. Primary placements remain
immutable intent. Losing a replica prevents new admission to affected tracks; already
admitted requests terminate unless M19 has separately qualified recovery.

## 5. Planning and flow

Planning freezes one qualified primary cycle, then evaluates deterministic replica
candidates in stable identity order. A candidate is accepted only when:

- exact memory, KV, workspace, disk, backend, representation, artifact, thermal/power,
  and directed-edge evidence permit the placement;
- at least one complete legal track uses it with positive flow;
- robust admitted goodput improves by more than the declared minimum after uncertainty
  and failure-domain penalties;
- TTFT/TPOT, fairness, queue, and reservation budgets remain satisfied; and
- the replica budget and per-node placement bounds are respected.

The flow solver uses placement service capacities and measured directed-edge
capacities. Its output records requested/admitted/unmet demand, complete tracks,
traffic fractions, bottleneck before/after, candidate marginal gain, and zero-flow
removal. Fractions sum to one within a fixed numeric tolerance. Deterministic ties use
stable placement/track IDs.

One request is assigned to one track. Fractional flow is a workload allocation across
requests, never a split of one request. Scheduling is bounded and fair across admitted
interactive and batch work; M16 resource reservations remain authoritative.

## 6. Physical qualification and promotion

The physical baseline is one already-qualified M17 deployment. M18 materializes only
accepted replica assignments and retains the incumbent deployment throughout canary
qualification. The same-run evidence bundle includes:

- primary and replica identities, artifacts, assignments, load proofs, graph, tracks,
  edges, workload, and qualification;
- at least two simultaneous requests pinned to different complete tracks;
- per-request track IDs, per-placement applied work, and before/after frame counters;
- primary-only and replicated samples under the same declared workload;
- throughput, TTFT, TPOT, queue, memory, and fairness measurements; and
- replica removal followed by truthful surviving-track capacity and admission state.

Promotion requires a predeclared material throughput gain without violating latency,
correctness, memory, or fairness budgets. Prediction error is recorded. A neutral or
negative result remains valid evidence but cannot promote the replica plan.

## 7. UI convergence

- **Plans:** groups, primary/replica candidates, exact ranges, predicted and measured
  marginal gain, accepted/rejected reasons, legal tracks, traffic fractions,
  before/after bottleneck, uncertainty, and failure-domain warnings.
- **Network:** primary, alternative, and active replica tracks are visually distinct;
  directed forward and closure edges retain measured provenance.
- **Nodes:** replica-group membership, artifact/load/qualification state, capacity,
  assigned request count, and traffic share without private paths.
- **Inference/history:** exact immutable track ID and placement sequence for every
  request; this is labelled data-parallel request routing, never tensor parallelism.
- **Readiness/incidents:** every replica lifecycle authority, freshness, missing proof,
  zero-flow removal, qualification failure, and replica-loss degradation.

Refresh, section navigation, Back/Forward, and reconnect preserve the bounded plan,
track bindings, terminal request history, and rejection reasons. Stale evidence changes
current authorization to re-evaluation required; it does not rewrite historical facts.

## 8. Verification gate

1. Contract tests reject unknown fields, duplicate identities, mismatched ranges,
   incomplete tracks, missing/reversed edges, mixed generations, stale evidence,
   invalid fractions, private data, and readiness claims from non-qualifiers.
2. Pure planner/flow tests prove deterministic positive-gain selection, exact resource
   fit, zero/negative-gain rejection, zero-flow removal, failure-domain warnings,
   fairness, and no per-request path mixing.
3. Provision/load tests prove assignment-local integrity, warm reuse, replica-specific
   load proofs, and incumbent rollback on any candidate failure.
4. Physical evidence proves concurrent requests on at least two different qualified
   complete tracks with exact per-placement work and immutable request bindings.
5. The declared replicated workload materially outperforms the primary-only baseline
   within frozen budgets, or M18 remains unpromoted.
6. Removing one replica exposes only surviving qualified tracks and reduced measured
   capacity; no in-flight migration or recovery is claimed.
7. Browser verification covers Plans, Network, Nodes, Readiness, Incidents, Inference,
   refresh, reconnect, and exact history attribution in live mode.

M18 completes only when the physical benefit, degradation, and UI gates pass. Existing
planner fixtures or modeled capacity alone cannot close the milestone.
