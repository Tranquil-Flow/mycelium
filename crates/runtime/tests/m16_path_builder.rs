// SPDX-License-Identifier: AGPL-3.0-only
//! M16 progressive path builder (CL-L1-07): source parity for `RoutePolicy` and
//! `ProgressivePathBuilder` (mycelium_router/routing.py:21-307 @ f4e366e9), `validate_hop_leases`
//! (leases.py:38-56), `validate_manifest` (validation.py:181-235) and the path contracts
//! (contracts.py:84-105, 222-241), composed as the M16 coordinator composes them
//! (mycelium_m16_runtime.py:389-393, the admit loop :424-443). The first seven tests' expected
//! values are derived by hand from the oracle lines cited beside them; none is computed by the
//! builder. The last test replays paired probes recorded from the real oracle
//! (fixtures/m16-path-builder-oracle.json).
use protocol::ContractError;
use protocol::execution_graph::{self, Graph};
use runtime::m16_capacity::{PlacementCapacity, PyNumber};
use runtime::m16_ledger::{
    IdSource, LeaseClock, LedgerError, ReservationCommitResult, ReservationRequest,
    ReservationResult, ResourceLedger,
};
use runtime::m16_path_builder::{
    CapacityPort, LeaseSecondsProvider, PathBuilderError, ProgressivePathBuilder, ProviderRaised,
    RoutePolicy, validate_hop_leases, validate_manifest,
};
use runtime::m16_route_scorer::{RouteScorer, ScorerError};
use runtime::m16_router_contracts::{
    DeviceState, IdMap, IdSet, PATH_MANIFEST_PROTOCOL, PathBuildState, PathHop, PathManifest,
    PyReal, RequestContext, RouterConfig, ScoreBreakdown, Unhashable,
};
use serde_json::{Value, json};
use std::cell::{Cell, RefCell};
use std::collections::VecDeque;
use std::rc::Rc;

const INF: f64 = f64::INFINITY;
const NAN: f64 = f64::NAN;
const INT_TOO_LARGE: &str = "m16_path_builder_int_too_large_for_float";
static NULL: Value = Value::Null;

fn f(x: f64) -> PyReal {
    PyReal::Float(x)
}

fn i(n: i64) -> PyReal {
    PyReal::int(n)
}

/// `10**exponent` as a Python int.
fn big(exponent: usize) -> PyReal {
    PyReal::signed(
        false,
        format!("1{}", "0".repeat(exponent))
            .parse()
            .expect("decimal digits"),
    )
}

/// A non-negative Python int from its decimal digits.
fn int(text: &str) -> PyNumber {
    PyNumber::Int(text.parse().expect("decimal digits"))
}

/// A JSON number literal kept verbatim (an int of any size).
fn number(text: &str) -> Value {
    serde_json::from_str(text).expect("a JSON number")
}

/// A settable lease clock (starts at 0.0).
#[derive(Default)]
struct Clock(Cell<f64>);

impl LeaseClock for Clock {
    fn now(&self) -> f64 {
        self.0.get()
    }
}

/// `new(prefix)` gives `prefix-1`, `prefix-2`, ... across prefixes (one counter, as the M16
/// coordinator shares one id source between its ledger and builder, :383-393).
#[derive(Default)]
struct Ids(Cell<u64>);

impl IdSource for Ids {
    fn new_id(&self, prefix: &str) -> String {
        let n = self.0.get() + 1;
        self.0.set(n);
        format!("{prefix}-{n}")
    }
}

/// One placement: `(placement_id, node_id, lifecycle_state)`.
type Pl = (Value, Value, Option<&'static str>);

fn pl(id: impl Into<Value>, node: impl Into<Value>) -> Pl {
    (id.into(), node.into(), None)
}

fn pl_state(id: &str, node: &str, lifecycle: &'static str) -> Pl {
    (json!(id), json!(node), Some(lifecycle))
}

/// A JSON id as Python's f-string shows it (`link-{edge_id}`).
fn text(v: &Value) -> String {
    v.as_str().map_or_else(|| v.to_string(), str::to_owned)
}

fn edge_docs(list: &[(Value, Value, Value)]) -> Value {
    Value::Array(
        list.iter()
            .map(|(id, from, to)| {
                json!({"edge_id": id, "from_placement_id": from, "to_placement_id": to,
                       "link_id": format!("link-{}", text(id))})
            })
            .collect(),
    )
}

/// An execution-graph document as the probe's `graph_doc` builds it: stage k is `stage-k` with
/// layers [k, k+1), prefill 2, decode 1 and the given KV cost; every placement of a stage feeds
/// every placement of the next (`edge-n` in that order) and every final placement loops back to
/// every first-stage placement (`loop-n`).
fn graph_doc(stages: &[(Value, Vec<Pl>)]) -> Value {
    let ids = |k: usize| stages[k].1.iter().map(|p| p.0.clone()).collect::<Vec<_>>();
    let mut edges = Vec::new();
    for k in 1..stages.len() {
        for from in ids(k - 1) {
            for to in ids(k) {
                edges.push((json!(format!("edge-{}", edges.len())), from.clone(), to));
            }
        }
    }
    let last = stages.len() - 1;
    let mut loopbacks = Vec::new();
    for from in ids(last) {
        for to in ids(0) {
            loopbacks.push((json!(format!("loop-{}", loopbacks.len())), from.clone(), to));
        }
    }
    let stage_docs: Vec<Value> = stages
        .iter()
        .enumerate()
        .map(|(k, (kv, placements))| {
            let placement_docs: Vec<Value> = placements
                .iter()
                .enumerate()
                .map(|(j, (id, node, lifecycle))| {
                    let mut p = json!({
                        "placement_id": id, "node_id": node,
                        "replica_group_id": format!("group-{k}"),
                        "assignment_id": format!("assignment-{k}-{j}"),
                        "stage_signature": format!("signature-{k}"),
                        "load_proof_digest": format!("proof-{k}-{j}"),
                        "runtime_backend": "numpy",
                        "runtime_endpoint": format!("local://{k}-{j}"),
                    });
                    if let Some(state) = lifecycle {
                        p["lifecycle_state"] = json!(state);
                    }
                    p
                })
                .collect();
            json!({
                "stage_id": format!("stage-{k}"),
                "range": {"start_layer": k, "end_layer_exclusive": k + 1, "layer_count": 1},
                "component_roles": ["decoder"],
                "stage_cost": {"prefill_work_units_per_prompt_token": 2,
                               "decode_work_units_per_token": 1,
                               "kv_bytes_per_context_token": kv},
                "placements": placement_docs,
            })
        })
        .collect();
    json!({
        "protocol": "mycelium.execution_graph.v1",
        "deployment_id": "deployment-m16", "deployment_epoch": 1, "topology_version": 1,
        "model_id": "model-m16", "resolved_commit": "commit-m16", "manifest_digest": "digest-m16",
        "entry_stage_id": "stage-0", "final_stage_id": format!("stage-{last}"),
        "hidden_size": 8, "activation_bytes": 2, "token_envelope_bytes": 16,
        "stages": stage_docs, "edges": edge_docs(&edges), "loopback_edges": edge_docs(&loopbacks),
    })
}

fn e(id: impl Into<Value>, from: &str, to: &str) -> (Value, Value, Value) {
    (id.into(), json!(from), json!(to))
}

fn with_edges(mut doc: Value, key: &str, list: &[(Value, Value, Value)]) -> Value {
    doc[key] = edge_docs(list);
    doc
}

fn parse(doc: &Value) -> Graph {
    execution_graph::parse(doc).expect("a valid execution graph")
}

/// stage-0 [p0b on node-a, p0a on node-b], stage-1 [p1b on node-c, p1a on node-d]: edge-0
/// p0b->p1b, edge-1 p0b->p1a, edge-2 p0a->p1b, edge-3 p0a->p1a, every loopback.
fn mesh() -> Value {
    graph_doc(&[
        (json!(0), vec![pl("p0b", "node-a"), pl("p0a", "node-b")]),
        (json!(0), vec![pl("p1b", "node-c"), pl("p1a", "node-d")]),
    ])
}

/// stage-0 [p0a on node-a, p0b on node-b], stage-1 [p1 on node-c]: edge-0 p0a->p1, edge-1
/// p0b->p1, loop-0 p1->p0a, loop-1 p1->p0b.
fn fan_in() -> Value {
    graph_doc(&[
        (json!(0), vec![pl("p0a", "node-a"), pl("p0b", "node-b")]),
        (json!(0), vec![pl("p1", "node-c")]),
    ])
}

/// stage-0 [p0 on node-a], stage-1 [p1a on node-b, p1b on node-c, p1c on node-d]: edges e2
/// p0->p1a, e1 p0->p1b, e10 p0->p1c; loop-0 p1a->p0, loop-1 p1b->p0, loop-2 p1c->p0.
fn fan_out() -> Value {
    with_edges(
        graph_doc(&[
            (json!(0), vec![pl("p0", "node-a")]),
            (
                json!(0),
                vec![
                    pl("p1a", "node-b"),
                    pl("p1b", "node-c"),
                    pl("p1c", "node-d"),
                ],
            ),
        ]),
        "edges",
        &[
            e("e2", "p0", "p1a"),
            e("e1", "p0", "p1b"),
            e("e10", "p0", "p1c"),
        ],
    )
}

/// Updated at 100.0, ALIVE, 1024.0 units/s, half free, no queue, no neighbours.
fn state(node: &str) -> DeviceState {
    DeviceState {
        node_id: json!(node),
        state_seq: i(1),
        last_updated: f(100.0),
        availability: "ALIVE".to_owned(),
        compute_units_per_second: f(1024.0),
        free_compute_fraction: f(0.5),
        available_kv_bytes: i(0),
        pending_hop_queue_depth: i(0),
        neighbor_rtt_ms: IdMap::new(),
        neighbor_bandwidth_bytes_per_second: IdMap::new(),
    }
}

fn with_rtt(mut s: DeviceState, to: &str, rtt: f64) -> DeviceState {
    s.neighbor_rtt_ms
        .insert(json!(to), f(rtt))
        .expect("hashable");
    s
}

fn dead(node: &str) -> DeviceState {
    state(node).with_availability("DEAD")
}

fn states(list: Vec<DeviceState>) -> IdMap<DeviceState> {
    let mut map = IdMap::new();
    for s in list {
        map.insert(s.node_id.clone(), s).expect("hashable node id");
    }
    map
}

fn nodes() -> IdMap<DeviceState> {
    states(vec![
        state("node-a"),
        state("node-b"),
        state("node-c"),
        state("node-d"),
    ])
}

/// The gateway's request shape (mycelium_request_gateway/backend.py:227-239): 4 prompt tokens.
fn request() -> RequestContext {
    RequestContext {
        request_id: "request-1".to_owned(),
        prompt_token_ids: (0..4).map(i).collect(),
        max_new_tokens: i(8),
        expected_new_tokens: i(8),
        qos_class: "interactive".to_owned(),
        admitted_at: f(0.0),
        target_ttft_ms: f(1000.0),
        target_tpot_ms: f(100.0),
        target_tokens_per_second: f(10.0),
        sampling_seed: i(0),
        generation_config_digest: "sha256:test".to_owned(),
    }
}

/// `RouterConfig(reservation_lease_seconds=3_600.0)`, the factory's (mycelium_m16_runtime.py:1260).
fn factory_config() -> RouterConfig {
    let mut config = RouterConfig::oracle_default().expect("oracle defaults");
    config.reservation_lease_seconds = f(3600.0);
    config
}

fn policy() -> RoutePolicy {
    RoutePolicy::new(RouteScorer::new(factory_config()))
}

/// scoring.py:40-168 for a two-stage route over two distinct default nodes (no neighbour
/// entries: rtt 100.0, bandwidth 1e6), 4 prompt tokens, interactive: rate 1024.0*0.5 = 512.0;
/// per stage prefill 2*4/512*1000 = 15.625, decode 1/512*1000 = 1.953125, no queue; payloads
/// 4*8*2 = 64 (prefill) and 16 (decode, token); links 100/2 + bytes/1e6*1000, the token
/// loopback as the decode link; total 0.35*(ttft/1000) + (1-0.35)*(tpot/100).
fn default_two_stage_total() -> f64 {
    let prefill_link = 100.0 / 2.0 + 64.0 / 1_000_000.0 * 1000.0;
    let decode_link = 100.0 / 2.0 + 16.0 / 1_000_000.0 * 1000.0;
    let ttft = (0.0 + (15.625 + 0.0) + (15.625 + 0.0)) + (0.0 + prefill_link);
    let tpot = (0.0 + (1.953125 + 0.0) + (1.953125 + 0.0)) + (0.0 + decode_link + decode_link);
    0.35 * (ttft / 1000.0) + (1.0 - 0.35) * (tpot / 100.0)
}

/// One call the builder made on its capacity port.
#[derive(Clone, Debug, PartialEq)]
enum Event {
    Reserve(ReservationRequest),
    Release(Vec<String>),
    Commit(Vec<String>, Value),
    Sync(String, usize),
}

/// A capacity port that logs every call and answers from the real ledger when it has one, else
/// from scripted results (and has `synchronize_build` only when syncs are scripted).
#[derive(Default)]
struct Port<'a> {
    ledger: Option<ResourceLedger<&'a Clock, &'a Ids>>,
    reserves: VecDeque<ReservationResult>,
    commits: VecDeque<ReservationCommitResult>,
    syncs: Option<VecDeque<ReservationCommitResult>>,
    events: Vec<Event>,
}

