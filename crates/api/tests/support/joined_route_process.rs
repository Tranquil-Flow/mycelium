// SPDX-License-Identifier: AGPL-3.0-only
//! Real product request path over the real Router carrier: HTTP POST -> M16
//! admission -> joined-route carrier executor -> two member-owned native workers
//! (two OS processes) -> TokenEvent -> registry -> SSE. Tokens are compared with
//! the pinned Python/MLX packed oracle (never with Rust output). The owner's
//! epoch (route host, carrier executor, M16 runtime, request gateway, pump) and
//! tokenizer are PRODUCT code (`api::joined_owner`, B16); this driver remains
//! the owner operator for pairing, shard emission, worker (re)prepare, address
//! discovery, death evidence and withdrawal delivery; route_ready stays false.
use super::native_process::{self, Fixture, admit_native_load, allowance};
use super::sse_client::{http, stream};
use super::*;
use api::{
    joined_owner::{
        self,
        codec::NativeCodec,
        epoch::{Carriers, Epoch},
        link::Stage,
    },
    joined_shard::{native, route},
    m16_requests::Host as _,
    m16_route::{self, Codec, StagePeer},
};
use budgets::{BudgetId as B, Entries};
use std::{
    process::{Child, Command},
    sync::{Arc, mpsc},
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
/// The product tokenizer over the selected snapshot (api::joined_owner::codec).
fn native_codec(tokenizer: &std::path::Path) -> NativeCodec {
    let binary = std::env::var_os("MYCELIUM_COMPILER_NATIVE_BINARY").expect("pinned native binary");
    NativeCodec::start(std::path::Path::new(&binary), tokenizer).unwrap()
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
    // Owner prepare step = PRODUCT code (api::joined_owner::link::prepare).
    let ranges: Vec<[u64; 2]> = (0..fx.nodes.len()).map(|i| assigned(fx, i)).collect();
    let mut stages: Vec<Stage<'_>> = fx
        .nodes
        .iter_mut()
        .enumerate()
        .filter(|(i, _)| which.contains(i))
        .map(|(i, node)| Stage {
            member: node,
            offer: &fx.offers[i],
            range: ranges[i],
        })
        .collect();
    let admitted = joined_owner::link::prepare(
        challenges,
        &fx.seed.coordinator,
        &mut stages,
        tokens,
        &fx.source,
        allowance(),
    )
    .unwrap();
    eprintln!("B7e prepared {which:?} in {:.1} ms", ms(began));
    admitted
}
/// Thin owner driver: issue the route, deliver it to the members, then open the
/// owner's PRODUCT epoch (api::joined_owner::epoch) on the chosen carrier.
fn open_epoch(
    fx: &mut Fixture,
    epoch: u64,
    proofs: &[native::Admitted],
    secret: &iroh::SecretKey,
    codec: &Arc<NativeCodec>,
    addrs: &[Value],
) -> Epoch {
    let model = json!({"model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca",
        "manifest_digest":fx.manifest_digest});
    let envelope = route::issue(
        &fx.seed.coordinator,
        &model,
        DEPLOYMENT,
        epoch,
        &secret.public().to_string(),
        proofs,
        app_host::now(),
    )
    .unwrap();
    for i in 0..2 {
        fx.nodes[i].send(json!({"command":"serve","route":envelope}));
    }
    let message = &envelope["message"];
    let carriers = match super::member_remote::lan() {
        // Direct LAN: dial only the member-signed, owner-verified advertisements.
        Some((interface, policy)) => Carriers::Lan {
            verified: (0..2)
                .map(|i| {
                    api::joined_shard::carrier::verify(
                        &fx.seed.coordinator,
                        &fx.offers[i],
                        &addrs[i]["advert"],
                        &policy,
                        app_host::now(),
                    )
                    .unwrap()
                })
                .collect(),
            interface,
            policy,
        },
        None => Carriers::Loopback(
            (0..2)
                .map(|i| StagePeer {
                    node_id: fx.ids[i].clone(),
                    endpoint: addrs[i]["endpoint_id"].as_str().unwrap().parse().unwrap(),
                    socket: serde_json::from_value(addrs[i]["socket"].clone()).unwrap(),
                    generation: message["stages"][i]["member"]["generation"]
                        .as_u64()
                        .unwrap(),
                })
                .collect(),
        ),
    };
    let current = joined_owner::epoch::currentness(&fx.seed.coordinator);
    for i in 0..2 {
        eprintln!(
            "B7e epoch={epoch} stage={i} signed_generation={} live={:?}",
            message["stages"][i]["member"]["generation"],
            current(&fx.ids[i])
        );
    }
    let codec_dyn: Arc<dyn Codec> = codec.clone();
    Epoch::open(
        fx.seed.coordinator.clone(),
        envelope.clone(),
        secret.clone(),
        carriers,
        codec_dyn,
    )
    .unwrap()
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
        "workload_profile_id":"interactive_chat_v1","qos_class":"interactive","qualification":e.host().qualification().unwrap()});
    let auth = format!("Authorization: Bearer {}\r\n", e.bearer());
    let posted = Instant::now();
    let (status, accepted) = http(e.addr(), "POST", "/v1/inference", &auth, &doc.to_string());
    assert_eq!(status, 202, "{accepted}");
    let id = accepted["request_id"].as_str().unwrap().to_owned();
    let token = accepted["session_token"].as_str().unwrap().to_owned();
    let (tx, events) = mpsc::channel();
    let (addr, path) = (
        e.addr(),
        accepted["stream_path"].as_str().unwrap().to_owned(),
    );
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
        "workload_profile_id":"interactive_chat_v1","qos_class":"interactive",
        "qualification":route::qualification(e.host().message()).unwrap()});
    let auth = format!("Authorization: Bearer {}\r\n", e.bearer());
    let (status, refused) = http(e.addr(), "POST", "/v1/inference", &auth, &doc.to_string());
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
    let o = e.handle().state.lock().unwrap();
    format!(
        "events={:?} terminal={:?} carrier: ready={} setup={:?} tokens={:?} completed={} failure={:?} failed_at_ms={:?} teardown_requested_ms={:?} stages={:?} finished={} refusals={:?}",
        all.iter()
            .map(|(e, _)| e["type"].clone())
            .collect::<Vec<_>>(),
        e.service().terminal(id),
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
        e.refusals(),
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
/// Worker absence per stage: exact local pid for local members; the member's
/// signed status (`worker_alive:false`) for a member on another host.
fn wait_gone(fx: &Fixture, pids: &[i32]) -> f64 {
    let began = Instant::now();
    for (i, pid) in pids.iter().enumerate() {
        if fx.nodes[i].local() {
            wait_workers_gone(&[*pid]);
        } else {
            wait_remote_worker_gone(fx, i, *pid);
        }
    }
    ms(began)
}
/// The member on the other host reports, in its signed status, that the
/// exact worker pid named in its own signed proof is gone (signal 0 there).
fn wait_remote_worker_gone(fx: &Fixture, i: usize, pid: i32) {
    let end = Instant::now() + span(B::RouterClose);
    let control = fx.nodes[i].control.clone().unwrap();
    loop {
        let reply = super::member_remote::probe(&control, None, &[pid]);
        if let Ok(s) = &reply
            && s["result"]["probe_alive"][pid.to_string()] == false
        {
            return;
        }
        assert!(
            Instant::now() < end,
            "remote worker {pid} still reported alive: {reply:?}"
        );
    }
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
    // Evidence label: the signed route keeps its protocol scope (class A bytes);
    // the RUN is labelled by its carrier. A direct-LAN run on one Mac is a
    // rehearsal of the two-Mac topology, never two-node qualification, and
    // physical route_ready stays false.
    let lan_run = super::member_remote::lan().is_some();
    let mut evidence = json!({"route_ready":false,"physical_route_ready":false,
        "scope":if lan_run { "lan_two_node_rehearsed" } else { route::SCOPE },
        "route_scope":route::SCOPE,"carrier":if lan_run { "direct_lan" } else { "loopback" }});
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
    let codec = Arc::new(native_codec(&tokenizer));
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
    let baseline = e.service().placement_status().unwrap();
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
    let gone = wait_gone(&fx, &e.worker_pids());
    let text: String = all
        .iter()
        .filter(|(e, _)| e["type"] == "token")
        .map(|(e, _)| e["text"].as_str().unwrap())
        .collect();
    eprintln!(
        "B7e chat tokens={tokens:?} == reference; ttft_ms={ttft:.1} tok_s={tps:.2} text={text:?}"
    );
    assert_eq!(
        e.service().placement_status().unwrap(),
        baseline,
        "reservations released"
    );
    let o = e.handle().state.lock().unwrap();
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
        e.bearer(),
        r.token
    );
    let stop_at = Instant::now();
    let (status, body) = http(
        e.addr(),
        "DELETE",
        &format!("/v1/inference/{}", r.id),
        &auth,
        "",
    );
    if status != 202 {
        eprintln!("stop not accepted: {}", diagnose(&e, &r.id, &all));
    }
    assert_eq!(status, 202, "{body}");
    let delete_ms = ms(stop_at);
    let mut seen = e.handle().wait(usize::MAX, Duration::ZERO);
    while !e.handle().state.lock().unwrap().teardown_finished {
        seen = e.handle().wait(seen, span(B::OwnerPoll));
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
    let gone = wait_gone(&fx, &e.worker_pids());
    let o = e.handle().state.lock().unwrap();
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
    assert_eq!(e.host().qualification().unwrap_err().code, "route_dropped");
    assert_epoch_retired(&e, "after stop");
    evidence["stop"]["pump_refusals"] = json!(e.close());

    // (f) Carrier stall: freeze a LOCAL member process mid-stream (the final
    // stage when both are local; on two Macs, the member on the owner's Mac).
    // Pairing order is random on two Macs, so either stage may be the local one;
    // `B8_FAULT_STAGE=0|1` forces the faulted stage when both are local.
    let k = match std::env::var("B8_FAULT_STAGE").ok().as_deref() {
        Some("0") => 0,
        Some("1") => 1,
        _ if fx.nodes[1].local() => 1,
        _ => 0,
    };
    let survivor = 1 - k;
    assert!(
        fx.nodes[k].local(),
        "fault phases need one member on this host"
    );
    eprintln!("B8 fault phases on stage {k} (local); survivor stage {survivor}");
    evidence["fault_stage"] = json!({"faulted":k,"survivor":survivor,
        "survivor_local":fx.nodes[survivor].local()});
    let proofs = prepare(&mut fx, &challenges, &[0, 1], capacity);
    let e = open_epoch(&mut fx, 3, &proofs, &secret, &codec, &addrs);
    let r = submit(&e);
    let mut all = Vec::new();
    collect(&e, &r, 3, &mut all);
    let member_pid = fx.nodes[k].pid();
    signal("-STOP", member_pid);
    let stalled = Instant::now();
    let mut seen = e.handle().wait(usize::MAX, Duration::ZERO);
    while e.handle().state.lock().unwrap().failure.is_none() {
        seen = e.handle().wait(seen, span(B::OwnerPoll));
        assert!(
            stalled.elapsed() < span(B::WorkerStartup),
            "stall must hit its deadline"
        );
    }
    let failed_ms = ms(stalled);
    let code = e.handle().state.lock().unwrap().failure.unwrap();
    signal("-CONT", member_pid);
    collect(&e, &r, usize::MAX, &mut all);
    r.reader.join().unwrap();
    let t = terminal(&all).unwrap().clone();
    let settled_ms = ms(stalled);
    let reports: Vec<Value> = (0..2)
        .map(|i| fx.nodes[i].result_with(allowance()))
        .collect();
    let gone = wait_gone(&fx, &e.worker_pids());
    let o = e.handle().state.lock().unwrap();
    eprintln!(
        "B7e stall failure={code} after {failed_ms:.1} ms; terminal={t} settled {settled_ms:.1} ms"
    );
    assert_eq!(t["type"], "failed");
    evidence["stall"] = json!({"failure":code,"failure_after_stall_ms":failed_ms,"terminal":t,
        "terminal_after_stall_ms":settled_ms,"stages":o.stages.iter().map(|s| json!({"ack_ms":s.acknowledged_ms,"failure":s.failure})).collect::<Vec<_>>(),
        "member_reports":reports,"workers_gone_ms":gone});
    drop(o);
    assert_eq!(e.host().qualification().unwrap_err().code, "route_dropped");
    assert_epoch_retired(&e, "after stall");
    evidence["stall"]["pump_refusals"] = json!(e.close());

    // (f) Member death mid-stream: SIGKILL the exact local member process. The
    // owner first observes the loss over the signed control channel (liveness),
    // then settles the stage with exact-process evidence from this host.
    let proofs = prepare(&mut fx, &challenges, &[0, 1], capacity);
    let e = open_epoch(&mut fx, 4, &proofs, &secret, &codec, &addrs);
    let baseline = e.service().placement_status().unwrap();
    let r = submit(&e);
    let mut all = Vec::new();
    collect(&e, &r, 3, &mut all);
    let killed = Instant::now();
    signal("-KILL", member_pid);
    let status = fx.nodes[k].wait_exit();
    let reaped_ms = ms(killed);
    let dead_worker = e.worker_pids()[k];
    let worker_gone = wait_workers_gone(&[dead_worker]);
    m16_route::record_stage_exit(e.handle(), k, json!({"process_exited":true,"worker_absent":true,
        "member_pid":member_pid,"exit_signal":std::os::unix::process::ExitStatusExt::signal(&status),
        "worker_pid":dead_worker,"observed_by":"owner_test_driver"})).unwrap();
    collect(&e, &r, usize::MAX, &mut all);
    r.reader.join().unwrap();
    let t = terminal(&all).unwrap().clone();
    let settled_ms = ms(killed);
    // Remote control: after the kill the signed channel gets no reply (bounded).
    let unreachable = fx.nodes[k].control.clone().map(|c| {
        let began = Instant::now();
        let err = super::member_remote::status(&c, None).err();
        json!({"error":err,"ms":ms(began)})
    });
    let survivor_report = fx.nodes[survivor].result_with(allowance());
    if fx.nodes[survivor].local() {
        wait_workers_gone(&[e.worker_pids()[survivor]]);
    } else {
        wait_remote_worker_gone(&fx, survivor, e.worker_pids()[survivor]);
    }
    let o = e.handle().state.lock().unwrap();
    eprintln!(
        "B7e death terminal={t} after {settled_ms:.1} ms; failure={:?} at {:?}",
        o.failure, o.failed_at_ms
    );
    assert_eq!(t["type"], "failed");
    assert_eq!(
        e.service().placement_status().unwrap(),
        baseline,
        "death releases reservations"
    );
    evidence["death"] = json!({"member_reaped_ms":reaped_ms,"dead_worker_gone_ms":worker_gone,
        "failure":o.failure,"terminal":t,"terminal_after_kill_ms":settled_ms,
        "stages":o.stages.iter().map(|s| json!({"ack_ms":s.acknowledged_ms,"failure":s.failure,"death":s.death_evidence})).collect::<Vec<_>>(),
        "survivor_report":survivor_report,"dead_member_root_retains_custody_debt":true,
        "control_channel_after_kill":unreachable});
    drop(o);

    // (f) Member leave through the product leave state machine, with a live worker.
    let proofs = prepare(&mut fx, &challenges, &[survivor], capacity);
    let leave_pid = proofs[0].proof()["message"]["proof"]["worker_pid"]
        .as_i64()
        .unwrap() as i32;
    // N3: the death already retired this epoch; the withdrawal then revokes it.
    assert_eq!(e.host().qualification().unwrap_err().code, "route_dropped");
    let began = Instant::now();
    let left = fx.nodes[survivor].call(json!({"command":"leave","mode":"smooth"}));
    let leave_ms = ms(began);
    let withdrawn = e
        .host()
        .withdraw(&fx.offers[survivor], &left["withdrawal"])
        .unwrap();
    let not_ready_ms = ms(began);
    assert_eq!(
        e.host().qualification().err().unwrap().code,
        "readiness_revoked"
    );
    assert_eq!(withdrawn["reason"], "member_leave");
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":PROMPT,"max_new_tokens":1,
        "workload_profile_id":"interactive_chat_v1","qos_class":"interactive","qualification":route::qualification(e.host().message()).unwrap()});
    let (status, refused) = http(
        e.addr(),
        "POST",
        "/v1/inference",
        &format!("Authorization: Bearer {}\r\n", e.bearer()),
        &doc.to_string(),
    );
    assert_eq!(
        (status, refused["error"].as_str()),
        (409, Some("readiness_revoked")),
        "{refused}"
    );
    if fx.nodes[survivor].local() {
        assert!(!alive(leave_pid), "leave reaped the worker");
        assert!(!fx.paths[survivor].exists(), "received shard retired");
        let ledger: Value = serde_json::from_slice(
            &fs::read(fx.member_roots[survivor].join("chain-runs/compiler-storage.private.json"))
                .unwrap(),
        )
        .unwrap();
        assert_eq!(ledger["entries"], json!({}), "refunded");
    } else {
        // Remote member: its signed leave reply is the evidence.
        assert_eq!(left["refunded"], true, "{left}");
        wait_remote_worker_gone(&fx, survivor, leave_pid);
    }
    assert_eq!(left["record"]["state"], "left");
    assert_eq!(
        left["record"]["compute_cleanup"],
        json!({"state":"proven","code":null})
    );
    let nonce2 = challenges
        .issue(&fx.ids[survivor], assigned(&fx, survivor))
        .unwrap();
    fx.nodes[survivor].send(json!({"command":"prepare_native","range":fx.ranges[survivor],"tokens":1,"nonce":nonce2,"forbidden_source":fx.source}));
    assert_eq!(fx.nodes[survivor].refusal(), "member_left");
    let survivor_local = fx.nodes[survivor].local();
    fx.nodes[survivor].close();
    // Durable reopen after exit: product leave record and ledger are what remain.
    let (reopened, consent) = if survivor_local {
        let reopened =
            api::leave::MemberLeave::new(&fx.member_roots[survivor], api::leave::Limits::default())
                .status()
                .unwrap()
                .unwrap();
        assert_eq!(reopened["state"], "left");
        assert!(!api::leave::cleanup_pending(&reopened));
        let consent = standing_consent::load(&fx.member_roots[survivor]).unwrap();
        (
            json!(reopened),
            json!(consent.as_ref().map(|c| c["state"].clone())),
        )
    } else {
        (
            json!({"remote_member":"durable record checked on its own host"}),
            Value::Null,
        )
    };
    eprintln!(
        "B7e leave {leave_ms:.1} ms; route not-ready {not_ready_ms:.1} ms; consent={consent}"
    );
    evidence["leave"] = json!({"leave_ms":leave_ms,"route_not_ready_ms":not_ready_ms,"reply":left,
        "withdrawal_verified":withdrawn,"submit_after_leave":{"status":status,"body":refused},
        "reopened":reopened,"consent_state":consent,"worker_pid":leave_pid});
    evidence["death"]["pump_refusals"] = json!(e.close());
    codec.close().unwrap();
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
        "B7e total elapsed={:?}; owner composition = api::joined_owner (driver is its operator); route_ready=false",
        began.elapsed()
    );
}

