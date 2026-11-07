// SPDX-License-Identifier: AGPL-3.0-only
//! Allocation, cycle search, phase scoring and primary planning
//! (`allocation.py`, `cycle_search.py`, `phase_score.py`, `primary_plan.py`) at f4e366e9.
use super::contracts::{
    Graph, ModelIdentity, NodeCapability, Policy, Profile, Scenario, format_g12, num_f, py_sum,
    transfer_time_ms,
};
use crate::py::{Failure, Num, PyResult, verr};
use serde_json::Value;
use std::cmp::Ordering;
use std::collections::BTreeMap;

// ---------------------------------------------------------------- allocation
#[derive(Debug, Clone)]
pub struct StageCost {
    pub feasible: bool,
    pub prefill_ms: f64,
    pub decode_ms: f64,
    pub spill_penalty_ms: f64,
    pub required_memory_bytes: f64,
    pub service_work_ms: f64,
    pub diagnostic: &'static str,
}

impl StageCost {
    fn infeasible(required: f64, diagnostic: &'static str) -> Self {
        Self {
            feasible: false,
            prefill_ms: f64::INFINITY,
            decode_ms: f64::INFINITY,
            spill_penalty_ms: f64::INFINITY,
            required_memory_bytes: required,
            service_work_ms: f64::INFINITY,
            diagnostic,
        }
    }
    pub fn effective_prefill_ms(&self) -> f64 {
        self.prefill_ms + self.spill_penalty_ms
    }
    pub fn effective_decode_ms(&self) -> f64 {
        self.decode_ms + self.spill_penalty_ms
    }
}

pub fn stage_cost(
    node: &NodeCapability,
    layer_count: i64,
    model: &ModelIdentity,
    workload: &Scenario,
    policy: &Policy,
) -> PyResult<StageCost> {
    if layer_count <= 0 {
        return verr("layer_count must be positive");
    }
    let lc = layer_count as f64;
    let weight_bytes = model.weight_bytes_per_layer() * lc;
    let kv_bytes = num_f(model.kv_bytes_per_layer_token())
        * lc
        * num_f(workload.total_context_tokens())
        * num_f(workload.concurrency);
    let required = weight_bytes + kv_bytes + node.workspace_bytes;
    if required > node.total_memory_bytes {
        return Ok(StageCost::infeasible(required, "total_memory_exceeded"));
    }
    let fast_limit = node.fast_memory_bytes * (1.0 - policy.memory_reserve_fraction);
    let spill_bytes = super::replicate::max0(required - fast_limit);
    if spill_bytes != 0.0 && node.spill_bandwidth_bps <= 0.0 {
        return Ok(StageCost::infeasible(required, "spill_unavailable"));
    }
    let spill_penalty = if spill_bytes != 0.0 {
        spill_bytes / node.spill_bandwidth_bps * 1000.0
    } else {
        0.0
    };
    let prefill = node.prefill_ms_per_layer_token * lc * num_f(workload.effective_prompt_tokens());
    let decode = node.decode_ms_per_layer_token * lc;
    let out = num_f(workload.output_tokens);
    let service_work = prefill + out * decode + (out + 1.0) * spill_penalty;
    Ok(StageCost {
        feasible: true,
        prefill_ms: prefill,
        decode_ms: decode,
        spill_penalty_ms: spill_penalty,
        required_memory_bytes: required,
        service_work_ms: service_work,
        diagnostic: "",
    })
}

#[derive(Debug, Clone)]
pub struct AllocatedStage {
    pub stage_index: usize,
    pub node_id: String,
    pub start: i64,
    pub end: i64,
    pub cost: StageCost,
}

#[derive(Debug, Clone)]
pub struct Allocation {
    pub stages: Vec<AllocatedStage>,
    pub feasible: bool,
    pub diagnostics: Vec<&'static str>,
}

fn objective_cost(cost: &StageCost, policy: &Policy) -> f64 {
    match policy.objective.as_str() {
        "prefill_ttft" => cost.effective_prefill_ms(),
        "decode_tpot" => cost.effective_decode_ms(),
        _ => cost.service_work_ms,
    }
}