impl CapacityPort for Port<'_> {
    fn reserve(&mut self, request: &ReservationRequest) -> Result<ReservationResult, LedgerError> {
        self.events.push(Event::Reserve(request.clone()));
        match &mut self.ledger {
            Some(ledger) => ledger.reserve(request),
            None => Ok(self.reserves.pop_front().expect("a scripted reservation")),
        }
    }

    fn release(&mut self, reservation_ids: &[String]) {
        self.events.push(Event::Release(reservation_ids.to_vec()));
        if let Some(ledger) = &mut self.ledger {
            ledger.release(reservation_ids);
        }
    }

    fn commit(
        &mut self,
        reservation_ids: &[String],
        deployment_epoch: &Value,
    ) -> ReservationCommitResult {
        self.events.push(Event::Commit(
            reservation_ids.to_vec(),
            deployment_epoch.clone(),
        ));
        match &mut self.ledger {
            Some(ledger) => ledger.commit(reservation_ids, deployment_epoch),
            None => self.commits.pop_front().expect("a scripted commit"),
        }
    }

    fn synchronize_build(&mut self, build: &PathBuildState) -> Option<ReservationCommitResult> {
        let syncs = self.syncs.as_mut()?;
        self.events
            .push(Event::Sync(build.path_id.clone(), build.ordered_hops.len()));
        Some(syncs.pop_front().expect("a scripted sync"))
    }
}

fn scripted(
    reserves: Vec<ReservationResult>,
    commits: Vec<ReservationCommitResult>,
) -> Port<'static> {
    Port {
        reserves: reserves.into(),
        commits: commits.into(),
        ..Port::default()
    }
}

fn accept(id: &str, epoch: Value, expires_at: f64) -> ReservationResult {
    ReservationResult {
        accepted: true,
        reservation_id: id.to_owned(),
        reason: "",
        deployment_epoch: epoch,
        expires_at,
    }
}

/// contracts.py:197-202 a refusal keeps the defaults -1 and 0.0.
fn refuse(reason: &'static str, id: &str) -> ReservationResult {
    ReservationResult {
        accepted: false,
        reservation_id: id.to_owned(),
        reason,
        deployment_epoch: json!(-1),
        expires_at: 0.0,
    }
}

fn committed() -> ReservationCommitResult {
    ReservationCommitResult {
        accepted: true,
        reason: "",
    }
}

fn rejected(reason: &'static str) -> ReservationCommitResult {
    ReservationCommitResult {
        accepted: false,
        reason,
    }
}

type Builder<'a> = ProgressivePathBuilder<Port<'a>, &'a Ids>;

fn builder<'a>(
    config: RouterConfig,
    port: Port<'a>,
    ids: &'a Ids,
    provider: Option<LeaseSecondsProvider>,
) -> Builder<'a> {
    ProgressivePathBuilder::new(
        RoutePolicy::new(RouteScorer::new(config)),
        port,
        ids,
        provider,
    )
}

fn start<P: CapacityPort, I: IdSource>(
    b: &ProgressivePathBuilder<P, I>,
    request: &RequestContext,
    graph: &Graph,
) -> PathBuildState {
    b.start(request, graph, 0, IdSet::new(), IdSet::new(), IdSet::new())
}

fn set(ids: &[Value]) -> IdSet {
    IdSet::of(ids.iter().cloned()).expect("hashable ids")
}

fn hop(
    stage: &str,
    placement: impl Into<Value>,
    reservation: &str,
    expires: f64,
    epoch: Value,
) -> PathHop {
    PathHop {
        reservation_expires_at: expires,
        reservation_epoch: epoch,
        ..PathHop::new(json!(stage), placement.into(), reservation)
    }
}

/// A reservation request of `request-1` on `path-1`, attempt 0, at the graph's epoch 1.
fn reserved(placement: &str, kv_bytes: PyNumber, lease_expires_at: f64) -> Event {
    Event::Reserve(ReservationRequest {
        request_id: "request-1".to_owned(),
        path_id: "path-1".to_owned(),
        path_attempt: 0,
        placement_id: json!(placement),
        kv_bytes,
        deployment_epoch: json!(1),
        lease_expires_at,
    })
}

fn released(ids: &[&str]) -> Event {
    Event::Release(ids.iter().map(|id| (*id).to_owned()).collect())
}

fn commit_event(ids: &[&str]) -> Event {
    Event::Commit(ids.iter().map(|id| (*id).to_owned()).collect(), json!(1))
}

fn routing(code: &'static str) -> PathBuilderError {
    PathBuilderError::Routing { code, detail: "" }
}

fn python(code: &'static str) -> PathBuilderError {
    PathBuilderError::Python(code)
}

fn routes(list: &[&[&str]]) -> Result<Vec<Vec<Value>>, PathBuilderError> {
    Ok(list
        .iter()
        .map(|route| route.iter().map(|id| json!(id)).collect())
        .collect())
}

/// A base lease, what the provider gives (or `None` for no provider), and the expected bits.
type LeaseCase = (
    PyReal,
    Option<Result<PyReal, ProviderRaised>>,
    Result<u64, PathBuilderError>,
);

#[test]
fn m16_path_builder_contracts() {
    // contracts.py:7
    assert_eq!(PATH_MANIFEST_PROTOCOL, "mycelium.path_manifest.v1");
    // contracts.py:84-90: the expiry defaults to float("inf"), the epoch to -1.
    let h = PathHop::new(json!("stage-0"), json!("p0"), "r-1");
    assert_eq!(
        (&h.stage_id, &h.placement_id, h.reservation_id.as_str()),
        (&json!("stage-0"), &json!("p0"), "r-1")
    );
    assert_eq!(h.reservation_expires_at.to_bits(), INF.to_bits());
    assert_eq!(h.reservation_epoch, json!(-1));
    // Output identity is bit identity: a NaN expiry is itself, 0.0 is not -0.0.
    let nan_hop = PathHop {
        reservation_expires_at: NAN,
        ..h.clone()
    };
    assert_eq!(nan_hop, nan_hop.clone());
    assert_ne!(
        hop("s", "p", "r", 0.0, json!(1)),
        hop("s", "p", "r", -0.0, json!(1))
    );
    // contracts.py:229-241: no hops and empty exclusions; with_excluded_devices replaces only
    // the devices.
    let graph = parse(&fan_in());
    let fresh = PathBuildState::new(request(), graph.clone(), "path-9", 3);
    assert_eq!(
        (
            fresh.path_id.as_str(),
            fresh.path_attempt,
            fresh.ordered_hops.len()
        ),
        ("path-9", 3, 0)
    );
    assert_eq!(
        (
            &fresh.excluded_placements,
            &fresh.excluded_edges,
            &fresh.excluded_devices
        ),
        (&IdSet::new(), &IdSet::new(), &IdSet::new())
    );
    let narrowed = PathBuildState {
        excluded_placements: set(&[json!("p0a")]),
        ordered_hops: vec![h.clone()],
        ..fresh.clone()
    }
    .with_excluded_devices(set(&[json!("node-c")]));
    assert_eq!(narrowed.excluded_devices, set(&[json!("node-c")]));
    assert_eq!(narrowed.excluded_placements, set(&[json!("p0a")]));
    assert_eq!(narrowed.ordered_hops, vec![h.clone()]);
    assert_eq!(
        (narrowed.path_id.as_str(), narrowed.path_attempt),
        ("path-9", 3)
    );
    // A frozenset: membership is Python == (1 == 1.0 == True, "1" != 1); a list is unhashable.
    let ones = set(&[json!(1)]);
    assert_eq!(ones.contains(&json!(1.0)), Ok(true));
    assert_eq!(ones.contains(&json!(true)), Ok(true));
    assert_eq!(ones.contains(&json!("1")), Ok(false));
    assert_eq!(ones.contains(&json!([1])), Err(Unhashable));
    assert_eq!(IdSet::of([json!({"a": 1})]).map(|_| ()), Err(Unhashable));
    assert_eq!(ones.with(&json!(1.0)).map(|s| s.entries().len()), Ok(1));
    assert_eq!(ones.with(&json!(2)).map(|s| s.entries().len()), Ok(2));

    // routing.py:152-175: start draws the path id from the id source and keeps the exclusions;
    // a build is complete when it has one hop per stage.
    let ids = Ids::default();
    let b = builder(factory_config(), Port::default(), &ids, None);
    let build = b.start(
        &request(),
        &graph,
        2,
        set(&[json!("p0b")]),
        set(&[json!("edge-1")]),
        set(&[json!("node-c")]),
    );
    assert_eq!(
        (
            build.path_id.as_str(),
            build.path_attempt,
            build.ordered_hops.len()
        ),
        ("path-1", 2, 0)
    );
    assert_eq!(build.excluded_placements, set(&[json!("p0b")]));
    assert_eq!(build.excluded_edges, set(&[json!("edge-1")]));
    assert_eq!(build.excluded_devices, set(&[json!("node-c")]));
    assert_eq!(build.request, request());
    let with_hops = |n: usize| PathBuildState {
        ordered_hops: vec![h.clone(); n],
        ..build.clone()
    };
    assert_eq!(
        [0, 1, 2, 3].map(|n| b.is_complete(&with_hops(n))),
        [false, false, true, false]
    );

    // routing.py:136-150: float(config) is the base; a provider that raises or returns a
    // non-finite value gives the base, else max(base, scaled) (the second only if it is
    // greater, so a NaN base stays and -0.0 never replaces 0.0).
    let lease = |x: f64| Ok(x.to_bits());
    let cases: Vec<LeaseCase> = vec![
        (f(3600.0), None, lease(3600.0)),
        (i(30), None, lease(30.0)),
        (PyReal::Bool(true), None, lease(1.0)),
        (big(400), None, Err(python(INT_TOO_LARGE))),
        (f(3600.0), Some(Err(ProviderRaised)), lease(3600.0)),
        (f(3600.0), Some(Ok(f(NAN))), lease(3600.0)),
        (f(3600.0), Some(Ok(f(INF))), lease(3600.0)),
        (f(3600.0), Some(Ok(f(-INF))), lease(3600.0)),
        (f(3600.0), Some(Ok(f(10.0))), lease(3600.0)),
        (f(3600.0), Some(Ok(f(7200.5))), lease(7200.5)),
        // float(10**400) raises OverflowError, which `except Exception` catches.
        (f(3600.0), Some(Ok(big(400))), lease(3600.0)),
        (f(3600.0), Some(Ok(i(4000))), lease(4000.0)),
        (f(3600.0), Some(Ok(PyReal::Bool(true))), lease(3600.0)),
        (f(NAN), Some(Ok(f(10.0))), lease(NAN)),
        (f(INF), Some(Ok(f(10.0))), lease(INF)),
        (f(0.0), Some(Ok(f(-0.0))), lease(0.0)),
        (f(-0.0), Some(Ok(f(0.0))), lease(-0.0)),
        // The base is converted first (:140), before the provider is called.
        (big(400), Some(Ok(f(10.0))), Err(python(INT_TOO_LARGE))),
    ];
    for (base, provided, expected) in cases {
        let mut config = factory_config();
        config.reservation_lease_seconds = base.clone();
        let provider = provided
            .clone()
            .map(|out| Box::new(move || out.clone()) as LeaseSecondsProvider);
        let b = builder(config, Port::default(), &ids, provider);
        assert_eq!(
            b.reservation_lease_seconds().map(f64::to_bits),
            expected,
            "base {base:?}, provider {provided:?}"
        );
    }

    // The exception kinds (admit catches only the routing kind, mycelium_m16_runtime.py:442).
    let kinds = [
        (routing("no_feasible_route"), "routing", "no_feasible_route"),
        (
            PathBuilderError::Contract("missing_reservation_id"),
            "contract",
            "missing_reservation_id",
        ),
        (
            PathBuilderError::Lease("reservation_expired"),
            "lease",
            "reservation_expired",
        ),
        (
            PathBuilderError::Scorer(ScorerError("m16_route_scorer_zero_division")),
            "scorer",
            "m16_route_scorer_zero_division",
        ),
        (
            PathBuilderError::Ledger(LedgerError("m16_ledger_unhashable_placement_id")),
            "ledger",
            "m16_ledger_unhashable_placement_id",
        ),
        (python(INT_TOO_LARGE), "python", INT_TOO_LARGE),
    ];
    for (error, kind, code) in kinds {
        assert_eq!((error.kind(), error.code()), (kind, code));
    }
}

