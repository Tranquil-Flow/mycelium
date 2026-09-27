# A5 replica-pair shape probe (lane: mycelium-a5-lane-20260927)

Base: `4fea2b6d513a714614ce6cf9004e2d1ff02d0533` (tree `7d18c81ee5caa0ad5d4499f9ebd3a92984fd9e6f`).

## What this is

A **shape constructibility** artifact for the single A5 input gap quantified by
`scripts/qualify_a5_replica_track.py::derive_track_pair`:

> a layer-planner graph with >= 2 stages, exactly one stage carrying exactly 2
> placements, and those two placements forming a legal replica pair (same
> `replica_group_id`, same `stage_signature`, different `node_id`).

## Claim boundary (do not over-read)

* This proves the replica-pair **graph shape is constructible with the existing
  planner and the existing graph builder**. Nothing here is an A5 closure claim.
* **No A5 closure, no physical qualification, no benchmark/TTFT/TPOT threshold is
  claimed.** No physical run was executed; no live route, demo process or port
  (4350/4351) was touched.
* `MYCELIUM_A5_OPERATOR_TOKEN` is **absent**: no value exists anywhere in this
  lane, none was invented, printed or provisioned. The physical qualifier's
  install endpoint still fails closed without it on the serving host.
* The deployment is a synthetic probe fixture (`org/a5-replica-pair-probe`), not
  a real model deployment.

## Reproduce

```
python3.14 -B scripts/build_a5_replica_pair_graph.py \
    --output-dir /tmp/a5-lane-20260927-replicapair
python3.14 -B scripts/probe_a5_replica_gain_threshold.py
```

## Result (real payload, shipped policy `minimum_replica_gain_fraction: 0.05`)

* `plan_snapshot` emitted `mycelium.route_plan.v2`, `handoff_state:
  placement_intent_only`, 3 stage groups, 2 legal tracks, and exactly one
  replicated group `stage-002` (`stage-002-primary` on `n2-bottleneck` +
  `stage-002-replica-n3-spare` on `n3-spare`, both `layer_range [3, 4]`).
* Replica acceptance came from the planner's own decision record:
  `reason: accepted_positive_robust_gain`, `robust_gain_rps 2.7916` vs
  `minimum_required_gain_rps 0.1754`, `replica_capacity_gain_rps 3.1018`.
* `mycelium_qualification.physical_deployment.build_replicated_execution_graph`
  turned that plan into `mycelium.execution_graph.v1` with
  `stages[2].placements == [stage-002-primary, stage-002-replica-n3-spare]`
  (one `stage_signature`, one `replica_group_id`, two `node_id`s).
* `derive_track_pair` **accepted** it and returned the incumbent/replica
  placement tracks.
* `build_live_m16_runtime(graph)` (the next consumer inside
  `scripts/qualify_a5_replica_track.py::build`) then constructed
  `M16RuntimeCoordinator` successfully.
* Negative control: giving the replica placement a different runtime backend
  (`mlx` vs `numpy-stdlib`) makes `derive_track_pair` raise
  `replica_stage_identity_mismatch` — the identity gate is intact, not weakened.

## Files

* `route_plan.v2.json` — the planner's replica-pair plan document.
* `replica_pair_execution_graph.json` — the resulting execution graph.