/// Python tuple ordering for `(float, tuple[int])` candidates.
fn tuple_less(a: &(f64, Vec<i64>), b: &(f64, Vec<i64>)) -> bool {
    if a.0 < b.0 {
        return true;
    }
    if a.0 > b.0 || a.0.is_nan() || b.0.is_nan() {
        return false;
    }
    a.1 < b.1
}

pub fn allocate_layers(
    nodes: &[&NodeCapability],
    model: &ModelIdentity,
    workload: &Scenario,
    policy: &Policy,
) -> PyResult<Allocation> {
    let fail = |d: &'static str| Allocation {
        stages: Vec::new(),
        feasible: false,
        diagnostics: vec![d],
    };
    if nodes.is_empty() {
        return Ok(fail("no nodes"));
    }
    let layers = model.layers();
    if nodes.len() as i64 > layers {
        return Ok(fail("stage count exceeds layer count"));
    }
    let mut dp: BTreeMap<(usize, i64), (f64, Vec<i64>)> = BTreeMap::new();
    dp.insert((0, 0), (0.0, Vec::new()));
    for (index, node) in nodes.iter().enumerate() {
        let stage_index = index + 1;
        let remaining = (nodes.len() - stage_index) as i64;
        for assigned in stage_index as i64..=(layers - remaining) {
            let mut best: Option<(f64, Vec<i64>)> = None;
            for previous in (stage_index as i64 - 1)..assigned {
                let Some(prior) = dp.get(&(stage_index - 1, previous)) else {
                    continue;
                };
                let take = assigned - previous;
                let cost = stage_cost(node, take, model, workload, policy)?;
                if !cost.feasible {
                    continue;
                }
                let mut counts = prior.1.clone();
                counts.push(take);
                let bottleneck = if prior.0 >= objective_cost(&cost, policy) {
                    prior.0
                } else {
                    objective_cost(&cost, policy)
                };
                let candidate = (bottleneck, counts);
                if best.as_ref().is_none_or(|b| tuple_less(&candidate, b)) {
                    best = Some(candidate);
                }
            }
            if let Some(best) = best {
                dp.insert((stage_index, assigned), best);
            }
        }
    }
    let Some(state) = dp.get(&(nodes.len(), layers)) else {
        return Ok(fail("no feasible contiguous allocation"));
    };
    let mut stages = Vec::new();
    let mut cursor = 0;
    for (index, (node, count)) in nodes.iter().zip(&state.1).enumerate() {
        let cost = stage_cost(node, *count, model, workload, policy)?;
        stages.push(AllocatedStage {
            stage_index: index,
            node_id: node.node_id.clone(),
            start: cursor,
            end: cursor + count,
            cost,
        });
        cursor += count;
    }
    Ok(Allocation {
        stages,
        feasible: true,
        diagnostics: Vec::new(),
    })
}

// ---------------------------------------------------------------- cycle search
#[derive(Debug, Clone)]
pub struct CycleResult {
    pub order: Vec<String>,
    pub cost: f64,
    pub mode: String,
    pub globally_exact: bool,
    pub explored_candidates: i64,
}

pub type CostFn<'a> = dyn Fn(&str, &str) -> Option<f64> + 'a;

fn edge(cost_fn: &CostFn<'_>, src: &str, dst: &str) -> f64 {
    match cost_fn(src, dst) {
        Some(v) if v.is_finite() => v,
        _ => f64::INFINITY,
    }
}

pub fn cycle_cost(order: &[String], cost_fn: &CostFn<'_>) -> f64 {
    if order.is_empty() {
        return f64::INFINITY;
    }
    if order.len() == 1 {
        return 0.0;
    }
    let mut total = 0.0;
    for (i, src) in order.iter().enumerate() {
        let dst = &order[(i + 1) % order.len()];
        let value = edge(cost_fn, src, dst);
        if !value.is_finite() {
            return f64::INFINITY;
        }
        total += value;
    }
    total
}

/// `(cost, order) < (best_cost, best_order)` for finite costs.
fn pair_less(a: &(f64, Vec<String>), b: &(f64, Vec<String>)) -> bool {
    match a.0.partial_cmp(&b.0) {
        Some(Ordering::Less) => true,
        Some(Ordering::Equal) => a.1 < b.1,
        _ => false,
    }
}

