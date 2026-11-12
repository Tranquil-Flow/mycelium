// SPDX-License-Identifier: AGPL-3.0-only
//! The REAL owner carrier executor (`m16_route::RouteExecutor`, under lane C's
//! M16 request service) against the REAL member stage server
//! (`joined_shard::serve`) over real Iroh QUIC, one thread-owned endpoint per
//! member. Only the stage COMPUTE is controlled: it returns scripted activation
//! and logit bytes, so every token here is a carrier test value and never model
//! output or native proof. The route envelope is signed by a test seed key
//! through the real member-side verifier. route_ready stays false.
use api::{
    app_host,
    compiler_inputs::Error,
    joined_shard::{
        route::{self},
        serve::{self, StageCompute},
    },
    m16_requests::{Host, M16Requests, Prepared},
    m16_route::{self, Codec, Currentness, RouteExecutor, RouteHandle, StagePeer},
    managed_carrier::HostClock,
    registry::Config,
    requests::Service,
    schema::PublicError,
};
use budgets::{BudgetId as B, Entries};
use protocol::identity::Identity;
use runtime::{
    iroh_host::{LanPolicy, LoopbackHost},
    m16_ledger::{IdSource, LeaseClock},
    m16_router_contracts::{PyReal, RequestContext},
};
use serde_json::{Value, json};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::Clock as _;

fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn digest(v: &Value) -> String {
    protocol::canonical::digest(v).unwrap()
}
const VOCAB: usize = 64;
/// Controlled final-stage logits: argmax at `token` (MYAC header of the native link).
fn logits(token: u32) -> Vec<u8> {
    let mut out = b"MYAC\x01\x03\x03\x01".to_vec();
    out.extend(((VOCAB * 4) as u64).to_be_bytes());
    for d in [1u32, 1, VOCAB as u32] {
        out.extend(d.to_be_bytes());
    }
    for i in 0..VOCAB {
        out.extend((if i as u32 == token { 1.0f32 } else { 0.0f32 }).to_le_bytes());
    }
    out
}
#[derive(Default)]
struct Log {
    steps: u64,
    released: Option<(Instant, &'static str)>,
    /// Ordered compute calls: ("step", session position), ("reset", 0),
    /// ("close", 0), ("abort", 0).
    events: Vec<(&'static str, u64)>,
}
/// Scripted stage behaviour. `bad_logits_at`: the final stage emits undecodable
/// logits at that token index. `close_gate`: graceful close blocks until opened.
#[derive(Default)]
struct Script {
    tokens: Vec<u32>,
    bad_logits_at: Option<u64>,
    close_gate: Option<mpsc::Receiver<()>>,
    /// The stage goes silent at this step index until the gate opens (a member
    /// Mac that stopped answering mid-request; nothing is ever sent for it).
    step_gate: Option<(u64, mpsc::Receiver<()>)>,
    /// EP1: a session reset signals `.0` when entered, then blocks until `.1` opens.
    reset_gate: Option<(mpsc::Sender<()>, mpsc::Receiver<()>)>,
    /// EP1: every session reset fails (the compute cannot release its state).
    reset_fails: bool,
    /// EPF: the member's own consent wall (its liveness refuses from then on).
    consent_wall: Option<Instant>,
    /// EPF: set = the member's standing consent changed (liveness refuses).
    consent_changed: Option<Arc<AtomicBool>>,
    /// EPF2: signalled once when a graceful close starts (before `close_gate`).
    close_entered: Option<mpsc::Sender<()>>,
}
struct Controlled {
    offer: Value,
    last: bool,
    owned: bool,
    script: Script,
    log: Arc<Mutex<Log>>,
    /// Steps since the last reset: 0 = PREFILL, as the native session's position.
    position: u64,
}
impl StageCompute for Controlled {
    fn offer(&self) -> &Value {
        &self.offer
    }
    fn worker_pid(&self) -> Option<u32> {
        self.owned.then_some(1)
    }
    fn step(
        &mut self,
        _: Option<&[u32]>,
        _: &[u8],
        current: serve::Current<'_>,
        cancel: Arc<AtomicBool>,
        _: Instant,
    ) -> Result<Vec<u8>, Error> {
        current()?;
        if cancel.load(Ordering::SeqCst) {
            return Err(Error("compiler_stopped"));
        }
        let mut log = self.log.lock().unwrap();
        let t = log.steps;
        log.steps += 1;
        let position = self.position;
        self.position += 1;
        log.events.push(("step", position));
        drop(log);
        if let Some((at, gate)) = &self.script.step_gate
            && *at == t
            && let Err(e) = gate.recv_timeout(span(B::WorkerStartup))
        {
            eprintln!("silent stage gate: {e}");
        }
        if !self.last {
            return Ok(vec![t as u8; 16]);
        }
        if self.script.bad_logits_at == Some(t) {
            return Ok(b"not logits".to_vec());
        }
        // By session position: equal to the global step count until a reset.
        Ok(logits(
            self.script.tokens[position as usize % self.script.tokens.len()],
        ))
    }
    fn close(&mut self) -> Result<Value, Error> {
        if let Some(entered) = self.script.close_entered.take()
            && let Err(e) = entered.send(())
        {
            eprintln!("close entered: {e}");
        }
        if let Some(gate) = self.script.close_gate.take() {
            gate.recv_timeout(span(B::WorkerStartup)).unwrap();
        }
        self.owned = false;
        let mut log = self.log.lock().unwrap();
        log.released = Some((Instant::now(), "close"));
        log.events.push(("close", 0));
        Ok(json!({"context_closed":true}))
    }
    fn abort(&mut self) -> Result<Value, Error> {
        self.owned = false;
        let mut log = self.log.lock().unwrap();
        log.released = Some((Instant::now(), "abort"));
        log.events.push(("abort", 0));
        Ok(json!({"context_closed":false,"worker_reaped":true}))
    }
    fn reset(&mut self, current: serve::Current<'_>) -> Result<Value, Error> {
        current()?;
        // Only the first reset is held.
        if let Some((entered, open)) = self.script.reset_gate.take() {
            entered.send(()).unwrap();
            open.recv_timeout(span(B::WorkerStartup)).unwrap();
        }
        if self.script.reset_fails {
            return Err(Error("native_session_reset_invalid"));
        }
        self.position = 0;
        self.log.lock().unwrap().events.push(("reset", 0));
        Ok(json!({"controlled_reset":true}))
    }
    fn live(&self, current: serve::Current<'_>) -> Result<(), Error> {
        current()?;
        if self
            .script
            .consent_wall
            .is_some_and(|w| Instant::now() >= w)
        {
            return Err(Error("joined_native_consent_exhausted"));
        }
        if self
            .script
            .consent_changed
            .as_ref()
            .is_some_and(|c| c.load(Ordering::SeqCst))
        {
            return Err(Error("joined_native_consent_changed"));
        }
        Ok(())
    }
}
struct Text;
impl Codec for Text {
    fn encode(&self, _: &str) -> Result<Vec<u32>, PublicError> {
        Ok((0..35).collect())
    }
    fn decode(&self, token: u32) -> Result<String, PublicError> {
        Ok(format!("<{token}>"))
    }
}
struct Wall;
impl LeaseClock for Wall {
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
/// Request host whose readiness follows the real carrier (`RouteWatch::refusal`).
struct Following {
    message: Value,
    handle: RouteHandle,
    prepared: AtomicU64,
}
impl Host for Following {
    fn authorized(&self, _: &api::http::Request) -> bool {
        true
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        self.handle.refusal(false)?;
        route::qualification(&self.message).map_err(|e| PublicError::new(409, e.0))
    }
    fn prepare(&self, doc: &Value) -> Result<Prepared, PublicError> {
        let n = self.prepared.fetch_add(1, Ordering::SeqCst);
        self.handle.refusal(n > 0)?;
        let latency =
            |id: B| PyReal::from_json(&json!(id.span(Entries::ZERO).unwrap().millis())).unwrap();
        Ok(Prepared {
            context: RequestContext {
                request_id: format!("route-7-{n}"),
                prompt_token_ids: (0..35).map(PyReal::int).collect(),
                max_new_tokens: PyReal::from_json(&doc["max_new_tokens"]).unwrap(),
                expected_new_tokens: PyReal::from_json(&doc["max_new_tokens"]).unwrap(),
                qos_class: "interactive".into(),
                admitted_at: PyReal::Float(app_host::now()),
                target_ttft_ms: latency(B::PyFinalMyceliumRequestGatewayBackendTargetTtftMsL234C12),
                target_tpot_ms: latency(B::PyFinalMyceliumRequestGatewayBackendTargetTpotMsL235C12),
                target_tokens_per_second: PyReal::int(1),
                sampling_seed: PyReal::int(0),
                generation_config_digest: digest(&json!({"sampling":"greedy"})),
            },
            owner_token: "s".repeat(32),
            excluded_placements: vec![],
        })
    }
}
/// One member stage server on its own thread, runtime and endpoint.
struct Member {
    endpoint: iroh::EndpointId,
    socket: std::net::SocketAddr,
    route: mpsc::Sender<Value>,
    thread: std::thread::JoinHandle<Result<Value, String>>,
    log: Arc<Mutex<Log>>,
}
impl Member {
    fn released(&self) -> Option<(Instant, &'static str)> {
        self.log.lock().unwrap().released
    }
}
/// How a member stage server binds its carrier endpoint.
enum Bind {
    Loopback,
    Lan(Box<(iroh::SecretKey, std::net::IpAddr, LanPolicy)>),
}
type Live = Arc<dyn Fn() -> Result<Value, Error> + Send + Sync>;
fn member(member: Value, offer: Value, last: bool, script: Script, proof: String) -> Member {
    let fixed: Live = Arc::new(move || Ok(member.clone()));
    member_at(Bind::Loopback, fixed, offer, last, script, proof)
}
fn member_at(
    bind: Bind,
    live: Live,
    offer: Value,
    last: bool,
    script: Script,
    proof: String,
) -> Member {
    let log = Arc::new(Mutex::new(Log::default()));
    let (addr_tx, addr_rx) = mpsc::channel();
    let (route_tx, route_rx) = mpsc::channel::<Value>();
    let shared = log.clone();
    let thread = std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let clock = HostClock::default();
        let until = |id: B| {
            clock
                .monotonic()
                .checked_add(id.span(Entries::ZERO).unwrap())
                .unwrap()
        };
        let mut host = match bind {
            Bind::Loopback => LoopbackHost::bind(&rt, &clock, until(B::RouterClose)).unwrap(),
            Bind::Lan(lan) => {
                let (secret, interface, policy) = *lan;
                LoopbackHost::bind_lan(
                    &rt,
                    &clock,
                    until(B::RouterClose),
                    secret,
                    &[transport::Channel::Router],
                    interface,
                    policy,
                )
                .unwrap()
            }
        };
        let addr = host.address().unwrap();
        addr_tx
            .send((addr.id, *addr.ip_addrs().next().unwrap()))
            .unwrap();
        let envelope = route_rx.recv_timeout(span(B::WorkerStartup)).unwrap();
        let mut compute = Controlled {
            offer,
            last,
            owned: true,
            script,
            log: shared,
            position: 0,
        };
        let current = || live();
        let report = serve::serve(
            &serve::Serve {
                host: &host,
                runtime: &rt,
                clock: &clock,
                current: &current,
                cancelled: Arc::new(AtomicBool::new(false)),
                deadline: Instant::now() + span(B::MemberChosenWall),
            },
            &mut compute,
            &envelope,
            &proof,
        );
        assert!(
            !compute.owned,
            "stage compute released before serve returned"
        );
        host.close(&clock, until(B::RouterClose)).unwrap();
        drop(host);
        drop(rt);
        report
    });
    let (endpoint, socket) = addr_rx.recv_timeout(span(B::WorkerStartup)).unwrap();
    Member {
        endpoint,
        socket,
        route: route_tx,
        thread,
        log,
    }
}
struct Route {
    message: Value,
    service: M16Requests,
    handle: RouteHandle,
    members: Vec<Member>,
    id: String,
    token: String,
}
/// Two stage servers, a seed-signed controlled route, a connected carrier and an
/// M16 service whose host follows the carrier; one request submitted.
fn route(scripts: [Script; 2], max_new_tokens: u64) -> Route {
    let seed = Identity::from_secret("test-seed", &[7; 32]).unwrap();
    let rows: Vec<Value> = ["node-a", "node-b"]
        .iter()
        .map(|n| {
            json!({"node_id":n,"generation":1,"swarm_id":"swarm",
            "seed_key_digest":seed.verification_key_digest()})
        })
        .collect();
    let offers: Vec<Value> = (0..2).map(|i| json!({"controlled_offer":i})).collect();
    let [first, second] = scripts;
    let members = vec![
        member(rows[0].clone(), offers[0].clone(), false, first, proof(0)),
        member(rows[1].clone(), offers[1].clone(), true, second, proof(1)),
    ];
    let peers: Vec<StagePeer> = members
        .iter()
        .zip(&rows)
        .map(|(m, r)| StagePeer {
            node_id: r["node_id"].as_str().unwrap().into(),
            endpoint: m.endpoint,
            socket: m.socket,
            generation: 1,
        })
        .collect();
    assemble(
        &seed,
        "swarm",
        &rows,
        &offers,
        members,
        max_new_tokens,
        |m, owner| RouteExecutor::connect(m, owner, peers, Arc::new(|_| Some(1)), Arc::new(Text)),
    )
    .unwrap()
}
fn proof(i: usize) -> String {
    digest(&json!({"controlled_proof":i}))
}
/// Sign the route with `seed`, hand it to the members, connect the owner carrier
/// and submit one request through the M16 service.
fn assemble(
    seed: &Identity,
    swarm: &str,
    rows: &[Value],
    offers: &[Value],
    members: Vec<Member>,
    max_new_tokens: u64,
    connect: impl FnOnce(Value, iroh::SecretKey) -> Result<(RouteExecutor, RouteHandle), PublicError>,
) -> Result<Route, PublicError> {
    let owner = iroh::SecretKey::generate();
    let endpoints: Vec<iroh::EndpointId> = members.iter().map(|m| m.endpoint).collect();
    let (message, envelope) = signed_route(seed, swarm, rows, offers, &endpoints, &owner);
    for m in &members {
        m.route.send(envelope.clone()).unwrap();
    }
    let (executor, handle) = match connect(message.clone(), owner) {
        Ok(connected) => connected,
        Err(e) => {
            // Members never saw an owner: they end at their accept deadline.
            drop(members);
            return Err(e);
        }
    };
    let runtime = m16_route::runtime(
        &message,
        Arc::new(Wall),
        Arc::new(Wall),
        Arc::new(Ids::default()),
    )
    .unwrap();
    let host = Arc::new(Following {
        message: message.clone(),
        handle: handle.clone(),
        prepared: AtomicU64::new(0),
    });
    let service = M16Requests::new(
        runtime,
        host,
        Box::new(executor),
        Config::new(16, 4, 0.0).unwrap(),
    )
    .unwrap();
    let accepted = service
        .submit(submission(&message, max_new_tokens))
        .unwrap();
    let (id, token) = (accepted.request_id, accepted.session_token);
    Ok(Route {
        message,
        service,
        handle,
        members,
        id,
        token,
    })
}
/// A seed-signed controlled route over `endpoints` (stage carriers) for `owner`.
fn signed_route(
    seed: &Identity,
    swarm: &str,
    rows: &[Value],
    offers: &[Value],
    endpoints: &[iroh::EndpointId],
    owner: &iroh::SecretKey,
) -> (Value, Value) {
    signed_route_as(
        route::ROUTE,
        span(B::MemberChosenWall),
        seed,
        swarm,
        rows,
        offers,
        endpoints,
        owner,
    )
}
/// [`signed_route`] with the route protocol and its lifetime from now.
#[allow(clippy::too_many_arguments)]
fn signed_route_as(
    protocol: &str,
    lifetime: Duration,
    seed: &Identity,
    swarm: &str,
    rows: &[Value],
    offers: &[Value],
    endpoints: &[iroh::EndpointId],
    owner: &iroh::SecretKey,
) -> (Value, Value) {
    let stage = |i: usize, start: u64, end: u64| {
        json!({"stage_index":i,"placement_id":format!("{}:{start}-{end}",rows[i]["node_id"].as_str().unwrap()),
        "member":rows[i],"carrier_endpoint_id":endpoints[i].to_string(),"run":format!("run-{i}"),
        "offer_digest":digest(&offers[i]),"proof_digest":proof(i),"range":[start,end],
        "worker_pid":100+i,"tokens":99,"memory":4_000_000_000u64})
    };
    let now = app_host::now();
    let message = json!({"protocol":protocol,"swarm_id":swarm,
        "deployment_id":"12345678-1234-5678-9234-abcdefabcdef","deployment_epoch":7,"topology_version":7,
        "model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca",
        "manifest_digest":digest(&json!("manifest")),"owner_endpoint_id":owner.public().to_string(),
        "stages":[stage(0,0,14),stage(1,14,28)],"issued_at":now,
        "expires_at":now + lifetime.as_secs_f64(),"route_ready":false,"scope":route::SCOPE});
    let envelope = json!({"protocol":"mycelium.joined_shard.signed.v1","signature":seed.sign(&message).unwrap(),
        "verification_key":seed.public_key_record(),"message":message});
    (message, envelope)
}
fn submission(message: &Value, max_new_tokens: u64) -> Value {
    json!({"protocol":"mycelium.request_gateway.v2","prompt":"p","max_new_tokens":max_new_tokens,
        "workload_profile_id":"interactive","qos_class":"interactive",
        "qualification":route::qualification(message).unwrap()})
}
impl Route {
    /// Tick the real M16 service, event-driven by carrier changes, until `done`.
    fn pump_until(&self, bound: Duration, done: impl Fn(&Self) -> bool) {
        let end = Instant::now() + bound;
        let mut seen = 0;
        while !done(self) {
            assert!(Instant::now() < end, "pump bound: {:?}", self.state());
            if let Err(e) = self.service.tick() {
                panic!("tick: {e:?}; {}", self.state());
            }
            seen = self.handle.wait(seen, span(B::OwnerPoll));
        }
    }
    fn terminal(&self) -> Option<String> {
        self.service.terminal(&self.id).unwrap()
    }
    fn state(&self) -> String {
        let o = self.handle.state.lock().unwrap();
        format!(
            "tokens={:?} completed={} failure={:?} attempts={} finished={} stages={:?} terminal={:?}",
            o.tokens,
            o.completed,
            o.failure,
            o.teardown_attempts,
            o.teardown_finished,
            o.stages
                .iter()
                .map(|s| (s.acknowledged_ms, s.failure.clone()))
                .collect::<Vec<_>>(),
            self.service.terminal(&self.id)
        )
    }
    fn finish(self) -> Vec<Value> {
        let Route {
            service, members, ..
        } = self;
        drop(service);
        members
            .into_iter()
            .map(|m| m.thread.join().unwrap().unwrap())
            .collect()
    }
}
fn scripted(tokens: &[u32]) -> Script {
    Script {
        tokens: tokens.to_vec(),
        ..Script::default()
    }
}

#[test]
fn carrier_streams_scripted_tokens_and_spends_the_epoch() {
    let r = route([Script::default(), scripted(&[5, 9, 3, 7])], 4);
    r.pump_until(span(B::WorkerStartup), |r| r.terminal().is_some());
    assert_eq!(r.terminal().as_deref(), Some("completed"), "{}", r.state());
    assert_eq!(r.handle.state.lock().unwrap().tokens, vec![5, 9, 3, 7]);
    assert!(r.handle.state.lock().unwrap().settled());
    // N3: the single-use epoch admits no second request.
    let again = r.service.submit(submission(&r.message, 1)).err().unwrap();
    assert_eq!(again.code, "route_dropped");
    let reports = r.finish();
    assert!(reports.iter().all(|v| v["teardown"] == "path_cancellation"));
}

fn tokens(r: &Route) -> usize {
    r.handle.state.lock().unwrap().tokens.len()
}
#[test]
fn stop_retires_the_epoch_after_both_stages_acknowledge() {
    let r = route([Script::default(), scripted(&[5, 9, 3, 7])], 32);
    r.pump_until(span(B::WorkerStartup), |r| tokens(r) >= 2);
    assert!(r.service.cancel(&r.id, Some(&r.token)).unwrap());
    r.pump_until(span(B::WorkerStartup), |r| r.terminal().is_some());
    assert_eq!(r.terminal().as_deref(), Some("cancelled"), "{}", r.state());
    assert!(r.handle.state.lock().unwrap().settled());
    // N3: a stopped epoch is no longer offered as ready.
    assert_eq!(r.service.qualification().unwrap_err().code, "route_dropped");
    let reports = r.finish();
    assert!(
        reports.iter().all(|v| v["ack_delivered"] == true),
        "{reports:?}"
    );
}
/// N4: an undecodable final-stage result releases the stage compute at once,
/// and the member still acknowledges the owner's teardown so the owner settles.
#[test]
fn stage_failure_releases_compute_before_teardown_and_still_acknowledges() {
    let r = route(
        [
            Script::default(),
            Script {
                tokens: vec![5, 9, 3, 7],
                bad_logits_at: Some(2),
                ..Script::default()
            },
        ],
        32,
    );
    r.pump_until(span(B::WorkerStartup), |r| r.terminal().is_some());
    assert_eq!(r.terminal().as_deref(), Some("failed"), "{}", r.state());
    let o = r.handle.state.lock().unwrap();
    assert_eq!(o.tokens, vec![5, 9]);
    assert_eq!(o.failure, Some("joined_route_hop_deadline"));
    assert!(o.settled(), "owner cleanup proven by ACK");
    let failed_at = o.started.unwrap() + Duration::from_secs_f64(o.failed_at_ms.unwrap() / 1000.0);
    drop(o);
    let (released, how) = r.members[1].released().expect("final stage released");
    assert!(
        released < failed_at,
        "compute released before the owner timed out"
    );
    assert_eq!(
        how, "close",
        "framing intact after a completed step: graceful close"
    );
    let reports = r.finish();
    assert_eq!(
        reports[1]["failure"], "native_logits_invalid",
        "{:?}",
        reports[1]
    );
    assert_eq!(reports[1]["ack_delivered"], true);
}
/// N2: a stage whose release outlasts the owner's close budget is settled by a
/// bounded re-dial instead of leaving the owner's debt stuck.
#[test]
fn stage_stalled_past_the_close_budget_is_settled_by_redial() {
    let (open, gate) = mpsc::channel();
    let r = route(
        [
            Script::default(),
            Script {
                tokens: vec![5, 9, 3, 7],
                close_gate: Some(gate),
                ..Script::default()
            },
        ],
        32,
    );
    r.pump_until(span(B::WorkerStartup), |r| tokens(r) >= 2);
    assert!(r.service.cancel(&r.id, Some(&r.token)).unwrap());
    r.pump_until(span(B::WorkerStartup), |r| {
        let o = r.handle.state.lock().unwrap();
        o.teardown_attempts >= 1 && !o.teardown_busy
    });
    {
        let o = r.handle.state.lock().unwrap();
        assert!(o.stages[0].acknowledged_ms.is_some());
        assert!(o.stages[1].acknowledged_ms.is_none(), "{:?}", o.stages[1]);
        assert!(o.stages[1].failure.is_some());
    }
    assert!(r.terminal().is_none(), "debt retained while unsettled");
    // A stale inbound dial (abandoned before the member could accept it) is
    // queued ahead of the owner's re-dial; it must not end the re-dial window.
    abandoned_dial(&r.members[1]);
    open.send(()).unwrap();
    r.pump_until(span(B::WorkerStartup), |r| r.terminal().is_some());
    assert_eq!(r.terminal().as_deref(), Some("cancelled"), "{}", r.state());
    let attempts = r.handle.state.lock().unwrap().teardown_attempts;
    assert!(attempts >= 2, "settled by a re-dial: {attempts}");
    let reports = r.finish();
    assert_eq!(reports[1]["ack_delivered"], false);
    assert!(reports[1]["redial_ack_ms"].is_number(), "{:?}", reports[1]);
}

/// Dial `member` from a throwaway endpoint while it is not accepting, give up
/// at a short deadline, and close that endpoint (deterministic stale incoming).
fn abandoned_dial(member: &Member) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = HostClock::default();
    let until = |id: B| {
        clock
            .monotonic()
            .checked_add(id.span(Entries::ZERO).unwrap())
            .unwrap()
    };
    let mut stray = LoopbackHost::bind(&rt, &clock, until(B::RouterClose)).unwrap();
    let addr = iroh::EndpointAddr::new(member.endpoint).with_ip_addr(member.socket);
    assert!(
        stray
            .connect_channel(
                &addr,
                transport::Channel::Router,
                &clock,
                until(B::OwnerPoll)
            )
            .is_err(),
        "member is blocked in close: the dial cannot complete"
    );
    stray.close(&clock, until(B::RouterClose)).unwrap();
}
// ---- Direct-LAN carrier over REAL pairing: seed rows, sessions, offers, signed adverts.
use api::{
    handoff, joined_shard,
    joined_shard::carrier::{self, VerifiedCarrier},
    member::Session,
    peer,
    preferences::{resource_policy, standing_consent},
};
use std::{net::IpAddr, path::PathBuf};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Paired {
    signer: Identity,
    carrier: Identity,
    secret: iroh::SecretKey,
    session: Session,
    offer: Value,
}
struct Swarm {
    root: PathBuf,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    members: Vec<Paired>,
}
impl Drop for Swarm {
    fn drop(&mut self) {
        self.seed.close();
        if let Err(e) = std::fs::remove_dir_all(&self.root) {
            eprintln!("swarm root cleanup: {e}");
        }
    }
}
fn swarm() -> Swarm {
    let root = std::env::temp_dir().join(format!(
        "b8-lan-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    std::fs::create_dir(&root).unwrap();
    let root = std::fs::canonicalize(root).unwrap();
    let owner = root.join("owner");
    std::fs::create_dir(&owner).unwrap();
    let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "b8-lan-swarm", None).unwrap();
    let (seed_signer, _, _) = app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let source_key = iroh::SecretKey::generate();
    let source = Identity::from_secret(&source_key.public().to_string(), &[9; 32]).unwrap();
    let mut members = Vec::new();
    for i in 0..2 {
        let member_root = root.join(format!("member{i}"));
        std::fs::create_dir(&member_root).unwrap();
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let issuer = handoff::Coordinator {
            signer: Identity::from_secret(seed_signer.endpoint_id(), &seed_signer.secret_bytes())
                .unwrap(),
            swarm_id: "b8-lan-swarm".into(),
            seed_url: seed.seed_url.clone(),
        };
        let ttl = B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
            .span(Entries::ZERO)
            .unwrap()
            .millis()
            / 1000;
        let approved = handoff::approve_pairing(&owner, true, Some(&issuer), &request.to_value(), &json!(ttl),
            &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}),
            app_host::now(), &handoff::Entropy::os).unwrap();
        let options = seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv {
            signer: &signer,
            clock: &app_host::now,
            random_hex: &random,
            seed: &options,
        };
        let mut accepted =
            peer::accept_handoff(&member_root, true, &approved.to_value(), &env).unwrap();
        peer::renew(&member_root, true, accepted.session.as_mut(), &env).unwrap();
        let session = accepted.session.unwrap();
        let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
        resource_policy::write(&member_root, &policy).unwrap();
        standing_consent::grant(&member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
        let binding = session.binding().unwrap();
        standing_consent::bind(&member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
        let secret = iroh::SecretKey::generate();
        let carrier =
            Identity::from_secret(&secret.public().to_string(), &signer.secret_bytes()).unwrap();
        let registration =
            joined_shard::register(&session, &signer, &carrier, app_host::now()).unwrap();
        let mut recipe = case["node_plans"]["node-0"].clone();
        recipe["node_id"] = json!(session.node_id());
        recipe.as_object_mut().unwrap().remove("node_plan_digest");
        recipe["node_plan_digest"] = json!(digest(&recipe));
        let transfer = case["node_files"]["node-0"][0].clone();
        let offer = joined_shard::issue(
            &seed.coordinator,
            &registration,
            &format!("b8-lan-transfer-{i}"),
            &transfer,
            &recipe,
            &source,
        )
        .unwrap();
        members.push(Paired {
            signer,
            carrier,
            secret,
            session,
            offer,
        });
    }
    // Route stages in node-id order, like the owner producer.
    members.sort_by(|a, b| a.session.node_id().cmp(b.session.node_id()));
    Swarm {
        root,
        seed,
        members,
    }
}
impl Swarm {
    fn live(&self, i: usize) -> Value {
        let m = &self.members[i];
        m.session
            .shard_identity(&m.signer, app_host::now())
            .unwrap()
    }
    fn advert(&self, i: usize, addrs: &[std::net::SocketAddr]) -> Value {
        let m = &self.members[i];
        carrier::advertise(&m.session, &m.signer, &m.carrier, addrs, app_host::now()).unwrap()
    }
    fn verify(
        &self,
        i: usize,
        advert: &Value,
        policy: &LanPolicy,
    ) -> Result<VerifiedCarrier, String> {
        carrier::verify(
            &self.seed.coordinator,
            &self.members[i].offer,
            advert,
            policy,
            app_host::now(),
        )
        .map_err(|e| e.0.to_owned())
    }
    /// Stage servers bound on `interface`; returns members + their verified carriers.
    fn serve_lan(
        &self,
        interface: IpAddr,
        policy: &LanPolicy,
        tokens: &[u32],
    ) -> (Vec<Member>, Vec<VerifiedCarrier>) {
        self.serve_lan_scripts(interface, policy, [Script::default(), scripted(tokens)])
    }
    /// Stage servers with one controlled script per stage.
    fn serve_lan_scripts(
        &self,
        interface: IpAddr,
        policy: &LanPolicy,
        scripts: [Script; 2],
    ) -> (Vec<Member>, Vec<VerifiedCarrier>) {
        let mut out = Vec::new();
        let mut verified = Vec::new();
        for (i, script) in scripts.into_iter().enumerate() {
            let m = &self.members[i];
            let (session, signer) = (
                m.session.clone(),
                Identity::from_secret(m.signer.endpoint_id(), &m.signer.secret_bytes()).unwrap(),
            );
            let live: Live = Arc::new(move || {
                session
                    .shard_identity(&signer, app_host::now())
                    .map_err(|_| Error("test_member_invalid"))
            });
            let served = member_at(
                Bind::Lan(Box::new((m.secret.clone(), interface, policy.clone()))),
                live,
                m.offer.clone(),
                i == 1,
                script,
                proof(i),
            );
            assert_eq!(served.endpoint, m.secret.public(), "registered carrier key");
            verified.push(
                self.verify(i, &self.advert(i, &[served.socket]), policy)
                    .unwrap(),
            );
            out.push(served);
        }
        (out, verified)
    }
    fn route(
        &self,
        members: Vec<Member>,
        connect: impl FnOnce(
            Value,
            iroh::SecretKey,
        ) -> Result<(RouteExecutor, RouteHandle), PublicError>,
    ) -> Result<Route, PublicError> {
        let (signer, _, _) =
            app_host::identity_in(&self.root.join("owner"), app_host::SEED_IDENTITY_FILE).unwrap();
        let rows = [self.live(0), self.live(1)];
        let offers = [self.members[0].offer.clone(), self.members[1].offer.clone()];
        assemble(
            &signer,
            self.seed.coordinator.swarm_id(),
            &rows,
            &offers,
            members,
            4,
            connect,
        )
    }
}
fn currentness(s: &Swarm) -> Currentness {
    let coordinator = s.seed.coordinator.clone();
    Arc::new(move |node: &str| {
        coordinator
            .member(node)
            .ok()
            .and_then(|r| r["generation"].as_u64())
    })
}
fn lan_streams(interface: IpAddr, policy: LanPolicy) {
    let s = swarm();
    let (members, verified) = s.serve_lan(interface, &policy, &[5, 9, 3, 7]);
    for (m, v) in members.iter().zip(&verified) {
        assert_eq!(v.addrs(), &[m.socket], "dials exactly the signed address");
        assert_eq!(m.socket.ip(), interface);
    }
    // A stray dial, abandoned before the member accepts, must not abort the
    // member's route setup (no remote kill switch on a shared network).
    if interface.is_loopback() {
        abandoned_dial(&members[0]);
    }
    let current = currentness(&s);
    let r = s
        .route(members, |m, owner| {
            RouteExecutor::connect_lan(
                m,
                owner,
                verified,
                interface,
                policy,
                current,
                Arc::new(Text),
            )
        })
        .unwrap();
    r.pump_until(span(B::WorkerStartup), |r| r.terminal().is_some());
    assert_eq!(r.terminal().as_deref(), Some("completed"), "{}", r.state());
    assert_eq!(r.handle.state.lock().unwrap().tokens, vec![5, 9, 3, 7]);
    let reports = r.finish();
    assert!(
        reports.iter().all(|v| v["ack_delivered"] == true),
        "{reports:?}"
    );
}
#[test]
fn lan_carrier_streams_over_signed_addresses_on_explicit_loopback() {
    let loopback: IpAddr = "127.0.0.1".parse().unwrap();
    lan_streams(loopback, LanPolicy::with_explicit(vec![loopback]).unwrap());
}
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn lan_carrier_streams_over_signed_addresses_on_the_real_interface() {
    let interface: IpAddr = std::env::var("MYCELIUM_LAN_IP").unwrap().parse().unwrap();
    lan_streams(interface, LanPolicy::private_only());
}
#[test]
fn lan_advertisement_must_be_enrolled_registered_fresh_and_allow_listed() {
    let s = swarm();
    let policy = LanPolicy::private_only();
    let ok: std::net::SocketAddr = "192.168.7.20:41000".parse().unwrap();
    assert!(s.verify(0, &s.advert(0, &[ok]), &policy).is_ok());
    // Wrong peer key: a stranger signs a carrier claim for this member.
    let stranger = Identity::from_secret(s.members[0].signer.endpoint_id(), &[77; 32]).unwrap();
    let mut forged = s.advert(0, &[ok]);
    forged["verification_key"] = stranger.public_key_record();
    forged["signature"] = stranger.sign(&forged["message"]).unwrap();
    assert_eq!(
        s.verify(0, &forged, &policy).unwrap_err(),
        "joined_shard_signature_invalid"
    );
    // Another member's (validly signed) advert is signed by a different enrolled key.
    assert_eq!(
        s.verify(0, &s.advert(1, &[ok]), &policy).unwrap_err(),
        "joined_shard_signature_invalid"
    );
    // The enrolled key cannot re-point its registered carrier to another endpoint.
    let m = &s.members[0];
    let elsewhere = Identity::from_secret(
        &iroh::SecretKey::generate().public().to_string(),
        &m.signer.secret_bytes(),
    )
    .unwrap();
    let repointed =
        carrier::advertise(&m.session, &m.signer, &elsewhere, &[ok], app_host::now()).unwrap();
    assert_eq!(
        s.verify(0, &repointed, &policy).unwrap_err(),
        "joined_carrier_identity_mismatch"
    );
    // Public / CGNAT addresses are refused unless explicitly configured.
    for public in ["8.8.8.8:41000", "100.84.252.4:41000"] {
        let advert = s.advert(0, &[public.parse().unwrap()]);
        assert_eq!(
            s.verify(0, &advert, &policy).unwrap_err(),
            "joined_carrier_address_refused"
        );
    }
    let tailnet = LanPolicy::with_explicit(vec!["100.84.252.4".parse().unwrap()]).unwrap();
    assert!(
        s.verify(
            0,
            &s.advert(0, &["100.84.252.4:41000".parse().unwrap()]),
            &tailnet
        )
        .is_ok()
    );
    // Tampering with the signed address list breaks the enrolled signature.
    let mut moved = s.advert(0, &[ok]);
    moved["message"]["direct_addrs"] = json!(["192.168.7.21:41000"]);
    assert_eq!(
        s.verify(0, &moved, &policy).unwrap_err(),
        "joined_shard_signature_invalid"
    );
}
#[test]
fn lan_carrier_refuses_mitm_keys_unexpected_peers_and_public_interfaces() {
    let s = swarm();
    let loopback: IpAddr = "127.0.0.1".parse().unwrap();
    let policy = LanPolicy::with_explicit(vec![loopback]).unwrap();
    // MITM: member 1's advert is validly signed, but its address is held by an
    // endpoint with a different key; the owner's pinned dial must fail.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = HostClock::default();
    let until = |id: B| {
        clock
            .monotonic()
            .checked_add(id.span(Entries::ZERO).unwrap())
            .unwrap()
    };
    let mut mitm = LoopbackHost::bind_lan(
        &rt,
        &clock,
        until(B::RouterClose),
        iroh::SecretKey::generate(),
        &[transport::Channel::Router],
        loopback,
        policy.clone(),
    )
    .unwrap();
    let mitm_socket = *mitm.address().unwrap().ip_addrs().next().unwrap();
    let (members, mut verified) = s.serve_lan(loopback, &policy, &[5]);
    verified[1] = s.verify(1, &s.advert(1, &[mitm_socket]), &policy).unwrap();
    let current = currentness(&s);
    let began = Instant::now();
    let refused = std::thread::scope(|scope| {
        let mitm_side = scope.spawn(|| mitm.accept(&clock, until(B::SidecarLocalHandshake)));
        let refused = s
            .route(members, |m, owner| {
                RouteExecutor::connect_lan(
                    m,
                    owner,
                    verified,
                    loopback,
                    policy.clone(),
                    current,
                    Arc::new(Text),
                )
            })
            .err()
            .unwrap();
        assert!(
            mitm_side.join().unwrap().is_err(),
            "MITM never completes a session"
        );
        refused
    });
    assert_eq!(refused.code, "joined_route_connect_failed");
    assert!(
        began.elapsed() < span(B::WorkerStartup),
        "refused within the connect budget"
    );
    mitm.close(&clock, until(B::RouterClose)).unwrap();
    // Unexpected peer: a verified carrier for the wrong stage is refused before dialing.
    let (members, verified) = s.serve_lan(loopback, &policy, &[5]);
    let swapped = vec![verified[1].clone(), verified[0].clone()];
    let current = currentness(&s);
    let refused = s
        .route(members, |m, owner| {
            RouteExecutor::connect_lan(
                m,
                owner,
                swapped,
                loopback,
                policy.clone(),
                current,
                Arc::new(Text),
            )
        })
        .err()
        .unwrap();
    assert_eq!(refused.code, "joined_route_peer_mismatch");
    // A public owner interface is refused even with verified carriers.
    let (members, verified) = s.serve_lan(loopback, &policy, &[5]);
    let current = currentness(&s);
    let refused = s
        .route(members, |m, owner| {
            RouteExecutor::connect_lan(
                m,
                owner,
                verified,
                "8.8.8.8".parse().unwrap(),
                policy.clone(),
                current,
                Arc::new(Text),
            )
        })
        .err()
        .unwrap();
    assert_eq!(refused.code, "joined_route_address_refused");
}

// ---- B16 (gap row 6): the owner's epoch composition as PRODUCT code
// (`api::joined_owner::epoch`), driven through its own loopback HTTP gateway
// and SSE stream, over real pairing rows and the direct-LAN carrier on an
// explicit loopback interface. Stage compute stays controlled (no model).
#[path = "support/sse_client.rs"]
mod sse_client;
use api::joined_owner::epoch::{Carriers, Epoch};
impl Swarm {
    fn signed(&self, endpoints: &[iroh::EndpointId], owner: &iroh::SecretKey) -> Value {
        self.signed_as(route::ROUTE, endpoints, owner)
    }
    fn signed_as(
        &self,
        protocol: &str,
        endpoints: &[iroh::EndpointId],
        owner: &iroh::SecretKey,
    ) -> Value {
        let (signer, _, _) =
            app_host::identity_in(&self.root.join("owner"), app_host::SEED_IDENTITY_FILE).unwrap();
        let rows = [self.live(0), self.live(1)];
        let offers = [self.members[0].offer.clone(), self.members[1].offer.clone()];
        signed_route_as(
            protocol,
            span(B::MemberChosenWall),
            &signer,
            self.seed.coordinator.swarm_id(),
            &rows,
            &offers,
            endpoints,
            owner,
        )
        .1
    }
}
fn product_epoch(s: &Swarm, tokens: &[u32]) -> (Epoch, Vec<Member>) {
    let loopback: IpAddr = "127.0.0.1".parse().unwrap();
    let policy = LanPolicy::with_explicit(vec![loopback]).unwrap();
    let (members, verified) = s.serve_lan(loopback, &policy, tokens);
    let owner = iroh::SecretKey::generate();
    let envelope = s.signed(
        &members.iter().map(|m| m.endpoint).collect::<Vec<_>>(),
        &owner,
    );
    for m in &members {
        m.route.send(envelope.clone()).unwrap();
    }
    let epoch = Epoch::open(
        s.seed.coordinator.clone(),
        envelope,
        owner,
        Carriers::Lan {
            interface: loopback,
            policy,
            verified,
        },
        Arc::new(Text),
    )
    .unwrap();
    (epoch, members)
}
fn bearer(e: &Epoch) -> String {
    format!("Authorization: Bearer {}\r\n", e.bearer())
}
fn post(e: &Epoch, max_new_tokens: u64) -> (u16, Value) {
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":"p","max_new_tokens":max_new_tokens,
        "workload_profile_id":"interactive","qos_class":"interactive",
        "qualification":route::qualification(e.host().message()).unwrap()});
    sse_client::http(
        e.addr(),
        "POST",
        "/v1/inference",
        &bearer(e),
        &doc.to_string(),
    )
}
/// Submit over HTTP and stream events into a channel (reader thread owned).
fn chat(
    e: &Epoch,
    max_new_tokens: u64,
) -> (
    Value,
    mpsc::Receiver<(Value, Instant)>,
    std::thread::JoinHandle<()>,
) {
    let (status, accepted) = post(e, max_new_tokens);
    assert_eq!(status, 202, "{accepted}");
    let (tx, rx) = mpsc::channel();
    let (addr, path) = (
        e.addr(),
        accepted["stream_path"].as_str().unwrap().to_owned(),
    );
    let auth = format!(
        "{}X-Mycelium-Session: {}\r\n",
        bearer(e),
        accepted["session_token"].as_str().unwrap()
    );
    let reader = std::thread::spawn(move || {
        sse_client::stream(addr, path, auth, tx, span(B::WorkerStartup))
    });
    (accepted, rx, reader)
}
fn until_terminal(rx: &mpsc::Receiver<(Value, Instant)>, tokens: usize) -> Vec<Value> {
    let mut all = Vec::new();
    loop {
        let (event, _) = rx.recv_timeout(span(B::WorkerStartup)).unwrap();
        let done = matches!(
            event["type"].as_str(),
            Some("completed" | "cancelled" | "failed")
        );
        all.push(event);
        if done || all.iter().filter(|e| e["type"] == "token").count() >= tokens {
            return all;
        }
    }
}
fn token_ids(all: &[Value]) -> Vec<u64> {
    all.iter()
        .filter(|e| e["type"] == "token")
        .map(|e| e["token_id"].as_u64().unwrap())
        .collect()
}
#[test]
fn product_epoch_streams_chat_over_its_gateway_and_close_frees_the_port() {
    let s = swarm();
    let (epoch, members) = product_epoch(&s, &[5, 9, 3, 7]);
    assert_eq!(
        epoch.worker_pids(),
        vec![100, 101],
        "pids named by the signed route"
    );
    let (_, rx, reader) = chat(&epoch, 4);
    let all = until_terminal(&rx, usize::MAX);
    reader.join().unwrap();
    assert_eq!(all.last().unwrap()["type"], "completed", "{all:?}");
    assert_eq!(token_ids(&all), vec![5, 9, 3, 7]);
    // N3 through the product gateway: the spent epoch admits nothing more.
    let (status, refused) = post(&epoch, 1);
    assert_eq!(
        (status, refused["error"].as_str()),
        (409, Some("route_dropped"))
    );
    let addr = epoch.addr();
    let refusals = epoch.close();
    assert!(
        refusals.iter().all(|r| r == "route_dropped"),
        "pump refusals recorded: {refusals:?}"
    );
    assert!(
        std::net::TcpStream::connect(addr).is_err(),
        "close joined the gateway and freed its port"
    );
    for m in members {
        let report = m.thread.join().unwrap().unwrap();
        assert_eq!(report["ack_delivered"], true, "{report}");
    }
}
/// REVIEW-laneB16f FU1 (v0.1.4 parity, `request_gateway/service.py:349,458`): the
/// epoch gateway caps ADMITTED requests (64, `gateway_capacity_exhausted`), not
/// open connections. Connections that never send a request (no bearer needed)
/// are bounded on their own, the oldest dropped for a newcomer, so a local
/// process holding 64 of them cannot starve the owner's chat; event streams
/// count against the admitted cap; Stop and status are never refused for
/// capacity. Finished connections stop counting (REVIEW-laneB16 F3).
#[test]
fn product_epoch_gateway_caps_admitted_requests_and_keeps_stop_servable() {
    use std::io::{Read, Write};
    let s = swarm();
    let (epoch, members) = product_epoch(&s, &[5, 9, 3, 7]);
    let cap = B::PyFinalMyceliumRequestGatewayServiceMaxPendingRequestsL310C36
        .count()
        .unwrap()
        .get() as usize;
    let patience = span(B::RouterClose);
    let connect = || {
        let stream = std::net::TcpStream::connect_timeout(&epoch.addr(), patience).unwrap();
        stream.set_read_timeout(Some(patience)).unwrap();
        stream
    };
    // 1. A local process holds `cap` connections and never sends a byte.
    let idle: Vec<std::net::TcpStream> = (0..cap).map(|_| connect()).collect();
    // 2. The owner's chat is still admitted and streams.
    let (accepted, rx, reader) = chat(&epoch, 32);
    let mut all = until_terminal(&rx, 2);
    // ... because the oldest silent connection was dropped instead.
    let mut first = &idle[0];
    let mut probe = [0u8; 1];
    assert_eq!(
        first.read(&mut probe).unwrap_or(0),
        0,
        "the oldest silent connection is dropped"
    );
    let auth = format!(
        "{}X-Mycelium-Session: {}\r\n",
        bearer(&epoch),
        accepted["session_token"].as_str().unwrap()
    );
    // 3. Submissions whose bodies never arrive hold the other admitted slots
    // (the chat's own event stream holds one).
    let held_request = format!(
        "POST /v1/inference HTTP/1.1\r\nHost: {}\r\n{}Content-Type: application/json\r\nContent-Length: 64\r\n\r\n",
        epoch.addr(),
        bearer(&epoch)
    );
    let held: Vec<std::net::TcpStream> = (1..cap)
        .map(|_| {
            let mut stream = connect();
            stream.write_all(held_request.as_bytes()).unwrap();
            stream
        })
        .collect();
    // 4. One more admitted request is refused with v0.1.4's code, never held.
    let over_request = format!(
        "POST /v1/inference HTTP/1.1\r\nHost: {}\r\n{}Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}",
        epoch.addr(),
        bearer(&epoch)
    );
    let end = Instant::now() + patience;
    let refused = loop {
        let mut over = connect();
        over.write_all(over_request.as_bytes()).unwrap();
        let mut text = String::new();
        over.read_to_string(&mut text).unwrap_or(0);
        if text.contains("gateway_capacity_exhausted") || Instant::now() >= end {
            break text;
        }
        std::thread::yield_now();
    };
    assert!(
        refused.starts_with("HTTP/1.1 503") && refused.contains("gateway_capacity_exhausted"),
        "request {} past the admitted cap was not refused: {refused:?}",
        cap + 1
    );
    // 5. Status and Stop are served while every admitted slot is held.
    let (status, body) =
        sse_client::http(epoch.addr(), "GET", "/v1/qualification/current", &auth, "");
    assert_eq!(status, 200, "status refused under load: {body}");
    let path = format!("/v1/inference/{}", accepted["request_id"].as_str().unwrap());
    let (status, body) = sse_client::http(epoch.addr(), "DELETE", &path, &auth, "");
    assert_eq!(status, 202, "Stop refused under load: {body}");
    all.extend(until_terminal(&rx, usize::MAX));
    reader.join().unwrap();
    assert_eq!(all.last().unwrap()["type"], "cancelled", "{all:?}");
    // 6. Ended connections stop counting: once the held submissions go, a
    // submission passes the gateway again and the spent epoch itself answers it.
    drop(held);
    drop(idle);
    let end = Instant::now() + patience;
    let answered = loop {
        let (status, body) = post(&epoch, 1);
        if status != 503 || Instant::now() >= end {
            break (status, body);
        }
        std::thread::yield_now();
    };
    assert_eq!(
        (answered.0, answered.1["error"].as_str()),
        (409, Some("route_dropped")),
        "finished connections still count: {answered:?}"
    );
    epoch.close();
    for m in members {
        let report = m.thread.join().unwrap().unwrap();
        assert_eq!(report["ack_delivered"], true, "{report}");
    }
}
#[test]
fn product_epoch_stop_over_its_gateway_cancels_after_both_stages_acknowledge() {
    let s = swarm();
    let (epoch, members) = product_epoch(&s, &[5, 9, 3, 7]);
    let (accepted, rx, reader) = chat(&epoch, 32);
    let mut all = until_terminal(&rx, 2);
    let auth = format!(
        "{}X-Mycelium-Session: {}\r\n",
        bearer(&epoch),
        accepted["session_token"].as_str().unwrap()
    );
    let path = format!("/v1/inference/{}", accepted["request_id"].as_str().unwrap());
    let (status, body) = sse_client::http(epoch.addr(), "DELETE", &path, &auth, "");
    assert_eq!(status, 202, "{body}");
    all.extend(until_terminal(&rx, usize::MAX));
    reader.join().unwrap();
    assert_eq!(all.last().unwrap()["type"], "cancelled", "{all:?}");
    let streamed = token_ids(&all);
    assert_eq!(streamed, [5, 9, 3, 7, 5, 9, 3, 7][..streamed.len()]);
    let end = Instant::now() + span(B::WorkerStartup);
    let mut seen = 0;
    while !epoch.handle().state.lock().unwrap().settled() {
        assert!(Instant::now() < end, "teardown bounded");
        seen = epoch.handle().wait(seen, span(B::OwnerPoll));
    }
    assert_eq!(epoch.qualification().unwrap_err().code, "route_dropped");
    epoch.close();
    for m in members {
        assert_eq!(m.thread.join().unwrap().unwrap()["ack_delivered"], true);
    }
}
#[test]
fn product_epoch_refuses_an_unreachable_stage_with_a_typed_error() {
    let s = swarm();
    // The registered carrier keys, but nothing listens at the dialled ports.
    let dead: Vec<std::net::SocketAddr> = (0..2)
        .map(|_| {
            std::net::UdpSocket::bind("127.0.0.1:0")
                .unwrap()
                .local_addr()
                .unwrap()
        })
        .collect();
    let endpoints: Vec<iroh::EndpointId> = s.members.iter().map(|m| m.secret.public()).collect();
    let owner = iroh::SecretKey::generate();
    let envelope = s.signed(&endpoints, &owner);
    let peers = (0..2)
        .map(|i| StagePeer {
            node_id: s.live(i)["node_id"].as_str().unwrap().into(),
            endpoint: endpoints[i],
            socket: dead[i],
            generation: s.live(i)["generation"].as_u64().unwrap(),
        })
        .collect();
    let began = Instant::now();
    let refused = Epoch::open(
        s.seed.coordinator.clone(),
        envelope,
        owner,
        Carriers::Loopback(peers),
        Arc::new(Text),
    )
    .err()
    .expect("an unreachable stage is refused, never a half-open epoch");
    eprintln!(
        "B16 unreachable stage: {} after {:?}",
        refused.code,
        began.elapsed()
    );
    assert_eq!(refused.status, 409);
    assert!(refused.code.starts_with("joined_route"), "{}", refused.code);
    // The executor's setup bound (WorkerStartup) plus its bounded close.
    assert!(
        began.elapsed() < span(B::WorkerStartup) + span(B::RouterClose),
        "bounded dial"
    );
}

