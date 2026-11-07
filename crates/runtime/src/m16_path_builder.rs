// SPDX-License-Identifier: AGPL-3.0-only
//! M16 progressive path builder: `RoutingError`, `RoutePolicy` and `ProgressivePathBuilder`
//! (mycelium_router/routing.py:21-307 @ f4e366e9), `validate_hop_leases` with
//! `ReservationLeaseError` (leases.py:38-56) and `validate_manifest` (validation.py:181-235), as
//! the M16 coordinator composes them: `ProgressivePathBuilder(policy=RoutePolicy(RouteScorer(
//! config)), capacity=ledger, id_source=id_source)` with no lease provider
//! (mycelium_m16_runtime.py:389-393), run at admission (`admit`, :424-443). It chooses each
//! stage's placement by the route scorer, reserves it on the capacity port, releases what it
//! holds on a routing refusal, and locks the full path into a committed manifest. No scheduling,
//! coordinator, recovery, admission status or round-trip measurement is here.
//!
//! Values are Python's at that seam: ids and epochs are the parsed graph's JSON values compared
//! with Python `==` (`1 == 1.0 == True`); KV sizes are Python ints of any size or floats; times
//! are the clocks' floats, and a request's `admitted_at` is the number it is ([`PyReal`]). Ties,
//! NaN totals and mixed-kind ids behave as CPython's tuple order, `sorted` and `min` make them:
//! a NaN total is kept and, in first place, never replaced; ids of different kinds that must be
//! ordered raise TypeError.
//!
//! Refusals keep the oracle's exception kind ([`PathBuilderError`]). `RoutingError` codes
//! (`no_feasible_route`, `path_already_complete`, `path_incomplete`, `deployment_epoch_mismatch`,
//! `reservation_expired`, `no_legal_loopback`, `path_reservation_sync_rejected`,
//! `path_commit_rejected`) are the only kind `admit` catches (:442). `validate_manifest` raises
//! `ContractError` codes, `validate_hop_leases` `ReservationLeaseError` codes, the scorer and the
//! capacity port their own exceptions (passed through unchanged), and these lines' own Python
//! TypeError, KeyError, IndexError and OverflowError carry `m16_path_builder_*` codes.
//!
//! `validate_execution_graph` (validation.py:41-178), which `start` (routing.py:162) and
//! `validate_manifest` (validation.py:184) call, is not re-run: a [`Graph`] exists only as
//! `execution_graph::parse`'s output (execution_graph.rs:204), which raises the same
//! `ContractError` code for every one of its rules, in the oracle's order (execution_graph.rs):
//! `unsupported_graph_protocol` :167-170, `missing_deployment_id` :207,
//! `invalid_deployment_epoch` and `invalid_topology_version` :208-221, `missing_manifest_digest`
//! :222, `missing_resolved_commit` :223, `invalid_hidden_size`, `invalid_activation_bytes` and
//! `invalid_token_envelope_bytes` :224-238, `missing_stages` :239-242, `duplicate_stage_id` :247,
//! `negative_layer_start` :249-254, `empty_or_reversed_layer_range` :255-260,
//! `range_count_mismatch` :261-268, `stage_without_placements` :269-272, `invalid_prefill_cost`,
//! `invalid_decode_cost` and `invalid_kv_cost` :273-291, `layer_gap_or_overlap` :292-300,
//! `duplicate_placement_id` :302-306, `missing_node_id`, `missing_assignment_id`,
//! `missing_stage_signature` and `missing_load_proof` :308-315, `invalid_placement_lifecycle`
//! :316-319, `stage_without_serving_placement` :321-324, `invalid_entry_stage` :326-329,
//! `invalid_final_stage` :330-333, `duplicate_edge_id` :340 and :366, `unknown_edge_placement`
//! :343-351, `non_adjacent_stage_edge` :352, `duplicate_edge_pair` :353-358, `invalid_loopback`
//! :369-377 and `missing_loopback` :380-387. So the graph check can never fail here; the parser's
//! own codes below stand only for shapes a parsed graph never has.
//!
//! Declared limits (RU-001, tagged V03):
//! - A KV size that is a negative int (`(len(prompt) + max_new_tokens) * kv cost` with
//!   `max_new_tokens < -len(prompt)` and a positive int cost) is refused
//!   (`m16_path_builder_negative_kv_bytes`) where the oracle would reserve it: the ledger's
//!   `ReservationRequest.kv_bytes` holds no negative int. The M16 path never builds one: the
//!   gateway admits only `1 <= max_new_tokens` (mycelium_request_gateway/contracts.py:221-224)
//!   and the parser only kv costs `>= 0`.
//! - A lease provider returns a Python number or raises; one returning another object that
//!   `float()` would accept (a numeric string) is not modelled. The M16 composition passes no
//!   provider (:389-393).
//! - `now` and `lease_now` are the clocks' floats, as the ledger's [`LeaseClock`] reads them
//!   (`time.monotonic()` and the distributed protocol clock both return floats), and a hop's
//!   expiry is the ledger's float; a `ContractError` carries its code, not its detail text (as
//!   `protocol::ContractError` does).
use crate::m16_capacity::{PyInt, PyNumber};
use crate::m16_ledger::{
    IdSource, LeaseClock, LedgerError, ReservationCommitResult, ReservationRequest,
    ReservationResult, ResourceLedger,
};
use crate::m16_route_scorer::{RouteScorer, ScorerError};
use crate::m16_router_contracts::{
    BranchDecision, DeviceState, IdMap, IdSet, PATH_MANIFEST_PROTOCOL, PathBuildState, PathHop,
    PathManifest, PyError, PyReal, RequestContext, ScoreBreakdown, Unhashable, hashable, py_add,
    py_cmp, py_float, py_le, py_mul, python_equal,
};
use protocol::execution_graph::Graph;
use serde_json::Value;
use std::cmp::Ordering;