/// Excluded placements, edges and devices.
type Excluded<'a> = [&'a [Value]; 3];

/// routing.py:57-123 `_complete_routes(build, prefix)` with the build's own hop prefix.
fn complete(
    doc: &Value,
    excluded: Excluded,
    hops: Vec<PathHop>,
) -> Result<Vec<Vec<Value>>, PathBuilderError> {
    let build = PathBuildState {
        excluded_placements: set(excluded[0]),
        excluded_edges: set(excluded[1]),
        excluded_devices: set(excluded[2]),
        ordered_hops: hops,
        ..PathBuildState::new(request(), parse(doc), "path-1", 0)
    };
    let prefix: Vec<Value> = build
        .ordered_hops
        .iter()
        .map(|h| h.placement_id.clone())
        .collect();
    policy().complete_routes(&build, &prefix)
}

#[test]
fn m16_path_builder_complete_routes() {
    let none: Excluded = [&[], &[], &[]];
    let at = |stage: &str, placement: &str| hop(stage, placement, "r-1", INF, json!(-1));

    // :94-111 stage-0 placements in graph order, each extended over its outgoing edges sorted
    // by (edge_id, destination); :113-123 full length with a loopback last -> first.
    let mesh_routes = routes(&[
        &["p0b", "p1b"],
        &["p0b", "p1a"],
        &["p0a", "p1b"],
        &["p0a", "p1a"],
    ]);
    assert_eq!(complete(&mesh(), none, vec![]), mesh_routes);
    // The same edges listed in reverse order: still sorted per source.
    let mut reversed = mesh();
    reversed["edges"].as_array_mut().expect("edges").reverse();
    assert_eq!(complete(&reversed, none, vec![]), mesh_routes);
    // String edge ids sort by code point (e1 < e10 < e2), numeric ones by value (2.5 < 9 < 10).
    assert_eq!(
        complete(&fan_out(), none, vec![]),
        routes(&[&["p0", "p1b"], &["p0", "p1c"], &["p0", "p1a"]])
    );
    let numeric = with_edges(
        fan_out(),
        "edges",
        &[e(10, "p0", "p1a"), e(9, "p0", "p1b"), e(2.5, "p0", "p1c")],
    );
    assert_eq!(
        complete(&numeric, none, vec![]),
        routes(&[&["p0", "p1c"], &["p0", "p1b"], &["p0", "p1a"]])
    );
    // :106 an excluded edge, :116 an excluded loopback.
    assert_eq!(
        complete(&fan_out(), [&[], &[json!("e1")], &[]], vec![]),
        routes(&[&["p0", "p1c"], &["p0", "p1a"]])
    );
    assert_eq!(
        complete(&fan_out(), [&[], &[json!("loop-1")], &[]], vec![]),
        routes(&[&["p0", "p1c"], &["p0", "p1a"]])
    );
    // :79-85 a placement is allowed when not excluded, its node not excluded and ACTIVE.
    let life = graph_doc(&[
        (
            json!(0),
            vec![
                pl("p0a", "node-a"),
                pl_state("p0b", "node-b", "RETIRING"),
                pl("p0c", "node-c"),
            ],
        ),
        (
            json!(0),
            vec![pl("p1a", "node-d"), pl_state("p1b", "node-a", "STANDBY")],
        ),
    ]);
    assert_eq!(
        complete(&life, none, vec![]),
        routes(&[&["p0a", "p1a"], &["p0c", "p1a"]])
    );
    assert_eq!(
        complete(&life, [&[], &[], &[json!("node-c")]], vec![]),
        routes(&[&["p0a", "p1a"]])
    );
    assert_eq!(
        complete(&life, [&[json!("p0a")], &[], &[]], vec![]),
        routes(&[&["p0c", "p1a"]])
    );
    // Python-equal exclusions: 1.0 excludes placement 1, True excludes node 1.
    let ints = graph_doc(&[
        (json!(0), vec![pl(1, 1), pl(2, 2)]),
        (json!(0), vec![pl(3, 3)]),
    ]);
    let two_three = Ok(vec![vec![json!(2), json!(3)]]);
    assert_eq!(
        complete(&ints, [&[json!(1.0)], &[], &[]], vec![]),
        two_three
    );
    assert_eq!(
        complete(&ints, [&[], &[], &[json!(true)]], vec![]),
        two_three
    );
    // :87-93 a prefix must be all allowed and end at stage len(prefix)-1.
    assert_eq!(
        complete(&mesh(), none, vec![at("stage-1", "p1a")]),
        Ok(vec![])
    );
    assert_eq!(
        complete(
            &mesh(),
            [&[json!("p0a")], &[], &[]],
            vec![at("stage-0", "p0a")]
        ),
        Ok(vec![])
    );
    assert_eq!(
        complete(
            &mesh(),
            [&[json!("p0a")], &[], &[]],
            vec![at("stage-0", "p0b")]
        ),
        routes(&[&["p0b", "p1b"], &["p0b", "p1a"]])
    );
    // A complete prefix is the one route when its loopback is legal.
    let full = || vec![at("stage-0", "p0a"), at("stage-1", "p1")];
    assert_eq!(complete(&fan_in(), none, full()), routes(&[&["p0a", "p1"]]));
    assert_eq!(
        complete(&fan_in(), [&[], &[json!("loop-0")], &[]], full()),
        Ok(vec![])
    );
    // :80 an unknown prefix placement is a KeyError.
    assert_eq!(
        complete(&mesh(), none, vec![at("stage-0", "nope")]),
        Err(python("m16_path_builder_unknown_hop_placement"))
    );
    // A single stage: a route is one placement with a loopback onto itself.
    let single = graph_doc(&[(
        json!(3),
        vec![pl("solo-b", "node-a"), pl("solo-a", "node-b")],
    )]);
    assert_eq!(
        complete(&single, none, vec![]),
        routes(&[&["solo-b"], &["solo-a"]])
    );
    let crossed = with_edges(
        single.clone(),
        "loopback_edges",
        &[
            e("loop-x", "solo-b", "solo-a"),
            e("loop-y", "solo-a", "solo-b"),
        ],
    );
    assert_eq!(complete(&crossed, none, vec![]), Ok(vec![]));
    // :105 sorted() over a str and an int edge id raises TypeError; :83 so does an unhashable
    // node id, unless the placement is excluded first (`and` short-circuits).
    let mixed = with_edges(
        graph_doc(&[
            (json!(0), vec![pl("p0", "node-a")]),
            (json!(0), vec![pl("p1a", "node-b"), pl("p1b", "node-c")]),
        ]),
        "edges",
        &[e("e1", "p0", "p1a"), e(2, "p0", "p1b")],
    );
    assert_eq!(
        complete(&mixed, none, vec![]),
        Err(python("m16_path_builder_unorderable_edge_ids"))
    );
    let listed = graph_doc(&[
        (json!(0), vec![pl("p0", json!(["node", "a"]))]),
        (json!(0), vec![pl("p1", "node-b")]),
    ]);
    assert_eq!(
        complete(&listed, none, vec![]),
        Err(python("m16_path_builder_unhashable_node_id"))
    );
    assert_eq!(
        complete(&listed, [&[json!("p0")], &[], &[]], vec![]),
        Ok(vec![])
    );
}

