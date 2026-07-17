# Mycelium Layer Planner Product V1 Architecture

**Status:** Implemented side-by-side with the legacy MVP in
`mycelium_layer_planner/`. Product V1 emits deterministic
`mycelium.route_plan.v2` placement intent. Live model calibration remains gated
and no measured performance claim is made without an evidence artifact.

**Component:** Layer Planner only. The Request/Inter-Layer Router, Weight
Provisioner/Layer Sharder, runtime executor, and simulator remain separate
components with explicit future handoffs.

**Legacy compatibility remains authoritative for `route_plan.v1`:**
`layer_planner.py` and `ALLOCATOR.md` retain their existing semantics. Product
V1 is a separate package and protocol; it does not silently mutate v1.

## 1. Purpose

This document collects future Layer Planner improvements without expanding the
current MVP claim boundary. It absorbs the planner-relevant architecture from:

- `HANDOVER.md`;
- `ALLOCATOR.md`;
- `PLANNER_SIMULATION_HANDOVER.md`;
- the canonical simulator architecture at
  `/Users/evinova/Projects/ddai/sim/design.md`.

It does not absorb Router execution policy into the Planner. The Router may use
`REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md` later, after the Planner, Layer
Sharder, and load-proof components provide the required handoffs.

## 2. Milestone and protocol versioning

“Product V1” is a future milestone. It is not the same thing as the already
emitted protocol name `mycelium.route_plan.v1`.

The current MVP protocol must not silently change semantics. In particular,
current `route_plan.v1` layer pairs are inclusive display ranges. Product V1
should use a new, explicitly versioned handoff—likely `mycelium.route_plan.v2`—
with half-open ranges:

```json
{
  "start_layer": 0,
  "end_layer_exclusive": 28,
  "layer_count": 28
}
```

The invariant is:

```text
end_layer_exclusive - start_layer == layer_count
```

## 3. Component boundary

### Layer Planner owns

- selecting eligible devices from capability and availability inputs;
- selecting a directed stage order;
- assigning contiguous model-layer ranges to stages;
- estimating prefill, decode, transfer, and memory costs;
- recording one explicit decode loopback from the final stage to stage 0;
- producing placement intent and diagnostics;
- producing alternative plans when requested;
- re-planning from a fresh snapshot after an external availability change.

### Layer Planner does not own

- downloading or repacking model weights;
- choosing exact checkpoint shard files;
- proving that layers are loaded;
- live request admission or scheduling;
- executing inter-layer transfers;
- handling an in-flight node failure;
- moving or rebuilding KV cache;
- gossip, transport, or runtime process management.

These remain separate responsibilities:

```text
Layer Planner
  -> placement intent and phase-aware graph
Weight Provisioner / Layer Sharder
  -> immutable assignments and layer-load proofs
Layer Builder
  -> ready execution graph
Request / Inter-Layer Router
  -> request path lock and execution
Runtime
  -> actual layer execution and KV ownership
```

The downstream V1 implementation now explicitly plans complete-successor
disk/RAM standby assignments, synchronous KV replication, whole-request replay
fallback, and Router-fenced activation. These remain Router, Runtime, and Weight
Provisioner responsibilities rather than Layer Planner behavior. The Planner
must expose enough range, memory, KV-size, runtime-compatibility, and directed-link
cost information for those components to validate candidates. See
`docs/plans/2026-07-16-successor-standby-kv-replication-v1.md`.

## 4. Graph model

Product V1 should keep three graph views separate.

### 4.1 Physical device graph

A directed graph or multigraph:

```text
G_physical = (devices, directed links)
```

Each directed link may have different bandwidth, latency, jitter, loss, and
availability from its reverse link. Physical cycles are allowed.

### 4.2 One-pass model execution graph

For one prefill pass or one decode pass through model layers, stages form an
acyclic forward chain:

```text
stage_0 -> stage_1 -> ... -> stage_last
```

Each stage owns one positive, contiguous, half-open layer range. Stage count
cannot exceed model layer count because every active stage needs at least one
layer.

### 4.3 Autoregressive decode cycles

Autoregressive generation adds one explicit relation:

```text
stage_0 -> stage_1 -> ... -> stage_last
   ^                                |
   |------- sampled token ----------|
```