// ---- B16 S5: the owner as a separate headless PROCESS (`examples/joined_owner`,
// composed only of `api::joined_owner`). This test is its operator (stdin
// commands) and its chat client (HTTP + SSE); members are `shard_member`
// processes driven ONLY by the owner process over the signed channel.
struct OwnerProcess {
    child: Child,
    input: Option<std::process::ChildStdin>,
    replies: mpsc::Receiver<Value>,
    reader: Option<std::thread::JoinHandle<()>>,
}
impl OwnerProcess {
    fn start(args: &[std::ffi::OsString]) -> Self {
        use std::io::BufRead as _;
        let exe = std::env::var_os("B16_OWNER_BINARY")
            .expect("build api --example joined_owner and set B16_OWNER_BINARY");
        let mut child = Command::new(exe)
            .args(args)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .spawn()
            .unwrap();
        let out = child.stdout.take().unwrap();
        let (tx, replies) = mpsc::channel();
        let reader = std::thread::spawn(move || {
            for line in std::io::BufReader::new(out).lines() {
                let Ok(line) = line else { return };
                let v: Value = serde_json::from_str(&line).unwrap();
                if tx.send(v).is_err() {
                    return;
                }
            }
        });
        Self {
            input: child.stdin.take(),
            child,
            replies,
            reader: Some(reader),
        }
    }
    fn send(&mut self, command: Value) -> Value {
        use std::io::Write as _;
        let input = self.input.as_mut().unwrap();
        writeln!(input, "{command}").unwrap();
        input.flush().unwrap();
        // Long owner steps (transfer, native prepare) are bounded by the
        // member wall per stage; the owner process enforces its own bounds.
        self.replies
            .recv_timeout(allowance() * 4)
            .unwrap_or_else(|e| panic!("owner reply to {command}: {e:?}"))
    }
    fn call(&mut self, command: Value) -> Value {
        let reply = self.send(command.clone());
        assert_eq!(reply["ok"], true, "{command} -> {reply}");
        reply["result"].clone()
    }
    fn close(mut self) {
        self.call(json!({"command":"close"}));
        self.input.take();
        let status = self.child.wait().unwrap();
        assert!(status.success(), "owner exit {status:?}");
        if let Some(r) = self.reader.take() {
            r.join().unwrap();
        }
    }
}
impl Drop for OwnerProcess {
    fn drop(&mut self) {
        if self.child.try_wait().unwrap().is_none() {
            self.child.kill().unwrap();
            self.child.wait().unwrap();
        }
    }
}
/// A spawned member process (remote control only, consent by its operator).
struct MemberProcess(Child);
impl Drop for MemberProcess {
    fn drop(&mut self) {
        if self.0.try_wait().unwrap().is_none() {
            self.0.kill().unwrap();
            self.0.wait().unwrap();
        }
    }
}
fn spawn_member(root: &std::path::Path) -> (MemberProcess, String) {
    let exe = std::env::var_os("B7_MEMBER_BINARY").expect("B7_MEMBER_BINARY");
    let decision = super::process::consent_file(root.parent().unwrap());
    let child = MemberProcess(
        Command::new(exe)
            .arg("--state-root")
            .arg(root)
            .args(super::member_remote::member_args())
            .arg("--consent-file")
            .arg(&decision)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    let notice = root.join("member-control.json");
    let end = Instant::now() + allowance();
    while !notice.exists() {
        assert!(
            Instant::now() < end,
            "member control endpoint not published"
        );
        std::thread::yield_now();
    }
    let info: Value = serde_json::from_slice(&fs::read(&notice).unwrap()).unwrap();
    let c = &info["member_control"];
    let addrs: Vec<&str> = c["addrs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a.as_str().unwrap())
        .collect();
    let addr = format!("{}@{}", c["endpoint_id"].as_str().unwrap(), addrs.join(","));
    (child, addr)
}
fn post_chat(addr: SocketAddr, bearer: &str, qualification: &Value, tokens: usize) -> Running {
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":PROMPT,"max_new_tokens":tokens,
        "workload_profile_id":"interactive_chat_v1","qos_class":"interactive","qualification":qualification});
    let auth = format!("Authorization: Bearer {bearer}\r\n");
    let posted = Instant::now();
    let (status, accepted) = http(addr, "POST", "/v1/inference", &auth, &doc.to_string());
    assert_eq!(status, 202, "{accepted}");
    let id = accepted["request_id"].as_str().unwrap().to_owned();
    let token = accepted["session_token"].as_str().unwrap().to_owned();
    let (tx, events) = mpsc::channel();
    let path = accepted["stream_path"].as_str().unwrap().to_owned();
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
fn events_until(r: &Running, tokens: usize, all: &mut Vec<(Value, Instant)>) {
    while all.iter().filter(|(e, _)| e["type"] == "token").count() < tokens
        && terminal(all).is_none()
    {
        match r.events.recv_timeout(span(B::WorkerStartup)) {
            Ok(event) => all.push(event),
            Err(error) => panic!("stream ended ({error:?}): {all:?}"),
        }
    }
}
use std::net::SocketAddr;
#[test]
#[ignore = "real Metal + local Qwen3; B16_OWNER_BINARY, B7_MEMBER_BINARY, native pins and resource gate required"]
fn owner_process_streams_chat_stops_and_leaves() {
    let began = Instant::now();
    let gate = std::env::var_os("B7_RESOURCE_GATE").expect("resource preflight script");
    let snapshot = PathBuf::from(std::env::var_os("MYCELIUM_QWEN3_SNAPSHOT").unwrap());
    let native_binary = std::env::var_os("MYCELIUM_COMPILER_NATIVE_BINARY").unwrap();
    let oracle: Value =
        serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap();
    let reference: Vec<u64> = serde_json::from_value(oracle["recorded"]["tokens"].clone()).unwrap();
    let prompt_ids = oracle["recorded"]["workload"]["prompt_ids"]
        .as_array()
        .unwrap()
        .len();
    let lan_run = super::member_remote::lan().is_some();
    let path = std::env::temp_dir().join(format!("b16-owner-{}", std::process::id()));
    fs::create_dir(&path).unwrap();
    let root = super::native_process::Root(path.canonicalize().unwrap());
    // The owner control key: the operator's key file, or a fresh one.
    let key = match std::env::var_os("B8_OWNER_CONTROL_KEY") {
        Some(file) => PathBuf::from(file),
        None => {
            let file = root.0.join("owner-control.key");
            let hex: String = super::member_remote::owner_secret()
                .to_bytes()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect();
            fs::write(&file, hex).unwrap();
            file
        }
    };
    let mut spawned = Vec::new();
    let mut addrs = Vec::new();
    let (m0, a0) = spawn_member(&root.0.join("member0"));
    spawned.push(m0);
    addrs.push(a0);
    match std::env::var("B8_REMOTE_MEMBER") {
        Ok(remote) => addrs.push(remote),
        Err(_) => {
            let (m1, a1) = spawn_member(&root.0.join("member1"));
            spawned.push(m1);
            addrs.push(a1);
        }
    }
    let mut args: Vec<std::ffi::OsString> = vec![
        "--state-root".into(),
        root.0.join("owner").into(),
        "--swarm".into(),
        "b16-owner".into(),
        "--control-key".into(),
        key.into(),
        "--snapshot".into(),
        snapshot.into(),
        "--native-binary".into(),
        native_binary,
    ];
    if let Some((ip, _)) = super::member_remote::lan() {
        args.extend(["--lan".into(), ip.to_string().into()]);
        for extra in std::env::var("B8_LAN_ALLOW").unwrap_or_default().split(',') {
            if !extra.is_empty() {
                args.extend(["--lan-allow".into(), extra.into()]);
            }
        }
    }
    let mut owner = OwnerProcess::start(&args);
    let started = owner.call(json!({"command":"status"}));
    eprintln!("B16 owner process: {started}");
    for addr in &addrs {
        let admitted = owner.call(json!({"command":"admit","member":addr}));
        eprintln!("B16 admitted {admitted}");
    }
    let compiled = owner.call(json!({"command":"compile","ranges":[[0,14],[14,28]]}));
    eprintln!("B16 compiled {compiled} elapsed={:?}", began.elapsed());
    let transferred = owner.call(json!({"command":"transfer"}));
    eprintln!(
        "B16 transferred {transferred} elapsed={:?}",
        began.elapsed()
    );
    let mut evidence = json!({"route_ready":false,"physical_route_ready":false,
        "scope":if lan_run { "lan_two_node_rehearsed" } else { route::SCOPE },
        "carrier":if lan_run { "direct_lan" } else { "loopback" },
        "owner":"examples/joined_owner process (api::joined_owner)","compiled":compiled,
        "transferred":transferred});
    let capacity = prompt_ids + 64;
    let open = |owner: &mut OwnerProcess, epoch: u64| -> (SocketAddr, String, Value) {
        for i in 0..2 {
            admit_native_load(&gate, &format!("stage={i}"));
        }
        owner.call(json!({"command":"prepare","tokens":capacity}));
        let opened = owner.call(json!({"command":"open","epoch":epoch}));
        (
            opened["gateway"].as_str().unwrap().parse().unwrap(),
            opened["bearer"].as_str().unwrap().to_owned(),
            opened["qualification"].clone(),
        )
    };
    // Chat: 32 greedy tokens == the independent Python/MLX oracle.
    let (addr, bearer, qualification) = open(&mut owner, 1);
    let r = post_chat(addr, &bearer, &qualification, NEW_TOKENS);
    let mut all = Vec::new();
    events_until(&r, NEW_TOKENS + 1, &mut all);
    r.reader.join().unwrap();
    let tokens = token_ids(&all);
    assert_eq!(terminal(&all).unwrap()["type"], "completed", "{all:?}");
    assert_eq!(tokens, reference[..NEW_TOKENS], "tokens == oracle");
    let ttft = all
        .iter()
        .find(|(e, _)| e["type"] == "token")
        .unwrap()
        .1
        .duration_since(r.posted)
        .as_secs_f64()
        * 1000.0;
    let settled = owner.call(json!({"command":"settle"}));
    eprintln!(
        "B16 owner-process chat tokens={tokens:?} == oracle; ttft_ms={ttft:.1}; settled={settled}"
    );
    evidence["chat"] = json!({"tokens":tokens,"reference":&reference[..NEW_TOKENS],"ttft_ms":ttft,"settled":settled});
    // Stop mid-stream through the owner's gateway.
    let (addr, bearer, qualification) = open(&mut owner, 2);
    let r = post_chat(addr, &bearer, &qualification, NEW_TOKENS);
    let mut all = Vec::new();
    events_until(&r, 4, &mut all);
    let stop_at = Instant::now();
    let auth = format!(
        "Authorization: Bearer {bearer}\r\nX-Mycelium-Session: {}\r\n",
        r.token
    );
    let (status, body) = http(
        addr,
        "DELETE",
        &format!("/v1/inference/{}", r.id),
        &auth,
        "",
    );
    assert_eq!(status, 202, "{body}");
    events_until(&r, usize::MAX, &mut all);
    r.reader.join().unwrap();
    let terminal_ms = ms(stop_at);
    assert_eq!(terminal(&all).unwrap()["type"], "cancelled", "{all:?}");
    let streamed = token_ids(&all);
    assert_eq!(
        streamed,
        reference[..streamed.len()],
        "pre-Stop tokens == oracle prefix"
    );
    let settled = owner.call(json!({"command":"settle"}));
    eprintln!(
        "B16 owner-process stop tokens={streamed:?} terminal_ms={terminal_ms:.1}; settled={settled}"
    );
    evidence["stop"] = json!({"tokens_before_stop":streamed,"terminal_after_stop_ms":terminal_ms,"settled":settled});
    // Leave: the member's product leave, the owner's verified withdrawal.
    let left = owner.call(json!({"command":"leave","stage":1}));
    assert_eq!(left["left"]["record"]["state"], "left", "{left}");
    assert_eq!(left["withdrawn"]["reason"], "member_leave", "{left}");
    assert_eq!(left["qualification_after"], "readiness_revoked", "{left}");
    evidence["leave"] = left;
    owner.close();
    // The owner closed every member it controls: the spawned ones exit 0.
    for mut m in spawned {
        let end = Instant::now() + allowance();
        let status = loop {
            if let Some(status) = m.0.try_wait().unwrap() {
                break status;
            }
            assert!(
                Instant::now() < end,
                "member did not exit after owner close"
            );
            std::thread::yield_now();
        };
        assert!(status.success(), "member exit {status:?}");
    }
    if let Some(path) = std::env::var_os("B7_ROUTE_EVIDENCE") {
        fs::write(path, serde_json::to_vec_pretty(&evidence).unwrap()).unwrap();
    }
    drop(root);
    eprintln!(
        "B16 owner process total elapsed={:?}; route_ready=false",
        began.elapsed()
    );
}

/// Lane EP2 (EP3): the owner process's work port serves FIVE chats on ONE route
/// epoch: the same prompt five times equals the pinned Python/MLX oracle each
/// time (each chat's session reset proven by its tokens), the member worker pids
/// never change, Stop of chat 3 mid-stream is followed by chat 4 equal to the
/// oracle, per-chat TTFT / request time / decode rate / member and worker RSS are
/// recorded with the host load beside each, and the run's Stop leaves no worker.
/// The 1.5 s TTFT bound is NOT asserted (a loaded host); chats 2..5 must start far
/// below the prepare every chat paid with per-request epochs.
#[test]
#[ignore = "real Metal + local Qwen3; B16_OWNER_BINARY, B7_MEMBER_BINARY, native pins and resource gate required"]
fn owner_work_port_serves_five_chats_on_one_epoch() {
    let began = Instant::now();
    let gate = std::env::var_os("B7_RESOURCE_GATE").expect("resource preflight script");
    let snapshot = PathBuf::from(std::env::var_os("MYCELIUM_QWEN3_SNAPSHOT").unwrap());
    let native_binary = std::env::var_os("MYCELIUM_COMPILER_NATIVE_BINARY").unwrap();
    let oracle: Value =
        serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap();
    let reference: Vec<u64> = serde_json::from_value(oracle["recorded"]["tokens"].clone()).unwrap();
    let prompt_ids = oracle["recorded"]["workload"]["prompt_ids"]
        .as_array()
        .unwrap()
        .len();
    let load = || {
        String::from_utf8_lossy(
            &Command::new("/usr/sbin/sysctl")
                .args(["-n", "vm.loadavg"])
                .output()
                .unwrap()
                .stdout,
        )
        .trim()
        .to_owned()
    };
    let rss_kib = |pid: i64| -> Option<u64> {
        let out = Command::new("/bin/ps")
            .args(["-o", "rss=", "-p", &pid.to_string()])
            .output()
            .ok()?;
        String::from_utf8_lossy(&out.stdout).trim().parse().ok()
    };
    let path = std::env::temp_dir().join(format!("ep2-owner-{}", std::process::id()));
    fs::create_dir(&path).unwrap();
    let root = super::native_process::Root(path.canonicalize().unwrap());
    let key = root.0.join("owner-control.key");
    let hex: String = super::member_remote::owner_secret()
        .to_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    fs::write(&key, hex).unwrap();
    let mut spawned = Vec::new();
    let mut addrs = Vec::new();
    for i in 0..2 {
        let (m, a) = spawn_member(&root.0.join(format!("member{i}")));
        spawned.push(m);
        addrs.push(a);
    }
    let member_pids: Vec<i64> = spawned.iter().map(|m| i64::from(m.0.id())).collect();
    let args: Vec<std::ffi::OsString> = vec![
        "--state-root".into(),
        root.0.join("owner").into(),
        "--swarm".into(),
        "ep2-owner".into(),
        "--control-key".into(),
        key.into(),
        "--snapshot".into(),
        snapshot.into(),
        "--native-binary".into(),
        native_binary,
    ];
    let mut owner = OwnerProcess::start(&args);
    for addr in &addrs {
        owner.call(json!({"command":"admit","member":addr}));
    }
    owner.call(json!({"command":"compile","ranges":[[0,14],[14,28]]}));
    let capacity = prompt_ids + 64;
    let wall = B::MemberChosenWall.span(Entries::ZERO).unwrap().millis() / 1000;
    for i in 0..2 {
        admit_native_load(&gate, &format!("stage={i}"));
    }
    let load_work = load();
    let work = owner.call(json!({"command":"work","tokens":capacity,"worker_wall_seconds":wall}));
    eprintln!(
        "EP2 work port serving {work} load={load_work} elapsed={:?}",
        began.elapsed()
    );
    let state = &work["state"];
    assert_eq!(state["state"], "api_running", "{work}");
    assert_eq!(state["epoch"], 1, "{work}");
    let gateway = state["gateway"].clone();
    let pids: Vec<i32> = serde_json::from_value(gateway["worker_pids"].clone()).unwrap();
    assert_eq!(pids.len(), 2, "{gateway}");
    let addr: SocketAddr = gateway["addr"].as_str().unwrap().parse().unwrap();
    let bearer = gateway["bearer"].as_str().unwrap().to_owned();
    let mut chats = Vec::new();
    for k in 1..=5usize {
        let load_before = load();
        let r = post_chat(addr, &bearer, &gateway["qualification"], NEW_TOKENS);
        let mut all = Vec::new();
        let stopped_mid = k == 3;
        let mut stop_ms = None;
        if stopped_mid {
            events_until(&r, 4, &mut all);
            let stop_at = Instant::now();
            let auth = format!(
                "Authorization: Bearer {bearer}\r\nX-Mycelium-Session: {}\r\n",
                r.token
            );
            let (status, body) = http(
                addr,
                "DELETE",
                &format!("/v1/inference/{}", r.id),
                &auth,
                "",
            );
            assert_eq!(status, 202, "{body}");
            events_until(&r, usize::MAX, &mut all);
            stop_ms = Some(ms(stop_at));
        } else {
            events_until(&r, NEW_TOKENS + 1, &mut all);
        }
        r.reader.join().unwrap();
        let tokens = token_ids(&all);
        let ends = terminal(&all).unwrap();
        let times: Vec<Instant> = all
            .iter()
            .filter(|(e, _)| e["type"] == "token")
            .map(|(_, t)| *t)
            .collect();
        let ttft = times[0].duration_since(r.posted).as_secs_f64() * 1000.0;
        let request_ms = all.last().unwrap().1.duration_since(r.posted).as_secs_f64() * 1000.0;
        let decode = (times.len() > 1).then(|| {
            (times.len() - 1) as f64
                / times[times.len() - 1]
                    .duration_since(times[0])
                    .as_secs_f64()
        });
        if stopped_mid {
            assert_eq!(ends["type"], "cancelled", "chat {k}: {all:?}");
            assert_eq!(
                tokens,
                reference[..tokens.len()],
                "chat {k}: pre-Stop tokens == oracle prefix"
            );
        } else {
            assert_eq!(ends["type"], "completed", "chat {k}: {all:?}");
            assert_eq!(
                tokens,
                reference[..NEW_TOKENS],
                "chat {k}: tokens == oracle"
            );
        }
        for pid in &pids {
            assert!(alive(*pid), "chat {k}: worker {pid} still serves");
        }
        let row = json!({"chat":k,"ttft_ms":ttft,"request_ms":request_ms,"decode_tokens_per_s":decode,
            "tokens":tokens.len(),"terminal":ends["type"],"stop_terminal_ms":stop_ms,
            "worker_pids":pids,"worker_rss_kib":pids.iter().map(|p| rss_kib(i64::from(*p))).collect::<Vec<_>>(),
            "member_rss_kib":member_pids.iter().map(|p| rss_kib(*p)).collect::<Vec<_>>(),
            "load_before":load_before,"load_after":load()});
        eprintln!("EP2 chat {row}");
        chats.push(row);
    }
    let ttft = |k: usize| chats[k - 1]["ttft_ms"].as_f64().unwrap();
    let staged = work["staged_ms"].as_f64().unwrap();
    for k in 2..=5 {
        assert!(
            ttft(k) * 4.0 < staged,
            "chat {k} starts far below the prepare a per-request epoch paid: ttft {} ms vs prepare {staged} ms",
            ttft(k)
        );
    }
    let stop_load = load();
    let stop_began = Instant::now();
    let stopped = owner.call(json!({"command":"work_stop"}));
    let whole_stop_ms = ms(stop_began);
    assert_eq!(stopped["state"], "stopped", "{stopped}");
    assert_eq!(stopped["cleanup_error"], Value::Null, "{stopped}");
    assert_eq!(stopped["epoch"], 1, "one epoch served the run: {stopped}");
    for report in stopped["serve_reports"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["node_id"].is_string())
    {
        assert_eq!(report["teardown"], "owner_closed", "{report}");
        assert_eq!(report["requests_served"], 5, "{report}");
    }
    let gone_ms = wait_workers_gone(&pids);
    eprintln!(
        "EP2 stopped {stopped}; whole Stop {whole_stop_ms:.1} ms; workers {pids:?} gone {gone_ms:.1} ms after return; load={stop_load}"
    );
    if let Some(path) = std::env::var_os("EP2_WORK_EVIDENCE") {
        fs::write(
            path,
            serde_json::to_vec_pretty(&json!({"work":work,"load_at_work":load_work,"chats":chats,
                "stopped":stopped,"stop_load":stop_load,"whole_stop_ms":whole_stop_ms,
                "workers_gone_after_stop_return_ms":gone_ms,"workers_gone_ms":gone_ms,
                "member_pids":member_pids,"route_ready":false}))
            .unwrap(),
        )
        .unwrap();
    }
    owner.close();
    for mut m in spawned {
        let end = Instant::now() + allowance();
        let status = loop {
            if let Some(status) = m.0.try_wait().unwrap() {
                break status;
            }
            assert!(
                Instant::now() < end,
                "member did not exit after owner close"
            );
            std::thread::yield_now();
        };
        assert!(status.success(), "member exit {status:?}");
    }
    for pid in &pids {
        assert!(!alive(*pid), "no worker left: {pid}");
    }
    drop(root);
    eprintln!("EP2 owner work port total elapsed={:?}", began.elapsed());
}