fn sorted_unique(nodes: &[String]) -> Vec<String> {
    let mut v = nodes.to_vec();
    v.sort();
    v.dedup();
    v
}

/// Lexicographic successor over index permutations (`itertools.permutations` order).
fn next_permutation(p: &mut [usize]) -> bool {
    if p.len() < 2 {
        return false;
    }
    let mut i = p.len() - 1;
    while i > 0 && p[i - 1] >= p[i] {
        i -= 1;
    }
    if i == 0 {
        return false;
    }
    let mut j = p.len() - 1;
    while p[j] <= p[i - 1] {
        j -= 1;
    }
    p.swap(i - 1, j);
    p[i..].reverse();
    true
}

fn factorial(n: usize) -> f64 {
    (1..=n).fold(1.0, |a, b| a * b as f64)
}

/// Enumerate `canonical[0] + permutation(tail)` in `itertools` order, at most `limit`.
fn enumerate_cycles(
    canonical: &[String],
    cost_fn: &CostFn<'_>,
    limit: f64,
) -> (Option<(f64, Vec<String>)>, i64) {
    let first = canonical[0].clone();
    let tail = &canonical[1..];
    let mut perm: Vec<usize> = (0..tail.len()).collect();
    let mut best: Option<(f64, Vec<String>)> = None;
    let mut explored = 0_i64;
    loop {
        if explored as f64 >= limit {
            break;
        }
        let mut order = vec![first.clone()];
        order.extend(perm.iter().map(|i| tail[*i].clone()));
        let cost = cycle_cost(&order, cost_fn);
        explored += 1;
        let candidate = (cost, order);
        if cost.is_finite() && best.as_ref().is_none_or(|b| pair_less(&candidate, b)) {
            best = Some(candidate);
        }
        if !next_permutation(&mut perm) {
            break;
        }
    }
    (best, explored)
}

pub fn exact_directed_cycle(nodes: &[String], cost_fn: &CostFn<'_>) -> PyResult<CycleResult> {
    let canonical = sorted_unique(nodes);
    if canonical.is_empty() {
        return verr("cycle requires at least one node");
    }
    if canonical.len() == 1 {
        return Ok(CycleResult {
            order: canonical,
            cost: 0.0,
            mode: "exact_enumeration".into(),
            globally_exact: true,
            explored_candidates: 1,
        });
    }
    let (best, explored) = enumerate_cycles(&canonical, cost_fn, f64::INFINITY);
    let Some(best) = best else {
        return verr("no feasible directed cycle");
    };
    Ok(CycleResult {
        order: best.1,
        cost: best.0,
        mode: "exact_enumeration".into(),
        globally_exact: true,
        explored_candidates: explored,
    })
}

pub fn held_karp_cycle(nodes: &[String], cost_fn: &CostFn<'_>) -> PyResult<CycleResult> {
    let canonical = sorted_unique(nodes);
    if canonical.is_empty() {
        return verr("cycle requires at least one node");
    }
    if canonical.len() == 1 {
        return Ok(CycleResult {
            order: canonical,
            cost: 0.0,
            mode: "held_karp".into(),
            globally_exact: true,
            explored_candidates: 1,
        });
    }
    let start = &canonical[0];
    let others = &canonical[1..];
    let n = others.len();
    // Orders hold indices into `others`; ascending index == ascending name.
    let mut dp: BTreeMap<(u64, usize), (f64, Vec<usize>)> = BTreeMap::new();
    let mut explored = 0_i64;
    for (index, node) in others.iter().enumerate() {
        let cost = edge(cost_fn, start, node);
        if cost.is_finite() {
            dp.insert((1 << index, index), (cost, vec![index]));
        }
    }
    let less = |a: &(f64, Vec<usize>), b: &(f64, Vec<usize>)| match a.0.partial_cmp(&b.0) {
        Some(Ordering::Less) => true,
        Some(Ordering::Equal) => a.1 < b.1,
        _ => false,
    };
    for size in 2..=n {
        for mask in 1_u64..(1 << n) {
            if mask.count_ones() as usize != size {
                continue;
            }
            for last in 0..n {
                if mask & (1 << last) == 0 {
                    continue;
                }
                let previous_mask = mask ^ (1 << last);
                let mut best: Option<(f64, Vec<usize>)> = None;
                for previous in 0..n {
                    let Some(state) = dp.get(&(previous_mask, previous)) else {
                        continue;
                    };
                    let e = edge(cost_fn, &others[previous], &others[last]);
                    let mut path = state.1.clone();
                    path.push(last);
                    let candidate = (state.0 + e, path);
                    explored += 1;
                    if candidate.0.is_finite() && best.as_ref().is_none_or(|b| less(&candidate, b))
                    {
                        best = Some(candidate);
                    }
                }
                if let Some(best) = best {
                    dp.insert((mask, last), best);
                }
            }
        }
    }
    let full = (1_u64 << n) - 1;
    let mut best_cycle: Option<(f64, Vec<usize>)> = None;
    for (last, last_node) in others.iter().enumerate() {
        let Some(state) = dp.get(&(full, last)) else {
            continue;
        };
        let candidate = (state.0 + edge(cost_fn, last_node, start), state.1.clone());
        if candidate.0.is_finite() && best_cycle.as_ref().is_none_or(|b| less(&candidate, b)) {
            best_cycle = Some(candidate);
        }
    }
    let Some(best) = best_cycle else {
        return verr("no feasible directed cycle");
    };
    let mut order = vec![start.clone()];
    order.extend(best.1.iter().map(|i| others[*i].clone()));
    Ok(CycleResult {
        order,
        cost: best.0,
        mode: "held_karp".into(),
        globally_exact: true,
        explored_candidates: explored,
    })
}

