// SPDX-License-Identifier: AGPL-3.0-only
//! Controlled execution at the real request service boundary; not native/MLX evidence.
use api::{
    m16_requests::{Dispatch, Execution, Executor, Host, M16Requests, Prepared},
    registry::Config,
    requests::Service,
    schema::PublicError,
};
use budgets::{BudgetId, Entries};
use protocol::execution_graph;
use runtime::{
    m16_coordinator::{SharedClock, SharedIdSource},
    m16_factory::{DeviceFacts, FactoryInputs, build_live_m16_runtime},
    m16_ledger::{IdSource, LeaseClock},
    m16_router_contracts::{DeviceState, IdMap, PyReal, RequestContext, RouterConfig},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};

fn latency(id: BudgetId) -> PyReal {
    PyReal::from_json(&json!(id.span(Entries::new(0)).unwrap().millis())).unwrap()
}
struct Clock;
impl LeaseClock for Clock {
    fn now(&self) -> f64 {
        0.0
    }
}
#[derive(Default)]
struct Ids(AtomicU64);
impl IdSource for Ids {
    fn new_id(&self, prefix: &str) -> String {
        format!("{prefix}-{}", self.0.fetch_add(1, Ordering::SeqCst))
    }
}
fn digest(text: &str) -> String {
    protocol::canonical::sha256_prefixed(text.as_bytes())
}
fn graph() -> execution_graph::Graph {
    execution_graph::parse(&json!({
        "protocol":"mycelium.execution_graph.v1", "deployment_id":"deployment", "deployment_epoch":1,"topology_version":1,
        "model_id":"model","resolved_commit":"commit","manifest_digest":digest("model"),
        "entry_stage_id":"stage","final_stage_id":"stage","hidden_size":8,"activation_bytes":2,"token_envelope_bytes":16,
        "stages":[{"stage_id":"stage","range":{"start_layer":0,"end_layer_exclusive":1,"layer_count":1},"component_roles":["decoder"],
        "stage_cost":{"prefill_work_units_per_prompt_token":2,"decode_work_units_per_token":1,"kv_bytes_per_context_token":256},
        "placements":[{"placement_id":"placement","node_id":"node","replica_group_id":"group","assignment_id":"assignment","stage_signature":"signature","load_proof_digest":digest("controlled-load"),"runtime_backend":"controlled","runtime_endpoint":"local://controlled"}]}],
        "edges":[],"loopback_edges":[{"edge_id":"loop","from_placement_id":"placement","to_placement_id":"placement","link_id":"link"}]
    })).unwrap()
}
fn inputs() -> FactoryInputs {
    FactoryInputs {
        graph: graph(),
        devices: BTreeMap::from([(
            "node".into(),
            DeviceFacts {
                fast_allocatable_bytes: 2_097_152,
                state: DeviceState {
                    node_id: json!("node"),
                    state_seq: PyReal::int(1),
                    last_updated: PyReal::Float(0.0),
                    availability: "ALIVE".into(),
                    compute_units_per_second: PyReal::int(1024),
                    free_compute_fraction: PyReal::int(1),
                    available_kv_bytes: PyReal::int(1_048_576),
                    pending_hop_queue_depth: PyReal::int(0),
                    neighbor_rtt_ms: IdMap::new(),
                    neighbor_bandwidth_bytes_per_second: IdMap::new(),
                },
            },
        )]),
        workload_profiles: BTreeMap::from([("interactive".into(), "interactive".into())]),
        clock: Arc::new(Clock) as SharedClock,
        lease_clock: Arc::new(Clock) as SharedClock,
        id_source: Arc::new(Ids::default()) as SharedIdSource,
        config: RouterConfig::oracle_default().unwrap(),
        max_concurrent_requests: 1,
    }
}
struct Authority {
    ready: AtomicBool,
    ids: Ids,
}
impl Host for Authority {
    fn authorized(&self, r: &api::http::Request) -> bool {
        r.header("authorization") == Some("Bearer controlled")
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        if !self.ready.load(Ordering::SeqCst) {
            return Err(PublicError::new(409, "stale_qualification"));
        }
        Ok(qualification())
    }
    fn prepare(&self, doc: &Value) -> Result<Prepared, PublicError> {
        Ok(Prepared {
            context: RequestContext {
                request_id: self.ids.new_id("request"),
                prompt_token_ids: vec![PyReal::int(7)],
                max_new_tokens: PyReal::from_json(&doc["max_new_tokens"]).unwrap(),
                expected_new_tokens: PyReal::int(1),
                qos_class: "interactive".into(),
                admitted_at: PyReal::Float(0.0),
                target_ttft_ms: latency(
                    BudgetId::PyFinalMyceliumRequestGatewayBackendTargetTtftMsL234C12,
                ),
                target_tpot_ms: latency(
                    BudgetId::PyFinalMyceliumRequestGatewayBackendTargetTpotMsL235C12,
                ),
                target_tokens_per_second: PyReal::int(1),
                sampling_seed: PyReal::int(0),
                generation_config_digest: digest("generation"),
            },
            owner_token: "s".repeat(32),
            excluded_placements: vec![],
        })
    }
}
fn qualification() -> Value {
    json!({"qualification_id":"controlled-qualification","qualification_digest":digest("qualification"),"deployment_id":"deployment","deployment_epoch":1,"topology_version":1,"model_id":"model","resolved_commit":"commit","manifest_digest":digest("model"),"path_manifest_digest":digest("qualified-route"),"stage_load_proof_digests":[digest("controlled-load")]})
}
fn document() -> Value {
    json!({"protocol":"mycelium.request_gateway.v2","prompt":"test","max_new_tokens":4095,"workload_profile_id":"interactive","qos_class":"interactive","qualification":qualification()})
}
#[derive(Default)]
struct Control {
    dispatched: Vec<Dispatch>,
    polls: usize,
    cleanup_calls: usize,
    clean: bool,
    refuse: bool,
    fail_start: bool,
    cleanup_cancels: Vec<bool>,
    cleanup_requests: Vec<String>,
    owner_refusals: BTreeMap<String, &'static str>,
}
struct Controlled(Arc<Mutex<Control>>);
impl Executor for Controlled {
    fn start(&mut self, d: &Dispatch) -> Result<(), PublicError> {
        let mut c = self.0.lock().unwrap();
        c.dispatched.push(d.clone());
        if c.fail_start {
            Err(PublicError::new(503, "controlled_start_failed"))
        } else {
            Ok(())
        }
    }
    fn poll(&mut self, _: &str) -> Result<Execution, PublicError> {
        let mut c = self.0.lock().unwrap();
        c.polls += 1;
        Ok(if c.polls == 1 {
            Execution::Token {
                token_id: 9,
                text: "hello".into(),
            }
        } else {
            Execution::Completed
        })
    }
    fn cleanup(&mut self, id: &str, cancel: bool) -> Result<bool, PublicError> {
        let mut c = self.0.lock().unwrap();
        c.cleanup_calls += 1;
        c.cleanup_cancels.push(cancel);
        c.cleanup_requests.push(id.to_owned());
        if let Some(code) = c.owner_refusals.get(id) {
            return Err(PublicError::new(503, code));
        }
        if c.refuse {
            Err(PublicError::new(503, "controlled_cleanup_refused"))
        } else {
            Ok(c.clean)
        }
    }
}
fn fixture() -> (M16Requests, Arc<Mutex<Control>>, Arc<Authority>) {
    fixture_with(inputs(), 16)
}
fn fixture_with(
    i: FactoryInputs,
    max_sessions: usize,
) -> (M16Requests, Arc<Mutex<Control>>, Arc<Authority>) {
    let control = Arc::new(Mutex::new(Control::default()));
    let host = Arc::new(Authority {
        ready: AtomicBool::new(true),
        ids: Ids::default(),
    });
    let service = M16Requests::new(
        build_live_m16_runtime(i).unwrap(),
        host.clone(),
        Box::new(Controlled(control.clone())),
        Config::new(16, max_sessions, 0.0).unwrap(),
    )
    .unwrap();
    (service, control, host)
}
#[test]
fn admitted_request_dispatches_and_releases_its_reservations() {
    let (s, c, _) = fixture();
    let baseline = s.placement_status().unwrap();
    let a = s.submit(document()).unwrap();
    let identity = s.route_identity(&a.request_id).unwrap();
    assert!(!s.reservations(&a.request_id).unwrap().is_empty());
    s.tick().unwrap();
    let dispatched = c.lock().unwrap().dispatched.clone();
    assert_eq!(dispatched.len(), 1);
    assert_eq!(dispatched[0].identity, identity);
    assert_eq!(dispatched[0].context.request_id, a.request_id);
    assert_eq!(dispatched[0].manifest["path_id"], identity.path_id);
    s.tick().unwrap();
    s.tick().unwrap();
    assert_eq!(s.terminal(&a.request_id).unwrap(), None);
    assert!(!s.reservations(&a.request_id).unwrap().is_empty());
    c.lock().unwrap().clean = true;
    s.tick().unwrap();
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("completed")
    );
    assert!(s.reservations(&a.request_id).unwrap().is_empty());
    assert_eq!(s.placement_status().unwrap(), baseline);
    let mut stream = s
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    for (sequence, kind) in ["accepted", "lifecycle", "token", "lifecycle", "completed"]
        .iter()
        .enumerate()
    {
        let e = stream.next().unwrap().unwrap();
        assert_eq!(e["sequence"], sequence);
        assert_eq!(e["type"], *kind);
        s.validate_event(&e).unwrap();
        stream.ack(sequence as u64).unwrap();
    }
    assert!(stream.next().unwrap().is_none());
    stream.close().unwrap();
    assert!(!s.cancel(&a.request_id, Some(&a.session_token)).unwrap());
}