#[test]
fn m16_path_builder_choose_next() {
    let policy = policy();
    let now = f(100.0);
    let fresh = |doc: &Value| PathBuildState::new(request(), parse(doc), "path-1", 0);
    let pick = |build: &PathBuildState, s: &IdMap<DeviceState>| {
        policy.choose_next(build, s, &now).map(|d| {
            (
                d.placement_id,
                d.complete_route,
                d.score.total_score.to_bits(),
            )
        })
    };
    let at = |stage: &str, placement: &str| hop(stage, placement, "r-0", INF, json!(-1));
    let total = default_two_stage_total().to_bits();

    // :39-55 every complete route scored; the least (total_score, route) wins and the next
    // placement is route[len(prefix)]. All mesh routes tie, so the route tuple decides.
    let build = fresh(&mesh());
    let decision = policy
        .choose_next(&build, &nodes(), &now)
        .expect("a decision");
    assert_eq!(decision.placement_id, json!("p0a"));
    assert_eq!(decision.complete_route, vec![json!("p0a"), json!("p1a")]);
    assert_eq!(decision.score.total_score.to_bits(), total);
    // The decision carries the scorer's breakdown of that route.
    let scored: Result<ScoreBreakdown, ScorerError> = RouteScorer::new(factory_config())
        .score_route(
            &build.request,
            &build.graph,
            &decision.complete_route,
            &nodes(),
            &now,
        );
    assert_eq!(Ok(decision.score), scored);
    // Int ids compare by value.
    let ints = graph_doc(&[
        (json!(0), vec![pl(2, "node-a"), pl(1, "node-b")]),
        (json!(0), vec![pl(20, "node-c"), pl(10, "node-d")]),
    ]);
    assert_eq!(
        pick(&fresh(&ints), &nodes()),
        Ok((json!(1), vec![json!(1), json!(10)], total))
    );
    // A tie between a str and an int route raises TypeError (:50); different totals never
    // compare the routes.
    let mixed = graph_doc(&[
        (json!(0), vec![pl("a", "node-a"), pl(1, "node-b")]),
        (json!(0), vec![pl("z", "node-c")]),
    ]);
    assert_eq!(
        pick(&fresh(&mixed), &nodes()),
        Err(python("m16_path_builder_unorderable_placement_ids"))
    );
    let slow_a = states(vec![
        with_rtt(state("node-a"), "node-c", 500.0),
        state("node-b"),
        state("node-c"),
    ]);
    assert_eq!(pick(&fresh(&mixed), &slow_a).map(|d| d.0), Ok(json!(1)));
    // The lower total wins over the route order.
    assert_eq!(
        pick(&fresh(&fan_in()), &slow_a),
        Ok((json!("p0b"), vec![json!("p0b"), json!("p1")], total))
    );
    // :46 only inf totals are dropped; a NaN total is kept, and min keeps its first item
    // unless a later key is less, which nothing is than NaN (even a smaller route tuple).
    let nan_a = states(vec![
        with_rtt(state("node-a"), "node-c", NAN),
        state("node-b"),
        state("node-c"),
    ]);
    let d = policy
        .choose_next(&fresh(&fan_in()), &nan_a, &now)
        .expect("a NaN decision");
    assert_eq!(
        (d.placement_id, d.score.total_score.is_nan()),
        (json!("p0a"), true)
    );
    let reorder = graph_doc(&[
        (json!(0), vec![pl("p0b", "node-a"), pl("p0a", "node-b")]),
        (json!(0), vec![pl("p1", "node-c")]),
    ]);
    let d = policy
        .choose_next(&fresh(&reorder), &nan_a, &now)
        .expect("a NaN decision");
    assert_eq!(
        (d.placement_id, d.score.total_score.is_nan()),
        (json!("p0b"), true)
    );
    let nan_b = states(vec![
        state("node-a"),
        with_rtt(state("node-b"), "node-c", NAN),
        state("node-c"),
    ]);
    assert_eq!(
        pick(&fresh(&fan_in()), &nan_b),
        Ok((json!("p0a"), vec![json!("p0a"), json!("p1")], total))
    );
    // :48-49 nothing scored below inf.
    let gone = states(vec![dead("node-a"), dead("node-b"), state("node-c")]);
    assert_eq!(
        pick(&fresh(&fan_in()), &gone),
        Err(routing("no_feasible_route"))
    );
    // An excluded loopback removes its routes.
    let no_loop0 = PathBuildState {
        excluded_edges: set(&[json!("loop-0")]),
        ..fresh(&fan_out())
    };
    assert_eq!(
        pick(&no_loop0, &nodes()),
        Ok((json!("p0"), vec![json!("p0"), json!("p1b")], total))
    );
    // With a prefix the next stage's placement of the best completion.
    let prefixed = PathBuildState {
        ordered_hops: vec![at("stage-0", "p0b")],
        ..fresh(&mesh())
    };
    assert_eq!(
        pick(&prefixed, &nodes()),
        Ok((json!("p1a"), vec![json!("p0b"), json!("p1a")], total))
    );
    // A complete prefix: route[len(prefix)] is an IndexError.
    let full = PathBuildState {
        ordered_hops: vec![at("stage-0", "p0a"), at("stage-1", "p1")],
        ..fresh(&fan_in())
    };
    assert_eq!(
        pick(&full, &nodes()),
        Err(python("m16_path_builder_route_index_out_of_range"))
    );
    // The scorer's own exception propagates (target_ttft_ms == 0, scoring.py:157).
    let mut zero = fresh(&fan_in());
    zero.request.target_ttft_ms = f(0.0);
    assert_eq!(
        pick(&zero, &nodes()),
        Err(PathBuilderError::Scorer(ScorerError(
            "m16_route_scorer_zero_division"
        )))
    );
}

#[test]
fn m16_path_builder_advance_reservations() {
    let twelve_e30 = int(&format!("12{}", "0".repeat(30)));
    let kv_doc = graph_doc(&[
        (
            number(&format!("1{}", "0".repeat(30))),
            vec![pl("p0", "node-a")],
        ),
        (json!(1.5), vec![pl("p1", "node-b")]),
    ]);
    // :195-224 kv bytes = (len(prompt) + max_new_tokens) * the stage's kv cost, kept as the
    // Python int or float it is; lease_expires_at = lease_base + the lease seconds; a live
    // reservation becomes the stage's hop.
    {
        let ids = Ids::default();
        let graph = parse(&kv_doc);
        let mut b = builder(
            factory_config(),
            scripted(
                vec![
                    accept("r-a", json!(1), 3605.0),
                    accept("r-b", json!(1), 3605.0),
                ],
                vec![],
            ),
            &ids,
            None,
        );
        let build = start(&b, &request(), &graph);
        let one = b
            .advance(&build, &nodes(), 100.0, Some(5.0))
            .expect("stage 0");
        let first = hop("stage-0", "p0", "r-a", 3605.0, json!(1));
        assert_eq!(one.ordered_hops, vec![first.clone()]);
        let two = b
            .advance(&one, &nodes(), 100.0, Some(5.0))
            .expect("stage 1");
        assert_eq!(
            two.ordered_hops,
            vec![first, hop("stage-1", "p1", "r-b", 3605.0, json!(1))]
        );
        assert!(b.is_complete(&two));
        assert_eq!(two.path_id, "path-1");
        assert_eq!(
            b.capacity().events,
            vec![
                reserved("p0", twelve_e30, 3605.0),
                reserved("p1", PyNumber::Float(18.0), 3605.0),
            ]
        );
    }
    // A bool max_new_tokens is the int 1: (4 + True) * 10**30. :188 with no lease_now the
    // base is now.
    {
        let ids = Ids::default();
        let mut req = request();
        req.max_new_tokens = PyReal::Bool(true);
        let mut b = builder(
            factory_config(),
            scripted(vec![accept("r-a", json!(1), 3700.0)], vec![]),
            &ids,
            None,
        );
        let build = start(&b, &req, &parse(&kv_doc));
        let one = b.advance(&build, &nodes(), 100.0, None).expect("stage 0");
        assert_eq!(
            one.ordered_hops,
            vec![hop("stage-0", "p0", "r-a", 3700.0, json!(1))]
        );
        assert_eq!(
            b.capacity().events,
            vec![reserved("p0", int(&format!("5{}", "0".repeat(30))), 3700.0)]
        );
    }
    // :211-215 live only if accepted, at the graph's epoch and expiring after the lease base:
    // one expiring exactly at the base is released and its placement excluded, and the next
    // placement is reserved; the returned build keeps the exclusion.
    {
        let ids = Ids::default();
        let mut b = builder(
            factory_config(),
            scripted(
                vec![
                    accept("r-a", json!(1), 5.0),
                    accept("r-b", json!(1), 5.000000000000001),
                ],
                vec![],
            ),
            &ids,
            None,
        );
        let build = start(&b, &request(), &parse(&fan_in()));
        let one = b
            .advance(&build, &nodes(), 100.0, Some(5.0))
            .expect("stage 0");
        assert_eq!(
            one.ordered_hops,
            vec![hop("stage-0", "p0b", "r-b", 5.000000000000001, json!(1))]
        );
        assert_eq!(one.excluded_placements, set(&[json!("p0a")]));
        assert_eq!(
            b.capacity().events,
            vec![
                reserved("p0a", PyNumber::from(0), 3605.0),
                released(&["r-a"]),
                reserved("p0b", PyNumber::from(0), 3605.0),
            ]
        );
    }
    // :225-226 an accepted reservation at another epoch is released; a refusal is not (even
    // with an id); with every placement excluded, no_feasible_route.
    {
        let ids = Ids::default();
        let mut b = builder(
            factory_config(),
            scripted(
                vec![
                    accept("r-a", json!(2), 3605.0),
                    refuse("resource_unavailable", "r-x"),
                ],
                vec![],
            ),
            &ids,
            None,
        );
        let build = start(&b, &request(), &parse(&fan_in()));
        assert_eq!(
            b.advance(&build, &nodes(), 100.0, Some(5.0))
                .map(|s| s.ordered_hops),
            Err(routing("no_feasible_route"))
        );
        assert_eq!(
            b.capacity().events,
            vec![
                reserved("p0a", PyNumber::from(0), 3605.0),
                released(&["r-a"]),
                reserved("p0b", PyNumber::from(0), 3605.0),
            ]
        );
    }
    // An accepted reservation without an id is not released; an epoch 1.0 equals epoch 1 and
    // the hop keeps the reservation's 1.0.
    {
        let ids = Ids::default();
        let mut b = builder(
            factory_config(),
            scripted(
                vec![accept("", json!(1), 1.0), accept("r-b", json!(1.0), 3605.0)],
                vec![],
            ),
            &ids,
            None,
        );
        let build = start(&b, &request(), &parse(&fan_in()));
        let one = b
            .advance(&build, &nodes(), 100.0, Some(5.0))
            .expect("stage 0");
        assert_eq!(
            one.ordered_hops,
            vec![hop("stage-0", "p0b", "r-b", 3605.0, json!(1.0))]
        );
        assert_eq!(
            b.capacity().events,
            vec![
                reserved("p0a", PyNumber::from(0), 3605.0),
                reserved("p0b", PyNumber::from(0), 3605.0),
            ]
        );
    }
    // :192-194 no_feasible_route aborts: every earlier hop's reservation is released.
    {
        let ids = Ids::default();
        let mut b = builder(
            factory_config(),
            scripted(vec![refuse("resource_unavailable", "")], vec![]),
            &ids,
            None,
        );
        let build = PathBuildState {
            ordered_hops: vec![hop("stage-0", "p0a", "r-0", 9e9, json!(1))],
            ..start(&b, &request(), &parse(&fan_in()))
        };
        assert_eq!(
            b.advance(&build, &nodes(), 100.0, Some(5.0))
                .map(|s| s.ordered_hops),
            Err(routing("no_feasible_route"))
        );
        assert_eq!(
            b.capacity().events,
            vec![
                reserved("p1", PyNumber::from(0), 3605.0),
                released(&["r-0"]),
            ]
        );
    }
    // :185-186 a complete build; the scorer's exception, a kv overflow and a base lease
    // overflow propagate without abort (only RoutingError aborts, :192).
    {
        let ids = Ids::default();
        let single = graph_doc(&[(
            json!(3),
            vec![pl("solo-b", "node-a"), pl("solo-a", "node-b")],
        )]);
        let mut b = builder(factory_config(), Port::default(), &ids, None);
        let done = PathBuildState {
            ordered_hops: vec![hop("stage-0", "solo-a", "r-0", INF, json!(-1))],
            ..start(&b, &request(), &parse(&single))
        };
        assert_eq!(
            b.advance(&done, &nodes(), 1.0, None)
                .map(|s| s.ordered_hops),
            Err(routing("path_already_complete"))
        );
        let mut zero = request();
        zero.target_ttft_ms = f(0.0);
        let held = PathBuildState {
            ordered_hops: vec![hop("stage-0", "p0a", "r-0", INF, json!(-1))],
            ..start(&b, &zero, &parse(&fan_in()))
        };
        assert_eq!(
            b.advance(&held, &nodes(), 100.0, None)
                .map(|s| s.ordered_hops),
            Err(PathBuilderError::Scorer(ScorerError(
                "m16_route_scorer_zero_division"
            )))
        );
        let mut huge = request();
        huge.max_new_tokens = big(400);
        let held = PathBuildState {
            ordered_hops: vec![hop("stage-0", "p0", "r-0", 9e9, json!(1))],
            ..start(&b, &huge, &parse(&kv_doc))
        };
        assert_eq!(
            b.advance(&held, &nodes(), 100.0, None)
                .map(|s| s.ordered_hops),
            Err(python(INT_TOO_LARGE))
        );
        assert!(b.capacity().events.is_empty());
    }
    {
        let ids = Ids::default();
        let mut config = factory_config();
        config.reservation_lease_seconds = big(400);
        let mut b = builder(config, Port::default(), &ids, None);
        let build = start(&b, &request(), &parse(&fan_in()));
        assert_eq!(
            b.advance(&build, &nodes(), 100.0, None)
                .map(|s| s.ordered_hops),
            Err(python(INT_TOO_LARGE))
        );
        assert!(b.capacity().events.is_empty());
    }
    // :208 the provider is read for every reservation attempt.
    {
        let ids = Ids::default();
        let calls = Rc::new(RefCell::new(VecDeque::from([f(7200.0), f(9000.0)])));
        let queue = Rc::clone(&calls);
        let provider: LeaseSecondsProvider =
            Box::new(move || Ok(queue.borrow_mut().pop_front().expect("a lease")));
        let mut b = builder(
            factory_config(),
            scripted(
                vec![
                    refuse("resource_unavailable", ""),
                    accept("r-b", json!(1), 9005.5),
                ],
                vec![],
            ),
            &ids,
            Some(provider),
        );
        let build = start(&b, &request(), &parse(&fan_in()));
        let one = b
            .advance(&build, &nodes(), 100.0, Some(5.5))
            .expect("stage 0");
        assert_eq!(
            one.ordered_hops,
            vec![hop("stage-0", "p0b", "r-b", 9005.5, json!(1))]
        );
        assert_eq!(
            b.capacity().events,
            vec![
                reserved("p0a", PyNumber::from(0), 7205.5),
                reserved("p0b", PyNumber::from(0), 9005.5),
            ]
        );
        assert!(calls.borrow().is_empty());
    }
    // A negative max_new_tokens: (4 - 20) * 0 is the int 0 and is reserved; (4 - 20) * 10 is
    // the negative int -160, which the oracle would reserve but the ledger's request cannot
    // carry (declared V03 limit: the gateway admits only 1 <= max_new_tokens,
    // mycelium_request_gateway/contracts.py:221-224, and the parser only kv costs >= 0).
    {
        let ids = Ids::default();
        let mut negative = request();
        negative.max_new_tokens = i(-20);
        let mut b = builder(
            factory_config(),
            scripted(vec![accept("r-a", json!(1), 3605.0)], vec![]),
            &ids,
            None,
        );
        let build = start(&b, &negative, &parse(&fan_in()));
        let one = b.advance(&build, &nodes(), 100.0, Some(5.0)).expect("kv 0");
        assert_eq!(one.ordered_hops.len(), 1);
        let charged = graph_doc(&[
            (json!(10), vec![pl("p0", "node-a")]),
            (json!(10), vec![pl("p1", "node-b")]),
        ]);
        let build = start(&b, &negative, &parse(&charged));
        assert_eq!(
            b.advance(&build, &nodes(), 100.0, Some(5.0))
                .map(|s| s.ordered_hops),
            Err(python("m16_path_builder_negative_kv_bytes"))
        );
        assert_eq!(
            b.capacity().events,
            vec![reserved("p0a", PyNumber::from(0), 3605.0)]
        );
    }
}