/// A refusal, by the kind of exception the oracle raises.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PathBuilderError {
    /// routing.py:21-25 `RoutingError(code, detail)`.
    Routing {
        code: &'static str,
        detail: &'static str,
    },
    /// validation.py:12-19 `ContractError(code)`.
    Contract(&'static str),
    /// leases.py:38-41 `ReservationLeaseError(code)`.
    Lease(&'static str),
    /// The route scorer's own exception, unchanged.
    Scorer(ScorerError),
    /// The capacity port's own exception, unchanged.
    Ledger(LedgerError),
    /// A Python TypeError, KeyError, IndexError or OverflowError raised by the builder's lines.
    Python(&'static str),
}

impl PathBuilderError {
    /// The specific code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Routing { code, .. } => code,
            Self::Contract(code) | Self::Lease(code) | Self::Python(code) => code,
            Self::Scorer(error) => error.0,
            Self::Ledger(error) => error.0,
        }
    }

    /// The exception kind: `routing`, `contract`, `lease`, `scorer`, `ledger` or `python`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Routing { .. } => "routing",
            Self::Contract(_) => "contract",
            Self::Lease(_) => "lease",
            Self::Scorer(_) => "scorer",
            Self::Ledger(_) => "ledger",
            Self::Python(_) => "python",
        }
    }
}

/// `RoutingError(code)` with no detail.
const fn routing(code: &'static str) -> PathBuilderError {
    PathBuilderError::Routing { code, detail: "" }
}

/// :50 `min` compares two tied routes whose first unequal ids are of different kinds.
const UNORDERABLE_PLACEMENT_IDS: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unorderable_placement_ids");
/// :52 `route[len(prefix)]` past a complete prefix (IndexError).
const ROUTE_INDEX_OUT_OF_RANGE: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_route_index_out_of_range");
/// :80 `placement_map[placement_id]` for a hop placement the graph does not have (KeyError).
const UNKNOWN_HOP_PLACEMENT: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unknown_hop_placement");
/// :80 `placement_map[placement_id]` for a list or dict hop placement (TypeError).
const UNHASHABLE_HOP_PLACEMENT_ID: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unhashable_hop_placement_id");
/// :83 `node_id not in excluded_devices` for a list or dict node id (TypeError).
const UNHASHABLE_NODE_ID: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unhashable_node_id");
/// :105 `sorted(outgoing)` over edge ids of different kinds (TypeError).
const UNORDERABLE_EDGE_IDS: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unorderable_edge_ids");
/// :140 `float(reservation_lease_seconds)` or :197-199 an int meeting a float KV cost, past the
/// double range (OverflowError).
const INT_TOO_LARGE_FOR_FLOAT: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_int_too_large_for_float");
/// Declared limit (module doc): a negative int KV size.
const NEGATIVE_KV_BYTES: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_negative_kv_bytes");
/// leases.py:51 `{-1, deployment_epoch}` with a list or dict deployment epoch (TypeError).
const UNHASHABLE_DEPLOYMENT_EPOCH: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unhashable_deployment_epoch");
/// leases.py:51 a list or dict reservation epoch as a set probe (TypeError).
const UNHASHABLE_RESERVATION_EPOCH: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unhashable_reservation_epoch");
/// validation.py:211 a list or dict hop placement id as a dict probe (TypeError).
const UNHASHABLE_MANIFEST_PLACEMENT_ID: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unhashable_manifest_placement_id");
/// validation.py:232 a list or dict loopback edge id as a set probe (TypeError).
const UNHASHABLE_LOOPBACK_EDGE_ID: PathBuilderError =
    PathBuilderError::Python("m16_path_builder_unhashable_loopback_edge_id");
