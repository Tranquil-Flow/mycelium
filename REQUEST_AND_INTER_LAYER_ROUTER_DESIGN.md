# Mycelium Request and Inter-Layer Router Design

**Status:** Standalone Router reference implemented and verified; planner/runtime/transport/simulator adapters remain later integration work.
**Date:** 2026-07-15
**Confirmed target:** standalone live-mesh Router library. This session builds request routing and inter-device hop routing; whole-system simulator integration and other component orchestration remain later work.
**Source specification:** `docs/specs/2026-07-15-router-component-source.md`

## 1. Goal

Build one Router implementation deployed identically on every device, with two internal roles:

- **Request Router / EntryCoordinator:** admits client requests, starts route construction, owns request checkpoints, streams output, and orchestrates recovery.
- **Inter-Layer Router / RelayEngine:** validates and schedules hop work, performs the local branch decision during prefill, forwards work along locked paths during decode, and reports local failures.

A `Router` facade hosts both roles. A device can be EntryCoordinator for requests it admitted while acting as RelayEngine for other requests.

This component selects and executes already-legal stage placements. It does not allocate layers, provision weights, declare a runtime ready, implement gossip, or execute model layers itself.

## 2. Reconciliation with existing Mycelium work

Three graph concepts must stay separate:

1. **Physical fleet graph:** arbitrary directed cyclic multigraph. This is the graph in the canonical DDAI simulator design.
2. **Model execution graph:** logically acyclic stage-placement graph for one prefill/decode pass. It has one explicit final-to-stage-0 loopback relation for autoregressive decode.
3. **Path manifest:** one request's concrete placement chain through the execution graph.

The Router traverses only the model execution DAG for one forward pass. The cut is the final-stage → stage-0 boundary: loopback is represented explicitly as a generation-cycle transition, not as a normal edge considered by DAG route construction. This keeps validation and suffix scoring finite and topologically ordered. It is not that routing systems can never support cycles; cycles simply add no useful execution semantics inside one ordered model pass.

The physical network may still contain cycles and multiple directed links. Those links are transport choices beneath the acyclic model-stage route.

The current analytical `planner_simulator.py` remains a planner/regression baseline. It does not yet emit a complete ready-to-route replica graph. Its `stage_signature` hashes only model ID and inclusive layer range, which is insufficient for runtime routing. This Router therefore consumes a new execution-graph contract and uses an adapter later; it does not silently reinterpret current planner output.

The canonical simulator excludes full-model data parallelism in v1. This live Router draft models **replicated stage placements**, not simulator-core full-model data parallelism. Simulator integration remains a separate future adapter and scope decision.

## 3. Non-negotiable invariants

1. **No invented edges.** Router selects only placement edges emitted by Layer Builder.
2. **Immutable execution identity.** Every path binds deployment ID, deployment epoch, model manifest digest, resolved model commit, topology version, stage signatures, assignment IDs, and load-proof digests.
3. **Half-open layer ranges.** Runtime-facing ranges use `[start_layer, end_layer_exclusive)` only.
4. **Ready means proved loaded.** A placement is selectable only with a same-epoch `mycelium.layer_load_proof.v1` marked ready.
5. **Structural snapshot pinned per attempt.** One path-construction/recovery attempt uses one immutable execution-graph version. Dynamic load readings may be refreshed at each branch; structural topology may not change under the attempt.
6. **Decode path is immutable.** Decode replays the locked manifest. No soft rerouting occurs without a new path attempt and full KV rebuild.
7. **Capacity is reserved before lock.** Every selected placement must hold a request-scoped KV reservation before the manifest becomes locked.
8. **Old attempts cannot act.** Hop, token, reservation, and failure messages carry `path_attempt`; stale-attempt work is rejected or cancelled idempotently.
9. **Recovery never duplicates client output.** Entry checkpoints committed token indexes and resumes from the next uncommitted token.
10. **Aging prevents starvation.** Scheduler boost terms are bounded; aging is unbounded or otherwise proven to eventually dominate.
11. **Privacy boundary.** Prompt and generated token history remain at Entry except when replaying the model path. Gossip receives load counters, never prompt/checkpoint content.

## 4. Component boundaries and ports

Core Router code remains stdlib-only and depends on interfaces, not concrete network/runtime implementations.

### 4.1 Read ports

- `TopologyProvider.snapshot() -> ExecutionGraph`
- `DeviceStateProvider.snapshot() -> DeviceStateTable`
- `Clock.now() -> float`