#[test]
fn stale_qualification_and_exhausted_capacity_refuse_without_dispatch() {
    let (s, c, h) = fixture();
    let baseline = s.placement_status().unwrap();
    let mut stale = document();
    stale["qualification"]["qualification_id"] = json!("old");
    assert_eq!(s.submit(stale).err().unwrap().code, "stale_qualification");
    h.ready.store(false, Ordering::SeqCst);
    assert_eq!(
        s.submit(document()).err().unwrap().code,
        "stale_qualification"
    );
    assert_eq!(s.placement_status().unwrap(), baseline);
    h.ready.store(true, Ordering::SeqCst);
    let a = s.submit(document()).unwrap();
    let held = s.placement_status().unwrap();
    assert_eq!(
        s.submit(document()).err().unwrap().code,
        "resource_unavailable"
    );
    assert_eq!(s.placement_status().unwrap(), held);
    assert!(c.lock().unwrap().dispatched.is_empty());
    s.cancel(&a.request_id, Some(&a.session_token)).unwrap();
    assert_eq!(s.placement_status().unwrap(), baseline);
}
#[test]
fn cancelled_pending_request_releases_without_touching_executor() {
    let (s, c, _) = fixture();
    let baseline = s.placement_status().unwrap();
    let a = s.submit(document()).unwrap();
    assert_eq!(
        s.cancel(&a.request_id, Some(&"x".repeat(32)))
            .unwrap_err()
            .code,
        "session_owner_mismatch"
    );
    assert!(!s.reservations(&a.request_id).unwrap().is_empty());
    assert!(s.cancel(&a.request_id, Some(&a.session_token)).unwrap());
    s.tick().unwrap();
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("cancelled")
    );
    assert_eq!(s.placement_status().unwrap(), baseline);
    assert!(c.lock().unwrap().dispatched.is_empty());
    assert_eq!(c.lock().unwrap().cleanup_calls, 0);
    let b = s.submit(document()).unwrap();
    s.cancel(&b.request_id, Some(&b.session_token)).unwrap();
}
#[test]
fn cleanup_refusal_keeps_reservations_and_retryable_debt_visible() {
    let (s, c, _) = fixture();
    let baseline = s.placement_status().unwrap();
    let a = s.submit(document()).unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    let held = s.placement_status().unwrap();
    c.lock().unwrap().refuse = true;
    assert_eq!(s.tick().unwrap_err().code, "controlled_cleanup_refused");
    assert_eq!(
        s.cleanup_debt(&a.request_id).unwrap(),
        Some("controlled_cleanup_refused")
    );
    assert_eq!(s.terminal(&a.request_id).unwrap(), None);
    assert_eq!(s.placement_status().unwrap(), held);
    assert_eq!(
        s.submit(document()).err().unwrap().code,
        "resource_unavailable"
    );
    {
        let mut c = c.lock().unwrap();
        c.refuse = false;
        c.clean = true;
    }
    s.tick().unwrap();
    assert_eq!(s.cleanup_debt(&a.request_id).unwrap(), None);
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("completed")
    );
    assert_eq!(s.placement_status().unwrap(), baseline);
}
#[test]
fn active_cancel_waits_for_cleanup_and_never_emits_more_tokens() {
    let (s, c, _) = fixture();
    let baseline = s.placement_status().unwrap();
    let a = s.submit(document()).unwrap();
    s.tick().unwrap();
    s.cancel(&a.request_id, Some(&a.session_token)).unwrap();
    s.tick().unwrap();
    assert_eq!(s.terminal(&a.request_id).unwrap(), None);
    assert_eq!(c.lock().unwrap().polls, 0);
    assert!(!s.reservations(&a.request_id).unwrap().is_empty());
    c.lock().unwrap().clean = true;
    s.tick().unwrap();
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("cancelled")
    );
    assert_eq!(s.placement_status().unwrap(), baseline);
}
#[test]
fn qualification_revocation_before_dispatch_and_before_completion_fails_closed() {
    for dispatched in [false, true] {
        let (s, c, h) = fixture();
        let baseline = s.placement_status().unwrap();
        let a = s.submit(document()).unwrap();
        if dispatched {
            s.tick().unwrap();
            s.tick().unwrap();
            s.tick().unwrap();
        }
        h.ready.store(false, Ordering::SeqCst);
        c.lock().unwrap().clean = true;
        s.tick().unwrap();
        assert_eq!(
            s.terminal(&a.request_id).unwrap().as_deref(),
            Some("failed")
        );
        assert_eq!(c.lock().unwrap().dispatched.len(), usize::from(dispatched));
        assert_eq!(s.placement_status().unwrap(), baseline);
    }
}

