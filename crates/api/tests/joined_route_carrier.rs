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
    Lan(iroh::SecretKey, std::net::IpAddr, LanPolicy),
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
            Bind::Lan(secret, interface, policy) => LoopbackHost::bind_lan(
                &rt,
                &clock,
                until(B::RouterClose),
                secret,
                &[transport::Channel::Router],
                interface,
                policy,
            )
            .unwrap(),
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
    let stage = |i: usize, start: u64, end: u64| {
        json!({"stage_index":i,"placement_id":format!("{}:{start}-{end}",rows[i]["node_id"].as_str().unwrap()),
        "member":rows[i],"carrier_endpoint_id":members[i].endpoint.to_string(),"run":format!("run-{i}"),
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
            self.service.tick().unwrap();
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
    open.send(()).unwrap();
    r.pump_until(span(B::WorkerStartup), |r| r.terminal().is_some());
    assert_eq!(r.terminal().as_deref(), Some("cancelled"), "{}", r.state());
    let attempts = r.handle.state.lock().unwrap().teardown_attempts;
    assert!(attempts >= 2, "settled by a re-dial: {attempts}");
    let reports = r.finish();
    assert_eq!(reports[1]["ack_delivered"], false);
    assert!(reports[1]["redial_ack_ms"].is_number(), "{:?}", reports[1]);
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
        let mut out = Vec::new();
        let mut verified = Vec::new();
        for i in 0..2 {
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
                Bind::Lan(m.secret.clone(), interface, policy.clone()),
                live,
                m.offer.clone(),
                i == 1,
                if i == 1 {
                    scripted(tokens)
                } else {
                    Script::default()
                },
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