// ---- SL (gap row 16): cross-host member death settled by the SEED's lease.
// v0.1.4 treats a member whose lease ran out as gone for good: the owner refuses
// every command and result wait to it (`control_session.py:315-326`
// `managed_control_member_stale`), the seed refuses its renewal
// (`coordinator.py:1367-1370`) and the member refuses its own work past its
// lease (`peer.py:625-640`). The seed view below is injected: a fixed seed clock
// and per-node (generation, lease expiry); the dead member is a stage that went
// silent mid-request (its compute blocked on a gate), never a second host.
use api::m16_route::SeedMembers;
use std::collections::BTreeMap;

/// The seed clock of these tests (an instant, not a timeout).
const SEED_NOW: f64 = 2_000_000.0;
fn clock_skew() -> f64 {
    span(B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31).as_secs_f64()
}
/// The owner's per-token hop deadline (`m16_route::generate`).
fn hop_deadline() -> Duration {
    span(B::PyFinalScriptsRunA11RouteBenchmarkTargetTpotMsL398C12)
}
fn lease() -> f64 {
    span(B::PyFinalMyceliumSeedCoordinatorLeaseSecondsL180C31).as_secs_f64()
}
#[derive(Clone, Default)]
struct SeedView(Arc<Mutex<BTreeMap<String, (u64, f64)>>>);
impl SeedView {
    /// Both members current: generation 1, lease running a full term from now.
    fn fresh(nodes: &[&str]) -> Self {
        let view = Self::default();
        for n in nodes {
            view.set(n, 1, SEED_NOW + lease());
        }
        view
    }
    fn set(&self, node: &str, generation: u64, lease_expires_at: f64) {
        self.0
            .lock()
            .unwrap()
            .insert(node.into(), (generation, lease_expires_at));
    }
    fn members(&self) -> SeedMembers {
        let view = self.0.clone();
        Arc::new(move |node: &str| {
            let (generation, lease) = *view.lock().unwrap().get(node)?;
            Some((
                json!({"node_id":node,"generation":generation,"lease_expires_at":lease,
                    "lifecycle_state":"RUNNING"}),
                SEED_NOW,
            ))
        })
    }
}
fn observe(r: &Route, view: &SeedView) -> Vec<usize> {
    m16_route::observe_member_leases(&r.handle, &r.message, &view.members()).unwrap()
}
fn evidence(r: &Route, stage: usize) -> Option<Value> {
    r.handle.state.lock().unwrap().stages[stage]
        .death_evidence
        .clone()
}
/// Tick the service like `pump_until`, but report instead of panicking at the bound.
fn pump_for(r: &Route, bound: Duration, done: impl Fn(&Route) -> bool) -> bool {
    let end = Instant::now() + bound;
    let mut seen = 0;
    while !done(r) {
        if Instant::now() >= end {
            return false;
        }
        if let Err(e) = r.service.tick() {
            panic!("tick: {e:?}; {}", r.state());
        }
        seen = r.handle.wait(seen, span(B::OwnerPoll));
    }
    true
}
fn silent_at(step: u64) -> (mpsc::Sender<()>, Script) {
    let (open, gate) = mpsc::channel();
    (
        open,
        Script {
            tokens: vec![5, 9, 3, 7],
            step_gate: Some((step, gate)),
            ..Script::default()
        },
    )
}

