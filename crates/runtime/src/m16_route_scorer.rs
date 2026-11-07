// SPDX-License-Identifier: AGPL-3.0-only
//! M16 route scorer: `RouteScorer` (mycelium_router/scoring.py:15-275 @ f4e366e9) as the M16
//! coordinator builds it, `RoutePolicy(RouteScorer(config))` with no rtt table
//! (mycelium_m16_runtime.py:389-390). It scores one complete placement route: compute and queue
//! cost per stage from the device states, link cost between stages, the loopback token link, a
//! QoS-weighted total, the lowest stage confidence and the nodes whose stale state was replaced
//! by the conservative fallback. No path is built, chosen, reserved or scheduled here, and no
//! round trip is measured.
//!
//! Values are Python's at that seam ([`PyReal`], [`IdMap`]); every operation runs in the
//! oracle's order with CPython's semantics, so a NaN, an infinity, a huge int or a bool flows
//! through exactly as it does in v0.1.4. Refusals are the oracle's own exceptions on the same
//! input, raised at the same step: `m16_route_scorer_zero_division` (ZeroDivisionError, e.g.
//! `target_ttft_ms == 0` at :157), `m16_route_scorer_int_too_large_for_float` (OverflowError of
//! an int-to-float conversion), `m16_route_scorer_int_division_too_large_for_float`
//! (OverflowError of `int / int`, :263), `m16_route_scorer_math_range_error` (OverflowError of
//! `math.exp`, :172), `m16_route_scorer_unhashable_placement_id` (TypeError, :57),
//! `m16_route_scorer_unhashable_node_id` (TypeError at a node-id dict lookup, :88/:191/:221/:252)
//! and `m16_route_scorer_unorderable_node_ids` (TypeError of `sorted`, :167). The remaining codes
//! are the graph parser's own for a shape a parsed [`Graph`] never has.
use crate::m16_capacity::PyInt;
use crate::m16_router_contracts::{
    DeviceState, IdMap, PyError, PyReal, RequestContext, RouterConfig, ScoreBreakdown, hashable,
    py_add, py_cmp, py_float, py_le, py_lt, py_max, py_min, py_mul, py_sub, py_truediv,
    python_equal,
};
use protocol::execution_graph::Graph;
use serde_json::Value;
use std::cmp::Ordering;

/// scoring.py:18-21 and :217: where a link's round-trip time came from.
pub const TRANSFER_PROVENANCE_MEASURED: &str = "measured";
pub const TRANSFER_PROVENANCE_STATE: &str = "state_neighbor_rtt_ms";
pub const TRANSFER_PROVENANCE_DEFAULT: &str = "fallback_default_rtt_ms";
pub const TRANSFER_PROVENANCE_UNKNOWN: &str = "unknown_source_node";
pub const TRANSFER_PROVENANCE_SAME_NODE: &str = "same_node";

/// A refusal with its specific code.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ScorerError(pub &'static str);

const ZERO_DIVISION: ScorerError = ScorerError("m16_route_scorer_zero_division");
const INT_TOO_LARGE_FOR_FLOAT: ScorerError =
    ScorerError("m16_route_scorer_int_too_large_for_float");
const INT_DIVISION_TOO_LARGE_FOR_FLOAT: ScorerError =
    ScorerError("m16_route_scorer_int_division_too_large_for_float");
const MATH_RANGE_ERROR: ScorerError = ScorerError("m16_route_scorer_math_range_error");
const UNHASHABLE_PLACEMENT_ID: ScorerError =
    ScorerError("m16_route_scorer_unhashable_placement_id");
const UNHASHABLE_NODE_ID: ScorerError = ScorerError("m16_route_scorer_unhashable_node_id");
const UNORDERABLE_NODE_IDS: ScorerError = ScorerError("m16_route_scorer_unorderable_node_ids");
// The graph parser's codes for the same input; a parsed Graph never carries it.
/// Non-numeric cost or size: execution_graph.rs:149-157.
const NOT_A_NUMBER: ScorerError = ScorerError("invalid_graph_field_type");
/// Stages not a non-empty list: execution_graph.rs:239-242.
const MISSING_STAGES: ScorerError = ScorerError("missing_stages");
/// Placements not a list: execution_graph.rs:269-272.
const NO_PLACEMENTS: ScorerError = ScorerError("stage_without_placements");
/// Edges not a list: execution_graph.rs:336-338.
const MISSING_FIELD: ScorerError = ScorerError("missing_contract_field");