#[test]
fn start_failure_and_active_revocation_stop_partial_work_before_releasing() {
    for start_failure in [true, false] {
        let (s, c, h) = fixture();
        let baseline = s.placement_status().unwrap();
        c.lock().unwrap().fail_start = start_failure;
        let a = s.submit(document()).unwrap();
        s.tick().unwrap();
        if !start_failure {
            h.ready.store(false, Ordering::SeqCst);
            s.tick().unwrap();
        }
        assert_eq!(s.terminal(&a.request_id).unwrap(), None);
        assert!(!s.reservations(&a.request_id).unwrap().is_empty());
        c.lock().unwrap().clean = true;
        s.tick().unwrap();
        assert_eq!(c.lock().unwrap().cleanup_cancels, vec![true]);
        assert_eq!(
            s.terminal(&a.request_id).unwrap().as_deref(),
            Some("failed")
        );
        assert_eq!(s.placement_status().unwrap(), baseline);
    }
}

fn http(s: M16Requests, method: &str, path: &str, owner: Option<&str>, body: &str) -> String {
    use std::{
        io::{Read, Write},
        net::{Ipv4Addr, TcpListener, TcpStream},
    };
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    // Connect before spawn so the accept cannot wait on a test-side assertion.
    let mut client = TcpStream::connect(addr).unwrap();
    let timeout = journey_tests::duration(budgets::BudgetId::ModelSelection).unwrap();
    client.set_read_timeout(Some(timeout)).unwrap();
    client.set_write_timeout(Some(timeout)).unwrap();
    let job = std::thread::spawn(move || {
        let gateway = api::requests::Gateway::new(s);
        let (stream, _) = listener.accept().unwrap();
        api::http::serve_response_connection(
            stream,
            timeout,
            api::requests::MAX_REQUEST_BODY,
            |r| gateway.preflight(r),
            |r| gateway.handle(r),
        )
        .unwrap();
    });
    let owner = owner
        .map(|v| format!("X-Mycelium-Session: {v}\r\n"))
        .unwrap_or_default();
    write!(client,"{method} {path} HTTP/1.1\r\nHost: {addr}\r\nAuthorization: Bearer controlled\r\n{owner}Content-Length: {}\r\n\r\n{body}",body.len()).unwrap();
    client.shutdown(std::net::Shutdown::Write).unwrap();
    let mut response = String::new();
    client.read_to_string(&mut response).unwrap();
    job.join().unwrap();
    response
}
#[test]
fn repeated_http_delete_stays_cancelling_until_cleanup_is_proven() {
    let (s, c, _) = fixture();
    let baseline = s.placement_status().unwrap();
    let a = s.submit(document()).unwrap();
    s.tick().unwrap();
    let path = format!("/v1/inference/{}", a.request_id);
    for refused in [false, true] {
        c.lock().unwrap().refuse = refused;
        for _ in 0..2 {
            let response = http(s.clone(), "DELETE", &path, Some(&a.session_token), "");
            assert!(response.starts_with("HTTP/1.1 202"), "{response}");
            let body: Value =
                serde_json::from_str(response.split_once("\r\n\r\n").unwrap().1).unwrap();
            assert_eq!(body["status"], "cancelling");
            if refused {
                assert_eq!(s.tick().unwrap_err().code, "controlled_cleanup_refused");
            } else {
                s.tick().unwrap();
            }
            assert_eq!(s.terminal(&a.request_id).unwrap(), None);
            assert!(!s.reservations(&a.request_id).unwrap().is_empty());
        }
    }
    {
        let mut control = c.lock().unwrap();
        control.refuse = false;
        control.clean = true;
    }
    s.tick().unwrap();
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("cancelled")
    );
    assert_eq!(s.placement_status().unwrap(), baseline);
    let response = http(s, "DELETE", &path, Some(&a.session_token), "");
    assert!(response.starts_with("HTTP/1.1 200"), "{response}");
    let body: Value = serde_json::from_str(response.split_once("\r\n\r\n").unwrap().1).unwrap();
    assert_eq!(body["status"], "terminal");
}

