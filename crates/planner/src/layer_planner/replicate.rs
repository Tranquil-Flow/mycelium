// SPDX-License-Identifier: AGPL-3.0-only
//! Flow assignment and capability-aware replication (`flow.py`, `replication.py`) at f4e366e9.
use super::contracts::{
    Graph, ModelIdentity, NUMERIC_EPSILON, Policy, Scenario, num_f, py_sum, transfer_time_ms,
};
use super::search::{PrimaryPlan, stage_cost};
use crate::py::{Num, PyResult, fsum, py_repr, verr};
use serde_json::{Value, json};
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

/// Python `max(0.0, x)` (returns the first argument on ties, including `-0.0`).
pub fn max0(x: f64) -> f64 {
    if x > 0.0 { x } else { 0.0 }
}

#[derive(Debug, Clone)]
pub struct Placement {
    pub placement_id: String,
    pub replica_group_id: String,
    pub node_id: String,
    pub start: i64,
    pub end: i64,
    pub primary: bool,
    pub service_capacity_rps: f64,
}

impl Placement {
    pub fn checked(self) -> PyResult<Self> {
        if self.placement_id.is_empty()
            || self.replica_group_id.is_empty()
            || self.node_id.is_empty()
        {
            return verr("placement identifiers are required");
        }
        if self.service_capacity_rps < 0.0 {
            return verr("service capacity must be non-negative");
        }
        Ok(self)
    }
}

#[derive(Debug, Clone)]
pub struct FlowEdge {
    pub src: String,
    pub dst: String,
    pub capacity: f64,
    pub cost_ms: f64,
}

impl FlowEdge {
    fn checked(self) -> PyResult<Self> {
        if self.src.is_empty() || self.dst.is_empty() || self.src == self.dst {
            return verr("flow edge requires distinct endpoints");
        }
        if self.capacity < 0.0 || self.cost_ms < 0.0 {
            return verr("flow edge capacity and cost must be non-negative");
        }
        Ok(self)
    }
}

#[derive(Debug, Clone)]
pub struct FlowTrack {
    pub placement_ids: Vec<String>,
    pub amount: f64,
    pub traffic_fraction: f64,
    pub cost_ms: f64,
}

#[derive(Debug, Clone)]
pub struct Flow {
    pub admitted: f64,
    pub unmet_demand: f64,
    pub tracks: Vec<FlowTrack>,
}

type EdgeMap = BTreeMap<(String, String), FlowEdge>;
type Path = (f64, Vec<String>);

fn edge_map(edges: &[FlowEdge]) -> PyResult<EdgeMap> {
    let mut out = BTreeMap::new();
    for edge in edges {
        let key = (edge.src.clone(), edge.dst.clone());
        if out.contains_key(&key) {
            return verr(format!(
                "duplicate flow edge ({}, {})",
                py_repr(&json!(key.0)),
                py_repr(&json!(key.1))
            ));
        }
        out.insert(key, edge.clone());
    }
    Ok(out)
}

fn path_less(a: &Path, b: &Path) -> bool {
    match a.0.partial_cmp(&b.0) {
        Some(Ordering::Less) => true,
        Some(Ordering::Equal) => a.1 < b.1,
        _ => false,
    }
}

fn residual_of(map: &BTreeMap<String, f64>, key: &str) -> f64 {
    map.get(key).copied().unwrap_or(0.0)
}