// The graph parser's codes for the same input; a parsed Graph never carries it.
/// A list or dict graph id: execution_graph.rs:136-138.
const UNHASHABLE_GRAPH_ID: PathBuilderError =
    PathBuilderError::Contract("invalid_graph_field_type");
/// A non-numeric KV cost: execution_graph.rs:149-157.
const NOT_A_NUMBER: PathBuilderError = PathBuilderError::Contract("invalid_graph_field_type");
/// Stages not a non-empty list: execution_graph.rs:239-242.
const MISSING_STAGES: PathBuilderError = PathBuilderError::Contract("missing_stages");
/// Placements not a list: execution_graph.rs:269-272.
const NO_PLACEMENTS: PathBuilderError = PathBuilderError::Contract("stage_without_placements");
/// Edges not a list: execution_graph.rs:336-338.
const MISSING_FIELD: PathBuilderError = PathBuilderError::Contract("missing_contract_field");

fn arithmetic(error: PyError) -> PathBuilderError {
    match error {
        PyError::IntTooLargeForFloat => INT_TOO_LARGE_FOR_FLOAT,
        // `+`, `*` and `float()` raise no other arithmetic error.
        PyError::IntDivisionTooLargeForFloat | PyError::ZeroDivision => INT_TOO_LARGE_FOR_FLOAT,
    }
}

fn graph_id(_: Unhashable) -> PathBuilderError {
    UNHASHABLE_GRAPH_ID
}

/// The lease-seconds provider raised an exception.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProviderRaised;

/// routing.py:131-134 the optional `() -> seconds` provider (a physical node passes its
/// transport's KV lease window; the M16 composition passes none).
pub type LeaseSecondsProvider = Box<dyn Fn() -> Result<PyReal, ProviderRaised>>;

/// The capacity the builder reserves against (routing.py:200, :226, :238, :291-303).
pub trait CapacityPort {
    fn reserve(&mut self, request: &ReservationRequest) -> Result<ReservationResult, LedgerError>;
    fn release(&mut self, reservation_ids: &[String]);
    fn commit(
        &mut self,
        reservation_ids: &[String],
        deployment_epoch: &Value,
    ) -> ReservationCommitResult;
    /// :291 `getattr(capacity, "synchronize_build", None)`: `None` when the port has none.
    fn synchronize_build(&mut self, _build: &PathBuildState) -> Option<ReservationCommitResult> {
        None
    }
}

/// `_ResourceLedgerCapacityPort` on the M16 path (mycelium_m16_runtime.py:197-290): reserve,
/// release and commit, and no `synchronize_build`.
impl<C: LeaseClock, I: IdSource> CapacityPort for ResourceLedger<C, I> {
    fn reserve(&mut self, request: &ReservationRequest) -> Result<ReservationResult, LedgerError> {
        ResourceLedger::reserve(self, request)
    }

    fn release(&mut self, reservation_ids: &[String]) {
        ResourceLedger::release(self, reservation_ids);
    }

    fn commit(
        &mut self,
        reservation_ids: &[String],
        deployment_epoch: &Value,
    ) -> ReservationCommitResult {
        ResourceLedger::commit(self, reservation_ids, deployment_epoch)
    }
}

/// leases.py:44-56: every hop's epoch is `-1` or the deployment's (else
/// `deployment_epoch_mismatch`), then every hop expires after `now` (else `reservation_expired`).
pub fn validate_hop_leases(
    hops: &[PathHop],
    deployment_epoch: &Value,
    now: &PyReal,
) -> Result<(), PathBuilderError> {
    // :50-54 `any(hop.reservation_epoch not in {-1, deployment_epoch})`, hop by hop.
    for hop in hops {
        if !hashable(deployment_epoch) {
            return Err(UNHASHABLE_DEPLOYMENT_EPOCH);
        }
        let epoch = &hop.reservation_epoch;
        if !hashable(epoch) {
            return Err(UNHASHABLE_RESERVATION_EPOCH);
        }
        if !(python_equal(epoch, &Value::from(-1)) || python_equal(epoch, deployment_epoch)) {
            return Err(PathBuilderError::Lease("deployment_epoch_mismatch"));
        }
    }
    // :55-56 `hop.reservation_expires_at <= now`, exact across int and float (NaN is never).
    if hops
        .iter()
        .any(|hop| py_le(&PyReal::Float(hop.reservation_expires_at), now))
    {
        return Err(PathBuilderError::Lease("reservation_expired"));
    }
    Ok(())
}

fn require(ok: bool, code: &'static str) -> Result<(), PathBuilderError> {
    if ok {
        Ok(())
    } else {
        Err(PathBuilderError::Contract(code))
    }
}