#[test]
fn rejected_submissions_exhaust_a_lifetime_attempt_budget_without_growing_incidents() {
    let limit = 4;
    let (s, c, _) = fixture_with(inputs(), limit);
    let baseline = s.placement_status().unwrap();
    let a = s.submit(document()).unwrap();
    for _ in 1..limit {
        assert_eq!(
            s.submit(document()).err().unwrap().code,
            "resource_unavailable"
        );
    }
    let incidents = s.incidents().unwrap();
    assert_eq!(incidents.len(), limit - 1);
    let held = s.placement_status().unwrap();
    let refusals: Vec<_> = (0..32)
        .map(|_| s.submit(document()).err().unwrap().code)
        .collect();
    assert_eq!(
        s.incidents().unwrap(),
        incidents,
        "rejected submissions must not grow retained coordinator history"
    );
    assert!(
        refusals
            .iter()
            .all(|code| *code == "m16_admission_attempt_budget_exhausted")
    );
    assert_eq!(s.placement_status().unwrap(), held);
    s.cancel(&a.request_id, Some(&a.session_token)).unwrap();
    assert_eq!(s.placement_status().unwrap(), baseline);
    let settled_incidents = s.incidents().unwrap();
    assert_eq!(
        s.submit(document()).err().unwrap().code,
        "m16_admission_attempt_budget_exhausted"
    );
    assert_eq!(s.incidents().unwrap(), settled_incidents);
    assert!(c.lock().unwrap().dispatched.is_empty());
}