fn arithmetic(error: PyError) -> ScorerError {
    match error {
        PyError::IntTooLargeForFloat => INT_TOO_LARGE_FOR_FLOAT,
        PyError::IntDivisionTooLargeForFloat => INT_DIVISION_TOO_LARGE_FOR_FLOAT,
        PyError::ZeroDivision => ZERO_DIVISION,
    }
}

/// scoring.py:200-241: where one directed link's transfer cost came from.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransferProvenance {
    pub source_node_id: Value,
    pub destination_node_id: Value,
    /// `None` for an unknown source node.
    pub rtt_ms: Option<PyReal>,
    pub provenance: &'static str,
    pub sample_count: u64,
    pub is_measured: bool,
}

/// scoring.py:24-38 with `rtt_table=None` (the M16 coordinator passes none).
#[derive(Clone, Debug)]
pub struct RouteScorer {
    config: RouterConfig,
}

/// scoring.py:265-275.
fn infeasible() -> ScoreBreakdown {
    ScoreBreakdown {
        ttft_ms: f64::INFINITY,
        tpot_ms: f64::INFINITY,
        prefill_transfer_ms: f64::INFINITY,
        decode_transfer_ms: f64::INFINITY,
        total_score: f64::INFINITY,
        confidence: 0.0,
        fallback_nodes: Vec::new(),
    }
}

/// A graph size or cost as Python holds it.
fn number(v: &Value) -> Result<PyReal, ScorerError> {
    PyReal::from_json(v).ok_or(NOT_A_NUMBER)
}

/// `v * x` for a float `x` (the int converts with `float()`).
fn times(v: &PyReal, x: f64) -> Result<f64, ScorerError> {
    py_float(&py_mul(v, &PyReal::Float(x)).map_err(arithmetic)?).map_err(arithmetic)
}

/// :167 `tuple(sorted(fallback_nodes))`: node ids of one kind sort by Python `<` (numbers and
/// bools by value, strings by code point); a string next to a number, or `None` next to
/// anything, raises TypeError.
fn sorted_ids(ids: Vec<&Value>) -> Result<Vec<Value>, ScorerError> {
    let kind = |v: &Value| match v {
        Value::String(_) => 0,
        Value::Null => 1,
        _ => 2,
    };
    if ids.iter().any(|v| kind(v) != kind(ids[0])) {
        return Err(UNORDERABLE_NODE_IDS);
    }
    let mut sorted: Vec<Value> = ids.into_iter().cloned().collect();
    sorted.sort_by(|a, b| match (a, b) {
        (Value::String(x), Value::String(y)) => x.cmp(y),
        // A set holds no two equal ids, and JSON has no NaN: these always order.
        _ => PyReal::from_json(a)
            .zip(PyReal::from_json(b))
            .and_then(|(x, y)| py_cmp(&x, &y))
            .unwrap_or(Ordering::Equal),
    });
    Ok(sorted)
}

impl RouteScorer {
    pub fn new(config: RouterConfig) -> Self {
        Self { config }
    }

    pub fn config(&self) -> &RouterConfig {
        &self.config
    }