#[test]
fn m16_path_builder_lock_and_commit() {
    let manifest = |hops: Vec<PathHop>, loopback: &str| PathManifest {
        path_id: "path-1".to_owned(),
        path_attempt: 0,
        request_id: "request-1".to_owned(),
        deployment_id: json!("deployment-m16"),
        deployment_epoch: json!(1),
        topology_version: json!(1),
        manifest_digest: json!("digest-m16"),
        ordered_hops: hops,
        loopback_edge_id: json!(loopback),
        protocol: PATH_MANIFEST_PROTOCOL.to_owned(),
    };
    let abort = released(&["r-0", "r-1"]);
    let commit = commit_event(&["r-0", "r-1"]);
    let expired = Err(routing("reservation_expired"));
    // :249-255 the lock time is admitted_at when now and lease_now are both None, else
    // lease_now when given, else now; :256-264 an expired lease aborts.
    {
        let ids = Ids::default();
        let mut req = request();
        req.admitted_at = f(40.0);
        let hops = vec![
            hop("stage-0", "p0a", "r-0", 50.0, json!(1)),
            hop("stage-1", "p1", "r-1", 60.0, json!(-1)),
        ];
        let mut b = builder(
            factory_config(),
            scripted(vec![], vec![committed(), committed(), committed()]),
            &ids,
            None,
        );
        let build = PathBuildState {
            ordered_hops: hops.clone(),
            ..start(&b, &req, &parse(&fan_in()))
        };
        let ok = Ok(manifest(hops, "loop-0"));
        assert_eq!(b.lock(&build, Some(55.0), None), expired);
        assert_eq!(b.lock(&build, None, None), ok);
        assert_eq!(b.lock(&build, Some(55.0), Some(45.0)), ok);
        assert_eq!(b.lock(&build, None, Some(50.0)), expired);
        assert_eq!(b.lock(&build, Some(10.0), Some(49.5)), ok);
        assert_eq!(
            b.capacity().events,
            vec![
                abort.clone(),
                commit.clone(),
                commit.clone(),
                abort.clone(),
                commit.clone()
            ]
        );
    }
    // An int admitted_at equal to the expiry: 50.0 <= 50 exactly, expired.
    {
        let ids = Ids::default();
        let mut req = request();
        req.admitted_at = i(50);
        let mut b = builder(factory_config(), Port::default(), &ids, None);
        let build = PathBuildState {
            ordered_hops: vec![
                hop("stage-0", "p0a", "r-0", 50.0, json!(1)),
                hop("stage-1", "p1", "r-1", 60.0, json!(1)),
            ],
            ..start(&b, &req, &parse(&fan_in()))
        };
        assert_eq!(b.lock(&build, None, None), expired);
        assert_eq!(b.capacity().events, vec![abort.clone()]);
    }
    // leases.py:50-56: epoch -1 hops pass, a mismatch is reported before an expiry, True and
    // 1.0 are epoch 1.
    {
        let ids = Ids::default();
        let mut b = builder(
            factory_config(),
            scripted(vec![], vec![committed(), committed()]),
            &ids,
            None,
        );
        let base = start(&b, &request(), &parse(&fan_in()));
        let with = |hops: Vec<PathHop>| PathBuildState {
            ordered_hops: hops,
            ..base.clone()
        };
        let defaults = vec![
            PathHop::new(json!("stage-0"), json!("p0a"), "r-0"),
            PathHop::new(json!("stage-1"), json!("p1"), "r-1"),
        ];
        assert_eq!(
            b.lock(&with(defaults.clone()), Some(1e300), None),
            Ok(manifest(defaults, "loop-0"))
        );
        let mismatched = vec![
            hop("stage-0", "p0a", "r-0", INF, json!(2)),
            hop("stage-1", "p1", "r-1", 0.0, json!(1)),
        ];
        assert_eq!(
            b.lock(&with(mismatched), Some(1.0), None),
            Err(routing("deployment_epoch_mismatch"))
        );
        let equal = vec![
            hop("stage-0", "p0a", "r-0", INF, json!(true)),
            hop("stage-1", "p1", "r-1", INF, json!(1.0)),
        ];
        assert_eq!(
            b.lock(&with(equal.clone()), Some(1.0), None),
            Ok(manifest(equal, "loop-0"))
        );
        assert_eq!(
            b.capacity().events,
            vec![commit.clone(), abort.clone(), commit.clone()]
        );
    }
    // :247-248 incomplete; :265-278 no legal loopback is refused without abort; the loopback is
    // the first legal one in graph order that is not excluded (two loopbacks may share a pair);
    // :279-290 a manifest the graph refuses is a ContractError, without abort.
    {
        let ids = Ids::default();
        let mut b = builder(
            factory_config(),
            scripted(vec![], vec![committed(), committed()]),
            &ids,
            None,
        );
        let half = PathBuildState {
            ordered_hops: vec![hop("stage-0", "p0a", "r-0", INF, json!(-1))],
            ..start(&b, &request(), &parse(&fan_in()))
        };
        assert_eq!(
            b.lock(&half, Some(1.0), None),
            Err(routing("path_incomplete"))
        );
        let unlooped = PathBuildState {
            excluded_edges: set(&[json!("loop-0")]),
            ordered_hops: vec![
                hop("stage-0", "p0", "r-0", INF, json!(-1)),
                hop("stage-1", "p1a", "r-1", INF, json!(-1)),
            ],
            ..start(&b, &request(), &parse(&fan_out()))
        };
        assert_eq!(
            b.lock(&unlooped, Some(1.0), None),
            Err(routing("no_legal_loopback"))
        );
        let shared = with_edges(
            fan_in(),
            "loopback_edges",
            &[
                e("loop-x", "p1", "p0a"),
                e("loop-y", "p1", "p0a"),
                e("loop-z", "p1", "p0b"),
            ],
        );
        let hops = vec![
            hop("stage-0", "p0a", "r-0", INF, json!(-1)),
            hop("stage-1", "p1", "r-1", INF, json!(-1)),
        ];
        let looped = PathBuildState {
            ordered_hops: hops.clone(),
            ..start(&b, &request(), &parse(&shared))
        };
        let path = looped.path_id.clone();
        let named = |m: PathManifest| PathManifest {
            path_id: path.clone(),
            ..m
        };
        assert_eq!(
            b.lock(&looped, Some(1.0), None),
            Ok(named(manifest(hops.clone(), "loop-x")))
        );
        let skip_x = PathBuildState {
            excluded_edges: set(&[json!("loop-x")]),
            ..looped.clone()
        };
        assert_eq!(
            b.lock(&skip_x, Some(1.0), None),
            Ok(named(manifest(hops, "loop-y")))
        );
        let unreserved = PathBuildState {
            ordered_hops: vec![
                hop("stage-0", "p0a", "r-0", INF, json!(-1)),
                hop("stage-1", "p1", "", INF, json!(-1)),
            ],
            ..start(&b, &request(), &parse(&fan_in()))
        };
        assert_eq!(
            b.lock(&unreserved, Some(1.0), None),
            Err(PathBuilderError::Contract("missing_reservation_id"))
        );
        assert_eq!(b.capacity().events, vec![commit.clone(), commit.clone()]);
    }
    // :300-306 a refused commit aborts: path_commit_rejected(reason).
    {
        let ids = Ids::default();
        let mut b = builder(
            factory_config(),
            scripted(vec![], vec![rejected("reservation_not_live")]),
            &ids,
            None,
        );
        let build = PathBuildState {
            ordered_hops: vec![
                hop("stage-0", "p0a", "r-0", INF, json!(-1)),
                hop("stage-1", "p1", "r-1", INF, json!(-1)),
            ],
            ..start(&b, &request(), &parse(&fan_in()))
        };
        assert_eq!(
            b.lock(&build, Some(1.0), None),
            Err(PathBuilderError::Routing {
                code: "path_commit_rejected",
                detail: "reservation_not_live"
            })
        );
        assert_eq!(b.capacity().events, vec![commit.clone(), abort.clone()]);
    }
    // :291-299 a port with synchronize_build: a refusal aborts with
    // path_reservation_sync_rejected(reason); an acceptance goes on to commit.
    {
        let ids = Ids::default();
        let port = Port {
            syncs: Some(VecDeque::from([
                rejected("capacity_graph_mismatch"),
                committed(),
            ])),
            ..scripted(vec![], vec![committed()])
        };
        let mut b = builder(factory_config(), port, &ids, None);
        let hops = vec![
            hop("stage-0", "p0b", "r-0", INF, json!(-1)),
            hop("stage-1", "p1", "r-1", INF, json!(-1)),
        ];
        let build = PathBuildState {
            ordered_hops: hops.clone(),
            ..start(&b, &request(), &parse(&fan_in()))
        };
        assert_eq!(
            b.lock(&build, Some(1.0), None),
            Err(PathBuilderError::Routing {
                code: "path_reservation_sync_rejected",
                detail: "capacity_graph_mismatch"
            })
        );
        assert_eq!(
            b.lock(&build, Some(1.0), None),
            Ok(manifest(hops, "loop-1"))
        );
        let sync = Event::Sync("path-1".to_owned(), 2);
        assert_eq!(b.capacity().events, vec![sync.clone(), abort, sync, commit]);
    }
}