#[test]
fn one_cleanup_refusal_does_not_starve_another_cancelled_owner() {
    let mut i = inputs();
    i.max_concurrent_requests = 2;
    let (s, c, _) = fixture_with(i, 16);
    let baseline = s.placement_status().unwrap();
    let mut doc = document();
    doc["max_new_tokens"] = json!(1);
    let a = s.submit(doc.clone()).unwrap();
    let b = s.submit(doc).unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    assert_eq!(c.lock().unwrap().dispatched.len(), 2);
    s.cancel(&a.request_id, Some(&a.session_token)).unwrap();
    s.cancel(&b.request_id, Some(&b.session_token)).unwrap();
    {
        let mut control = c.lock().unwrap();
        control.clean = true;
        control
            .owner_refusals
            .insert(a.request_id.clone(), "owner_a_cleanup_refused");
    }
    assert_eq!(s.tick().unwrap_err().code, "owner_a_cleanup_refused");
    assert_eq!(
        s.terminal(&b.request_id).unwrap().as_deref(),
        Some("cancelled")
    );
    assert!(s.reservations(&b.request_id).unwrap().is_empty());
    assert_eq!(s.cleanup_debt(&b.request_id).unwrap(), None);
    assert_eq!(s.terminal(&a.request_id).unwrap(), None);
    assert!(!s.reservations(&a.request_id).unwrap().is_empty());
    assert_eq!(
        s.cleanup_debt(&a.request_id).unwrap(),
        Some("owner_a_cleanup_refused")
    );
    assert_eq!(
        c.lock().unwrap().cleanup_requests,
        vec![a.request_id.clone(), b.request_id.clone()]
    );
    assert_eq!(c.lock().unwrap().cleanup_cancels, vec![true, true]);
    c.lock().unwrap().owner_refusals.clear();
    s.tick().unwrap();
    assert_eq!(s.placement_status().unwrap(), baseline);
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("cancelled")
    );
}