fn stage_list(graph: &Value) -> Result<&Vec<Value>, PathBuilderError> {
    graph["stages"].as_array().ok_or(MISSING_STAGES)
}

fn placement_list(stage: &Value) -> Result<&Vec<Value>, PathBuilderError> {
    stage["placements"].as_array().ok_or(NO_PLACEMENTS)
}

fn edge_list<'a>(graph: &'a Value, key: &str) -> Result<&'a Vec<Value>, PathBuilderError> {
    graph[key].as_array().ok_or(MISSING_FIELD)
}

/// validation.py:181-235 (the graph part, :184, is the parser's: module doc).
pub fn validate_manifest(manifest: &PathManifest, graph: &Graph) -> Result<(), PathBuilderError> {
    let g = graph.as_value();
    require(
        manifest.protocol == PATH_MANIFEST_PROTOCOL,
        "unsupported_manifest_protocol",
    )?;
    require(
        python_equal(&manifest.deployment_id, &g["deployment_id"]),
        "deployment_id_mismatch",
    )?;
    require(
        python_equal(&manifest.deployment_epoch, &g["deployment_epoch"]),
        "deployment_epoch_mismatch",
    )?;
    require(
        python_equal(&manifest.topology_version, &g["topology_version"]),
        "topology_version_mismatch",
    )?;
    require(
        python_equal(&manifest.manifest_digest, &g["manifest_digest"]),
        "manifest_digest_mismatch",
    )?;
    let stages = stage_list(g)?;
    require(
        manifest.ordered_hops.len() == stages.len(),
        "manifest_stage_count_mismatch",
    )?;
    // :201-208 every graph placement id, and the legal (from, to) edge pairs.
    let mut placement_ids: Vec<&Value> = Vec::new();
    for stage in stages {
        placement_ids.extend(placement_list(stage)?.iter().map(|p| &p["placement_id"]));
    }
    let edges = edge_list(g, "edges")?;
    // :209-216
    for (stage, hop) in stages.iter().zip(&manifest.ordered_hops) {
        require(
            python_equal(&hop.stage_id, &stage["stage_id"]),
            "manifest_stage_order_mismatch",
        )?;
        if !hashable(&hop.placement_id) {
            return Err(UNHASHABLE_MANIFEST_PLACEMENT_ID);
        }
        require(
            placement_ids
                .iter()
                .any(|id| python_equal(id, &hop.placement_id)),
            "unknown_manifest_placement",
        )?;
        require(
            placement_list(stage)?
                .iter()
                .any(|p| python_equal(&p["placement_id"], &hop.placement_id)),
            "placement_stage_mismatch",
        )?;
        require(!hop.reservation_id.is_empty(), "missing_reservation_id")?;
    }
    // :217-222
    for pair in manifest.ordered_hops.windows(2) {
        require(
            edges.iter().any(|edge| {
                python_equal(&edge["from_placement_id"], &pair[0].placement_id)
                    && python_equal(&edge["to_placement_id"], &pair[1].placement_id)
            }),
            "illegal_manifest_edge",
        )?;
    }
    // :224-234 the loopback is one of the graph's from the last hop to the first.
    let (Some(first), Some(last)) = (manifest.ordered_hops.first(), manifest.ordered_hops.last())
    else {
        return Err(MISSING_STAGES);
    };
    let loopbacks = edge_list(g, "loopback_edges")?;
    if !hashable(&manifest.loopback_edge_id) {
        return Err(UNHASHABLE_LOOPBACK_EDGE_ID);
    }
    require(
        loopbacks.iter().any(|edge| {
            python_equal(&edge["from_placement_id"], &last.placement_id)
                && python_equal(&edge["to_placement_id"], &first.placement_id)
                && python_equal(&edge["edge_id"], &manifest.loopback_edge_id)
        }),
        "illegal_manifest_loopback",
    )
}

/// The kind Python orders within: strings with strings, numbers and bools with each other;
/// `None` with nothing.
fn order_kind(v: &Value) -> u8 {
    match v {
        Value::String(_) => 0,
        Value::Number(_) | Value::Bool(_) => 1,
        Value::Null => 2,
        Value::Array(_) | Value::Object(_) => 3,
    }
}

/// Python `a < b` for two ids; `None` where it raises TypeError (ids of different kinds, or
/// `None`).
fn id_less(a: &Value, b: &Value) -> Option<bool> {
    match (a, b) {
        (Value::String(x), Value::String(y)) => Some(x < y),
        _ => match (PyReal::from_json(a), PyReal::from_json(b)) {
            (Some(x), Some(y)) => Some(py_cmp(&x, &y) == Some(Ordering::Less)),
            _ => None,
        },
    }
}