fn canonical_rotation(order: &[String]) -> Vec<String> {
    (0..order.len())
        .map(|i| {
            let mut r = order[i..].to_vec();
            r.extend_from_slice(&order[..i]);
            r
        })
        .min()
        .unwrap_or_default()
}

fn nearest_neighbor(nodes: &[String], cost_fn: &CostFn<'_>, start: &str) -> Option<Vec<String>> {
    let mut remaining: Vec<&String> = nodes.iter().filter(|n| n.as_str() != start).collect();
    let mut order = vec![start.to_owned()];
    while !remaining.is_empty() {
        let current = order.last().cloned().unwrap_or_default();
        let mut candidates: Vec<(f64, &String)> = remaining
            .iter()
            .map(|n| (edge(cost_fn, &current, n), *n))
            .collect();
        candidates.sort_by(|a, b| {
            a.0.partial_cmp(&b.0)
                .unwrap_or(Ordering::Equal)
                .then_with(|| a.1.cmp(b.1))
        });
        if !candidates[0].0.is_finite() {
            return None;
        }
        let node = candidates[0].1.clone();
        remaining.retain(|n| **n != node);
        order.push(node);
    }
    if order.len() > 1 && !edge(cost_fn, &order[order.len() - 1], &order[0]).is_finite() {
        return None;
    }
    Some(order)
}

/// CPython iteration order of a `set` of up to four small non-negative ints built by
/// successive `add` (8-slot table, hash == value, perturbed probing, no resize).
pub fn small_int_set_order(values: &[usize]) -> Vec<usize> {
    const MASK: usize = 7;
    let mut table: [Option<usize>; 8] = [None; 8];
    for &value in values {
        let mut i = value & MASK;
        let mut perturb = value;
        loop {
            match table[i] {
                None => {
                    table[i] = Some(value);
                    break;
                }
                Some(existing) if existing == value => break,
                Some(_) => {
                    perturb >>= 5;
                    i = (i * 5 + 1 + perturb) & MASK;
                }
            }
        }
    }
    table.iter().flatten().copied().collect()
}

fn local_swap(order: &[String], cost_fn: &CostFn<'_>) -> Vec<String> {
    let mut best = canonical_rotation(order);
    let mut best_cost = cycle_cost(&best, cost_fn);
    let n = best.len();
    let max_passes = if n <= 32 { 4 } else { 2 };
    for _ in 0..max_passes {
        let mut improved = false;
        for i in 1..n.saturating_sub(1) {
            for j in (i + 1)..n {
                let impacted = small_int_set_order(&[(i + n - 1) % n, i, (j + n - 1) % n, j]);
                let old_edges = py_sum(
                    impacted
                        .iter()
                        .map(|k| edge(cost_fn, &best[*k], &best[(k + 1) % n])),
                );
                let mut candidate = best.clone();
                candidate.swap(i, j);
                let new_edges = py_sum(
                    impacted
                        .iter()
                        .map(|k| edge(cost_fn, &candidate[*k], &candidate[(k + 1) % n])),
                );
                let cost = best_cost - old_edges + new_edges;
                let better = if cost < best_cost {
                    true
                } else {
                    cost == best_cost && candidate < best
                };
                if better {
                    best = candidate;
                    best_cost = cost;
                    improved = true;
                }
            }
        }
        if !improved {
            break;
        }
    }
    best
}

