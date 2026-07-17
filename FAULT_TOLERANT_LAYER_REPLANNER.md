# Fault-Tolerant Layer Replanner Architecture

Status: focused Planner-side MVP design. This document does not claim heartbeat,
KV replication, runtime readiness, route activation, or transparent request
recovery.

## 1. Goal

Let the Layer Planner react deterministically to an immutable topology event and
answer two questions:

1. Does the current placement intent still contain a complete legal track that
   avoids the failed node or directed edge?
2. If not—or if a useful node joined—what fresh `mycelium.route_plan.v2`
   placement intent should downstream systems provision and validate?

The Replanner never executes failover. It produces assessment and replacement
placement intent.

## 2. Ownership boundary

Five control loops cooperate without sharing authority:

| Component | Owns | Must not claim |
|---|---|---|
| Gossip / Fault Handler | Liveness evidence, active-edge quarantine, typed topology events, coalescing | Route or layer choice |
| Router | Immediate request-local track switch, attempt fencing, replay, token deduplication | Layer allocation or loaded weights |
| Successor standby subsystem | `mycelium.failover_candidate.v1`, successor artifacts, KV replication/watermarks, one fenced takeover | General multi-node replanning |
| Layer Replanner | Placement-intent viability assessment and deterministic full candidate replan | Runtime readiness, activation, deployment epoch authority |
| Provisioner / Layer Builder | Assignment compilation, download/load proof, execution graph publication, retirement | Failure detection or request replay |

Existing canonical design remains authoritative for successor takeover:
`docs/plans/2026-07-16-successor-standby-kv-replication-v1.md`.
This Replanner references that candidate tier but does not compile, provision,
activate, or replicate it.

## 3. Recovery ladder

For one unavailable device or directed edge:

0. Quarantine the active edge/request immediately in Fault Handler/Router.
1. If an unaffected complete legal track exists in the current placement intent,
   report `existing_track_intent`. Router/Builder must still prove every selected
   placement and edge is currently loaded, leased, epoch-compatible, and KV-safe.
2. If no current track survives and exactly one interior logical stage was lost,
   report escalation order:
   `successor_standby_candidate -> full_replan`.
3. Otherwise report `full_replan` immediately.
4. A full replan emits only new `placement_intent_only`. Old routes remain pinned
   until downstream provisioning, challenge, and fenced activation succeed.

For a joining device:

1. Existing requests and deployment stay unchanged.
2. Build a candidate plan from the newer immutable fleet snapshot.
3. Compare planned request capacity with configurable hysteresis.
4. Recommend candidate provisioning only above threshold; never activate merely
   because a node appeared.

For measurement drift or capacity changes, use the same deferred candidate path.

## 4. Protocol: `mycelium.layer_replan.v1`

### Topology event

```json
{
  "event_id": "event-42",
  "snapshot_generation": 83,
  "kind": "device_unavailable|edge_unavailable|device_joined|measurement_drift",
  "node_ids": ["node-b"],
  "edges": [["node-a", "node-b"]]
}
```

Rules:

- Event ID is stable for idempotent coalescing.
- Snapshot generation is monotonic evidence supplied by Gossip/Fault Handler.
- Directed edges stay directed.
- Device-unavailable events carry node IDs only; edge-unavailable events carry
  directed edges only; join-link facts belong in the fresh snapshot.
- Measurement drift may scope nodes, directed edges, or both.
- Device-unavailable and edge-unavailable events fail closed when malformed.
- The Planner does not decide `suspect` versus `dead`; the caller chooses when to
  request a candidate.

### Assessment

```json
{
  "protocol": "mycelium.layer_replan_assessment.v1",
  "action": "existing_track_intent|full_replan|candidate_replan|no_action",
  "urgency": "immediate|deferred|none",
  "surviving_track_ids": ["track-001"],
  "affected_group_ids": ["stage-001"],
  "escalation_order": ["successor_standby_candidate", "full_replan"],
  "external_readiness_required": true,
  "reason": "..."
}
```

`existing_track_intent` means graph viability only. It is deliberately not named
`failover_ready`.

### Outcome

Outcome binds:

- topology event and snapshot generation;
- assessment;
- previous and candidate plan digests;
- optional candidate `route_plan.v2`;
- planned capacity gain fraction;
- recommendation;
- reason and explicit `placement_intent_only` state.

Recommendations are advisory:

- `router_builder_validate_existing_track`;
- `prefer_route_ready_successor_standby_else_provision_candidate`;
- `provision_replanned_intent`;
- `provision_candidate`;
- `retain_current_plan`;
- `no_viable_plan`.

## 5. Deterministic assessment algorithm

1. Map placement IDs to node IDs and logical stage groups.
2. Mark placements unavailable when their node is unavailable.
3. Mark a track unavailable when it contains an unavailable placement or one of
   its directed adjacent/loopback node edges is unavailable.
4. If any complete legal track remains, return all surviving track IDs in stable
   order and require external readiness validation.
5. If no track remains, derive affected logical groups.
6. Permit successor-standby escalation only for one unavailable device affecting
   exactly one interior group. Stage 0, final stage, multiple affected groups,
   directed-edge-only failure, and multiple-device failure use full replan.
7. Unknown unavailable devices that do not participate in the current plan are a
   no-op for the active plan; they may still alter a future fleet snapshot.
8. Join and drift events are deferred candidate evaluations.

## 6. Deterministic candidate replan

1. Deep-copy the immutable input snapshot.
2. Mark unavailable nodes ineligible with explicit event reason.
3. Remove only explicitly unavailable directed links.
4. Require joined nodes to exist in the new snapshot with measured/candidate
   links; do not synthesize capabilities or reverse edges.
5. Call existing `plan_snapshot()` unchanged.
6. Preserve `placement_intent_only` and validate the candidate normally.
7. Hash canonical serialized old/new plans.
8. For urgent loss, prepare candidate provisioning regardless of capacity gain.
9. For join/drift, recommend provisioning only if capacity gain meets hysteresis.
10. Return typed `no_viable_plan` rather than reusing a stale failed route.

This operation does not mutate the prior plan or source snapshot.

## 7. Generation and activation safety

`RoutePlanV2.snapshot_digest` binds facts but is not deployment authority.
Replanner records `snapshot_generation`; downstream Coordinator/Builder owns:

- monotonic deployment epoch;
- assignment IDs and immutable manifests;
- load proofs and leases;
- route generation/fencing token;
- atomic activation and old-route retirement.

A new plan may be computed while the old deployment remains active. New requests
must not use candidate placements until Builder publishes a validated execution
graph. Existing requests remain on their pinned route or Router recovery attempt.

## 8. MVP simulation matrix

Use the existing replicated six-node fixture:

1. Drop a replica-only node: unaffected legal tracks survive; no full replan.
2. Drop the unique interior node `n1`: no legal track survives; successor standby
   is preferred if externally `route_ready`, while full replan is prepared.
3. Fail directed edge `n2 -> n3`: alternate final-stage replica tracks survive.
4. Add a fast `n6`: candidate replan is deferred and hysteresis-gated.
5. Add a non-beneficial node: retain current plan.
6. Make all feasible coverage disappear: return `no_viable_plan`.

Simulation proves deterministic Planner decisions and valid placement intent. It
confines `base_snapshot` references to the scenario directory and does not prove
heartbeat latency, weights/KV readiness, fenced activation, request replay, or
real-machine recovery time.

## 9. Integration events

Expected input from Fault Handler/Gossip:

- immutable `AllocatorView` snapshot generation;
- event ID and typed scope;
- unavailable nodes and directed edges;
- complete fresh node/link facts for joins.

Expected output to Coordinator/Provisioner:

- assessment and escalation ladder;
- optional candidate `route_plan.v2`;
- old/new canonical digests;
- explicit external readiness requirements.

No direct import of Router, Gossip, provisioning, or standby-runtime packages is
permitted in `mycelium_layer_planner`.

## 10. Deferred beyond this focused MVP

- automatic debounce/coalescing policy;
- multi-failure robust optimization;
- recursive successor absorption;
- KV migration cost in global replan scoring;
- stage-movement minimization and download-byte-aware objective;
- deployment-epoch and route-generation activation adapter;
- whole-system simulator integration;
- physical kill/restart evidence.