The Planner must include the final-to-first directed link in decode topology
selection and cost. Replicated placements generalize this into ordered stage
groups. A complete placement selection may form an independent decode loop,
while directed edges between adjacent groups may cross between loops. Hybrid
tracks are legal only when they still select exactly one placement from every
ordered group and possess a final-to-selected-first loopback.

The Router may still open any selected cycle at `stage_last` when representing
one execution pass: forward stages remain a DAG, while the chosen loopback is
carried as explicit metadata for starting the next token step.

This avoids forcing a cycle into Router DAG validation while preserving the
real network cost of returning each generated token to stage 0.

## 5. Prefill and decode require different graph objectives

One node order and layer allocation may be shared initially, but Product V1
must calculate and report phase-specific costs.

### 5.1 Prefill: acyclic path view

Prefill traverses the model once. It does not use the final-to-first loopback
before producing the first token.

For each forward stage boundary:

```text
prefill_payload_bytes = batch_size
                      * prompt_tokens
                      * hidden_size
                      * activation_bytes
```

Approximate prefill objective:

```text
TTFT = sum(stage_prefill_compute)
     + sum(forward_prefill_transfers)
     + queue/startup terms when available
```

The best prefill path is therefore an acyclic source-to-final-stage path.

### 5.2 Autoregressive decode: cyclic path view

Each generated token performs one complete forward traversal and then returns a
small token envelope from the final stage to stage 0.

For ordinary forward boundaries:

```text
decode_activation_bytes = active_batch_size
                        * hidden_size
                        * activation_bytes
```

For the final-to-first closure:

```text
loopback_payload = sampled token envelope
```

The loopback is much smaller than a hidden-state activation, but its latency and
jitter occur once per generated token and must be included.

For one request, approximate decode latency is:

```text
TPOT = sum(stage_decode_compute)
     + sum(forward_decode_transfers)
     + final_to_first_token_transfer
```

For concurrent pipeline traffic, Product V1 should also report the maximum
stage/link service time as the throughput bottleneck rather than confusing it
with single-request serial latency.

### 5.3 Combined selection

Do not collapse prefill and decode into one unlabelled number. Report both and
select using an explicit policy, for example:

```text
score = prefill_weight * normalized_TTFT
      + decode_weight  * normalized_TPOT
```

Alternative policies may optimize prefill, decode, full expected response, or a
Pareto frontier. Every route plan must record the policy and assumptions used.

### 5.4 Unknown prefill/decode ratios

The Planner must not hide one guessed prompt-to-output ratio inside the route
score. Its workload profile should carry distributions or explicit scenario
points for:

- prompt tokens;
- expected and maximum output tokens;
- concurrent requests and batch shape;
- interactive versus batch traffic;
- speculative-decoding proposal width and acceptance rate when enabled.

For one scenario, a useful response objective is:

```text
expected_response_ms = TTFT + expected_output_tokens * TPOT
```

When workload data is uncertain, evaluate a scenario matrix and either choose a
robust route, return a Pareto frontier, or state which scenario selected the
route. Report phase-optimal counterfactuals, but keep one request on one shared
placement chain by default: changing chains after prefill would require KV
migration or full replay.

### 5.5 Phase-specific directed network cost

The physical directed edge is the same link in both phases, but its effective
weight is not invariant because payload sizes differ. For directed edge `e` and
payload size `B`:

```text
geo_floor_ms = distance_km * propagation_constant_ms_per_km
base_one_way_ms = max(measured_RTT_ms / 2, geo_floor_ms)
transfer_ms(e, B) = base_one_way_ms
                  + jitter_guard_ms
                  + B * 8 / measured_bandwidth_bps * 1000
                  + loss_retry_penalty_ms
                  + contention_or_queue_penalty_ms
```

Use device-reported active measurements for RTT, jitter, and streamed transfer
speed. Geolocation provides a physical lower bound, anomaly check, and fallback
when measurements are absent or stale. Do not add full geolocation latency to an
already measured RTT; that would double-count propagation.

Prefill uses a large sequence activation, decode forward edges use a small
per-token activation, and decode closure uses a smaller sampled-token envelope.
Activation width is read from the immutable machine-readable model config and
is assumed invariant across ordinary transformer blocks in Product V1.

Equal activation width across hops does **not** make route ranking invariant:
`latency + payload / bandwidth` contains both an additive latency term and a
payload-scaled serialization term. Small decode payloads emphasize RTT/jitter;
large prefill payloads reduce their relative importance and emphasize streamed
bandwidth. Consequently the best directed order can differ between prefill and
decode even though graph direction and per-boundary dimensions are unchanged.