    /// scoring.py:40-168.
    pub fn score_route(
        &self,
        request: &RequestContext,
        graph: &Graph,
        placement_ids: &[Value],
        states: &IdMap<DeviceState>,
        now: &PyReal,
    ) -> Result<ScoreBreakdown, ScorerError> {
        let g = graph.as_value();
        let stages = g["stages"].as_array().ok_or(MISSING_STAGES)?;
        // :49-53 placement id -> (stage, placement); a later equal id replaces the entry.
        let mut placement_map: Vec<(&Value, (&Value, &Value))> = Vec::new();
        for stage in stages {
            for placement in stage["placements"].as_array().ok_or(NO_PLACEMENTS)? {
                let id = &placement["placement_id"];
                if !hashable(id) {
                    return Err(UNHASHABLE_PLACEMENT_ID);
                }
                match placement_map
                    .iter_mut()
                    .find(|(held, _)| python_equal(held, id))
                {
                    Some((_, entry)) => *entry = (stage, placement),
                    None => placement_map.push((id, (stage, placement))),
                }
            }
        }
        // :54-55
        if placement_ids.len() != stages.len() {
            return Ok(infeasible());
        }
        // :56-59 an unknown id is infeasible; an unhashable one raises, in route order.
        let mut stage_placements = Vec::with_capacity(placement_ids.len());
        for item in placement_ids {
            if !hashable(item) {
                return Err(UNHASHABLE_PLACEMENT_ID);
            }
            match placement_map
                .iter()
                .find(|(held, _)| python_equal(held, item))
            {
                Some((_, entry)) => stage_placements.push(*entry),
                None => return Ok(infeasible()),
            }
        }
        // :61-67 every consecutive pair is a graph edge.
        let edges = g["edges"].as_array().ok_or(MISSING_FIELD)?;
        if edges.iter().any(|edge| {
            !hashable(&edge["from_placement_id"]) || !hashable(&edge["to_placement_id"])
        }) {
            return Err(UNHASHABLE_PLACEMENT_ID);
        }
        for pair in placement_ids.windows(2) {
            if !edges.iter().any(|edge| {
                python_equal(&edge["from_placement_id"], &pair[0])
                    && python_equal(&edge["to_placement_id"], &pair[1])
            }) {
                return Ok(infeasible());
            }
        }
        // :68-78 a loopback edge from the last placement to the first.
        let (Some(first), Some(last)) = (placement_ids.first(), placement_ids.last()) else {
            return Err(MISSING_STAGES);
        };
        let loopbacks = g["loopback_edges"].as_array().ok_or(MISSING_FIELD)?;
        if !loopbacks.iter().any(|edge| {
            python_equal(&edge["from_placement_id"], last)
                && python_equal(&edge["to_placement_id"], first)
        }) {
            return Ok(infeasible());
        }

        let prompt_tokens =
            PyReal::signed(false, PyInt::from(request.prompt_token_ids.len() as u64));
        let mut ttft_ms = 0.0;
        let mut tpot_ms = 0.0;
        let mut prefill_transfer_ms = 0.0;
        let mut decode_transfer_ms = 0.0;
        let mut confidence_values: Vec<f64> = Vec::new();
        // :85 a set: the first of Python-equal node ids is kept.
        let mut fallback_nodes: Vec<&Value> = Vec::new();
        for (stage, placement) in &stage_placements {
            let node_id = &placement["node_id"];
            // :88-90
            let state = match states.get(node_id).map_err(|_| UNHASHABLE_NODE_ID)? {
                Some(state) if state.availability == "ALIVE" => state,
                _ => return Ok(infeasible()),
            };
            // :91 projected_age = max(0.0, now - last_updated) + ttft_ms / 1000.0
            let elapsed = py_max(
                &PyReal::Float(0.0),
                &py_sub(now, &state.last_updated).map_err(arithmetic)?,
            );
            let projected_age = py_add(&elapsed, &PyReal::Float(ttft_ms / 1000.0))
                .and_then(|age| py_float(&age))
                .map_err(arithmetic)?;
            // :92-96
            let confidence = self.confidence(projected_age)?;
            confidence_values.push(confidence);
            let use_fallback = py_lt(&PyReal::Float(confidence), &self.config.minimum_confidence);
            if use_fallback
                && !fallback_nodes
                    .iter()
                    .any(|held| python_equal(held, node_id))
            {
                fallback_nodes.push(node_id);
            }
            // :97-106
            let free_fraction = if use_fallback {
                self.config.conservative_compute_fraction.clone()
            } else {
                py_max(
                    &PyReal::Float(0.001),
                    &py_min(&PyReal::Float(1.0), &state.free_compute_fraction),
                )
            };
            let queue_depth = if use_fallback {
                self.config.conservative_queue_depth.clone()
            } else {
                py_max(&PyReal::int(0), &state.pending_hop_queue_depth)
            };
            // :107-109
            let compute_rate =
                py_mul(&state.compute_units_per_second, &free_fraction).map_err(arithmetic)?;
            if py_le(&compute_rate, &PyReal::int(0)) {
                return Ok(infeasible());
            }
            // :110-122 (prefill * prompt tokens) / compute_rate * 1000.0, the decode cost the
            // same way, and the queue term once into each of TTFT and TPOT.
            let cost = &stage["stage_cost"];
            let prefill_work = py_mul(
                &number(&cost["prefill_work_units_per_prompt_token"])?,
                &prompt_tokens,
            )
            .map_err(arithmetic)?;
            let prefill_compute_ms =
                py_truediv(&prefill_work, &compute_rate).map_err(arithmetic)? * 1000.0;
            let decode_compute_ms = py_truediv(
                &number(&cost["decode_work_units_per_token"])?,
                &compute_rate,
            )
            .map_err(arithmetic)?
                * 1000.0;
            let queued_ms = times(&queue_depth, decode_compute_ms)?;
            ttft_ms += prefill_compute_ms + queued_ms;
            tpot_ms += decode_compute_ms + queued_ms;
        }

        // :124-128
        let hidden_size = number(&g["hidden_size"])?;
        let activation_bytes = number(&g["activation_bytes"])?;
        let prefill_payload_bytes = py_mul(
            &py_mul(&prompt_tokens, &hidden_size).map_err(arithmetic)?,
            &activation_bytes,
        )
        .map_err(arithmetic)?;
        let decode_payload_bytes = py_mul(&hidden_size, &activation_bytes).map_err(arithmetic)?;
        let token_payload_bytes = number(&g["token_envelope_bytes"])?;
        // :129-145
        for pair in stage_placements.windows(2) {
            let (left, right) = (&pair[0].1["node_id"], &pair[1].1["node_id"]);
            let prefill_link = self.transfer_ms(left, right, &prefill_payload_bytes, states)?;
            let decode_link = self.transfer_ms(left, right, &decode_payload_bytes, states)?;
            prefill_transfer_ms += prefill_link;
            decode_transfer_ms += decode_link;
        }
        // :146-155 the loopback token link, last node to first.
        let (Some(head), Some(tail)) = (stage_placements.first(), stage_placements.last()) else {
            return Err(MISSING_STAGES);
        };
        decode_transfer_ms += self.transfer_ms(
            &tail.1["node_id"],
            &head.1["node_id"],
            &token_payload_bytes,
            states,
        )?;
        ttft_ms += prefill_transfer_ms;
        tpot_ms += decode_transfer_ms;
        // :156-159 alpha * (ttft / target_ttft) + (1.0 - alpha) * (tpot / target_tpot)
        let alpha = self.config.alpha_for(&request.qos_class);
        let ttft_part = times(
            alpha,
            py_truediv(&PyReal::Float(ttft_ms), &request.target_ttft_ms).map_err(arithmetic)?,
        )?;
        let complement = py_sub(&PyReal::Float(1.0), alpha).map_err(arithmetic)?;
        let tpot_part = times(
            &complement,
            py_truediv(&PyReal::Float(tpot_ms), &request.target_tpot_ms).map_err(arithmetic)?,
        )?;
        let total_score = ttft_part + tpot_part;
        // :166 builtin min(confidence_values, default=0.0): a later value replaces the current
        // one only if it compares strictly below it. So a NaN in first place is kept (stage 0's
        // confidence is NaN when its age and the half-life are both infinite, e.g. now=100.0,
        // last_updated=-inf and confidence_half_life_seconds=inf at this numeric seam; the
        // factory's time.monotonic() stamps never give it), and a later NaN never replaces the
        // current value.
        let confidence = confidence_values
            .into_iter()
            .reduce(|current, item| if item < current { item } else { current })
            .unwrap_or(0.0);
        Ok(ScoreBreakdown {
            ttft_ms,
            tpot_ms,
            prefill_transfer_ms,
            decode_transfer_ms,
            total_score,
            confidence,
            fallback_nodes: sorted_ids(fallback_nodes)?,
        })
    }

