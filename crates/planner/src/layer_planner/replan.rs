// SPDX-License-Identifier: AGPL-3.0-only
//! Topology-event replanning and the replan simulator (`replanning.py`,
//! `replan_simulator.py`) at f4e366e9. Placement intent only; no runtime authority.
use super::contracts::{NUMERIC_EPSILON, other, py_float, py_int, py_iter};
use super::plan::{RoutePlan, plan_snapshot};
use crate::py::{Failure, Kind, PyResult, get, has, py_str, verr};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

pub const BUNDLE_PROTOCOL: &str = "mycelium.layer_replan_simulation.v1";
pub const REPORT_PROTOCOL: &str = "mycelium.layer_replan_simulation_report.v1";
pub const OUTCOME_PROTOCOL: &str = "mycelium.layer_replan.v1";
pub const ASSESSMENT_PROTOCOL: &str = "mycelium.layer_replan_assessment.v1";
const EVENT_KINDS: [&str; 4] = [
    "device_unavailable",
    "edge_unavailable",
    "device_joined",
    "measurement_drift",
];

fn blank(s: &str) -> bool {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        .is_empty()
}

#[derive(Debug, Clone)]
pub struct TopologyEvent {
    pub event_id: String,
    pub snapshot_generation: i128,
    pub kind: String,
    pub node_ids: Vec<String>,
    pub edges: Vec<(String, String)>,
}

impl TopologyEvent {
    /// `replan_simulator._event_from_dict` followed by `TopologyEvent.__post_init__`.
    pub fn from_value(data: &Value) -> PyResult<Self> {
        let edges_raw: Vec<Value> = match get(data, "edges")? {
            Value::Null if !has(data, "edges") => Vec::new(),
            v => py_iter(v)?,
        };
        let mut pairs = Vec::new();
        for edge in &edges_raw {
            pairs.push(py_iter(edge)?);
        }
        if pairs.iter().any(|e| e.len() != 2) {
            return verr("each topology-event edge must contain src and dst");
        }
        let event_id = py_str(crate::py::req(data, "event_id")?);
        let snapshot_generation = py_int(crate::py::req(data, "snapshot_generation")?)?;
        let kind = py_str(crate::py::req(data, "kind")?);
        let node_ids: Vec<String> = match get(data, "node_ids")? {
            Value::Null if !has(data, "node_ids") => Vec::new(),
            v => py_iter(v)?.iter().map(py_str).collect(),
        };
        let edges: Vec<(String, String)> = pairs
            .iter()
            .map(|e| (py_str(&e[0]), py_str(&e[1])))
            .collect();
        Self::new(event_id, snapshot_generation, kind, node_ids, edges)
    }

    pub fn new(
        event_id: String,
        snapshot_generation: i128,
        kind: String,
        node_ids: Vec<String>,
        edges: Vec<(String, String)>,
    ) -> PyResult<Self> {
        if blank(&event_id) {
            return verr("event_id must be a non-empty string");
        }
        if snapshot_generation < 0 {
            return verr("snapshot_generation must be a non-negative integer");
        }
        if !EVENT_KINDS.contains(&kind.as_str()) {
            return verr(format!("unsupported topology event kind: {kind}"));
        }
        if node_ids.iter().any(String::is_empty) {
            return verr("node_ids must contain non-empty strings");
        }
        let unique: BTreeSet<&String> = node_ids.iter().collect();
        if unique.len() != node_ids.len() {
            return verr("node_ids must be unique");
        }
        let mut node_ids = node_ids;
        node_ids.sort();
        for (src, dst) in &edges {
            if src.is_empty() || dst.is_empty() {
                return verr("edge endpoints must be non-empty strings");
            }
            if src == dst {
                return verr("topology-event edges must be directed between distinct nodes");
            }
        }
        let unique_edges: BTreeSet<&(String, String)> = edges.iter().collect();
        if unique_edges.len() != edges.len() {
            return verr("edges must be unique");
        }
        let mut edges = edges;
        edges.sort();
        match kind.as_str() {
            "device_unavailable" => {
                if node_ids.is_empty() {
                    return verr("device_unavailable requires at least one node_id");
                }
                if !edges.is_empty() {
                    return verr("device_unavailable cannot carry directed edges");
                }
            }
            "edge_unavailable" => {
                if edges.is_empty() {
                    return verr("edge_unavailable requires at least one directed edge");
                }
                if !node_ids.is_empty() {
                    return verr("edge_unavailable cannot carry node_ids");
                }
            }
            "device_joined" => {
                if node_ids.is_empty() {
                    return verr("device_joined requires at least one node_id");
                }
                if !edges.is_empty() {
                    return verr("device_joined link facts belong in the snapshot");
                }
            }
            _ => {
                if node_ids.is_empty() && edges.is_empty() {
                    return verr("measurement_drift requires a node_id or directed edge");
                }
            }
        }
        Ok(Self {
            event_id,
            snapshot_generation,
            kind,
            node_ids,
            edges,
        })
    }

