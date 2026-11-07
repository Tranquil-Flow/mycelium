// SPDX-License-Identifier: AGPL-3.0-only
//! `plan_snapshot`, route-plan serialization and validation, speculative scoring
//! (`planner.py`, `serialization.py`, `validation.py`, `speculative.py`) at f4e366e9.
use super::contracts::{
    Graph, Link, ModelIdentity, NUMERIC_EPSILON, NodeCapability, Policy, Profile, num_f, num_value,
    other, py_float, py_int, py_iter, py_sum, truthy,
};
use super::replicate::{Placement, replicate_stages};
use super::search::{Provenance, admit_primary_nodes, plan_primary};
use crate::py::{Failure, PyResult, get, py_str, verr};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

pub const ROUTE_PLAN_PROTOCOL: &str = "mycelium.route_plan.v2";

/// A JSON number that is finite (the oracle would emit `Infinity`/`NaN`, invalid JSON).
fn fv(x: f64) -> PyResult<Value> {
    if x.is_finite() {
        Ok(json!(x))
    } else {
        other("non-finite float in plan output (oracle emits Infinity/NaN tokens)")
    }
}

#[derive(Debug, Clone)]
pub struct LegalTrack {
    pub track_id: String,
    pub placement_ids: Vec<String>,
    pub traffic_fraction: f64,
    pub cost_ms: f64,
}

#[derive(Debug, Clone)]
pub struct PlanEdge {
    pub src_placement_id: String,
    pub dst_placement_id: String,
    pub capacity_rps: f64,
    pub cost_ms: f64,
}

#[derive(Debug, Clone)]
pub struct Loopback {
    pub src_placement_id: String,
    pub dst_placement_id: String,
    pub payload_bytes: i64,
    pub cost_ms: f64,
}

#[derive(Debug, Clone)]
pub struct RoutePlan {
    pub model: ModelIdentity,
    pub snapshot_digest: String,
    pub placements: Vec<Placement>,
    pub legal_tracks: Vec<LegalTrack>,
    pub forward_edges: Vec<PlanEdge>,
    pub loopbacks: Vec<Loopback>,
    pub provenance: Provenance,
    pub workload_name: String,
    pub metrics: Value,
    pub diagnostics: Value,
}