### 5.6 Speculative-decoding planning note

Product V1 should represent speculative decoding as an optional overlay, disabled
unless nodes and runtimes advertise compatible draft/verify support.

A speculative plan records:

- draft model identity and draft placement path;
- target/verifier model identity and verifier placement path;
- proposal block size;
- expected acceptance-rate distribution, not only one optimistic value;
- draft compute and transfer cost;
- batched verification compute and transfer cost;
- rejection/rollback or repair cost;
- KV ownership and compatibility for both draft and verifier paths.

Draft-token proposals are small control/token payloads, while target verification
still executes the target model's stage graph. The Planner should score expected
accepted target tokens per second and TTFT, plus low-acceptance sensitivity. It
must not claim speculative gain from draft speed alone. A request remains pinned
to compatible draft and verifier chains unless Router/Runtime recovery explicitly
rebuilds or transfers state.

## 6. KV-cache note

Normal pinned autoregressive decoding does not send KV cache around the cycle.
Each stage retains the KV tensors for its own layers. The ordinary loopback from
last stage to stage 0 carries the generated token envelope, not the complete KV
cache.

KV transfer becomes relevant only when a request changes placements or routes.
The confirmed downstream V1 recovery plan supports both:

1. synchronously replicating compatible successor KV tensors to a fenced
   predecessor takeover placement; and
2. rebuilding KV state by replaying the original prompt plus committed output
   tokens through a newly planned path when replication cannot prove a complete
   committed watermark.

A future planner may estimate KV migration using:

```text
kv_transfer_bytes = assigned_layers
                  * 2
                  * kv_heads
                  * head_dim
                  * sequence_length
                  * kv_dtype_bytes
```

However, the Layer Planner should only expose the estimate and compatibility
requirements. The Router decides when recovery is needed, and the Runtime owns
KV serialization, transfer, validation, or replay.

## 7. Recommended shortest directed-cycle search

### 7.1 Pure network cycle baseline

For a fixed selected device set with allocation-independent edge costs, the
problem is a directed Hamiltonian cycle / asymmetric travelling-salesperson
problem.

Recommended exact baseline for small pools:

- Held–Karp subset dynamic programming;
- directed edge costs;
- explicit final-to-first closure cost;
- fix or enumerate the start stage when closure payload differs from ordinary
  activation payload;
- time complexity `O(n^2 * 2^n)`;
- memory complexity `O(n * 2^n)`.

The existing analytical simulator already uses directed Held–Karp through 12
nodes for the network-shortest ring.

### 7.2 Real Planner objective

Pure network-shortest cycle is not sufficient because node order and layer
counts affect compute and memory feasibility. Product V1 should use a nested
search:

1. generate a directed cycle candidate;
2. open it at each feasible stage-0 choice;
3. run contiguous layer-allocation DP for that order;
4. calculate separate prefill and decode scores;
5. include the final-to-first decode closure;
6. reject memory-infeasible assignments;
7. retain the best candidate under the selected policy.

For very small pools, exact permutation search plus inner layer DP provides an
exact joint answer. The existing simulator uses this approach through seven
nodes for its full single-request-throughput objective.

For larger pools, use:

- Held–Karp network cycle as one seed when still affordable;
- multi-start directed nearest-neighbour or cheapest insertion;
- directed swap and Or-opt/local relocation improvement;
- re-run layer-allocation DP after every accepted topology mutation;
- optional throughput-based node pruning after a complete feasible plan exists.

This is preferable to calling one network-shortest cycle “optimal” when its
slow compute or memory spill makes end-to-end inference worse.

### 7.3 Node pruning

The analytical simulator's 5% throughput-improvement threshold is a useful V1
reference policy:

```text
drop a node only when a fully re-evaluated plan improves the selected metric
by more than 5%
```

This is optimization pruning, not runtime dropout handling. Runtime failure and
in-flight recovery remain Router responsibilities. A changed availability
snapshot may trigger the Planner to produce a new plan for future/rebuilt
requests.

### 7.4 Fleet-size search policy

Product V1 has no configured maximum candidate-node count. Search budgets choose
algorithms; they do not silently discard every device after a fixed rank. All
eligible devices remain candidates, although an optimal plan may leave some idle.

Recommended defaults:

| Eligible devices | Primary-cycle search | Replication search |
|---:|---|---|
| 1–7 | Exact directed order enumeration plus inner layer DP | Exact one-step replica alternatives, then iterate |
| 8–12 | Directed Held–Karp network seed, legal cycle openings, inner layer DP | Greedy best marginal gain with capacity-aware flow assignment |
| 13–32 | Multi-start directed nearest-neighbour plus directed local swap | Greedy bottleneck relief with capacity-aware shortest-track flow evaluation |
| 33–128 | Capped multi-start nearest-neighbour plus directed local swap (`clustered_refinement` provenance tier; true network clustering remains future work) | Greedy one-at-a-time positive-gain replicas with capacity-aware flow assignment |
| More than 128 | Capped multi-start nearest-neighbour plus directed local swap (`hierarchical_refinement` provenance tier; true region coarsening remains future work) | Greedy one-at-a-time positive-gain replicas; hierarchical replica pools remain future work |

These thresholds are configurable search modes, not architectural fleet limits.
The primary pipeline still has at most `L` stages for `L` model layers, but
replicated stage placements may make total participating devices exceed `L`.

### 7.5 Iterative data-parallel stage replication

Here “data parallel” means replicating the same contiguous stage range so
different requests can use different replicas. It does not split one request's
layer tensor across devices; that would be tensor parallelism and requires a
separate collective-communication planner.

Recommended Product V1 algorithm: **greedy bottleneck relief with min-cost-flow
re-evaluation**.

1. Build one feasible primary pipeline/cycle and layer allocation.
2. Estimate per-stage compute capacity and per-link service capacity under the
   workload scenarios.
3. Solve fractional request-flow assignment across currently legal complete
   paths; identify the highest-utilization stage or edge.
4. Generate replicas only for bottleneck or near-bottleneck stage ranges on
   compatible nodes with enough memory and the same immutable model revision.
5. For each candidate replica, add legal incoming/outgoing directed edges and
   any legal decode-loopback relation, then resolve request-flow assignment.
6. Score marginal gain in throughput, TTFT, TPOT, memory headroom, provisioning
   cost, and robustness.
7. Accept the best candidate only when it improves the selected robust objective
   beyond a configured epsilon.
8. Pin each request to one complete replica track so its stage-local KV remains
   coherent.
9. Recompute bottlenecks and repeat until no positive-gain replica remains,
   resources are exhausted, or the deterministic replica-iteration budget is
   exhausted.
10. Remove zero-flow placements before emitting the plan.

Primary stage order and half-open layer-range order are frozen before this
replication loop. A node not selected for the primary may become a placement in
one ordered replica group; it does not reorder the primary. Each candidate is
connected to every physically legal placement in the immediately prior and
posterior layer groups. Final-group placements receive legal loopbacks to
first-group placements. This permits independent replica cycles and cross-cycle
hybrid tracks without changing model-layer order.

The flow solver may use min-cost max-flow when capacities are integral or a small
linear-programming adapter when fractional traffic shares are required. A later
upgrade may use column generation: each legal full pipeline/cycle is a column,
the master problem allocates traffic, and a reduced-cost directed-cycle search
proposes new columns. The greedy algorithm is the preferred V1 implementation
because every iteration is explainable and can be tested against brute-force
small-fleet oracles.

Stage replication changes the graph from one chain into a DAG of alternative
stage placements plus one or more explicit legal decode loopbacks. Router
chooses one legal complete track per request and pins stage-local KV to that
track; Planner does not schedule live requests.

## 8. Current MVP size arguments and search steps

Let:

- `N` = eligible devices;
- `K` = active stages/devices in one candidate plan;
- `L` = model layer count.

Current `layer_planner.py` behavior:

1. Parse node profiles and an optional directed link matrix.
2. Reject nodes failing power, backend, or memory eligibility.
3. Rank candidates and keep at most `max_nodes` (`6` by default).
4. Set `K <= min(N, max_nodes, L)`.
5. Prefer the widest feasible `K`.
6. Enumerate directed node-order permutations for that width.
7. For each order, run contiguous layer-count DP.
8. Add transfer costs only for adjacent forward stages.
9. Choose the minimum estimated compute-plus-transfer objective.
10. Emit one linear `mycelium.route_plan.v1` route.

