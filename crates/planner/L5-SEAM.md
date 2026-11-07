<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# L7 → L5 seam (round 6): bound compiler support and snapshot/event replanner

Branch `v02/l7-planner`; code commits `8223bf8` (bound compiler) and `ec2aaef` (snapshot planner + replanner), on preview 4 (`c50ccb7`).
Oracle `f4e366e98a1f705dfaf182897044500d290dbd7f` (read-only); vectors generated with Python 3.14.8.

## Identity 1 — `planner_assignment.py:23–94,359–470` (bound compiler / control-plane tranche)

Call the real L7 code; do not copy it into `protocol`.

| Oracle function | Rust path | Notes |
| --- | --- | --- |
| `compile_bound_layer_assignments` | `planner::bound::compile_bound_layer_assignments(&BoundInputs) -> PyResult<Vec<Value>>` | `BoundInputs { route_plan, planner_snapshot, evidence_bundle, manifest, deployment_id, deployment_epoch, cache_roots, runtime_by_node, limits }` |
| `compile_bound_replica_assignments` | `planner::bound::compile_bound_replica_assignments(&BoundInputs) -> PyResult<BTreeMap<String, Value>>` | ported; no oracle vectors this round (needs a ≥3-node bound bundle) |
| `validate_control_plane_tranche` | `planner::bound::validate_control_plane_tranche(&doc, &manifest, limits)` | re-compiles and compares |
| `product_route_to_manual_provisioning_route` | `planner::bound::product_route_to_manual_provisioning_route` | |
| `compile_layer_assignments` | `planner::bound::compile_layer_assignments(route, manifest, deployment_id, epoch, cache_roots, runtime_by_node, binding)` | faithful, incl. GPT-2 (`transformer.h.{layer}.`), any `block_prefix_template`, legacy `route_plan.v1` upgrade |
| `runtime_contracts.validate_normalized_runtime` | `planner::runtime::validate_normalized_runtime(&rt, expected_backend)` (+ `normalize_{gpt2,qwen2,qwen3,qwen35,qwen35_moe}_model_config`) | backends `mlx`, `numpy`, `mobile-rust-qwen2` |
| `manifest_digest_ref` / `verify_manifest_digest` | `planner::bound::{manifest_digest_ref, verify_manifest_digest}` | |
| `planner_snapshot_digest` | `planner::bound::planner_snapshot_digest` | |

Types: errors are `planner::py::{PyResult, Failure { kind: Kind::{Value, Other}, message }}`. `Kind::Value` is an oracle `ValueError` with the oracle's text; `Kind::Other` is any other Python exception class (still a refusal; text not an oracle contract).
`limits` is the caller-owned gossip record TTL cap (`protocol::gossip::Limits`); L7 does not default it (L1 registration pending upstream). Evidence-bundle refusals surface as the single message `gossip_evidence_invalid` because parsing delegates to `protocol::gossip::parse_bundle`; the assignment UUIDv5 delegates to `protocol::assignment::{id_for,validate_identity,validate_cache_root}` (this also resolves the "forked `assignment_id`" request: `planner::compile::assignment_id` now calls `protocol::assignment::id_for`).

Vectors (data, provenance-pinned): `crates/planner/tests/fixtures/bound-compiler.json` (+ `bound-compiler-provenance.json`): 828 runtime-contract, 863 model-config-normalizer, 1,406 layer-compile (GPT-2 and Qwen2 manifests), 766 bound-compile, 257 tranche cases; counts of accepted/refused are asserted by `tests/bound_compiler.rs`.
Cross-check: L5's own `protocol/fixtures/contracts/control-plane-tranche-v1.json` validates through `validate_control_plane_tranche`, and its embedded `route_plan` is byte-equal to `plan_snapshot(tranche.planner_snapshot)` (`tests/layer_planner.rs::l5_control_plane_tranche_fixture_…`).

## Identity 2 — `replan_simulator.py:53–125` (`plan_snapshot` + `replan_for_event`)