### 4.2 Side-effect ports

- `CapacityPort.reserve(...) -> ReservationResult`
- `CapacityPort.commit(...)`
- `CapacityPort.release(...)`
- `TransportPort.send_hop(...)`
- `TransportPort.send_manifest_delta(...)`
- `TransportPort.send_failure_report(...)`
- `TransportPort.send_token_event(...)`
- `RuntimePort.execute(...)`

The Router tests use deterministic in-memory implementations. Real HTTP/QUIC/gRPC, process RPC, and gossip-table adapters are later orchestration work.

## 5. Layer Builder handoff: `mycelium.execution_graph.v1`

The source Router specification does not define the graph payload precisely enough to implement safe routing. Add this explicit handoff contract:

```json
{
  "protocol": "mycelium.execution_graph.v1",
  "deployment_id": "uuid",
  "deployment_epoch": 7,
  "topology_version": 19,
  "model_id": "org/model",
  "resolved_commit": "40-hex-commit",
  "manifest_digest": "sha256:...",
  "entry_stage_id": "stage-000",
  "final_stage_id": "stage-003",
  "stages": [
    {
      "stage_id": "stage-001",
      "range": {
        "start_layer": 8,
        "end_layer_exclusive": 16,
        "layer_count": 8
      },
      "component_roles": ["decoder"],
      "stage_cost": {
        "prefill_work_units_per_prompt_token": 1.0,
        "decode_work_units_per_token": 1.0,
        "kv_bytes_per_context_token": 32768
      },
      "placements": [
        {
          "placement_id": "placement-a-stage-001",
          "node_id": "node-a",
          "replica_group_id": "stage-001-replicas",
          "assignment_id": "uuid",
          "stage_signature": "sha256:...",
          "load_proof_digest": "sha256:...",
          "runtime_backend": "mlx",
          "runtime_endpoint": "opaque://node-a/stage-001"
        }
      ]
    }
  ],
  "edges": [
    {
      "edge_id": "edge-a-b",
      "from_placement_id": "placement-a-stage-000",
      "to_placement_id": "placement-b-stage-001",
      "link_id": "node-a-to-node-b"
    }
  ],
  "loopback_edges": [
    {
      "edge_id": "loop-z-a",
      "from_placement_id": "placement-z-stage-003",
      "to_placement_id": "placement-a-stage-000",
      "link_id": "node-z-to-node-a"
    }
  ]
}
```

Validation fails closed on:

- range count mismatch, gaps, unauthorized overlaps, or mixed range semantics;
- cycles inside the stage DAG;
- edges that move backward or skip stages unless Layer Builder explicitly marks a legal fragment transition;
- missing entry/final coverage;
- placement identity mismatch with assignment/load proof;
- stale deployment epoch or manifest digest;
- duplicate placement IDs, edge IDs, or ambiguous stage order;
- a final placement lacking a legal loopback to the chosen stage-0 placement.

`replica_group_id` belongs to a placement, not a device-global record. A physical device may host multiple stages and therefore multiple placements/groups.

## 6. Dynamic state handoff: `mycelium.router_device_state.v1`

Keep structural placement identity out of the dynamic gossip state. Router consumes a table keyed by node ID:

```json
{
  "protocol": "mycelium.router_device_state.v1",
  "device_id": "node-a",
  "availability_state": "ALIVE",
  "compute_units_per_second": 420.0,
  "free_compute_fraction": 0.72,
  "free_memory_bytes": 12884901888,
  "internal_bandwidth_bytes_per_second": 200000000000,
  "pending_hop_queue_depth": 3,
  "placement_queue_depths": {"placement-a-stage-001": 1},
  "neighbor_latency_rtt_ms": {"node-b": 8.0},
  "neighbor_bandwidth_bytes_per_second": {"node-b": 125000000.0},
  "last_updated_timestamp": 1784136000.0
}
```

MVP permits `placement_queue_depths` to be absent and falls back to device-level depth. `compute_units_per_second` must use the same work-unit definition as `stage_cost`; otherwise the Router labels score confidence `FALLBACK` and uses configured conservative service times.

The current `node_profile.v1` and `network_probe.v1` are insufficient alone: they are capability/measurement inputs, not a placement-ready dynamic service contract.

## 7. Request and path contracts

### 7.1 RequestContext

Entry creates immutable request inputs:

- `request_id`
- prompt token IDs or opaque prompt reference available only at Entry
- `prompt_token_count`
- `max_new_tokens`
- `expected_new_tokens` used for scoring
- canonical generation-config digest
- sampling seed and deterministic sampling counter/state
- QoS class and target TTFT/TPOT/tok/s
- admission timestamp

### 7.2 `mycelium.path_manifest.v1`

```json
{
  "protocol": "mycelium.path_manifest.v1",
  "path_id": "uuid",
  "path_attempt": 0,
  "request_id": "uuid",
  "entry_device_id": "node-entry",
  "deployment_id": "uuid",
  "deployment_epoch": 7,
  "topology_version": 19,
  "model_id": "org/model",
  "resolved_commit": "40-hex-commit",
  "manifest_digest": "sha256:...",
  "ordered_hops": [
    {
      "hop_index": 0,
      "stage_id": "stage-000",
      "placement_id": "placement-a-stage-000",
      "device_id": "node-a",
      "replica_group_id": "stage-000-replicas",
      "assignment_id": "uuid",
      "stage_signature": "sha256:...",
      "load_proof_digest": "sha256:...",
      "reservation_id": "uuid"
    }
  ],
  "loopback_edge_id": "loop-z-a",
  "qos_class": "interactive",
  "alpha_resolved": 0.35,
  "score_mode": "sla_normalized_v1",
  "locked_at_timestamp": 1784136001.0,
  "manifest_state": "LOCKED"
}
```

Manifest state transitions:

```text
BUILDING -> RESERVING -> LOCKED -> COMPLETED
                         |          |
                         v          v
                      ABORTED    FAILED
```

A new recovery path gets a new `path_id` and incremented `path_attempt`. Never mutate a locked manifest in place.

### 7.3 Hop header

The original four-field header is not sufficient for idempotency or stale-attempt rejection. Use:

```json
{
  "protocol": "mycelium.hop_header.v1",
  "request_id": "uuid",
  "entry_device_id": "node-entry",
  "path_id": "uuid",
  "path_attempt": 0,
  "hop_index": 1,
  "stage_id": "stage-001",
  "placement_id": "placement-b-stage-001",
  "phase": "PREFILL",
  "token_index": null,
  "qos_class": "interactive",
  "admission_timestamp": 1784136000.0,
  "deadline_timestamp": 1784136005.0,
  "idempotency_key": "request:path_attempt:phase:token:hop",
  "priority_hint": {
    "target_tokens_per_second": 8.0,
    "observed_tokens_per_second": 6.0
  }
}
```

Relay rejects unknown path IDs, mismatched placement/hop index, expired deadlines, and lower path attempts. Duplicate idempotency keys return the cached prior disposition rather than executing twice.

## 8. Progressive path construction

### 8.1 Start

1. Entry pins the latest valid `ExecutionGraph` snapshot.
2. Entry reads dynamic state and filters unavailable/unready stage-0 placements.
3. If stage 0 has alternatives, Entry performs the first branch decision; otherwise it chooses the only legal placement.
4. Entry requests a tentative KV reservation and dispatches prefill.

### 8.2 Branch decision at Relay

At a branch placement:

1. Enumerate legal next placements from the pinned graph.
2. Exclude request-local failed devices, placements, and edges.
3. Exclude `SUSPECTED`/`DEAD`, stale-beyond-fallback, unready, or memory-infeasible candidates.
4. For each candidate, compute the cheapest legal suffix through the final stage plus a compatible loopback to the chosen stage-0 placement.
5. Score the candidate suffix.
6. Tentatively reserve KV capacity on the chosen next placement.
7. Append one manifest delta and mirror it idempotently to Entry.
8. Forward prefill.

If reservation fails, exclude that candidate for this decision and rescore. If no candidate remains, return a typed admission/backpressure failure to Entry.

### 8.3 Final lock

At final stage:

1. Validate gap-free ordered stage coverage and legal edges.
2. Validate every reservation remains live and assignment-bound.
3. Validate a legal loopback to the selected stage-0 placement.
4. Entry atomically commits all reservations and marks the manifest `LOCKED`.
5. Decode can begin only after Entry acknowledges lock.

This keeps branch choice physically local while preventing a partially built route from becoming an unreserved OOM path.

## 9. Scoring

### 9.1 Transfer cost

The attached source mentions latency and exposes bandwidth, but its formulas omit payload serialization. Router must include both:

```text
one_way_ms = RTT_ms / 2
transfer_ms = one_way_ms
            + jitter_guard_ms
            + payload_bytes / bandwidth_bytes_per_second * 1000
```

- prefill payload: `prompt_tokens * hidden_size * activation_bytes`
- decode payload: `hidden_size * activation_bytes`
- loopback payload: sampled token envelope, not hidden-state activation

Loss/retry cost can be added when the upstream state contract supplies it.

### 9.2 Compute cost

```text
available_rate = compute_units_per_second
               * max(free_compute_fraction, compute_floor)

prefill_compute_ms = stage.prefill_work_units_per_prompt_token
                   * prompt_token_count / available_rate * 1000

decode_compute_ms = stage.decode_work_units_per_token
                  / available_rate * 1000
```

Add a conservative queue-wait estimate from placement/device queue depth. Exact runtime-calibrated service curves replace this fallback later without changing RoutePolicy.

### 9.3 Staleness

Use projected age, not raw hop count alone:

```text
projected_age_s = now - last_updated + predicted_time_until_reached_s
confidence = 2 ** (-projected_age_s / half_life_s)
```

If confidence falls below threshold, substitute configured conservative compute, queue, latency, and bandwidth estimates. Never multiply an optimistic estimate by a low confidence factor, because that would make stale devices appear faster.

### 9.4 QoS score correction

Literal `alpha * TTFT_ms + (1-alpha) * TPOT_ms` does not make alpha a true 35/65 trade-off: TTFT is usually orders of magnitude larger than TPOT and will dominate despite alpha=0.35.

Recommended default:

```text
score = alpha * (TTFT_estimate_ms / target_TTFT_ms)
      + (1 - alpha) * (TPOT_estimate_ms / target_TPOT_ms)
```

This is dimensionless and interprets alpha as intended. Preserve a configurable `literal_weighted_ms_v1` mode only if strict source-spec parity is required. Every manifest records `score_mode`.

## 10. Decode execution

- Relay reads `ordered_hops[hop_index + 1]`; it does not call RoutePolicy.
- Each decode hop validates path attempt and idempotency key.
- Final stage emits a `TokenEvent` to Entry and sends the loopback token envelope to stage 0.
- Entry commits token ID and sampling counter before exposing the token to the client.
- Stage 0 starts the next token only after the required committed-token/loopback ordering signal.

Entry may be neither final stage nor stage 0; client output delivery is a control-plane event separate from the data-plane loopback edge.

## 11. Scheduling

Each device owns one scheduler over all local placement queues so multiple stages cannot independently overbook the same accelerator.

```text
priority = base_priority[qos_class]
         + aging_rate * wait_time_ms
         + min(max_deficit_boost,
               deficit_rate * max(0, target_tps - observed_tps))
```

Rules:

- deficit boost applies only to interactive class;
- deficit boost is capped;
- aging eventually dominates all bounded class/deficit differences;
- ties break by admission sequence, then deterministic request ID;
- queue depth exported to gossip updates on enqueue/dequeue;
- scheduler hands selected work to `RuntimePort`; batching mechanics remain runtime-owned.

## 12. Fault detection and recovery

### 12.1 Failure scopes

Do not always exclude the whole device:

- connection/transfer timeout -> exclude edge first;
- runtime-stage rejection/load-proof loss -> exclude placement;
- process/device death or gossip `DEAD` -> exclude device;
- capacity rejection -> exclude placement for this attempt, not permanently.

### 12.2 Two deadlines

Separate:

1. transfer/acceptance deadline;
2. execution/result deadline after acceptance.

This avoids blaming a device for time spent in the local scheduler. Expected deadlines include measured transfer, advertised remote queue wait, compute estimate, and timeout multiplier.

### 12.3 Failure report

Failure reports bind request ID, path ID, path attempt, failed edge/placement/device, phase, token index, typed reason, observations, and reporter ID. Entry ignores reports for completed tokens or stale attempts.

### 12.4 Recovery

1. Entry marks request `RECOVERING` and increments `path_attempt`.
2. Entry sends best-effort cancel for the old attempt and releases old reservations.
3. Entry derives request-local exclusions from typed failure scope.
4. Entry pins the newest valid topology snapshot.
5. Entry progressively builds and reserves a new path.
6. Recovery prefill replays original prompt plus committed generated token IDs.
7. Sampling state/seed/counter is restored.
8. Generation resumes at the first uncommitted token index.
9. Exceeding `max_rebuild_attempts` fails the request explicitly.