/// :105 `sorted(outgoing.get(current, []))`: `(edge_id, destination)` tuples. Edge ids are
/// unique (execution_graph.rs:340), so the ids alone decide. A successful sort compares every
/// pair that ends up adjacent, so ids of two kinds always meet and raise TypeError; ids of one
/// kind always order (JSON has no NaN).
fn sorted_edges<'a>(
    mut edges: Vec<(&'a Value, &'a Value)>,
) -> Result<Vec<(&'a Value, &'a Value)>, PathBuilderError> {
    if let Some((first, _)) = edges.first() {
        let kind = order_kind(first);
        if edges.iter().any(|(id, _)| order_kind(id) != kind) {
            return Err(UNORDERABLE_EDGE_IDS);
        }
    }
    edges.sort_by(|(a, _), (b, _)| match (a, b) {
        (Value::String(x), Value::String(y)) => x.cmp(y),
        _ => PyReal::from_json(a)
            .zip(PyReal::from_json(b))
            .and_then(|(x, y)| py_cmp(&x, &y))
            .unwrap_or(Ordering::Equal),
    });
    Ok(edges)
}

/// :50 CPython `(a_total, a_route) < (b_total, b_route)`. The first unequal items decide: the
/// totals unless they are `==` (each is a fresh float, so a NaN never is, and NaN `<` anything
/// is false); else the routes, element by element, whose first unequal ids decide by `<`
/// (TypeError for ids of different kinds); equal routes are not less.
fn key_less(a: (f64, &[Value]), b: (f64, &[Value])) -> Result<bool, PathBuilderError> {
    if a.0 != b.0 {
        return Ok(a.0 < b.0);
    }
    for (x, y) in a.1.iter().zip(b.1) {
        if !python_equal(x, y) {
            return id_less(x, y).ok_or(UNORDERABLE_PLACEMENT_IDS);
        }
    }
    Ok(a.1.len() < b.1.len())
}

/// routing.py:28-123.
#[derive(Clone, Debug)]
pub struct RoutePolicy {
    scorer: RouteScorer,
}

impl RoutePolicy {
    pub fn new(scorer: RouteScorer) -> Self {
        Self { scorer }
    }

    pub fn scorer(&self) -> &RouteScorer {
        &self.scorer
    }

    /// routing.py:32-55: score every complete route extending the build's hops, keep those
    /// whose total is not infinite (a NaN total is kept), and choose the least
    /// `(total_score, route)`; the next placement is `route[len(prefix)]`.
    pub fn choose_next(
        &self,
        build: &PathBuildState,
        states: &IdMap<DeviceState>,
        now: &PyReal,
    ) -> Result<BranchDecision, PathBuilderError> {
        let prefix: Vec<Value> = build
            .ordered_hops
            .iter()
            .map(|hop| hop.placement_id.clone())
            .collect();
        let candidates = self.complete_routes(build, &prefix)?;
        // :41-47 every route is scored before any is compared.
        let mut scored: Vec<(Vec<Value>, ScoreBreakdown)> = Vec::new();
        for route in candidates {
            let score = self
                .scorer
                .score_route(&build.request, &build.graph, &route, states, now)
                .map_err(PathBuilderError::Scorer)?;
            if !score.total_score.is_infinite() {
                scored.push((route, score));
            }
        }
        // :48-50 builtin min: a later item replaces the current one only if its key is less.
        let mut items = scored.into_iter();
        let Some(mut best) = items.next() else {
            return Err(routing("no_feasible_route"));
        };
        for item in items {
            if key_less((item.1.total_score, &item.0), (best.1.total_score, &best.0))? {
                best = item;
            }
        }
        let (route, score) = best;
        let placement_id = route
            .get(prefix.len())
            .cloned()
            .ok_or(ROUTE_INDEX_OUT_OF_RANGE)?;
        Ok(BranchDecision {
            placement_id,
            complete_route: route,
            score,
        })
    }