    /// :170-172 `math.exp(-math.log(2.0) * age / max(0.001, half_life))`; `math.exp` raises
    /// OverflowError where a finite argument overflows.
    fn confidence(&self, projected_age_seconds: f64) -> Result<f64, ScorerError> {
        let half_life = py_max(
            &PyReal::Float(0.001),
            &self.config.confidence_half_life_seconds,
        );
        let exponent = py_truediv(
            &PyReal::Float(-(2.0_f64.ln()) * projected_age_seconds),
            &half_life,
        )
        .map_err(arithmetic)?;
        let value = exponent.exp();
        if value.is_infinite() && exponent.is_finite() {
            return Err(MATH_RANGE_ERROR);
        }
        Ok(value)
    }

    /// :174-198 with no rtt table: the stated neighbour value as a float, else the default.
    fn resolve_rtt_ms(
        &self,
        destination_node: &Value,
        source_state: &DeviceState,
    ) -> Result<(PyReal, &'static str), ScorerError> {
        match source_state
            .neighbor_rtt_ms
            .get(destination_node)
            .map_err(|_| UNHASHABLE_NODE_ID)?
        {
            Some(stated) => Ok((
                PyReal::Float(py_float(stated).map_err(arithmetic)?),
                TRANSFER_PROVENANCE_STATE,
            )),
            None => Ok((
                self.config.default_rtt_ms.clone(),
                TRANSFER_PROVENANCE_DEFAULT,
            )),
        }
    }