/// Two members, the second dies mid-answer: once the seed shows its lease ran
/// out (past the members' clock tolerance), the request fails with the code and
/// settles at once; the surviving member ACKs, the dead one is settled by the
/// seed's evidence, recorded once; the epoch is spent afterwards.
#[test]
fn a_member_silent_mid_request_settles_when_its_seed_lease_runs_out() {
    let (open, script) = silent_at(2);
    let r = route([Script::default(), script], 32);
    r.pump_until(span(B::WorkerStartup), |r| tokens(r) >= 2);
    let view = SeedView::fresh(&["node-a", "node-b"]);
    assert_eq!(
        observe(&r, &view),
        Vec::<usize>::new(),
        "both leases current"
    );
    // The last renewal of node-b was a full lease ago, plus the clock tolerance.
    view.set("node-b", 1, SEED_NOW - clock_skew());
    assert_eq!(observe(&r, &view), vec![1], "{}", r.state());
    assert!(
        pump_for(&r, hop_deadline(), |r| r.terminal().is_some()),
        "settled before the hop deadline could: {}",
        r.state()
    );
    assert_eq!(r.terminal().as_deref(), Some("failed"), "{}", r.state());
    {
        let o = r.handle.state.lock().unwrap();
        assert_eq!(o.failure, Some("joined_route_member_stale"));
        assert!(o.settled());
        assert!(o.stages[0].acknowledged_ms.is_some(), "{:?}", o.stages[0]);
        assert!(o.stages[0].death_evidence.is_none());
        assert_eq!(o.teardown_attempts, 1, "no re-dial of a settled stage");
    }
    let dead = evidence(&r, 1).unwrap();
    assert_eq!(
        dead,
        json!({"source":"seed_member_lease","node_id":"node-b","generation":1,
            "lease_expires_at":SEED_NOW - clock_skew(),"observed_at":SEED_NOW,
            "clock_skew_seconds":clock_skew()})
    );
    assert_eq!(observe(&r, &view), Vec::<usize>::new(), "recorded once");
    assert_eq!(evidence(&r, 1).unwrap(), dead);
    assert_eq!(r.service.qualification().unwrap_err().code, "route_dropped");
    open.send(()).unwrap();
    r.finish();
}

/// The realistic order on two Macs: the hop deadline fails the request first
/// (the member's Mac is gone, nothing answers the teardown), the debt is
/// retained without a terminal; when the seed's lease runs out the stage is
/// settled and the request publishes `failed` with the code it failed with.
#[test]
fn a_member_dead_past_the_hop_deadline_publishes_its_failure_once_its_lease_runs_out() {
    let (open, script) = silent_at(2);
    let r = route([Script::default(), script], 32);
    r.pump_until(span(B::WorkerStartup), |r| {
        let o = r.handle.state.lock().unwrap();
        o.failure.is_some() && o.teardown_attempts >= 1 && !o.teardown_busy
    });
    assert_eq!(
        r.handle.state.lock().unwrap().failure,
        Some("joined_route_hop_deadline")
    );
    assert!(r.terminal().is_none(), "debt retained while unsettled");
    let view = SeedView::fresh(&["node-a", "node-b"]);
    view.set("node-b", 1, SEED_NOW - lease());
    assert_eq!(observe(&r, &view), vec![1], "{}", r.state());
    assert!(
        pump_for(&r, span(B::WorkerStartup), |r| r.terminal().is_some()),
        "{}",
        r.state()
    );
    assert_eq!(r.terminal().as_deref(), Some("failed"));
    let o = r.handle.state.lock().unwrap();
    assert_eq!(
        o.failure,
        Some("joined_route_hop_deadline"),
        "first cause kept"
    );
    assert!(o.settled());
    drop(o);
    open.send(()).unwrap();
    r.finish();
}

/// A member that dies while the epoch is idle: the epoch is no longer offered
/// and the queued request is refused with the code (v0.1.4 refuses admission to
/// a lost peer, `route.py:3086-3106`), never dispatched to the dead member.
#[test]
fn a_member_silent_while_idle_retires_the_epoch_on_its_seed_lease() {
    let r = route([Script::default(), scripted(&[5, 9, 3, 7])], 4);
    let view = SeedView::fresh(&["node-a", "node-b"]);
    view.set("node-b", 1, SEED_NOW - lease());
    assert_eq!(observe(&r, &view), vec![1], "{}", r.state());
    assert_eq!(
        r.service.qualification().unwrap_err().code,
        "joined_route_member_stale"
    );
    assert!(pump_for(&r, hop_deadline(), |r| r.terminal().is_some()));
    assert_eq!(r.terminal().as_deref(), Some("failed"), "{}", r.state());
    assert_eq!(evidence(&r, 1).unwrap()["source"], "seed_member_lease");
    assert!(evidence(&r, 0).is_none());
    assert_eq!(r.handle.state.lock().unwrap().tokens, Vec::<u32>::new());
    r.finish();
}