pub fn heuristic_cycle(
    nodes: &[String],
    cost_fn: &CostFn<'_>,
    mode: &str,
) -> PyResult<CycleResult> {
    let canonical = sorted_unique(nodes);
    if canonical.is_empty() {
        return verr("cycle requires nodes");
    }
    let mut candidates: Vec<(f64, Vec<String>)> = Vec::new();
    let mut explored = 0_i64;
    let starts: Vec<&String> = if canonical.len() <= 32 {
        canonical.iter().collect()
    } else {
        canonical.iter().take(16.min(canonical.len())).collect()
    };
    for start in starts {
        let order = nearest_neighbor(&canonical, cost_fn, start);
        explored += 1;
        let Some(order) = order else {
            continue;
        };
        let order = local_swap(&order, cost_fn);
        let candidate = (cycle_cost(&order, cost_fn), canonical_rotation(&order));
        if candidate.0.is_finite() {
            candidates.push(candidate);
        }
    }
    let Some(best) = candidates
        .into_iter()
        .reduce(|a, b| if pair_less(&b, &a) { b } else { a })
    else {
        return verr("no feasible directed cycle");
    };
    Ok(CycleResult {
        order: best.1,
        cost: best.0,
        mode: mode.to_owned(),
        globally_exact: false,
        explored_candidates: explored,
    })
}

pub fn search_cycle(
    nodes: &[String],
    cost_fn: &CostFn<'_>,
    policy: &Policy,
) -> PyResult<CycleResult> {
    let canonical = sorted_unique(nodes);
    let count = canonical.len() as f64;
    if count <= policy.exact_cycle_max_nodes {
        return exact_directed_cycle(&canonical, cost_fn);
    }
    if count <= policy.held_karp_max_nodes {
        return held_karp_cycle(&canonical, cost_fn);
    }
    let mode = if count <= policy.local_search_max_nodes {
        "multi_start_insertion"
    } else if count <= policy.clustered_max_nodes {
        "clustered_refinement"
    } else {
        "hierarchical_refinement"
    };
    heuristic_cycle(&canonical, cost_fn, mode)
}

// ---------------------------------------------------------------- phase scoring
#[derive(Debug, Clone)]
pub struct PhaseScore {
    pub scenario_name: String,
    pub ttft_ms: f64,
    pub tpot_ms: f64,
    pub single_request_tps: f64,
    pub output_goodput_tps: f64,
    pub prefill_compute_ms: f64,
    pub prefill_network_ms: Num,
    pub decode_compute_ms: f64,
    pub decode_network_ms: f64,
    pub decode_loopback_ms: f64,
    pub prefill_payload_bytes: Num,
    pub decode_payload_bytes: Num,
    pub expected_response_ms: f64,
    pub required_memory_bytes: f64,
    pub confidence: Num,
}

pub fn expected_response_ms(ttft: f64, tpot: f64, output_tokens: f64) -> PyResult<f64> {
    if ttft.min(tpot).min(output_tokens) < 0.0 {
        return verr("latency and output tokens must be non-negative");
    }
    Ok(ttft + output_tokens * tpot)
}

fn graph_cost(
    graph: &Graph,
    src: &str,
    dst: &str,
    payload: f64,
    policy: &Policy,
) -> PyResult<super::contracts::EdgeCost> {
    let Some(link) = graph.link(src, dst) else {
        return verr(format!("missing directed link {src}->{dst}"));
    };
    transfer_time_ms(link, payload, policy)
}