Current per-order layer-allocation DP is approximately `O(K * L^2)`. Order
search is factorial/permutation-based and kept bounded by `max_nodes=6`.
Current production code does not score the final-to-first decode link.

Product V1 should remove the fixed device-count limit as an architectural rule.
Search thresholds may select exact versus heuristic algorithms, but every
reported plan must retain the physical constraint `K <= L`.

## 9. Product V1 graph-construction steps

### Step 1: Pin inputs

Capture one immutable planning snapshot:

- model architecture and immutable model identity when available;
- static node capabilities;
- dynamic availability and free capacity;
- directed RTT, jitter, loss, streamed-throughput, and geolocation observations;
- measurement timestamps and confidence;
- workload scenario matrix;
- speculative-decoding capabilities when available;
- planning policy and deterministic candidate/replica budgets.

### Step 2: Build the directed physical graph

Create one vertex per eligible device; do not truncate the candidate pool to a
fixed `max_nodes`. Create one edge per usable measured direction. Mark missing,
stale, or inferred links explicitly.

### Step 3: Build phase-specific edge-cost functions

For each physical edge, derive a payload-sensitive transfer function from measured
RTT, jitter, streamed bandwidth, loss, queue/contention state, and geolocation
lower bound. Instantiate different weights for:

- prefill activation transfer;
- decode activation transfer;
- sampled-token loopback;
- speculative draft proposals;
- optional KV migration/recovery estimates.

### Step 4: Estimate node capacity curves

For each device, estimate:

- maximum positive layer count;
- weight and KV memory demand;
- prefill and decode compute curves by layer count;
- optional VRAM-to-RAM spill behavior;
- backend, precision, model-revision, and speculative-role compatibility;
- service capacity at expected concurrency.

### Step 5: Build workload scenarios

Use observed workload distributions when available. Otherwise evaluate explicit
prompt/output/concurrency/acceptance-rate scenarios instead of hiding one ratio.
Keep TTFT, TPOT, throughput, and tail-risk results separate.

The implemented empirical chat preset carries observed prompt/response shape
and a configurable Poisson capacity sweep. User count/load scale is explicit.
Fleet size changes available capacity and search strategy; it does not silently
invent more demand. Custom scenarios may set prompt, output, concurrency,
probability, system-prefix/history overhead, and user scale.

### Step 6: Generate primary cycle/order candidates

Choose exact or fleet-scaled bounded heuristic search from policy thresholds. The
current implementation labels 33–128-node and larger modes for future clustered
and hierarchical refinement, but today uses deterministic capped multi-start
nearest-neighbour plus directed local swap in both tiers; it does not claim that
geographic coarsening is already implemented. The primary candidate is a
directed cycle for decode scoring and the same cycle opened at its final stage
for prefill/one-pass execution. A deterministic candidate-evaluation budget
bounds nested primary scoring. Provenance records the budget, explored count,
exhaustion status, and exactness; wall-clock watchdog telemetry stays outside the
byte-stable placement protocol.

### Step 7: Allocate contiguous layers

For each candidate order, assign every target-model layer exactly once with
positive, contiguous, half-open ranges. Reject gaps, accidental overlaps, model
revision mismatch, and capacity violations.

### Step 8: Score shared and phase-optimal views

Calculate:

- prefill acyclic TTFT and bottleneck;
- decode cyclic TPOT and bottleneck;
- expected full-response latency across workload scenarios;
- separate prefill-optimal and decode-optimal counterfactuals;
- memory headroom and input confidence.

Use one shared target-model chain by default to preserve KV locality. Emit a
phase switch only when its measured benefit exceeds explicit KV transfer/replay
cost and the downstream runtime supports it.

### Step 9: Add data-parallel stage replicas iteratively

Run greedy bottleneck relief with flow re-evaluation. Add one compatible stage
replica at a time, resolve traffic shares, accept only positive robust gain, and
repeat. Preserve primary order. Build independent complete replica loops when
placements cover every group, retain legal adjacent-group cross-loop edges, and
map every request to one complete legal track.

### Step 10: Add optional speculative-decoding overlay

When supported, jointly place draft and verifier paths, then score proposal,
verification, rejection, and state costs over acceptance-rate scenarios. Keep
ordinary target-only decoding as a fallback plan.

### Step 11: Validate and simplify

Remove zero-flow placements. Validate directed edge existence, layer coverage,
replica range equality, model identity, memory feasibility, cycle closure,
Router-openable DAG views, and deterministic tie-breaking.