Prompt + token IDs rebuild KV state; sampling state is also retained so recovery does not silently change stochastic-generation semantics.

Entry-device failure remains out of scope and fails the request.

## 13. Topology changes and leases

- New admission reads latest graph version.
- In-flight locked paths retain their pinned graph and placement leases.
- Layer Builder may mark placements `RETIRING`, but must keep their assignments/runtimes alive until leases drain or a retirement deadline forces an ordinary Router recovery.
- A new deployment epoch cannot reuse old load proofs, reservations, or path manifests.
- Structural changes during a progressive build abort and retry the build on one new pinned snapshot; the Router never stitches two topology versions into one manifest.

## 14. MVP test harness

Use deterministic fakes:

- `FakeTopologyProvider` with versioned snapshots;
- `FakeDeviceStateProvider` with controllable timestamps/load;
- `FakeCapacityPort` with accept/reject/expiry behavior;
- `FakeTransportPort` with edge delays/failures and message capture;
- `FakeRuntimePort` with deterministic token production and placement failures;
- `ManualClock` and deterministic ID source.

Definition-of-done scenarios:

1. Progressive branch chooses the lower SLA-normalized suffix and locks a manifest.
2. Decode replays locked path despite later dynamic-state changes.
3. Prefill includes bandwidth serialization; slower bandwidth can reverse a latency-only choice.
4. Stale optimistic state falls back conservatively.
5. Reservation rejection causes local rescore; mandatory-stage exhaustion returns typed backpressure.
6. Non-entry placement failure causes a new attempt, full replay, and completion without duplicate client token IDs.
7. Edge failure excludes edge without unnecessarily banning device.
8. Two overlapping requests use priority scheduling; aging proves batch request eventually runs.
9. New topology version affects new requests only; locked request stays on old leased path.
10. Stale attempt messages, duplicate hop keys, stale load proofs, and mixed epochs are rejected.
11. Entry failure returns explicit unrecoverable failure.
12. Property tests generate small DAGs and prove every locked manifest has legal edges, gap-free stages, valid loopback, and one placement per stage.

## 15. Explicit non-goals for this component session

- gossip transport/aggregation/membership;
- Layer Builder algorithms or weight provisioning;
- real model loading/execution;
- concrete HTTP/QUIC/gRPC transport;
- cross-request runtime batching;
- KV migration or shadow KV;
- soft mid-request rerouting;
- Entry failover;
- trust/signatures beyond carrying proof digests;
- simulator plugin integration;
- production benchmarks or performance claims.

## 16. Decisions for Evi

The build can proceed with the recommended defaults below. Please override any that do not match intent:

1. **Target:** standalone live Router with fakes now; adapters/integration later. Recommended: yes.
2. **Internal split:** one deployable Router, explicit `EntryCoordinator` + `RelayEngine` classes. Recommended: yes.
3. **Score semantics:** SLA-normalized score rather than literal weighted milliseconds. Recommended: normalized.
4. **Structural consistency:** pin one topology version per path attempt while refreshing only dynamic load state at each branch. Recommended: yes.
5. **Admission:** tentative per-placement KV reservations before final path lock. Recommended: yes.
6. **Core dependencies:** stdlib-only, matching current Mycelium core. Recommended: yes.
7. **QoS MVP:** `interactive` and `batch`; default alpha interactive 0.35, batch 0.10, all constants configurable. Recommended: yes.
8. **Expected output length:** request supplies `max_new_tokens`; use a capped `expected_new_tokens` for scoring and max for memory admission. Recommended: yes.

## 17. Integration gates for the later orchestration session

Router cannot safely connect to current components until these seams exist:

1. Layer Builder emits `execution_graph.v1` with half-open ranges and strong stage signatures.
2. Every placement carries a same-epoch assignment and load-proof digest.
3. Gossip adapter exposes comparable compute units, dynamic free capacity, placement queue depth, RTT convention, and link bandwidth.
4. Runtime implements idempotent execute/cancel plus request-scoped KV reservation lifecycle.
5. Transport distinguishes control-plane events from activation/token data plane.
6. Placement retirement honors in-flight leases.
7. Full-chain integration test proves one deployment epoch from model manifest through route completion.

Until these pass, this component is a tested Router reference implementation, not a working distributed inference runtime.