#[test]
fn m16_path_builder_graph_and_manifest_validation() {
    // validate_execution_graph (validation.py:41-178) runs in start (routing.py:162) and in
    // validate_manifest (:184). A Graph is only ever execution_graph::parse's output
    // (execution_graph.rs:204), which enforces every one of its rules (cited per code in
    // m16_path_builder.rs); protocol's oracle vectors (tests/graph_contracts.rs) cover every
    // code but these two (validation.py:140-147), and a loopback reusing an edge id (:159-163).
    let line = graph_doc(&[
        (json!(0), vec![pl("p0", "node-a")]),
        (json!(0), vec![pl("p1", "node-b")]),
        (json!(0), vec![pl("p2", "node-c")]),
    ]);
    let refused = |doc: Value| execution_graph::parse(&doc).map(|_| ());
    for (edges, code) in [
        (
            vec![
                e("edge-0", "p0", "p1"),
                e("edge-1", "p1", "p2"),
                e("edge-2", "p0", "p2"),
            ],
            "non_adjacent_stage_edge",
        ),
        (
            vec![
                e("edge-0", "p0", "p1"),
                e("edge-1", "p1", "p2"),
                e("edge-2", "p1", "p0"),
            ],
            "non_adjacent_stage_edge",
        ),
        (
            vec![
                e("edge-0", "p0", "p1"),
                e("edge-1", "p1", "p2"),
                e("edge-2", "p0", "p1"),
            ],
            "duplicate_edge_pair",
        ),
    ] {
        assert_eq!(
            refused(with_edges(line.clone(), "edges", &edges)),
            Err(ContractError(code))
        );
    }
    assert_eq!(
        refused(with_edges(
            line.clone(),
            "loopback_edges",
            &[e("edge-0", "p2", "p0")]
        )),
        Err(ContractError("duplicate_edge_id"))
    );
    // Two loopbacks on one pair are no rule.
    assert_eq!(
        refused(with_edges(
            line.clone(),
            "loopback_edges",
            &[e("loop-x", "p2", "p0"), e("loop-y", "p2", "p0")]
        )),
        Ok(())
    );

    // validation.py:181-235 over fan_in (edges p0a->p1, p0b->p1; loop-0 p1->p0a, loop-1 p1->p0b).
    let graph = parse(&fan_in());
    let hops = vec![
        PathHop::new(json!("stage-0"), json!("p0a"), "r-0"),
        PathHop::new(json!("stage-1"), json!("p1"), "r-1"),
    ];
    let ok = PathManifest {
        path_id: "path-1".to_owned(),
        path_attempt: 0,
        request_id: "request-1".to_owned(),
        deployment_id: json!("deployment-m16"),
        deployment_epoch: json!(1),
        topology_version: json!(1),
        manifest_digest: json!("digest-m16"),
        ordered_hops: hops.clone(),
        loopback_edge_id: json!("loop-0"),
        protocol: PATH_MANIFEST_PROTOCOL.to_owned(),
    };
    let with_hops = |list: Vec<PathHop>| PathManifest {
        ordered_hops: list,
        ..ok.clone()
    };
    let contract = |code| Err(PathBuilderError::Contract(code));
    let cases: Vec<(PathManifest, Result<(), PathBuilderError>)> = vec![
        (ok.clone(), Ok(())),
        (
            PathManifest {
                protocol: "mycelium.path_manifest.v2".to_owned(),
                ..ok.clone()
            },
            contract("unsupported_manifest_protocol"),
        ),
        (
            PathManifest {
                deployment_id: json!("other"),
                ..ok.clone()
            },
            contract("deployment_id_mismatch"),
        ),
        (
            PathManifest {
                deployment_epoch: json!(2),
                ..ok.clone()
            },
            contract("deployment_epoch_mismatch"),
        ),
        // Python ==: 1.0 == 1 and True == 1.
        (
            PathManifest {
                deployment_epoch: json!(1.0),
                topology_version: json!(true),
                ..ok.clone()
            },
            Ok(()),
        ),
        (
            PathManifest {
                topology_version: json!(2),
                ..ok.clone()
            },
            contract("topology_version_mismatch"),
        ),
        (
            PathManifest {
                manifest_digest: json!("digest-other"),
                ..ok.clone()
            },
            contract("manifest_digest_mismatch"),
        ),
        (
            with_hops(hops[..1].to_vec()),
            contract("manifest_stage_count_mismatch"),
        ),
        (
            with_hops(vec![
                PathHop::new(json!("stage-1"), json!("p0a"), "r-0"),
                hops[1].clone(),
            ]),
            contract("manifest_stage_order_mismatch"),
        ),
        (
            with_hops(vec![
                PathHop::new(json!("stage-0"), json!("nope"), "r-0"),
                hops[1].clone(),
            ]),
            contract("unknown_manifest_placement"),
        ),
        (
            with_hops(vec![
                PathHop::new(json!("stage-0"), json!("p1"), "r-0"),
                hops[1].clone(),
            ]),
            contract("placement_stage_mismatch"),
        ),
        (
            with_hops(vec![
                hops[0].clone(),
                PathHop::new(json!("stage-1"), json!("p1"), ""),
            ]),
            contract("missing_reservation_id"),
        ),
        (
            PathManifest {
                loopback_edge_id: json!("loop-1"),
                ..ok.clone()
            },
            contract("illegal_manifest_loopback"),
        ),
        (
            PathManifest {
                loopback_edge_id: json!("edge-0"),
                ..ok.clone()
            },
            contract("illegal_manifest_loopback"),
        ),
        // :211 and :232 hash an unhashable id: TypeError.
        (
            with_hops(vec![
                PathHop::new(json!("stage-0"), json!(["p0a"]), "r-0"),
                hops[1].clone(),
            ]),
            Err(python("m16_path_builder_unhashable_manifest_placement_id")),
        ),
        (
            PathManifest {
                loopback_edge_id: json!(["loop-0"]),
                ..ok.clone()
            },
            Err(python("m16_path_builder_unhashable_loopback_edge_id")),
        ),
    ];
    for (manifest, expected) in cases {
        assert_eq!(
            validate_manifest(&manifest, &graph),
            expected,
            "{manifest:?}"
        );
    }
    // :217-222 consecutive hops need a graph edge.
    let one_edge = with_edges(fan_in(), "edges", &[e("edge-0", "p0b", "p1")]);
    assert_eq!(
        validate_manifest(&ok, &parse(&one_edge)),
        contract("illegal_manifest_edge")
    );

    // leases.py:44-56: an epoch outside {-1, deployment_epoch} first, then an expiry at or
    // before now; NaN compares false; an unhashable epoch is a TypeError.
    let h = |expires: f64, epoch: Value| hop("s", "p", "r", expires, epoch);
    let lease = |code| Err(PathBuilderError::Lease(code));
    let cases: Vec<(Vec<PathHop>, PyReal, Result<(), PathBuilderError>)> = vec![
        (
            vec![h(5.0, json!(1)), h(4.0, json!(3))],
            f(4.0),
            lease("deployment_epoch_mismatch"),
        ),
        (
            vec![h(5.0, json!(-1)), h(4.0, json!(true))],
            f(4.0),
            lease("reservation_expired"),
        ),
        (vec![h(4.5, json!(1.0))], i(4), Ok(())),
        (vec![h(4.0, json!(1))], i(4), lease("reservation_expired")),
        (vec![h(NAN, json!(1))], f(4.0), Ok(())),
        (vec![h(5.0, json!(1))], f(NAN), Ok(())),
        (vec![], f(INF), Ok(())),
        (vec![h(INF, json!(1))], f(INF), lease("reservation_expired")),
        (
            vec![h(5.0, json!([1]))],
            f(4.0),
            Err(python("m16_path_builder_unhashable_reservation_epoch")),
        ),
        // A mismatch before the unhashable hop is reported first (any() stops there).
        (
            vec![h(5.0, json!(7)), h(5.0, json!([1]))],
            f(4.0),
            lease("deployment_epoch_mismatch"),
        ),
    ];
    for (list, now, expected) in cases {
        assert_eq!(
            validate_hop_leases(&list, &json!(1), &now),
            expected,
            "{list:?} at {now:?}"
        );
    }
}

/// The admit loop (mycelium_m16_runtime.py:424-441): start at attempt 0, advance until
/// complete, lock; the build reached is returned with the outcome.
fn admit<P: CapacityPort, I: IdSource>(
    b: &mut ProgressivePathBuilder<P, I>,
    graph: &Graph,
    now: f64,
    lease_now: f64,
) -> (Result<PathManifest, PathBuilderError>, PathBuildState) {
    let mut build = start(b, &request(), graph);
    while !b.is_complete(&build) {
        match b.advance(&build, &nodes(), now, Some(lease_now)) {
            Ok(next) => build = next,
            Err(error) => return (Err(error), build),
        }
    }
    (b.lock(&build, Some(now), Some(lease_now)), build)
}

fn capacity(id: &str, memory: u64, kv: u64, workspace: u64) -> PlacementCapacity {
    PlacementCapacity {
        placement_id: json!(id),
        memory_capacity_bytes: PyNumber::from(memory),
        kv_capacity_bytes: PyNumber::from(kv),
        workspace_capacity_bytes: PyNumber::from(workspace),
        maximum_reservations: 64,
    }
}

type LedgerBuilder<'a> = ProgressivePathBuilder<ResourceLedger<&'a Clock, &'a Ids>, &'a Ids>;

/// mycelium_m16_runtime.py:383-393 with the factory's RouterConfig (:1260) and no provider.
fn compose<'a>(
    graph: &Graph,
    caps: &[PlacementCapacity],
    clock: &'a Clock,
    ids: &'a Ids,
) -> LedgerBuilder<'a> {
    let ledger = ResourceLedger::new(graph, caps, clock, ids).expect("a ledger");
    ProgressivePathBuilder::new(
        RoutePolicy::new(RouteScorer::new(factory_config())),
        ledger,
        ids,
        None,
    )
}

/// Per placement: reserved KV and memory bytes and active reservations (:308-338).
fn ledger_status(b: &LedgerBuilder) -> Vec<(Value, PyNumber, PyNumber, u64)> {
    b.capacity()
        .placement_status()
        .expect("placement status")
        .into_iter()
        .map(|s| {
            (
                s.placement_id,
                s.reserved_kv_bytes,
                s.reserved_memory_bytes,
                s.active_reservations,
            )
        })
        .collect()
}