#[test]
fn multiple_cleanup_refusals_are_all_retained_and_reported() {
    let mut i = inputs();
    i.max_concurrent_requests = 2;
    let (s, c, _) = fixture_with(i, 16);
    let mut doc = document();
    doc["max_new_tokens"] = json!(1);
    let a = s.submit(doc.clone()).unwrap();
    let b = s.submit(doc).unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    for (id, token, code) in [
        (&a.request_id, &a.session_token, "owner_a_cleanup_refused"),
        (&b.request_id, &b.session_token, "owner_b_cleanup_refused"),
    ] {
        s.cancel(id, Some(token)).unwrap();
        c.lock().unwrap().owner_refusals.insert(id.clone(), code);
    }
    let refusal = s.tick().unwrap_err();
    assert_eq!(
        s.cleanup_debt(&a.request_id).unwrap(),
        Some("owner_a_cleanup_refused")
    );
    assert_eq!(
        s.cleanup_debt(&b.request_id).unwrap(),
        Some("owner_b_cleanup_refused")
    );
    assert_eq!(refusal.code, "m16_multiple_progress_refusals");
    assert_eq!(s.terminal(&a.request_id).unwrap(), None);
    assert_eq!(s.terminal(&b.request_id).unwrap(), None);
    assert!(!s.reservations(&a.request_id).unwrap().is_empty());
    assert!(!s.reservations(&b.request_id).unwrap().is_empty());
    {
        let mut control = c.lock().unwrap();
        control.clean = true;
        control.owner_refusals.clear();
    }
    s.tick().unwrap();
    assert_eq!(s.cleanup_debt(&a.request_id).unwrap(), None);
    assert_eq!(s.cleanup_debt(&b.request_id).unwrap(), None);
}

#[derive(Default)]
struct ScriptClock(Mutex<VecDeque<f64>>);
impl LeaseClock for ScriptClock {
    fn now(&self) -> f64 {
        self.0.lock().unwrap().pop_front().unwrap_or(0.0)
    }
}
#[test]
fn publication_clock_fault_retries_only_unfinished_work() {
    let clock = Arc::new(ScriptClock::default());
    let mut i = inputs();
    i.clock = clock.clone();
    let (s, c, _) = fixture_with(i, 16);
    let baseline = s.placement_status().unwrap();
    let a = s.submit(document()).unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    c.lock().unwrap().clean = true;
    // complete() reads the finite timestamp, then terminal publication reads NaN.
    *clock.0.lock().unwrap() = VecDeque::from([0.0, f64::NAN]);
    assert_eq!(s.tick().unwrap_err().code, "m16_clock_invalid");
    assert!(clock.0.lock().unwrap().is_empty());
    assert_eq!(s.terminal(&a.request_id).unwrap(), None);
    assert_eq!(s.placement_status().unwrap(), baseline);
    assert!(s.reservations(&a.request_id).unwrap().is_empty());
    assert_eq!(c.lock().unwrap().cleanup_calls, 1);
    s.tick().unwrap();
    s.tick().unwrap();
    assert_eq!(c.lock().unwrap().cleanup_calls, 1);
    assert_eq!(s.cleanup_debt(&a.request_id).unwrap(), None);
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("completed")
    );
    assert_eq!(s.placement_status().unwrap(), baseline);
    let mut stream = s
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    let mut terminals = 0;
    while let Some(event) = stream.next().unwrap() {
        if event["type"] == "completed" {
            terminals += 1;
        }
        stream.ack(event["sequence"].as_u64().unwrap()).unwrap();
    }
    assert_eq!(terminals, 1);
    stream.close().unwrap();
}