fn shortest_complete_loop(
    groups: &[Vec<String>],
    placement_residual: &BTreeMap<String, f64>,
    forward: &EdgeMap,
    forward_residual: &BTreeMap<(String, String), f64>,
    loopbacks: &EdgeMap,
    loopback_residual: &BTreeMap<(String, String), f64>,
) -> Option<Path> {
    let mut candidates: Vec<Path> = Vec::new();
    for first in &groups[0] {
        if residual_of(placement_residual, first) <= NUMERIC_EPSILON {
            continue;
        }
        let mut states: Vec<(String, Path)> = vec![(first.clone(), (0.0, vec![first.clone()]))];
        for group in &groups[1..] {
            let mut next_states: Vec<(String, Path)> = Vec::new();
            for dst in group {
                if residual_of(placement_residual, dst) <= NUMERIC_EPSILON {
                    continue;
                }
                let mut best: Option<Path> = None;
                for (src, state) in &states {
                    let key = (src.clone(), dst.clone());
                    let Some(edge) = forward.get(&key) else {
                        continue;
                    };
                    if forward_residual.get(&key).copied().unwrap_or(0.0) <= NUMERIC_EPSILON {
                        continue;
                    }
                    let mut path = state.1.clone();
                    path.push(dst.clone());
                    let candidate = (state.0 + edge.cost_ms, path);
                    if best.as_ref().is_none_or(|b| path_less(&candidate, b)) {
                        best = Some(candidate);
                    }
                }
                if let Some(best) = best {
                    next_states.push((dst.clone(), best));
                }
            }
            states = next_states;
            if states.is_empty() {
                break;
            }
        }
        for (last, state) in &states {
            if groups.len() == 1 && last == first {
                candidates.push(state.clone());
                continue;
            }
            let key = (last.clone(), first.clone());
            if let Some(loop_edge) = loopbacks.get(&key)
                && loopback_residual.get(&key).copied().unwrap_or(0.0) > NUMERIC_EPSILON
            {
                candidates.push((state.0 + loop_edge.cost_ms, state.1.clone()));
            }
        }
    }
    candidates
        .into_iter()
        .reduce(|a, b| if path_less(&b, &a) { b } else { a })
}

pub fn assign_flow(
    groups: &[Vec<String>],
    placements: &BTreeMap<String, Placement>,
    forward_edges: &[FlowEdge],
    loopback_edges: &[FlowEdge],
    demand: f64,
) -> PyResult<Flow> {
    if demand < 0.0 {
        return verr("demand must be non-negative");
    }
    let ordered: Vec<Vec<String>> = groups
        .iter()
        .map(|g| {
            let mut g = g.clone();
            g.sort();
            g
        })
        .collect();
    if ordered.is_empty() || ordered.iter().any(Vec::is_empty) {
        return verr("stage groups must be non-empty");
    }
    let flattened: Vec<&String> = ordered.iter().flatten().collect();
    let unique: BTreeSet<&&String> = flattened.iter().collect();
    if unique.len() != flattened.len() || flattened.iter().any(|p| !placements.contains_key(*p)) {
        return verr("groups must reference each placement exactly once");
    }
    let forward = edge_map(forward_edges)?;
    let loops = edge_map(loopback_edges)?;
    let mut placement_residual: BTreeMap<String, f64> = flattened
        .iter()
        .map(|p| ((*p).clone(), placements[*p].service_capacity_rps))
        .collect();
    let mut forward_residual: BTreeMap<(String, String), f64> = forward
        .iter()
        .map(|(k, e)| (k.clone(), e.capacity))
        .collect();
    let mut loop_residual: BTreeMap<(String, String), f64> =
        loops.iter().map(|(k, e)| (k.clone(), e.capacity)).collect();
    let mut remaining = demand;
    let mut raw_tracks: Vec<(Vec<String>, f64, f64)> = Vec::new();
    while remaining > NUMERIC_EPSILON {
        let Some((cost, path)) = shortest_complete_loop(
            &ordered,
            &placement_residual,
            &forward,
            &forward_residual,
            &loops,
            &loop_residual,
        ) else {
            break;
        };
        let mut capacities: Vec<f64> = path.iter().map(|p| placement_residual[p]).collect();
        capacities.extend(
            path.windows(2)
                .map(|w| forward_residual[&(w[0].clone(), w[1].clone())]),
        );
        if path.len() > 1 {
            capacities.push(loop_residual[&(path[path.len() - 1].clone(), path[0].clone())]);
        }
        let amount = capacities
            .iter()
            .fold(remaining, |a, b| if *b < a { *b } else { a });
        if amount <= NUMERIC_EPSILON || !amount.is_finite() {
            break;
        }
        for pid in &path {
            if let Some(r) = placement_residual.get_mut(pid) {
                *r -= amount;
            }
        }
        for w in path.windows(2) {
            if let Some(r) = forward_residual.get_mut(&(w[0].clone(), w[1].clone())) {
                *r -= amount;
            }
        }
        if path.len() > 1
            && let Some(r) = loop_residual.get_mut(&(path[path.len() - 1].clone(), path[0].clone()))
        {
            *r -= amount;
        }
        raw_tracks.push((path, amount, cost));
        remaining -= amount;
    }
    let admitted = fsum(raw_tracks.iter().map(|t| t.1));
    let remaining = max0(demand - admitted);
    let mut tracks = Vec::new();
    let mut assigned_fraction = 0.0;
    for (index, (path, amount, cost)) in raw_tracks.iter().enumerate() {
        let fraction = if admitted == 0.0 {
            0.0
        } else if index == raw_tracks.len() - 1 {
            let f = 1.0 - assigned_fraction;
            max0(if f < 1.0 { f } else { 1.0 })
        } else {
            let f = amount / admitted;
            let f = max0(if f < 1.0 { f } else { 1.0 });
            assigned_fraction = fsum([assigned_fraction, f]);
            f
        };
        tracks.push(FlowTrack {
            placement_ids: path.clone(),
            amount: *amount,
            traffic_fraction: fraction,
            cost_ms: *cost,
        });
    }
    Ok(Flow {
        admitted,
        unmet_demand: remaining,
        tracks,
    })
}