impl RoutePlan {
    /// `route_plan_to_dict(plan)` (`dataclasses.asdict`).
    pub fn to_value(&self) -> PyResult<Value> {
        let placements = self
            .placements
            .iter()
            .map(|p| {
                Ok(json!({
                    "placement_id": p.placement_id,
                    "replica_group_id": p.replica_group_id,
                    "node_id": p.node_id,
                    "layer_range": {"start": p.start, "end": p.end},
                    "primary": p.primary,
                    "service_capacity_rps": fv(p.service_capacity_rps)?,
                }))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let tracks = self
            .legal_tracks
            .iter()
            .map(|t| {
                Ok(json!({
                    "track_id": t.track_id,
                    "placement_ids": t.placement_ids,
                    "traffic_fraction": fv(t.traffic_fraction)?,
                    "cost_ms": fv(t.cost_ms)?,
                }))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let edges = self
            .forward_edges
            .iter()
            .map(|e| {
                Ok(json!({
                    "src_placement_id": e.src_placement_id,
                    "dst_placement_id": e.dst_placement_id,
                    "capacity_rps": fv(e.capacity_rps)?,
                    "cost_ms": fv(e.cost_ms)?,
                    "kind": "forward",
                }))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let loops = self
            .loopbacks
            .iter()
            .map(|l| {
                Ok(json!({
                    "src_placement_id": l.src_placement_id,
                    "dst_placement_id": l.dst_placement_id,
                    "payload_bytes": l.payload_bytes,
                    "cost_ms": fv(l.cost_ms)?,
                }))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let value = json!({
            "model": self.model.to_value(),
            "snapshot_digest": self.snapshot_digest,
            "placements": placements,
            "legal_tracks": tracks,
            "forward_edges": edges,
            "loopbacks": loops,
            "provenance": {
                "mode": self.provenance.mode,
                "globally_exact": self.provenance.globally_exact,
                "explored_candidates": self.provenance.explored_candidates,
                "candidate_node_count": self.provenance.candidate_node_count,
                "candidate_budget": self.provenance.candidate_budget_raw,
                "budget_exhausted": self.provenance.budget_exhausted,
            },
            "workload_name": self.workload_name,
            "metrics": self.metrics,
            "diagnostics": self.diagnostics,
            "handoff_state": "placement_intent_only",
            "protocol": ROUTE_PLAN_PROTOCOL,
        });
        Ok(value)
    }

    /// `dumps_route_plan(plan)` (compact, sorted keys, `ensure_ascii=False`).
    pub fn dumps(&self) -> PyResult<String> {
        crate::canonical(&self.to_value()?, false)
            .map_err(|_| Failure::other("route plan is not canonical JSON"))
    }

    /// `sha256:` of `dumps_route_plan(plan)` (the replanner's plan digest).
    pub fn digest(&self) -> PyResult<String> {
        use sha2::{Digest, Sha256};
        Ok(format!(
            "sha256:{:x}",
            Sha256::digest(self.dumps()?.as_bytes())
        ))
    }
}

// ---------------------------------------------------------------- speculative
struct SpecConfig {
    draft_model_id: String,
    draft_revision: String,
    proposal_width: i128,
    distribution: Vec<f64>,
}

fn parse_speculative(data: &Value) -> PyResult<Option<SpecConfig>> {
    if !truthy(data) {
        return Ok(None);
    }
    let req = |key: &str| -> PyResult<&Value> { crate::py::req(data, key) };
    let distribution: Vec<f64> = match req("accepted_count_distribution")? {
        Value::Array(a) => a.iter().map(py_float).collect::<PyResult<_>>()?,
        _ => return other("accepted_count_distribution not iterable"),
    };
    let config = SpecConfig {
        draft_model_id: py_str(req("draft_model_id")?),
        draft_revision: py_str(req("draft_revision")?),
        proposal_width: py_int(req("proposal_width")?)?,
        distribution,
    };
    if config.draft_model_id.is_empty()
        || config.draft_revision.is_empty()
        || config.proposal_width <= 0
    {
        return verr("invalid speculative configuration");
    }
    if config.distribution.len() as i128 != config.proposal_width + 1 {
        return verr("acceptance distribution must cover counts 0..proposal_width");
    }
    if config.distribution.iter().any(|p| *p < 0.0) {
        return verr("acceptance probabilities must be non-negative");
    }
    if (py_sum(config.distribution.iter().copied()) - 1.0).abs() > NUMERIC_EPSILON {
        return verr("acceptance probabilities must sum to one");
    }
    Ok(Some(config))
}

fn speculative_diagnostics(
    config: &SpecConfig,
    timing: &Value,
    default_ms: f64,
) -> PyResult<Value> {
    let float_of = |key: &str, default: f64| -> PyResult<f64> {
        match get(timing, key)? {
            Value::Null if !crate::py::has(timing, key) => Ok(default),
            v => py_float(v),
        }
    };
    let target = float_of("target_decode_ms", default_ms)?;
    let draft = float_of("draft_decode_ms", default_ms)?;
    let verification = float_of("verification_ms", default_ms)?;
    let transfer = float_of("proposal_transfer_ms", 0.0)?;
    let runtime_supported =
        crate::py::has(timing, "runtime_supported") && truthy(get(timing, "runtime_supported")?);
    let payload = if crate::py::has(timing, "proposal_payload_bytes") {
        py_int(get(timing, "proposal_payload_bytes")?)?
    } else {
        16
    };
    let threshold = float_of("material_gain_threshold", 0.10)?;
    if target.min(draft).min(verification).min(transfer) < 0.0 {
        return verr("speculative timing inputs must be non-negative");
    }
    if payload <= 0 {
        return verr("proposal payload must be positive");
    }
    if !(0.0..1.0).contains(&threshold) {
        return verr("material gain threshold must be in [0, 1)");
    }
    let target_tps = if target > 0.0 {
        1000.0 / target
    } else {
        f64::INFINITY
    };
    let width = config.proposal_width;
    let expected = py_sum(
        config
            .distribution
            .iter()
            .enumerate()
            .map(|(count, p)| count as f64 * p),
    );
    let committed = py_sum(config.distribution.iter().enumerate().map(|(count, p)| {
        let tokens = if count as i128 == width {
            count
        } else {
            count + 1
        };
        tokens as f64 * p
    }));
    let cycle_ms = width as f64 * draft + verification + transfer;
    let speculative_tps = if cycle_ms > 0.0 {
        committed * 1000.0 / cycle_ms
    } else {
        f64::INFINITY
    };
    let gain = if target_tps.is_finite() && target_tps > 0.0 {
        (speculative_tps / target_tps) - 1.0
    } else {
        f64::NEG_INFINITY
    };
    let enabled = runtime_supported;
    let use_speculative = enabled && gain >= threshold;
    let reason = if use_speculative {
        "beneficial"
    } else if !enabled {
        "runtime_unsupported"
    } else {
        "material_gain_not_predicted"
    };
    Ok(json!({
        "enabled": enabled,
        "use_speculative": use_speculative,
        "reason": reason,
        "target_fallback": true,
        "expected_accepted_tokens": fv(expected)?,
        "expected_committed_tokens": fv(committed)?,
        "predicted_gain_fraction": fv(gain)?,
        "material_gain_threshold": fv(threshold)?,
        "draft_kv_owner": format!("draft:{}@{}", config.draft_model_id, config.draft_revision),
        "target_kv_owner": "target_model_track",
    }))
}

// ---------------------------------------------------------------- validation
const FORBIDDEN_RUNTIME_KEYS: [&str; 6] = [
    "loaded",
    "ready",
    "leased",
    "load_proof",
    "runtime_ready",
    "weights_loaded",
];

fn contains_forbidden_claim(v: &Value) -> bool {
    match v {
        Value::Object(m) => m.iter().any(|(k, child)| {
            FORBIDDEN_RUNTIME_KEYS.contains(&k.to_lowercase().as_str())
                || contains_forbidden_claim(child)
        }),
        Value::Array(a) => a.iter().any(contains_forbidden_claim),
        _ => false,
    }
}

pub fn validate_route_plan(plan: &RoutePlan) -> PyResult<()> {
    if contains_forbidden_claim(&plan.diagnostics) || contains_forbidden_claim(&plan.metrics) {
        return verr("Planner output contains a forbidden runtime-readiness claim");
    }
    let mut by_id: BTreeMap<&str, &Placement> = BTreeMap::new();
    for p in &plan.placements {
        by_id.insert(&p.placement_id, p);
    }
    if by_id.len() != plan.placements.len() || by_id.is_empty() {
        return verr("placements must be non-empty and uniquely identified");
    }
    let mut groups: BTreeMap<&str, Vec<&Placement>> = BTreeMap::new();
    for p in &plan.placements {
        groups.entry(&p.replica_group_id).or_default().push(p);
    }
    let mut ordered: Vec<(&str, &Vec<&Placement>)> = groups.iter().map(|(k, v)| (*k, v)).collect();
    ordered.sort_by(|a, b| (a.1[0].start, a.0).cmp(&(b.1[0].start, b.0)));
    let mut expected_start = 0;
    let mut group_order: Vec<&str> = Vec::new();
    for (group_id, members) in &ordered {
        let first = (members[0].start, members[0].end);
        if members.iter().any(|m| (m.start, m.end) != first) {
            return verr(format!("replica group {group_id} has mismatched ranges"));
        }
        if first.0 != expected_start {
            return verr("primary layer groups contain a gap or overlap");
        }
        expected_start = first.1;
        if members.iter().filter(|m| m.primary).count() != 1 {
            return verr("every replica group requires exactly one primary placement");
        }
        group_order.push(group_id);
    }
    if num_f(plan.model.num_layers) != expected_start as f64 {
        return verr("layer groups do not cover the complete model");
    }
    let group_of =
        |pid: &str| -> Option<&str> { by_id.get(pid).map(|p| p.replica_group_id.as_str()) };
    let index_of = |g: &str| group_order.iter().position(|x| *x == g);
    let forward: std::collections::BTreeSet<(&str, &str)> = plan
        .forward_edges
        .iter()
        .map(|e| (e.src_placement_id.as_str(), e.dst_placement_id.as_str()))
        .collect();
    let loops: std::collections::BTreeSet<(&str, &str)> = plan
        .loopbacks
        .iter()
        .map(|l| (l.src_placement_id.as_str(), l.dst_placement_id.as_str()))
        .collect();
    for edge in &plan.forward_edges {
        let (Some(sg), Some(dg)) = (
            group_of(&edge.src_placement_id),
            group_of(&edge.dst_placement_id),
        ) else {
            return verr("forward edge references unknown placement");
        };
        let (si, di) = (
            index_of(sg).unwrap_or(0) as i64,
            index_of(dg).unwrap_or(0) as i64,
        );
        if di - si != 1 {
            return verr("forward edges must connect adjacent ordered layer groups");
        }
    }
    if !plan.legal_tracks.is_empty() {
        let total = py_sum(plan.legal_tracks.iter().map(|t| t.traffic_fraction));
        if (total - 1.0).abs() > NUMERIC_EPSILON {
            return verr("legal track traffic fractions must sum to one");
        }
    }
    for track in &plan.legal_tracks {
        if track.placement_ids.len() != group_order.len() {
            return verr("legal track does not contain exactly one placement per layer group");
        }
        if track
            .placement_ids
            .iter()
            .any(|p| !by_id.contains_key(p.as_str()))
        {
            return verr("legal track references unknown placement");
        }
        let track_groups: Vec<&str> = track
            .placement_ids
            .iter()
            .filter_map(|p| group_of(p))
            .collect();
        if track_groups != group_order {
            return verr("legal track violates immutable layer-group order");
        }
        if track
            .placement_ids
            .windows(2)
            .any(|w| !forward.contains(&(w[0].as_str(), w[1].as_str())))
        {
            return verr("legal track uses nonexistent directed forward edge");
        }
        if track.placement_ids.len() > 1
            && !loops.contains(&(
                track.placement_ids[track.placement_ids.len() - 1].as_str(),
                track.placement_ids[0].as_str(),
            ))
        {
            return verr("legal decode track lacks final-to-first loopback");
        }
    }
    for l in &plan.loopbacks {
        let (Some(sg), Some(dg)) = (group_of(&l.src_placement_id), group_of(&l.dst_placement_id))
        else {
            return verr("loopback references unknown placement");
        };
        if Some(&sg) != group_order.last() || Some(&dg) != group_order.first() {
            return verr("loopback must connect final group to first group");
        }
    }
    if plan.provenance.globally_exact
        && ![
            "exact_joint_enumeration",
            "exact_topology_enumeration",
            "held_karp",
        ]
        .contains(&plan.provenance.mode.as_str())
    {
        return verr("unknown global optimality claim");
    }
    Ok(())
}

// ---------------------------------------------------------------- plan_snapshot
/// `plan_snapshot(snapshot)`.
pub fn plan_snapshot(snapshot: &Value) -> PyResult<RoutePlan> {
    if !snapshot.is_object() {
        return verr("snapshot must be a mapping");
    }
    let missing = |key: &str| -> Failure {
        Failure::value(format!("snapshot missing required field: {key}"))
    };
    let model_value = snapshot.get("model").ok_or_else(|| missing("model"))?;
    let model = ModelIdentity::parse(model_value)?;
    let nodes_value = snapshot.get("nodes").ok_or_else(|| missing("nodes"))?;
    let nodes = py_iter(nodes_value)?
        .iter()
        .map(NodeCapability::parse)
        .collect::<PyResult<Vec<_>>>()?;
    let links_value = snapshot.get("links").ok_or_else(|| missing("links"))?;
    let links = py_iter(links_value)?
        .iter()
        .map(Link::parse)
        .collect::<PyResult<Vec<_>>>()?;
    let policy = Policy::parse(snapshot.get("policy").unwrap_or(&json!({})))?;
    let profile = Profile::parse(
        snapshot
            .get("workload")
            .unwrap_or(&json!({"preset": "interactive_chat_v1"})),
    )?;
    let graph = Graph::build(&nodes, &links, &policy)?;
    let admitted: Vec<String> = match snapshot.get("admitted_node_ids") {
        None | Some(Value::Null) => admit_primary_nodes(&graph, &model, &profile, &policy)?,
        Some(Value::Array(ids)) => {
            let strings: Option<Vec<String>> =
                ids.iter().map(|v| v.as_str().map(str::to_owned)).collect();
            let mut unique = strings.clone().unwrap_or_default();
            unique.sort();
            unique.dedup();
            match strings {
                Some(s) if !s.is_empty() && unique.len() == s.len() => s,
                _ => return verr("admitted_node_ids is invalid"),
            }
        }
        Some(_) => return verr("admitted_node_ids is invalid"),
    };
    let primary = plan_primary(&graph, &model, &profile, &policy, &admitted)?;
    let representative = profile.representative();
    let replicated = replicate_stages(&primary, &graph, &model, representative, &policy)?;
    let mut legal_tracks = Vec::new();
    for (index, track) in replicated.flow.tracks.iter().enumerate() {
        if !(0.0..=1.0).contains(&track.traffic_fraction) || track.cost_ms < 0.0 {
            return verr("invalid legal track");
        }
        legal_tracks.push(LegalTrack {
            track_id: format!("track-{index:03}"),
            placement_ids: track.placement_ids.clone(),
            traffic_fraction: track.traffic_fraction,
            cost_ms: track.cost_ms,
        });
    }
    let mut forward_edges = Vec::new();
    for e in &replicated.forward_edges {
        forward_edges.push(PlanEdge {
            src_placement_id: e.src.clone(),
            dst_placement_id: e.dst.clone(),
            capacity_rps: e.capacity,
            cost_ms: e.cost_ms,
        });
    }
    let loopbacks: Vec<Loopback> = replicated
        .loopback_edges
        .iter()
        .map(|e| Loopback {
            src_placement_id: e.src.clone(),
            dst_placement_id: e.dst.clone(),
            payload_bytes: 16,
            cost_ms: e.cost_ms,
        })
        .collect();
    let mut scenario_metrics = Vec::new();
    for score in &primary.scenario_scores {
        scenario_metrics.push(json!({
            "name": score.scenario_name,
            "ttft_ms": fv(score.ttft_ms)?,
            "tpot_ms": fv(score.tpot_ms)?,
            "single_request_tps": fv(score.single_request_tps)?,
            "output_goodput_tps": fv(score.output_goodput_tps)?,
            "prefill_compute_ms": fv(score.prefill_compute_ms)?,
            "prefill_transfer_ms": num_value(score.prefill_network_ms),
            "decode_compute_ms": fv(score.decode_compute_ms)?,
            "decode_transfer_ms": fv(score.decode_network_ms + score.decode_loopback_ms)?,
            "prefill_payload_bytes": num_value(score.prefill_payload_bytes),
            "decode_payload_bytes": num_value(score.decode_payload_bytes),
            "expected_response_ms": fv(score.expected_response_ms)?,
            "required_memory_bytes": fv(score.required_memory_bytes)?,
            "confidence": num_value(score.confidence),
        }));
    }
    let metrics = json!({
        "scenarios": scenario_metrics,
        "primary_request_capacity_rps": fv(replicated.primary_capacity_rps)?,
        "replicated_request_capacity_rps": fv(replicated.flow.admitted)?,
        "replica_capacity_gain_rps": fv(replicated.flow.admitted - replicated.primary_capacity_rps)?,
        "replicated_output_capacity_tps": fv(replicated.flow.admitted * num_f(representative.output_tokens))?,
        "unmet_capacity_sweep_demand_rps": fv(replicated.flow.unmet_demand)?,
    });
    let speculative = parse_speculative(snapshot.get("speculative").unwrap_or(&Value::Null))?;
    let speculative_diag = match &speculative {
        None => json!({"enabled": false, "target_fallback": true}),
        Some(config) => {
            let timing = snapshot
                .get("speculative_timing")
                .cloned()
                .unwrap_or_else(|| json!({}));
            speculative_diagnostics(config, &timing, primary.scenario_scores[0].tpot_ms)?
        }
    };
    let node_runtime = match snapshot.get("node_runtime") {
        None => json!({}),
        Some(v) if v.is_object() => v.clone(),
        Some(_) => return other("dict(node_runtime) of a non-mapping"),
    };
    let decisions = replicated
        .candidate_decisions
        .iter()
        .map(|d| {
            let mut v = d.to_value();
            for key in [
                "baseline_admitted_rps",
                "proposed_admitted_rps",
                "raw_gain_rps",
                "robust_gain_rps",
                "minimum_required_gain_rps",
            ] {
                v[key] = fv(v[key].as_f64().unwrap_or(f64::NAN))?;
            }
            Ok(v)
        })
        .collect::<PyResult<Vec<_>>>()?;
    let mut excluded = Map::new();
    for (k, v) in &graph.exclusions {
        excluded.insert(k.clone(), v.clone());
    }
    let diagnostics = json!({
        "placement_provenance": snapshot.get("placement_provenance").cloned().unwrap_or_else(|| json!("planner_v2")),
        "decode_mode": snapshot.get("decode_mode").cloned().unwrap_or_else(|| json!("unknown")),
        "node_runtime": node_runtime,
        "topology": "ordered_stage_groups_with_independent_loops_and_cross_edges",
        "primary_order": primary.order,
        "frozen_primary_order": primary.order,
        "candidate_node_ids": primary.candidate_node_ids,
        "admitted_node_ids": admitted,
        "unplaced_node_ids": primary.unplaced_node_ids,
        "accepted_replica_nodes": replicated.accepted_replica_nodes,
        "replication": {
            "authority": "capability_aware_replica_flow_planner",
            "minimum_gain_fraction": policy.echo_gain,
            "uncertainty_fraction": policy.echo_uncertainty,
            "candidate_decisions": decisions,
            "zero_flow_removed_placement_ids": replicated.zero_flow_removed_placement_ids,
            "request_routing_semantics": "one_complete_track_per_request",
            "kv_locality": "request_track_pinned_no_migration",
            "parallelism_label": "data_parallel_request_routing",
            "route_ready": false,
        },
        "excluded_nodes": Value::Object(excluded),
        "workload": {
            "name": profile.name,
            "source": profile.source,
            "mode": profile.mode,
            "arrival_process": profile.arrival_process,
            "user_scale": profile.scenarios[0].user_scale_raw,
            "network_size_changes_capacity_not_demand": true,
        },
        "path_cost": "phase_sensitive_latency_plus_payload_over_bandwidth",
        "primary_search": primary.diagnostics,
        "speculative": speculative_diag,
    });
    let snapshot_digest = crate::digest(snapshot, false)
        .map_err(|_| Failure::other("snapshot is not canonical JSON"))?;
    let plan = RoutePlan {
        model,
        snapshot_digest,
        placements: replicated.placements,
        legal_tracks,
        forward_edges,
        loopbacks,
        provenance: primary.provenance,
        workload_name: profile.name,
        metrics,
        diagnostics,
    };
    if plan.snapshot_digest.is_empty() {
        return verr("snapshot digest is required");
    }
    validate_route_plan(&plan)?;
    Ok(plan)
}
