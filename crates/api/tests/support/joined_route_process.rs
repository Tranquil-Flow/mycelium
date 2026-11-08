// SPDX-License-Identifier: AGPL-3.0-only
//! Real product request path over the real Router carrier: HTTP POST -> M16
//! admission -> joined-route carrier executor -> two member-owned native workers
//! (two OS processes) -> TokenEvent -> registry -> SSE. Tokens are compared with
//! the pinned Python/MLX packed oracle (never with Rust output). Owner remains a
//! test driver for pairing, shard emission, worker (re)prepare, address discovery,
//! death evidence and withdrawal delivery; route_ready stays false.
use super::native_process::{self, Fixture, admit_native_load, allowance};
use super::sse_client::{http, stream};
use super::*;
use api::{
    joined_shard::{native, route},
    m16_requests::{Host as _, M16Requests},
    m16_route::{self, Codec, Currentness, RouteExecutor, RouteHandle, RouteHost, StagePeer},
    registry::Config,
    schema::PublicError,
};
use budgets::{BudgetId as B, Entries};
use runtime::m16_ledger::{IdSource, LeaseClock};
use std::{
    io::{BufRead, BufReader, Write},
    net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
const PROMPT: &str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nExplain how rain forms and why plants need it. Give a detailed answer.<|im_end|>\n<|im_start|>assistant\n";
const NEW_TOKENS: usize = 32;
const DEPLOYMENT: &str = "12345678-1234-5678-9234-abcdefabcdef";
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn ms(since: Instant) -> f64 {
    since.elapsed().as_secs_f64() * 1000.0
}
/// The product tokenizer crate over the selected snapshot, via the pinned
/// native binary's private helper (api links no tokenizer).
struct Helper {
    child: Child,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
}
struct HelperCodec(Mutex<Helper>);
impl HelperCodec {
    fn start(tokenizer: &std::path::Path) -> Self {
        let binary =
            std::env::var_os("MYCELIUM_COMPILER_NATIVE_BINARY").expect("pinned native binary");
        let mut child = Command::new(binary)
            .arg("tokenize")
            .arg(tokenizer)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let input = child.stdin.take().unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        Self(Mutex::new(Helper {
            child,
            input: Some(input),
            output,
        }))
    }
    fn call(&self, request: Value) -> Result<Value, PublicError> {
        let mut h = self.0.lock().unwrap();
        let input = h
            .input
            .as_mut()
            .ok_or(PublicError::new(503, "tokenizer_unavailable"))?;
        writeln!(input, "{request}").map_err(|_| PublicError::new(503, "tokenizer_unavailable"))?;
        input
            .flush()
            .map_err(|_| PublicError::new(503, "tokenizer_unavailable"))?;
        let mut line = String::new();
        h.output
            .read_line(&mut line)
            .map_err(|_| PublicError::new(503, "tokenizer_unavailable"))?;
        serde_json::from_str(&line).map_err(|_| PublicError::new(503, "tokenizer_unavailable"))
    }
    fn close(&self) {
        let mut h = self.0.lock().unwrap();
        // EOF ends the helper; require its clean exit (exact owned child).
        h.input.take();
        assert!(h.child.wait().unwrap().success());
    }
}
impl Codec for HelperCodec {
    fn encode(&self, text: &str) -> Result<Vec<u32>, PublicError> {
        serde_json::from_value(self.call(json!({"encode":text}))?["ids"].clone())
            .map_err(|_| PublicError::new(503, "tokenizer_unavailable"))
    }
    fn decode(&self, token: u32) -> Result<String, PublicError> {
        Ok(self.call(json!({"decode":token}))?["text"]
            .as_str()
            .ok_or(PublicError::new(503, "tokenizer_unavailable"))?
            .to_owned())
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
/// One route epoch: route doc, carrier executor, M16 runtime, HTTP gateway, pump.
struct Epoch {
    service: M16Requests,
    handle: RouteHandle,
    host: Arc<RouteHost>,
    addr: SocketAddr,
    bearer: String,
    stop: Arc<AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
    refusals: Arc<Mutex<Vec<String>>>,
    worker_pids: Vec<i32>,
}
impl Epoch {
    fn close(mut self) -> Vec<String> {
        self.stop.store(true, Ordering::SeqCst);
        // Wake the accept loop so it observes `stop`; a refused connect means it already exited.
        if let Err(e) = TcpStream::connect(self.addr) {
            eprintln!("epoch close wake: {e}");
        }
        self.handle.changed.notify_all();
        for t in self.threads.drain(..) {
            t.join().unwrap();
        }
        self.refusals.lock().unwrap().clone()
    }
}
fn alive(pid: i32) -> bool {
    Command::new("/bin/kill")
        .args(["-0", &pid.to_string()])
        .output()
        .unwrap()
        .status
        .success()
}
fn signal(sig: &str, pid: u32) {
    assert!(
        Command::new("/bin/kill")
            .args([sig, &pid.to_string()])
            .status()
            .unwrap()
            .success()
    );
}
fn assigned(fx: &Fixture, i: usize) -> [u64; 2] {
    [fx.ranges[i][0] as u64, fx.ranges[i][1] as u64]
}
/// Owner-issued single-use challenge per member; the verified proof consumes it.
fn prepare(
    fx: &mut Fixture,
    challenges: &native::Challenges,
    which: &[usize],
    tokens: usize,
) -> Vec<native::Admitted> {
    for &i in which {
        admit_native_load(&fx.resource_gate, &format!("node={i}"));
    }
    let began = Instant::now();
    for &i in which {
        let nonce = challenges.issue(&fx.ids[i], assigned(fx, i)).unwrap();
        fx.nodes[i].send(
            json!({"command":"prepare_native","range":fx.ranges[i],"tokens":tokens,
            "nonce":nonce,"forbidden_source":fx.source}),
        );
    }
    let admitted = which
        .iter()
        .map(|&i| {
            let signed = fx.nodes[i].result_with(allowance());
            challenges
                .admit(&fx.seed.coordinator, &fx.offers[i], &signed)
                .unwrap()
        })
        .collect();
    eprintln!("B7e prepared {which:?} in {:.1} ms", ms(began));
    admitted
}
#[allow(clippy::too_many_arguments)]
fn open_epoch(
    fx: &mut Fixture,
    epoch: u64,
    proofs: &[native::Admitted],
    secret: &iroh::SecretKey,
    codec: &Arc<HelperCodec>,
    addrs: &[Value],
) -> Epoch {
    let model = json!({"model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca",
        "manifest_digest":fx.manifest_digest});
    let stages = proofs.to_vec();
    let envelope = route::issue(
        &fx.seed.coordinator,
        &model,
        DEPLOYMENT,
        epoch,
        &secret.public().to_string(),
        &stages,
        app_host::now(),
    )
    .unwrap();
    for i in 0..2 {
        fx.nodes[i].send(json!({"command":"serve","route":envelope}));
    }
    let bearer = app_host::hex(&app_host::random(32).unwrap());
    let codec_dyn: Arc<dyn Codec> = codec.clone();
    let host = Arc::new(
        RouteHost::new(
            fx.seed.coordinator.clone(),
            envelope.clone(),
            codec_dyn.clone(),
            bearer.clone(),
        )
        .unwrap(),
    );
    let message = host.message().clone();
    let peers = (0..2)
        .map(|i| StagePeer {
            node_id: fx.ids[i].clone(),
            endpoint: addrs[i]["endpoint_id"].as_str().unwrap().parse().unwrap(),
            socket: serde_json::from_value(addrs[i]["socket"].clone()).unwrap(),
            generation: message["stages"][i]["member"]["generation"]
                .as_u64()
                .unwrap(),
        })
        .collect();
    let coordinator = fx.seed.coordinator.clone();
    let currentness: Currentness = Arc::new(move |node: &str| {
        coordinator
            .member(node)
            .ok()
            .and_then(|r| r["generation"].as_u64())
    });
    for i in 0..2 {
        eprintln!(
            "B7e epoch={epoch} stage={i} signed_generation={} live={:?}",
            message["stages"][i]["member"]["generation"],
            currentness(&fx.ids[i])
        );
    }
    let (executor, handle) = RouteExecutor::connect(
        message.clone(),
        secret.clone(),
        peers,
        currentness,
        codec_dyn,
    )
    .unwrap();
    host.attach(handle.clone()).unwrap();
    let runtime = m16_route::runtime(
        &message,
        Arc::new(Wall),
        Arc::new(Wall),
        Arc::new(Ids::default()),
    )
    .unwrap();
    let host_dyn: Arc<dyn api::m16_requests::Host> = host.clone();
    let service = M16Requests::new(
        runtime,
        host_dyn,
        Box::new(executor),
        Config::new(64, 4, 0.0).unwrap(),
    )
    .unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let refusals = Arc::new(Mutex::new(Vec::new()));
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    let mut threads = Vec::new();
    {
        let (service, stop) = (service.clone(), stop.clone());
        threads.push(std::thread::spawn(move || {
            let mut connections = Vec::new();
            for incoming in listener.incoming() {
                if stop.load(Ordering::SeqCst) {
                    break;
                }
                let stream = incoming.unwrap();
                let service = service.clone();
                connections.push(std::thread::spawn(move || {
                    let gateway = api::requests::Gateway::new(service);
                    if let Err(e) = api::http::serve_response_connection(
                        stream,
                        span(B::ModelSelection),
                        api::requests::MAX_REQUEST_BODY,
                        |r| gateway.preflight(r),
                        |r| gateway.handle(r),
                    ) {
                        // Bounded polls end streams; recorded, never silently dropped.
                        eprintln!("gateway connection ended: {e:?}");
                    }
                }));
            }
            for c in connections {
                c.join().unwrap();
            }
        }));
    }
    {
        let (service, stop, handle, refusals) = (
            service.clone(),
            stop.clone(),
            handle.clone(),
            refusals.clone(),
        );
        // Host pump: event-driven by carrier changes, bounded by the owner poll.
        threads.push(std::thread::spawn(move || {
            let mut seen = 0;
            while !stop.load(Ordering::SeqCst) {
                if let Err(e) = service.tick() {
                    let mut r = refusals.lock().unwrap();
                    if r.last().is_none_or(|last| last != e.code) {
                        r.push(e.code.to_owned());
                    }
                }
                seen = handle.wait(seen, span(B::OwnerPoll));
            }
        }));
    }
    let worker_pids = proofs
        .iter()
        .map(|p| {
            p.proof()["message"]["proof"]["worker_pid"]
                .as_i64()
                .unwrap() as i32
        })
        .collect();
    Epoch {
        service,
        handle,
        host,
        addr,
        bearer,
        stop,
        threads,
        refusals,
        worker_pids,
    }
}
struct Running {
    id: String,
    token: String,
    posted: Instant,
    events: mpsc::Receiver<(Value, Instant)>,
    reader: std::thread::JoinHandle<()>,
}
fn submit(e: &Epoch) -> Running {
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":PROMPT,"max_new_tokens":NEW_TOKENS,
        "workload_profile_id":"interactive","qos_class":"interactive","qualification":e.host.qualification().unwrap()});
    let auth = format!("Authorization: Bearer {}\r\n", e.bearer);
    let posted = Instant::now();
    let (status, accepted) = http(e.addr, "POST", "/v1/inference", &auth, &doc.to_string());
    assert_eq!(status, 202, "{accepted}");
    let id = accepted["request_id"].as_str().unwrap().to_owned();
    let token = accepted["session_token"].as_str().unwrap().to_owned();
    let (tx, events) = mpsc::channel();
    let (addr, path) = (e.addr, accepted["stream_path"].as_str().unwrap().to_owned());
    let headers = format!("{auth}X-Mycelium-Session: {token}\r\n");
    let reader = std::thread::spawn(move || stream(addr, path, headers, tx, allowance()));
    Running {
        id,
        token,
        posted,
        events,
        reader,
    }
}
/// N3: a spent or retired single-use epoch admits no further request.
fn assert_epoch_retired(e: &Epoch, phase: &str) {
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":PROMPT,"max_new_tokens":1,
        "workload_profile_id":"interactive","qos_class":"interactive",
        "qualification":route::qualification(e.host.message()).unwrap()});
    let auth = format!("Authorization: Bearer {}\r\n", e.bearer);
    let (status, refused) = http(e.addr, "POST", "/v1/inference", &auth, &doc.to_string());
    assert_eq!(
        (status, refused["error"].as_str()),
        (409, Some("route_dropped")),
        "{phase}: {refused}"
    );
    eprintln!("B8 {phase}: epoch retired, resubmit -> 409 route_dropped");
}
/// Collect events until `tokens` token events or a terminal; returns all so far.
fn collect(e: &Epoch, r: &Running, tokens: usize, all: &mut Vec<(Value, Instant)>) {
    while all.iter().filter(|(e, _)| e["type"] == "token").count() < tokens
        && !all.iter().any(|(e, _)| {
            matches!(
                e["type"].as_str(),
                Some("completed" | "cancelled" | "failed")
            )
        })
    {
        // Each event (prefill included) is bounded by the worker startup budget.
        match r.events.recv_timeout(span(B::WorkerStartup)) {
            Ok(event) => all.push(event),
            Err(error) => panic!("stream ended ({error:?}): {}", diagnose(e, &r.id, all)),
        }
    }
}
fn diagnose(e: &Epoch, id: &str, all: &[(Value, Instant)]) -> String {
    let o = e.handle.state.lock().unwrap();
    format!(
        "events={:?} terminal={:?} carrier: ready={} setup={:?} tokens={:?} completed={} failure={:?} failed_at_ms={:?} teardown_requested_ms={:?} stages={:?} finished={} refusals={:?}",
        all.iter()
            .map(|(e, _)| e["type"].clone())
            .collect::<Vec<_>>(),
        e.service.terminal(id),
        o.ready,
        o.setup_failure,
        o.tokens,
        o.completed,
        o.failure,
        o.failed_at_ms,
        o.teardown_requested_ms,
        o.stages
            .iter()
            .map(|s| (s.acknowledged_ms, s.failure.clone()))
            .collect::<Vec<_>>(),
        o.teardown_finished,
        e.refusals.lock().unwrap(),
    )
}
fn terminal(all: &[(Value, Instant)]) -> Option<&Value> {
    all.iter().map(|(e, _)| e).find(|e| {
        matches!(
            e["type"].as_str(),
            Some("completed" | "cancelled" | "failed")
        )
    })
}
fn token_ids(all: &[(Value, Instant)]) -> Vec<u64> {
    all.iter()
        .filter(|(e, _)| e["type"] == "token")
        .map(|(e, _)| e["token_id"].as_u64().unwrap())
        .collect()
}
fn wait_workers_gone(pids: &[i32]) -> f64 {
    let began = Instant::now();
    let end = began + span(B::RouterClose);
    while pids.iter().any(|p| alive(*p)) {
        assert!(Instant::now() < end, "native worker still alive: {pids:?}");
        std::thread::yield_now();
    }
    ms(began)
}
#[test]
#[ignore = "real Metal + local Qwen3; B7_MEMBER_BINARY, native pins and resource gate required"]
fn two_member_route_streams_chat_stops_settles_and_leaves() {
    let mut fx = native_process::setup(2, "b7e-route");
    let mut evidence = json!({"route_ready":false,"scope":route::SCOPE});
    let oracle: Value =
        serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap();
    let reference: Vec<u64> = serde_json::from_value(oracle["recorded"]["tokens"].clone()).unwrap();
    let snapshot = PathBuf::from(std::env::var_os("MYCELIUM_QWEN3_SNAPSHOT").unwrap());
    let tokenizer = snapshot.join("tokenizer.json");
    assert_eq!(
        protocol::canonical::sha256_prefixed(&fs::read(&tokenizer).unwrap())[7..],
        *oracle["recorded"]["identity"]["tokenizer_sha256"]
            .as_str()
            .unwrap(),
        "selected tokenizer must be the oracle's pinned tokenizer"
    );
    let codec = Arc::new(HelperCodec::start(&tokenizer));
    let prompt_ids = codec.encode(PROMPT).unwrap();
    assert_eq!(
        json!(prompt_ids),
        oracle["recorded"]["workload"]["prompt_ids"],
        "tokenized prompt == oracle prompt"
    );
    let capacity = prompt_ids.len() + 64;
    let addrs: Vec<Value> = (0..2)
        .map(|i| fx.nodes[i].call(json!({"command":"carrier_address"})))
        .collect();
    let secret = iroh::SecretKey::generate();

    // (e) Reference chat through POST -> M16 -> carrier -> SSE.
    let challenges = native::Challenges::new();
    let proofs = prepare(&mut fx, &challenges, &[0, 1], capacity);
    let e = open_epoch(&mut fx, 1, &proofs, &secret, &codec, &addrs);
    let baseline = e.service.placement_status().unwrap();
    let r = submit(&e);
    let mut all = Vec::new();
    collect(&e, &r, NEW_TOKENS + 1, &mut all);
    r.reader.join().unwrap();
    let tokens = token_ids(&all);
    eprintln!("B7e chat terminal={:?} tokens={tokens:?}", terminal(&all));
    assert_eq!(
        terminal(&all).unwrap()["type"],
        "completed",
        "{}",
        diagnose(&e, &r.id, &all)
    );
    assert_eq!(
        tokens,
        reference[..NEW_TOKENS],
        "streamed tokens == independent Python/MLX oracle"
    );
    let arrivals: Vec<Instant> = all
        .iter()
        .filter(|(e, _)| e["type"] == "token")
        .map(|(_, t)| *t)
        .collect();
    let ttft = arrivals[0].duration_since(r.posted).as_secs_f64() * 1000.0;
    let decode_s = arrivals[arrivals.len() - 1]
        .duration_since(arrivals[0])
        .as_secs_f64();
    let tps = (arrivals.len() - 1) as f64 / decode_s;
    let end_to_end = arrivals[arrivals.len() - 1]
        .duration_since(r.posted)
        .as_secs_f64();
    let reports: Vec<Value> = (0..2)
        .map(|i| fx.nodes[i].result_with(allowance()))
        .collect();
    let gone = wait_workers_gone(&e.worker_pids);
    let text: String = all
        .iter()
        .filter(|(e, _)| e["type"] == "token")
        .map(|(e, _)| e["text"].as_str().unwrap())
        .collect();
    eprintln!(
        "B7e chat tokens={tokens:?} == reference; ttft_ms={ttft:.1} tok_s={tps:.2} text={text:?}"
    );
    assert_eq!(
        e.service.placement_status().unwrap(),
        baseline,
        "reservations released"
    );
    let o = e.handle.state.lock().unwrap();
    evidence["chat"] = json!({"request_id":r.id,"tokens":tokens,"reference":&reference[..NEW_TOKENS],
        "prompt_ids":prompt_ids,"ttft_ms":ttft,"decode_tokens_per_second":tps,
        "end_to_end_tokens_per_second":tokens.len() as f64/end_to_end,"text":text,
        "carrier_token_ms":o.token_times_ms,"teardown":o.stages.iter().map(|s| json!({"ack_ms":s.acknowledged_ms,"failure":s.failure})).collect::<Vec<_>>(),
        "member_reports":reports,"workers_gone_ms":gone,"events":all.iter().map(|(e,_)|e["type"].clone()).collect::<Vec<_>>()});
    drop(o);
    assert_epoch_retired(&e, "after completed chat");
    evidence["chat"]["pump_refusals"] = json!(e.close());

    // (f) Public Stop mid-stream.
    let proofs = prepare(&mut fx, &challenges, &[0, 1], capacity);
    let e = open_epoch(&mut fx, 2, &proofs, &secret, &codec, &addrs);
    let r = submit(&e);
    let mut all = Vec::new();
    collect(&e, &r, 4, &mut all);
    let auth = format!(
        "Authorization: Bearer {}\r\nX-Mycelium-Session: {}\r\n",
        e.bearer, r.token
    );
    let stop_at = Instant::now();
    let (status, body) = http(
        e.addr,
        "DELETE",
        &format!("/v1/inference/{}", r.id),
        &auth,
        "",
    );
    assert_eq!(status, 202, "{body}");
    let delete_ms = ms(stop_at);
    let mut seen = e.handle.wait(usize::MAX, Duration::ZERO);
    while !e.handle.state.lock().unwrap().teardown_finished {
        seen = e.handle.wait(seen, span(B::OwnerPoll));
        assert!(
            stop_at.elapsed() < span(B::WorkerStartup),
            "stop teardown bounded"
        );
    }
    let both_ack_ms = ms(stop_at);
    collect(&e, &r, usize::MAX, &mut all);
    r.reader.join().unwrap();
    assert_eq!(terminal(&all).unwrap()["type"], "cancelled");
    let terminal_ms = all.last().unwrap().1.duration_since(stop_at).as_secs_f64() * 1000.0;
    let streamed = token_ids(&all);
    assert_eq!(
        streamed,
        reference[..streamed.len()],
        "pre-Stop tokens are reference tokens"
    );
    let reports: Vec<Value> = (0..2)
        .map(|i| fx.nodes[i].result_with(allowance()))
        .collect();
    let gone = wait_workers_gone(&e.worker_pids);
    let o = e.handle.state.lock().unwrap();
    assert!(
        o.stages.iter().all(|s| s.acknowledged_ms.is_some()),
        "both workers acknowledged"
    );
    eprintln!(
        "B7e stop delete_ms={delete_ms:.1} both_ack_ms={both_ack_ms:.1} terminal_ms={terminal_ms:.1}"
    );
    evidence["stop"] = json!({"tokens_before_stop":streamed,"delete_response_ms":delete_ms,
        "both_workers_acknowledged_ms":both_ack_ms,"per_stage_ack_ms_from_teardown":o.stages.iter().map(|s| s.acknowledged_ms).collect::<Vec<_>>(),
        "terminal_after_stop_ms":terminal_ms,"member_reports":reports,"workers_gone_ms":gone});
    drop(o);
    assert_eq!(e.host.qualification().unwrap_err().code, "route_dropped");
    assert_epoch_retired(&e, "after stop");
    evidence["stop"]["pump_refusals"] = json!(e.close());

    // (f) Carrier stall: freeze the final-stage member process mid-stream.
    let proofs = prepare(&mut fx, &challenges, &[0, 1], capacity);
    let e = open_epoch(&mut fx, 3, &proofs, &secret, &codec, &addrs);
    let r = submit(&e);
    let mut all = Vec::new();
    collect(&e, &r, 3, &mut all);
    let member_pid = fx.nodes[1].child.id();
    signal("-STOP", member_pid);
    let stalled = Instant::now();
    let mut seen = e.handle.wait(usize::MAX, Duration::ZERO);
    while e.handle.state.lock().unwrap().failure.is_none() {
        seen = e.handle.wait(seen, span(B::OwnerPoll));
        assert!(
            stalled.elapsed() < span(B::WorkerStartup),
            "stall must hit its deadline"
        );
    }
    let failed_ms = ms(stalled);
    let code = e.handle.state.lock().unwrap().failure.unwrap();
    signal("-CONT", member_pid);
    collect(&e, &r, usize::MAX, &mut all);
    r.reader.join().unwrap();
    let t = terminal(&all).unwrap().clone();
    let settled_ms = ms(stalled);
    let reports: Vec<Value> = (0..2)
        .map(|i| fx.nodes[i].result_with(allowance()))
        .collect();
    let gone = wait_workers_gone(&e.worker_pids);
    let o = e.handle.state.lock().unwrap();
    eprintln!(
        "B7e stall failure={code} after {failed_ms:.1} ms; terminal={t} settled {settled_ms:.1} ms"
    );
    assert_eq!(t["type"], "failed");
    evidence["stall"] = json!({"failure":code,"failure_after_stall_ms":failed_ms,"terminal":t,
        "terminal_after_stall_ms":settled_ms,"stages":o.stages.iter().map(|s| json!({"ack_ms":s.acknowledged_ms,"failure":s.failure})).collect::<Vec<_>>(),
        "member_reports":reports,"workers_gone_ms":gone});
    drop(o);
    assert_eq!(e.host.qualification().unwrap_err().code, "route_dropped");
    assert_epoch_retired(&e, "after stall");
    evidence["stall"]["pump_refusals"] = json!(e.close());

    // (f) Member death mid-stream: SIGKILL the exact final-stage member process.
    let proofs = prepare(&mut fx, &challenges, &[0, 1], capacity);
    let e = open_epoch(&mut fx, 4, &proofs, &secret, &codec, &addrs);
    let baseline = e.service.placement_status().unwrap();
    let r = submit(&e);
    let mut all = Vec::new();
    collect(&e, &r, 3, &mut all);
    let killed = Instant::now();
    signal("-KILL", member_pid);
    let status = fx.nodes[1].child.wait().unwrap();
    let reaped_ms = ms(killed);
    let dead_worker = e.worker_pids[1];
    let worker_gone = wait_workers_gone(&[dead_worker]);
    m16_route::record_stage_exit(&e.handle, 1, json!({"process_exited":true,"worker_absent":true,
        "member_pid":member_pid,"exit_signal":std::os::unix::process::ExitStatusExt::signal(&status),
        "worker_pid":dead_worker,"observed_by":"owner_test_driver"})).unwrap();
    collect(&e, &r, usize::MAX, &mut all);
    r.reader.join().unwrap();
    let t = terminal(&all).unwrap().clone();
    let settled_ms = ms(killed);
    let survivor = fx.nodes[0].result_with(allowance());
    wait_workers_gone(&e.worker_pids);
    let o = e.handle.state.lock().unwrap();
    eprintln!(
        "B7e death terminal={t} after {settled_ms:.1} ms; failure={:?} at {:?}",
        o.failure, o.failed_at_ms
    );
    assert_eq!(t["type"], "failed");
    assert_eq!(
        e.service.placement_status().unwrap(),
        baseline,
        "death releases reservations"
    );
    evidence["death"] = json!({"member_reaped_ms":reaped_ms,"dead_worker_gone_ms":worker_gone,
        "failure":o.failure,"terminal":t,"terminal_after_kill_ms":settled_ms,
        "stages":o.stages.iter().map(|s| json!({"ack_ms":s.acknowledged_ms,"failure":s.failure,"death":s.death_evidence})).collect::<Vec<_>>(),
        "survivor_report":survivor,"dead_member_root_retains_custody_debt":true});
    drop(o);

    // (f) Member leave through the product leave state machine, with a live worker.
    let proofs = prepare(&mut fx, &challenges, &[0], capacity);
    let leave_pid = proofs[0].proof()["message"]["proof"]["worker_pid"]
        .as_i64()
        .unwrap() as i32;
    // N3: the death already retired this epoch; the withdrawal then revokes it.
    assert_eq!(e.host.qualification().unwrap_err().code, "route_dropped");
    let began = Instant::now();
    let left = fx.nodes[0].call(json!({"command":"leave","mode":"smooth"}));
    let leave_ms = ms(began);
    let withdrawn = e.host.withdraw(&fx.offers[0], &left["withdrawal"]).unwrap();
    let not_ready_ms = ms(began);
    assert_eq!(
        e.host.qualification().err().unwrap().code,
        "readiness_revoked"
    );
    assert_eq!(withdrawn["reason"], "member_leave");
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":PROMPT,"max_new_tokens":1,
        "workload_profile_id":"interactive","qos_class":"interactive","qualification":route::qualification(e.host.message()).unwrap()});
    let (status, refused) = http(
        e.addr,
        "POST",
        "/v1/inference",
        &format!("Authorization: Bearer {}\r\n", e.bearer),
        &doc.to_string(),
    );
    assert_eq!(
        (status, refused["error"].as_str()),
        (409, Some("readiness_revoked")),
        "{refused}"
    );
    assert!(!alive(leave_pid), "leave reaped the worker");
    assert!(!fx.paths[0].exists(), "received shard retired");
    let ledger: Value = serde_json::from_slice(
        &fs::read(fx.member_roots[0].join("chain-runs/compiler-storage.private.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(ledger["entries"], json!({}), "refunded");
    assert_eq!(left["record"]["state"], "left");
    assert_eq!(
        left["record"]["compute_cleanup"],
        json!({"state":"proven","code":null})
    );
    let nonce2 = challenges.issue(&fx.ids[0], assigned(&fx, 0)).unwrap();
    fx.nodes[0].send(json!({"command":"prepare_native","range":[0,14],"tokens":1,"nonce":nonce2,"forbidden_source":fx.source}));
    assert_eq!(fx.nodes[0].refusal(), "member_left");
    fx.nodes[0].close();
    // Durable reopen after exit: product leave record and ledger are what remain.
    let reopened = api::leave::MemberLeave::new(&fx.member_roots[0], api::leave::Limits::default())
        .status()
        .unwrap()
        .unwrap();
    assert_eq!(reopened["state"], "left");
    assert!(!api::leave::cleanup_pending(&reopened));
    let consent = standing_consent::load(&fx.member_roots[0]).unwrap();
    eprintln!(
        "B7e leave {leave_ms:.1} ms; route not-ready {not_ready_ms:.1} ms; consent={:?}",
        consent.as_ref().map(|c| c["state"].clone())
    );
    evidence["leave"] = json!({"leave_ms":leave_ms,"route_not_ready_ms":not_ready_ms,"reply":left,
        "withdrawal_verified":withdrawn,"submit_after_leave":{"status":status,"body":refused},
        "reopened":reopened,"consent_state":consent.map(|c| c["state"].clone()),"worker_pid":leave_pid});
    evidence["death"]["pump_refusals"] = json!(e.close());
    codec.close();
    let began = fx.began;
    let Fixture {
        seed, nodes, root, ..
    } = fx;
    seed.close();
    drop(nodes);
    drop(root);
    if let Some(path) = std::env::var_os("B7_ROUTE_EVIDENCE") {
        fs::write(path, serde_json::to_vec_pretty(&evidence).unwrap()).unwrap();
    }
    eprintln!(
        "B7e total elapsed={:?}; owner remains test driver; route_ready=false",
        began.elapsed()
    );
}