pub fn score_phases(
    allocation: &Allocation,
    graph: &Graph,
    model: &ModelIdentity,
    workload: &Scenario,
    policy: &Policy,
) -> PyResult<PhaseScore> {
    if !allocation.feasible || allocation.stages.is_empty() {
        return verr("cannot score infeasible allocation");
    }
    let stages = &allocation.stages;
    let prefill_payload = model.activation_bytes(workload.effective_prompt_tokens())?;
    let decode_payload = model.activation_bytes(Num::Int(1))?;
    let mut prefill_edges = Vec::new();
    let mut decode_edges = Vec::new();
    for pair in stages.windows(2) {
        prefill_edges.push(graph_cost(
            graph,
            &pair[0].node_id,
            &pair[1].node_id,
            num_f(prefill_payload),
            policy,
        )?);
        decode_edges.push(graph_cost(
            graph,
            &pair[0].node_id,
            &pair[1].node_id,
            num_f(decode_payload),
            policy,
        )?);
    }
    let (decode_loopback, loopback_confidence) = if stages.len() == 1 {
        (0.0, (1.0, false))
    } else {
        let l = graph_cost(
            graph,
            &stages[stages.len() - 1].node_id,
            &stages[0].node_id,
            16.0,
            policy,
        )?;
        (l.total_ms, (l.confidence, l.confidence_int))
    };
    let prefill_compute = py_sum(stages.iter().map(|s| s.cost.effective_prefill_ms()));
    let decode_compute = py_sum(stages.iter().map(|s| s.cost.effective_decode_ms()));
    // `sum([])` is the int 0 in Python; the spelling reaches plan output.
    let prefill_network = if prefill_edges.is_empty() {
        Num::Int(0)
    } else {
        Num::Float(py_sum(prefill_edges.iter().map(|e| e.total_ms)))
    };
    let decode_network = py_sum(decode_edges.iter().map(|e| e.total_ms));
    let ttft = prefill_compute + num_f(prefill_network);
    let tpot = decode_compute + decode_network + decode_loopback;
    let mut resources: Vec<f64> = stages
        .iter()
        .map(|s| s.cost.effective_decode_ms())
        .collect();
    resources.extend(decode_edges.iter().map(|e| e.total_ms));
    if stages.len() > 1 {
        resources.push(decode_loopback);
    }
    let bottleneck = resources
        .iter()
        .copied()
        .fold(resources[0], |a, b| if b > a { b } else { a });
    let single_tps = if tpot > 0.0 {
        1000.0 / tpot
    } else {
        f64::INFINITY
    };
    let saturated_tps = if bottleneck > 0.0 {
        1000.0 / bottleneck
    } else {
        f64::INFINITY
    };
    let concurrency_limited = num_f(workload.concurrency) * single_tps;
    let mut goodput = if concurrency_limited < saturated_tps {
        concurrency_limited
    } else {
        saturated_tps
    };
    if ttft > policy.ttft_slo_ms || tpot > policy.tpot_slo_ms {
        goodput = 0.0;
    }
    let mut confidences: Vec<(f64, bool)> = stages
        .iter()
        .map(|s| {
            let node = &graph.nodes[&s.node_id];
            (node.calibration_confidence, node.calibration_confidence_int)
        })
        .collect();
    confidences.extend(
        prefill_edges
            .iter()
            .map(|e| (e.confidence, e.confidence_int)),
    );
    confidences.extend(
        decode_edges
            .iter()
            .map(|e| (e.confidence, e.confidence_int)),
    );
    confidences.push(loopback_confidence);
    // Python `min()` keeps the first of equal elements, with its original spelling.
    let chosen = confidences
        .iter()
        .copied()
        .reduce(|a, b| if b.0 < a.0 { b } else { a })
        .unwrap_or((1.0, false));
    let confidence = if chosen.1 {
        Num::Int(chosen.0 as i128)
    } else {
        Num::Float(chosen.0)
    };
    Ok(PhaseScore {
        scenario_name: workload.name.clone(),
        ttft_ms: ttft,
        tpot_ms: tpot,
        single_request_tps: single_tps,
        output_goodput_tps: goodput,
        prefill_compute_ms: prefill_compute,
        prefill_network_ms: prefill_network,
        decode_compute_ms: decode_compute,
        decode_network_ms: decode_network,
        decode_loopback_ms: decode_loopback,
        prefill_payload_bytes: prefill_payload,
        decode_payload_bytes: decode_payload,
        expected_response_ms: expected_response_ms(ttft, tpot, num_f(workload.output_tokens))?,
        required_memory_bytes: py_sum(stages.iter().map(|s| s.cost.required_memory_bytes)),
        confidence,
    })
}

