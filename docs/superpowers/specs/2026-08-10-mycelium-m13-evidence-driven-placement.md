# Mycelium M13 evidence-driven physical placement specification

## Objective and authority

M13 replaces operator-selected live layer ranges with a deterministic `planner_v2`
candidate compiled from one atomic, signed Gossip evidence bundle. The planner may
choose only native members whose signed membership, calibrated capacity, directed
links, model binding, and decode mode are all current in the same snapshot. A plan,
assignment, staged artifact, or local load proof is never serving authority by itself;
the existing qualifier and atomic candidate promotion remain authoritative.

## Atomic evidence eligibility

`PlannerInputAdapter` accepts exactly one canonical
`mycelium.gossip.evidence_bundle.v1`. It validates the bundle signature/source binding,
swarm, deployment epoch, snapshot generation, record TTLs, and model/revision/manifest
identity before projection. Every admitted node must have exactly one current signed
membership record and one current status record at that snapshot generation.

A node is excluded with a stable reason unless all of these hold:

- activation-eligible peer class and exact runtime capability;
- lifecycle `CONFIGURED` or `RUNNING`, fresh lease, and no revocation;
- calibrated capacity profile for the exact model digest, quantization, backend,
  runtime build, hardware class, and power mode;
- positive fast and total allocatable memory with fast not exceeding total;
- finite prefill/decode coefficients and memory/spill bandwidth; and
- a declared qualified decode mode compatible with every other primary stage.

Mixed generations, unsigned membership, stale TTL, malformed calibration, or a model
binding mismatch fail the whole snapshot closed. An individually ineligible node is
retained in planner/UI exclusions but never silently admitted.

## Capacity and memory tiers

`mycelium_capacity_profiles` is the sole calibration authority. Gossip status records
carry the profile identity/digest and its measured coefficients; adapters do not
invent defaults or implement another calibration formula.

Allocator view exposes both `fast_allocatable_bytes` and
`total_allocatable_bytes`. Reservations are applied once by the evidence producer;
planner policy reserve must therefore be zero. Required weight, KV, and workspace
bytes first consume fast memory. The remainder may spill only when total memory fits
and measured spill bandwidth is positive. Spill bytes and penalty remain visible in
the DP result and UI.

## Topology, decode mode, and allocation

For M13, the candidate set uses the currently qualified physical node order while M14
owns measured cycle selection. Nevertheless, every directed edge required by a
candidate order and its closure must exist, be reachable, current, and non-inferred;
missing or unreachable required edges fail validation rather than becoming a sparse
graph.

Every runtime backend declares a qualified decode mode. The M13 physical homogeneous
MLX route requires `stage_local_kv`. A heterogeneous candidate that would require
`complete_context_replay` is rejected unless a later qualification explicitly allows
and surfaces that downgrade. Silent route-wide KV downgrade is forbidden.

The existing contiguous allocation DP runs over each legal opening supplied to it.
It assigns at least one layer per primary stage, covers `[0, num_layers)` exactly once,
preserves entry/intermediate/final component roles, minimizes the maximum modeled
service work, and resolves equal costs lexicographically by layer counts and stable
node identity. Output provenance must be `planner_v2`.

## Assignment-local deployment and cache

The planner result binds deployment/epoch, snapshot digest, ordered node IDs, ranges,
model revision/manifest, quantization, backend, decode mode, and assignment digest.
Stage packs contain only assignment-owned tensors plus explicitly permitted shared
tokenizer/config assets.

One content-addressed cache is keyed by public model revision, manifest, format,
quantization, and tensor digest. A model format is downloaded and verified once.
Peers receive only missing objects. Reuse revalidates digest; partial/corrupt entries
are quarantined. Population is single-writer and atomic, readers see only committed
entries, eviction is bounded and never removes pinned active assignments.

## Candidate promotion and UI

Every topology is a candidate deployment. Bounded canary prompts, negative checks,
quality evidence, and `mycelium.performance_budget.v1` comparison decide atomic
promotion or rollback. Already admitted requests remain bound to their captured
incumbent qualification.

Plans shows snapshot inputs, exclusions, calibration/profile bindings, memory
feasibility, spill, DP candidates, deterministic ties, and A/B deltas. Network shows
the selected order and layer ranges. Nodes shows calibrated capacity, decode mode,
assigned objects, and load state. Readiness shows `planner_v2` provenance and exact
qualification/load proofs.

## Acceptance gates

1. Contract and adversarial tests reject mixed/stale/unsigned evidence, missing
   calibration, collapsed memory tiers, missing required directed edges, decode-mode
   mismatch, non-deterministic ties, and non-`planner_v2` live assignments.
2. A compute-only A/B changes one measured capacity and produces a traceable
   allocation or eligibility change.
3. A memory-only A/B changes fast versus total memory without changing compute and
   produces a traceable feasibility/allocation/spill change.
4. File-open and loaded-tensor evidence proves no partial-stage host receives or loads
   the complete checkpoint.
5. A physical candidate serves two arbitrary browser prompts, survives canary
   promotion, proves rollback, and keeps incumbent requests continuous.
6. Plans, Network, Nodes, and Readiness explain the same signed snapshot and result
   after navigation and refresh.