/// A member that renewed before expiry, or whose lease ended inside the
/// members' clock tolerance, is not dead: the request completes normally.
#[test]
fn a_member_renewed_before_expiry_or_inside_the_clock_skew_is_not_dead() {
    let r = route([Script::default(), scripted(&[5, 9, 3, 7])], 4);
    let view = SeedView::fresh(&["node-a", "node-b"]);
    view.set("node-b", 1, SEED_NOW - clock_skew() + 0.5);
    assert_eq!(observe(&r, &view), Vec::<usize>::new(), "inside the skew");
    view.set("node-b", 1, SEED_NOW + lease());
    assert_eq!(observe(&r, &view), Vec::<usize>::new(), "renewed");
    r.pump_until(span(B::WorkerStartup), |r| r.terminal().is_some());
    assert_eq!(r.terminal().as_deref(), Some("completed"), "{}", r.state());
    let o = r.handle.state.lock().unwrap();
    assert!(o.stages.iter().all(|s| s.death_evidence.is_none()));
    assert_eq!(o.tokens, vec![5, 9, 3, 7]);
    drop(o);
    r.finish();
}

/// The member comes back after its lease ran out (resumed: a newer generation
/// with a fresh lease): the route's member stays settled, evidence unchanged and
/// recorded once; the spent epoch is not revived (v0.1.4: an expired member must
/// re-join; the old route never serves it again).
#[test]
fn a_member_back_after_its_lease_ran_out_stays_settled_once() {
    let r = route([Script::default(), scripted(&[5, 9, 3, 7])], 4);
    let view = SeedView::fresh(&["node-a", "node-b"]);
    view.set("node-b", 1, SEED_NOW - lease());
    assert_eq!(observe(&r, &view), vec![1]);
    let dead = evidence(&r, 1).unwrap();
    view.set("node-b", 2, SEED_NOW + lease());
    assert_eq!(observe(&r, &view), Vec::<usize>::new());
    view.set("node-b", 1, SEED_NOW + lease());
    assert_eq!(observe(&r, &view), Vec::<usize>::new());
    assert_eq!(evidence(&r, 1).unwrap(), dead);
    assert_eq!(
        r.service.qualification().unwrap_err().code,
        "joined_route_member_stale"
    );
    r.finish();
}

/// A resumed member (newer generation) whose NEW lease later runs out is not
/// evidence about the route's member: the route signed generation 1.
#[test]
fn a_lease_of_another_generation_is_not_evidence_for_the_route_member() {
    let r = route([Script::default(), scripted(&[5, 9, 3, 7])], 4);
    let view = SeedView::fresh(&["node-a", "node-b"]);
    view.set("node-b", 2, SEED_NOW - lease());
    assert_eq!(observe(&r, &view), Vec::<usize>::new());
    assert!(evidence(&r, 1).is_none());
    r.finish();
}

/// The product epoch's own pump reads the seed view: a member that goes silent
/// mid-answer ends the chat stream `failed` with the code once its seed lease
/// ran out, with nothing but the injected seed clock moving.
#[test]
fn product_epoch_settles_a_member_whose_seed_lease_ran_out() {
    let s = swarm();
    let loopback: IpAddr = "127.0.0.1".parse().unwrap();
    let policy = LanPolicy::with_explicit(vec![loopback]).unwrap();
    let (open, script) = silent_at(2);
    let (members, verified) = s.serve_lan_scripts(loopback, &policy, [Script::default(), script]);
    let owner = iroh::SecretKey::generate();
    let envelope = s.signed(
        &members.iter().map(|m| m.endpoint).collect::<Vec<_>>(),
        &owner,
    );
    for m in &members {
        m.route.send(envelope.clone()).unwrap();
    }
    let nodes: Vec<String> = (0..2)
        .map(|i| s.live(i)["node_id"].as_str().unwrap().to_owned())
        .collect();
    let view = SeedView::fresh(&[nodes[0].as_str(), nodes[1].as_str()]);
    let epoch = Epoch::open_watched(
        s.seed.coordinator.clone(),
        envelope,
        owner,
        Carriers::Lan {
            interface: loopback,
            policy,
            verified,
        },
        Arc::new(Text),
        view.members(),
    )
    .unwrap();
    let (_, rx, reader) = chat(&epoch, 32);
    let mut all = until_terminal(&rx, 2);
    view.set(&nodes[1], 1, SEED_NOW - clock_skew());
    // Collect for less than the hop deadline: only the seed can end it in time.
    let end = Instant::now() + hop_deadline();
    while let Some(left) = end.checked_duration_since(Instant::now()) {
        let Ok((event, _)) = rx.recv_timeout(left) else {
            break;
        };
        let done = matches!(
            event["type"].as_str(),
            Some("completed" | "cancelled" | "failed")
        );
        all.push(event);
        if done {
            break;
        }
    }
    let last = all.last().unwrap().clone();
    assert_eq!(last["type"], "failed", "{all:?}");
    assert_eq!(last["code"], "joined_route_member_stale", "{all:?}");
    reader.join().unwrap();
    let o = epoch.handle().state.lock().unwrap();
    assert!(o.settled());
    assert_eq!(
        o.stages[1].death_evidence.as_ref().unwrap()["node_id"],
        nodes[1].as_str()
    );
    drop(o);
    assert_eq!(epoch.qualification().unwrap_err().code, "route_dropped");
    epoch.close();
    open.send(()).unwrap();
    for m in members {
        m.thread.join().unwrap().unwrap();
    }
}

