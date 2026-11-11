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
}
struct Controlled {
    offer: Value,
    last: bool,
    owned: bool,
    script: Script,
    log: Arc<Mutex<Log>>,
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
        Ok(logits(
            self.script.tokens[t as usize % self.script.tokens.len()],
        ))
    }
    fn close(&mut self) -> Result<Value, Error> {
        if let Some(gate) = self.script.close_gate.take() {
            gate.recv_timeout(span(B::WorkerStartup)).unwrap();
        }
        self.owned = false;
        self.log.lock().unwrap().released = Some((Instant::now(), "close"));
        Ok(json!({"context_closed":true}))
    }
    fn abort(&mut self) -> Result<Value, Error> {
        self.owned = false;
        self.log.lock().unwrap().released = Some((Instant::now(), "abort"));
        Ok(json!({"context_closed":false,"worker_reaped":true}))
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
        self.handle
            .refusal(self.prepared.fetch_add(1, Ordering::SeqCst) > 0)?;
        let latency =
            |id: B| PyReal::from_json(&json!(id.span(Entries::ZERO).unwrap().millis())).unwrap();
        Ok(Prepared {
            context: RequestContext {
                request_id: "route-7-0".into(),
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
    let stage = |i: usize, start: u64, end: u64| {
        json!({"stage_index":i,"placement_id":format!("{}:{start}-{end}",rows[i]["node_id"].as_str().unwrap()),
        "member":rows[i],"carrier_endpoint_id":endpoints[i].to_string(),"run":format!("run-{i}"),
        "offer_digest":digest(&offers[i]),"proof_digest":proof(i),"range":[start,end],
        "worker_pid":100+i,"tokens":99,"memory":4_000_000_000u64})
    };
    let now = app_host::now();
    let message = json!({"protocol":route::ROUTE,"swarm_id":swarm,
        "deployment_id":"12345678-1234-5678-9234-abcdefabcdef","deployment_epoch":7,"topology_version":7,
        "model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca",
        "manifest_digest":digest(&json!("manifest")),"owner_endpoint_id":owner.public().to_string(),
        "stages":[stage(0,0,14),stage(1,14,28)],"issued_at":now,
        "expires_at":now + span(B::MemberChosenWall).as_secs_f64(),"route_ready":false,"scope":route::SCOPE});
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
    swarm_clocked(Box::new(app_host::now))
}
/// [`swarm`] whose seed coordinator reads `clock`.
fn swarm_clocked(clock: seed::Clock) -> Swarm {
    let root = std::env::temp_dir().join(format!(
        "b8-lan-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    std::fs::create_dir(&root).unwrap();
    let root = std::fs::canonicalize(root).unwrap();
    let owner = root.join("owner");
    std::fs::create_dir(&owner).unwrap();
    let seed =
        app_host::start_seed_clocked::<app_host::Unmanaged>(&owner, "b8-lan-swarm", None, clock)
            .unwrap();
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
        let (signer, _, _) =
            app_host::identity_in(&self.root.join("owner"), app_host::SEED_IDENTITY_FILE).unwrap();
        let rows = [self.live(0), self.live(1)];
        let offers = [self.members[0].offer.clone(), self.members[1].offer.clone()];
        signed_route(
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

/// The PRODUCTION seed view (`Epoch::open` -> `epoch::seed_members` over the real coordinator,
/// SL review F1 / mutant M8): a swarm seed whose own clock the test moves (real time plus an
/// offset, as v0.1.4's `set_test_clock_offset`), two members whose last renewal set their seed
/// lease; one goes silent mid-answer; once the seed's clock passes its lease plus the members'
/// clock tolerance, the epoch's own pump settles it from the seed row and the chat stream ends
/// `failed` `joined_route_member_stale` before the hop deadline could.
#[test]
fn product_epoch_reads_member_leases_from_the_real_seed() {
    let offset = Arc::new(AtomicU64::new(0f64.to_bits()));
    let seed_offset = offset.clone();
    let s = swarm_clocked(Box::new(move || {
        app_host::now() + f64::from_bits(seed_offset.load(Ordering::SeqCst))
    }));
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
    let (_, rx, reader) = chat(&epoch, 32);
    let mut all = until_terminal(&rx, 2);
    // Both leases still run: no evidence from the real seed rows.
    assert!(
        epoch
            .handle()
            .state
            .lock()
            .unwrap()
            .stages
            .iter()
            .all(|stage| stage.death_evidence.is_none()),
        "evidence while both leases run"
    );
    // The seed's clock passes a full lease plus the members' clock tolerance: no renewal can land.
    offset.store((lease() + clock_skew() + 1.0).to_bits(), Ordering::SeqCst);
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
    let evidence = o.stages[1].death_evidence.clone().unwrap();
    drop(o);
    assert_eq!(evidence["source"], "seed_member_lease", "{evidence}");
    assert_eq!(evidence["node_id"], nodes[1].as_str(), "{evidence}");
    assert_eq!(evidence["generation"], 1, "{evidence}");
    epoch.close();
    open.send(()).unwrap();
    for m in members {
        m.thread.join().unwrap().unwrap();
    }
}