    /// routing.py:57-123 `_complete_routes(build, prefix)`: every full-length route that
    /// extends `prefix` through allowed placements (not excluded, node not excluded, ACTIVE)
    /// over non-excluded edges sorted by `(edge_id, destination)`, and returns over a
    /// non-excluded loopback from its last placement to its first.
    pub fn complete_routes(
        &self,
        build: &PathBuildState,
        prefix: &[Value],
    ) -> Result<Vec<Vec<Value>>, PathBuilderError> {
        let graph = build.graph.as_value();
        let stages = stage_list(graph)?;
        // :63-72 placement id -> (placement, stage index); a later equal id replaces both.
        let mut placements: Vec<(&Value, &Value, usize)> = Vec::new();
        for (index, stage) in stages.iter().enumerate() {
            for placement in placement_list(stage)? {
                let id = &placement["placement_id"];
                match placements
                    .iter_mut()
                    .find(|(held, ..)| python_equal(held, id))
                {
                    Some(entry) => {
                        entry.1 = placement;
                        entry.2 = index;
                    }
                    None => placements.push((id, placement, index)),
                }
            }
        }
        // :73-77 each source's (edge_id, destination) pairs in graph order.
        let mut outgoing: Vec<(&Value, Vec<(&Value, &Value)>)> = Vec::new();
        for edge in edge_list(graph, "edges")? {
            let from = &edge["from_placement_id"];
            let item = (&edge["edge_id"], &edge["to_placement_id"]);
            match outgoing
                .iter_mut()
                .find(|(held, _)| python_equal(held, from))
            {
                Some((_, list)) => list.push(item),
                None => outgoing.push((from, vec![item])),
            }
        }
        let lookup = |id: &Value| -> Result<(&Value, usize), PathBuilderError> {
            if !hashable(id) {
                return Err(UNHASHABLE_HOP_PLACEMENT_ID);
            }
            placements
                .iter()
                .find(|(held, ..)| python_equal(held, id))
                .map(|(_, placement, index)| (*placement, *index))
                .ok_or(UNKNOWN_HOP_PLACEMENT)
        };
        // :79-85, evaluated left to right as far as `and` goes.
        let allowed = |id: &Value| -> Result<bool, PathBuilderError> {
            let (placement, _) = lookup(id)?;
            if build.excluded_placements.contains(id).map_err(graph_id)? {
                return Ok(false);
            }
            if build
                .excluded_devices
                .contains(&placement["node_id"])
                .map_err(|_| UNHASHABLE_NODE_ID)?
            {
                return Ok(false);
            }
            Ok(placement["lifecycle_state"] == "ACTIVE")
        };
        // :87-99
        let mut partials: Vec<Vec<Value>> = Vec::new();
        if let Some(last) = prefix.last() {
            for item in prefix {
                if !allowed(item)? {
                    return Ok(Vec::new());
                }
            }
            if lookup(last)?.1 != prefix.len() - 1 {
                return Ok(Vec::new());
            }
            partials.push(prefix.to_vec());
        } else {
            for placement in placement_list(stages.first().ok_or(MISSING_STAGES)?)? {
                let id = &placement["placement_id"];
                if allowed(id)? {
                    partials.push(vec![id.clone()]);
                }
            }
        }
        // :101-111 extend stage by stage.
        while partials.first().is_some_and(|p| p.len() < stages.len()) {
            let mut expanded = Vec::new();
            for partial in &partials {
                let Some(current) = partial.last() else {
                    continue;
                };
                let edges = outgoing
                    .iter()
                    .find(|(from, _)| python_equal(from, current))
                    .map(|(_, list)| list.clone())
                    .unwrap_or_default();
                for (edge_id, destination) in sorted_edges(edges)? {
                    if build.excluded_edges.contains(edge_id).map_err(graph_id)?
                        || !allowed(destination)?
                    {
                        continue;
                    }
                    if lookup(destination)?.1 != partial.len() {
                        continue;
                    }
                    let mut route = partial.clone();
                    route.push(destination.clone());
                    expanded.push(route);
                }
            }
            partials = expanded;
        }
        // :113-123
        let mut loopback_pairs: Vec<(&Value, &Value)> = Vec::new();
        for edge in edge_list(graph, "loopback_edges")? {
            if !build
                .excluded_edges
                .contains(&edge["edge_id"])
                .map_err(graph_id)?
            {
                loopback_pairs.push((&edge["from_placement_id"], &edge["to_placement_id"]));
            }
        }
        Ok(partials
            .into_iter()
            .filter(|route| {
                route.len() == stages.len()
                    && route
                        .first()
                        .zip(route.last())
                        .is_some_and(|(first, last)| {
                            loopback_pairs.iter().any(|(from, to)| {
                                python_equal(from, last) && python_equal(to, first)
                            })
                        })
            })
            .collect())
    }
}

/// routing.py:197-199 `(len(prompt_token_ids) + max_new_tokens) * kv_bytes_per_context_token`.
fn kv_bytes(request: &RequestContext, stage: &Value) -> Result<PyNumber, PathBuilderError> {
    let tokens = PyReal::signed(false, PyInt::from(request.prompt_token_ids.len() as u64));
    let cost = PyReal::from_json(&stage["stage_cost"]["kv_bytes_per_context_token"])
        .ok_or(NOT_A_NUMBER)?;
    let count = py_add(&tokens, &request.max_new_tokens).map_err(arithmetic)?;
    match py_mul(&count, &cost).map_err(arithmetic)? {
        PyReal::Float(x) => Ok(PyNumber::Float(x)),
        PyReal::Int {
            negative: false,
            magnitude,
        } => Ok(PyNumber::Int(magnitude)),
        PyReal::Int { negative: true, .. } => Err(NEGATIVE_KV_BYTES),
        // int arithmetic yields an int, never a bool.
        PyReal::Bool(flag) => Ok(PyNumber::from(u64::from(flag))),
    }
}