// ---------------------------------------------------------------- primary plan
#[derive(Debug, Clone)]
pub struct Provenance {
    pub mode: String,
    pub globally_exact: bool,
    pub explored_candidates: i64,
    pub candidate_node_count: i64,
    pub candidate_budget: Num,
    pub candidate_budget_raw: Value,
    pub budget_exhausted: bool,
}

#[derive(Debug, Clone)]
pub struct PrimaryPlan {
    pub order: Vec<String>,
    pub allocation: Allocation,
    pub scenario_scores: Vec<PhaseScore>,
    pub provenance: Provenance,
    pub candidate_node_ids: Vec<String>,
    pub unplaced_node_ids: Vec<String>,
    pub diagnostics: Vec<String>,
}

fn topology_cost_fn(graph: &Graph) -> impl Fn(&str, &str) -> Option<f64> + '_ {
    move |src, dst| {
        let link = graph.link(src, dst)?;
        let one_way = (link.rtt_ms / 2.0).max(link.geolocation_floor_ms);
        Some(one_way + link.jitter_ms)
    }
}

fn exact_topology_order(
    nodes: &[String],
    cost_fn: &CostFn<'_>,
    budget: f64,
) -> PyResult<CycleResult> {
    let canonical = sorted_unique(nodes);
    if canonical.is_empty() {
        return verr("topology order requires at least one admitted node");
    }
    if canonical.len() == 1 {
        return Ok(CycleResult {
            order: canonical,
            cost: 0.0,
            mode: "exact_topology_enumeration".into(),
            globally_exact: true,
            explored_candidates: 1,
        });
    }
    let total = factorial(canonical.len() - 1);
    let (best, explored) = enumerate_cycles(&canonical, cost_fn, budget);
    let Some(best) = best else {
        return verr("no feasible directed topology cycle");
    };
    Ok(CycleResult {
        order: best.1,
        cost: best.0,
        mode: "exact_topology_enumeration".into(),
        globally_exact: explored as f64 == total,
        explored_candidates: explored,
    })
}

pub fn select_topology_order(
    graph: &Graph,
    policy: &Policy,
    admitted: &[String],
) -> PyResult<CycleResult> {
    if admitted.is_empty() {
        return verr("no eligible candidate nodes");
    }
    let unique = sorted_unique(admitted);
    if unique.len() != admitted.len() || admitted.iter().any(|n| !graph.nodes.contains_key(n)) {
        return verr("admitted nodes must be distinct and eligible");
    }
    let cost_fn = topology_cost_fn(graph);
    if admitted.len() as f64 <= policy.exact_cycle_max_nodes {
        return exact_topology_order(admitted, &cost_fn, num_f(policy.search_candidate_budget));
    }
    let result = search_cycle(admitted, &cost_fn, policy)?;
    if result.explored_candidates as f64 > num_f(policy.search_candidate_budget) {
        return verr("topology search candidate budget exceeded");
    }
    Ok(result)
}

pub fn admit_primary_nodes(
    graph: &Graph,
    model: &ModelIdentity,
    profile: &Profile,
    policy: &Policy,
) -> PyResult<Vec<String>> {
    let representative = profile.primary_representative();
    let mut feasible = Vec::new();
    for node_id in graph.candidates() {
        if stage_cost(&graph.nodes[&node_id], 1, model, representative, policy)?.feasible {
            feasible.push(node_id);
        }
    }
    if feasible.is_empty() {
        return verr("no node can host one model layer");
    }
    let count = (model.layers().max(0) as usize).min(feasible.len());
    feasible.sort();
    feasible.truncate(count);
    Ok(feasible)
}