### Step 12: Emit placement intent and hand off

Emit stages, replicas, legal tracks/traffic fractions, directed forward edges,
explicit decode loopbacks, phase estimates, workload assumptions, confidence,
and diagnostics. Do not claim weights are loaded.

Weight Provisioner/Layer Sharder resolves exact files and emits assignments and
load proofs. Layer Builder publishes only ready placements. Router then selects
and executes request paths without changing Planner ownership.

## 10. Product V1 handoff shape

The implementation emits this protocol family through typed contracts,
validation, and deterministic serialization. The following remains an
illustrative abbreviated shape:

```json
{
  "protocol": "mycelium.route_plan.v2",
  "graph_kind": "directed_pipeline_with_decode_loopback",
  "model": {
    "model_id": "org/model",
    "resolved_commit": "40-hex-commit",
    "manifest_digest": "sha256:..."
  },
  "policy": {
    "objective": "robust_prefill_decode_workload",
    "search_mode": "fleet_scaled_exact_heuristic_or_hierarchical",
    "node_prune_minimum_improvement": 0.05,
    "fixed_candidate_node_cap": null
  },
  "workload_profile": {
    "scenarios": [],
    "speculative_decoding": {
      "enabled": false,
      "proposal_width": null,
      "acceptance_rate_scenarios": []
    }
  },
  "stages": [
    {
      "stage_id": "stage-000-primary",
      "replica_group_id": "stage-000",
      "node_id": "node-a",
      "range": {
        "start_layer": 0,
        "end_layer_exclusive": 8,
        "layer_count": 8
      }
    }
  ],
  "replica_groups": [
    {
      "replica_group_id": "stage-000",
      "range": {"start_layer": 0, "end_layer_exclusive": 8},
      "placement_stage_ids": ["stage-000-primary"]
    }
  ],
  "legal_tracks": [
    {
      "track_id": "track-primary",
      "stage_ids": ["stage-000-primary"],
      "planned_traffic_fraction": 1.0
    }
  ],
  "forward_edges": [
    {
      "from_stage_id": "stage-000-primary",
      "to_stage_id": "stage-001-primary",
      "payload_class": "activation"
    }
  ],
  "decode_loopback": {
    "from_stage_id": "stage-last-primary",
    "to_stage_id": "stage-000-primary",
    "payload_class": "sampled_token_envelope"
  },
  "phase_estimates": {
    "prefill": {"graph_view": "acyclic"},
    "decode": {"graph_view": "cyclic"}
  },
  "handoff_state": "placement_intent_only"
}
```

Exact checkpoint files, assignment IDs, load-proof digests, runtime endpoints,
and placement leases are added by later components. They must not be fabricated
by the Planner.

## 11. Product V1 improvement registry and implementation boundary

The implemented package covers the placement-intent contracts and algorithms
listed below, with explicit exceptions: true geographic/hierarchical coarsening,
measured per-layer calibration, threshold-based post-plan pruning, KV migration,
and live-model validation remain future gates rather than fabricated behavior:

1. phase-aware prefill versus decode optimization;
2. explicit final-to-first decode-loopback scoring;
3. payload-sensitive directed edge costs from measured throughput/RTT/jitter/loss;
4. geolocation propagation floors and stale-measurement confidence;
5. exact, Held–Karp, and bounded multi-start directed-cycle search by fleet size;
6. removal of fixed architectural device-count caps;
7. calibration-artifact schema and heuristic-versus-measured evidence gate;
8. explicit memory hierarchy and VRAM-to-RAM spill estimates;
9. scenario-based prompt/output/concurrency assumptions;
10. robust optimization or Pareto output when workload ratios are unknown;
11. iterative data-parallel stage replication with flow re-evaluation;
12. candidate subset selection during joint search; threshold-based post-plan
    throughput pruning remains future work;
13. speculative draft/verifier placement and acceptance-rate sensitivity;
14. alternate placement or fragment-plan output;
15. confidence and staleness reporting;
16. candidate `route_plan.v2` handoff with half-open ranges, replica groups,
    legal tracks, and explicit loopbacks;
17. immutable model identity in every executable downstream artifact;
18. KV-migration estimate as optional recovery metadata;
19. end-to-end validation against real, small model runs before performance
    claims.

## 12. MVP-to-Product-V1 implementation order

