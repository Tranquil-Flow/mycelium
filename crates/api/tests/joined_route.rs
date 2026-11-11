// SPDX-License-Identifier: AGPL-3.0-only
//! Joined-route producer and Router hop contract. The model-free cases use a
//! CONTROLLED route message shape (no signature, no native proof) to exercise the
//! graph/qualification producer, M16 factory admission and frame bindings; they
//! are NOT route qualification. The real two-process run is the ignored test in
//! support/joined_route_process.rs.
use api::app_host;
use api::sse_client;
use api::{
    joined_shard::route::{self, Path},
    m16_requests::{Executor, Host, M16Requests},
    registry::Config,
    requests::Service,
    schema::PublicError,
};
use protocol::router_wire;
use runtime::m16_ledger::{IdSource, LeaseClock};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};

fn span(id: budgets::BudgetId) -> std::time::Duration {
    std::time::Duration::from_millis(id.span(budgets::Entries::ZERO).unwrap().millis())
}
/// Test pump/accept poll interval (typed start-poll row).
fn poll() -> std::time::Duration {
    span(budgets::BudgetId::PyPhysicalInferenceNodePhysicalnodeserviceStartPollIntervalSecondsL3528)
}
fn digest(text: &str) -> String {
    protocol::canonical::sha256_prefixed(text.as_bytes())
}
fn controlled_message() -> Value {
    let stage = |i: u64, node: &str, start: u64, end: u64| {
        json!({"stage_index":i,"placement_id":format!("{node}:{start}-{end}"),
            "member":{"node_id":node,"generation":1},"carrier_endpoint_id":format!("endpoint-{i}"),
            "run":format!("run-{i}"),"offer_digest":digest(&format!("offer-{i}")),
            "proof_digest":digest(&format!("proof-{i}")),"range":[start,end],"worker_pid":100+i,
            "tokens":99,"memory":4_000_000_000u64})
    };
    json!({"protocol":route::ROUTE,"swarm_id":"swarm","deployment_id":"12345678-1234-5678-9234-abcdefabcdef",
        "deployment_epoch":7,"topology_version":7,"model_id":"Qwen/Qwen3-0.6B",
        "resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca","manifest_digest":digest("manifest"),
        "owner_endpoint_id":"owner","stages":[stage(0,"node-a",0,14),stage(1,"node-b",14,28)],
        "issued_at":0.0,"expires_at":1.0e12,"route_ready":false,"scope":route::SCOPE})
}
struct Clock;
impl LeaseClock for Clock {
    fn now(&self) -> f64 {
        app_host::now()
    }
}
#[derive(Default)]
struct Ids(AtomicU64);
impl IdSource for Ids {
    fn new_id(&self, prefix: &str) -> String {
        format!("{prefix}-{}", self.0.fetch_add(1, Ordering::SeqCst))
    }
}
struct Controlled(Value);
impl Host for Controlled {
    fn authorized(&self, _: &api::http::Request) -> bool {
        true
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        route::qualification(&self.0).map_err(|e| PublicError::new(409, e.0))
    }
    fn prepare(&self, doc: &Value) -> Result<api::m16_requests::Prepared, PublicError> {
        use runtime::m16_router_contracts::{PyReal, RequestContext};
        let latency = |id: budgets::BudgetId| {
            PyReal::from_json(&json!(id.span(budgets::Entries::ZERO).unwrap().millis())).unwrap()
        };
        // Controlled context of the real fixed-prompt size (35 ids, 32 new tokens).
        Ok(api::m16_requests::Prepared {
            context: RequestContext {
                request_id: "route-7-0".into(),
                prompt_token_ids: (0..35).map(PyReal::int).collect(),
                max_new_tokens: PyReal::from_json(&doc["max_new_tokens"]).unwrap(),
                expected_new_tokens: PyReal::from_json(&doc["max_new_tokens"]).unwrap(),
                qos_class: "interactive".into(),
                admitted_at: PyReal::Float(app_host::now()),
                target_ttft_ms: latency(
                    budgets::BudgetId::PyFinalMyceliumRequestGatewayBackendTargetTtftMsL234C12,
                ),
                target_tpot_ms: latency(
                    budgets::BudgetId::PyFinalMyceliumRequestGatewayBackendTargetTpotMsL235C12,
                ),
                target_tokens_per_second: PyReal::int(1),
                sampling_seed: PyReal::int(0),
                generation_config_digest: digest("greedy"),
            },
            owner_token: "s".repeat(32),
            excluded_placements: vec![],
        })
    }
}
struct Never(Arc<std::sync::Mutex<Vec<api::m16_requests::Dispatch>>>);
/// Emits a fixed controlled token sequence, then completes (gateway path only).
struct Emits(Vec<u32>, std::time::Duration, Option<std::time::Instant>);
impl Executor for Emits {
    fn start(&mut self, _: &api::m16_requests::Dispatch) -> Result<(), PublicError> {
        Ok(())
    }
    fn poll(&mut self, _: &str) -> Result<api::m16_requests::Execution, PublicError> {
        if self.2.is_some_and(|t| t.elapsed() < self.1) {
            return Ok(api::m16_requests::Execution::Pending);
        }
        self.2 = Some(std::time::Instant::now());
        Ok(if self.0.is_empty() {
            api::m16_requests::Execution::Completed
        } else {
            let t = self.0.remove(0);
            api::m16_requests::Execution::Token {
                token_id: t,
                text: format!("<{t}>"),
            }
        })
    }
    fn cleanup(&mut self, _: &str, _: bool) -> Result<bool, PublicError> {
        Ok(true)
    }
}
impl Executor for Never {
    fn start(&mut self, d: &api::m16_requests::Dispatch) -> Result<(), PublicError> {
        self.0.lock().unwrap().push(d.clone());
        Ok(())
    }
    fn poll(&mut self, _: &str) -> Result<api::m16_requests::Execution, PublicError> {
        Ok(api::m16_requests::Execution::Completed)
    }
    fn cleanup(&mut self, _: &str, _: bool) -> Result<bool, PublicError> {
        Ok(true)
    }
}
#[test]
fn route_graph_admits_through_the_live_factory_and_binds_qualification() {
    let message = controlled_message();
    let graph = route::graph(&message).unwrap();
    assert_eq!(graph.as_value()["stages"].as_array().unwrap().len(), 2);
    let q = route::qualification(&message).unwrap();
    assert_eq!(
        q["stage_load_proof_digests"],
        json!([digest("proof-0"), digest("proof-1")])
    );
    let runtime = api::m16_route::runtime(
        &message,
        Arc::new(Clock),
        Arc::new(Clock),
        Arc::new(Ids::default()),
    )
    .unwrap();
    let dispatched = Arc::new(std::sync::Mutex::new(Vec::new()));
    let service = M16Requests::new(
        runtime,
        Arc::new(Controlled(message.clone())),
        Box::new(Never(dispatched.clone())),
        Config::new(16, 4, 0.0).unwrap(),
    )
    .unwrap();
    assert_eq!(service.qualification().unwrap(), q);
    // A changed proof digest is a different qualification, refused at submit.
    let mut stale = message.clone();
    stale["stages"][1]["proof_digest"] = json!(digest("other-proof"));
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":"p","max_new_tokens":4,
        "workload_profile_id":"interactive","qos_class":"interactive",
        "qualification":route::qualification(&stale).unwrap()});
    assert_eq!(
        service.submit(doc.clone()).err().unwrap().code,
        "stale_qualification"
    );
    // The real route graph and device facts admit and dispatch the two-stage path.
    let mut current = doc;
    current["max_new_tokens"] = json!(32);
    current["qualification"] = q;
    let baseline = service.placement_status().unwrap();
    let accepted = service.submit(current).unwrap();
    for _ in 0..4 {
        service.tick().unwrap();
    }
    let d = dispatched.lock().unwrap();
    assert_eq!(d.len(), 1);
    let hops: Vec<_> = d[0].manifest["ordered_hops"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| h["placement_id"].clone())
        .collect();
    assert_eq!(hops, vec![json!("node-a:0-14"), json!("node-b:14-28")]);
    assert_eq!(
        service.terminal(&accepted.request_id).unwrap().as_deref(),
        Some("completed")
    );
    assert_eq!(service.placement_status().unwrap(), baseline);
}
#[test]
fn hop_frames_bind_request_position_and_placement() {
    let message = controlled_message();
    let path = Path {
        request_id: "route-7-0".into(),
        path_id: "path-1".into(),
        path_attempt: 0,
    };
    let entry = route::hop(&message, &path, 0, 0).unwrap();
    assert_eq!(entry["source_placement_id"], "");
    assert_eq!(entry["phase"], "PREFILL");
    let decode = route::hop(&message, &path, 3, 0).unwrap();
    assert_eq!(decode["source_placement_id"], "node-b:14-28");
    let payload = route::entry_payload(&[151644, 8948]).unwrap();
    assert_eq!(route::entry_tokens(&payload).unwrap(), vec![151644, 8948]);
    assert!(route::entry_tokens(&payload[..payload.len() - 1]).is_err());
    let bytes = router_wire::encode_frame("HopHeader", &entry, &payload).unwrap();
    let frame = router_wire::decode_frame(&bytes).unwrap();
    assert!(route::body_matches(&frame, "HopHeader", &entry));
    for (field, value) in [
        ("token_index", json!(1)),
        ("destination_placement_id", json!("node-b:14-28")),
        ("request_id", json!("route-7-1")),
        ("topology_version", json!(8)),
    ] {
        let mut wrong = entry.clone();
        wrong[field] = value;
        let f = router_wire::decode_frame(
            &router_wire::encode_frame("HopHeader", &wrong, &payload).unwrap(),
        )
        .unwrap();
        assert!(!route::body_matches(&f, "HopHeader", &entry), "{field}");
    }
    let token = route::token_event(&path, 0, 151667);
    let f =
        router_wire::decode_frame(&router_wire::encode_frame("TokenEvent", &token, &[]).unwrap())
            .unwrap();
    assert!(route::body_matches(&f, "TokenEvent", &token));
    assert!(!route::body_matches(
        &f,
        "TokenEvent",
        &route::token_event(&path, 1, 151667)
    ));
    let cancel = route::cancellation(&message, &path);
    assert!(router_wire::encode_frame("PathCancellation", &cancel, &[]).is_ok());
}