fn reprice(
    base: &Allocation,
    graph: &Graph,
    model: &ModelIdentity,
    scenario: &Scenario,
    policy: &Policy,
) -> PyResult<Result<Allocation, &'static str>> {
    let mut stages = Vec::new();
    for stage in &base.stages {
        let cost = stage_cost(
            &graph.nodes[&stage.node_id],
            stage.end - stage.start,
            model,
            scenario,
            policy,
        )?;
        if !cost.feasible {
            return Ok(Err(cost.diagnostic));
        }
        stages.push(AllocatedStage {
            cost,
            ..stage.clone()
        });
    }
    Ok(Ok(Allocation {
        stages,
        feasible: true,
        diagnostics: Vec::new(),
    }))
}

pub fn plan_primary(
    graph: &Graph,
    model: &ModelIdentity,
    profile: &Profile,
    policy: &Policy,
    admitted_node_ids: &[String],
) -> PyResult<PrimaryPlan> {
    let candidates = graph.candidates();
    if candidates.is_empty() {
        return verr("no eligible candidate nodes");
    }
    let admitted = admitted_node_ids;
    let unique = sorted_unique(admitted);
    if admitted.is_empty()
        || unique.len() != admitted.len()
        || admitted.iter().any(|n| !graph.nodes.contains_key(n))
    {
        return verr("admitted nodes must be distinct and eligible");
    }
    if admitted.len() as i64 > model.layers() {
        return verr("admitted node count exceeds layer count");
    }
    let topology = select_topology_order(graph, policy, admitted)?;
    let budget_exhausted = !topology.globally_exact
        && admitted.len() as f64 <= policy.exact_cycle_max_nodes
        && topology.explored_candidates as f64 >= num_f(policy.search_candidate_budget);
    let order = topology.order.clone();
    let representative = profile.primary_representative();
    let nodes: Vec<&NodeCapability> = order.iter().map(|n| &graph.nodes[n]).collect();
    let allocation = allocate_layers(&nodes, model, representative, policy)?;
    if !allocation.feasible {
        let detail = allocation.diagnostics.join("; ");
        return verr(format!(
            "no feasible primary allocation{}",
            if detail.is_empty() {
                String::new()
            } else {
                format!("; {detail}")
            }
        ));
    }
    let mut scores = Vec::new();
    for scenario in &profile.scenarios {
        let priced = match reprice(&allocation, graph, model, scenario, policy)? {
            Ok(p) => p,
            Err(d) => {
                return verr(format!(
                    "no feasible scenario allocation{}",
                    if d.is_empty() {
                        String::new()
                    } else {
                        format!("; {d}")
                    }
                ));
            }
        };
        match score_phases(&priced, graph, model, scenario, policy) {
            Ok(s) => scores.push(s),
            Err(e) if e.kind == crate::py::Kind::Value => {
                return verr(format!("phase scoring failed: {}", e.message));
            }
            Err(e) => return Err(e),
        }
    }
    let provenance = Provenance {
        mode: topology.mode.clone(),
        globally_exact: topology.globally_exact,
        explored_candidates: topology.explored_candidates,
        candidate_node_count: admitted.len() as i64,
        candidate_budget: policy.search_candidate_budget,
        candidate_budget_raw: policy.echo_budget.clone(),
        budget_exhausted,
    };
    if let Some(message) = provenance.invalid() {
        return Err(Failure::value(message));
    }
    let unplaced = candidates
        .iter()
        .filter(|n| !order.contains(n))
        .cloned()
        .collect();
    let diagnostics = vec![
        "primary order frozen from directed latency and jitter only".to_owned(),
        format!("admitted_node_count={}", admitted.len()),
        format!("topology_cycle_cost_ms={}", format_g12(topology.cost)),
    ];
    Ok(PrimaryPlan {
        order,
        allocation,
        scenario_scores: scores,
        provenance,
        candidate_node_ids: candidates,
        unplaced_node_ids: unplaced,
        diagnostics,
    })
}

impl Provenance {
    /// `SearchProvenance.__post_init__` refusals.
    fn invalid(&self) -> Option<&'static str> {
        if self.mode.is_empty() || self.explored_candidates < 0 || self.candidate_node_count < 0 {
            return Some("invalid search provenance");
        }
        if num_f(self.candidate_budget) <= 0.0
            || self.explored_candidates as f64 > num_f(self.candidate_budget)
        {
            return Some("invalid search candidate budget provenance");
        }
        if self.globally_exact && self.budget_exhausted {
            return Some("budget-exhausted search cannot claim global exactness");
        }
        None
    }
}