/// routing.py:126-307.
pub struct ProgressivePathBuilder<P, I> {
    policy: RoutePolicy,
    capacity: P,
    id_source: I,
    lease_seconds: Option<LeaseSecondsProvider>,
}

impl<P: CapacityPort, I: IdSource> ProgressivePathBuilder<P, I> {
    /// :127-134.
    pub fn new(
        policy: RoutePolicy,
        capacity: P,
        id_source: I,
        lease_seconds: Option<LeaseSecondsProvider>,
    ) -> Self {
        Self {
            policy,
            capacity,
            id_source,
            lease_seconds,
        }
    }

    pub fn policy(&self) -> &RoutePolicy {
        &self.policy
    }

    pub fn capacity(&self) -> &P {
        &self.capacity
    }

    pub fn capacity_mut(&mut self) -> &mut P {
        &mut self.capacity
    }

    /// :136-150 `float(config.reservation_lease_seconds)`, or with a provider
    /// `max(base, float(provider()))` unless the provider raises (or `float()` does) or gives a
    /// non-finite value, which keep the base.
    pub fn reservation_lease_seconds(&self) -> Result<f64, PathBuilderError> {
        let base =
            py_float(&self.policy.scorer.config().reservation_lease_seconds).map_err(arithmetic)?;
        let Some(provider) = &self.lease_seconds else {
            return Ok(base);
        };
        // :144-147 `except Exception: return base`.
        let scaled = match provider().map(|value| py_float(&value)) {
            Ok(Ok(scaled)) => scaled,
            Ok(Err(_)) | Err(ProviderRaised) => return Ok(base),
        };
        if !scaled.is_finite() {
            return Ok(base);
        }
        // Builtin max(base, scaled): the second only if it is greater.
        Ok(if scaled > base { scaled } else { base })
    }

    /// :152-171. `validate_execution_graph(graph)` (:162) holds for every parsed [`Graph`]
    /// (module doc), so starting cannot fail. That includes the raw JSON integer `-0`, which
    /// `json.loads` reads as int 0: the parser's id equality (checks.rs `python_equal`) and its
    /// numeric order (execution_graph.rs `int_text`) take `-0` as `0`, so a `-0` id repeats a `0`
    /// or `false` id (`duplicate_placement_id`) and `-0` is a valid zero cost, epoch or layer
    /// start (protocol `execution_graph_raw_negative_zero_integer_parity`, graph_contracts.rs).
    pub fn start(
        &self,
        request: &RequestContext,
        graph: &Graph,
        path_attempt: u64,
        excluded_placements: IdSet,
        excluded_edges: IdSet,
        excluded_devices: IdSet,
    ) -> PathBuildState {
        PathBuildState {
            excluded_placements,
            excluded_edges,
            excluded_devices,
            ..PathBuildState::new(
                request.clone(),
                graph.clone(),
                &self.id_source.new_id("path"),
                path_attempt,
            )
        }
    }

    /// :173-175 one hop per stage.
    pub fn is_complete(&self, build: &PathBuildState) -> bool {
        build.graph.as_value()["stages"]
            .as_array()
            .is_some_and(|stages| build.ordered_hops.len() == stages.len())
    }

    /// :177-231: reserve the next stage's chosen placement until a reservation is live
    /// (accepted, at the graph's epoch, expiring after the lease base); an accepted non-live
    /// reservation with an id is released, and every refused placement is excluded. A routing
    /// refusal aborts (releases every held hop); any other exception propagates as it is.
    pub fn advance(
        &mut self,
        build: &PathBuildState,
        states: &IdMap<DeviceState>,
        now: f64,
        lease_now: Option<f64>,
    ) -> Result<PathBuildState, PathBuilderError> {
        if self.is_complete(build) {
            return Err(routing("path_already_complete"));
        }
        let mut current = build.clone();
        let lease_base = lease_now.unwrap_or(now);
        loop {
            let decision = match self
                .policy
                .choose_next(&current, states, &PyReal::Float(now))
            {
                Ok(decision) => decision,
                Err(error @ PathBuilderError::Routing { .. }) => {
                    self.abort(&current);
                    return Err(error);
                }
                Err(error) => return Err(error),
            };
            let graph = current.graph.as_value();
            let stage = stage_list(graph)?
                .get(current.ordered_hops.len())
                .ok_or(MISSING_STAGES)?;
            let epoch = graph["deployment_epoch"].clone();
            let stage_id = stage["stage_id"].clone();
            let kv_bytes = kv_bytes(&current.request, stage)?;
            let request = ReservationRequest {
                request_id: current.request.request_id.clone(),
                path_id: current.path_id.clone(),
                path_attempt: current.path_attempt,
                placement_id: decision.placement_id.clone(),
                kv_bytes,
                deployment_epoch: epoch.clone(),
                lease_expires_at: lease_base + self.reservation_lease_seconds()?,
            };
            let reservation = self
                .capacity
                .reserve(&request)
                .map_err(PathBuilderError::Ledger)?;
            let live = reservation.accepted
                && python_equal(&reservation.deployment_epoch, &epoch)
                && reservation.expires_at > lease_base;
            if live {
                current.ordered_hops.push(PathHop {
                    stage_id,
                    placement_id: decision.placement_id,
                    reservation_id: reservation.reservation_id,
                    reservation_expires_at: reservation.expires_at,
                    reservation_epoch: reservation.deployment_epoch,
                });
                return Ok(current);
            }
            if reservation.accepted && !reservation.reservation_id.is_empty() {
                self.capacity.release(&[reservation.reservation_id]);
            }
            current.excluded_placements = current
                .excluded_placements
                .with(&decision.placement_id)
                .map_err(graph_id)?;
        }
    }