    pub fn to_value(&self) -> Value {
        json!({
            "edges": self.edges.iter().map(|(a, b)| json!([a, b])).collect::<Vec<_>>(),
            "event_id": self.event_id,
            "kind": self.kind,
            "node_ids": self.node_ids,
            "snapshot_generation": i64::try_from(self.snapshot_generation).unwrap_or(i64::MAX),
        })
    }
}

#[derive(Debug, Clone)]
pub struct Assessment {
    pub action: &'static str,
    pub urgency: &'static str,
    pub surviving_track_ids: Vec<String>,
    pub affected_group_ids: Vec<String>,
    pub escalation_order: Vec<&'static str>,
    pub external_readiness_required: bool,
    pub reason: &'static str,
}

fn track_node_edges(plan: &RoutePlan, placement_ids: &[String]) -> PyResult<Vec<(String, String)>> {
    let nodes: BTreeMap<&str, &str> = plan
        .placements
        .iter()
        .map(|p| (p.placement_id.as_str(), p.node_id.as_str()))
        .collect();
    let mut seq = Vec::new();
    for id in placement_ids {
        let Some(node) = nodes.get(id.as_str()) else {
            return Err(Failure::other(format!("KeyError: {id}")));
        };
        seq.push((*node).to_owned());
    }
    if seq.len() < 2 {
        return Ok(Vec::new());
    }
    let mut edges: Vec<(String, String)> = seq
        .windows(2)
        .map(|w| (w[0].clone(), w[1].clone()))
        .collect();
    edges.push((seq[seq.len() - 1].clone(), seq[0].clone()));
    Ok(edges)
}