// ---------------------------------------------------------------- replication
#[derive(Debug, Clone)]
pub struct Decision {
    pub iteration: i64,
    pub placement_id: String,
    pub node_id: String,
    pub replica_group_id: String,
    pub accepted: bool,
    pub reason: String,
    pub baseline_admitted_rps: f64,
    pub proposed_admitted_rps: f64,
    pub raw_gain_rps: f64,
    pub robust_gain_rps: f64,
    pub minimum_required_gain_rps: f64,
    pub failure_domain: Value,
    pub failure_domain_warning: Option<&'static str>,
}

impl Decision {
    pub fn to_value(&self) -> Value {
        json!({
            "iteration": self.iteration,
            "placement_id": self.placement_id,
            "node_id": self.node_id,
            "replica_group_id": self.replica_group_id,
            "accepted": self.accepted,
            "reason": self.reason,
            "baseline_admitted_rps": self.baseline_admitted_rps,
            "proposed_admitted_rps": self.proposed_admitted_rps,
            "raw_gain_rps": self.raw_gain_rps,
            "robust_gain_rps": self.robust_gain_rps,
            "minimum_required_gain_rps": self.minimum_required_gain_rps,
            "failure_domain": self.failure_domain,
            "failure_domain_warning": self.failure_domain_warning,
        })
    }
}

#[derive(Debug, Clone)]
pub struct Replicated {
    pub placements: Vec<Placement>,
    pub forward_edges: Vec<FlowEdge>,
    pub loopback_edges: Vec<FlowEdge>,
    pub flow: Flow,
    pub accepted_replica_nodes: Vec<String>,
    pub candidate_decisions: Vec<Decision>,
    pub zero_flow_removed_placement_ids: Vec<String>,
    pub primary_capacity_rps: f64,
}