    /// :233-238 release every hop's reservation.
    pub fn abort(&mut self, build: &PathBuildState) {
        let reservation_ids: Vec<String> = build
            .ordered_hops
            .iter()
            .map(|hop| hop.reservation_id.clone())
            .collect();
        if !reservation_ids.is_empty() {
            self.capacity.release(&reservation_ids);
        }
    }

    /// :240-307: check every hop's lease at the lock time (`admitted_at` when `now` and
    /// `lease_now` are both absent, else `lease_now` when given, else `now`), pick the first
    /// non-excluded loopback from the last hop to the first, validate the manifest, synchronize
    /// when the port can, and commit every reservation. A lease, sync or commit refusal aborts;
    /// a missing loopback and a manifest the graph refuses do not.
    pub fn lock(
        &mut self,
        build: &PathBuildState,
        now: Option<f64>,
        lease_now: Option<f64>,
    ) -> Result<PathManifest, PathBuilderError> {
        if !self.is_complete(build) {
            return Err(routing("path_incomplete"));
        }
        let lock_time = match (now, lease_now) {
            (None, None) => build.request.admitted_at.clone(),
            (_, Some(lease_now)) => PyReal::Float(lease_now),
            (Some(now), None) => PyReal::Float(now),
        };
        let graph = build.graph.as_value();
        let epoch = &graph["deployment_epoch"];
        if let Err(error) = validate_hop_leases(&build.ordered_hops, epoch, &lock_time) {
            if let PathBuilderError::Lease(code) = error {
                self.abort(build);
                return Err(routing(code));
            }
            return Err(error);
        }
        let (Some(first), Some(last)) = (build.ordered_hops.first(), build.ordered_hops.last())
        else {
            return Err(MISSING_STAGES);
        };
        let mut loopback = None;
        for edge in edge_list(graph, "loopback_edges")? {
            if python_equal(&edge["from_placement_id"], &last.placement_id)
                && python_equal(&edge["to_placement_id"], &first.placement_id)
                && !build
                    .excluded_edges
                    .contains(&edge["edge_id"])
                    .map_err(graph_id)?
            {
                loopback = Some(edge);
                break;
            }
        }
        let Some(loopback) = loopback else {
            return Err(routing("no_legal_loopback"));
        };
        let manifest = PathManifest {
            path_id: build.path_id.clone(),
            path_attempt: build.path_attempt,
            request_id: build.request.request_id.clone(),
            deployment_id: graph["deployment_id"].clone(),
            deployment_epoch: epoch.clone(),
            topology_version: graph["topology_version"].clone(),
            manifest_digest: graph["manifest_digest"].clone(),
            ordered_hops: build.ordered_hops.clone(),
            loopback_edge_id: loopback["edge_id"].clone(),
            protocol: PATH_MANIFEST_PROTOCOL.to_owned(),
        };
        validate_manifest(&manifest, &build.graph)?;
        if let Some(synchronized) = self.capacity.synchronize_build(build)
            && !synchronized.accepted
        {
            self.abort(build);
            return Err(PathBuilderError::Routing {
                code: "path_reservation_sync_rejected",
                detail: synchronized.reason,
            });
        }
        let reservation_ids: Vec<String> = build
            .ordered_hops
            .iter()
            .map(|hop| hop.reservation_id.clone())
            .collect();
        let commit = self.capacity.commit(&reservation_ids, epoch);
        if !commit.accepted {
            self.abort(build);
            return Err(PathBuilderError::Routing {
                code: "path_commit_rejected",
                detail: commit.reason,
            });
        }
        Ok(manifest)
    }
}
