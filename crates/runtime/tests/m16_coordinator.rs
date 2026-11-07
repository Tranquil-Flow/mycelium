// SPDX-License-Identifier: AGPL-3.0-only
//! M16 runtime coordinator core (CL-L1-07): source parity for `M16AdmissionError`,
//! `path_manifest_document_digest` and `M16RuntimeCoordinator` construction, `admit`,
//! `next_dispatch`, `mark_phase`, `complete`, `retire_dispatch_slot`, `cancel`, the accessors
//! and the incidents (mycelium_m16_runtime.py:75-138, 348-707, 1020-1041 @ f4e366e9), composed
//! over the real merged ledger, route scorer, path builder and hop scheduler and a graph parsed
//! by `execution_graph::parse`. The first seven tests' expected values are derived by hand from
//! the oracle lines cited beside them (the three digests and the graph digest are the oracle's
//! recorded outputs, cited by fixture case; each hand-written canonical text is checked against
//! them with an independent SHA-256); none is computed by the coordinator. The clock is a
//! scripted clock returning base, base + step, ... and logging every read; the id source counts
//! `prefix-n` across prefixes and logs every id. The last test replays paired probes recorded
//! from the real oracle coordinator (fixtures/m16-coordinator-oracle.json).
use protocol::execution_graph::{self, Graph};
use runtime::m16_capacity::{PlacementCapacity, PyNumber};
use runtime::m16_coordinator::{
    CoordinatorError, CoordinatorInputs, DigestError, Incident, M16AdmissionError,
    M16RuntimeCoordinator, RequestRecord, RouteIdentity, canonical_json, digest_document,
    execution_graph_digest, path_manifest_document_digest, path_manifest_to_dict,
};
use runtime::m16_hop_scheduler::SchedulerError;
use runtime::m16_ledger::{IdSource, LeaseClock, LedgerError, PlacementStatus};
use runtime::m16_path_builder::PathBuilderError;
use runtime::m16_router_contracts::{
    DeviceState, HopPayload, HopWorkItem, IdMap, PATH_MANIFEST_PROTOCOL, PathHop, PathManifest,
    PyJson, PyReal, RequestContext, RouterConfig,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

const INF: f64 = f64::INFINITY;

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

type Reads = Arc<Mutex<Vec<(&'static str, u64)>>>;

/// `now()` returns base, base + step, ... (one float addition per read) and logs each read
/// (its name and the value's bits).
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

    /// The default rig: clock 100.0 stepping 0.25, no separate lease clock.
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

fn placement(k: usize, pid: &str, node: &str) -> Value {
    json!({
        "placement_id": pid, "node_id": node, "replica_group_id": format!("group-{k}"),
        "assignment_id": format!("assignment-{k}-0"), "stage_signature": format!("signature-{k}"),
        "load_proof_digest": format!("proof-{k}-0"), "runtime_backend": "numpy",
        "runtime_endpoint": format!("local://{k}-0"),
    })
}

fn stage(k: usize, kv: u64, placements: Vec<Value>) -> Value {
    json!({
        "stage_id": format!("stage-{k}"),
        "range": {"start_layer": k, "end_layer_exclusive": k + 1, "layer_count": 1},
        "component_roles": ["decoder"],
        "stage_cost": {"prefill_work_units_per_prompt_token": 2, "decode_work_units_per_token": 1,
                       "kv_bytes_per_context_token": kv},
        "placements": placements,
    })
}

fn edge(id: &str, from: &str, to: &str) -> Value {
    json!({"edge_id": id, "from_placement_id": from, "to_placement_id": to,
           "link_id": format!("link-{id}")})
}

/// stage-0 [p0 on node-a, KV 0 per token], stage-1 [p1 on node-b, KV 10 per token], edge-0
/// p0->p1, loop-0 p1->p0: the probe's `graph_text(LINEAR)`. A request of 4 prompt and 8 new
/// tokens reserves 0 KV bytes on p0 and (4 + 8) * 10 = 120 on p1, each with activation_bytes * 2
/// = 4 workspace bytes (mycelium_m16_runtime.py:227-228).
fn linear_doc() -> Value {
    json!({
        "protocol": "mycelium.execution_graph.v1",
        "deployment_id": "deployment-m16", "deployment_epoch": 1, "topology_version": 1,
        "model_id": "model-m16", "resolved_commit": "commit-m16", "manifest_digest": "digest-m16",
        "entry_stage_id": "stage-0", "final_stage_id": "stage-1",
        "hidden_size": 8, "activation_bytes": 2, "token_envelope_bytes": 16,
        "stages": [stage(0, 0, vec![placement(0, "p0", "node-a")]),
                   stage(1, 10, vec![placement(1, "p1", "node-b")])],
        "edges": [edge("edge-0", "p0", "p1")],
        "loopback_edges": [edge("loop-0", "p1", "p0")],
    })
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

fn capacity(id: Value, kv: u64) -> PlacementCapacity {
    let plenty = 1_000_000_000_000;
    PlacementCapacity {
        placement_id: id,
        memory_capacity_bytes: PyNumber::from(plenty),
        kv_capacity_bytes: PyNumber::from(kv),
        workspace_capacity_bytes: PyNumber::from(plenty),
        maximum_reservations: 64,
    }
}

fn caps() -> Vec<PlacementCapacity> {
    let plenty = 1_000_000_000_000;
    vec![capacity(json!("p0"), plenty), capacity(json!("p1"), plenty)]
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

/// `RouterConfig(reservation_lease_seconds=3_600.0)` (mycelium_m16_runtime.py:1260).
fn factory_config() -> RouterConfig {
    let mut config = RouterConfig::oracle_default().expect("oracle defaults");
    config.reservation_lease_seconds = f(3600.0);
    config
}

/// The gateway's request shape (mycelium_request_gateway/backend.py:227-239).
fn request_with(id: &str, qos: &str, tokens: Vec<PyReal>, max_new: i64) -> RequestContext {
    RequestContext {
        request_id: id.to_owned(),
        prompt_token_ids: tokens,
        max_new_tokens: i(max_new),
        expected_new_tokens: i(max_new),
        qos_class: qos.to_owned(),
        admitted_at: f(0.0),
        target_ttft_ms: f(1000.0),
        target_tpot_ms: f(100.0),
        target_tokens_per_second: f(10.0),
        sampling_seed: i(0),
        generation_config_digest: "sha256:test".to_owned(),
    }
}

fn request(id: &str, qos: &str) -> RequestContext {
    request_with(id, qos, (1..=4).map(i).collect(), 8)
}

fn coordinator(rig: &Rig, config: RouterConfig, max: Option<PyReal>) -> M16RuntimeCoordinator {
    let mut inputs = rig.inputs(parse(&linear_doc()), caps(), config);
    inputs.max_concurrent_requests = max;
    M16RuntimeCoordinator::new(inputs).expect("a coordinator")
}

fn adm(code: &'static str) -> CoordinatorError {
    CoordinatorError::Admission(M16AdmissionError {
        code,
        retry_after_seconds: None,
    })
}

fn not_admitted() -> M16AdmissionError {
    M16AdmissionError {
        code: "request_not_admitted",
        retry_after_seconds: None,
    }
}

fn hop(stage: &str, placement: &str, reservation: &str, expires: f64, epoch: Value) -> PathHop {
    PathHop {
        stage_id: json!(stage),
        placement_id: json!(placement),
        reservation_id: reservation.to_owned(),
        reservation_expires_at: expires,
        reservation_epoch: epoch,
    }
}

/// The linear graph's manifest: `path`, hops reserving `reservation-{r0}` and
/// `reservation-{r1}` with these expiries.
fn manifest(request: &str, path: &str, hops: [(&str, f64); 2]) -> PathManifest {
    PathManifest {
        path_id: path.to_owned(),
        path_attempt: 0,
        request_id: request.to_owned(),
        deployment_id: json!("deployment-m16"),
        deployment_epoch: json!(1),
        topology_version: json!(1),
        manifest_digest: json!("digest-m16"),
        ordered_hops: vec![
            hop("stage-0", "p0", hops[0].0, hops[0].1, json!(1)),
            hop("stage-1", "p1", hops[1].0, hops[1].1, json!(1)),
        ],
        loopback_edge_id: json!("loop-0"),
        protocol: PATH_MANIFEST_PROTOCOL.to_owned(),
    }
}

/// `sha256:` + hex, computed independently of the coordinator.
fn sha256(text: &str) -> String {
    let digest = Sha256::digest(text.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("sha256:{hex}")
}

/// Live reservations: (id, placement, expiry bits, committed).
fn live(c: &M16RuntimeCoordinator, request_id: &str) -> Vec<(String, Value, u64, bool)> {
    c.request_reservations(request_id)
        .into_iter()
        .map(|r| {
            (
                r.reservation_id,
                r.placement_id,
                r.expires_at.to_bits(),
                r.committed,
            )
        })
        .collect()
}

/// Per placement: (id, reserved KV, reserved memory, active reservations).
fn placements(c: &M16RuntimeCoordinator) -> Vec<(Value, PyNumber, PyNumber, u64)> {
    c.placement_status()
        .expect("placement status")
        .into_iter()
        .map(|s: PlacementStatus| {
            (
                s.placement_id,
                s.reserved_kv_bytes,
                s.reserved_memory_bytes,
                s.active_reservations,
            )
        })
        .collect()
}

fn reserved(p0: (u64, u64), p1: (u64, u64)) -> Vec<(Value, PyNumber, PyNumber, u64)> {
    let row =
        |id: &str, n: u64, kv: u64| (json!(id), PyNumber::from(kv), PyNumber::from(kv + 4 * n), n);
    vec![row("p0", p0.0, p0.1), row("p1", p1.0, p1.1)]
}

fn incident(
    n: u64,
    kind: &'static str,
    request_id: &str,
    scope: &'static str,
    state: &'static str,
    at: f64,
    retry: Option<PyReal>,
) -> Incident {
    Incident {
        incident_id: format!("m16-incident-{n}"),
        kind,
        request_id: request_id.to_owned(),
        scope,
        state,
        observed_at_monotonic_s: at,
        retry_after_seconds: retry,
    }
}

/// The record :482-496 builds, with the lifecycle fields given.
#[allow(clippy::too_many_arguments)]
fn record(
    request: RequestContext,
    profile: &str,
    manifest: PathManifest,
    digest: &str,
    phase: &str,
    queued_at: f64,
    dispatch_at: Option<f64>,
    terminal: Option<(f64, &str)>,
) -> RequestRecord {
    RequestRecord {
        request,
        request_attempt: 1,
        workload_profile_id: profile.to_owned(),
        manifest,
        path_manifest_digest: digest.to_owned(),
        recovery_source_fenced: false,
        recovery_attempts: 0,
        phase: phase.to_owned(),
        queued_at,
        dispatch_at,
        terminal_at: terminal.map(|(at, _)| at),
        terminal_state: terminal.map(|(_, s)| s.to_owned()),
    }
}

/// Recorded by the probe (case construct_default): `_digest(execution_graph_to_dict(graph))`
/// of [`linear_doc`].
const LINEAR_GRAPH_DIGEST: &str =
    "sha256:c127d544ac21f5ec2edb32a2c22681e15d8f576aed4f1f8089b9c3b4b44b68cd";
/// Recorded by the probe (case flow_default_lease_clock): the digest of r1's path.
const R1_PATH_DIGEST: &str =
    "sha256:c4d07c89c64da519eb1978dae218cd6723aacddf9240928ac3617017e8403bc9";
/// r1's path document as `json.dumps(sort_keys=True, separators=(",", ":"))` writes it
/// (serialization.py:229-245): keys in code-point order, the expiries as float reprs.
const R1_PATH_TEXT: &str = concat!(
    r#"{"deployment_epoch":1,"deployment_id":"deployment-m16","loopback_edge_id":"loop-0","#,
    r#""manifest_digest":"digest-m16","ordered_hops":[{"placement_id":"p0","reservation_epoch":1,"#,
    r#""reservation_expires_at":3700.25,"reservation_id":"reservation-2","stage_id":"stage-0"},"#,
    r#"{"placement_id":"p1","reservation_epoch":1,"reservation_expires_at":3700.75,"#,
    r#""reservation_id":"reservation-3","stage_id":"stage-1"}],"path_attempt":0,"path_id":"path-1","#,
    r#""protocol":"mycelium.path_manifest.v1","request_id":"r1","topology_version":1}"#
);

/// The admit of one request on a fresh standard rig, whose clock is the lease clock: advance
/// reads (clock, lease) per stage (:430-434), lock reads (clock, lease) (:437-441), the ledger's
/// commit reads its (lease) clock once per reservation (:274-283), and the queue time one more
/// (:451): nine reads from `base`.
fn admit_reads(base: f64) -> Vec<(&'static str, u64)> {
    seq("clock", base, 0.25, 9)
}

#[test]
fn m16_coordinator_construction() {
    fn send_sync<T: Send + Sync>() {}
    send_sync::<M16RuntimeCoordinator>();
    let build = |rig: &Rig, f: &dyn Fn(&mut CoordinatorInputs)| {
        let mut inputs = rig.inputs(parse(&linear_doc()), caps(), factory_config());
        f(&mut inputs);
        M16RuntimeCoordinator::new(inputs).map(|c| c.maximum_concurrent_requests())
    };
    let rig = Rig::standard();
    // :364-368 the profiles: empty, or a value that is not exactly "interactive" or "batch".
    let profiles_invalid = Err(CoordinatorError::Value("m16_workload_profiles_invalid"));
    for bad in [
        vec![],
        vec![("p", "Interactive")],
        vec![("a", "interactive"), ("b", "bogus")],
        vec![("a", "")],
    ] {
        let map: BTreeMap<String, String> = bad
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v.to_owned()))
            .collect();
        assert_eq!(
            build(&rig, &|inputs| inputs.workload_profiles = map.clone()),
            profiles_invalid
        );
    }
    let only_batch: BTreeMap<String, String> =
        [("b".to_owned(), "batch".to_owned())].into_iter().collect();
    assert_eq!(
        build(&rig, &|inputs| inputs.workload_profiles =
            only_batch.clone()),
        Ok(1)
    );
    // :375-380 an int, bool excluded, in 1..=64; the default is 1 (:362).
    let max_invalid = Err(CoordinatorError::Value("invalid_max_concurrent_requests"));
    for bad in [
        i(0),
        i(65),
        i(-1),
        PyReal::Bool(true),
        PyReal::Bool(false),
        f(1.0),
        f(64.0),
        big(20),
        PyReal::signed(false, "18446744073709551616".parse().expect("digits")),
    ] {
        assert_eq!(
            build(&rig, &|inputs| inputs.max_concurrent_requests =
                Some(bad.clone())),
            max_invalid,
            "{bad:?}"
        );
    }
    for (good, expected) in [
        (Some(i(1)), 1),
        (Some(i(64)), 64),
        (Some(i(4)), 4),
        (None, 1),
    ] {
        assert_eq!(
            build(&rig, &|inputs| inputs.max_concurrent_requests =
                good.clone()),
            Ok(expected)
        );
    }
    // The order: profiles, then the concurrency, then the ledger (:383-388; its own
    // ValueError codes, unchanged), then the graph digest last (:399).
    let one_cap = || vec![capacity(json!("p0"), 1)];
    assert_eq!(
        build(&rig, &|inputs| {
            inputs.workload_profiles = BTreeMap::new();
            inputs.max_concurrent_requests = Some(i(0));
        }),
        profiles_invalid
    );
    assert_eq!(
        build(&rig, &|inputs| {
            inputs.max_concurrent_requests = Some(i(0));
            inputs.placement_capacities = one_cap();
        }),
        max_invalid
    );
    assert_eq!(
        build(&rig, &|inputs| inputs.placement_capacities = one_cap()),
        Err(CoordinatorError::Ledger(LedgerError(
            "m16_capacity_placement_mismatch"
        )))
    );
    assert_eq!(
        build(&rig, &|inputs| inputs.placement_capacities =
            vec![capacity(json!("p0"), 0), capacity(json!("p1"), 1)]),
        Err(CoordinatorError::Ledger(LedgerError(
            "m16_capacity_invalid"
        )))
    );
    // json.loads reads 1e400 as inf; `_digest` refuses it (allow_nan=False) after every
    // other check passed (a placement's runtime_backend, which no graph rule reads).
    let mut infinite = linear_doc();
    infinite["stages"][0]["placements"][0]["runtime_backend"] =
        serde_json::from_str("1e400").expect("a JSON number");
    let digest_refused = |caps: Vec<PlacementCapacity>| {
        M16RuntimeCoordinator::new(rig.inputs(parse(&infinite), caps, factory_config()))
            .map(|c| c.graph_digest())
    };
    assert_eq!(
        digest_refused(caps()),
        Err(CoordinatorError::Digest(DigestError::OutOfRangeFloat))
    );
    assert_eq!(
        digest_refused(one_cap()),
        Err(CoordinatorError::Ledger(LedgerError(
            "m16_capacity_placement_mismatch"
        )))
    );
    // Construction reads no clock and draws no id.
    assert_eq!(rig.take(), (vec![], vec![]));
    // The graph digest is the oracle's for the same graph (case construct_default).
    let c = coordinator(&rig, factory_config(), None);
    assert_eq!(c.graph_digest(), LINEAR_GRAPH_DIGEST);
    assert_eq!(
        execution_graph_digest(&parse(&linear_doc())),
        Ok(LINEAR_GRAPH_DIGEST.to_owned())
    );
    // :373 lease_clock=None is the very same clock: the ledger, builder and coordinator read
    // the one clock, so the first hop expires at the second read plus 3600 (:208-210).
    c.admit(&request("r1", "interactive"), "interactive_chat_v1", &[])
        .expect("admitted");
    assert_eq!(rig.take().0, admit_reads(100.0));
    assert_eq!(
        live(&c, "r1"),
        vec![
            (
                "reservation-2".to_owned(),
                json!("p0"),
                3700.25f64.to_bits(),
                true
            ),
            (
                "reservation-3".to_owned(),
                json!("p1"),
                3700.75f64.to_bits(),
                true
            ),
        ]
    );
    // A separate lease clock: advance and lock read the clock then the lease clock, the
    // ledger's commit reads the lease clock, the queue time the clock.
    let rig = Rig::new(100.0, 1.0, Some((1000.0, 1.0)));
    let c = coordinator(&rig, factory_config(), None);
    c.admit(&request("r1", "interactive"), "interactive_chat_v1", &[])
        .expect("admitted");
    assert_eq!(
        rig.take().0,
        reads(&[
            ("clock", 100.0),
            ("lease", 1000.0),
            ("clock", 101.0),
            ("lease", 1001.0),
            ("clock", 102.0),
            ("lease", 1002.0),
            ("lease", 1003.0),
            ("lease", 1004.0),
            ("clock", 103.0),
        ])
    );
    assert_eq!(
        live(&c, "r1").into_iter().map(|r| r.2).collect::<Vec<_>>(),
        vec![4600.0f64.to_bits(), 4601.0f64.to_bits()]
    );
    // The cancellation owner thread is a real caller (:543-547).
    let shared = Arc::new(c);
    let worker = {
        let shared = Arc::clone(&shared);
        std::thread::spawn(move || shared.cancel("r1"))
    };
    assert!(worker.join().expect("the thread ran"));
    assert_eq!(shared.terminal_state("r1"), Some("cancelled".to_owned()));
}

#[test]
fn m16_coordinator_admit() {
    let rig = Rig::standard();
    let c = coordinator(&rig, factory_config(), None);
    let r1 = request("r1", "interactive");
    // :413-421 in order; none reads a clock, draws an id or records anything.
    for (excluded, profile, request, code) in [
        (
            vec![json!(1)],
            "interactive_chat_v1",
            r1.clone(),
            "invalid_excluded_placements",
        ),
        (
            vec![json!("")],
            "interactive_chat_v1",
            r1.clone(),
            "invalid_excluded_placements",
        ),
        (
            vec![json!("p9"), json!(2.5)],
            "interactive_chat_v1",
            r1.clone(),
            "invalid_excluded_placements",
        ),
        (
            vec![json!(null)],
            "unknown",
            r1.clone(),
            "invalid_excluded_placements",
        ),
        (vec![], "unknown", r1.clone(), "workload_not_qualified"),
        (
            vec![],
            "interactive_chat_v1",
            request("r1", "Interactive"),
            "workload_not_qualified",
        ),
        (
            vec![],
            "interactive_chat_v1",
            request("r1", "batch"),
            "workload_not_qualified",
        ),
        (
            vec![],
            "sustained_batch_v1",
            r1.clone(),
            "workload_not_qualified",
        ),
    ] {
        assert_eq!(c.admit(&request, profile, &excluded), Err(adm(code)));
    }
    assert_eq!(rig.take(), (vec![], vec![]));
    assert_eq!(c.incidents(), vec![]);
    // A non-empty str naming no placement is a valid exclusion.
    let m1 = manifest(
        "r1",
        "path-1",
        [("reservation-2", 3700.25), ("reservation-3", 3700.75)],
    );
    assert_eq!(
        c.admit(&r1, "interactive_chat_v1", &[json!("p9")]),
        Ok(m1.clone())
    );
    assert_eq!(
        rig.take(),
        (
            admit_reads(100.0),
            ids(&["path-1", "reservation-2", "reservation-3"])
        )
    );
    // :482-496 the record, queued at the ninth read.
    assert_eq!(
        c.request_record("r1"),
        Some(record(
            r1.clone(),
            "interactive_chat_v1",
            m1.clone(),
            R1_PATH_DIGEST,
            "queued",
            102.0,
            None,
            None
        ))
    );
    // :452-467 the REQUEST item field for field, as it is queued and popped.
    let mut item = HopWorkItem::new(
        "r1",
        "path-1",
        i(0),
        "REQUEST",
        i(-1),
        i(0),
        PyJson::str("p0"),
        "interactive",
        0.0,
        102.0,
        "m16:r1:path-1",
        HopPayload::Tuple((1..=4).map(i).collect()),
    );
    item.lease_expires_at = f(3700.25);
    assert_eq!(c.queued_items(), vec![item]);
    assert_eq!(c.queued_payload_bytes(), 16);
    assert_eq!(c.phase("r1"), Some("queued".to_owned()));
    // :420 the recorded id.
    assert_eq!(
        c.admit(&r1, "interactive_chat_v1", &[]),
        Err(adm("duplicate_request_id"))
    );
    assert_eq!(rig.take(), (vec![], vec![]));
    // :442-449 a RoutingError (stage 1's only placement excluded: no feasible route at the
    // first advance) records the incident at the next read and raises resource_unavailable.
    assert_eq!(
        c.admit(
            &request("r2", "interactive"),
            "interactive_chat_v1",
            &[json!("p1")]
        ),
        Err(adm("resource_unavailable"))
    );
    assert_eq!(
        rig.take(),
        (seq("clock", 102.25, 0.25, 3), ids(&["path-4"]))
    );
    assert_eq!(
        c.incidents(),
        vec![incident(
            1,
            "admission_rejected",
            "r2",
            "deployment",
            "resource_unavailable",
            102.75,
            None
        )]
    );
    assert_eq!(c.request_record("r2"), None);
    // A refusal by capacity at stage 1 (119 KV bytes for 120) releases stage 0's reservation
    // (routing.py:194-204, 233-238) before the incident.
    let rig_cap = Rig::standard();
    let mut inputs = rig_cap.inputs(
        parse(&linear_doc()),
        vec![capacity(json!("p0"), 1_000_000), capacity(json!("p1"), 119)],
        factory_config(),
    );
    inputs.max_concurrent_requests = None;
    let tight = M16RuntimeCoordinator::new(inputs).expect("a coordinator");
    assert_eq!(
        tight.admit(&r1, "interactive_chat_v1", &[]),
        Err(adm("resource_unavailable"))
    );
    assert_eq!(
        rig_cap.take(),
        (
            seq("clock", 100.0, 0.25, 5),
            ids(&["path-1", "reservation-2"])
        )
    );
    assert_eq!(placements(&tight), reserved((0, 0), (0, 0)));
    assert_eq!(
        tight.incidents(),
        vec![incident(
            1,
            "admission_rejected",
            "r1",
            "deployment",
            "resource_unavailable",
            101.0,
            None
        )]
    );
    // 4 + 7 tokens fit: (4 + 7) * 10 = 110.
    let r2 = request_with("r2", "interactive", (1..=4).map(i).collect(), 7);
    assert!(tight.admit(&r2, "interactive_chat_v1", &[]).is_ok());
    assert_eq!(placements(&tight), reserved((1, 0), (1, 110)));
    // A builder exception that is not a RoutingError propagates as it is, with no incident:
    // float(10**400) at the first advance (routing.py:140) after its two reads.
    let rig_overflow = Rig::standard();
    let mut config = factory_config();
    config.reservation_lease_seconds = big(400);
    let overflow = coordinator(&rig_overflow, config, None);
    assert_eq!(
        overflow.admit(&r1, "interactive_chat_v1", &[]),
        Err(CoordinatorError::PathBuilder(PathBuilderError::Python(
            "m16_path_builder_int_too_large_for_float"
        )))
    );
    assert_eq!(
        rig_overflow.take(),
        (seq("clock", 100.0, 0.25, 2), ids(&["path-1"]))
    );
    assert_eq!(
        (overflow.incidents(), overflow.request_record("r1")),
        (vec![], None)
    );
    assert_eq!(placements(&overflow), reserved((0, 0), (0, 0)));
    // A scheduler exception other than backpressure propagates after lock (a float prompt
    // token has no byte size, scheduler.py:265-275): the committed reservations stay, nothing
    // is recorded (:468-470 catch only BackpressureError).
    let rig_payload = Rig::standard();
    let payload = coordinator(&rig_payload, factory_config(), None);
    let floats = request_with("r1", "interactive", vec![i(1), f(2.0), i(3)], 8);
    assert_eq!(
        payload.admit(&floats, "interactive_chat_v1", &[]),
        Err(CoordinatorError::Scheduler(SchedulerError::Type(
            "unsupported_hop_payload_for_byte_accounting"
        )))
    );
    assert_eq!(rig_payload.take().0, admit_reads(100.0));
    assert_eq!(
        (
            payload.incidents(),
            payload.request_record("r1"),
            payload.queue_depth()
        ),
        (vec![], None, 0)
    );
    assert_eq!(placements(&payload), reserved((1, 0), (1, 110)));
    assert_eq!(
        live(&payload, "r1")
            .into_iter()
            .map(|r| r.3)
            .collect::<Vec<_>>(),
        vec![true, true]
    );
}

#[test]
fn m16_coordinator_admit_backpressure() {
    // :468-480 `work_limit`: the second request finds the one-hop queue full. Its reservations
    // (taken and committed by lock) are released, the incident carries the reason and the
    // retry `max(0.001, 0.01)` (scheduler.py:52), and nothing is recorded for it.
    let rig = Rig::standard();
    let mut config = factory_config();
    config.maximum_pending_hops = i(1);
    let c = coordinator(&rig, config, None);
    c.admit(&request("r1", "interactive"), "interactive_chat_v1", &[])
        .expect("r1 admitted");
    rig.take();
    let queue_full = |retry: PyReal| {
        Err(CoordinatorError::Admission(M16AdmissionError {
            code: "queue_full",
            retry_after_seconds: Some(retry),
        }))
    };
    assert_eq!(
        c.admit(&request("r2", "interactive"), "interactive_chat_v1", &[]),
        queue_full(f(0.01))
    );
    assert_eq!(
        rig.take(),
        (
            seq("clock", 102.25, 0.25, 10),
            ids(&["path-4", "reservation-5", "reservation-6"])
        )
    );
    assert_eq!(
        c.incidents(),
        vec![incident(
            1,
            "backpressure",
            "r2",
            "queue",
            "work_limit",
            104.5,
            Some(f(0.01))
        )]
    );
    assert_eq!((c.request_record("r2"), c.phase("r2")), (None, None));
    assert_eq!(live(&c, "r2"), vec![]);
    assert_eq!(placements(&c), reserved((1, 0), (1, 120)));
    assert_eq!(c.queue_depth(), 1);
    // Once r1 is popped, r2 is admitted (it was never recorded, so it is no duplicate).
    assert_eq!(c.next_dispatch(), Ok(Some("r1".to_owned())));
    assert!(
        c.admit(&request("r2", "interactive"), "interactive_chat_v1", &[])
            .is_ok()
    );
    assert_eq!(placements(&c), reserved((2, 0), (2, 240)));
    // `byte_limit`: 16 queued bytes plus 8 exceed 20; then 16 plus 4 is exactly 20 and fits
    // (scheduler.py:56-60, `>`).
    let rig = Rig::standard();
    let mut config = factory_config();
    config.maximum_pending_bytes = i(20);
    let c = coordinator(&rig, config, None);
    c.admit(&request("r1", "interactive"), "interactive_chat_v1", &[])
        .expect("r1 admitted");
    let two = request_with("r2", "interactive", vec![i(1), i(2)], 8);
    assert_eq!(
        c.admit(&two, "interactive_chat_v1", &[]),
        queue_full(f(0.01))
    );
    let one = request_with("r3", "interactive", vec![i(1)], 8);
    assert!(c.admit(&one, "interactive_chat_v1", &[]).is_ok());
    assert_eq!(
        c.incidents(),
        vec![incident(
            1,
            "backpressure",
            "r2",
            "queue",
            "byte_limit",
            104.5,
            Some(f(0.01))
        )]
    );
    assert_eq!((c.queue_depth(), c.queued_payload_bytes()), (2, 20));
    assert_eq!(live(&c, "r2"), vec![]);
    assert_eq!(c.request_record("r2"), None);
    // The retry is Python's `max(0.001, configured)`: a configured 0.0 gives 0.001, `True`
    // stays `True` (True > 0.001), an int 7 stays the int.
    for (field, retry, expected) in [
        ("hops", f(0.0), f(0.001)),
        ("bytes", PyReal::Bool(true), PyReal::Bool(true)),
        ("hops", i(7), i(7)),
    ] {
        let rig = Rig::standard();
        let mut config = factory_config();
        if field == "hops" {
            config.maximum_pending_hops = i(0);
        } else {
            config.maximum_pending_bytes = i(0);
        }
        config.backpressure_retry_after_seconds = retry;
        let c = coordinator(&rig, config, None);
        assert_eq!(
            c.admit(&request("r1", "interactive"), "interactive_chat_v1", &[]),
            queue_full(expected.clone())
        );
        let reason = if field == "hops" {
            "work_limit"
        } else {
            "byte_limit"
        };
        assert_eq!(
            c.incidents(),
            vec![incident(
                1,
                "backpressure",
                "r1",
                "queue",
                reason,
                102.25,
                Some(expected)
            )]
        );
        assert_eq!(placements(&c), reserved((0, 0), (0, 0)));
    }
}

#[test]
fn m16_coordinator_next_dispatch() {
    // :500-504 an empty queue: None, no read.
    let rig = Rig::standard();
    let c = coordinator(&rig, factory_config(), Some(i(1)));
    assert_eq!(c.next_dispatch(), Ok(None));
    assert_eq!(rig.take(), (vec![], vec![]));
    // Priority, not arrival order (scheduler.py:69-84): the interactive request admitted
    // second (base 100.0) outranks the batch one (base 10.0 plus aging).
    let b1 = request("b1", "batch");
    c.admit(&b1, "sustained_batch_v1", &[])
        .expect("b1 admitted");
    c.admit(&request("i1", "interactive"), "interactive_chat_v1", &[])
        .expect("i1 admitted");
    rig.take();
    assert_eq!(c.next_dispatch(), Ok(Some("i1".to_owned())));
    // pop_next at a clock read, one lease read per live reservation (none expired), the
    // dispatch time at the next clock read.
    assert_eq!(rig.take().0, seq("clock", 104.5, 0.25, 4));
    assert_eq!(c.phase("i1"), Some("prefill".to_owned()));
    assert_eq!(
        c.request_record("i1").map(|r| (r.dispatch_at, r.queued_at)),
        Some((Some(105.25), 104.25))
    );
    assert_eq!(c.active_request_ids(), vec!["i1".to_owned()]);
    // At the cap (one active) nothing is popped and nothing read.
    assert_eq!(c.next_dispatch(), Ok(None));
    assert_eq!(rig.take(), (vec![], vec![]));
    assert_eq!(c.queue_depth(), 1);
    c.complete("i1", "completed").expect("completed");
    assert_eq!(c.next_dispatch(), Ok(Some("b1".to_owned())));
    assert_eq!(c.next_dispatch(), Ok(None));
    // :507-520 an expired reservation, at equality (`expires_at <= lease now`): r1's first
    // hop expires at 1000.0 + 5.0. Lease reads: r1 1000-1004 (two advances, lock, two
    // commits), r2 1005-1009; the clock reads 100-103 and 104-107 (queue times 103.0, 107.0).
    let rig = Rig::new(100.0, 1.0, Some((1000.0, 1.0)));
    let mut config = factory_config();
    config.reservation_lease_seconds = f(5.0);
    let c = coordinator(&rig, config.clone(), Some(i(4)));
    let r1 = request("r1", "interactive");
    c.admit(&r1, "interactive_chat_v1", &[])
        .expect("r1 admitted");
    c.admit(&request("r2", "interactive"), "interactive_chat_v1", &[])
        .expect("r2 admitted");
    assert_eq!(
        live(&c, "r2").into_iter().map(|r| r.2).collect::<Vec<_>>(),
        vec![1010.0f64.to_bits(), 1011.0f64.to_bits()]
    );
    rig.take();
    rig.lease.as_ref().expect("a lease clock").set(1005.0);
    // One call: pop r1 (clock 108), its first reservation 1005.0 <= 1005.0 (one lease read,
    // `any` stops), release, fail at 109, incident at 110; pop r2 (clock 111), 1010 > 1006 and
    // 1011 > 1007, dispatch at 112.
    assert_eq!(c.next_dispatch(), Ok(Some("r2".to_owned())));
    assert_eq!(
        rig.take(),
        (
            reads(&[
                ("clock", 108.0),
                ("lease", 1005.0),
                ("clock", 109.0),
                ("clock", 110.0),
                ("clock", 111.0),
                ("lease", 1006.0),
                ("lease", 1007.0),
                ("clock", 112.0),
            ]),
            vec![]
        )
    );
    assert_eq!(
        c.request_record("r1").map(|r| (
            r.phase,
            r.terminal_state,
            r.terminal_at,
            r.dispatch_at,
            r.queued_at
        )),
        Some((
            "failed".to_owned(),
            Some("reservation_expired".to_owned()),
            Some(109.0),
            None,
            103.0
        ))
    );
    assert_eq!(live(&c, "r1"), vec![]);
    assert_eq!(
        c.incidents(),
        vec![incident(
            1,
            "reservation_expired",
            "r1",
            "request",
            "failed",
            110.0,
            None
        )]
    );
    assert_eq!(c.active_request_ids(), vec!["r2".to_owned()]);
    assert_eq!(
        c.request_record("r2").map(|r| (r.phase, r.dispatch_at)),
        Some(("prefill".to_owned(), Some(112.0)))
    );
    assert_eq!(placements(&c), reserved((1, 0), (1, 120)));
    assert_eq!(c.next_dispatch(), Ok(None));
    // A quarter second before expiry the request is live: 1005.0 > 1004.75, 1006.0 > 1005.75.
    let rig = Rig::new(100.0, 1.0, Some((1000.0, 1.0)));
    let c = coordinator(&rig, config.clone(), None);
    c.admit(&request("r1", "interactive"), "interactive_chat_v1", &[])
        .expect("r1 admitted");
    rig.take();
    rig.lease.as_ref().expect("a lease clock").set(1004.75);
    assert_eq!(c.next_dispatch(), Ok(Some("r1".to_owned())));
    assert_eq!(
        rig.take().0,
        reads(&[
            ("clock", 104.0),
            ("lease", 1004.75),
            ("lease", 1005.75),
            ("clock", 105.0)
        ])
    );
    assert_eq!(c.incidents(), vec![]);
    // Every queued request expired: each is failed with an incident and the call returns
    // None once the queue is empty (the shared clock jumped to 1e6: per request a pop, one
    // lease read, the terminal time and the incident, so the incidents read 1e6 + 1.5 and
    // 1e6 + 3.5).
    let rig = Rig::new(100.0, 0.5, None);
    let mut short = factory_config();
    short.reservation_lease_seconds = f(3.0);
    let c = coordinator(&rig, short, Some(i(4)));
    for id in ["r1", "r2"] {
        c.admit(&request(id, "interactive"), "interactive_chat_v1", &[])
            .expect("admitted");
    }
    rig.clock.set(1e6);
    assert_eq!(c.next_dispatch(), Ok(None));
    assert_eq!(
        c.incidents()
            .into_iter()
            .map(|x| (x.incident_id, x.request_id, x.observed_at_monotonic_s))
            .collect::<Vec<_>>(),
        vec![
            ("m16-incident-1".to_owned(), "r1".to_owned(), 1e6 + 1.5),
            ("m16-incident-2".to_owned(), "r2".to_owned(), 1e6 + 3.5),
        ]
    );
    assert_eq!(
        (
            c.terminal_state("r1"),
            c.terminal_state("r2"),
            c.queue_depth()
        ),
        (
            Some("reservation_expired".to_owned()),
            Some("reservation_expired".to_owned()),
            0
        )
    );
    assert_eq!(c.active_request_ids(), Vec::<String>::new());
}

#[test]
fn m16_coordinator_phase_and_complete() {
    let rig = Rig::standard();
    let c = coordinator(&rig, factory_config(), Some(i(2)));
    let phase_invalid = Err(CoordinatorError::Value("m16_phase_transition_invalid"));
    let completion_invalid = Err(CoordinatorError::Value("m16_completion_invalid"));
    // :530-536 only an active request, only these four phases.
    assert_eq!(c.mark_phase("r1", "prefill"), phase_invalid);
    let r1 = request("r1", "interactive");
    let m1 = c.admit(&r1, "interactive_chat_v1", &[]).expect("admitted");
    assert_eq!(c.mark_phase("r1", "prefill"), phase_invalid);
    // :552-561 a queued request is neither active nor holding the cleanup proof.
    assert_eq!(c.complete("r1", "completed"), completion_invalid);
    assert_eq!(c.next_dispatch(), Ok(Some("r1".to_owned())));
    for bad in ["queued", "Decode", "failed", "completed", ""] {
        assert_eq!(c.mark_phase("r1", bad), phase_invalid, "{bad}");
    }
    for good in ["prefill", "first_token", "decode", "cleanup"] {
        assert_eq!(c.mark_phase("r1", good), Ok(()));
        assert_eq!(c.phase("r1"), Some(good.to_owned()));
    }
    for bad in ["done", "Completed", "reservation_expired", ""] {
        assert_eq!(c.complete("r1", bad), completion_invalid, "{bad}");
    }
    rig.take();
    // :567-579 retire frees only the slot: phase cleanup, no terminal, reservations held.
    c.retire_dispatch_slot("r1");
    assert_eq!(c.active_request_ids(), Vec::<String>::new());
    assert_eq!(
        (c.phase("r1"), c.terminal_state("r1")),
        (Some("cleanup".to_owned()), None)
    );
    assert_eq!(placements(&c), reserved((1, 0), (1, 120)));
    // :548-553 the late cleanup proof completes the retired request: release, phase and
    // terminal state, terminal time at the next read.
    assert_eq!(c.complete("r1", "failed"), Ok(()));
    assert_eq!(rig.take(), (seq("clock", 103.25, 0.25, 1), vec![]));
    assert_eq!(
        c.request_record("r1"),
        Some(record(
            r1,
            "interactive_chat_v1",
            m1,
            R1_PATH_DIGEST,
            "failed",
            102.0,
            Some(103.0),
            Some((103.25, "failed"))
        ))
    );
    assert_eq!(placements(&c), reserved((0, 0), (0, 0)));
    // A terminal request holds no proof any more; unknown ids neither.
    assert_eq!(c.complete("r1", "completed"), completion_invalid);
    assert_eq!(c.mark_phase("r1", "decode"), phase_invalid);
    assert_eq!(c.complete("nobody", "completed"), completion_invalid);
    c.retire_dispatch_slot("nobody");
    assert_eq!(rig.take(), (vec![], vec![]));
    // A retired request that never reached cleanup holds no proof (:550).
    c.admit(&request("r2", "interactive"), "interactive_chat_v1", &[])
        .expect("admitted");
    assert_eq!(c.next_dispatch(), Ok(Some("r2".to_owned())));
    c.retire_dispatch_slot("r2");
    assert_eq!(c.complete("r2", "completed"), completion_invalid);
    assert_eq!(c.phase("r2"), Some("prefill".to_owned()));
    // Each terminal state ends an active request and releases its reservations; cancel of a
    // terminal request then changes nothing (:583-584).
    let rig = Rig::standard();
    let c = coordinator(&rig, factory_config(), Some(i(3)));
    for id in ["a", "b", "c"] {
        c.admit(&request(id, "interactive"), "interactive_chat_v1", &[])
            .expect("admitted");
    }
    for id in ["a", "b", "c"] {
        assert_eq!(c.next_dispatch(), Ok(Some(id.to_owned())));
    }
    for (id, state) in [("a", "completed"), ("b", "failed"), ("c", "cancelled")] {
        assert_eq!(c.complete(id, state), Ok(()));
        assert_eq!(
            (c.phase(id), c.terminal_state(id)),
            (Some(state.to_owned()), Some(state.to_owned()))
        );
        assert_eq!(live(&c, id), vec![]);
    }
    assert_eq!(placements(&c), reserved((0, 0), (0, 0)));
    assert_eq!(c.active_request_ids(), Vec::<String>::new());
    assert!(!c.cancel("c"));
    assert_eq!(c.incidents(), vec![]);
}

#[test]
fn m16_coordinator_retire_and_cancel() {
    let rig = Rig::standard();
    let c = coordinator(&rig, factory_config(), Some(i(1)));
    c.admit(&request("q1", "interactive"), "interactive_chat_v1", &[])
        .expect("admitted");
    c.admit(&request("q2", "interactive"), "interactive_chat_v1", &[])
        .expect("admitted");
    rig.take();
    // :581-599 a queued request: its queued path is dropped (release_path), its reservations
    // released, cancelled at the next read, the incident at the one after.
    assert!(c.cancel("q2"));
    assert_eq!(rig.take(), (seq("clock", 104.5, 0.25, 2), vec![]));
    assert_eq!(
        c.request_record("q2")
            .map(|r| (r.phase, r.terminal_state, r.terminal_at, r.dispatch_at)),
        Some((
            "cancelled".to_owned(),
            Some("cancelled".to_owned()),
            Some(104.5),
            None
        ))
    );
    assert_eq!(
        c.incidents(),
        vec![incident(
            1,
            "cancellation_cleanup",
            "q2",
            "request",
            "resources_released",
            104.75,
            None
        )]
    );
    assert_eq!((c.queue_depth(), c.queued_payload_bytes()), (1, 16));
    assert_eq!(
        c.queued_items()
            .into_iter()
            .map(|x| x.request_id)
            .collect::<Vec<_>>(),
        vec!["q1".to_owned()]
    );
    assert_eq!(placements(&c), reserved((1, 0), (1, 120)));
    // retire frees only the dispatch slot (:567-579): phase, terminal state and the ledger
    // stay, and the slot is free again.
    assert_eq!(c.next_dispatch(), Ok(Some("q1".to_owned())));
    c.retire_dispatch_slot("q1");
    assert_eq!(c.active_request_ids(), Vec::<String>::new());
    assert_eq!(
        (c.phase("q1"), c.terminal_state("q1")),
        (Some("prefill".to_owned()), None)
    );
    assert_eq!(placements(&c), reserved((1, 0), (1, 120)));
    assert_eq!(c.next_dispatch(), Ok(None));
    rig.take();
    // A dispatched (here retired, not terminal) request: released, cancelled, incident.
    assert!(c.cancel("q1"));
    assert_eq!(rig.take(), (seq("clock", 106.0, 0.25, 2), vec![]));
    assert_eq!(placements(&c), reserved((0, 0), (0, 0)));
    assert_eq!(
        c.incidents().last().cloned(),
        Some(incident(
            2,
            "cancellation_cleanup",
            "q1",
            "request",
            "resources_released",
            106.25,
            None
        ))
    );
    // Unknown and terminal requests: false, no read, no incident (:583-584).
    let before = (c.incidents(), c.request_records());
    assert!(!c.cancel("q1"));
    assert!(!c.cancel("nobody"));
    assert_eq!(rig.take(), (vec![], vec![]));
    assert_eq!((c.incidents(), c.request_records()), before);
    // An active request in cleanup, retired, is still cancellable; it then holds no cleanup
    // proof (its phase is cancelled).
    c.admit(&request("q3", "interactive"), "interactive_chat_v1", &[])
        .expect("admitted");
    assert_eq!(c.next_dispatch(), Ok(Some("q3".to_owned())));
    assert_eq!(c.mark_phase("q3", "cleanup"), Ok(()));
    c.retire_dispatch_slot("q3");
    rig.take();
    assert!(c.cancel("q3"));
    assert_eq!(rig.take(), (seq("clock", 109.75, 0.25, 2), vec![]));
    assert_eq!(
        c.complete("q3", "completed"),
        Err(CoordinatorError::Value("m16_completion_invalid"))
    );
    assert_eq!(
        c.incidents()
            .into_iter()
            .map(|x| x.incident_id)
            .collect::<Vec<_>>(),
        vec!["m16-incident-1", "m16-incident-2", "m16-incident-3"]
    );
    // cancel of an active request frees its slot.
    let rig = Rig::standard();
    let c = coordinator(&rig, factory_config(), Some(i(1)));
    c.admit(&request("a", "interactive"), "interactive_chat_v1", &[])
        .expect("admitted");
    c.admit(&request("b", "interactive"), "interactive_chat_v1", &[])
        .expect("admitted");
    assert_eq!(c.next_dispatch(), Ok(Some("a".to_owned())));
    assert_eq!(c.next_dispatch(), Ok(None));
    assert!(c.cancel("a"));
    assert_eq!(c.active_request_ids(), Vec::<String>::new());
    assert_eq!(c.next_dispatch(), Ok(Some("b".to_owned())));
}

#[test]
fn m16_coordinator_route_identity_and_digests() {
    let rig = Rig::standard();
    let c = coordinator(&rig, factory_config(), None);
    let r1 = request("r1", "interactive");
    let m1 = c.admit(&r1, "interactive_chat_v1", &[]).expect("admitted");
    // The hand-written document text is the oracle's (independent SHA-256 of it equals the
    // recorded digest), and the port digests the manifest to the same bytes.
    assert_eq!(sha256(R1_PATH_TEXT), R1_PATH_DIGEST);
    assert_eq!(
        path_manifest_document_digest(&m1),
        Ok(R1_PATH_DIGEST.to_owned())
    );
    let document = path_manifest_to_dict(&m1).expect("a document");
    assert_eq!(canonical_json(&document), Ok(R1_PATH_TEXT.to_owned()));
    // :675-695.
    assert_eq!(
        c.route_identity("r1"),
        Ok(RouteIdentity {
            request_id: "r1".to_owned(),
            request_attempt: 1,
            path_id: "path-1".to_owned(),
            path_attempt: 0,
            path_manifest_digest: R1_PATH_DIGEST.to_owned(),
            deployment_id: json!("deployment-m16"),
            deployment_epoch: json!(1),
            topology_generation: json!(1),
        })
    );
    // :697-707 the path document; :666-673 the exact request context.
    assert_eq!(c.path_manifest("r1"), Ok(document));
    assert_eq!(c.request_context("r1"), Ok(r1.clone()));
    // An id never admitted: request_not_admitted (M16AdmissionError), None for the phases.
    assert_eq!(c.request_context("ghost"), Err(not_admitted()));
    assert_eq!(c.route_identity("ghost"), Err(not_admitted()));
    assert_eq!(c.path_manifest("ghost"), Err(not_admitted()));
    assert_eq!((c.phase("ghost"), c.terminal_state("ghost")), (None, None));
    // The identity stays after the request ends.
    assert!(c.cancel("r1"));
    assert_eq!(
        c.route_identity("r1").map(|r| r.path_manifest_digest),
        Ok(R1_PATH_DIGEST.to_owned())
    );
    // An exact-int epoch past 64 bits, a float topology version and non-ASCII ids (case
    // flow_numeric_epoch_and_unicode): the epoch prints exactly, 1.0 as a float, the text raw.
    let mut doc = linear_doc();
    doc["deployment_epoch"] = serde_json::from_str("18446744073709551617").expect("a number");
    doc["topology_version"] = serde_json::from_str("1.0").expect("a number");
    doc["deployment_id"] = json!("d\u{e9}ploiement-\u{1f600}");
    let rig = Rig::standard();
    let wide = M16RuntimeCoordinator::new(rig.inputs(parse(&doc), caps(), factory_config()))
        .expect("a coordinator");
    let m = wide
        .admit(&r1, "interactive_chat_v1", &[])
        .expect("admitted");
    let epoch = "18446744073709551617";
    let text = format!(
        "{{\"deployment_epoch\":{epoch},\"deployment_id\":\"d\u{e9}ploiement-\u{1f600}\",\
         \"loopback_edge_id\":\"loop-0\",\"manifest_digest\":\"digest-m16\",\"ordered_hops\":[\
         {{\"placement_id\":\"p0\",\"reservation_epoch\":{epoch},\"reservation_expires_at\":3700.25,\
         \"reservation_id\":\"reservation-2\",\"stage_id\":\"stage-0\"}},{{\"placement_id\":\"p1\",\
         \"reservation_epoch\":{epoch},\"reservation_expires_at\":3700.75,\"reservation_id\":\
         \"reservation-3\",\"stage_id\":\"stage-1\"}}],\"path_attempt\":0,\"path_id\":\"path-1\",\
         \"protocol\":\"mycelium.path_manifest.v1\",\"request_id\":\"r1\",\"topology_version\":1.0}}"
    );
    let recorded = "sha256:db2b9cbd5bc60f789570b27453c8a61f2a8da8bac3fcbdb844dce3b22d7958a9";
    assert_eq!(sha256(&text), recorded);
    assert_eq!(path_manifest_document_digest(&m), Ok(recorded.to_owned()));
    assert_eq!(
        wide.route_identity("r1").map(|r| (
            r.deployment_id,
            r.deployment_epoch,
            r.topology_generation
        )),
        Ok((
            json!("d\u{e9}ploiement-\u{1f600}"),
            doc["deployment_epoch"].clone(),
            doc["topology_version"].clone()
        ))
    );
    // `_digest` itself (:116-124): keys by code point (U+E000 before U+FFFF before U+1F600,
    // where UTF-16 units would put the astral key first), C0 controls escaped and DEL raw, the
    // raw int -0 as 0, floats as repr (1e+16, -0.0, 5e-324, 100000.0, 1e-05), ints exact
    // (recorded digest: fixture digests[4]).
    let value: Value = serde_json::from_str(concat!(
        r#"{"z":1,"\uffff":2,"\ud83d\ude00":3,"\ue000":4,"\u0001":"\u007f\"\\\n","-0":-0,"#,
        r#""f":[1e16,-0.0,5e-324,1E5,0.1,1e-5,123456789012345678901234567890]}"#
    ))
    .expect("JSON");
    let text = "{\"\\u0001\":\"\u{7f}\\\"\\\\\\n\",\"-0\":0,\"f\":[1e+16,-0.0,5e-324,100000.0,0.1,\
                1e-05,123456789012345678901234567890],\"z\":1,\"\u{e000}\":4,\"\u{ffff}\":2,\
                \"\u{1f600}\":3}";
    let recorded = "sha256:ef38c069dcc4eee98c05858666cbeec5049e6863bbff6cb8dfa911338e92cb3b";
    assert_eq!(sha256(text), recorded);
    assert_eq!(digest_document(&value), Ok(recorded.to_owned()));
    // float.__repr__ breaks an exact tie between the two shortest candidates to the even last
    // digit (dtoa.c mode 0): 1059438285926254.25 lies half-way between ...254.2 and ...254.3,
    // 26363981746409.3125 between ...409.312 and ...409.313, and the lease expiry
    // 1729996400.00390625 + 3600.0 = 1730000000.00390625 between ...0039062 and ...0039063.
    let ties: Value = serde_json::from_str(
        "[1059438285926254.25,-1425502010969177.25,26363981746409.3125,1730000000.00390625,0.5,2.5]",
    )
    .expect("JSON");
    assert_eq!(
        canonical_json(&ties),
        Ok("[1059438285926254.2,-1425502010969177.2,26363981746409.312,1730000000.0039062,0.5,2.5]"
            .to_owned())
    );
    // 1729996400 + 2**-8 and 1730000000 + 2**-8, both exact doubles.
    let rig_tie = Rig::new(100.0, 0.25, Some((1_729_996_400.0 + 0.003_906_25, 0.0)));
    let tie = coordinator(&rig_tie, factory_config(), None);
    let m = tie
        .admit(&r1, "interactive_chat_v1", &[])
        .expect("admitted");
    assert_eq!(
        m.ordered_hops
            .iter()
            .map(|h| h.reservation_expires_at.to_bits())
            .collect::<Vec<_>>(),
        vec![(1_730_000_000.0_f64 + 0.003_906_25).to_bits(); 2]
    );
    let tie_text = path_manifest_to_dict(&m)
        .and_then(|document| canonical_json(&document))
        .expect("a document");
    assert_eq!(
        tie_text
            .matches("\"reservation_expires_at\":1730000000.0039062,")
            .count(),
        2
    );
    // allow_nan=False: an infinite or NaN expiry, a 1e400 graph value.
    let out_of_range = Err(DigestError::OutOfRangeFloat);
    for bad in [INF, -INF, f64::NAN] {
        let mut m = m1.clone();
        m.ordered_hops[1].reservation_expires_at = bad;
        assert_eq!(path_manifest_document_digest(&m), out_of_range);
        assert_eq!(path_manifest_to_dict(&m), Err(DigestError::OutOfRangeFloat));
    }
    let mut m = m1.clone();
    m.deployment_epoch = serde_json::from_str("1e400").expect("a number");
    assert_eq!(path_manifest_document_digest(&m), out_of_range);
    for text in ["[1e400]", "[-1e400]", r#"{"a":[1,{"b":1e999}]}"#] {
        let value: Value = serde_json::from_str(text).expect("JSON");
        assert_eq!(digest_document(&value), out_of_range, "{text}");
    }
    // A lone surrogate, which `.encode("utf-8")` refuses (UnicodeEncodeError), never reaches
    // a digest here: serde_json refuses it at decode.
    for text in [r#""\ud800""#, r#"{"\udc00":1}"#, r#"["\udc00\ud800"]"#] {
        assert!(serde_json::from_str::<Value>(text).is_err(), "{text}");
    }
    // :450 the path digest error after lock (an infinite lease): the committed reservations
    // stay, nothing is recorded, no incident; the request was never admitted.
    let rig = Rig::standard();
    let mut config = factory_config();
    config.reservation_lease_seconds = f(INF);
    let c = coordinator(&rig, config, None);
    assert_eq!(
        c.admit(&r1, "interactive_chat_v1", &[]),
        Err(CoordinatorError::Digest(DigestError::OutOfRangeFloat))
    );
    // Eight reads: no queue time is read after the refused digest.
    assert_eq!(rig.take().0, seq("clock", 100.0, 0.25, 8));
    assert_eq!(placements(&c), reserved((1, 0), (1, 120)));
    assert_eq!(
        live(&c, "r1")
            .into_iter()
            .map(|r| (r.2, r.3))
            .collect::<Vec<_>>(),
        vec![(INF.to_bits(), true), (INF.to_bits(), true)]
    );
    assert_eq!(
        (c.incidents(), c.request_record("r1"), c.queue_depth()),
        (vec![], None, 0)
    );
    assert_eq!(c.route_identity("r1"), Err(not_admitted()));
}

// ---------------------------------------------------------------------------------------------
// Paired oracle probes.

const ORACLE: &str = include_str!("fixtures/m16-coordinator-oracle.json");

/// A tagged Python number.
fn recorded(tagged: &Value) -> PyReal {
    if let Some(flag) = tagged.get("bool").and_then(Value::as_bool) {
        return PyReal::Bool(flag);
    }
    if let Some(text) = tagged.get("int").and_then(Value::as_str) {
        let (negative, digits) = text
            .strip_prefix('-')
            .map_or((false, text), |digits| (true, digits));
        return PyReal::signed(negative, digits.parse().expect("decimal digits"));
    }
    PyReal::Float(recorded_f64(tagged))
}

fn recorded_f64(tagged: &Value) -> f64 {
    let bits = tagged["bits"].as_str().expect("float bits");
    f64::from_bits(u64::from_str_radix(bits, 16).expect("hex bits"))
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

/// A Python number tagged as the probe tags it (floats by bits only).
fn tag(v: &PyReal) -> Value {
    match v {
        PyReal::Bool(flag) => json!({"bool": flag}),
        PyReal::Int {
            negative,
            magnitude,
        } => json!({"int": format!("{}{magnitude}", if *negative { "-" } else { "" })}),
        PyReal::Float(x) => json!({"bits": format!("{:016x}", x.to_bits())}),
    }
}

fn tag_number(n: &PyNumber) -> Value {
    match n {
        PyNumber::Int(magnitude) => json!({"int": magnitude.to_string()}),
        PyNumber::Float(x) => json!({"bits": format!("{:016x}", x.to_bits())}),
    }
}

fn tag_f64(x: f64) -> Value {
    json!({"bits": format!("{:016x}", x.to_bits())})
}

/// The recorded tag without its `float` repr (the port is compared by bits).
fn bits_only(v: &Value) -> Value {
    match v {
        Value::Object(map) if map.contains_key("bits") => json!({"bits": map["bits"]}),
        Value::Array(items) => Value::Array(items.iter().map(bits_only).collect()),
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, item)| (k.clone(), bits_only(item)))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn opt_f64(v: Option<f64>) -> Value {
    v.map_or(Value::Null, tag_f64)
}

/// The Python exception the oracle raises for this port error: (type, message prefix).
fn python_error(error: &CoordinatorError) -> (&'static str, String) {
    match error {
        CoordinatorError::Admission(e) => ("M16AdmissionError", e.code.to_owned()),
        CoordinatorError::Value(code) => ("ValueError", (*code).to_owned()),
        CoordinatorError::Ledger(LedgerError(code)) => ("ValueError", (*code).to_owned()),
        CoordinatorError::Digest(DigestError::OutOfRangeFloat) => (
            "ValueError",
            "Out of range float values are not JSON compliant".to_owned(),
        ),
        CoordinatorError::PathBuilder(PathBuilderError::Python(
            "m16_path_builder_int_too_large_for_float",
        )) => (
            "OverflowError",
            "int too large to convert to float".to_owned(),
        ),
        CoordinatorError::Scheduler(SchedulerError::Type(code)) => {
            ("TypeError", (*code).to_owned())
        }
        other => panic!("no oracle counterpart in these probes: {other:?}"),
    }
}

fn assert_error(name: &str, recorded: &Value, error: &CoordinatorError) {
    let (kind, message) = python_error(error);
    assert_eq!(recorded["type"], kind, "{name}: {error:?}");
    let text = recorded["message"].as_str().expect("message");
    assert!(text.starts_with(&message), "{name}: {text} vs {message}");
    if let CoordinatorError::Admission(e) = error {
        assert_eq!(recorded["code"], e.code, "{name}");
        assert_eq!(
            bits_only(&recorded["retry_after_seconds"]),
            e.retry_after_seconds.as_ref().map_or(Value::Null, tag),
            "{name}"
        );
    }
}

fn canonical_text(v: &Value) -> String {
    canonical_json(v).expect("canonical")
}

fn request_from(spec: &Value) -> RequestContext {
    let s = |k: &str| spec[k].as_str().expect("a str").to_owned();
    RequestContext {
        request_id: s("request_id"),
        prompt_token_ids: spec["prompt_token_ids"]
            .as_array()
            .expect("tokens")
            .iter()
            .map(recorded)
            .collect(),
        max_new_tokens: recorded(&spec["max_new_tokens"]),
        expected_new_tokens: recorded(&spec["expected_new_tokens"]),
        qos_class: s("qos_class"),
        admitted_at: recorded(&spec["admitted_at"]),
        target_ttft_ms: recorded(&spec["target_ttft_ms"]),
        target_tpot_ms: recorded(&spec["target_tpot_ms"]),
        target_tokens_per_second: recorded(&spec["target_tokens_per_second"]),
        sampling_seed: recorded(&spec["sampling_seed"]),
        generation_config_digest: s("generation_config_digest"),
    }
}

fn request_doc(r: &RequestContext) -> Value {
    json!({
        "request_id": r.request_id,
        "prompt_token_ids": r.prompt_token_ids.iter().map(tag).collect::<Vec<_>>(),
        "max_new_tokens": tag(&r.max_new_tokens), "qos_class": r.qos_class,
        "admitted_at": tag(&r.admitted_at),
    })
}

fn identity_doc(r: &RouteIdentity) -> Value {
    json!({
        "request_id": r.request_id, "request_attempt": r.request_attempt, "path_id": r.path_id,
        "path_attempt": r.path_attempt, "path_manifest_digest": r.path_manifest_digest,
        "deployment_id": r.deployment_id, "deployment_epoch": r.deployment_epoch,
        "topology_generation": r.topology_generation,
    })
}

fn item_doc(item: &HopWorkItem) -> Value {
    let HopPayload::Tuple(payload) = &item.payload else {
        panic!("a REQUEST item carries the prompt tuple")
    };
    json!({
        "request_id": item.request_id, "path_id": item.path_id,
        "path_attempt": tag(&item.path_attempt), "phase": item.phase,
        "token_index": tag(&item.token_index), "hop_index": tag(&item.hop_index),
        "qos_class": item.qos_class, "deficit_ratio": tag_f64(item.deficit_ratio),
        "enqueued_at": tag_f64(item.enqueued_at), "idempotency_key": item.idempotency_key,
        "payload": payload.iter().map(tag).collect::<Vec<_>>(), "payload_type": "tuple",
        "prefill_chunk_token_count": tag(&item.prefill_chunk_token_count),
        "batch_key": Value::Null, "deadline_at": tag_f64(item.deadline_at),
        "position": tag(&item.position), "emits_token": item.emits_token,
        "terminal": item.terminal, "lease_expires_at": tag(&item.lease_expires_at),
    })
}

fn incident_doc(x: &Incident) -> Value {
    json!({
        "incident_id": x.incident_id, "kind": x.kind, "request_id": x.request_id,
        "scope": x.scope, "state": x.state,
        "observed_at_monotonic_s": tag_f64(x.observed_at_monotonic_s),
        "retry_after_seconds": x.retry_after_seconds.as_ref().map_or(Value::Null, tag),
    })
}

/// The probe's `snapshot(c)` from the port's accessors.
fn snapshot(c: &M16RuntimeCoordinator) -> Value {
    let requests: Vec<Value> = c
        .request_records()
        .iter()
        .map(|r| {
            let id = &r.request.request_id;
            json!({
                "request_id": id, "request": request_doc(&r.request),
                "request_attempt": r.request_attempt,
                "workload_profile_id": r.workload_profile_id,
                "manifest_json": canonical_text(&path_manifest_to_dict(&r.manifest).expect("finite")),
                "path_manifest_digest": r.path_manifest_digest,
                "recovery_successor": Value::Null,
                "recovery_source_fenced": r.recovery_source_fenced,
                "recovery_attempts": r.recovery_attempts, "phase": r.phase,
                "queued_at": tag_f64(r.queued_at), "dispatch_at": opt_f64(r.dispatch_at),
                "terminal_at": opt_f64(r.terminal_at), "terminal_state": r.terminal_state,
                "live_reservations": c.request_reservations(id).iter().map(|x| json!({
                    "reservation_id": x.reservation_id, "placement_id": x.placement_id,
                    "expires_at": tag_f64(x.expires_at), "committed": x.committed,
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    let placements: Vec<Value> = c
        .placement_status()
        .expect("placement status")
        .iter()
        .map(|p| {
            json!({
                "placement_id": p.placement_id,
                "reserved_memory_bytes": tag_number(&p.reserved_memory_bytes),
                "reserved_kv_bytes": tag_number(&p.reserved_kv_bytes),
                "reserved_workspace_bytes": tag_number(&p.reserved_workspace_bytes),
                "active_reservations": p.active_reservations,
            })
        })
        .collect();
    json!({
        "requests": requests,
        "incidents": c.incidents().iter().map(incident_doc).collect::<Vec<_>>(),
        "active_request_ids": c.active_request_ids(),
        "queue_depth": c.queue_depth(),
        "queued_payload_bytes": c.queued_payload_bytes(),
        "queued_items": c.queued_items().iter().map(item_doc).collect::<Vec<_>>(),
        "placements": placements,
    })
}

/// A recorded snapshot with every float compared by bits and each queued item's placement id
/// read back as `json.loads` would (compared to the port's [`PyJson`] separately).
fn recorded_snapshot(s: &Value, c: &M16RuntimeCoordinator, name: &str) -> Value {
    let mut s = bits_only(s);
    let items = c.queued_items();
    for (k, recorded_item) in s["queued_items"]
        .as_array_mut()
        .expect("items")
        .iter_mut()
        .enumerate()
    {
        let id = recorded_item
            .as_object_mut()
            .expect("an item")
            .remove("placement_id")
            .expect("a placement id");
        assert_eq!(
            items.get(k).map(|item| item.placement_id.clone()),
            PyJson::from_json(&id),
            "{name}: item {k}"
        );
    }
    s
}

struct Replay {
    rig: Rig,
    coordinator: M16RuntimeCoordinator,
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
            other => panic!("unmodelled override {other}"),
        }
    }
    config
}

/// A graph text the Rust carriers refuse before a coordinator exists.
#[derive(Debug, PartialEq)]
enum Unbuilt {
    /// serde_json refuses the text (a lone surrogate escape).
    Json,
    /// `execution_graph::parse` refuses the document.
    Graph,
}

/// Builds the case's coordinator (`Err` inside with the port's refusal).
fn construct(
    setup: &Value,
) -> Result<(Rig, Result<M16RuntimeCoordinator, CoordinatorError>), Unbuilt> {
    let graph: Value = serde_json::from_str(setup["graph_text"].as_str().expect("text"))
        .map_err(|_refused| Unbuilt::Json)?;
    let graph = execution_graph::parse(&graph).map_err(|_refused| Unbuilt::Graph)?;
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
    let mut states = IdMap::new();
    for s in setup["states"].as_array().expect("states") {
        let mut st = state(s["node_id"].as_str().expect("node id"));
        st.state_seq = recorded(&s["state_seq"]);
        st.last_updated = recorded(&s["last_updated"]);
        st.availability = s["availability"].as_str().expect("availability").to_owned();
        st.compute_units_per_second = recorded(&s["compute_units_per_second"]);
        st.free_compute_fraction = recorded(&s["free_compute_fraction"]);
        st.available_kv_bytes = recorded(&s["available_kv_bytes"]);
        st.pending_hop_queue_depth = recorded(&s["pending_hop_queue_depth"]);
        states.insert(st.node_id.clone(), st).expect("hashable");
    }
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
    inputs.device_states = states;
    inputs.workload_profiles = setup["profiles"]
        .as_array()
        .expect("profiles")
        .iter()
        .map(|p| {
            (
                p[0].as_str().expect("id").to_owned(),
                p[1].as_str().expect("qos").to_owned(),
            )
        })
        .collect();
    inputs.max_concurrent_requests = (!setup["max_concurrent_requests"].is_null())
        .then(|| recorded(&setup["max_concurrent_requests"]));
    let built = M16RuntimeCoordinator::new(inputs);
    Ok((rig, built))
}

/// Runs one op; the result as the probe records it, or the port's error.
fn run(replay: &Replay, op: &Value) -> Result<Value, CoordinatorError> {
    let c = &replay.coordinator;
    let id = || op["request_id"].as_str().expect("request id");
    Ok(match op["op"].as_str().expect("op") {
        "admit" => {
            let excluded: Vec<Value> = op["excluded"].as_array().expect("excluded").clone();
            let m = c.admit(
                &request_from(&op["request"]),
                op["profile"].as_str().expect("profile"),
                &excluded,
            )?;
            json!({
                "manifest_json": canonical_text(&path_manifest_to_dict(&m).expect("finite")),
                "manifest_digest": path_manifest_document_digest(&m).expect("a digest"),
                "expires_at": m.ordered_hops.iter().map(|h| tag_f64(h.reservation_expires_at)).collect::<Vec<_>>(),
            })
        }
        "next_dispatch" => c.next_dispatch()?.map_or(Value::Null, Value::String),
        "mark_phase" => {
            c.mark_phase(id(), op["phase"].as_str().expect("phase"))?;
            Value::Null
        }
        "complete" => {
            c.complete(id(), op["state"].as_str().expect("state"))?;
            Value::Null
        }
        "retire" => {
            c.retire_dispatch_slot(id());
            Value::Null
        }
        "cancel" => Value::Bool(c.cancel(id())),
        "phase" => c.phase(id()).map_or(Value::Null, Value::String),
        "terminal_state" => c.terminal_state(id()).map_or(Value::Null, Value::String),
        "request_context" => request_doc(
            &c.request_context(id())
                .map_err(CoordinatorError::Admission)?,
        ),
        "route_identity" => Value::String(canonical_text(&identity_doc(
            &c.route_identity(id())
                .map_err(CoordinatorError::Admission)?,
        ))),
        "path_manifest" => Value::String(canonical_text(
            &c.path_manifest(id()).map_err(CoordinatorError::Admission)?,
        )),
        "set_clock" => {
            let value = recorded_f64(&op["value"]);
            match op["clock"].as_str().expect("clock") {
                "clock" => replay.rig.clock.set(value),
                _ => replay.rig.lease.as_ref().expect("a lease clock").set(value),
            }
            Value::Null
        }
        "state" => snapshot(c),
        other => panic!("unmodelled op {other}"),
    })
}

fn recorded_reads(list: &Value) -> Vec<(String, u64)> {
    list.as_array()
        .expect("reads")
        .iter()
        .map(|r| {
            (
                r[0].as_str().expect("clock name").to_owned(),
                recorded_f64(&r[1]).to_bits(),
            )
        })
        .collect()
}

fn port_reads(rig: &Rig) -> (Vec<(String, u64)>, Vec<String>) {
    let (reads, ids) = rig.take();
    (
        reads.into_iter().map(|(n, b)| (n.to_owned(), b)).collect(),
        ids,
    )
}

#[test]
fn m16_coordinator_python_value_domain_parity() {
    let oracle: Value = serde_json::from_str(ORACLE).expect("the fixture");
    let provenance = &oracle["provenance"];
    assert_eq!(
        provenance["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let cases = oracle["cases"].as_array().expect("cases");
    let (mut ops, mut declared) = (0, 0);
    for case in cases {
        let name = case["name"].as_str().expect("name");
        let construct_record = &case["construct"];
        let (rig, built) = match construct(&case["setup"]) {
            Ok(built) => built,
            // Declared differences, both refusals before a coordinator holds the graph: the
            // oracle's json.loads keeps a lone surrogate and its `_digest` refuses it at
            // construction (UnicodeEncodeError), where serde_json refuses the text; the
            // oracle's graph check takes `hidden_size` 1e400 (inf > 0) and its `_digest`
            // refuses the inf (ValueError), where `execution_graph::parse` already refuses it.
            Err(unbuilt) => {
                let (kind, message) = match unbuilt {
                    Unbuilt::Json => ("UnicodeEncodeError", "'utf-8' codec can't encode"),
                    Unbuilt::Graph => (
                        "ValueError",
                        "Out of range float values are not JSON compliant",
                    ),
                };
                let error = &construct_record["error"];
                assert_eq!(error["type"], kind, "{name}");
                assert!(
                    error["message"]
                        .as_str()
                        .is_some_and(|text| text.starts_with(message)),
                    "{name}"
                );
                declared += 1;
                continue;
            }
        };
        assert_eq!(
            port_reads(&rig),
            (recorded_reads(&case["construct_reads"]), vec![]),
            "{name}"
        );
        let coordinator = match built {
            Err(error) => {
                assert_error(name, &construct_record["error"], &error);
                continue;
            }
            Ok(coordinator) => coordinator,
        };
        assert_eq!(
            coordinator.graph_digest(),
            construct_record["graph_digest"],
            "{name}"
        );
        assert_eq!(
            coordinator.maximum_concurrent_requests(),
            construct_record["maximum_concurrent_requests"]
                .as_u64()
                .expect("a count"),
            "{name}"
        );
        let replay = Replay { rig, coordinator };
        let results = case["results"].as_array().expect("results");
        for (k, (op, record)) in case["ops"]
            .as_array()
            .expect("ops")
            .iter()
            .zip(results)
            .enumerate()
        {
            let label = format!("{name} op {k} {}", op["op"]);
            match run(&replay, op) {
                Ok(value) if op["op"] == "state" => assert_eq!(
                    value,
                    recorded_snapshot(&record["result"], &replay.coordinator, &label),
                    "{label}"
                ),
                Ok(value) => assert_eq!(value, bits_only(&record["result"]), "{label}"),
                Err(error) => assert_error(&label, &record["error"], &error),
            }
            let (reads, drawn) = port_reads(&replay.rig);
            assert_eq!(reads, recorded_reads(&record["reads"]), "{label}");
            assert_eq!(Value::from(drawn), record["ids"], "{label}");
            ops += 1;
        }
    }
    // Standalone `_digest` probes.
    let digests = oracle["digests"].as_array().expect("digests");
    for (k, probe) in digests.iter().enumerate() {
        let text = probe["text"].as_str().expect("text");
        let Ok(value) = serde_json::from_str::<Value>(text) else {
            assert_eq!(probe["error"]["type"], "UnicodeEncodeError", "digest {k}");
            declared += 1;
            continue;
        };
        match digest_document(&value) {
            Ok(digest) => {
                assert_eq!(digest, probe["digest"], "digest {k}");
                assert_eq!(canonical_text(&value), probe["canonical"], "digest {k}");
            }
            Err(error) => assert_error(
                &format!("digest {k}"),
                &probe["error"],
                &CoordinatorError::Digest(error),
            ),
        }
    }
    assert_eq!(
        cases.len() as u64,
        provenance["case_count"].as_u64().expect("count")
    );
    assert_eq!(ops, provenance["op_count"].as_u64().expect("count"));
    assert_eq!(
        digests.len() as u64,
        provenance["digest_count"].as_u64().expect("count")
    );
    assert_eq!(
        (cases.len(), ops, digests.len(), declared),
        (49, 194, 15, 5)
    );
}