// ---- EP1: ONE multi-request route epoch (`mycelium.joined_route.v2`).
// v0.1.4 serves a whole run on ONE route with the native workers resident; per
// chat it creates and releases only request state (DemoApp app.py:505 ->
// chat.py:300-398 -> RegistryClient.stream :106-147 -> route.infer
// mycelium_live/route.py:4592; request-scoped cleanup proof :6064-6110; one
// chat at a time, chat.py:319-321). Same REAL owner carrier, M16 service and
// member stage server as above over real Iroh QUIC; only the compute is
// controlled and logs every step (with its session position), reset and close.
use runtime::{iroh::IrohTransport, iroh_receive::RouterReceiver};
struct EpochRoute {
    message: Value,
    service: M16Requests,
    handle: RouteHandle,
    members: Vec<Member>,
    /// The seed's current generation per stage member, as the carrier reads it.
    generations: Arc<Mutex<[u64; 2]>>,
}
fn epoch_route(scripts: [Script; 2]) -> EpochRoute {
    epoch_route_for(scripts, span(B::MemberChosenWall))
}
/// Two stage servers, a seed-signed v2 route living `lifetime` from now, the
/// owner carrier and an M16 service bounded by the epoch's admission row.
fn epoch_route_for(scripts: [Script; 2], lifetime: Duration) -> EpochRoute {
    let seed = Identity::from_secret("test-seed", &[7; 32]).unwrap();
    let rows: Vec<Value> = ["node-a", "node-b"]
        .iter()
        .map(|n| {
            json!({"node_id":n,"generation":1,"swarm_id":"swarm",
            "seed_key_digest":seed.verification_key_digest()})
        })
        .collect();
    let offers: Vec<Value> = (0..2).map(|i| json!({"controlled_offer":i})).collect();
    let [first, second] = scripts;
    let members = vec![
        member(rows[0].clone(), offers[0].clone(), false, first, proof(0)),
        member(rows[1].clone(), offers[1].clone(), true, second, proof(1)),
    ];
    let peers: Vec<StagePeer> = members
        .iter()
        .zip(&rows)
        .map(|(m, r)| StagePeer {
            node_id: r["node_id"].as_str().unwrap().into(),
            endpoint: m.endpoint,
            socket: m.socket,
            generation: 1,
        })
        .collect();
    let owner = iroh::SecretKey::generate();
    let endpoints: Vec<iroh::EndpointId> = members.iter().map(|m| m.endpoint).collect();
    let (message, envelope) = signed_route_as(
        route::ROUTE_V2,
        lifetime,
        &seed,
        "swarm",
        &rows,
        &offers,
        &endpoints,
        &owner,
    );
    for m in &members {
        m.route.send(envelope.clone()).unwrap();
    }
    let generations = Arc::new(Mutex::new([1u64, 1]));
    let seen = generations.clone();
    let currentness: Currentness = Arc::new(move |node: &str| {
        let g = seen.lock().unwrap();
        match node {
            "node-a" => Some(g[0]),
            "node-b" => Some(g[1]),
            _ => None,
        }
    });
    let (executor, handle) =
        RouteExecutor::connect(message.clone(), owner, peers, currentness, Arc::new(Text)).unwrap();
    let runtime = m16_route::runtime(
        &message,
        Arc::new(Wall),
        Arc::new(Wall),
        Arc::new(Ids::default()),
    )
    .unwrap();
    let host = Arc::new(Following {
        message: message.clone(),
        handle: handle.clone(),
        prepared: AtomicU64::new(0),
    });
    let sessions = B::JoinedEpochMaxSessions.count().unwrap().get() as usize;
    let service = M16Requests::new(
        runtime,
        host,
        Box::new(executor),
        Config::new(16, sessions, 0.0).unwrap(),
    )
    .unwrap();
    EpochRoute {
        message,
        service,
        handle,
        members,
        generations,
    }
}
impl EpochRoute {
    fn submit(&self, max_new_tokens: u64) -> Result<(String, String), PublicError> {
        self.service
            .submit(submission(&self.message, max_new_tokens))
            .map(|a| (a.request_id, a.session_token))
    }
    fn pump_until(&self, bound: Duration, done: impl Fn(&Self) -> bool) {
        let end = Instant::now() + bound;
        let mut seen = 0;
        while !done(self) {
            assert!(Instant::now() < end, "pump bound: {}", self.state());
            if let Err(e) = self.service.tick() {
                panic!("tick: {e:?}; {}", self.state());
            }
            seen = self.handle.wait(seen, span(B::OwnerPoll));
        }
    }
    fn terminal(&self, id: &str) -> Option<String> {
        self.service.terminal(id).unwrap()
    }
    fn tokens(&self) -> Vec<u32> {
        self.handle.state.lock().unwrap().tokens.clone()
    }
    /// One chat to its terminal: (terminal, tokens the carrier streamed).
    fn chat(&self, max_new_tokens: u64) -> (String, Vec<u32>) {
        let submitted = self.submit(max_new_tokens);
        assert!(
            submitted.is_ok(),
            "chat refused: {:?}; {}",
            submitted.as_ref().err().map(|e| e.code),
            self.state()
        );
        let (id, _) = submitted.unwrap();
        self.pump_until(span(B::WorkerStartup), |r| r.terminal(&id).is_some());
        (self.terminal(&id).unwrap(), self.tokens())
    }
    fn events(&self, stage: usize) -> Vec<(&'static str, u64)> {
        self.members[stage].log.lock().unwrap().events.clone()
    }
    fn state(&self) -> String {
        let o = self.handle.state.lock().unwrap();
        format!(
            "requests={} settled={} tokens={:?} completed={} failure={:?} epoch_failure={:?} closed={} attempts={} finished={} stages={:?}",
            o.requests,
            o.settled_requests,
            o.tokens,
            o.completed,
            o.failure,
            o.epoch_failure,
            o.closed,
            o.teardown_attempts,
            o.teardown_finished,
            o.stages
                .iter()
                .map(|s| (s.acknowledged_ms, s.failure.clone(), s.touched))
                .collect::<Vec<_>>(),
        )
    }
    /// Wait (bounded, event-driven) until the carrier closed the epoch.
    fn until_closed(&self) -> bool {
        let end = Instant::now() + span(B::WorkerStartup);
        let mut seen = 0;
        while !self.handle.state.lock().unwrap().closed {
            if Instant::now() >= end {
                return false;
            }
            seen = self.handle.wait(seen, span(B::OwnerPoll));
        }
        true
    }
    /// Drop the owner (its carrier closes the epoch) and collect member reports.
    fn finish(self) -> Vec<Value> {
        let EpochRoute {
            service, members, ..
        } = self;
        drop(service);
        members
            .into_iter()
            .map(|m| m.thread.join().unwrap().unwrap())
            .collect()
    }
}
fn kinds(events: &[(&'static str, u64)], kind: &str) -> usize {
    events.iter().filter(|e| e.0 == kind).count()
}

/// EP-PLAN RED 1: chat 2 runs on the SAME epoch: same signed route and
/// qualification, the same four carrier connections, the same compute (no new
/// prepare), one reset per chat and never a close while the epoch serves.
#[test]
fn a_second_chat_on_the_same_epoch_runs_without_a_new_prepare_or_route() {
    let r = epoch_route([Script::default(), scripted(&[5, 9, 3, 7])]);
    let qualification = r.service.qualification().unwrap();
    for chat in 0..2 {
        assert_eq!(
            r.chat(4),
            ("completed".to_owned(), vec![5, 9, 3, 7]),
            "chat {chat}: {}",
            r.state()
        );
    }
    assert_eq!(r.service.qualification().unwrap(), qualification);
    {
        // Formatted BEFORE the lock: `state()` takes the same mutex, so a
        // failing assertion would otherwise deadlock instead of failing.
        let shown = r.state();
        let o = r.handle.state.lock().unwrap();
        assert_eq!((o.requests, o.settled_requests), (2, 2), "{shown}");
        assert_eq!(o.connections, 4, "no new carrier connection for chat 2");
        assert!(!o.retired());
    }
    for stage in 0..2 {
        let events = r.events(stage);
        assert_eq!(kinds(&events, "reset"), 2, "stage {stage}: {events:?}");
        assert_eq!(
            kinds(&events, "close") + kinds(&events, "abort"),
            0,
            "stage {stage}: {events:?}"
        );
        assert_eq!(
            events.iter().filter(|e| **e == ("step", 0)).count(),
            2,
            "one PREFILL per chat on a fresh session: {events:?}"
        );
    }
    let logs: Vec<_> = r.members.iter().map(|m| m.log.clone()).collect();
    let reports = r.finish();
    for (report, log) in reports.iter().zip(logs) {
        assert_eq!(report["multi_request"], true, "{report}");
        assert_eq!(report["requests_served"], 2, "{report}");
        assert_eq!(report["connections_accepted"], 2, "{report}");
        assert_eq!(report["teardown"], "owner_closed", "{report}");
        let events = log.lock().unwrap().events.clone();
        assert_eq!(
            kinds(&events, "close"),
            1,
            "closed once, at the end: {events:?}"
        );
        assert_eq!(events.last(), Some(&("close", 0)));
    }
}

/// EP-PLAN RED 2: Stop of one chat (M16 cancel = request-scoped
/// PathCancellation) ends that chat `cancelled`; the epoch keeps serving.
#[test]
fn stop_of_one_chat_leaves_the_epoch_serving() {
    let r = epoch_route([Script::default(), scripted(&[5, 9, 3, 7])]);
    let (id, token) = r.submit(32).unwrap();
    r.pump_until(span(B::WorkerStartup), |r| r.tokens().len() >= 2);
    assert!(r.service.cancel(&id, Some(&token)).unwrap());
    r.pump_until(span(B::WorkerStartup), |r| r.terminal(&id).is_some());
    assert_eq!(
        r.terminal(&id).as_deref(),
        Some("cancelled"),
        "{}",
        r.state()
    );
    let still = r.service.qualification();
    assert!(
        still.is_ok(),
        "a stopped chat retired the epoch: {:?}",
        still.err().map(|e| e.code)
    );
    assert_eq!(r.chat(4), ("completed".to_owned(), vec![5, 9, 3, 7]));
    for stage in 0..2 {
        let events = r.events(stage);
        assert_eq!(kinds(&events, "reset"), 2, "stage {stage}: {events:?}");
        assert_eq!(kinds(&events, "close") + kinds(&events, "abort"), 0);
    }
    let reports = r.finish();
    assert!(
        reports.iter().all(|v| v["requests_served"] == 2),
        "{reports:?}"
    );
}

/// EP-PLAN RED 3 (cleanup-proof rule): chat 2 never touches a stage whose
/// previous request is not proven clean. Stage 1's reset is held: chat 1 is
/// not settled, chat 2 stays queued and no stage sees a step of it; once the
/// reset is done, chat 2's PREFILL runs at session position 0 after it.
#[test]
fn a_cancelled_or_failed_request_is_reset_before_the_next_prefill() {
    let (entered_tx, entered) = mpsc::channel();
    let (open, gate) = mpsc::channel();
    let r = epoch_route([
        Script::default(),
        Script {
            tokens: vec![5, 9, 3, 7],
            reset_gate: Some((entered_tx, gate)),
            ..Script::default()
        },
    ]);
    let (first, _) = r.submit(4).unwrap();
    r.pump_until(span(B::WorkerStartup), |_| entered.try_recv().is_ok());
    let queued = r.submit(4);
    assert!(
        queued.is_ok(),
        "{:?}",
        queued.as_ref().err().map(|e| e.code)
    );
    let (second, _) = queued.unwrap();
    for _ in 0..3 {
        r.service.tick().unwrap();
    }
    assert_eq!(
        r.terminal(&first),
        None,
        "chat 1 settles only after its reset"
    );
    assert_eq!(r.terminal(&second), None);
    for stage in 0..2 {
        assert_eq!(
            kinds(&r.events(stage), "step"),
            4,
            "stage {stage} saw chat 2 before stage 1 was clean: {:?}",
            r.events(stage)
        );
    }
    open.send(()).unwrap();
    r.pump_until(span(B::WorkerStartup), |r| r.terminal(&second).is_some());
    assert_eq!(r.terminal(&first).as_deref(), Some("completed"));
    assert_eq!(
        r.terminal(&second).as_deref(),
        Some("completed"),
        "{}",
        r.state()
    );
    let chat = [
        ("step", 0),
        ("step", 1),
        ("step", 2),
        ("step", 3),
        ("reset", 0),
    ];
    for stage in 0..2 {
        assert_eq!(r.events(stage), [chat, chat].concat(), "stage {stage}");
    }
    r.finish();
}

/// EP-PLAN RED 3, second half: a failure that leaves no usable compute (the
/// final stage's result is undecodable: it released its compute at once) is a
/// carrier-level failure: the request fails and the EPOCH retires; every
/// compute ends released (stage 0 reset by the teardown, then closed with the
/// epoch; stage 1 closed at its failure, never reset).
#[test]
fn an_interrupting_failure_retires_the_epoch_with_compute_released() {
    let r = epoch_route([
        Script::default(),
        Script {
            tokens: vec![5, 9, 3, 7],
            bad_logits_at: Some(2),
            ..Script::default()
        },
    ]);
    let (id, _) = r.submit(4).unwrap();
    r.pump_until(span(B::WorkerStartup), |r| r.terminal(&id).is_some());
    assert_eq!(r.terminal(&id).as_deref(), Some("failed"), "{}", r.state());
    assert_eq!(
        r.handle.state.lock().unwrap().failure,
        Some("joined_route_hop_deadline")
    );
    assert_eq!(r.submit(4).unwrap_err().code, "route_dropped");
    assert!(r.until_closed(), "the retired epoch closes: {}", r.state());
    let logs: Vec<_> = r.members.iter().map(|m| m.log.clone()).collect();
    let reports = r.finish();
    let events: Vec<_> = logs
        .iter()
        .map(|l| l.lock().unwrap().events.clone())
        .collect();
    assert_eq!(
        events[0][3..],
        [("reset", 0), ("close", 0)],
        "stage 0: {:?}",
        events[0]
    );
    assert_eq!(kinds(&events[1], "reset"), 0, "{:?}", events[1]);
    assert_eq!(kinds(&events[1], "close"), 1, "{:?}", events[1]);
    assert_eq!(
        reports[1]["failure"], "native_logits_invalid",
        "{}",
        reports[1]
    );
}

/// Invariant 1: a reset that fails is never ACKed. The member closes its
/// compute and REJECTS the PathCancellation; the owner retires the epoch and
/// settles the request through its bounded re-dial, which the member answers
/// after the close.
#[test]
fn a_failed_reset_is_never_acknowledged_and_retires_the_epoch() {
    let r = epoch_route([
        Script::default(),
        Script {
            tokens: vec![5, 9, 3, 7],
            reset_fails: true,
            ..Script::default()
        },
    ]);
    let (id, _) = r.submit(4).unwrap();
    r.pump_until(span(B::WorkerStartup), |r| r.terminal(&id).is_some());
    assert_eq!(
        r.terminal(&id).as_deref(),
        Some("completed"),
        "{}",
        r.state()
    );
    {
        // Formatted BEFORE the lock: `state()` takes the same mutex, so a
        // failing assertion would otherwise deadlock instead of failing.
        let shown = r.state();
        let o = r.handle.state.lock().unwrap();
        assert!(o.teardown_attempts >= 2, "settled by a re-dial: {shown}");
        assert!(o.stages[1].acknowledged_ms.is_some(), "{shown}");
        assert!(o.retired(), "{shown}");
    }
    assert_eq!(r.submit(4).unwrap_err().code, "route_dropped");
    let reports = r.finish();
    assert_eq!(reports[1]["ack_delivered"], false, "{}", reports[1]);
    assert!(reports[1]["redial_ack_ms"].is_number(), "{}", reports[1]);
    assert_eq!(reports[1]["native"]["graceful"], true, "{}", reports[1]);
}

fn product_epoch_v2(s: &Swarm, tokens: &[u32]) -> (Epoch, Vec<Member>) {
    let loopback: IpAddr = "127.0.0.1".parse().unwrap();
    let policy = LanPolicy::with_explicit(vec![loopback]).unwrap();
    let (members, verified) = s.serve_lan(loopback, &policy, tokens);
    let owner = iroh::SecretKey::generate();
    let envelope = s.signed_as(
        route::ROUTE_V2,
        &members.iter().map(|m| m.endpoint).collect::<Vec<_>>(),
        &owner,
    );
    for m in &members {
        m.route.send(envelope.clone()).unwrap();
    }
    let epoch = Epoch::open(
        s.seed.coordinator.clone(),
        envelope,
        owner,
        Carriers::Lan {
            interface: loopback,
            policy,
            verified,
        },
        Arc::new(Text),
    )
    .unwrap();
    (epoch, members)
}

/// EP-PLAN RED 4, through the product epoch's gateway: six chats on one
/// epoch, past the old M16 bound of four lifetime admissions (now the typed
/// row `JoinedEpochMaxSessions`, the shipped gateway's 1024 sessions).
#[test]
fn an_epoch_serves_more_than_the_old_four_admissions() {
    let s = swarm();
    let (epoch, members) = product_epoch_v2(&s, &[5, 9, 3, 7]);
    for n in 0..6 {
        let (_, rx, reader) = chat(&epoch, 4);
        let all = until_terminal(&rx, usize::MAX);
        reader.join().unwrap();
        assert_eq!(
            all.last().unwrap()["type"],
            "completed",
            "chat {n}: {all:?}"
        );
        assert_eq!(token_ids(&all), vec![5, 9, 3, 7], "chat {n}");
    }
    assert!(!epoch.handle().state.lock().unwrap().retired());
    epoch.close();
    for m in members {
        let report = m.thread.join().unwrap().unwrap();
        assert_eq!(report["requests_served"], 6, "{report}");
    }
}

/// EP-PLAN RED 6 (the owner side of rollover/ceiling/Stop of a run): closing
/// a long-lived epoch ends every member's serve with its compute closed
/// exactly once, after one reset per chat. The 480/570 s triggers that call
/// this close live in the work port (slice EP2).
#[test]
fn closing_a_long_lived_epoch_closes_member_compute_once() {
    let s = swarm();
    let (epoch, members) = product_epoch_v2(&s, &[5, 9, 3, 7]);
    for n in 0..3 {
        let (_, rx, reader) = chat(&epoch, 4);
        let all = until_terminal(&rx, usize::MAX);
        reader.join().unwrap();
        assert_eq!(
            all.last().unwrap()["type"],
            "completed",
            "chat {n}: {all:?}"
        );
    }
    let logs: Vec<_> = members.iter().map(|m| m.log.clone()).collect();
    epoch.close();
    for (m, log) in members.into_iter().zip(logs) {
        let report = m.thread.join().unwrap().unwrap();
        assert_eq!(report["teardown"], "owner_closed", "{report}");
        assert_eq!(report["requests_served"], 3, "{report}");
        let events = log.lock().unwrap().events.clone();
        assert_eq!(kinds(&events, "reset"), 3, "{events:?}");
        assert_eq!(kinds(&events, "close"), 1, "{events:?}");
        assert_eq!(kinds(&events, "abort"), 0, "{events:?}");
        assert_eq!(events.last(), Some(&("close", 0)));
    }
}

/// EP-PLAN RED 5 (replay): a raw owner on the member's real stage server.
/// PREFILL of request A, its PathCancellation (reset + ACK), then a PREFILL of
/// the settled request A again: the member refuses it, never steps it, and
/// ends its epoch with the compute closed.
#[test]
fn a_replayed_prefill_of_a_settled_request_is_refused() {
    let seed = Identity::from_secret("test-seed", &[7; 32]).unwrap();
    let rows: Vec<Value> = ["node-a", "node-b"]
        .iter()
        .map(|n| {
            json!({"node_id":n,"generation":1,"swarm_id":"swarm",
            "seed_key_digest":seed.verification_key_digest()})
        })
        .collect();
    let offers: Vec<Value> = (0..2).map(|i| json!({"controlled_offer":i})).collect();
    let entry = member(
        rows[0].clone(),
        offers[0].clone(),
        false,
        Script::default(),
        proof(0),
    );
    let owner = iroh::SecretKey::generate();
    let other = iroh::SecretKey::generate().public();
    let (message, envelope) = signed_route_as(
        route::ROUTE_V2,
        span(B::MemberChosenWall),
        &seed,
        "swarm",
        &rows,
        &offers,
        &[entry.endpoint, other],
        &owner,
    );
    entry.route.send(envelope).unwrap();
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = HostClock::default();
    let until = |id: B| {
        clock
            .monotonic()
            .checked_add(id.span(Entries::ZERO).unwrap())
            .unwrap()
    };
    let mut host =
        LoopbackHost::bind_with_secret(&rt, &clock, until(B::WorkerStartup), owner).unwrap();
    let addr = iroh::EndpointAddr::new(entry.endpoint).with_ip_addr(entry.socket);
    let data = host
        .connect_channel(
            &addr,
            transport::Channel::Router,
            &clock,
            until(B::WorkerStartup),
        )
        .unwrap();
    let control = host
        .connect_channel(
            &addr,
            transport::Channel::Router,
            &clock,
            until(B::WorkerStartup),
        )
        .unwrap();
    let fence = transport::ConnectionFence {
        endpoint_id: *entry.endpoint.as_bytes(),
        local_generation: 7,
        remote_generation: 1,
    };
    let current = move || fence;
    let mut to_data = IrohTransport::new(data.clone(), &rt, &clock, fence, current).unwrap();
    let mut from_data = RouterReceiver::new(data.clone(), &rt, &clock, fence, current).unwrap();
    let mut to_control = IrohTransport::new(control.clone(), &rt, &clock, fence, current).unwrap();
    let send = |t: &mut IrohTransport<'_, HostClock, _>, id: u128, frame: Vec<u8>| {
        transport::exchange(
            t,
            &transport::Exchange {
                peer: fence,
                channel: transport::Channel::Router,
                message_id: id.to_be_bytes(),
                payload: frame,
                deadline: until(B::RouterClose),
                maximum_response_bytes: protocol::remote::REMOTE_MAX_FRAME_BYTES,
            },
            &clock,
        )
        .map(drop)
    };
    let path = route::Path {
        request_id: "route-7-0".into(),
        path_id: "path-a".into(),
        path_attempt: 0,
    };
    let prefill = || {
        protocol::router_wire::encode_frame(
            "HopHeader",
            &route::hop(&message, &path, 0, 0).unwrap(),
            &route::entry_payload(&(0..35).collect::<Vec<u32>>()).unwrap(),
        )
        .unwrap()
    };
    let cancellation = || {
        protocol::router_wire::encode_frame(
            "PathCancellation",
            &route::cancellation(&message, &path),
            &[],
        )
        .unwrap()
    };
    let mut outcomes = Vec::new();
    outcomes.push(("prefill", send(&mut to_data, 0, prefill()).is_ok()));
    // The member forwards stage 0's output to the owner: take and ACK it.
    let forwarded = from_data
        .receive(until(B::WorkerStartup))
        .and_then(|d| d.acknowledge());
    outcomes.push(("forwarded", forwarded.is_ok()));
    outcomes.push(("cancel", send(&mut to_control, 0, cancellation()).is_ok()));
    outcomes.push(("replayed", send(&mut to_data, 1, prefill()).is_ok()));
    outcomes.push((
        "cancel_again",
        send(&mut to_control, 1, cancellation()).is_ok(),
    ));
    data.close(0u32.into(), b"test_done");
    control.close(0u32.into(), b"test_done");
    let log = entry.log.clone();
    let report = entry.thread.join().unwrap().unwrap();
    host.close(&clock, until(B::RouterClose)).unwrap();
    assert!(outcomes.iter().all(|o| o.1), "{outcomes:?}; {report}");
    assert_eq!(report["requests_served"], 1, "{report}");
    assert_eq!(report["failure"], "route_request_settled", "{report}");
    assert_eq!(
        log.lock().unwrap().events,
        [("step", 0), ("reset", 0), ("close", 0)],
        "the replayed PREFILL never reached the compute"
    );
}

/// EP-PLAN RED 7 (lease evidence): a member whose seed lease ran out while the
/// epoch is idle ends the EPOCH: it refuses with the code, its carrier closes,
/// and the surviving member's compute is closed without another request.
#[test]
fn a_lease_expired_member_ends_the_epoch_not_only_the_request() {
    let r = epoch_route([Script::default(), scripted(&[5, 9, 3, 7])]);
    assert_eq!(r.chat(4).0, "completed");
    let view = SeedView::fresh(&["node-a", "node-b"]);
    view.set("node-b", 1, SEED_NOW - lease());
    assert_eq!(
        m16_route::observe_member_leases(&r.handle, &r.message, &view.members()).unwrap(),
        vec![1]
    );
    let refused = r.service.qualification();
    assert_eq!(
        refused.as_ref().err().map(|e| e.code),
        Some("joined_route_member_stale")
    );
    assert_eq!(
        r.submit(4).err().map(|e| e.code),
        Some("joined_route_member_stale")
    );
    assert!(
        r.until_closed(),
        "the epoch's carrier closed: {}",
        r.state()
    );
    let logs: Vec<_> = r.members.iter().map(|m| m.log.clone()).collect();
    let reports = r.finish();
    assert_eq!(reports[0]["teardown"], "owner_closed", "{}", reports[0]);
    let events = logs[0].lock().unwrap().events.clone();
    assert_eq!(events[4..], [("reset", 0), ("close", 0)], "{events:?}");
}

/// EP-PLAN RED 8 (revoke): the seed advanced node-b's generation (a revoke)
/// after chat 1. Chat 2 is refused at the carrier before any frame leaves the
/// owner, with `joined_route_generation_changed`; the epoch retires and closes.
#[test]
fn a_revoked_member_ends_the_epoch() {
    let r = epoch_route([Script::default(), scripted(&[5, 9, 3, 7])]);
    assert_eq!(r.chat(4).0, "completed");
    r.generations.lock().unwrap()[1] = 2;
    let submitted = r.submit(4);
    assert!(
        submitted.is_ok(),
        "{:?}",
        submitted.as_ref().err().map(|e| e.code)
    );
    let (id, _) = submitted.unwrap();
    r.pump_until(span(B::WorkerStartup), |r| r.terminal(&id).is_some());
    assert_eq!(r.terminal(&id).as_deref(), Some("failed"));
    assert_eq!(
        r.handle.state.lock().unwrap().failure,
        Some("joined_route_generation_changed")
    );
    for stage in 0..2 {
        assert_eq!(kinds(&r.events(stage), "step"), 4, "stage {stage}");
    }
    assert_eq!(
        r.service.qualification().err().map(|e| e.code),
        Some("joined_route_generation_changed")
    );
    assert!(r.until_closed());
    let reports = r.finish();
    assert!(
        reports
            .iter()
            .all(|v| v["teardown"] == "owner_closed" && v["requests_served"] == 1),
        "{reports:?}"
    );
}

/// EP-PLAN RED 9 (ruling U4): the member check every build before EP1 runs
/// (`route::verify_member`, unchanged) refuses the v2 marker, so a mixed
/// owner/member pair fails closed; a member of this build serves the same
/// route as a multi-request epoch.
#[test]
fn an_old_member_refuses_a_v2_route() {
    let seed = Identity::from_secret("test-seed", &[7; 32]).unwrap();
    let rows: Vec<Value> = ["node-a", "node-b"]
        .iter()
        .map(|n| {
            json!({"node_id":n,"generation":1,"swarm_id":"swarm",
            "seed_key_digest":seed.verification_key_digest()})
        })
        .collect();
    let offers: Vec<Value> = (0..2).map(|i| json!({"controlled_offer":i})).collect();
    let owner = iroh::SecretKey::generate();
    let endpoints = [
        iroh::SecretKey::generate().public(),
        iroh::SecretKey::generate().public(),
    ];
    let (_, v1) = signed_route(&seed, "swarm", &rows, &offers, &endpoints, &owner);
    let (_, v2) = signed_route_as(
        route::ROUTE_V2,
        span(B::MemberChosenWall),
        &seed,
        "swarm",
        &rows,
        &offers,
        &endpoints,
        &owner,
    );
    let old = |envelope: &Value| {
        route::verify_member(envelope, &rows[0], &offers[0], &proof(0), app_host::now())
            .map(|(i, _)| i)
            .map_err(|e| e.0)
    };
    assert_eq!(old(&v1), Ok(0));
    assert_eq!(old(&v2), Err("joined_shard_signature_invalid"));
    let r = epoch_route([Script::default(), scripted(&[5, 9, 3, 7])]);
    assert_eq!(r.chat(4).0, "completed");
    let reports = r.finish();
    assert!(
        reports.iter().all(|v| v["multi_request"] == true),
        "{reports:?}"
    );
}

/// Invariant 5: the signed `expires_at` bounds the epoch on both sides. Each
/// member ends its serve at the expiry by itself (compute closed); the owner
/// refuses a request past it before any frame is sent.
#[test]
fn a_member_ends_its_epoch_at_the_signed_route_expiry() {
    let mut r = epoch_route_for(
        [Script::default(), scripted(&[5, 9, 3, 7])],
        span(B::WorkerStartup),
    );
    assert_eq!(r.chat(4).0, "completed");
    let members = std::mem::take(&mut r.members);
    // Bounded (REVIEW-laneEP F6, M4 hung to the 600 s member deadline): each
    // member must have ended by itself by the expiry plus one loop pass.
    let left = r.message["expires_at"].as_f64().unwrap() - app_host::now();
    let end = Instant::now() + Duration::from_secs_f64(left.max(0.0)) + span(B::RouterClose);
    for m in members {
        while !m.thread.is_finished() && Instant::now() < end {
            std::thread::sleep(span(B::OwnerPoll) / 4);
        }
        assert!(
            m.thread.is_finished(),
            "a member still serves {:?} past the signed expiry",
            span(B::RouterClose)
        );
        let report = m.thread.join().unwrap().unwrap();
        assert_eq!(report["teardown"], "route_expired", "{report}");
        assert_eq!(report["native"]["graceful"], true, "{report}");
    }
    let submitted = r.submit(4);
    if let Ok((id, _)) = submitted {
        r.pump_until(span(B::WorkerStartup), |r| r.terminal(&id).is_some());
        assert_eq!(r.terminal(&id).as_deref(), Some("failed"));
        assert_eq!(
            r.handle.state.lock().unwrap().failure,
            Some("joined_route_expired")
        );
    }
    r.finish();
}

// ---- EPF (REVIEW-laneEP R1): a member's v2 serve ends at its own bounds
// whatever the owner sends. v0.1.4 ends the member's worker at its consented
// wall by a timer and checks its membership every 0.25 s, independent of the
// traffic: DemoApp (app.py:505) -> /api/work/accept (app.py:1478-1500) ->
// work_acceptance.accept_work (:54-66) -> TargetApplication.start_work
// (target_app.py:172) -> _start_offer (:189-227) -> prepare_runtime
// (:240-263) -> ManagedTargetRuntime (managed_runtime.py:117) -> _make_worker
// (:196) -> ManagedWorker (:227) -> start: _arm_deadline (worker.py:1063,
// :985-1000) -> _expire (:1453-1457); _watch (:1066, :1459) -> _check
// (:800-818: membership current, wall).

/// A raw owner's operations on ONE member's real v2 stage server.
struct Raw<'a> {
    /// A request-scoped PathCancellation of this request (control link): ACKed?
    cancel: Box<dyn FnMut(&str) -> bool + 'a>,
    /// The PREFILL of this request (data link): ACKed (queued for compute)?
    prefill: Box<dyn FnMut(&str) -> bool + 'a>,
    /// EPF2: a PathCancellation of this request sent on the DATA link (the
    /// member's data receiver accepts one too): ACKed?
    cancel_on_data: Box<dyn FnMut(&str) -> bool + 'a>,
    /// Take the member's forwarded stage output and ACK it.
    forwarded: Box<dyn FnMut() -> bool + 'a>,
}
fn raw_path(request_id: &str) -> route::Path {
    route::Path {
        request_id: request_id.into(),
        path_id: format!("path-{request_id}"),
        path_attempt: 0,
    }
}
fn raw_send(
    t: &mut impl transport::Transport,
    clock: &HostClock,
    peer: transport::ConnectionFence,
    id: u128,
    payload: Vec<u8>,
) -> bool {
    let deadline = clock
        .monotonic()
        .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
        .unwrap();
    transport::exchange(
        t,
        &transport::Exchange {
            peer,
            channel: transport::Channel::Router,
            message_id: id.to_be_bytes(),
            payload,
            deadline,
            maximum_response_bytes: protocol::remote::REMOTE_MAX_FRAME_BYTES,
        },
        clock,
    )
    .is_ok()
}
/// Stage 0 of a v2 route living `lifetime` from now, served by the REAL member
/// stage server (controlled compute; its member row stops being current once
/// `revoked` is set); a raw owner (test-held endpoint and its two connections)
/// runs `body`, then closes. Returns the member's report and compute log.
fn raw_owner(
    lifetime: Duration,
    script: Script,
    revoked: Option<Arc<AtomicBool>>,
    body: impl FnOnce(&Value, &Member, &mut Raw<'_>),
) -> (Value, Vec<(&'static str, u64)>) {
    let seed = Identity::from_secret("test-seed", &[7; 32]).unwrap();
    let rows: Vec<Value> = ["node-a", "node-b"]
        .iter()
        .map(|n| {
            json!({"node_id":n,"generation":1,"swarm_id":"swarm",
            "seed_key_digest":seed.verification_key_digest()})
        })
        .collect();
    let offers: Vec<Value> = (0..2).map(|i| json!({"controlled_offer":i})).collect();
    let row = rows[0].clone();
    let live: Live = Arc::new(move || match &revoked {
        Some(r) if r.load(Ordering::SeqCst) => Err(Error("joined_shard_member_changed")),
        _ => Ok(row.clone()),
    });
    let entry = member_at(
        Bind::Loopback,
        live,
        offers[0].clone(),
        false,
        script,
        proof(0),
    );
    let owner = iroh::SecretKey::generate();
    let other = iroh::SecretKey::generate().public();
    let (message, envelope) = signed_route_as(
        route::ROUTE_V2,
        lifetime,
        &seed,
        "swarm",
        &rows,
        &offers,
        &[entry.endpoint, other],
        &owner,
    );
    entry.route.send(envelope).unwrap();
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = HostClock::default();
    let until = |id: B| {
        clock
            .monotonic()
            .checked_add(id.span(Entries::ZERO).unwrap())
            .unwrap()
    };
    let mut host =
        LoopbackHost::bind_with_secret(&rt, &clock, until(B::WorkerStartup), owner).unwrap();
    let addr = iroh::EndpointAddr::new(entry.endpoint).with_ip_addr(entry.socket);
    let channel = transport::Channel::Router;
    let data = host
        .connect_channel(&addr, channel, &clock, until(B::WorkerStartup))
        .unwrap();
    let control = host
        .connect_channel(&addr, channel, &clock, until(B::WorkerStartup))
        .unwrap();
    let fence = transport::ConnectionFence {
        endpoint_id: *entry.endpoint.as_bytes(),
        local_generation: 7,
        remote_generation: 1,
    };
    let current = move || fence;
    let to_data = std::cell::RefCell::new(
        IrohTransport::new(data.clone(), &rt, &clock, fence, current).unwrap(),
    );
    let mut from_data = RouterReceiver::new(data.clone(), &rt, &clock, fence, current).unwrap();
    let mut to_control = IrohTransport::new(control.clone(), &rt, &clock, fence, current).unwrap();
    let (data_ids, mut control_ids) = (std::cell::Cell::new(0u128), 0u128);
    let data_send = |payload: Vec<u8>| {
        data_ids.set(data_ids.get() + 1);
        raw_send(
            &mut *to_data.borrow_mut(),
            &clock,
            fence,
            data_ids.get() - 1,
            payload,
        )
    };
    {
        let (message, clock) = (&message, &clock);
        let mut raw = Raw {
            cancel: Box::new(|id| {
                let frame = protocol::router_wire::encode_frame(
                    "PathCancellation",
                    &route::cancellation(message, &raw_path(id)),
                    &[],
                )
                .unwrap();
                control_ids += 1;
                raw_send(&mut to_control, clock, fence, control_ids - 1, frame)
            }),
            prefill: Box::new(|id| {
                let frame = protocol::router_wire::encode_frame(
                    "HopHeader",
                    &route::hop(message, &raw_path(id), 0, 0).unwrap(),
                    &route::entry_payload(&(0..35).collect::<Vec<u32>>()).unwrap(),
                )
                .unwrap();
                data_send(frame)
            }),
            cancel_on_data: Box::new(|id| {
                data_send(
                    protocol::router_wire::encode_frame(
                        "PathCancellation",
                        &route::cancellation(message, &raw_path(id)),
                        &[],
                    )
                    .unwrap(),
                )
            }),
            forwarded: Box::new(|| {
                from_data
                    .receive(until(B::WorkerStartup))
                    .and_then(|d| d.acknowledge())
                    .is_ok()
            }),
        };
        body(message, &entry, &mut raw);
    }
    data.close(0u32.into(), b"test_done");
    control.close(0u32.into(), b"test_done");
    let log = entry.log.clone();
    let report = entry.thread.join().unwrap().unwrap();
    host.close(&clock, until(B::RouterClose)).unwrap();
    let events = log.lock().unwrap().events.clone();
    (report, events)
}
/// When `expires_at` (wall seconds) falls on the monotonic clock.
fn at_wall(expires_at: f64) -> Instant {
    Instant::now() + Duration::from_secs_f64((expires_at - app_host::now()).max(0.0))
}
/// The review's chatter, paced: a PathCancellation of a request that never ran
/// every quarter OwnerPoll (gaps below the member's idle poll), until one is
/// refused or `stop` passes. Returns the ACK times and the first refusal.
fn chatter(raw: &mut Raw<'_>, stop: Instant) -> (Vec<Instant>, Option<Instant>) {
    let mut acked = Vec::new();
    let mut n = 0u64;
    while Instant::now() < stop {
        let ok = (raw.cancel)(&format!("never-ran-{n}"));
        n += 1;
        if !ok {
            return (acked, Some(Instant::now()));
        }
        acked.push(Instant::now());
        std::thread::sleep(span(B::OwnerPoll) / 4);
    }
    (acked, None)
}
/// Assert the chatter stopped being ACKed at `bound` (at most one ACK whose
/// check ran just before it) and was refused within one more loop pass.
fn assert_ended_at(what: &str, bound: Instant, acked: &[Instant], refused: Option<Instant>) {
    let late = acked.iter().filter(|t| **t > bound).count();
    assert!(
        late <= 1 && refused.is_some(),
        "the member kept ACKing idle cancellations past {what}: {late} late of {} ACKs, \
         refused {:?} after it",
        acked.len(),
        refused.map(|r| r.saturating_duration_since(bound))
    );
}

/// REVIEW-laneEP R1, the reviewer's probe made a bounded test: an owner
/// chattering idle PathCancellations (requests that never ran) cannot keep the
/// member's serve and compute past the route's SIGNED expiry. The member ends
/// by the expiry plus one loop pass, compute closed, and counts no request.
#[test]
fn idle_cancellations_cannot_keep_a_member_past_the_signed_expiry() {
    let (report, events) = raw_owner(
        span(B::RouterClose),
        Script::default(),
        None,
        |message, _, raw| {
            let expires = at_wall(message["expires_at"].as_f64().unwrap());
            let (acked, refused) = chatter(raw, expires + span(B::RouterClose));
            assert_ended_at("the signed expiry", expires, &acked, refused);
        },
    );
    assert_eq!(report["teardown"], "route_expired", "{report}");
    assert_eq!(report["native"]["graceful"], true, "{report}");
    assert_eq!(report["requests_served"], 0, "{report}");
    assert_eq!(events, [("close", 0)]);
}

/// REVIEW-laneEP R1: the member's OWN consent wall, earlier than the signed
/// expiry, ends its serve the same way under the same chatter.
#[test]
fn idle_cancellations_cannot_keep_a_member_past_its_own_consent_wall() {
    let wall = Instant::now() + span(B::RouterClose);
    let script = Script {
        consent_wall: Some(wall),
        ..Script::default()
    };
    let (report, events) = raw_owner(span(B::MemberChosenWall), script, None, |_, _, raw| {
        let (acked, refused) = chatter(raw, wall + span(B::RouterClose));
        assert_ended_at("its consent wall", wall, &acked, refused);
    });
    assert_eq!(report["teardown"], "member_consent_ended", "{report}");
    assert_eq!(
        report["failure"], "joined_native_consent_exhausted",
        "{report}"
    );
    assert_eq!(report["native"]["graceful"], true, "{report}");
    assert_eq!(report["requests_served"], 0, "{report}");
    assert_eq!(events, [("close", 0)]);
}

/// REVIEW-laneEP R1: an IDLE member (no traffic at all) whose membership stops
/// being current, or whose standing consent changes, ends within one loop pass
/// with its compute closed; it does not wait for the next request.
#[test]
fn an_idle_member_ends_when_its_membership_or_consent_changes() {
    for (case, code) in [
        ("membership", "joined_shard_member_changed"),
        ("consent", "joined_native_consent_changed"),
    ] {
        let flag = Arc::new(AtomicBool::new(false));
        let script = Script {
            consent_changed: (case == "consent").then(|| flag.clone()),
            ..Script::default()
        };
        let revoked = (case == "membership").then(|| flag.clone());
        let (report, events) = raw_owner(
            span(B::MemberChosenWall),
            script,
            revoked,
            |_, entry, raw| {
                assert!((raw.cancel)("never-ran-0"), "{case}: the epoch serves");
                flag.store(true, Ordering::SeqCst);
                let end = Instant::now() + span(B::RouterClose);
                while !entry.thread.is_finished() && Instant::now() < end {
                    std::thread::sleep(span(B::OwnerPoll) / 4);
                }
                assert!(
                    entry.thread.is_finished(),
                    "{case}: an idle member still serves {:?} after its {case} changed",
                    span(B::RouterClose)
                );
            },
        );
        assert_eq!(
            report["teardown"], "member_consent_ended",
            "{case}: {report}"
        );
        assert_eq!(report["failure"], code, "{case}: {report}");
        assert_eq!(report["native"]["graceful"], true, "{case}: {report}");
        assert_eq!(events, [("close", 0)], "{case}");
    }
}

/// REVIEW-laneEP R1: the served set and `requests_served` hold only requests
/// the member admitted. An idle cancellation (a request that never ran here)
/// is remembered as a tombstone and counted against the epoch's lifetime
/// bound (`JoinedEpochMaxSessions`); past it, it is refused and the epoch ends
/// with the compute closed (as the shipped relay refuses a pending
/// cancellation once its table is full, relay.py:718-743).
#[test]
fn idle_cancellations_are_bounded_and_never_counted_as_served_requests() {
    let capacity = usize::try_from(B::JoinedEpochMaxSessions.count().unwrap().get()).unwrap();
    let (report, events) = raw_owner(
        span(B::MemberChosenWall),
        Script::default(),
        None,
        |_, _, raw| {
            assert!((raw.prefill)("route-7-0"), "PREFILL queued");
            assert!((raw.forwarded)(), "stage output forwarded");
            assert!((raw.cancel)("route-7-0"), "the admitted request is reset");
            let mut idle = 0;
            while idle <= capacity && (raw.cancel)(&format!("never-ran-{idle}")) {
                idle += 1;
            }
            assert_eq!(
                idle,
                capacity - 1,
                "idle cancellations ACKed; the epoch's bound is {capacity} requests"
            );
        },
    );
    assert_eq!(report["requests_served"], 1, "{report}");
    assert_eq!(report["failure"], "route_request_capacity", "{report}");
    assert_eq!(events, [("step", 0), ("reset", 0), ("close", 0)]);
}

/// A request id the member saw only as an idle cancellation is settled there
/// (the shipped relay's pending tombstone, relay.py:718-743, consumed by a
/// later hop of that path, :1051): its PREFILL is refused, never reaches the
/// compute, and is not a served request.
#[test]
fn a_prefill_of_an_idle_cancelled_request_is_refused() {
    let (report, events) = raw_owner(
        span(B::MemberChosenWall),
        Script::default(),
        None,
        |_, _, raw| {
            assert!((raw.cancel)("route-7-0"), "idle cancellation ACKed");
            assert!((raw.prefill)("route-7-0"), "PREFILL queued");
            assert!((raw.cancel)("route-7-0"), "the released compute is ACKed");
        },
    );
    assert_eq!(report["failure"], "route_request_settled", "{report}");
    assert_eq!(report["requests_served"], 0, "{report}");
    assert_eq!(events, [("close", 0)]);
}

/// REVIEW-laneEP F6 (mutant M5b survived): a revoke seen by the seed lease
/// pump while the epoch is IDLE (a fresh lease of another generation: no death
/// evidence) retires the epoch at once with `joined_route_generation_changed`
/// and closes its carrier, without waiting for the next Start; both members
/// end their serves `owner_closed` with their compute closed after one reset.
#[test]
fn a_revoke_seen_while_idle_retires_the_epoch_without_a_next_request() {
    let r = epoch_route([Script::default(), scripted(&[5, 9, 3, 7])]);
    assert_eq!(r.chat(4).0, "completed");
    let view = SeedView::fresh(&["node-a", "node-b"]);
    view.set("node-b", 2, SEED_NOW + lease());
    assert_eq!(
        m16_route::observe_member_leases(&r.handle, &r.message, &view.members()).unwrap(),
        Vec::<usize>::new(),
        "a fresh lease is no death evidence"
    );
    assert_eq!(
        r.service.qualification().err().map(|e| e.code),
        Some("joined_route_generation_changed"),
        "the idle epoch retired on the generation change: {}",
        r.state()
    );
    assert!(
        r.until_closed(),
        "the epoch's carrier closed: {}",
        r.state()
    );
    let logs: Vec<_> = r.members.iter().map(|m| m.log.clone()).collect();
    let reports = r.finish();
    for (report, log) in reports.iter().zip(logs) {
        assert_eq!(report["teardown"], "owner_closed", "{report}");
        assert_eq!(report["requests_served"], 1, "{report}");
        let events = log.lock().unwrap().events.clone();
        assert_eq!(events[4..], [("reset", 0), ("close", 0)], "{events:?}");
    }
}

// ---- EPF2 (REVIEW-laneEPF N1): a member whose epoch ends while a
// PathCancellation is still QUEUED for its main loop (its receiver thread
// waits for the verdict) still returns from its serve. Both links accept a
// PathCancellation, so up to two can be queued. Each owner below runs on its
// own thread, so a serve that never returns fails an assertion at its bound
// instead of hanging the test.

/// Runs `owner` (a raw owner over the real member) on its own thread; returns
/// its verdict (sent from inside the owner's body) and then its result.
type RawOutcome = (Value, Vec<(&'static str, u64)>);
fn bounded_owner<V: Send + 'static>(
    owner: impl FnOnce(mpsc::Sender<V>) -> RawOutcome + Send + 'static,
) -> (V, impl FnOnce() -> RawOutcome) {
    let (verdict_tx, verdict) = mpsc::channel();
    let (out_tx, out) = mpsc::channel();
    let thread = std::thread::spawn(move || {
        if let Err(e) = out_tx.send(owner(verdict_tx)) {
            eprintln!("raw owner result: {e}");
        }
    });
    let v = verdict
        .recv_timeout(span(B::WorkerStartup) * 2)
        .expect("the raw owner reached its verdict");
    (v, move || {
        let result = out
            .recv_timeout(span(B::WorkerStartup))
            .expect("the raw owner closed");
        thread.join().unwrap();
        result
    })
}
/// Waits at most `bound` for the member's serve thread to finish.
fn returned_within(entry: &Member, bound: Duration) -> (bool, Duration) {
    let began = Instant::now();
    while !entry.thread.is_finished() && began.elapsed() < bound {
        std::thread::sleep(span(B::OwnerPoll) / 4);
    }
    (entry.thread.is_finished(), began.elapsed())
}

/// ONE cancellation queued at the ending pass: the member's own consent wall
/// ends its epoch and the pass is held inside its graceful close; a
/// PathCancellation reaches the control receiver there and waits (until the
/// owner's own exchange deadline); then the close is released. The serve must
/// return within `RouterClose` of the release.
#[test]
fn a_member_returns_when_its_epoch_ends_with_one_cancellation_queued() {
    let (entered_tx, entered) = mpsc::channel();
    let (open, gate) = mpsc::channel();
    let script = Script {
        close_gate: Some(gate),
        close_entered: Some(entered_tx),
        consent_wall: Some(Instant::now() + span(B::OwnerPoll)),
        ..Script::default()
    };
    let ((acked, (returned, after)), finish) = bounded_owner(move |verdict| {
        raw_owner(span(B::MemberChosenWall), script, None, |_, entry, raw| {
            entered
                .recv_timeout(span(B::WorkerStartup))
                .expect("the member's ending pass entered its graceful close");
            let acked = (raw.cancel)("queued-0");
            open.send(()).unwrap();
            verdict
                .send((acked, returned_within(entry, span(B::RouterClose))))
                .unwrap();
        })
    });
    assert!(
        returned,
        "N1: the member's serve has not returned {after:?} after its epoch ended with a \
         PathCancellation still queued for its main loop"
    );
    assert!(
        !acked,
        "a cancellation queued at the ending pass is never ACKed"
    );
    let (report, events) = finish();
    assert_eq!(report["teardown"], "member_consent_ended", "{report}");
    assert_eq!(report["native"]["graceful"], true, "{report}");
    assert_eq!(events, [("close", 0)]);
}

/// TWO cancellations queued (one per link) while a PREFILL step runs; the
/// member's consent then changes and the step ends: the next pass takes ONE
/// cancellation and ends the epoch at its check, the other is still queued.
/// The request it admitted is still open, so after its epoch ends the member
/// waits its bounded re-dial window (`WorkerStartup`, serve.rs `linger`) for
/// the owner, which does not re-dial here: the serve must return within
/// `RouterClose` plus that window of the step's release.
#[test]
fn a_member_returns_when_its_epoch_ends_with_two_cancellations_queued() {
    let (open, gate) = mpsc::channel();
    let changed = Arc::new(AtomicBool::new(false));
    let script = Script {
        tokens: vec![5],
        step_gate: Some((0, gate)),
        consent_changed: Some(changed.clone()),
        ..Script::default()
    };
    let ((acked, (returned, after)), finish) = bounded_owner(move |verdict| {
        raw_owner(span(B::MemberChosenWall), script, None, |_, entry, raw| {
            assert!((raw.prefill)("route-7-0"), "PREFILL queued");
            let began = Instant::now();
            while !entry.log.lock().unwrap().events.contains(&("step", 0))
                && began.elapsed() < span(B::WorkerStartup)
            {
                std::thread::sleep(span(B::OwnerPoll) / 4);
            }
            let acked = [(raw.cancel)("route-7-0"), (raw.cancel_on_data)("route-7-0")];
            changed.store(true, Ordering::SeqCst);
            open.send(()).unwrap();
            verdict
                .send((
                    acked,
                    returned_within(entry, span(B::RouterClose) + span(B::WorkerStartup)),
                ))
                .unwrap();
        })
    });
    assert!(
        returned,
        "N1: the member's serve has not returned {after:?} after its epoch ended with a \
         PathCancellation still queued on each link"
    );
    assert_eq!(
        acked,
        [false, false],
        "queued cancellations are never ACKed"
    );
    let (report, events) = finish();
    assert_eq!(report["teardown"], "member_consent_ended", "{report}");
    assert_eq!(
        report["failure"], "joined_native_consent_changed",
        "{report}"
    );
    assert_eq!(report["deferred_ack"], true, "{report}");
    assert_eq!(report["redial_ack_ms"], Value::Null, "{report}");
    assert_eq!(events, [("step", 0), ("close", 0)]);
}

// ---- EPF2 F11 (REVIEW-laneEPF Q2(b)): the member keeps settled request ids
// for the epoch's life, so their LENGTH must be bounded, not only their count.
// The owner's ids follow the shipped gateway's grammar
// `[A-Za-z0-9][A-Za-z0-9._~-]{0,127}` (v0.1.4 mycelium_request_gateway
// contracts.py:27, admission service.py:468; v0.2 registry::valid_request_id,
// checked at m16_requests.rs:496 before any dispatch): at most 128 bytes.

/// An idle PathCancellation whose id is longer than the owner's 128-byte
/// bound is refused before it is kept or ACKed: compute closed, epoch ended.
#[test]
fn an_idle_cancellation_of_an_overlong_request_id_is_refused() {
    let (report, events) = raw_owner(
        span(B::MemberChosenWall),
        Script::default(),
        None,
        |_, _, raw| {
            assert!(
                (raw.cancel)(&"a".repeat(128)),
                "an id at the owner's bound is ACKed"
            );
            assert!(
                !(raw.cancel)(&"a".repeat(129)),
                "an idle cancellation of a 129-byte request id is refused"
            );
        },
    );
    assert_eq!(report["failure"], "route_cancellation_invalid", "{report}");
    assert_eq!(report["requests_served"], 0, "{report}");
    assert_eq!(report["native"]["graceful"], true, "{report}");
    assert_eq!(events, [("close", 0)]);
}

/// A PREFILL whose id is longer than the owner's bound is refused (never
/// ACKed, never stepped): the compute is closed and the owner's teardown is
/// then ACKed against the closed compute, ending the epoch.
#[test]
fn a_prefill_of_an_overlong_request_id_is_refused_before_it_runs() {
    let (report, events) = raw_owner(
        span(B::MemberChosenWall),
        Script::default(),
        None,
        |_, _, raw| {
            assert!(
                !(raw.prefill)(&"a".repeat(129)),
                "a PREFILL of a 129-byte request id is refused"
            );
            assert!((raw.cancel)("route-7-0"), "the released compute is ACKed");
        },
    );
    assert_eq!(report["failure"], "route_request_invalid", "{report}");
    assert_eq!(report["requests_served"], 0, "{report}");
    assert_eq!(events, [("close", 0)]);
}

// ---- EPF2 F10 (REVIEW-laneEPF R1 residue): one loop pass of a v2 member
// (a PREFILL step plus its forwarding exchange) ends by the route's SIGNED
// expiry, not only by the hop budget (WorkerStartup for a PREFILL).

/// The owner admits a PREFILL just before the signed expiry and never takes
/// the forwarded stage output: the member's forwarding exchange must give up
/// at the expiry and close the compute, not hold it for the hop budget.
#[test]
fn a_forwarding_exchange_ends_at_the_signed_expiry() {
    let (report, events) = raw_owner(
        span(B::RouterClose),
        Script::default(),
        None,
        |message, entry, raw| {
            let expires = at_wall(message["expires_at"].as_f64().unwrap());
            assert!((raw.prefill)("route-7-0"), "PREFILL queued");
            let bound = expires + span(B::RouterClose);
            while entry.released().is_none() && Instant::now() < bound {
                std::thread::sleep(span(B::OwnerPoll) / 4);
            }
            let released = entry.released();
            assert!(
                released.is_some_and(|(at, _)| at <= bound),
                "F10: the member still held its compute {:?} past the signed expiry \
                 (released {:?} after it)",
                span(B::RouterClose),
                released.map(|(at, _)| at.saturating_duration_since(expires))
            );
        },
    );
    assert_eq!(report["teardown"], "route_expired", "{report}");
    assert_eq!(report["native"]["graceful"], true, "{report}");
    assert_eq!(report["deferred_ack"], true, "{report}");
    assert_eq!(events, [("step", 0), ("close", 0)]);
}