fn request_edge(
    graph: &Graph,
    src: &str,
    dst: &str,
    model: &ModelIdentity,
    workload: &Scenario,
    policy: &Policy,
    loopback: bool,
) -> PyResult<Option<(f64, f64)>> {
    if src == dst {
        return Ok(Some((1e12, 0.0)));
    }
    let Some(link) = graph.link(src, dst) else {
        return Ok(None);
    };
    let output = num_f(workload.output_tokens);
    let total_ms = if loopback {
        output * transfer_time_ms(link, 16.0, policy)?.total_ms
    } else {
        let prefill = transfer_time_ms(
            link,
            num_f(model.activation_bytes(workload.effective_prompt_tokens())?),
            policy,
        )?
        .total_ms;
        let decode =
            transfer_time_ms(link, num_f(model.activation_bytes(Num::Int(1))?), policy)?.total_ms;
        prefill + output * decode
    };
    let capacity = if total_ms > 0.0 {
        1000.0 / total_ms
    } else {
        1e12
    };
    Ok(Some((capacity, total_ms)))
}

type Edges = (Vec<FlowEdge>, Vec<FlowEdge>);

fn build_edges(
    groups: &[Vec<String>],
    placements: &BTreeMap<String, Placement>,
    graph: &Graph,
    model: &ModelIdentity,
    workload: &Scenario,
    policy: &Policy,
) -> PyResult<Edges> {
    let mut forward = Vec::new();
    for pair in groups.windows(2) {
        for src in &pair[0] {
            for dst in &pair[1] {
                if let Some((capacity, cost)) = request_edge(
                    graph,
                    &placements[src].node_id,
                    &placements[dst].node_id,
                    model,
                    workload,
                    policy,
                    false,
                )? {
                    forward.push(
                        FlowEdge {
                            src: src.clone(),
                            dst: dst.clone(),
                            capacity,
                            cost_ms: cost,
                        }
                        .checked()?,
                    );
                }
            }
        }
    }
    let mut loops = Vec::new();
    if groups.len() > 1 {
        for src in &groups[groups.len() - 1] {
            for dst in &groups[0] {
                if let Some((capacity, cost)) = request_edge(
                    graph,
                    &placements[src].node_id,
                    &placements[dst].node_id,
                    model,
                    workload,
                    policy,
                    true,
                )? {
                    loops.push(
                        FlowEdge {
                            src: src.clone(),
                            dst: dst.clone(),
                            capacity,
                            cost_ms: cost,
                        }
                        .checked()?,
                    );
                }
            }
        }
    }
    forward.sort_by(|a, b| (&a.src, &a.dst).cmp(&(&b.src, &b.dst)));
    loops.sort_by(|a, b| (&a.src, &a.dst).cmp(&(&b.src, &b.dst)));
    Ok((forward, loops))
}

fn solve(
    groups: &[Vec<String>],
    placements: &BTreeMap<String, Placement>,
    graph: &Graph,
    model: &ModelIdentity,
    workload: &Scenario,
    policy: &Policy,
) -> PyResult<(Vec<FlowEdge>, Vec<FlowEdge>, Flow)> {
    let (forward, loops) = build_edges(groups, placements, graph, model, workload, policy)?;
    let demand = py_sum(groups[0].iter().map(|p| placements[p].service_capacity_rps));
    let flow = assign_flow(groups, placements, &forward, &loops, demand)?;
    Ok((forward, loops, flow))
}

fn candidate_greater(a: &(f64, f64, f64, String), b: &(f64, f64, f64, String)) -> bool {
    for (x, y) in [(a.0, b.0), (a.1, b.1), (a.2, b.2)] {
        match x.partial_cmp(&y) {
            Some(Ordering::Greater) => return true,
            Some(Ordering::Less) | None => return false,
            Some(Ordering::Equal) => {}
        }
    }
    a.3 > b.3
}

struct Candidate {
    key: (f64, f64, f64, String),
    proposal: Placement,
    groups: Vec<Vec<String>>,
    forward: Vec<FlowEdge>,
    loops: Vec<FlowEdge>,
    flow: Flow,
}