    /// scoring.py:200-241.
    pub fn transfer_provenance(
        &self,
        source_node: &Value,
        destination_node: &Value,
        states: &IdMap<DeviceState>,
    ) -> Result<TransferProvenance, ScorerError> {
        let report = |rtt_ms, provenance| TransferProvenance {
            source_node_id: source_node.clone(),
            destination_node_id: destination_node.clone(),
            rtt_ms,
            provenance,
            sample_count: 0,
            is_measured: provenance == TRANSFER_PROVENANCE_MEASURED,
        };
        if python_equal(source_node, destination_node) {
            return Ok(report(
                Some(PyReal::Float(0.0)),
                TRANSFER_PROVENANCE_SAME_NODE,
            ));
        }
        let Some(source_state) = states.get(source_node).map_err(|_| UNHASHABLE_NODE_ID)? else {
            return Ok(report(None, TRANSFER_PROVENANCE_UNKNOWN));
        };
        let (rtt_ms, provenance) = self.resolve_rtt_ms(destination_node, source_state)?;
        Ok(report(Some(rtt_ms), provenance))
    }

    /// :243-263 `rtt_ms / 2.0 + payload_bytes / bandwidth * 1000.0`; 0 on one node, inf with
    /// no source state or a bandwidth not above 0.
    fn transfer_ms(
        &self,
        source_node: &Value,
        destination_node: &Value,
        payload_bytes: &PyReal,
        states: &IdMap<DeviceState>,
    ) -> Result<f64, ScorerError> {
        if python_equal(source_node, destination_node) {
            return Ok(0.0);
        }
        let Some(source) = states.get(source_node).map_err(|_| UNHASHABLE_NODE_ID)? else {
            return Ok(f64::INFINITY);
        };
        let (rtt_ms, _provenance) = self.resolve_rtt_ms(destination_node, source)?;
        let bandwidth = source
            .neighbor_bandwidth_bytes_per_second
            .get(destination_node)
            .map_err(|_| UNHASHABLE_NODE_ID)?
            .unwrap_or(&self.config.default_bandwidth_bytes_per_second);
        if py_le(bandwidth, &PyReal::int(0)) {
            return Ok(f64::INFINITY);
        }
        let half_rtt = py_truediv(&rtt_ms, &PyReal::Float(2.0)).map_err(arithmetic)?;
        let per_byte = py_truediv(payload_bytes, bandwidth).map_err(arithmetic)?;
        Ok(half_rtt + per_byte * 1000.0)
    }
}
