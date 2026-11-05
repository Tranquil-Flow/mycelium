// SPDX-License-Identifier: AGPL-3.0-only
//! M16 coordinator status projection (CL-L1-07): source parity for `status`,
//! `_request_projection`, `attach_performance_budget` and `validate_m16_runtime_status` with
//! `_reject_private` / `_finite` (mycelium_m16_runtime.py:37-72, 601-652, 988-1018, 1043-1160
//! @ f4e366e9) and `validate_performance_budget_v3` (mycelium_performance_budget.py:14-53,
//! 180-239), over the real merged coordinator core, ledger, route scorer, path builder and hop
//! scheduler and a graph parsed by `execution_graph::parse`. The first four tests' expected
//! values are derived by hand from the cited oracle lines (the graph digest and the queue-wait
//! bits are the oracle's recorded outputs, cited by fixture case; each path digest is the
//! sha256 of a hand-written canonical text, the helper checked against the recorded r1
//! digest); none is computed by the status code. The clock is a scripted clock returning base,
//! base + step, ... and logging every read; the id source counts `prefix-n` across prefixes.
//! The last test replays paired probes recorded from the real oracle
//! (fixtures/m16-coordinator-status-oracle.json).
use protocol::execution_graph::{self, Graph};
use runtime::m16_capacity::{PlacementCapacity, PyNumber};
use runtime::m16_coordinator::{
    BudgetError, CoordinatorError, CoordinatorInputs, M16RuntimeCoordinator,
    STATUS_INT_TOO_LARGE_FOR_FLOAT, StatusError, StatusValidationError, canonical_json,
    validate_m16_runtime_status, validate_performance_budget_v3,
};
use runtime::m16_ledger::{IdSource, LeaseClock, LedgerError};
use runtime::m16_router_contracts::{
    DeviceState, IdMap, PyJson, PyReal, PyStr, RequestContext, RouterConfig,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

// ---------------------------------------------------------------------------------------------
// The coordinator rig (as in tests/m16_coordinator.rs).

type Reads = Arc<Mutex<Vec<(&'static str, u64)>>>;

/// `now()` returns base, base + step, ... (one float addition per read) and logs each read.
struct ScriptedClock {
    name: &'static str,
    next: Mutex<f64>,
    step: f64,
    reads: Reads,
}

impl LeaseClock for ScriptedClock {
    fn now(&self) -> f64 {
        let mut next = self.next.lock().expect("clock");
        let value = *next;
        *next = value + self.step;
        self.reads
            .lock()
            .expect("reads")
            .push((self.name, value.to_bits()));
        value
    }
}

impl ScriptedClock {
    fn set(&self, value: f64) {
        *self.next.lock().expect("clock") = value;
    }
}

/// `new(prefix)` gives `prefix-1`, `prefix-2`, ... across prefixes and logs each id.
struct ScriptedIds {
    count: Mutex<u64>,
    drawn: Arc<Mutex<Vec<String>>>,
}

impl IdSource for ScriptedIds {
    fn new_id(&self, prefix: &str) -> String {
        let mut count = self.count.lock().expect("ids");
        *count += 1;
        let id = format!("{prefix}-{count}");
        self.drawn.lock().expect("drawn").push(id.clone());
        id
    }
}

struct Rig {
    reads: Reads,
    drawn: Arc<Mutex<Vec<String>>>,
    clock: Arc<ScriptedClock>,
    lease: Option<Arc<ScriptedClock>>,
    ids: Arc<ScriptedIds>,
}

impl Rig {
    fn new(base: f64, step: f64, lease: Option<(f64, f64)>) -> Self {
        let reads: Reads = Arc::default();
        let drawn: Arc<Mutex<Vec<String>>> = Arc::default();
        let clock = |name, base, step| {
            Arc::new(ScriptedClock {
                name,
                next: Mutex::new(base),
                step,
                reads: Arc::clone(&reads),
            })
        };
        Self {
            clock: clock("clock", base, step),
            lease: lease.map(|(base, step)| clock("lease", base, step)),
            ids: Arc::new(ScriptedIds {
                count: Mutex::new(0),
                drawn: Arc::clone(&drawn),
            }),
            reads,
            drawn,
        }
    }

    /// Clock 100.0 stepping 0.25, no separate lease clock.
    fn standard() -> Self {
        Self::new(100.0, 0.25, None)
    }

    /// The reads and ids since the last call.
    fn take(&self) -> (Vec<(&'static str, u64)>, Vec<String>) {
        (
            std::mem::take(&mut *self.reads.lock().expect("reads")),
            std::mem::take(&mut *self.drawn.lock().expect("drawn")),
        )
    }

    fn inputs(
        &self,
        graph: Graph,
        caps: Vec<PlacementCapacity>,
        config: RouterConfig,
    ) -> CoordinatorInputs {
        let mut inputs = CoordinatorInputs::new(
            graph,
            states(),
            caps,
            profiles(),
            self.clock.clone(),
            self.ids.clone(),
            config,
        );
        inputs.lease_clock = self
            .lease
            .clone()
            .map(|lease| lease as Arc<dyn LeaseClock + Send + Sync>);
        inputs
    }
}

fn f(x: f64) -> PyReal {
    PyReal::Float(x)
}

fn i(n: i64) -> PyReal {
    PyReal::int(n)
}

/// A Python int from its decimal digits.
fn decimal(digits: &str) -> PyReal {
    let (negative, digits) = digits
        .strip_prefix('-')
        .map_or((false, digits), |rest| (true, rest));
    PyReal::signed(negative, digits.parse().expect("decimal digits"))
}

/// `count` reads of `name` from `base`, each the previous plus `step`.
fn seq(name: &'static str, base: f64, step: f64, count: usize) -> Vec<(&'static str, u64)> {
    let mut value = base;
    let mut out = Vec::new();
    for _ in 0..count {
        out.push((name, value.to_bits()));
        value += step;
    }
    out
}

fn reads(list: &[(&'static str, f64)]) -> Vec<(&'static str, u64)> {
    list.iter().map(|(name, v)| (*name, v.to_bits())).collect()
}

fn ids(list: &[&str]) -> Vec<String> {
    list.iter().map(|s| (*s).to_owned()).collect()
}

fn placement_doc(k: usize, pid: Value, node: &str) -> Value {
    json!({
        "placement_id": pid, "node_id": node, "replica_group_id": format!("group-{k}"),
        "assignment_id": format!("assignment-{k}-0"), "stage_signature": format!("signature-{k}"),
        "load_proof_digest": format!("proof-{k}-0"), "runtime_backend": "numpy",
        "runtime_endpoint": format!("local://{k}-0"),
    })
}

fn stage_doc(k: usize, kv: u64, placement: Value) -> Value {
    json!({
        "stage_id": format!("stage-{k}"),
        "range": {"start_layer": k, "end_layer_exclusive": k + 1, "layer_count": 1},
        "component_roles": ["decoder"],
        "stage_cost": {"prefill_work_units_per_prompt_token": 2, "decode_work_units_per_token": 1,
                       "kv_bytes_per_context_token": kv},
        "placements": [placement],
    })
}

/// stage-0 [p0 on node-a, KV 0 per token], stage-1 [p1 on node-b, KV 10 per token], edge-0
/// p0->p1, loop-0 p1->p0 (the probes' `graph_text(LINEAR)`), with these ids and versions. A
/// request of 4 prompt and 8 new tokens reserves 0 KV bytes on p0 and (4 + 8) * 10 = 120 on p1,
/// each with activation_bytes * 2 = 4 workspace bytes (mycelium_m16_runtime.py:227-228).
fn graph_doc(p0: Value, p1: Value, epoch: Value, topology: Value) -> Value {
    json!({
        "protocol": "mycelium.execution_graph.v1",
        "deployment_id": "deployment-m16", "deployment_epoch": epoch, "topology_version": topology,
        "model_id": "model-m16", "resolved_commit": "commit-m16", "manifest_digest": "digest-m16",
        "entry_stage_id": "stage-0", "final_stage_id": "stage-1",
        "hidden_size": 8, "activation_bytes": 2, "token_envelope_bytes": 16,
        "stages": [stage_doc(0, 0, placement_doc(0, p0.clone(), "node-a")),
                   stage_doc(1, 10, placement_doc(1, p1.clone(), "node-b"))],
        "edges": [{"edge_id": "edge-0", "from_placement_id": p0, "to_placement_id": p1,
                   "link_id": "link-edge-0"}],
        "loopback_edges": [{"edge_id": "loop-0", "from_placement_id": p1, "to_placement_id": p0,
                            "link_id": "link-loop-0"}],
    })
}

fn linear_doc() -> Value {
    graph_doc(json!("p0"), json!("p1"), json!(1), json!(1))
}

fn parse(doc: &Value) -> Graph {
    execution_graph::parse(doc).expect("a valid execution graph")
}

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

fn states() -> IdMap<DeviceState> {
    let mut map = IdMap::new();
    for node in ["node-a", "node-b", "node-c"] {
        map.insert(json!(node), state(node)).expect("hashable");
    }
    map
}

const PLENTY: u64 = 1_000_000_000_000;

fn capacity(id: Value) -> PlacementCapacity {
    PlacementCapacity {
        placement_id: id,
        memory_capacity_bytes: PyNumber::from(PLENTY),
        kv_capacity_bytes: PyNumber::from(PLENTY),
        workspace_capacity_bytes: PyNumber::from(PLENTY),
        maximum_reservations: 64,
    }
}

fn caps() -> Vec<PlacementCapacity> {
    vec![capacity(json!("p0")), capacity(json!("p1"))]
}

/// The factory's default profiles (mycelium_m16_runtime.py:1240-1244).
fn profiles() -> BTreeMap<String, String> {
    [
        ("interactive_chat_v1", "interactive"),
        ("sustained_batch_v1", "batch"),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_owned(), v.to_owned()))
    .collect()
}

/// `RouterConfig(reservation_lease_seconds=3_600.0)` (mycelium_m16_runtime.py:1260); its
/// `maximum_pending_hops` 1_024, `maximum_pending_bytes` 268_435_456 and
/// `maximum_runtime_batch_size` 20 (mycelium_router/contracts.py:159-163) reach the status.
fn factory_config() -> RouterConfig {
    let mut config = RouterConfig::oracle_default().expect("oracle defaults");
    config.reservation_lease_seconds = f(3600.0);
    config
}

/// The gateway's request shape (mycelium_request_gateway/backend.py:227-239): 4 prompt
/// tokens (16 queued payload bytes, scheduler.py:271-274), 8 new tokens, admitted at 0.0.
fn request_at(id: &str, qos: &str, admitted_at: PyReal) -> RequestContext {
    RequestContext {
        request_id: id.to_owned(),
        prompt_token_ids: (1..=4).map(i).collect(),
        max_new_tokens: i(8),
        expected_new_tokens: i(8),
        qos_class: qos.to_owned(),
        admitted_at,
        target_ttft_ms: f(1000.0),
        target_tpot_ms: f(100.0),
        target_tokens_per_second: f(10.0),
        sampling_seed: i(0),
        generation_config_digest: "sha256:test".to_owned(),
    }
}

fn request(id: &str, qos: &str) -> RequestContext {
    request_at(id, qos, f(0.0))
}

fn coordinator_over(
    rig: &Rig,
    graph: &Value,
    caps: Vec<PlacementCapacity>,
    config: RouterConfig,
    max: i64,
) -> M16RuntimeCoordinator {
    let mut inputs = rig.inputs(parse(graph), caps, config);
    inputs.max_concurrent_requests = Some(i(max));
    M16RuntimeCoordinator::new(inputs).expect("a coordinator")
}

fn coordinator(rig: &Rig, max: i64) -> M16RuntimeCoordinator {
    coordinator_over(rig, &linear_doc(), caps(), factory_config(), max)
}

fn admit(c: &M16RuntimeCoordinator, id: &str, qos: &str) {
    let profile = if qos == "batch" {
        "sustained_batch_v1"
    } else {
        "interactive_chat_v1"
    };
    c.admit(&request(id, qos), profile, &[]).expect("admitted");
}

/// `sha256:` + hex, computed independently of the coordinator.
fn sha256(text: &str) -> String {
    let digest = Sha256::digest(text.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("sha256:{hex}")
}

/// A linear-graph path document as `json.dumps(sort_keys=True, separators=(",", ":"))` writes
/// it (serialization.py:229-245), the expiry literals written by hand.
fn path_text(request: &str, path: &str, hops: [(&str, &str); 2]) -> String {
    let [(r0, e0), (r1, e1)] = hops;
    format!(
        concat!(
            r#"{{"deployment_epoch":1,"deployment_id":"deployment-m16","loopback_edge_id":"loop-0","#,
            r#""manifest_digest":"digest-m16","ordered_hops":[{{"placement_id":"p0","reservation_epoch":1,"#,
            r#""reservation_expires_at":{e0},"reservation_id":"{r0}","stage_id":"stage-0"}},"#,
            r#"{{"placement_id":"p1","reservation_epoch":1,"reservation_expires_at":{e1},"#,
            r#""reservation_id":"{r1}","stage_id":"stage-1"}}],"path_attempt":0,"path_id":"{path}","#,
            r#""protocol":"mycelium.path_manifest.v1","request_id":"{request}","topology_version":1}}"#
        ),
        e0 = e0,
        r0 = r0,
        e1 = e1,
        r1 = r1,
        path = path,
        request = request
    )
}

fn path_digest(request: &str, path: &str, hops: [(&str, &str); 2]) -> String {
    sha256(&path_text(request, path, hops))
}

/// Recorded by the core probe (m16-coordinator-oracle.json, case construct_default) and by
/// this task's probe (case status_fresh): `_digest(execution_graph_to_dict(graph))` of the
/// linear graph.
const LINEAR_GRAPH_DIGEST: &str =
    "sha256:c127d544ac21f5ec2edb32a2c22681e15d8f576aed4f1f8089b9c3b4b44b68cd";
/// Recorded by the core probe (case flow_default_lease_clock): the digest of r1's path when r1
/// is the first admission on a standard rig (expiries 3700.25 and 3700.75).
const R1_PATH_DIGEST: &str =
    "sha256:c4d07c89c64da519eb1978dae218cd6723aacddf9240928ac3617017e8403bc9";

// ---------------------------------------------------------------------------------------------
// Python values.

fn s(text: &str) -> PyJson {
    PyJson::str(text)
}

fn n(value: i64) -> PyJson {
    PyJson::Num(PyReal::int(value))
}

fn big(digits: &str) -> PyJson {
    PyJson::Num(decimal(digits))
}

fn x(value: f64) -> PyJson {
    PyJson::Num(PyReal::Float(value))
}

fn b(value: bool) -> PyJson {
    PyJson::Num(PyReal::Bool(value))
}

fn none() -> PyJson {
    PyJson::None
}

fn list(items: Vec<PyJson>) -> PyJson {
    PyJson::List(items)
}

fn opt_x(value: Option<f64>) -> PyJson {
    value.map_or(PyJson::None, x)
}

fn opt_s(value: Option<&str>) -> PyJson {
    value.map_or(PyJson::None, s)
}

/// A dict in exactly this key order.
fn dict(entries: Vec<(&str, PyJson)>) -> PyJson {
    PyJson::Dict(
        entries
            .into_iter()
            .map(|(k, v)| (PyStr::new(k), v))
            .collect(),
    )
}

/// A dict with its keys in code-point order (the order the validator's round trip leaves).
fn obj(entries: Vec<(&str, PyJson)>) -> PyJson {
    let mut entries: Vec<(PyStr, PyJson)> = entries
        .into_iter()
        .map(|(k, v)| (PyStr::new(k), v))
        .collect();
    entries.sort_by(|a, b| a.0.cmp(&b.0));
    PyJson::Dict(entries)
}

/// The same value with every dict's keys reversed (an unsorted input).
fn reversed(v: &PyJson) -> PyJson {
    match v {
        PyJson::Dict(entries) => PyJson::Dict(
            entries
                .iter()
                .rev()
                .map(|(k, item)| (k.clone(), reversed(item)))
                .collect(),
        ),
        PyJson::List(items) => PyJson::List(items.iter().map(reversed).collect()),
        other => other.clone(),
    }
}

fn get<'a>(v: &'a PyJson, key: &str) -> &'a PyJson {
    let PyJson::Dict(entries) = v else {
        panic!("not a dict: {v:?}")
    };
    let key = PyStr::new(key);
    entries
        .iter()
        .find(|(k, _)| *k == key)
        .map(|(_, item)| item)
        .unwrap_or_else(|| panic!("no key {key:?}"))
}

fn items(v: &PyJson) -> &[PyJson] {
    match v {
        PyJson::List(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

/// `{**v, key: value}`.
fn with(v: &PyJson, key: &str, value: PyJson) -> PyJson {
    let PyJson::Dict(entries) = v else {
        panic!("not a dict")
    };
    let name = PyStr::new(key);
    let mut entries = entries.clone();
    match entries.iter_mut().find(|(k, _)| *k == name) {
        Some(slot) => slot.1 = value,
        None => entries.push((name, value)),
    }
    PyJson::Dict(entries)
}

/// `{k: x for k, x in v.items() if k != key}`.
fn without(v: &PyJson, key: &str) -> PyJson {
    let PyJson::Dict(entries) = v else {
        panic!("not a dict")
    };
    let name = PyStr::new(key);
    PyJson::Dict(
        entries
            .iter()
            .filter(|(k, _)| *k != name)
            .cloned()
            .collect(),
    )
}

fn repeat(item: &PyJson, count: usize) -> PyJson {
    PyJson::List(vec![item.clone(); count])
}

fn text_of(v: &PyJson) -> String {
    match v {
        PyJson::Str(text) => text.to_text().expect("no surrogate"),
        other => panic!("not a str: {other:?}"),
    }
}

/// `10**exponent` (negated when `negative`) as a Python int.
fn pow10(exponent: usize, negative: bool) -> PyJson {
    let digits = format!(
        "{}1{}",
        if negative { "-" } else { "" },
        "0".repeat(exponent)
    );
    big(&digits)
}

/// The largest int `float()` keeps finite (2**1024 - 2**970 - 1, rounding down to f64::MAX)
/// and the smallest that overflows (2**1024 - 2**970, a tie rounding up to 2**1024).
const MAX_FINITE_INT: &str = "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497791";
const FIRST_OVERFLOW_INT: &str = "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497792";

// ---------------------------------------------------------------------------------------------
// Expected documents (mycelium_m16_runtime.py:615-650, 321-336, 1000-1018, 1031-1039).

const PROTOCOL: &str = "mycelium.concurrent_request_runtime.v1";
const CLAIM: &str = "bounded admission, immutable paths, QoS queue, and cleanup; physical microbatch and pipeline overlap unclaimed until observed";

/// A linear-graph placement with `active` live reservations holding `kv` KV and `workspace`
/// workspace bytes (memory = their sum; free = capacity - reserved, :321-336).
fn placement(pid: &str, node: &str, active: u64, kv: u64, workspace: u64) -> PyJson {
    let memory = kv + workspace;
    let u = |v: u64| n(i64::try_from(v).expect("small"));
    obj(vec![
        ("placement_id", s(pid)),
        ("node_id", s(node)),
        ("memory_capacity_bytes", u(PLENTY)),
        ("reserved_memory_bytes", u(memory)),
        ("free_memory_bytes", u(PLENTY - memory)),
        ("kv_capacity_bytes", u(PLENTY)),
        ("reserved_kv_bytes", u(kv)),
        ("free_kv_bytes", u(PLENTY - kv)),
        ("workspace_capacity_bytes", u(PLENTY)),
        ("reserved_workspace_bytes", u(workspace)),
        ("free_workspace_bytes", u(PLENTY - workspace)),
        ("active_reservations", u(active)),
        ("maximum_reservations", n(64)),
    ])
}

/// Both linear placements with `held` requests' reservations live.
fn linear_placements(held: u64) -> Vec<PyJson> {
    vec![
        placement("p0", "node-a", held, 0, 4 * held),
        placement("p1", "node-b", held, 120 * held, 4 * held),
    ]
}

fn queue(depth: i64, interactive: i64, batch: i64, active: &[&str], maximum: i64) -> PyJson {
    obj(vec![
        ("depth", n(depth)),
        ("maximum_items", n(1_024)),
        ("queued_bytes", n(16 * depth)),
        ("maximum_bytes", n(268_435_456)),
        ("interactive_depth", n(interactive)),
        ("batch_depth", n(batch)),
        (
            "active_request_ids",
            list(active.iter().map(|id| s(id)).collect()),
        ),
        ("maximum_active_requests", n(maximum)),
    ])
}

/// One request projection on the linear graph.
struct Projection<'a> {
    id: &'a str,
    qos: &'a str,
    phase: &'a str,
    path: &'a str,
    digest: String,
    reservations: i64,
    queued_at: f64,
    dispatch_at: Option<f64>,
    terminal: Option<(f64, &'a str)>,
    queue_wait_ms: Option<f64>,
}

fn projection(p: Projection<'_>) -> PyJson {
    let profile = if p.qos == "batch" {
        "sustained_batch_v1"
    } else {
        "interactive_chat_v1"
    };
    obj(vec![
        ("request_id", s(p.id)),
        ("workload_profile_id", s(profile)),
        ("qos_class", s(p.qos)),
        ("phase", s(p.phase)),
        ("path_id", s(p.path)),
        ("path_attempt", n(0)),
        ("path_manifest_digest", s(&p.digest)),
        ("topology_version", n(1)),
        ("path_state", s("locked")),
        ("candidate_placement_ids", list(vec![s("p0"), s("p1")])),
        ("placement_ids", list(vec![s("p0"), s("p1")])),
        ("reservation_count", n(p.reservations)),
        ("admitted_at_monotonic_s", x(0.0)),
        ("queued_at_monotonic_s", x(p.queued_at)),
        ("dispatch_at_monotonic_s", opt_x(p.dispatch_at)),
        (
            "terminal_at_monotonic_s",
            opt_x(p.terminal.map(|(at, _)| at)),
        ),
        ("queue_wait_ms", opt_x(p.queue_wait_ms)),
        ("terminal_state", opt_s(p.terminal.map(|(_, state)| state))),
    ])
}

fn incident(
    k: u64,
    kind: &str,
    request_id: &str,
    scope: &str,
    state: &str,
    at: f64,
    retry: PyJson,
) -> PyJson {
    obj(vec![
        ("incident_id", s(&format!("m16-incident-{k}"))),
        ("kind", s(kind)),
        ("request_id", s(request_id)),
        ("scope", s(scope)),
        ("state", s(state)),
        ("observed_at_monotonic_s", x(at)),
        ("retry_after_seconds", retry),
    ])
}

fn batch_state() -> PyJson {
    obj(vec![
        ("mode", s("concurrent_request_sequential_stage_dispatch")),
        ("maximum_runtime_batch_size", n(20)),
        ("observed_batches", list(vec![])),
        ("continuous_batching", b(false)),
        ("pipeline_overlap", b(false)),
    ])
}

fn status_doc(
    generated_at: f64,
    queue: PyJson,
    placements: Vec<PyJson>,
    requests: Vec<PyJson>,
    incidents: Vec<PyJson>,
    budgets: Vec<PyJson>,
) -> PyJson {
    obj(vec![
        ("protocol", s(PROTOCOL)),
        ("generated_at_monotonic_s", x(generated_at)),
        ("deployment_id", s("deployment-m16")),
        ("deployment_epoch", n(1)),
        ("topology_version", n(1)),
        ("graph_digest", s(LINEAR_GRAPH_DIGEST)),
        ("queue", queue),
        ("placements", list(placements)),
        ("requests", list(requests)),
        ("incidents", list(incidents)),
        ("batch_state", batch_state()),
        ("claim_boundary", s(CLAIM)),
        ("performance_budgets", list(budgets)),
    ])
}

fn request_ids(doc: &PyJson) -> Vec<String> {
    items(get(doc, "requests"))
        .iter()
        .map(|r| text_of(get(r, "request_id")))
        .collect()
}

fn active_ids(doc: &PyJson) -> Vec<String> {
    items(get(get(doc, "queue"), "active_request_ids"))
        .iter()
        .map(text_of)
        .collect()
}

/// Admits `h-{k:05}` for each `k`, dispatches and completes it.
fn serve(c: &M16RuntimeCoordinator, ks: std::ops::RangeInclusive<u64>) {
    for k in ks {
        let id = format!("h-{k:05}");
        admit(c, &id, "interactive");
        assert_eq!(c.next_dispatch(), Ok(Some(id.clone())), "{id}");
        c.complete(&id, "completed").expect("completed");
    }
}

/// Admits `x-{k:05}` for each `k` with stage-1's only placement excluded: a RoutingError, one
/// `admission_rejected` incident and no record (:442-449).
fn reject(c: &M16RuntimeCoordinator, ks: std::ops::RangeInclusive<u64>) {
    for k in ks {
        let id = format!("x-{k:05}");
        let refused = c.admit(
            &request(&id, "interactive"),
            "interactive_chat_v1",
            &[json!("p1")],
        );
        assert!(
            matches!(&refused, Err(CoordinatorError::Admission(e)) if e.code == "resource_unavailable"),
            "{id}: {refused:?}"
        );
    }
}

#[test]
fn m16_coordinator_status_document() {
    // The hand-written path text reproduces the oracle's recorded r1 digest.
    assert_eq!(
        path_digest(
            "r1",
            "path-1",
            [("reservation-2", "3700.25"), ("reservation-3", "3700.75")]
        ),
        R1_PATH_DIGEST
    );
    let rig = Rig::standard();
    let c = coordinator(&rig, 2);
    assert_eq!(rig.take(), (vec![], vec![]));
    // A fresh coordinator: one clock read for generated_at_monotonic_s (:617), the config's
    // bounds, both placements free, nothing else.
    assert_eq!(
        c.status(),
        Ok(status_doc(
            100.0,
            queue(0, 0, 0, &[], 2),
            linear_placements(0),
            vec![],
            vec![],
            vec![]
        ))
    );
    assert_eq!(rig.take(), (reads(&[("clock", 100.0)]), vec![]));

    // r1 (interactive) then b1 (batch): nine reads each (advance clock + lease per stage, lock
    // clock + lease, the ledger's commit one lease read per reservation, the queue time).
    admit(&c, "r1", "interactive");
    admit(&c, "b1", "batch");
    assert_eq!(
        rig.take(),
        (
            seq("clock", 100.25, 0.25, 18),
            ids(&[
                "path-1",
                "reservation-2",
                "reservation-3",
                "path-4",
                "reservation-5",
                "reservation-6"
            ])
        )
    );
    // Each expiry is its stage's lease read + 3600.0.
    let r1_digest = path_digest(
        "r1",
        "path-1",
        [("reservation-2", "3700.5"), ("reservation-3", "3701.0")],
    );
    let b1_digest = path_digest(
        "b1",
        "path-4",
        [("reservation-5", "3702.75"), ("reservation-6", "3703.25")],
    );
    let r1 = |phase, reservations, dispatch_at, terminal, queue_wait_ms| {
        projection(Projection {
            id: "r1",
            qos: "interactive",
            phase,
            path: "path-1",
            digest: r1_digest.clone(),
            reservations,
            queued_at: 102.25,
            dispatch_at,
            terminal,
            queue_wait_ms,
        })
    };
    let b1 = |phase, reservations, terminal| {
        projection(Projection {
            id: "b1",
            qos: "batch",
            phase,
            path: "path-4",
            digest: b1_digest.clone(),
            reservations,
            queued_at: 104.5,
            dispatch_at: None,
            terminal,
            queue_wait_ms: None,
        })
    };
    // Two queued records, one per QoS class (:625-631, `str ==`), 2 x 16 queued bytes.
    assert_eq!(
        c.status(),
        Ok(status_doc(
            104.75,
            queue(2, 1, 1, &[], 2),
            linear_placements(2),
            vec![r1("queued", 2, None, None, None), b1("queued", 2, None)],
            vec![],
            vec![]
        ))
    );
    assert_eq!(rig.take(), (reads(&[("clock", 104.75)]), vec![]));

    // r1 dispatches: pop (105.0), one lease read per live reservation (105.25, 105.5), the
    // dispatch time 105.75; queue_wait_ms = (105.75 - 102.25) * 1000.0 = 3500.0.
    assert_eq!(c.next_dispatch(), Ok(Some("r1".to_owned())));
    assert_eq!(rig.take(), (seq("clock", 105.0, 0.25, 4), vec![]));
    assert_eq!(
        c.status(),
        Ok(status_doc(
            106.0,
            queue(1, 0, 1, &["r1"], 2),
            linear_placements(2),
            vec![
                r1("prefill", 2, Some(105.75), None, Some(3500.0)),
                b1("queued", 2, None)
            ],
            vec![],
            vec![]
        ))
    );
    assert_eq!(rig.take(), (reads(&[("clock", 106.0)]), vec![]));

    // r1 completes at 106.25; b1 is cancelled at 106.5, its incident observed at 106.75.
    c.complete("r1", "completed").expect("completed");
    assert!(c.cancel("b1"));
    assert_eq!(rig.take(), (seq("clock", 106.25, 0.25, 3), vec![]));
    assert_eq!(
        c.status(),
        Ok(status_doc(
            107.0,
            queue(0, 0, 0, &[], 2),
            linear_placements(0),
            vec![
                r1(
                    "completed",
                    0,
                    Some(105.75),
                    Some((106.25, "completed")),
                    Some(3500.0)
                ),
                b1("cancelled", 0, Some((106.5, "cancelled")))
            ],
            vec![incident(
                1,
                "cancellation_cleanup",
                "b1",
                "request",
                "resources_released",
                106.75,
                none()
            )],
            vec![]
        ))
    );
    assert_eq!(rig.take(), (reads(&[("clock", 107.0)]), vec![]));

    // The clock is read before placement_status (:617 before :621): a ledger exception (here
    // `sorted` of a str and an int placement id, TypeError) propagates after the read.
    let rig = Rig::standard();
    let mixed = graph_doc(json!("p0"), json!(1), json!(1), json!(1));
    let c = coordinator_over(
        &rig,
        &mixed,
        vec![capacity(json!("p0")), capacity(json!(1))],
        factory_config(),
        1,
    );
    assert_eq!(
        c.status(),
        Err(StatusError::Ledger(LedgerError(
            "m16_ledger_unorderable_placement_ids"
        )))
    );
    assert_eq!(rig.take(), (reads(&[("clock", 100.0)]), vec![]));

    // The projection bound (:614): the newest 1,024 records in insertion order, while
    // queue.active_request_ids keeps every active request, sorted.
    let rig = Rig::standard();
    let c = coordinator(&rig, 3);
    admit(&c, "z-old", "interactive");
    assert_eq!(c.next_dispatch(), Ok(Some("z-old".to_owned())));
    admit(&c, "a-old", "interactive");
    assert_eq!(c.next_dispatch(), Ok(Some("a-old".to_owned())));
    serve(&c, 1..=1022);
    let history: Vec<String> = ["z-old", "a-old"]
        .into_iter()
        .map(str::to_owned)
        .chain((1..=1024).map(|k| format!("h-{k:05}")))
        .collect();
    let doc = c.status().expect("1,024 records");
    assert_eq!(request_ids(&doc), history[..1024].to_vec());
    assert_eq!(active_ids(&doc), ["a-old", "z-old"]);
    serve(&c, 1023..=1023);
    let doc = c.status().expect("1,025 records");
    assert_eq!(request_ids(&doc), history[1..1025].to_vec());
    assert_eq!(active_ids(&doc), ["a-old", "z-old"]);
    serve(&c, 1024..=1024);
    let doc = c.status().expect("1,026 records");
    assert_eq!(request_ids(&doc), history[2..1026].to_vec());
    assert_eq!(active_ids(&doc), ["a-old", "z-old"]);
    assert_eq!(get(doc_queue(&doc), "depth"), &n(0));
    assert_eq!(get(doc_queue(&doc), "interactive_depth"), &n(0));
    let last = items(get(&doc, "requests")).last().expect("a request");
    assert_eq!(get(last, "phase"), &s("completed"));
    assert_eq!(get(last, "reservation_count"), &n(0));

    // The incident bound (:634): the last 256 in order.
    let rig = Rig::standard();
    let c = coordinator(&rig, 1);
    reject(&c, 1..=256);
    let incident_ids = |doc: &PyJson| -> Vec<String> {
        items(get(doc, "incidents"))
            .iter()
            .map(|x| text_of(get(x, "incident_id")))
            .collect()
    };
    let doc = c.status().expect("256 incidents");
    assert_eq!(
        incident_ids(&doc),
        (1..=256)
            .map(|k| format!("m16-incident-{k}"))
            .collect::<Vec<_>>()
    );
    reject(&c, 257..=257);
    let doc = c.status().expect("257 incidents");
    assert_eq!(
        incident_ids(&doc),
        (2..=257)
            .map(|k| format!("m16-incident-{k}"))
            .collect::<Vec<_>>()
    );
    let newest = items(get(&doc, "incidents")).last().expect("an incident");
    assert_eq!(get(newest, "kind"), &s("admission_rejected"));
    assert_eq!(get(newest, "request_id"), &s("x-00257"));
    assert_eq!(get(newest, "scope"), &s("deployment"));
    assert_eq!(get(newest, "state"), &s("resource_unavailable"));
    assert_eq!(get(newest, "retry_after_seconds"), &none());
    assert_eq!(get(&doc, "requests"), &list(vec![]));
}

fn doc_queue(doc: &PyJson) -> &PyJson {
    get(doc, "queue")
}

#[test]
fn m16_coordinator_status_request_projection() {
    // One record per lifecycle state, each admitted (nine reads) and, but for the queued ones,
    // dispatched (four reads) right away: path-(3k-2) with reservations 3k-1 and 3k.
    let rig = Rig::standard();
    let c = coordinator(&rig, 4);
    let dispatch = |id: &str| {
        assert_eq!(c.next_dispatch(), Ok(Some(id.to_owned())), "{id}");
    };
    admit(&c, "p", "interactive"); // 100.0 .. 102.0
    dispatch("p"); // 102.25 .. 103.0
    admit(&c, "d", "interactive"); // 103.25 .. 105.25
    dispatch("d"); // 105.5 .. 106.25
    c.mark_phase("d", "first_token").expect("first_token");
    c.mark_phase("d", "decode").expect("decode");
    admit(&c, "k", "interactive"); // 106.5 .. 108.5
    dispatch("k"); // 108.75 .. 109.5
    c.mark_phase("k", "cleanup").expect("cleanup");
    c.retire_dispatch_slot("k");
    admit(&c, "c", "interactive"); // 109.75 .. 111.75
    dispatch("c"); // 112.0 .. 112.75
    c.complete("c", "completed").expect("completed"); // 113.0
    admit(&c, "f", "batch"); // 113.25 .. 115.25
    dispatch("f"); // 115.5 .. 116.25
    c.complete("f", "failed").expect("failed"); // 116.5
    admit(&c, "a", "interactive"); // 116.75 .. 118.75
    dispatch("a"); // 119.0 .. 119.75
    assert!(c.cancel("a")); // 120.0, incident 120.25
    admit(&c, "q", "batch"); // 120.5 .. 122.5
    admit(&c, "x", "interactive"); // 122.75 .. 124.75
    assert!(c.cancel("x")); // 125.0, incident 125.25
    let (drawn_reads, _) = rig.take();
    assert_eq!(drawn_reads, seq("clock", 100.0, 0.25, 102));
    let digest = |id: &str, k: u64, e0: &str, e1: &str| {
        path_digest(
            id,
            &format!("path-{}", 3 * k - 2),
            [
                (&format!("reservation-{}", 3 * k - 1), e0),
                (&format!("reservation-{}", 3 * k), e1),
            ],
        )
    };
    let doc = c.status().expect("a status");
    assert_eq!(rig.take(), (reads(&[("clock", 125.5)]), vec![]));
    let expected = vec![
        projection(Projection {
            id: "p",
            qos: "interactive",
            phase: "prefill",
            path: "path-1",
            digest: digest("p", 1, "3700.25", "3700.75"),
            reservations: 2,
            queued_at: 102.0,
            dispatch_at: Some(103.0),
            terminal: None,
            queue_wait_ms: Some(1000.0),
        }),
        projection(Projection {
            id: "d",
            qos: "interactive",
            phase: "decode",
            path: "path-4",
            digest: digest("d", 2, "3703.5", "3704.0"),
            reservations: 2,
            queued_at: 105.25,
            dispatch_at: Some(106.25),
            terminal: None,
            queue_wait_ms: Some(1000.0),
        }),
        projection(Projection {
            id: "k",
            qos: "interactive",
            phase: "cleanup",
            path: "path-7",
            digest: digest("k", 3, "3706.75", "3707.25"),
            reservations: 2,
            queued_at: 108.5,
            dispatch_at: Some(109.5),
            terminal: None,
            queue_wait_ms: Some(1000.0),
        }),
        projection(Projection {
            id: "c",
            qos: "interactive",
            phase: "completed",
            path: "path-10",
            digest: digest("c", 4, "3710.0", "3710.5"),
            reservations: 0,
            queued_at: 111.75,
            dispatch_at: Some(112.75),
            terminal: Some((113.0, "completed")),
            queue_wait_ms: Some(1000.0),
        }),
        projection(Projection {
            id: "f",
            qos: "batch",
            phase: "failed",
            path: "path-13",
            digest: digest("f", 5, "3713.5", "3714.0"),
            reservations: 0,
            queued_at: 115.25,
            dispatch_at: Some(116.25),
            terminal: Some((116.5, "failed")),
            queue_wait_ms: Some(1000.0),
        }),
        projection(Projection {
            id: "a",
            qos: "interactive",
            phase: "cancelled",
            path: "path-16",
            digest: digest("a", 6, "3717.0", "3717.5"),
            reservations: 0,
            queued_at: 118.75,
            dispatch_at: Some(119.75),
            terminal: Some((120.0, "cancelled")),
            queue_wait_ms: Some(1000.0),
        }),
        projection(Projection {
            id: "q",
            qos: "batch",
            phase: "queued",
            path: "path-19",
            digest: digest("q", 7, "3720.75", "3721.25"),
            reservations: 2,
            queued_at: 122.5,
            dispatch_at: None,
            terminal: None,
            queue_wait_ms: None,
        }),
        projection(Projection {
            id: "x",
            qos: "interactive",
            phase: "cancelled",
            path: "path-22",
            digest: digest("x", 8, "3723.0", "3723.5"),
            reservations: 0,
            queued_at: 124.75,
            dispatch_at: None,
            terminal: Some((125.0, "cancelled")),
            queue_wait_ms: None,
        }),
    ];
    assert_eq!(get(&doc, "requests"), &list(expected));
    // p, d (dispatched) and q (queued) hold their reservations; k too, retired in cleanup.
    assert_eq!(get(&doc, "placements"), &list(linear_placements(4)));
    assert_eq!(get(&doc, "queue"), &queue(1, 0, 1, &["d", "p"], 4));
    // The supervisor's sum (supervisor.py:5309): 2 + 2 + 2 + 2.
    let held: i64 = items(get(&doc, "requests"))
        .iter()
        .map(|r| match get(r, "reservation_count") {
            PyJson::Num(PyReal::Int { magnitude, .. }) => {
                i64::try_from(magnitude.to_string().parse::<u64>().expect("small")).expect("small")
            }
            other => panic!("not an int: {other:?}"),
        })
        .sum();
    assert_eq!(held, 8);

    // queue_wait_ms in float arithmetic, bit for bit: a clock 0.1 stepping 0.1 queues r1 at
    // 0.8999999999999999 and dispatches it at 1.3; (1.3 - 0.8999999999999999) * 1000.0 is
    // 400.0000000000001 (recorded by the probe, case status_float_noise).
    let rig = Rig::new(0.1, 0.1, None);
    let c = coordinator(&rig, 1);
    admit(&c, "r1", "interactive");
    assert_eq!(c.next_dispatch(), Ok(Some("r1".to_owned())));
    c.complete("r1", "completed").expect("completed");
    let doc = c.status().expect("a status");
    let r1 = &items(get(&doc, "requests"))[0];
    let bits = |key: &str| match get(r1, key) {
        PyJson::Num(PyReal::Float(v)) => v.to_bits(),
        other => panic!("{key}: not a float: {other:?}"),
    };
    assert_eq!(bits("queued_at_monotonic_s"), 0x3fec_cccc_cccc_cccc);
    assert_eq!(bits("dispatch_at_monotonic_s"), 0x3ff4_cccc_cccc_cccd);
    assert_eq!(bits("terminal_at_monotonic_s"), 0x3ff6_6666_6666_6667);
    assert_eq!(bits("queue_wait_ms"), 0x4079_0000_0000_0002);
    assert_eq!(
        get(&doc, "generated_at_monotonic_s"),
        &x(f64::from_bits(0x3ff8_0000_0000_0001))
    );

    // A request that expires queued (:507-518): failed, reservation_expired, never dispatched.
    let rig = Rig::new(100.0, 1.0, Some((1000.0, 1.0)));
    let mut config = factory_config();
    config.reservation_lease_seconds = f(5.0);
    let c = coordinator_over(&rig, &linear_doc(), caps(), config, 4);
    admit(&c, "r1", "interactive");
    rig.lease.as_ref().expect("a lease clock").set(1005.0);
    assert_eq!(c.next_dispatch(), Ok(None));
    let doc = c.status().expect("a status");
    let r1 = &items(get(&doc, "requests"))[0];
    assert_eq!(get(r1, "phase"), &s("failed"));
    assert_eq!(get(r1, "terminal_state"), &s("reservation_expired"));
    assert_eq!(get(r1, "queued_at_monotonic_s"), &x(103.0));
    assert_eq!(get(r1, "dispatch_at_monotonic_s"), &none());
    assert_eq!(get(r1, "terminal_at_monotonic_s"), &x(105.0));
    assert_eq!(get(r1, "queue_wait_ms"), &none());
    assert_eq!(get(r1, "reservation_count"), &n(0));
    assert_eq!(
        get(&doc, "incidents"),
        &list(vec![incident(
            1,
            "reservation_expired",
            "r1",
            "request",
            "failed",
            106.0,
            none()
        )])
    );
    assert_eq!(get(&doc, "generated_at_monotonic_s"), &x(107.0));

    // `float(request.admitted_at)` (:1012): an int, a bool, -0.0, an int past 2**64 (rounded
    // half even), the largest int that stays finite; past it, OverflowError after the read.
    let rig = Rig::standard();
    let c = coordinator(&rig, 8);
    for (id, admitted_at) in [
        ("int", i(5)),
        ("true", PyReal::Bool(true)),
        ("neg_zero", f(-0.0)),
        ("wide", decimal("18446744073709551617")),
        ("max", decimal(MAX_FINITE_INT)),
    ] {
        c.admit(
            &request_at(id, "interactive", admitted_at),
            "interactive_chat_v1",
            &[],
        )
        .expect("admitted");
    }
    let doc = c.status().expect("a status");
    let admitted: Vec<u64> = items(get(&doc, "requests"))
        .iter()
        .map(|r| match get(r, "admitted_at_monotonic_s") {
            PyJson::Num(PyReal::Float(v)) => v.to_bits(),
            other => panic!("not a float: {other:?}"),
        })
        .collect();
    assert_eq!(
        admitted,
        [
            5.0f64.to_bits(),
            1.0f64.to_bits(),
            (-0.0f64).to_bits(),
            18_446_744_073_709_551_616.0f64.to_bits(),
            f64::MAX.to_bits()
        ]
    );
    c.admit(
        &request_at("huge", "interactive", decimal(FIRST_OVERFLOW_INT)),
        "interactive_chat_v1",
        &[],
    )
    .expect("admitted");
    rig.take();
    assert_eq!(
        c.status(),
        Err(StatusError::Python(STATUS_INT_TOO_LARGE_FOR_FLOAT))
    );
    assert_eq!(rig.take(), (seq("clock", 113.75, 0.25, 1), vec![]));

    // Numeric placement ids and versions keep their Python types: candidate and placement ids
    // [1, 2.5], topology_version the int 2**64 + 1, deployment_epoch the float 1.0.
    let rig = Rig::standard();
    let topology: Value = serde_json::from_str("18446744073709551617").expect("an int");
    let numeric = graph_doc(json!(1), json!(2.5), json!(1.0), topology);
    let c = coordinator_over(
        &rig,
        &numeric,
        vec![capacity(json!(1)), capacity(json!(2.5))],
        factory_config(),
        1,
    );
    admit(&c, "r1", "interactive");
    let doc = c.status().expect("a status");
    let r1 = &items(get(&doc, "requests"))[0];
    let ids_value = list(vec![n(1), x(2.5)]);
    assert_eq!(get(r1, "candidate_placement_ids"), &ids_value);
    assert_eq!(get(r1, "placement_ids"), &ids_value);
    assert_eq!(get(r1, "topology_version"), &big("18446744073709551617"));
    assert_eq!(get(&doc, "topology_version"), &big("18446744073709551617"));
    assert_eq!(get(&doc, "deployment_epoch"), &x(1.0));
    let placement_ids: Vec<PyJson> = items(get(&doc, "placements"))
        .iter()
        .map(|p| get(p, "placement_id").clone())
        .collect();
    assert_eq!(placement_ids, [n(1), x(2.5)]);
}

/// A valid v3 budget, keys in the builder's (unsorted) order
/// (scripts/build_m16_performance_budget.py:68-213).
fn budget(budget_id: &str) -> PyJson {
    let dimension = |name: &str, state: &str, bound: f64, observed: f64, unit: &str| {
        dict(vec![
            ("dimension", s(name)),
            ("state", s(state)),
            ("bound", x(bound)),
            ("observed", x(observed)),
            ("unit", s(unit)),
            (
                "evidence_digest",
                s(&format!("sha256:{}", "0123456789abcdef".repeat(4))),
            ),
            ("reason", s("observed")),
        ])
    };
    dict(vec![
        ("protocol", s("mycelium.performance_budget.v3")),
        ("budget_id", s(budget_id)),
        ("profile_id", s("mixed_interactive_batch_v1")),
        ("evidence_scope", s("concurrent_physical_observed")),
        ("observed_request_count", n(3)),
        (
            "dimensions",
            list(vec![
                dimension(
                    "admission_latency_p95_ms",
                    "met",
                    100.0,
                    12.5,
                    "milliseconds",
                ),
                dimension("queue_wait_p95_ms", "met", 60_000.0, 431.25, "milliseconds"),
                dimension("maximum_queue_depth", "met", 32.0, 3.0, "requests"),
                dimension("completed_requests", "met", 2.0, 2.0, "requests"),
                dimension(
                    "interactive_latency_regression_ratio",
                    "met",
                    6.0,
                    1.0000000000000002,
                    "ratio",
                ),
                dimension(
                    "batch_starvation_interval_ms",
                    "met",
                    60_000.0,
                    0.1,
                    "milliseconds",
                ),
                dimension(
                    "cancellation_release_latency_ms",
                    "met",
                    1_000.0,
                    0.30000000000000004,
                    "milliseconds",
                ),
                dimension(
                    "runtime_batch_size",
                    "approved_exclusion",
                    4.0,
                    1.0,
                    "requests",
                ),
            ]),
        ),
        ("overall_state", s("met_with_approved_exclusions")),
    ])
}

/// The budget with dimension `k`'s `key` set to `value`.
fn with_dimension(doc: &PyJson, k: usize, key: &str, value: PyJson) -> PyJson {
    let mut dims = items(get(doc, "dimensions")).to_vec();
    dims[k] = with(&dims[k], key, value);
    with(doc, "dimensions", list(dims))
}

/// `budget`'s validated document: every dict's keys in code-point order.
fn sorted(doc: &PyJson) -> PyJson {
    match doc {
        PyJson::Dict(entries) => {
            let mut entries: Vec<(PyStr, PyJson)> = entries
                .iter()
                .map(|(k, v)| (k.clone(), sorted(v)))
                .collect();
            entries.sort_by(|a, b| a.0.cmp(&b.0));
            PyJson::Dict(entries)
        }
        PyJson::List(items) => PyJson::List(items.iter().map(sorted).collect()),
        other => other.clone(),
    }
}

/// A small valid status document, keys unsorted (reversed): one placement, one request, one
/// incident with a float retry, one budget.
fn base_status() -> PyJson {
    let request = projection(Projection {
        id: "r1",
        qos: "interactive",
        phase: "prefill",
        path: "path-1",
        digest: R1_PATH_DIGEST.to_owned(),
        reservations: 2,
        queued_at: 102.0,
        dispatch_at: Some(103.0),
        terminal: None,
        queue_wait_ms: Some(1000.0),
    });
    reversed(&status_doc(
        104.0,
        queue(1, 0, 1, &["r1"], 2),
        vec![placement("p0", "node-a", 1, 0, 4)],
        vec![request],
        vec![incident(
            1,
            "backpressure",
            "r3",
            "queue",
            "work_limit",
            103.5,
            x(0.01),
        )],
        vec![sorted(&budget("m16-concurrent-physical-v1"))],
    ))
}

#[test]
fn m16_runtime_status_validation_refusals() {
    use StatusValidationError as E;
    let base = base_status();
    let ok = sorted(&base);
    let validate = |doc: &PyJson| validate_m16_runtime_status(doc);
    // The round trip sorts every dict's keys and keeps every value (:1157).
    assert_eq!(validate(&base), Ok(ok.clone()));
    assert_eq!(validate(&ok), Ok(ok.clone()));

    // 1. Shape (:1066-1067).
    assert_eq!(validate(&list(vec![base.clone()])), Err(E::Shape));
    assert_eq!(validate(&none()), Err(E::Shape));
    assert_eq!(validate(&s("status")), Err(E::Shape));
    assert_eq!(validate(&without(&base, "claim_boundary")), Err(E::Shape));
    assert_eq!(validate(&with(&base, "surprise", b(true))), Err(E::Shape));
    assert_eq!(
        validate(&without(&with(&base, "protocol", s("x")), "incidents")),
        Err(E::Shape)
    );

    // 2. Protocol (:1068-1069): Python `!=`, so no other type equals the str.
    assert_eq!(
        validate(&with(
            &base,
            "protocol",
            s("mycelium.concurrent_request_runtime.v2")
        )),
        Err(E::Protocol)
    );
    assert_eq!(validate(&with(&base, "protocol", n(1))), Err(E::Protocol));
    assert_eq!(validate(&with(&base, "protocol", none())), Err(E::Protocol));
    assert_eq!(
        validate(&with(
            &with(&base, "protocol", s("x")),
            "queue",
            list(vec![])
        )),
        Err(E::Protocol)
    );

    // 3. Queue shape (:1070-1080): the keys only, not the values.
    let base_queue = get(&base, "queue").clone();
    assert_eq!(
        validate(&with(&base, "queue", list(vec![]))),
        Err(E::QueueShape)
    );
    assert_eq!(
        validate(&with(&base, "queue", without(&base_queue, "depth"))),
        Err(E::QueueShape)
    );
    assert_eq!(
        validate(&with(&base, "queue", with(&base_queue, "prompt", n(1)))),
        Err(E::QueueShape)
    );
    let loose_queue = with(
        &with(&base_queue, "depth", s("anything")),
        "active_request_ids",
        none(),
    );
    assert_eq!(
        validate(&with(&base, "queue", loose_queue.clone())),
        Ok(with(&ok, "queue", sorted(&loose_queue)))
    );
    assert_eq!(
        validate(&with(
            &with(&base, "queue", list(vec![])),
            "placements",
            list(vec![])
        )),
        Err(E::QueueShape)
    );

    // 4. Nested shape (:1125-1146), each bound on both sides.
    let one_placement = items(get(&base, "placements"))[0].clone();
    let one_request = items(get(&base, "requests"))[0].clone();
    let one_incident = items(get(&base, "incidents"))[0].clone();
    let one_budget = items(get(&base, "performance_budgets"))[0].clone();
    assert_eq!(
        validate(&with(&base, "placements", list(vec![]))),
        Err(E::NestedShape)
    );
    assert_eq!(
        validate(&with(
            &base,
            "placements",
            dict(vec![("a", one_placement.clone())])
        )),
        Err(E::NestedShape)
    );
    assert_eq!(
        validate(&with(
            &base,
            "placements",
            list(vec![without(&one_placement, "node_id")])
        )),
        Err(E::NestedShape)
    );
    assert_eq!(
        validate(&with(
            &base,
            "placements",
            list(vec![one_placement.clone(), n(1)])
        )),
        Err(E::NestedShape)
    );
    let two_placements = repeat(&one_placement, 2);
    assert_eq!(
        validate(&with(&base, "placements", two_placements.clone())),
        Ok(with(&ok, "placements", sorted(&two_placements)))
    );
    let requests_1024 = repeat(&one_request, 1024);
    assert_eq!(
        validate(&with(&base, "requests", requests_1024.clone())),
        Ok(with(&ok, "requests", sorted(&requests_1024)))
    );
    assert_eq!(
        validate(&with(&base, "requests", repeat(&one_request, 1025))),
        Err(E::NestedShape)
    );
    assert_eq!(
        validate(&with(&base, "requests", list(vec![]))),
        Ok(with(&ok, "requests", list(vec![])))
    );
    assert_eq!(
        validate(&with(
            &base,
            "requests",
            list(vec![with(&one_request, "tokens", list(vec![]))])
        )),
        Err(E::NestedShape)
    );
    let incidents_256 = repeat(&one_incident, 256);
    assert_eq!(
        validate(&with(&base, "incidents", incidents_256.clone())),
        Ok(with(&ok, "incidents", sorted(&incidents_256)))
    );
    assert_eq!(
        validate(&with(&base, "incidents", repeat(&one_incident, 257))),
        Err(E::NestedShape)
    );
    // A private key in an incident fails its exact key set first.
    assert_eq!(
        validate(&with(
            &base,
            "incidents",
            list(vec![with(&one_incident, "prompt", s("x"))])
        )),
        Err(E::NestedShape)
    );
    assert_eq!(
        validate(&with(
            &base,
            "batch_state",
            without(get(&base, "batch_state"), "mode")
        )),
        Err(E::NestedShape)
    );
    assert_eq!(
        validate(&with(&base, "batch_state", list(vec![]))),
        Err(E::NestedShape)
    );
    let budgets_16 = repeat(&one_budget, 16);
    assert_eq!(
        validate(&with(&base, "performance_budgets", budgets_16.clone())),
        Ok(with(&ok, "performance_budgets", sorted(&budgets_16)))
    );
    assert_eq!(
        validate(&with(&base, "performance_budgets", repeat(&one_budget, 17))),
        Err(E::NestedShape)
    );
    assert_eq!(
        validate(&with(&base, "performance_budgets", dict(vec![]))),
        Err(E::NestedShape)
    );

    // 5. Budget shape (:1147-1153): PerformanceBudgetError and TypeError; OverflowError passes.
    let bad_budget = with(&one_budget, "protocol", s("x"));
    assert_eq!(
        validate(&with(
            &base,
            "performance_budgets",
            list(vec![bad_budget.clone()])
        )),
        Err(E::BudgetShape)
    );
    assert_eq!(
        validate(&with(
            &base,
            "performance_budgets",
            list(vec![with(&one_budget, "prompt", s("x"))])
        )),
        Err(E::BudgetShape)
    );
    assert_eq!(
        validate(&with(
            &base,
            "performance_budgets",
            list(vec![with_dimension(
                &one_budget,
                0,
                "dimension",
                list(vec![n(1)])
            )])
        )),
        Err(E::BudgetShape)
    );
    assert_eq!(
        validate(&with(
            &base,
            "performance_budgets",
            list(vec![with_dimension(
                &one_budget,
                0,
                "bound",
                pow10(400, false)
            )])
        )),
        Err(E::BudgetIntTooLargeForFloat)
    );
    assert_eq!(
        validate(&with(
            &base,
            "performance_budgets",
            list(vec![
                one_budget.clone(),
                with(&one_budget, "overall_state", s("met"))
            ])
        )),
        Err(E::BudgetShape)
    );
    assert_eq!(
        validate(&with(
            &with(&base, "placements", list(vec![])),
            "performance_budgets",
            list(vec![bad_budget.clone()])
        )),
        Err(E::NestedShape)
    );
    // An unsorted budget is detached sorted.
    assert_eq!(
        validate(&with(
            &base,
            "performance_budgets",
            list(vec![reversed(&one_budget)])
        )),
        Ok(ok.clone())
    );

    // 6. Private content (:1043-1051): a mapping key anywhere, case and spacing exact.
    let base_batch = get(&base, "batch_state").clone();
    let private_batches = with(
        &base_batch,
        "observed_batches",
        list(vec![dict(vec![("prompt", s("x"))])]),
    );
    assert_eq!(
        validate(&with(&base, "batch_state", private_batches)),
        Err(E::PrivateContent)
    );
    assert_eq!(
        validate(&with(&base, "deployment_id", dict(vec![("secret", n(1))]))),
        Err(E::PrivateContent)
    );
    assert_eq!(
        validate(&with(
            &base,
            "topology_version",
            list(vec![dict(vec![("a", dict(vec![("kv_content", n(1))]))])])
        )),
        Err(E::PrivateContent)
    );
    let near_private = dict(vec![
        ("Prompt", n(1)),
        ("prompt ", n(2)),
        ("token_id", n(3)),
    ]);
    assert_eq!(
        validate(&with(&base, "deployment_id", near_private.clone())),
        Ok(with(&ok, "deployment_id", sorted(&near_private)))
    );
    assert_eq!(
        validate(&with(&base, "deployment_id", s("prompt"))),
        Ok(with(&ok, "deployment_id", s("prompt")))
    );
    assert_eq!(
        validate(&with(
            &with(&base, "deployment_id", dict(vec![("secret", n(1))])),
            "performance_budgets",
            list(vec![bad_budget])
        )),
        Err(E::BudgetShape)
    );

    // 7. Non-finite (:1054-1062).
    assert_eq!(
        validate(&with(&base, "generated_at_monotonic_s", x(f64::INFINITY))),
        Err(E::NonFinite)
    );
    assert_eq!(
        validate(&with(&base, "generated_at_monotonic_s", x(f64::NAN))),
        Err(E::NonFinite)
    );
    assert_eq!(
        validate(&with(&base, "generated_at_monotonic_s", x(f64::MAX))),
        Ok(with(&ok, "generated_at_monotonic_s", x(f64::MAX)))
    );
    assert_eq!(
        validate(&with(
            &base,
            "batch_state",
            with(
                &base_batch,
                "observed_batches",
                list(vec![list(vec![
                    n(1),
                    dict(vec![("x", x(f64::NEG_INFINITY))])
                ])])
            )
        )),
        Err(E::NonFinite)
    );
    assert_eq!(
        validate(&with(
            &base,
            "requests",
            list(vec![with(&one_request, "queue_wait_ms", x(f64::NAN))])
        )),
        Err(E::NonFinite)
    );
    assert_eq!(
        validate(&with(
            &with(&base, "generated_at_monotonic_s", x(f64::INFINITY)),
            "deployment_id",
            dict(vec![("tensor", n(1))])
        )),
        Err(E::PrivateContent)
    );

    // 8. Not JSON-safe (:1156-1159): json.dumps refuses an int past 4,300 digits.
    assert_eq!(
        validate(&with(&base, "deployment_epoch", pow10(4299, false))),
        Ok(with(&ok, "deployment_epoch", pow10(4299, false)))
    );
    assert_eq!(
        validate(&with(&base, "deployment_epoch", pow10(4300, false))),
        Err(E::NotJsonSafe)
    );
    assert_eq!(
        validate(&with(&base, "topology_version", pow10(4300, true))),
        Err(E::NotJsonSafe)
    );
    assert_eq!(
        validate(&with(
            &with(&base, "deployment_epoch", pow10(4300, false)),
            "generated_at_monotonic_s",
            x(f64::NAN)
        )),
        Err(E::NonFinite)
    );

    // 9. The round trip keeps int, float and bool apart, -0.0, wide ints, None, and sorts
    // keys by code point (a lone surrogate between U+00E9 and U+E000; astral last).
    let typed = list(vec![
        n(1),
        x(1.0),
        b(true),
        b(false),
        x(-0.0),
        x(0.0),
        big("18446744073709551617"),
        big("-9223372036854775809"),
        x(0.30000000000000004),
        x(5e-324),
        none(),
        s("s"),
        list(vec![]),
        dict(vec![]),
    ]);
    assert_eq!(
        validate(&with(
            &base,
            "batch_state",
            with(&base_batch, "observed_batches", typed.clone())
        )),
        Ok(with(
            &ok,
            "batch_state",
            with(get(&ok, "batch_state"), "observed_batches", typed)
        ))
    );
    let lone = PyStr::from_code_points([0xd800]).expect("a code point");
    let keyed = |order: &[usize]| {
        let keys: Vec<PyStr> = vec![
            PyStr::new("z"),
            PyStr::new("\u{e9}"),
            PyStr::new("\u{1f600}"),
            PyStr::new("\u{ffff}"),
            PyStr::new("A"),
            lone.clone(),
            PyStr::new(""),
            PyStr::new("\u{0}"),
            PyStr::new("\u{e000}"),
        ];
        PyJson::Dict(
            order
                .iter()
                .map(|k| (keys[*k].clone(), n(i64::try_from(*k).expect("small"))))
                .collect(),
        )
    };
    let unsorted = keyed(&[0, 1, 2, 3, 4, 5, 6, 7, 8]);
    let code_point_order = keyed(&[6, 7, 4, 0, 1, 5, 8, 3, 2]);
    assert_eq!(
        validate(&with(
            &base,
            "batch_state",
            with(&base_batch, "observed_batches", list(vec![unsorted]))
        )),
        Ok(with(
            &ok,
            "batch_state",
            with(
                get(&ok, "batch_state"),
                "observed_batches",
                list(vec![code_point_order])
            )
        ))
    );
    let surrogate_id = PyJson::Str(PyStr::from_code_points([0xdc00, 0xd800]).expect("code points"));
    assert_eq!(
        validate(&with(&base, "deployment_id", surrogate_id.clone())),
        Ok(with(&ok, "deployment_id", surrogate_id))
    );

    // Each refusal's Python exception (kind, str()) and its own code.
    let all = [
        (E::Shape, "value", "M16 runtime status shape is invalid"),
        (
            E::Protocol,
            "value",
            "M16 runtime status protocol is invalid",
        ),
        (
            E::QueueShape,
            "value",
            "M16 runtime status queue shape is invalid",
        ),
        (
            E::NestedShape,
            "value",
            "M16 runtime status nested shape is invalid",
        ),
        (
            E::BudgetShape,
            "value",
            "M16 runtime status budget shape is invalid",
        ),
        (
            E::PrivateContent,
            "value",
            "M16 runtime status contains private content",
        ),
        (
            E::NonFinite,
            "value",
            "M16 runtime status contains non-finite value",
        ),
        (
            E::NotJsonSafe,
            "value",
            "M16 runtime status is not JSON-safe",
        ),
        (
            E::BudgetIntTooLargeForFloat,
            "overflow",
            "int too large to convert to float",
        ),
    ];
    let mut codes = std::collections::BTreeSet::new();
    for (error, kind, message) in all {
        assert_eq!(
            (error.kind(), error.message()),
            (kind, message),
            "{error:?}"
        );
        assert!(codes.insert(error.code()), "{error:?} code repeated");
        assert_ne!(
            error.code(),
            "resource_unavailable",
            "not an admission code"
        );
    }
}

#[test]
fn m16_coordinator_status_performance_budgets() {
    let valid = budget("m16-concurrent-physical-v1");
    let validated = sorted(&valid);
    // The detached document: every dict's keys sorted, every value kept (:236-239).
    assert_eq!(
        validate_performance_budget_v3(&valid),
        Ok(validated.clone())
    );
    assert_eq!(
        validate_performance_budget_v3(&validated),
        Ok(validated.clone())
    );

    let rig = Rig::standard();
    let c = coordinator(&rig, 1);
    assert_eq!(c.performance_budgets(), vec![]);
    assert_eq!(
        c.status()
            .map(|doc| get(&doc, "performance_budgets").clone()),
        Ok(list(vec![]))
    );
    // attach replaces the list with the one validated document (:989-991); no clock read.
    rig.take();
    assert_eq!(c.attach_performance_budget(&valid), Ok(()));
    assert_eq!(rig.take(), (vec![], vec![]));
    assert_eq!(c.performance_budgets(), vec![validated.clone()]);
    assert_eq!(
        c.status()
            .map(|doc| get(&doc, "performance_budgets").clone()),
        Ok(list(vec![validated.clone()]))
    );
    // A refusal is the oracle's error and leaves the list unchanged.
    let refusals = [
        (
            with(&valid, "protocol", s("mycelium.performance_budget.v2")),
            BudgetError::Invalid,
            "performance_budget",
            "performance_budget_invalid".to_owned(),
        ),
        (
            with_dimension(&valid, 7, "dimension", list(vec![s("runtime_batch_size")])),
            BudgetError::Unhashable("list"),
            "type",
            "cannot use 'list' as a set element (unhashable type: 'list')".to_owned(),
        ),
        (
            with_dimension(&valid, 0, "state", dict(vec![])),
            BudgetError::Unhashable("dict"),
            "type",
            "cannot use 'dict' as a set element (unhashable type: 'dict')".to_owned(),
        ),
        (
            with_dimension(&valid, 2, "bound", pow10(400, false)),
            BudgetError::IntTooLargeForFloat,
            "overflow",
            "int too large to convert to float".to_owned(),
        ),
    ];
    for (doc, error, kind, message) in refusals {
        assert_eq!(c.attach_performance_budget(&doc), Err(error));
        assert_eq!((error.kind(), error.message()), (kind, message));
        assert_eq!(
            c.performance_budgets(),
            vec![validated.clone()],
            "{error:?}"
        );
    }
    assert_eq!(BudgetError::Invalid.code(), "performance_budget_invalid");
    // A second valid document replaces the first.
    let second = budget("m16-second-v1");
    assert_eq!(c.attach_performance_budget(&reversed(&second)), Ok(()));
    assert_eq!(c.performance_budgets(), vec![sorted(&second)]);
    let doc = c.status().expect("a status");
    assert_eq!(
        get(&doc, "performance_budgets"),
        &list(vec![sorted(&second)])
    );
    // At validation, at most 16 budgets (:1144-1145).
    let sixteen = with(&doc, "performance_budgets", repeat(&sorted(&second), 16));
    assert_eq!(validate_m16_runtime_status(&sixteen), Ok(sixteen.clone()));
    assert_eq!(
        validate_m16_runtime_status(&with(
            &doc,
            "performance_budgets",
            repeat(&sorted(&second), 17)
        )),
        Err(StatusValidationError::NestedShape)
    );

    // The v3 rules (mycelium_performance_budget.py:27-53, 192-234), each on both sides.
    let v3 = |doc: &PyJson| validate_performance_budget_v3(doc).map(|_| ());
    let top = |key: &str, value: PyJson| v3(&with(&valid, key, value));
    let dim = |k: usize, key: &str, value: PyJson| v3(&with_dimension(&valid, k, key, value));
    use BudgetError::{IntTooLargeForFloat, Invalid, Unhashable};
    assert_eq!(v3(&list(vec![valid.clone()])), Err(Invalid));
    assert_eq!(v3(&without(&valid, "overall_state")), Err(Invalid));
    assert_eq!(v3(&with(&valid, "prompt", s("x"))), Err(Invalid));
    assert_eq!(top("protocol", n(3)), Err(Invalid));
    assert_eq!(top("budget_id", s(&"a".repeat(128))), Ok(()));
    assert_eq!(top("budget_id", s(&"a".repeat(129))), Err(Invalid));
    assert_eq!(top("budget_id", s("a._:~-z")), Ok(()));
    for refused in [".a", "~a", "a b", "", "a\u{e9}", "a\n"] {
        assert_eq!(top("budget_id", s(refused)), Err(Invalid), "{refused:?}");
    }
    assert_eq!(top("budget_id", n(7)), Err(Invalid));
    assert_eq!(top("profile_id", s("a/b")), Err(Invalid));
    assert_eq!(
        top("evidence_scope", s("sequential_observed")),
        Err(Invalid)
    );
    assert_eq!(top("observed_request_count", n(1)), Ok(()));
    assert_eq!(
        top("observed_request_count", n(9_007_199_254_740_991)),
        Ok(())
    );
    for refused in [n(0), n(-3), n(9_007_199_254_740_992), b(true), x(3.0)] {
        assert_eq!(
            top("observed_request_count", refused.clone()),
            Err(Invalid),
            "{refused:?}"
        );
    }
    assert_eq!(top("dimensions", dict(vec![("a", n(1))])), Err(Invalid));
    let dims = items(get(&valid, "dimensions")).to_vec();
    assert_eq!(top("dimensions", list(dims[..7].to_vec())), Err(Invalid));
    let mut nine = dims.clone();
    nine.push(dims[0].clone());
    assert_eq!(top("dimensions", list(nine)), Err(Invalid));
    assert_eq!(
        dim(1, "dimension", s("admission_latency_p95_ms")),
        Err(Invalid)
    );
    assert_eq!(dim(1, "dimension", n(1)), Err(Invalid));
    assert_eq!(dim(3, "prompt", s("x")), Err(Invalid));
    // Order: the protocol before the TypeError of the names; a dimension's own shape first.
    assert_eq!(
        v3(&with(
            &with_dimension(&valid, 7, "dimension", list(vec![n(1)])),
            "protocol",
            s("x")
        )),
        Err(Invalid)
    );
    let mut shapeless = dims.clone();
    shapeless[0] = dict(vec![("dimension", list(vec![n(1)]))]);
    assert_eq!(top("dimensions", list(shapeless)), Err(Invalid));
    assert_eq!(dim(0, "state", s("Met")), Err(Invalid));
    assert_eq!(
        dim(0, "state", list(vec![s("met")])),
        Err(Unhashable("list"))
    );
    // `any` stops at the first bad dimension; within one, the state is hashed first.
    assert_eq!(
        v3(&with_dimension(
            &with_dimension(&valid, 0, "bound", x(-1.0)),
            1,
            "state",
            list(vec![])
        )),
        Err(Invalid)
    );
    assert_eq!(
        v3(&with_dimension(
            &with_dimension(&valid, 0, "bound", x(-1.0)),
            0,
            "state",
            list(vec![])
        )),
        Err(Unhashable("list"))
    );
    for accepted in [x(-0.0), n(0), x(5e-324), big(MAX_FINITE_INT)] {
        assert_eq!(dim(2, "bound", accepted.clone()), Ok(()), "{accepted:?}");
    }
    for refused in [
        x(-1.0),
        b(true),
        x(f64::NAN),
        x(f64::INFINITY),
        s("1"),
        none(),
    ] {
        assert_eq!(
            dim(2, "bound", refused.clone()),
            Err(Invalid),
            "{refused:?}"
        );
    }
    for overflow in [big(FIRST_OVERFLOW_INT), pow10(400, false), pow10(400, true)] {
        assert_eq!(dim(2, "bound", overflow), Err(IntTooLargeForFloat));
    }
    // observed is converted before the unit is checked.
    assert_eq!(
        v3(&with_dimension(
            &with_dimension(&valid, 5, "unit", s("a b")),
            5,
            "observed",
            pow10(309, false)
        )),
        Err(IntTooLargeForFloat)
    );
    assert_eq!(
        v3(&with_dimension(
            &with_dimension(&valid, 1, "reason", s("")),
            2,
            "bound",
            pow10(309, false)
        )),
        Err(Invalid)
    );
    assert_eq!(dim(4, "unit", s(&"u".repeat(128))), Ok(()));
    assert_eq!(dim(4, "unit", s(&"u".repeat(129))), Err(Invalid));
    for refused in [
        format!("sha256:{}", "0123456789ABCDEF".repeat(4)),
        format!("sha256:{}", "0".repeat(63)),
        format!("sha256:{}", "0".repeat(65)),
        format!("sha512:{}", "0".repeat(64)),
        format!("sha256:{}\n", "0".repeat(64)),
    ] {
        assert_eq!(
            dim(6, "evidence_digest", s(&refused)),
            Err(Invalid),
            "{refused}"
        );
    }
    assert_eq!(dim(0, "reason", s("")), Err(Invalid));
    assert_eq!(dim(0, "reason", n(1)), Err(Invalid));
    let surrogate_reason =
        PyJson::Str(PyStr::from_code_points([0x77, 0xd800, 0x1f600]).expect("code points"));
    assert_eq!(
        validate_performance_budget_v3(&with_dimension(
            &valid,
            0,
            "reason",
            surrogate_reason.clone()
        )),
        Ok(sorted(&with_dimension(
            &valid,
            0,
            "reason",
            surrogate_reason
        )))
    );
    // The overall state the dimension states imply (:221-229).
    let met = with(
        &with_dimension(
            &with_dimension(&valid, 7, "state", s("met")),
            7,
            "observed",
            x(2.0),
        ),
        "overall_state",
        s("met"),
    );
    assert_eq!(v3(&met), Ok(()));
    assert_eq!(
        v3(&with(
            &met,
            "overall_state",
            s("met_with_approved_exclusions")
        )),
        Err(Invalid)
    );
    let failed = with_dimension(&valid, 3, "state", s("failed"));
    assert_eq!(v3(&with(&failed, "overall_state", s("failed"))), Ok(()));
    assert_eq!(v3(&failed), Err(Invalid));
    assert_eq!(top("overall_state", s("met")), Err(Invalid));
    assert_eq!(top("overall_state", n(1)), Err(Invalid));
    // An approved-exclusion batch size observed == 1 (:230-232).
    assert_eq!(dim(7, "observed", n(1)), Ok(()));
    assert_eq!(dim(7, "observed", x(1.0)), Ok(()));
    for refused in [x(2.0), x(0.9999999999999999), n(0)] {
        assert_eq!(
            dim(7, "observed", refused.clone()),
            Err(Invalid),
            "{refused:?}"
        );
    }
}

// ---------------------------------------------------------------------------------------------
// Oracle replay.

const ORACLE: &str = include_str!("fixtures/m16-coordinator-status-oracle.json");

/// A Python value from the probe's `json.dumps(value, ensure_ascii=True, allow_nan=True,
/// separators=(",", ":"))` text: a literal with `.`, `e` or `E` is a float, any other number
/// an int of any size, `NaN` / `Infinity` / `-Infinity` floats, and `\uXXXX` escapes joined
/// into one code point only for a high surrogate directly followed by a low one (as CPython's
/// scanner decodes them).
struct PyText<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl PyText<'_> {
    fn parse(text: &str) -> PyJson {
        let mut parser = PyText {
            bytes: text.as_bytes(),
            at: 0,
        };
        let value = parser.value();
        assert_eq!(parser.at, parser.bytes.len(), "trailing text");
        value
    }

    fn peek(&self) -> u8 {
        self.bytes[self.at]
    }

    fn eat(&mut self, literal: &str) -> bool {
        let found = self.bytes[self.at..].starts_with(literal.as_bytes());
        if found {
            self.at += literal.len();
        }
        found
    }

    fn value(&mut self) -> PyJson {
        match self.peek() {
            b'{' => {
                self.at += 1;
                let mut entries = Vec::new();
                if !self.eat("}") {
                    loop {
                        let key = self.string();
                        assert!(self.eat(":"), "a colon at {}", self.at);
                        entries.push((key, self.value()));
                        if self.eat("}") {
                            break;
                        }
                        assert!(self.eat(","), "a comma at {}", self.at);
                    }
                }
                PyJson::Dict(entries)
            }
            b'[' => {
                self.at += 1;
                let mut entries = Vec::new();
                if !self.eat("]") {
                    loop {
                        entries.push(self.value());
                        if self.eat("]") {
                            break;
                        }
                        assert!(self.eat(","), "a comma at {}", self.at);
                    }
                }
                PyJson::List(entries)
            }
            b'"' => PyJson::Str(self.string()),
            _ if self.eat("null") => PyJson::None,
            _ if self.eat("true") => PyJson::Num(PyReal::Bool(true)),
            _ if self.eat("false") => PyJson::Num(PyReal::Bool(false)),
            _ if self.eat("NaN") => PyJson::Num(PyReal::Float(f64::NAN)),
            _ if self.eat("Infinity") => PyJson::Num(PyReal::Float(f64::INFINITY)),
            _ if self.eat("-Infinity") => PyJson::Num(PyReal::Float(f64::NEG_INFINITY)),
            _ => {
                let start = self.at;
                while self.at < self.bytes.len()
                    && matches!(self.peek(), b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E')
                {
                    self.at += 1;
                }
                let literal = std::str::from_utf8(&self.bytes[start..self.at]).expect("ascii");
                if literal.contains(['.', 'e', 'E']) {
                    PyJson::Num(PyReal::Float(literal.parse().expect("a float literal")))
                } else {
                    PyJson::Num(decimal(literal))
                }
            }
        }
    }

    fn hex4(&mut self) -> u32 {
        let digits = std::str::from_utf8(&self.bytes[self.at..self.at + 4]).expect("ascii");
        self.at += 4;
        u32::from_str_radix(digits, 16).expect("hex")
    }

    fn string(&mut self) -> PyStr {
        assert!(self.eat("\""), "a string at {}", self.at);
        let mut points = Vec::new();
        loop {
            let byte = self.peek();
            self.at += 1;
            match byte {
                b'"' => break,
                b'\\' => {
                    let escape = self.peek();
                    self.at += 1;
                    let point = match escape {
                        b'"' => 0x22,
                        b'\\' => 0x5c,
                        b'/' => 0x2f,
                        b'b' => 0x08,
                        b'f' => 0x0c,
                        b'n' => 0x0a,
                        b'r' => 0x0d,
                        b't' => 0x09,
                        b'u' => {
                            let high = self.hex4();
                            let next = self.bytes[self.at..].starts_with(b"\\u");
                            if (0xd800..0xdc00).contains(&high) && next {
                                let mark = self.at;
                                self.at += 2;
                                let low = self.hex4();
                                if (0xdc00..0xe000).contains(&low) {
                                    0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00)
                                } else {
                                    self.at = mark;
                                    high
                                }
                            } else {
                                high
                            }
                        }
                        other => panic!("escape {other}"),
                    };
                    points.push(point);
                }
                ascii => {
                    assert!(ascii < 0x80, "ensure_ascii text");
                    points.push(u32::from(ascii));
                }
            }
        }
        PyStr::from_code_points(points).expect("code points")
    }
}

/// The probe's bits-only tagged form of a Python value (floats by bits, strs as text or code
/// points, dicts as pairs in order).
fn tagged(v: &PyJson) -> Value {
    match v {
        PyJson::None => Value::Null,
        PyJson::Num(PyReal::Bool(flag)) => json!({"bool": flag}),
        PyJson::Num(PyReal::Int {
            negative,
            magnitude,
        }) => json!({"int": format!("{}{magnitude}", if *negative { "-" } else { "" })}),
        PyJson::Num(PyReal::Float(v)) => json!({"bits": format!("{:016x}", v.to_bits())}),
        PyJson::Str(text) => match text.to_text() {
            Some(text) => json!({ "str": text }),
            None => json!({ "cp": text.code_points() }),
        },
        PyJson::List(items) => json!({"list": items.iter().map(tagged).collect::<Vec<_>>()}),
        PyJson::Dict(entries) => json!({"dict": entries
            .iter()
            .map(|(k, item)| json!([tagged(&PyJson::Str(k.clone())), tagged(item)]))
            .collect::<Vec<_>>()}),
    }
}

/// The same value as a `serde_json::Value` for the core's canonical writer (`None` when a str
/// holds a surrogate): a float as an exponent literal that reads back as its double.
fn to_value(v: &PyJson) -> Option<Value> {
    Some(match v {
        PyJson::None => Value::Null,
        PyJson::Num(PyReal::Bool(flag)) => Value::Bool(*flag),
        PyJson::Num(PyReal::Int {
            negative,
            magnitude,
        }) => serde_json::from_str(&format!("{}{magnitude}", if *negative { "-" } else { "" }))
            .expect("an int literal"),
        PyJson::Num(PyReal::Float(v)) => {
            serde_json::from_str(&format!("{v:e}")).expect("a float literal")
        }
        PyJson::Str(text) => Value::String(text.to_text()?),
        PyJson::List(items) => Value::Array(items.iter().map(to_value).collect::<Option<_>>()?),
        PyJson::Dict(entries) => Value::Object(
            entries
                .iter()
                .map(|(k, item)| Some((k.to_text()?, to_value(item)?)))
                .collect::<Option<_>>()?,
        ),
    })
}

fn hex_sha256(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// A recorded document against the port's: exact text (types, order, bits, code points) when
/// kept, the bits-only tagged digest always, the canonical json.dumps digest through the
/// core's writer when encodable.
fn assert_document(label: &str, record: &Value, port: &PyJson) {
    if let Some(text) = record["text"].as_str() {
        assert_eq!(port, &PyText::parse(text), "{label}: document");
    }
    assert_eq!(
        hex_sha256(&serde_json::to_string(&tagged(port)).expect("tagged text")),
        record["tagged_sha256"],
        "{label}: tagged digest"
    );
    match record["canonical_sha256"].as_str() {
        Some(digest) => {
            let value = to_value(port).expect("no surrogate");
            assert_eq!(
                hex_sha256(&canonical_json(&value).expect("canonical")),
                digest,
                "{label}: canonical digest"
            );
        }
        None => assert!(to_value(port).is_none(), "{label}: encodable"),
    }
    let summary = &record["summary"];
    let first_last = |list: &str, key: &str, count: &str, first: &str, last: &str| {
        if let Some(n) = summary[count].as_u64() {
            let entries = items(get(port, list));
            assert_eq!(entries.len() as u64, n, "{label}: {count}");
            let id = |r: Option<&PyJson>| r.map_or(Value::Null, |r| tagged(get(r, key)));
            assert_eq!(id(entries.first()), summary[first], "{label}: {first}");
            assert_eq!(id(entries.last()), summary[last], "{label}: {last}");
        }
    };
    first_last(
        "requests",
        "request_id",
        "request_count",
        "first_request_id",
        "last_request_id",
    );
    first_last(
        "incidents",
        "incident_id",
        "incident_count",
        "first_incident_id",
        "last_incident_id",
    );
    if let Some(recorded) = summary.get("active_request_ids") {
        let active: Vec<Value> = items(get(get(port, "queue"), "active_request_ids"))
            .iter()
            .map(tagged)
            .collect();
        assert_eq!(&Value::Array(active), recorded, "{label}: active ids");
    }
    if let Some(n) = summary["budget_count"].as_u64() {
        assert_eq!(
            items(get(port, "performance_budgets")).len() as u64,
            n,
            "{label}: budgets"
        );
    }
}

/// The Python exception the oracle raised: (type, message, code).
fn assert_python_error(label: &str, recorded: &Value, kind: &str, message: &str) {
    assert_eq!(recorded["type"], kind, "{label}");
    let text = recorded["message"].as_str().expect("a message");
    assert!(text.starts_with(message), "{label}: {text} vs {message}");
}

fn assert_status_error(label: &str, recorded: &Value, error: &StatusError) {
    match error {
        StatusError::Validation(e) => {
            let kind = if e.kind() == "overflow" {
                "OverflowError"
            } else {
                "ValueError"
            };
            assert_python_error(label, recorded, kind, e.message());
            assert_eq!(recorded["message"], e.message(), "{label}");
        }
        StatusError::Ledger(LedgerError("m16_ledger_unorderable_placement_ids")) => {
            assert_python_error(
                label,
                recorded,
                "TypeError",
                "'<' not supported between instances of",
            )
        }
        StatusError::Python(code) if *code == STATUS_INT_TOO_LARGE_FOR_FLOAT => {
            assert_python_error(
                label,
                recorded,
                "OverflowError",
                "int too large to convert to float",
            )
        }
        other => panic!("{label}: no oracle counterpart in these probes: {other:?}"),
    }
}

fn assert_budget_error(label: &str, recorded: &Value, error: &BudgetError) {
    let kind = match error {
        BudgetError::Invalid => "PerformanceBudgetError",
        BudgetError::Unhashable(_) => "TypeError",
        BudgetError::IntTooLargeForFloat => "OverflowError",
    };
    assert_eq!(recorded["type"], kind, "{label}");
    assert_eq!(recorded["message"], error.message(), "{label}");
    if let BudgetError::Invalid = error {
        assert_eq!(recorded["code"], error.code(), "{label}");
    }
}

fn recorded_f64(tagged: &Value) -> f64 {
    let bits = tagged["bits"].as_str().expect("float bits");
    f64::from_bits(u64::from_str_radix(bits, 16).expect("hex bits"))
}

/// A tagged Python number (the probe's `tag`, or `{"pow10": n, "negative": b}`).
fn recorded(tagged: &Value) -> PyReal {
    if let Some(flag) = tagged.get("bool").and_then(Value::as_bool) {
        return PyReal::Bool(flag);
    }
    if let Some(text) = tagged.get("int").and_then(Value::as_str) {
        return decimal(text);
    }
    PyReal::Float(recorded_f64(tagged))
}

fn recorded_number(tagged: &Value) -> PyNumber {
    match recorded(tagged) {
        PyReal::Int {
            negative: false,
            magnitude,
        } => PyNumber::Int(magnitude),
        PyReal::Float(x) => PyNumber::Float(x),
        other => panic!("not a ledger number: {other:?}"),
    }
}

fn config_from(overrides: &Value) -> RouterConfig {
    let mut config = factory_config();
    for (field, value) in overrides.as_object().expect("overrides") {
        let value = recorded(value);
        match field.as_str() {
            "reservation_lease_seconds" => config.reservation_lease_seconds = value,
            "maximum_pending_hops" => config.maximum_pending_hops = value,
            "maximum_pending_bytes" => config.maximum_pending_bytes = value,
            "backpressure_retry_after_seconds" => config.backpressure_retry_after_seconds = value,
            "maximum_runtime_batch_size" => config.maximum_runtime_batch_size = value,
            other => panic!("unmodelled override {other}"),
        }
    }
    config
}

/// The case's rig and coordinator.
fn construct(setup: &Value) -> (Rig, M16RuntimeCoordinator) {
    let graph: Value =
        serde_json::from_str(setup["graph_text"].as_str().expect("text")).expect("a graph text");
    let graph = execution_graph::parse(&graph).expect("a graph");
    let clock = &setup["clock"];
    let lease = (!setup["lease_clock"].is_null()).then(|| {
        (
            recorded_f64(&setup["lease_clock"]["base"]),
            recorded_f64(&setup["lease_clock"]["step"]),
        )
    });
    let rig = Rig::new(
        recorded_f64(&clock["base"]),
        recorded_f64(&clock["step"]),
        lease,
    );
    let caps = setup["capacities"]
        .as_array()
        .expect("capacities")
        .iter()
        .map(|pair| {
            let raw = &pair[1];
            PlacementCapacity {
                placement_id: pair[0].clone(),
                memory_capacity_bytes: recorded_number(&raw["memory_capacity_bytes"]),
                kv_capacity_bytes: recorded_number(&raw["kv_capacity_bytes"]),
                workspace_capacity_bytes: recorded_number(&raw["workspace_capacity_bytes"]),
                maximum_reservations: raw["maximum_reservations"]["int"]
                    .as_str()
                    .expect("an int")
                    .parse()
                    .expect("a count"),
            }
        })
        .collect();
    let mut inputs = rig.inputs(graph, caps, config_from(&setup["config"]));
    inputs.max_concurrent_requests = (!setup["max_concurrent_requests"].is_null())
        .then(|| recorded(&setup["max_concurrent_requests"]));
    let c = M16RuntimeCoordinator::new(inputs).expect("every probe's coordinator is built");
    (rig, c)
}

fn request_from(spec: &Value) -> RequestContext {
    let text = |k: &str| spec[k].as_str().expect("a str").to_owned();
    RequestContext {
        request_id: text("request_id"),
        prompt_token_ids: spec["prompt_token_ids"]
            .as_array()
            .expect("tokens")
            .iter()
            .map(recorded)
            .collect(),
        max_new_tokens: recorded(&spec["max_new_tokens"]),
        expected_new_tokens: recorded(&spec["expected_new_tokens"]),
        qos_class: text("qos_class"),
        admitted_at: recorded(&spec["admitted_at"]),
        target_ttft_ms: recorded(&spec["target_ttft_ms"]),
        target_tpot_ms: recorded(&spec["target_tpot_ms"]),
        target_tokens_per_second: recorded(&spec["target_tokens_per_second"]),
        sampling_seed: recorded(&spec["sampling_seed"]),
        generation_config_digest: text("generation_config_digest"),
    }
}

/// The probe's `request_spec(request_id)` defaults.
fn default_request(id: &str) -> RequestContext {
    request(id, "interactive")
}

fn port_reads(rig: &Rig) -> (Vec<Value>, Vec<String>) {
    let (reads, drawn) = rig.take();
    (
        reads
            .into_iter()
            .map(|(name, bits)| json!([name, format!("{bits:016x}")]))
            .collect(),
        drawn,
    )
}

/// One op against the coordinator, checked against its record; returns whether it was a
/// status op.
fn replay_op(
    label: &str,
    rig: &Rig,
    c: &M16RuntimeCoordinator,
    op: &Value,
    record: &Value,
) -> bool {
    let id = || op["request_id"].as_str().expect("request id");
    let admission_error = |error: &CoordinatorError| match error {
        CoordinatorError::Admission(e) => {
            assert_python_error(label, &record["error"], "M16AdmissionError", e.code);
            assert_eq!(record["error"]["code"], e.code, "{label}");
            let retry = e
                .retry_after_seconds
                .as_ref()
                .map_or(Value::Null, |r| tagged(&PyJson::Num(r.clone())));
            let recorded_retry = match &record["error"]["retry_after_seconds"] {
                Value::Null => Value::Null,
                t => tagged(&PyJson::Num(recorded(t))),
            };
            assert_eq!(retry, recorded_retry, "{label}");
        }
        other => panic!("{label}: no oracle counterpart in these probes: {other:?}"),
    };
    let mut status = false;
    match op["op"].as_str().expect("op") {
        "admit" => {
            let excluded = op["excluded"].as_array().expect("excluded").clone();
            match c.admit(
                &request_from(&op["request"]),
                op["profile"].as_str().expect("profile"),
                &excluded,
            ) {
                Ok(_) => assert!(record["result"].is_null(), "{label}"),
                Err(error) => admission_error(&error),
            }
        }
        "next_dispatch" => assert_eq!(
            c.next_dispatch()
                .expect("dispatch")
                .map_or(Value::Null, Value::String),
            record["result"],
            "{label}"
        ),
        "mark_phase" => c
            .mark_phase(id(), op["phase"].as_str().expect("phase"))
            .expect("a phase"),
        "complete" => c
            .complete(id(), op["state"].as_str().expect("state"))
            .expect("completed"),
        "retire" => c.retire_dispatch_slot(id()),
        "cancel" => assert_eq!(Value::Bool(c.cancel(id())), record["result"], "{label}"),
        "set_clock" => {
            let value = recorded_f64(&op["value"]);
            match op["clock"].as_str().expect("clock") {
                "clock" => rig.clock.set(value),
                _ => rig.lease.as_ref().expect("a lease clock").set(value),
            }
        }
        "status" => {
            status = true;
            match c.status() {
                Ok(doc) => assert_document(label, &record["result"], &doc),
                Err(error) => assert_status_error(label, &record["error"], &error),
            }
        }
        "attach" => {
            let document = PyText::parse(op["document_text"].as_str().expect("a document"));
            match c.attach_performance_budget(&document) {
                Ok(()) => assert!(record["result"].is_null(), "{label}"),
                Err(error) => assert_budget_error(label, &record["error"], &error),
            }
        }
        "bulk" => {
            let prefix = op["prefix"].as_str().expect("prefix");
            let start = op["start"].as_u64().expect("start");
            let count = op["count"].as_u64().expect("count");
            let mut errors = 0u64;
            for k in start..start + count {
                let request = default_request(&format!("{prefix}-{k:05}"));
                if op["mode"] == "reject" {
                    if c.admit(&request, "interactive_chat_v1", &[json!("p1")])
                        .is_err()
                    {
                        errors += 1;
                    }
                } else {
                    let served = c
                        .admit(&request, "interactive_chat_v1", &[])
                        .and_then(|_| c.next_dispatch())
                        .and_then(|dispatched| match dispatched {
                            Some(id) => c.complete(&id, "completed"),
                            None => Err(CoordinatorError::Value("m16_completion_invalid")),
                        });
                    if served.is_err() {
                        errors += 1;
                    }
                }
            }
            assert_eq!(json!({"errors": errors}), record["result"], "{label}");
            let (reads, drawn) = port_reads(rig);
            assert_eq!(reads.len() as u64, record["read_count"], "{label}");
            assert_eq!(
                hex_sha256(&serde_json::to_string(&reads).expect("reads")),
                record["reads_sha256"],
                "{label}"
            );
            assert_eq!(drawn.len() as u64, record["id_count"], "{label}");
            assert_eq!(
                hex_sha256(&serde_json::to_string(&drawn).expect("ids")),
                record["ids_sha256"],
                "{label}"
            );
            return false;
        }
        other => panic!("unmodelled op {other}"),
    }
    let (reads, drawn) = port_reads(rig);
    assert_eq!(Value::Array(reads), record["reads"], "{label}: reads");
    assert_eq!(Value::from(drawn), record["ids"], "{label}: ids");
    status
}

/// A validation probe's input: its text, or the base document with one list field holding
/// `count` copies of its first element.
fn validation_input(base: &PyJson, input: &Value) -> PyJson {
    if let Some(text) = input["text"].as_str() {
        return PyText::parse(text);
    }
    let spec = &input["repeat"];
    let field = spec["field"].as_str().expect("a field");
    let count = usize::try_from(spec["count"].as_u64().expect("a count")).expect("small");
    let first = items(get(base, field))[0].clone();
    with(base, field, repeat(&first, count))
}

#[test]
fn m16_coordinator_status_python_value_domain_parity() {
    let oracle: Value = serde_json::from_str(ORACLE).expect("the fixture");
    let provenance = &oracle["provenance"];
    assert_eq!(
        provenance["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    assert_eq!(provenance["oracle_tree_clean"], true);
    let cases = oracle["cases"].as_array().expect("cases");
    let (mut ops, mut statuses) = (0u64, 0u64);
    for case in cases {
        let name = case["name"].as_str().expect("name");
        let (rig, c) = construct(&case["setup"]);
        assert_eq!(
            c.graph_digest(),
            case["construct"]["graph_digest"],
            "{name}"
        );
        let (reads, drawn) = port_reads(&rig);
        assert_eq!(Value::Array(reads), case["construct_reads"], "{name}");
        assert_eq!(Value::from(drawn), case["construct_ids"], "{name}");
        let results = case["results"].as_array().expect("results");
        let case_ops = case["ops"].as_array().expect("ops");
        assert_eq!(case_ops.len(), results.len(), "{name}");
        for (k, (op, record)) in case_ops.iter().zip(results).enumerate() {
            let label = format!("{name} op {k} {}", op["op"]);
            if replay_op(&label, &rig, &c, op, record) {
                statuses += 1;
            }
            ops += 1;
        }
    }
    let base = PyText::parse(
        oracle["validation_base_text"]
            .as_str()
            .expect("the base document"),
    );
    let validations = oracle["validations"].as_array().expect("validations");
    for probe in validations {
        let label = format!("validation {}", probe["name"]);
        let input = validation_input(&base, &probe["input"]);
        match validate_m16_runtime_status(&input) {
            Ok(doc) => assert_document(&label, &probe["result"], &doc),
            Err(error) => {
                assert_status_error(&label, &probe["error"], &StatusError::Validation(error))
            }
        }
    }
    let budgets = oracle["budgets"].as_array().expect("budgets");
    for probe in budgets {
        let label = format!("budget {}", probe["name"]);
        let input = PyText::parse(probe["input"]["text"].as_str().expect("a text"));
        match validate_performance_budget_v3(&input) {
            Ok(doc) => assert_document(&label, &probe["result"], &doc),
            Err(error) => assert_budget_error(&label, &probe["error"], &error),
        }
    }
    assert_eq!(
        cases.len() as u64,
        provenance["case_count"].as_u64().expect("count")
    );
    assert_eq!(ops, provenance["op_count"].as_u64().expect("count"));
    assert_eq!(
        statuses,
        provenance["status_op_count"].as_u64().expect("count")
    );
    assert_eq!(
        validations.len() as u64,
        provenance["validation_count"].as_u64().expect("count")
    );
    assert_eq!(
        budgets.len() as u64,
        provenance["budget_count"].as_u64().expect("count")
    );
    assert_eq!(
        (cases.len(), ops, statuses, validations.len(), budgets.len()),
        (30, 145, 57, 59, 85)
    );
}