pub fn assess_topology_event(plan: &RoutePlan, event: &TopologyEvent) -> PyResult<Assessment> {
    let all_tracks: Vec<String> = plan
        .legal_tracks
        .iter()
        .map(|t| t.track_id.clone())
        .collect();
    if event.kind == "device_joined" || event.kind == "measurement_drift" {
        return Ok(Assessment {
            action: "candidate_replan",
            urgency: "deferred",
            surviving_track_ids: all_tracks,
            affected_group_ids: Vec::new(),
            escalation_order: vec!["full_replan"],
            external_readiness_required: true,
            reason: "new or changed fleet facts merit a hysteresis-gated candidate plan",
        });
    }
    let unavailable_nodes: BTreeSet<&String> = event.node_ids.iter().collect();
    let unavailable_edges: BTreeSet<&(String, String)> = event.edges.iter().collect();
    let unavailable_placements: BTreeSet<&str> = plan
        .placements
        .iter()
        .filter(|p| unavailable_nodes.contains(&p.node_id))
        .map(|p| p.placement_id.as_str())
        .collect();
    let affected: Vec<String> = plan
        .placements
        .iter()
        .filter(|p| unavailable_placements.contains(p.placement_id.as_str()))
        .map(|p| p.replica_group_id.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let mut surviving = Vec::new();
    let mut used_by_route = !unavailable_placements.is_empty();
    for track in &plan.legal_tracks {
        let uses_node = track
            .placement_ids
            .iter()
            .any(|p| unavailable_placements.contains(p.as_str()));
        let node_edges = track_node_edges(plan, &track.placement_ids)?;
        let uses_edge = node_edges.iter().any(|e| unavailable_edges.contains(e));
        used_by_route = used_by_route || uses_edge;
        if !uses_node && !uses_edge {
            surviving.push(track.track_id.clone());
        }
    }
    if !used_by_route {
        return Ok(Assessment {
            action: "no_action",
            urgency: "none",
            surviving_track_ids: all_tracks,
            affected_group_ids: affected,
            escalation_order: Vec::new(),
            external_readiness_required: false,
            reason: "topology event does not affect any current legal track",
        });
    }
    if !surviving.is_empty() {
        return Ok(Assessment {
            action: "existing_track_intent",
            urgency: "immediate",
            surviving_track_ids: surviving,
            affected_group_ids: affected,
            escalation_order: Vec::new(),
            external_readiness_required: true,
            reason: "an unaffected legal placement-intent track survives; runtime readiness remains external",
        });
    }
    let mut starts: BTreeMap<&str, i64> = BTreeMap::new();
    for p in &plan.placements {
        let entry = starts.entry(&p.replica_group_id).or_insert(p.start);
        if p.start < *entry {
            *entry = p.start;
        }
    }
    let mut ordered: Vec<(&str, i64)> = starts.into_iter().collect();
    ordered.sort_by(|a, b| (a.1, a.0).cmp(&(b.1, b.0)));
    let mut standby = false;
    if event.kind == "device_unavailable" && event.node_ids.len() == 1 && affected.len() == 1 {
        let index = ordered
            .iter()
            .position(|(g, _)| *g == affected[0])
            .ok_or_else(|| Failure::value("tuple.index(x): x not in tuple"))?;
        standby = 0 < index && index + 1 < ordered.len();
    }
    Ok(Assessment {
        action: "full_replan",
        urgency: "immediate",
        surviving_track_ids: Vec::new(),
        affected_group_ids: affected,
        escalation_order: if standby {
            vec!["successor_standby_candidate", "full_replan"]
        } else {
            vec!["full_replan"]
        },
        external_readiness_required: true,
        reason: "no complete legal placement-intent track survives the topology event",
    })
}

fn capacity(plan: &RoutePlan) -> PyResult<f64> {
    let value = plan
        .metrics
        .get("replicated_request_capacity_rps")
        .and_then(crate::py::float)
        .ok_or_else(|| Failure::value("plan lacks finite replicated_request_capacity_rps"))?;
    if !value.is_finite() || value < 0.0 {
        return verr("replicated_request_capacity_rps must be finite and non-negative");
    }
    Ok(value)
}

fn snapshot_for_event(snapshot: &Value, event: &TopologyEvent) -> PyResult<Value> {
    let Some(map) = snapshot.as_object() else {
        return other("dict(snapshot) of a non-mapping");
    };
    let mut candidate = Value::Object(map.clone());
    let (nodes_ok, links_ok) = (
        candidate.get("nodes").is_some_and(Value::is_array),
        candidate.get("links").is_some_and(Value::is_array),
    );
    if !nodes_ok || !links_ok {
        return verr("snapshot nodes and links must be arrays");
    }
    let node_ids: BTreeSet<String> = candidate["nodes"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|n| n.get("node_id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    if event.kind == "device_joined" {
        let missing: Vec<&String> = event
            .node_ids
            .iter()
            .filter(|n| !node_ids.contains(*n))
            .collect();
        if !missing.is_empty() {
            let names: Vec<&str> = missing.iter().map(|s| s.as_str()).collect();
            return verr(format!(
                "joined nodes missing from candidate snapshot: {}",
                names.join(", ")
            ));
        }
    }
    let unavailable: BTreeSet<&String> = if event.kind == "device_unavailable" {
        event.node_ids.iter().collect()
    } else {
        BTreeSet::new()
    };
    if let Some(nodes) = candidate.get_mut("nodes").and_then(Value::as_array_mut) {
        for node in nodes {
            let Some(obj) = node.as_object_mut() else {
                return verr("snapshot nodes must be objects");
            };
            let hit = obj
                .get("node_id")
                .and_then(Value::as_str)
                .is_some_and(|id| unavailable.iter().any(|u| u.as_str() == id));
            if hit {
                obj.insert("eligible".into(), json!(false));
                obj.insert(
                    "exclusion_reason".into(),
                    json!(format!(
                        "topology_event:{}:device_unavailable",
                        event.event_id
                    )),
                );
            }
        }
    }
    if event.kind == "edge_unavailable" && !event.edges.is_empty() {
        let mut filtered = Vec::new();
        for link in candidate["links"].as_array().cloned().unwrap_or_default() {
            let Some(obj) = link.as_object() else {
                return verr("snapshot links must be objects");
            };
            let edge = (
                obj.get("src").and_then(Value::as_str).map(str::to_owned),
                obj.get("dst").and_then(Value::as_str).map(str::to_owned),
            );
            let blocked = match &edge {
                (Some(a), Some(b)) => event.edges.iter().any(|(x, y)| x == a && y == b),
                _ => false,
            };
            if !blocked {
                filtered.push(link);
            }
        }
        candidate["links"] = Value::Array(filtered);
    }
    Ok(candidate)
}

#[derive(Debug, Clone)]
pub struct Outcome {
    pub event: TopologyEvent,
    pub assessment: Assessment,
    pub previous_plan_digest: String,
    pub candidate_plan: Option<RoutePlan>,
    pub candidate_plan_digest: Option<String>,
    pub capacity_gain_fraction: Option<f64>,
    pub recommendation: &'static str,
    pub reason: String,
}

impl Outcome {
    fn checked(self) -> PyResult<Self> {
        if self.capacity_gain_fraction.is_some_and(|g| !g.is_finite()) {
            return verr("capacity_gain_fraction must be finite");
        }
        Ok(self)
    }

    /// `replan_outcome_to_dict`.
    pub fn to_value(&self) -> PyResult<Value> {
        let candidate = match &self.candidate_plan {
            Some(plan) => plan.to_value()?,
            None => Value::Null,
        };
        let a = &self.assessment;
        Ok(json!({
            "assessment": {
                "action": a.action,
                "affected_group_ids": a.affected_group_ids,
                "escalation_order": a.escalation_order,
                "external_readiness_required": a.external_readiness_required,
                "protocol": ASSESSMENT_PROTOCOL,
                "reason": a.reason,
                "surviving_track_ids": a.surviving_track_ids,
                "urgency": a.urgency,
            },
            "candidate_plan": candidate,
            "candidate_plan_digest": self.candidate_plan_digest,
            "capacity_gain_fraction": self.capacity_gain_fraction,
            "event": self.event.to_value(),
            "handoff_state": "placement_intent_only",
            "previous_plan_digest": self.previous_plan_digest,
            "protocol": OUTCOME_PROTOCOL,
            "reason": self.reason,
            "recommendation": self.recommendation,
        }))
    }
}

/// `replan_for_event(previous_plan, snapshot, event, min_capacity_gain_fraction=...)`.
pub fn replan_for_event(
    previous_plan: &RoutePlan,
    snapshot: &Value,
    event: &TopologyEvent,
    min_capacity_gain_fraction: f64,
) -> PyResult<Outcome> {
    let threshold = min_capacity_gain_fraction;
    if !threshold.is_finite() || threshold < 0.0 {
        return verr("min_capacity_gain_fraction must be finite and non-negative");
    }
    let assessment = assess_topology_event(previous_plan, event)?;
    let previous_digest = previous_plan.digest()?;
    let simple = |recommendation: &'static str, assessment: Assessment| Outcome {
        event: event.clone(),
        previous_plan_digest: previous_digest.clone(),
        candidate_plan: None,
        candidate_plan_digest: None,
        capacity_gain_fraction: None,
        recommendation,
        reason: assessment.reason.to_owned(),
        assessment,
    };
    if assessment.action == "no_action" {
        return simple("retain_current_plan", assessment).checked();
    }
    if assessment.action == "existing_track_intent" {
        return simple("router_builder_validate_existing_track", assessment).checked();
    }
    let current_capacity = capacity(previous_plan)?;
    let candidate_snapshot = snapshot_for_event(snapshot, event)?;
    let candidate = match plan_snapshot(&candidate_snapshot) {
        Ok(plan) => plan,
        Err(e) if e.kind == Kind::Value => {
            return Outcome {
                event: event.clone(),
                assessment,
                previous_plan_digest: previous_digest,
                candidate_plan: None,
                candidate_plan_digest: None,
                capacity_gain_fraction: None,
                recommendation: "no_viable_plan",
                reason: format!("planner found no viable candidate: {}", e.message),
            }
            .checked();
        }
        Err(e) => return Err(e),
    };
    let candidate_capacity = capacity(&candidate)?;
    let denominator = if NUMERIC_EPSILON > current_capacity {
        NUMERIC_EPSILON
    } else {
        current_capacity
    };
    let gain = (candidate_capacity - current_capacity) / denominator;
    let recommendation = if assessment.action == "full_replan" {
        if assessment
            .escalation_order
            .contains(&"successor_standby_candidate")
        {
            "prefer_route_ready_successor_standby_else_provision_candidate"
        } else {
            "provision_replanned_intent"
        }
    } else if gain >= threshold {
        "provision_candidate"
    } else {
        "retain_current_plan"
    };
    let digest = candidate.digest()?;
    Outcome {
        event: event.clone(),
        assessment,
        previous_plan_digest: previous_digest,
        candidate_plan: Some(candidate),
        candidate_plan_digest: Some(digest),
        capacity_gain_fraction: Some(gain),
        recommendation,
        reason: "fresh placement intent computed from the supplied topology snapshot".to_owned(),
    }
    .checked()
}

// ---------------------------------------------------------------- simulator
fn extend_snapshot(snapshot: &mut Value, case: &Value) -> PyResult<()> {
    let add_nodes = if has(case, "add_nodes") {
        get(case, "add_nodes")?.clone()
    } else {
        json!([])
    };
    let add_links = if has(case, "add_links") {
        get(case, "add_links")?.clone()
    } else {
        json!([])
    };
    let (Some(nodes), Some(links)) = (add_nodes.as_array(), add_links.as_array()) else {
        return verr("add_nodes and add_links must be arrays");
    };
    for (key, extra) in [("nodes", nodes), ("links", links)] {
        match snapshot.get_mut(key).and_then(Value::as_array_mut) {
            Some(list) => list.extend(extra.iter().cloned()),
            None => return other("snapshot list extend on a non-list"),
        }
    }
    Ok(())
}

/// `simulate_bundle` over an already loaded bundle and base snapshot (no file access).
pub fn simulate_bundle(bundle: &Value, base_snapshot: &Value) -> PyResult<Value> {
    let base_plan = plan_snapshot(base_snapshot)?;
    let cases = match bundle.get("cases") {
        Some(Value::Array(a)) if !a.is_empty() => a,
        _ => return verr("cases must be a non-empty array"),
    };
    let mut reports = Vec::new();
    let mut seen = BTreeSet::new();
    for raw_case in cases {
        if !raw_case.is_object() {
            return verr("each simulation case must be an object");
        }
        let name = match raw_case.get("name") {
            Some(Value::String(s)) if !s.is_empty() && !seen.contains(s) => s.clone(),
            _ => return verr("case names must be non-empty and unique"),
        };
        seen.insert(name.clone());
        let raw_event = raw_case.get("event").unwrap_or(&Value::Null);
        if !raw_event.is_object() {
            return verr(format!("{name}: event must be an object"));
        }
        let event = TopologyEvent::from_value(raw_event)?;
        let mut event_snapshot = base_snapshot.clone();
        extend_snapshot(&mut event_snapshot, raw_case)?;
        let threshold = match raw_case.get("min_capacity_gain_fraction") {
            None => 0.05,
            Some(v) => py_float(v)?,
        };
        let outcome = replan_for_event(&base_plan, &event_snapshot, &event, threshold)?;
        let a = &outcome.assessment;
        let candidate = match &outcome.candidate_plan {
            Some(plan) => plan.to_value()?,
            None => Value::Null,
        };
        reports.push(json!({
            "action": a.action,
            "affected_group_ids": a.affected_group_ids,
            "candidate_plan": candidate,
            "candidate_plan_digest": outcome.candidate_plan_digest,
            "capacity_gain_fraction": outcome.capacity_gain_fraction,
            "escalation_order": a.escalation_order,
            "event_id": event.event_id,
            "external_readiness_required": a.external_readiness_required,
            "handoff_state": "placement_intent_only",
            "name": name,
            "reason": outcome.reason,
            "recommendation": outcome.recommendation,
            "snapshot_generation": i64::try_from(event.snapshot_generation).unwrap_or(i64::MAX),
            "surviving_track_ids": a.surviving_track_ids,
            "urgency": a.urgency,
        }));
    }
    Ok(json!({
        "base_plan_digest": base_plan.digest()?,
        "cases": reports,
        "handoff_state": "placement_intent_only",
        "protocol": REPORT_PROTOCOL,
    }))
}

fn load_object(path: &Path) -> PyResult<Value> {
    let text = std::fs::read_to_string(path).map_err(|e| {
        Failure::other(format!(
            "FileNotFoundError/OSError: {}: {e}",
            path.display()
        ))
    })?;
    let value: Value = serde_json::from_str(&text)
        .map_err(|e| Failure::value(format!("{}: invalid JSON: {e}", path.display())))?;
    if !value.is_object() {
        return verr(format!("{}: JSON root must be an object", path.display()));
    }
    Ok(value)
}

fn resolve(path: &Path) -> PathBuf {
    if let Ok(real) = std::fs::canonicalize(path) {
        return real;
    }
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// `simulate_bundle(path)`: the scenario bundle file and its base snapshot beside it.
pub fn simulate_bundle_file(path: &Path) -> PyResult<Value> {
    let bundle_path = resolve(path);
    let bundle = load_object(&bundle_path)?;
    if bundle.get("protocol") != Some(&json!(BUNDLE_PROTOCOL)) {
        return verr(format!("bundle protocol must be {BUNDLE_PROTOCOL}"));
    }
    let Some(base_name) = bundle
        .get("base_snapshot")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    else {
        return verr("base_snapshot must be a non-empty path");
    };
    let base_dir = bundle_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_default();
    let base_path = resolve(&base_dir.join(base_name));
    if !base_path.starts_with(&base_dir) {
        return verr("base_snapshot must stay inside the scenario directory");
    }
    let base_snapshot = load_object(&base_path)?;
    simulate_bundle(&bundle, &base_snapshot)
}