#[test]
fn m16_path_builder_ledger_admission_flow() {
    // The M16 composition (:383-393): the real ledger as the capacity, on the lease clock, one
    // id source for ledger and builder, RoutePolicy(RouteScorer(RouterConfig(lease 3600.0)))
    // and no lease provider. stage-0 [p0 on node-a, kv 0], stage-1 [p1a on node-b, p1b on
    // node-c, kv 10 per token]: stage 1 needs (4 + 8) * 10 = 120 KV bytes, every reservation
    // activation_bytes * 2 = 4 workspace bytes (:227-228).
    let doc = graph_doc(&[
        (json!(0), vec![pl("p0", "node-a")]),
        (json!(10), vec![pl("p1a", "node-b"), pl("p1b", "node-c")]),
    ]);
    let graph = parse(&doc);
    let plenty = 1_000_000_000_000;
    let idle = |id: &str| (json!(id), PyNumber::from(0), PyNumber::from(0), 0);
    // The routes (p0, p1a) and (p0, p1b) tie, so p1a is tried first; its 119 KV bytes refuse
    // 120 (resource_unavailable, :229-238), it is excluded and p1b is reserved. Committed
    // reservations stay active in placement_status (only release frees them, :340-345).
    {
        let (clock, ids) = (Clock::default(), Ids::default());
        clock.0.set(5.0);
        let mut b = compose(
            &graph,
            &[
                capacity("p0", plenty, plenty, plenty),
                capacity("p1a", plenty, 119, plenty),
                capacity("p1b", plenty, plenty, plenty),
            ],
            &clock,
            &ids,
        );
        let (manifest, build) = admit(&mut b, &graph, 100.0, 5.0);
        let hops = vec![
            hop("stage-0", "p0", "reservation-2", 3605.0, json!(1)),
            hop("stage-1", "p1b", "reservation-3", 3605.0, json!(1)),
        ];
        assert_eq!(
            manifest,
            Ok(PathManifest {
                path_id: "path-1".to_owned(),
                path_attempt: 0,
                request_id: "request-1".to_owned(),
                deployment_id: json!("deployment-m16"),
                deployment_epoch: json!(1),
                topology_version: json!(1),
                manifest_digest: json!("digest-m16"),
                ordered_hops: hops,
                loopback_edge_id: json!("loop-1"),
                protocol: PATH_MANIFEST_PROTOCOL.to_owned(),
            })
        );
        assert_eq!(build.excluded_placements, set(&[json!("p1a")]));
        assert_eq!(
            ledger_status(&b),
            vec![
                (json!("p0"), PyNumber::from(0), PyNumber::from(4), 1),
                idle("p1a"),
                (json!("p1b"), PyNumber::from(120), PyNumber::from(124), 1),
            ]
        );
        let held: Vec<(String, bool, u64)> = b
            .capacity()
            .request_reservations("request-1")
            .into_iter()
            .map(|r| (r.reservation_id, r.committed, r.expires_at.to_bits()))
            .collect();
        assert_eq!(
            held,
            vec![
                ("reservation-2".to_owned(), true, 3605.0f64.to_bits()),
                ("reservation-3".to_owned(), true, 3605.0f64.to_bits()),
            ]
        );
    }
    // p1b's 3 workspace bytes refuse 4 too: no_feasible_route, the routing refusal admit
    // turns into resource_unavailable (:442-449), and the stage-0 hop is released.
    {
        let (clock, ids) = (Clock::default(), Ids::default());
        clock.0.set(5.0);
        let mut b = compose(
            &graph,
            &[
                capacity("p0", plenty, plenty, plenty),
                capacity("p1a", plenty, 119, plenty),
                capacity("p1b", plenty, plenty, 3),
            ],
            &clock,
            &ids,
        );
        let (manifest, build) = admit(&mut b, &graph, 100.0, 5.0);
        assert_eq!(manifest, Err(routing("no_feasible_route")));
        assert_eq!(manifest.map(|_| ()).map_err(|e| e.kind()), Err("routing"));
        assert_eq!(
            build.ordered_hops,
            vec![hop("stage-0", "p0", "reservation-2", 3605.0, json!(1))]
        );
        assert_eq!(
            ledger_status(&b),
            vec![idle("p0"), idle("p1a"), idle("p1b")]
        );
        assert!(b.capacity().request_reservations("request-1").is_empty());
    }
    // The lease clock past the leases at commit: the ledger refuses (reservation_not_live,
    // :271-278), lock aborts and every hop is released.
    {
        let (clock, ids) = (Clock::default(), Ids::default());
        clock.0.set(4000.0);
        let mut b = compose(
            &graph,
            &[
                capacity("p0", plenty, plenty, plenty),
                capacity("p1a", plenty, plenty, plenty),
                capacity("p1b", plenty, plenty, plenty),
            ],
            &clock,
            &ids,
        );
        let (manifest, _) = admit(&mut b, &graph, 100.0, 5.0);
        assert_eq!(
            manifest,
            Err(PathBuilderError::Routing {
                code: "path_commit_rejected",
                detail: "reservation_not_live"
            })
        );
        assert_eq!(
            ledger_status(&b),
            vec![idle("p0"), idle("p1a"), idle("p1b")]
        );
    }
}

// Paired oracle probes. Every case below was executed through the real v0.1.4 code path at
// f4e366e9 (execution_graph_from_dict, ProgressivePathBuilder(policy=RoutePolicy(RouteScorer(
// config)), capacity, id_source) with the real _ResourceLedgerCapacityPort or a scripted port,
// validate_hop_leases and validate_manifest) by the probe named in the fixture's provenance
// (CPython 3.14.2). The fixture keeps each number's Python type: {"int": str(n)}, {"bool": b} or
// {"float": repr(x), "bits"}.
const ORACLE: &str = include_str!("fixtures/m16-path-builder-oracle.json");

/// A recorded number as the value the oracle held.
fn recorded(tagged: &Value) -> PyReal {
    if let Some(flag) = tagged["bool"].as_bool() {
        return PyReal::Bool(flag);
    }
    if let Some(text) = tagged["int"].as_str() {
        let (negative, magnitude) = text
            .strip_prefix('-')
            .map_or((false, text), |rest| (true, rest));
        return PyReal::signed(negative, magnitude.parse().expect("int digits"));
    }
    let bits = tagged["bits"].as_str().expect("a number tag");
    PyReal::Float(f64::from_bits(
        u64::from_str_radix(bits, 16).expect("hex bits"),
    ))
}

fn recorded_f64(tagged: &Value) -> f64 {
    match recorded(tagged) {
        PyReal::Float(x) => x,
        other => panic!("not a float: {other:?}"),
    }
}

fn optional_f64(tagged: &Value) -> Option<f64> {
    (!tagged.is_null()).then(|| recorded_f64(tagged))
}

/// A test-owned spelling of a Python number: its type and exact value (float bits).
fn tag(v: &PyReal) -> Value {
    json!(match v {
        PyReal::Bool(b) => format!("bool:{b}"),
        PyReal::Int {
            negative,
            magnitude,
        } => {
            let digits = magnitude.to_string();
            let sign = if *negative && digits != "0" { "-" } else { "" };
            format!("int:{sign}{digits}")
        }
        PyReal::Float(x) => format!("float:{:016x}", x.to_bits()),
    })
}

fn tag_f64(x: f64) -> Value {
    tag(&PyReal::Float(x))
}

fn tag_number(n: &PyNumber) -> Value {
    match n {
        PyNumber::Int(magnitude) => tag(&PyReal::signed(false, magnitude.clone())),
        PyNumber::Float(x) => tag_f64(*x),
    }
}

/// The fixture's document with every number tag in [`tag`]'s spelling.
fn normalized(v: &Value) -> Value {
    match v {
        Value::Object(map)
            if map.contains_key("bits") || map.contains_key("int") || map.contains_key("bool") =>
        {
            tag(&recorded(v))
        }
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, item)| (key.clone(), normalized(item)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(normalized).collect()),
        other => other.clone(),
    }
}

fn hop_doc(h: &PathHop) -> Value {
    json!({
        "stage_id": h.stage_id, "placement_id": h.placement_id,
        "reservation_id": h.reservation_id,
        "reservation_expires_at": tag_f64(h.reservation_expires_at),
        "reservation_epoch": h.reservation_epoch,
    })
}

fn recorded_hop(d: &Value) -> PathHop {
    PathHop {
        stage_id: d["stage_id"].clone(),
        placement_id: d["placement_id"].clone(),
        reservation_id: d["reservation_id"]
            .as_str()
            .expect("reservation id")
            .to_owned(),
        reservation_expires_at: recorded_f64(&d["reservation_expires_at"]),
        reservation_epoch: d["reservation_epoch"].clone(),
    }
}

fn recorded_hops(list: &Value) -> Vec<PathHop> {
    list.as_array()
        .expect("hops")
        .iter()
        .map(recorded_hop)
        .collect()
}

/// A frozenset as the probe writes it: sorted by its JSON text.
fn set_doc(set: &IdSet) -> Value {
    let mut ids: Vec<Value> = set.ids().cloned().collect();
    ids.sort_by_key(|id| id.to_string());
    Value::Array(ids)
}

fn build_doc(b: &PathBuildState) -> Value {
    json!({
        "path_id": b.path_id, "path_attempt": b.path_attempt,
        "hops": b.ordered_hops.iter().map(hop_doc).collect::<Vec<_>>(),
        "excluded_placements": set_doc(&b.excluded_placements),
        "excluded_edges": set_doc(&b.excluded_edges),
        "excluded_devices": set_doc(&b.excluded_devices),
    })
}

fn manifest_doc(m: &PathManifest) -> Value {
    json!({
        "path_id": m.path_id, "path_attempt": m.path_attempt, "request_id": m.request_id,
        "deployment_id": m.deployment_id, "deployment_epoch": m.deployment_epoch,
        "topology_version": m.topology_version, "manifest_digest": m.manifest_digest,
        "ordered_hops": m.ordered_hops.iter().map(hop_doc).collect::<Vec<_>>(),
        "loopback_edge_id": m.loopback_edge_id, "protocol": m.protocol,
    })
}

fn recorded_manifest(d: &Value) -> PathManifest {
    PathManifest {
        path_id: d["path_id"].as_str().expect("path id").to_owned(),
        path_attempt: d["path_attempt"].as_u64().expect("attempt"),
        request_id: d["request_id"].as_str().expect("request id").to_owned(),
        deployment_id: d["deployment_id"].clone(),
        deployment_epoch: d["deployment_epoch"].clone(),
        topology_version: d["topology_version"].clone(),
        manifest_digest: d["manifest_digest"].clone(),
        ordered_hops: recorded_hops(&d["ordered_hops"]),
        loopback_edge_id: d["loopback_edge_id"].clone(),
        protocol: d["protocol"].as_str().expect("protocol").to_owned(),
    }
}

fn event_doc(event: &Event) -> Value {
    match event {
        Event::Reserve(r) => json!(["reserve", {
            "request_id": r.request_id, "path_id": r.path_id, "path_attempt": r.path_attempt,
            "placement_id": r.placement_id, "kv_bytes": tag_number(&r.kv_bytes),
            "deployment_epoch": r.deployment_epoch,
            "lease_expires_at": tag_f64(r.lease_expires_at),
        }]),
        Event::Release(ids) => json!(["release", ids]),
        Event::Commit(ids, epoch) => json!(["commit", ids, epoch]),
        Event::Sync(path, hops) => json!(["sync", path, hops]),
    }
}

/// A scripted reason as the `&'static str` the ledger's records carry.
fn reason(v: &Value) -> &'static str {
    Box::leak(v.as_str().expect("reason").to_owned().into_boxed_str())
}

fn recorded_reservation(d: &Value) -> ReservationResult {
    ReservationResult {
        accepted: d["accepted"].as_bool().expect("accepted"),
        reservation_id: d["reservation_id"].as_str().expect("id").to_owned(),
        reason: reason(&d["reason"]),
        deployment_epoch: d["deployment_epoch"].clone(),
        expires_at: recorded_f64(&d["expires_at"]),
    }
}

fn recorded_commit(d: &Value) -> ReservationCommitResult {
    ReservationCommitResult {
        accepted: d["accepted"].as_bool().expect("accepted"),
        reason: reason(&d["reason"]),
    }
}

fn recorded_list<T>(v: &Value, each: fn(&Value) -> T) -> VecDeque<T> {
    v.as_array().into_iter().flatten().map(each).collect()
}

fn recorded_ids(v: &Value) -> IdSet {
    IdSet::of(v.as_array().into_iter().flatten().cloned()).expect("hashable ids")
}

fn recorded_states(list: &Value) -> IdMap<DeviceState> {
    let pairs = |v: &Value| {
        let mut map = IdMap::new();
        for pair in v.as_array().expect("pairs") {
            map.insert(pair[0].clone(), recorded(&pair[1]))
                .expect("hashable key");
        }
        map
    };
    let mut map = IdMap::new();
    for entry in list.as_array().expect("states") {
        let (key, doc) = (&entry[0], &entry[1]);
        let s = DeviceState {
            node_id: key.clone(),
            state_seq: i(1),
            last_updated: recorded(&doc["last_updated"]),
            availability: doc["availability"]
                .as_str()
                .expect("availability")
                .to_owned(),
            compute_units_per_second: recorded(&doc["compute_units_per_second"]),
            free_compute_fraction: recorded(&doc["free_compute_fraction"]),
            available_kv_bytes: i(0),
            pending_hop_queue_depth: recorded(&doc["pending_hop_queue_depth"]),
            neighbor_rtt_ms: pairs(&doc["neighbor_rtt_ms"]),
            neighbor_bandwidth_bytes_per_second: pairs(&doc["neighbor_bandwidth_bytes_per_second"]),
        };
        map.insert(key.clone(), s).expect("hashable node id");
    }
    map
}