| Oracle | Rust path |
| --- | --- |
| `planner.plan_snapshot` | `planner::layer_planner::plan_snapshot(&snapshot) -> PyResult<RoutePlan>`; `RoutePlan::{to_value, dumps, digest}` (= `route_plan_to_dict`, `dumps_route_plan`, plan digest) |
| `replanning.replan_for_event` | `planner::layer_planner::replan_for_event(&prev, &snapshot, &TopologyEvent, min_gain) -> PyResult<Outcome>`; `Outcome::to_value` = `replan_outcome_to_dict` |
| `TopologyEvent` (+ `_event_from_dict`) | `TopologyEvent::from_value(&raw)` |
| `replan_simulator.simulate_bundle(path)` | `simulate_bundle_file(&Path)`; pure form `simulate_bundle(&bundle, &base_snapshot)` |

Cross-check: L5's `layer-replan-simulation-report-v1.json` is reproduced byte-for-byte from the pinned `product-v1-replanning.json` scenario (`l5_replan_report_fixture_is_reproduced_by_the_real_replanner`).
Vectors: `tests/fixtures/layer-planner.json` (+ provenance): 719 `plan_snapshot` cases (299 accepted) (5 shipped scenarios, per-path mutations, seeded generated fleets, forced search modes, large heuristic fleets), 1,059 `replan_for_event` cases (904 accepted) (every event kind, all recommendations, event-validation refusals, thresholds), 40 `simulate_bundle` cases (shipped scenario + generated + bundle refusals), 200 Python `sum()` / `math.fsum` vectors, 300 `format(x, '.12g')` vectors, 5,984 CPython small-int `set` iteration orders (the oracle sums `_local_swap` edge deltas in set order).

## Known limits (honest, for L5 and the integrator)

* Oracle JSON with `Infinity`/`NaN` tokens (invalid JSON) is not representable: Rust returns `Kind::Other` and the capture excluded such cases (counted in provenance; 0 in this capture).
* Dictionary insertion order: JSON documents are sorted-key maps; the only order-dependent output is `components` order (sorted-key manifests).
* `str.format` supports `{layer}` only; `uuid.UUID` spellings with `int(hex,16)` oddities (underscores, `0x`) are refused.
* One oracle fleet (exact enumeration with replication, >15 s in Python) was excluded by the capture's per-case timeout (`oracle_timeout_15s` = 1 in provenance).
* Legacy `planner::compile::compile_assignments` (round 1–5 API, qwen-only, stricter than the oracle — e.g. rejects braced/bare-hex deployment IDs the oracle canonicalises) is unchanged; use `planner::bound::*` for oracle parity. Unifying the two needs an integrator ruling because it would loosen an existing test.
* No product fixes; all behaviour (including oracle quirks) is preserved.

## SE-L5-L7: `validate_preparation_assignment` (round 10)

`route_assignment.validate_preparation_assignment(assignment, pack, node_id)` (v0.1.4 `mycelium_seed/route_assignment.py:311-337`) needs the
planner-owned `layer_assignment.validate_assignment_identity` and `runtime_contracts.validate_normalized_runtime(expected_backend='mobile-rust-qwen2')`.
Both already existed as `planner::bound::validate_assignment_identity` / `planner::runtime::validate_normalized_runtime`; the composed check is now
`planner::preparation::validate_preparation_assignment(&assignment, &pack, &node_id) -> Result<(), PreparationError>`:

* `Ok(())` = the oracle returned; `Err(PreparationError::Invalid)` = `SeedCoordinatorError('phone_preparation_assignment_invalid')`
  (`PreparationError::code()` gives the string); `Err(PreparationError::Escaped(_))` = an exception outside the oracle's
  `KeyError`/`TypeError`/`ValueError` net (the 2,261-case corpus has none; the arm is kept for fidelity).
* Bindings use Python `==` (`1 == 1.0 == True`) for node id, model id, commit, start/end layer and the tensor-name list; `manifest_digest` must fullmatch
  `sha256:[0-9a-f]{64}`; the pack must be a signed v2 manifest (`protocol::qwen_stage_manifest::is_signed_stage_manifest`). The pack signature is NOT verified here (v0.1.4 does not).
* `protocol` cannot call this (planner depends on protocol); the host/seed layer that issues route assignments calls it. Vectors: `tests/fixtures/preparation.json`
  (2,261 executed-oracle cases, FX-L7-preparation-r10), test `tests/preparation_parity.rs`.