pub fn replicate_stages(
    primary: &PrimaryPlan,
    graph: &Graph,
    model: &ModelIdentity,
    workload: &Scenario,
    policy: &Policy,
) -> PyResult<Replicated> {
    let mut placements: BTreeMap<String, Placement> = BTreeMap::new();
    let mut groups: Vec<Vec<String>> = Vec::new();
    for stage in &primary.allocation.stages {
        let group_id = format!("stage-{:03}", stage.stage_index);
        let placement_id = format!("{group_id}-primary");
        let capacity = if stage.cost.service_work_ms > 0.0 {
            1000.0 / stage.cost.service_work_ms
        } else {
            1e12
        };
        placements.insert(
            placement_id.clone(),
            Placement {
                placement_id: placement_id.clone(),
                replica_group_id: group_id,
                node_id: stage.node_id.clone(),
                start: stage.start,
                end: stage.end,
                primary: true,
                service_capacity_rps: capacity,
            }
            .checked()?,
        );
        groups.push(vec![placement_id]);
    }
    let mut unused: BTreeSet<String> = primary.unplaced_node_ids.iter().cloned().collect();
    let mut accepted: Vec<String> = Vec::new();
    let (mut forward, mut loops, mut current) =
        solve(&groups, &placements, graph, model, workload, policy)?;
    let primary_capacity_rps = current.admitted;
    let mut decisions: Vec<Decision> = Vec::new();
    let mut iterations = 0_i64;
    let min_gain_fraction = num_f(policy.minimum_replica_gain_fraction);
    let uncertainty_fraction = num_f(policy.replica_uncertainty_fraction);
    while !unused.is_empty() && (iterations as f64) < policy.replica_budget {
        let mut best: Option<Candidate> = None;
        let mut evaluated: Vec<Decision> = Vec::new();
        for node_id in unused.clone() {
            let node = &graph.nodes[&node_id];
            let unknown_region = node.region == json!("unknown");
            for (group_index, group) in groups.iter().enumerate() {
                let template = placements[&group[0]].clone();
                let cost =
                    stage_cost(node, template.end - template.start, model, workload, policy)?;
                let placement_id = format!("stage-{group_index:03}-replica-{node_id}");
                if !cost.feasible || cost.service_work_ms <= 0.0 {
                    decisions.push(Decision {
                        iteration: iterations,
                        placement_id,
                        node_id: node_id.clone(),
                        replica_group_id: template.replica_group_id.clone(),
                        accepted: false,
                        reason: format!(
                            "resource_{}",
                            if cost.diagnostic.is_empty() {
                                "infeasible"
                            } else {
                                cost.diagnostic
                            }
                        ),
                        baseline_admitted_rps: current.admitted,
                        proposed_admitted_rps: current.admitted,
                        raw_gain_rps: 0.0,
                        robust_gain_rps: 0.0,
                        minimum_required_gain_rps: current.admitted * min_gain_fraction,
                        failure_domain: node.region.clone(),
                        failure_domain_warning: unknown_region.then_some("failure_domain_unknown"),
                    });
                    continue;
                }
                let proposal = Placement {
                    placement_id: placement_id.clone(),
                    replica_group_id: template.replica_group_id.clone(),
                    node_id: node_id.clone(),
                    start: template.start,
                    end: template.end,
                    primary: false,
                    service_capacity_rps: 1000.0 / cost.service_work_ms,
                }
                .checked()?;
                let mut proposal_placements = placements.clone();
                proposal_placements.insert(placement_id.clone(), proposal.clone());
                let mut proposal_groups = groups.clone();
                proposal_groups[group_index].push(placement_id.clone());
                proposal_groups[group_index].sort();
                let (pf, pl, pflow) = solve(
                    &proposal_groups,
                    &proposal_placements,
                    graph,
                    model,
                    workload,
                    policy,
                )?;
                let gain = pflow.admitted - current.admitted;
                let confidence_gap = 1.0 - node.calibration_confidence;
                let uncertainty = if confidence_gap > uncertainty_fraction {
                    confidence_gap
                } else {
                    uncertainty_fraction
                };
                let robust_gain = max0(gain * (1.0 - uncertainty));
                let minimum_gain = current.admitted * min_gain_fraction;
                let primary_node = &graph.nodes[&template.node_id];
                let warning = if unknown_region || primary_node.region == json!("unknown") {
                    Some("failure_domain_unknown")
                } else if node.region == primary_node.region {
                    Some("shared_failure_domain")
                } else {
                    None
                };
                let key = (robust_gain, gain, -pflow.unmet_demand, placement_id.clone());
                let threshold = if NUMERIC_EPSILON > minimum_gain {
                    NUMERIC_EPSILON
                } else {
                    minimum_gain
                };
                evaluated.push(Decision {
                    iteration: iterations,
                    placement_id: placement_id.clone(),
                    node_id: node_id.clone(),
                    replica_group_id: template.replica_group_id.clone(),
                    accepted: false,
                    reason: if robust_gain <= threshold {
                        "gain_below_threshold".into()
                    } else {
                        "lower_ranked_candidate".into()
                    },
                    baseline_admitted_rps: current.admitted,
                    proposed_admitted_rps: pflow.admitted,
                    raw_gain_rps: gain,
                    robust_gain_rps: robust_gain,
                    minimum_required_gain_rps: minimum_gain,
                    failure_domain: node.region.clone(),
                    failure_domain_warning: warning,
                });
                if best
                    .as_ref()
                    .is_none_or(|b| candidate_greater(&key, &b.key))
                {
                    best = Some(Candidate {
                        key,
                        proposal,
                        groups: proposal_groups,
                        forward: pf,
                        loops: pl,
                        flow: pflow,
                    });
                }
            }
        }
        let floor = current.admitted * min_gain_fraction;
        let floor = if NUMERIC_EPSILON > floor {
            NUMERIC_EPSILON
        } else {
            floor
        };
        let Some(best) = best.filter(|b| b.key.0 > floor) else {
            decisions.extend(evaluated);
            break;
        };
        let selected_id = best.key.3.clone();
        for mut decision in evaluated {
            decision.accepted = decision.placement_id == selected_id;
            if decision.accepted {
                decision.reason = "accepted_positive_robust_gain".into();
            }
            decisions.push(decision);
        }
        placements.insert(best.proposal.placement_id.clone(), best.proposal.clone());
        groups = best.groups;
        forward = best.forward;
        loops = best.loops;
        current = best.flow;
        unused.remove(&best.proposal.node_id);
        accepted.push(best.proposal.node_id.clone());
        iterations += 1;
    }

    let used: BTreeSet<&String> = current
        .tracks
        .iter()
        .flat_map(|t| &t.placement_ids)
        .collect();
    let removable: BTreeSet<String> = placements
        .iter()
        .filter(|(pid, p)| !p.primary && !used.contains(pid))
        .map(|(pid, _)| pid.clone())
        .collect();
    if !removable.is_empty() {
        placements.retain(|pid, _| !removable.contains(pid));
        groups = groups
            .into_iter()
            .map(|g| g.into_iter().filter(|p| !removable.contains(p)).collect())
            .collect();
        accepted.retain(|node| placements.values().any(|p| &p.node_id == node));
        let (f, l, c) = solve(&groups, &placements, graph, model, workload, policy)?;
        forward = f;
        loops = l;
        current = c;
    }
    Ok(Replicated {
        placements: placements.values().cloned().collect(),
        forward_edges: forward,
        loopback_edges: loops,
        flow: current,
        accepted_replica_nodes: accepted,
        candidate_decisions: decisions,
        zero_flow_removed_placement_ids: removable.into_iter().collect(),
        primary_capacity_rps,
    })
}