/// The port's (kind, code, detail) for the exception the oracle raised, by type and line.
fn expected_error(error: &Value) -> (&'static str, String, String) {
    let line = error["line"].as_str().expect("line");
    let code = || error["code"].as_str().expect("code").to_owned();
    let detail = error["detail"].as_str().unwrap_or("").to_owned();
    let python = |code: &str| ("python", code.to_owned(), String::new());
    match (error["exception"].as_str().expect("exception"), line) {
        ("RoutingError", _) => ("routing", code(), detail),
        // The port carries a ContractError's code, not its detail text.
        ("ContractError", _) => ("contract", code(), String::new()),
        ("ReservationLeaseError", _) => ("lease", code(), String::new()),
        ("ZeroDivisionError", "mycelium_router/scoring.py:157") => (
            "scorer",
            "m16_route_scorer_zero_division".to_owned(),
            String::new(),
        ),
        ("TypeError", "mycelium_router/routing.py:50") => {
            python("m16_path_builder_unorderable_placement_ids")
        }
        ("TypeError", "mycelium_router/routing.py:83") => {
            python("m16_path_builder_unhashable_node_id")
        }
        ("TypeError", "mycelium_router/routing.py:105") => {
            python("m16_path_builder_unorderable_edge_ids")
        }
        ("OverflowError", "mycelium_router/routing.py:140" | "mycelium_router/routing.py:197") => {
            python(INT_TOO_LARGE)
        }
        ("TypeError", "mycelium_router/leases.py:51") => {
            python("m16_path_builder_unhashable_reservation_epoch")
        }
        ("TypeError", "mycelium_router/validation.py:211") => {
            python("m16_path_builder_unhashable_manifest_placement_id")
        }
        ("TypeError", "mycelium_router/validation.py:232") => {
            python("m16_path_builder_unhashable_loopback_edge_id")
        }
        other => panic!("unmapped oracle exception {other:?}"),
    }
}

fn actual_error(error: &PathBuilderError) -> (&'static str, String, String) {
    let detail = match error {
        PathBuilderError::Routing { detail, .. } => (*detail).to_owned(),
        _ => String::new(),
    };
    (error.kind(), error.code().to_owned(), detail)
}

type Outcome = Result<Value, (&'static str, String, String)>;

fn expected_outcome(result: &Value) -> Outcome {
    match result.get("error") {
        Some(error) => Err(expected_error(error)),
        None => Ok(normalized(&result["ok"])),
    }
}

fn status_doc(ledger: &ResourceLedger<&Clock, &Ids>) -> Value {
    Value::Array(
        ledger
            .placement_status()
            .expect("placement status")
            .iter()
            .map(|s| {
                json!({
                    "placement_id": s.placement_id,
                    "reserved_kv_bytes": tag_number(&s.reserved_kv_bytes),
                    "reserved_memory_bytes": tag_number(&s.reserved_memory_bytes),
                    "active_reservations": s.active_reservations,
                })
            })
            .collect(),
    )
}

fn set_field(config: &mut RouterConfig, name: &str, value: PyReal) {
    match name {
        "reservation_lease_seconds" => config.reservation_lease_seconds = value,
        other => panic!("unmapped RouterConfig field {other}"),
    }
}

fn started(build: &Option<PathBuildState>) -> PathBuildState {
    build.clone().expect("a started build")
}

/// Replays one recorded case op by op; returns the number of ops compared.
fn replay(case: &Value) -> usize {
    let name = case["name"].as_str().expect("case name");
    let document: Value =
        serde_json::from_str(case["graph_json"].as_str().expect("graph text")).expect("graph");
    let graph = execution_graph::parse(&document).expect("the oracle parsed this graph");
    let mut config = RouterConfig::oracle_default().expect("oracle defaults");
    for (field, value) in case["config"].as_object().expect("config") {
        set_field(&mut config, field, recorded(value));
    }
    let r = &case["request"];
    let req = RequestContext {
        prompt_token_ids: (0..r["prompt_len"].as_i64().expect("prompt length"))
            .map(i)
            .collect(),
        max_new_tokens: recorded(&r["max_new_tokens"]),
        expected_new_tokens: recorded(&r["max_new_tokens"]),
        qos_class: r["qos_class"].as_str().expect("qos").to_owned(),
        admitted_at: recorded(&r["admitted_at"]),
        target_ttft_ms: recorded(&r["target_ttft_ms"]),
        target_tpot_ms: recorded(&r["target_tpot_ms"]),
        ..request()
    };
    let device_states = recorded_states(&case["states"]);
    let clock = Clock::default();
    let ids = Ids::default();
    let spec = &case["capacity"];
    let port = if spec["kind"] == "ledger" {
        let caps: Vec<PlacementCapacity> = spec["capacities"]
            .as_array()
            .expect("capacities")
            .iter()
            .map(|entry| {
                let n = |k: usize| PyNumber::from(entry[1][k].as_u64().expect("capacity"));
                PlacementCapacity {
                    placement_id: entry[0].clone(),
                    memory_capacity_bytes: n(0),
                    kv_capacity_bytes: n(1),
                    workspace_capacity_bytes: n(2),
                    maximum_reservations: entry[1][3].as_u64().expect("maximum"),
                }
            })
            .collect();
        Port {
            ledger: Some(ResourceLedger::new(&graph, &caps, &clock, &ids).expect("a ledger")),
            ..Port::default()
        }
    } else {
        Port {
            reserves: recorded_list(&spec["reserve"], recorded_reservation),
            commits: recorded_list(&spec["commit"], recorded_commit),
            syncs: spec
                .get("sync")
                .map(|syncs| recorded_list(syncs, recorded_commit)),
            ..Port::default()
        }
    };
    let provider = case["lease_provider"].as_array().map(|calls| {
        let queue = RefCell::new(calls.iter().cloned().collect::<VecDeque<Value>>());
        Box::new(move || {
            let item = queue.borrow_mut().pop_front().expect("a provider value");
            if item == "raise" {
                Err(ProviderRaised)
            } else {
                Ok(recorded(&item))
            }
        }) as LeaseSecondsProvider
    });
    let mut b = builder(config, port, &ids, provider);
    let mut build: Option<PathBuildState> = None;
    let ops = case["ops"].as_array().expect("ops");
    let results = case["results"].as_array().expect("results");
    assert_eq!(ops.len(), results.len(), "{name}");
    for (k, (op, result)) in ops.iter().zip(results).enumerate() {
        if let Some(at) = op.get("ledger_clock") {
            clock.0.set(recorded_f64(at));
        }
        let before = b.capacity().events.len();
        let now = || recorded_f64(&op["now"]);
        let lease_now = || optional_f64(op.get("lease_now").unwrap_or(&NULL));
        let lock_now = || optional_f64(op.get("now").unwrap_or(&NULL));
        let op_name = op["op"].as_str().expect("op");
        let got: Result<Value, PathBuilderError> = match op_name {
            "start" => {
                let started = b.start(
                    &req,
                    &graph,
                    op.get("path_attempt").and_then(Value::as_u64).unwrap_or(0),
                    recorded_ids(&op["excluded_placements"]),
                    recorded_ids(&op["excluded_edges"]),
                    recorded_ids(&op["excluded_devices"]),
                );
                build = Some(started.clone());
                Ok(build_doc(&started))
            }
            "set_hops" => {
                let next = PathBuildState {
                    ordered_hops: recorded_hops(&op["hops"]),
                    ..started(&build)
                };
                build = Some(next.clone());
                Ok(build_doc(&next))
            }
            "with_excluded_devices" => {
                let next = started(&build).with_excluded_devices(recorded_ids(&op["devices"]));
                build = Some(next.clone());
                Ok(build_doc(&next))
            }
            "is_complete" => Ok(json!(b.is_complete(&started(&build)))),
            "complete_routes" => {
                let state = started(&build);
                let prefix: Vec<Value> = state
                    .ordered_hops
                    .iter()
                    .map(|h| h.placement_id.clone())
                    .collect();
                b.policy()
                    .complete_routes(&state, &prefix)
                    .map(|routes| json!(routes))
            }
            "choose_next" => b
                .policy()
                .choose_next(&started(&build), &device_states, &recorded(&op["now"]))
                .map(|d| {
                    let s = &d.score;
                    json!({
                        "placement_id": d.placement_id, "complete_route": d.complete_route,
                        "score": {
                            "ttft_ms": tag_f64(s.ttft_ms), "tpot_ms": tag_f64(s.tpot_ms),
                            "prefill_transfer_ms": tag_f64(s.prefill_transfer_ms),
                            "decode_transfer_ms": tag_f64(s.decode_transfer_ms),
                            "total_score": tag_f64(s.total_score),
                            "confidence": tag_f64(s.confidence),
                            "fallback_nodes": s.fallback_nodes,
                        },
                    })
                }),
            "lease_seconds" => b.reservation_lease_seconds().map(tag_f64),
            "advance" => b
                .advance(&started(&build), &device_states, now(), lease_now())
                .map(|next| {
                    build = Some(next.clone());
                    build_doc(&next)
                }),
            "lock" => b
                .lock(&started(&build), lock_now(), lease_now())
                .map(|m| manifest_doc(&m)),
            "admit_loop" => {
                let mut state = started(&build);
                let mut outcome = None;
                while !b.is_complete(&state) {
                    match b.advance(&state, &device_states, now(), lease_now()) {
                        Ok(next) => state = next,
                        Err(error) => {
                            outcome = Some(Err(error));
                            break;
                        }
                    }
                }
                let outcome = outcome.unwrap_or_else(|| {
                    b.lock(&state, Some(now()), lease_now())
                        .map(|m| manifest_doc(&m))
                });
                build = Some(state);
                outcome
            }
            "validate_hop_leases" => validate_hop_leases(
                &recorded_hops(&op["hops"]),
                &op["deployment_epoch"],
                &recorded(&op["now"]),
            )
            .map(|()| Value::Null),
            "validate_manifest" => {
                validate_manifest(&recorded_manifest(&op["manifest"]), &graph).map(|()| Value::Null)
            }
            "placement_status" => Ok(status_doc(
                b.capacity().ledger.as_ref().expect("a ledger case"),
            )),
            other => panic!("{name}: unknown op {other}"),
        };
        let got = got.map_err(|error| actual_error(&error));
        assert_eq!(got, expected_outcome(result), "{name} op {k} ({op_name})");
        let events: Vec<Value> = b.capacity().events[before..]
            .iter()
            .map(event_doc)
            .collect();
        assert_eq!(
            Value::Array(events),
            normalized(&result["events"]),
            "{name} op {k} ({op_name}): capacity calls"
        );
        if let Some(after) = result.get("build") {
            assert_eq!(
                build_doc(build.as_ref().expect("a build")),
                normalized(after),
                "{name} op {k} ({op_name}): build"
            );
        }
    }
    ops.len()
}

#[test]
fn m16_path_builder_python_value_domain_parity() {
    let fixture: Value = serde_json::from_str(ORACLE).expect("fixture JSON");
    let provenance = &fixture["provenance"];
    assert_eq!(
        provenance["oracle_commit"],
        json!("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert_eq!(provenance["interpreter"], json!("CPython 3.14.2"));
    assert_eq!(
        provenance["probe"],
        json!(
            "/Users/joinmac/Projects/mycelium-core-program/status/moonweaver/workers/l1b-m16-path-builder/evidence/oracle_path_builder_probe.py"
        )
    );
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 20, "{} paired probes", cases.len());
    let ops: usize = cases.iter().map(replay).sum();
    // Both outcomes are exercised across the probes.
    let (mut ok, mut raised) = (0, 0);
    for case in cases {
        for result in case["results"].as_array().expect("results") {
            if result.get("error").is_some() {
                raised += 1;
            } else {
                ok += 1;
            }
        }
    }
    assert_eq!(ok + raised, ops);
    assert!(ok >= 60 && raised >= 30, "{ok} ok, {raised} raised");
}