#[test]
fn queued_cancellation_publication_fault_preserves_the_settled_outcome() {
    let clock = Arc::new(ScriptClock::default());
    let mut i = inputs();
    i.clock = clock.clone();
    let (s, c, _) = fixture_with(i, 16);
    let baseline = s.placement_status().unwrap();
    let a = s.submit(document()).unwrap();
    // Queued cancel timestamps settlement and its incident before publication.
    *clock.0.lock().unwrap() = VecDeque::from([0.0, 0.0, f64::NAN]);
    assert_eq!(
        s.cancel(&a.request_id, Some(&a.session_token))
            .unwrap_err()
            .code,
        "m16_clock_invalid"
    );
    assert!(clock.0.lock().unwrap().is_empty());
    assert_eq!(s.terminal(&a.request_id).unwrap(), None);
    assert_eq!(s.placement_status().unwrap(), baseline);
    let incidents = s.incidents().unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("cancelled")
    );
    assert_eq!(s.incidents().unwrap(), incidents);
    assert_eq!(c.lock().unwrap().cleanup_calls, 0);
    assert!(c.lock().unwrap().dispatched.is_empty());
    assert!(!s.cancel(&a.request_id, Some(&a.session_token)).unwrap());
}

#[test]
fn cancellation_during_publication_retry_cannot_rewrite_settlement() {
    let clock = Arc::new(ScriptClock::default());
    let mut i = inputs();
    i.clock = clock.clone();
    let (s, c, _) = fixture_with(i, 16);
    let a = s.submit(document()).unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    s.tick().unwrap();
    c.lock().unwrap().clean = true;
    *clock.0.lock().unwrap() = VecDeque::from([0.0, f64::NAN]);
    assert_eq!(s.tick().unwrap_err().code, "m16_clock_invalid");
    assert!(s.cancel(&a.request_id, Some(&a.session_token)).unwrap());
    assert!(s.cancel(&a.request_id, Some(&a.session_token)).unwrap());
    s.tick().unwrap();
    assert_eq!(
        s.terminal(&a.request_id).unwrap().as_deref(),
        Some("completed")
    );
    assert_eq!(c.lock().unwrap().cleanup_calls, 1);
    assert_eq!(c.lock().unwrap().cleanup_cancels, vec![false]);
}

#[test]
fn loopback_http_admission_stream_and_cancel_use_the_production_adapter() {
    let (s, c, _) = fixture();
    let baseline = s.placement_status().unwrap();
    let response = http(
        s.clone(),
        "POST",
        "/v1/inference",
        None,
        &document().to_string(),
    );
    assert!(response.starts_with("HTTP/1.1 202"), "{response}");
    let a: Value = serde_json::from_str(response.split_once("\r\n\r\n").unwrap().1).unwrap();
    let id = a["request_id"].as_str().unwrap();
    let token = a["session_token"].as_str().unwrap();
    s.tick().unwrap();
    let denied = http(
        s.clone(),
        "DELETE",
        a["cancel_path"].as_str().unwrap(),
        Some(&"x".repeat(32)),
        "",
    );
    assert!(denied.starts_with("HTTP/1.1 409"));
    let cancelled = http(
        s.clone(),
        "DELETE",
        a["cancel_path"].as_str().unwrap(),
        Some(token),
        "",
    );
    assert!(cancelled.starts_with("HTTP/1.1 202"));
    assert_eq!(s.terminal(id).unwrap(), None);
    c.lock().unwrap().clean = true;
    s.tick().unwrap();
    let stream = http(
        s.clone(),
        "GET",
        a["stream_path"].as_str().unwrap(),
        Some(token),
        "",
    );
    assert!(stream.starts_with("HTTP/1.1 200"));
    let events: Vec<Value> = stream
        .lines()
        .filter_map(|line| line.strip_prefix("data: "))
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(
        events
            .iter()
            .map(|e| e["type"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["accepted", "lifecycle", "lifecycle", "cancelled"]
    );
    assert_eq!(s.placement_status().unwrap(), baseline);
    assert_eq!(c.lock().unwrap().dispatched[0].context.request_id, id);
}