#[test]
fn http_gateway_streams_route_tokens_over_sse_with_reconnect_client() {
    gateway_stream(
        std::time::Duration::ZERO,
        span(budgets::BudgetId::ModelSelection),
        2,
    );
}
#[test]
fn sse_client_resumes_across_bounded_stream_polls() {
    // Tokens slower than the M16 stream's idle bound (OwnerPoll) end each stream,
    // forcing Last-Event-ID resumes; the gap is the gateway's TTFT target row.
    gateway_stream(
        span(budgets::BudgetId::PyFinalMyceliumRequestGatewayBackendTargetTtftMsL234C12),
        span(budgets::BudgetId::ModelSelection),
        256,
    );
}
fn gateway_stream(gap: std::time::Duration, budget: std::time::Duration, connections: usize) {
    use std::{net::TcpListener, sync::mpsc};
    let message = controlled_message();
    let runtime = api::m16_route::runtime(
        &message,
        Arc::new(Clock),
        Arc::new(Clock),
        Arc::new(Ids::default()),
    )
    .unwrap();
    let service = M16Requests::new(
        runtime,
        Arc::new(Controlled(message.clone())),
        Box::new(Emits(vec![11, 12, 13], gap, None)),
        Config::new(16, 4, 0.0).unwrap(),
    )
    .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let served = service.clone();
    listener.set_nonblocking(true).unwrap();
    let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let server_stop = stop.clone();
    let server = std::thread::spawn(move || {
        let stop = server_stop;
        let end = std::time::Instant::now() + span(budgets::BudgetId::WorkerStartup);
        let mut served_count = 0;
        while served_count < connections
            && std::time::Instant::now() < end
            && !stop.load(Ordering::SeqCst)
        {
            let Ok((stream, _)) = listener.accept() else {
                std::thread::sleep(poll());
                continue;
            };
            stream.set_nonblocking(false).unwrap();
            served_count += 1;
            let gateway = api::requests::Gateway::new(served.clone());
            if let Err(e) = api::http::serve_response_connection(
                stream,
                budget,
                api::requests::MAX_REQUEST_BODY,
                |r| gateway.preflight(r),
                |r| gateway.handle(r),
            ) {
                // Bounded polls end streams by design; recorded, not hidden.
                eprintln!("gateway connection ended: {e:?}");
            }
        }
    });
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":"p","max_new_tokens":32,
        "workload_profile_id":"interactive","qos_class":"interactive",
        "qualification":route::qualification(&message).unwrap()});
    let (status, accepted) = sse_client::http(addr, "POST", "/v1/inference", "", &doc.to_string());
    assert_eq!(status, 202, "{accepted}");
    let (tx, rx) = mpsc::channel();
    let path = accepted["stream_path"].as_str().unwrap().to_owned();
    let auth = format!(
        "X-Mycelium-Session: {}\r\n",
        accepted["session_token"].as_str().unwrap()
    );
    let reader = std::thread::spawn(move || {
        sse_client::stream(addr, path, auth, tx, span(budgets::BudgetId::WorkerStartup))
    });
    let done = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let ticker = {
        let (service, done) = (service.clone(), done.clone());
        std::thread::spawn(move || {
            while !done.load(Ordering::SeqCst) {
                service.tick().unwrap();
                std::thread::sleep(poll());
            }
        })
    };
    let events: Vec<_> = rx.iter().map(|(e, _)| e).collect();
    reader.join().unwrap();
    done.store(true, Ordering::SeqCst);
    stop.store(true, Ordering::SeqCst);
    ticker.join().unwrap();
    server.join().unwrap();
    let tokens: Vec<_> = events
        .iter()
        .filter(|e| e["type"] == "token")
        .map(|e| e["token_id"].clone())
        .collect();
    assert_eq!(tokens, vec![json!(11), json!(12), json!(13)]);
    assert_eq!(events.last().unwrap()["type"], "completed");
}