Detailed TDD execution plan:
`docs/plans/2026-07-15-layer-planner-product-v1.md`.

No real model is required for the first slices.

1. Specify and test the future half-open route-plan handoff with synthetic
   fixtures.
2. Add the directed physical graph and payload-sensitive network-cost model.
3. Add explicit forward edges and decode-loopback metadata.
4. Separate prefill/decode metrics and scenario-based workload scoring.
5. Add exact tiny-fleet cycle search with brute-force oracle tests.
6. Add small/medium/large fleet heuristic and hierarchical search modes without
   a fixed candidate-node cap.
7. Add contiguous layer-allocation DP under each candidate order.
8. Add iterative data-parallel stage replication and flow assignment.
9. Add speculative draft/verifier planning as an optional, fail-closed overlay.
10. Add streamed network-throughput measurements, geolocation sanity checks, and
    staleness metadata.
11. Add synthetic capacity-curve fixtures and property tests.
12. Calibrate with a tiny real model or representative decoder layer.
13. Integrate immutable manifest and assignment contracts.
14. Connect to Layer Builder and Router only in the later joint integration
    session.

## 13. Acceptance gates before calling Product V1 complete

- Prefill uses an acyclic forward path and excludes loopback from TTFT.
- Decode includes the directed final-to-first token loopback.
- Directed asymmetry changes selected routes in tests.
- Prefill/decode payload changes can change edge ranking without changing physical
  edge direction.
- Measured RTT/throughput dominate; geolocation is a floor/fallback and is not
  double-counted.
- No fixed candidate-node cap exists; fleet-size thresholds choose search modes.
- All stage ranges are half-open, positive, contiguous, and gap-free.
- Every primary stage receives at least one layer.
- Replicas match the exact range/model identity of their replica group.
- Every emitted traffic fraction maps to a legal complete track.
- Exact small-case results match brute-force oracles.
- Heuristic large-case output is feasible and never reported as globally exact.
- Unknown workload ratios produce explicit scenarios, robust selection, or a
  Pareto result rather than an invisible guess.
- Speculative plans report proposal width and acceptance sensitivity and preserve
  a target-only fallback.
- KV cache remains stage-local during normal decode.
- KV migration and replay are labeled alternative recovery costs.
- Route output remains placement intent until downstream load proofs exist.
- Router and Planner responsibilities remain separate.
- Real-model performance claims require measured calibration artifacts.

## 14. Fault-tolerant replanning boundary

The Planner now exposes `mycelium_layer_planner.replanning` for deterministic
reaction to immutable topology events. Detailed contract:
`FAULT_TOLERANT_LAYER_REPLANNER.md`; execution plan:
`docs/plans/2026-07-16-fault-tolerant-layer-replanner-mvp.md`.

Implemented behavior:

1. `TopologyEvent` validates unavailable-device, unavailable-directed-edge,
   joined-device, and measurement-drift facts with a snapshot generation.
2. `assess_topology_event()` filters complete legal tracks by unavailable
   placements and directed adjacent/loopback node edges.
3. An unaffected track yields `existing_track_intent`, never a runtime-ready
   claim. Router and Builder must validate loaded assignments, leases, route
   generation, and KV compatibility.
4. Loss of one unique interior stage reports the external escalation ladder
   `successor_standby_candidate -> full_replan`. The canonical standby/KV
   subsystem owns that first tier; Planner does not implement it.
5. Other route-breaking failures require a fresh full candidate plan.
6. Join and drift events produce deferred candidate plans gated by configurable
   request-capacity hysteresis.
7. Failed nodes become ineligible in a copied snapshot; failed directed edges
   alone are removed. Source snapshots and prior plans remain unchanged.
8. No feasible replacement returns typed `no_viable_plan`; it never revives a
   stale invalid route.
9. Every candidate remains `mycelium.route_plan.v2` with
   `handoff_state=placement_intent_only`.

Executable simulation:

```bash
python3 simulate_layer_replanner.py \
  --scenario scenarios/product-v1-replanning.json \
  --output outputs/product-v1-replanning-report.json
```

The verified four cases cover replica-only dropout, unique interior-stage
loss, directed-edge loss with alternate tracks, and a beneficial node join.
The simulation proves deterministic Planner decisions and valid placement
intent only. Failure detection, successor KV replication, request replay,
load proofs, deployment epochs, fenced activation, and physical recovery-time
evidence remain owned by their separate components.
