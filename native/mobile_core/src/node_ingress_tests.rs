// SPDX-License-Identifier: AGPL-3.0-only
// Included in node::tests: real loopback transport/inbox, production lifecycle
// transitions and Qwen kernels with tiny synthetic weights; not physical proof.

// Drop safely on an intended RED assertion too (Tokio forbids Runtime::drop
// from an async context). Every test-owned blocking hold has a bounded wait.
struct IngressRuntime(Option<tokio::runtime::Runtime>);
impl std::ops::Deref for IngressRuntime {
    type Target = tokio::runtime::Runtime;
    fn deref(&self) -> &Self::Target { self.0.as_ref().unwrap() }
}
impl Drop for IngressRuntime {
    fn drop(&mut self) { if let Some(rt) = self.0.take() { rt.shutdown_background(); } }
}

async fn ingress_wait(mut ready: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while !ready() { tokio::time::sleep(Duration::from_millis(1)).await; }
    }).await.expect("deterministic ingress barrier reached");
}

fn ingress_manifest(name: &str) -> Vec<u8> {
    let mut frame = wire::decode_frame(&golden("01-manifest-locked.bin")).unwrap();
    for key in ["path_id", "request_id"] {
        frame.body[key] = json!(name);
        frame.body["manifest"][key] = json!(name);
    }
    frame.body["build"]["path_id"] = json!(name);
    frame.body["build"]["request"]["request_id"] = json!(name);
    wire::encode_frame(&frame.message_type, &frame.body, &frame.payload).unwrap()
}

async fn ingress_control(controller: &NodeTransport, phone: &NodeTransport, command: &str) -> Value {
    let bytes = serde_json::to_vec(&json!({"protocol":crate::node_control::NODE_CONTROL_PROTOCOL,
        "command_id":format!("ingress-{command}"),"run_id":"ingress-run","deployment_id":"ingress-dep",
        "command":command,"payload":{}})).unwrap();
    let response: Value = serde_json::from_slice(&controller.control_call(phone.addr(), &bytes).await.unwrap()).unwrap();
    assert_eq!(response["ok"], true, "{response}");
    response
}

async fn ingress_accepted_resume(shared: &Arc<Mutex<Shared>>, phone: &NodeTransport) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let seed = Identity::from_secret("ingress-seed", &[91;32]).unwrap();
    let reply = membership::envelope(&seed, json!({"sender_endpoint_id":"ingress-seed",
        "protocol":membership::RESUME_ACCEPTANCE_PROTOCOL,"membership_generation":2,
        "lease_expires_at":now_s()+60.0})).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut member = test_membership("old", 1);
    member.seed_url = format!("http://{}",listener.local_addr().unwrap());
    member.pinned = seed.public_key_record();
    member.pending_resume = Some(("accepted-new".into(),json!({"test_pending_envelope":true})));
    shared.lock().unwrap().membership = Some(member.clone());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut input = [0u8;8192]; socket.read(&mut input).await.unwrap();
        let body = serde_json::to_vec(&reply).unwrap();
        let header = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len());
        socket.write_all(header.as_bytes()).await.unwrap();
        socket.write_all(&body).await.unwrap();
    });
    let dir = std::env::temp_dir().join(format!("native-ingress-{}-{}",std::process::id(),phone.endpoint_id()));
    std::fs::create_dir_all(&dir).unwrap();
    let supervisor = Supervisor { identity:Arc::new(Identity::from_secret(&phone.endpoint_id(), &[92;32]).unwrap()),
        http:reqwest::Client::new(), shared:shared.clone(), transport:phone.clone(), data_dir:dir.clone(), timing:NodeTiming::default() };
    assert!(matches!(supervisor.resume_once(member).await, Step::Held));
    server.await.unwrap();
    assert_eq!(shared.lock().unwrap().membership.as_ref().unwrap().cfg.incarnation,"accepted-new");
    std::fs::remove_dir_all(dir).unwrap();
}

async fn ingress_retirement_case(resume: bool, after_dequeue: bool) {
    use crate::transport::PeerBinding;
    let phone = NodeTransport::bind(&[93;32],Reachability::LocalOnly).await.unwrap();
    let entry = NodeTransport::bind(&[94;32],Reachability::LocalOnly).await.unwrap();
    let last = NodeTransport::bind(&[95;32],Reachability::LocalOnly).await.unwrap();
    let controller = NodeTransport::bind(&[96;32],Reachability::LocalOnly).await.unwrap();
    let bindings = vec![PeerBinding {generation:1,node_id:"node-mac-entry".into(),addr:entry.addr()},
        PeerBinding {generation:1,node_id:"node-mac-final".into(),addr:last.addr()}];

    entry.configure_peer(phone.addr().id,1).await;
    last.configure_peer(phone.addr().id,1).await;
    let (entry_tx, mut entry_rx) = mpsc::channel(8);
    let (last_tx, mut last_rx) = mpsc::channel(8);
    let entry_accept = entry.spawn_accept(entry_tx);
    let last_accept = last.spawn_accept(last_tx);
    let (entered_tx,entered_rx) = std::sync::mpsc::channel();
    let (release_tx,release_rx) = std::sync::mpsc::channel();
    let mut runtime = tiny_qwen();
    runtime.execute(&StageCall::prefill("old-replay",1,&[0.0;8])).unwrap();
    let shared = lifecycle_shared(Box::new(HeldQwen {inner:runtime,entered:entered_tx,release:release_rx}));
    {
        let mut s = shared.lock().unwrap();
        s.status.stage_loaded = Some("stage-golden".into());
        s.membership = Some(test_membership("old",1));
    }
    let admission = shared.lock().unwrap().admission.clone();
    phone.configure_route_peers_authorized(bindings.clone(), admission).await;
    let (tx, rx) = mpsc::channel(8);
    let accept = phone.spawn_accept_dispatch(tx.clone());
    let (control_tx, control_rx) = mpsc::channel(8);
    phone.set_control_handler(Some(control_tx)).await;
    phone.configure_controllers([controller.addr().id]).await;
    let control = tokio::spawn(serve_control(control_rx, shared.clone(), phone.clone(),
        Arc::new(Identity::from_secret(&phone.endpoint_id(), &[97;32]).unwrap()), "old".into()));
    ingress_control(&controller,&phone,"hello").await;
    shared.lock().unwrap().control_peers = true;

    // A separate, single-blocking-thread runtime makes the interval between
    // dequeue and blocking dispatch controllable without a production test hook.
    // The control server stays on this runtime, so stop remains a real command.
    let dispatch_runtime = IngressRuntime(Some(tokio::runtime::Builder::new_multi_thread().worker_threads(1)
        .max_blocking_threads(1).enable_all().build().unwrap()));
    let inbox = dispatch_runtime.spawn(serve_inbox(rx,shared.clone(),phone.clone()));
    entry.send(phone.addr(),1,&golden("01-manifest-locked.bin")).await.unwrap();
    let (pool_release_tx,pool_release_rx) = std::sync::mpsc::channel();
    let (pool_entered_tx,pool_entered_rx) = std::sync::mpsc::channel();
    let mut pool_holder = None;
    let mut held_send = None;
    if after_dequeue {
        pool_holder = Some(dispatch_runtime.spawn_blocking(move || {
            pool_entered_tx.send(()).unwrap();
            pool_release_rx.recv_timeout(Duration::from_secs(20)).unwrap();
        }));
        pool_entered_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    } else {
        let (sender,addr) = (entry.clone(),phone.addr());
        held_send = Some(tokio::spawn(async move { sender.send(addr,1,&golden("02-hop-prefill-in.bin")).await }));
        ingress_wait(|| entered_rx.try_recv().is_ok()).await;
    }
    let old_manifest = ingress_manifest("queued-old");
    let (sender,addr,frame) = (entry.clone(),phone.addr(),old_manifest.clone());
    let old_send = tokio::spawn(async move { sender.send(addr,1,&frame).await });
    if after_dequeue {
        ingress_wait(|| shared.lock().unwrap().inflight_for_request("queued-old") == 1).await;
        assert_eq!(tx.capacity(),8,"old manifest has left the real inbox");
    } else {
        ingress_wait(|| tx.capacity() == 7).await;
        assert_eq!(shared.lock().unwrap().inflight_for_request("queued-old"),0,
            "transport has queued it but worker is held in the real Qwen checkpoint");
    }
    if resume { ingress_accepted_resume(&shared,&phone).await; }
    else {
        let stopped = ingress_control(&controller,&phone,"stop").await;
        let observation = &stopped["result"]["final_observation"];
        crate::identity::verify(&observation["observation"],&observation["signature"],&observation["verification_key"]).unwrap();
    }
    assert!(phone.peer_for_node("node-mac-entry").await.is_none());
    // Same endpoint AND generation: a current-peer lookup cannot distinguish
    // old admission from successor authority. Only the retained epoch can.
    let admission = shared.lock().unwrap().admission.clone();
    phone.configure_route_peers_authorized(bindings, admission).await;
    if after_dequeue { pool_release_tx.send(()).unwrap(); pool_holder.unwrap().await.unwrap(); }
    else { release_tx.send(()).unwrap(); }
    let _ = tokio::time::timeout(Duration::from_secs(5),old_send).await.unwrap().unwrap();
    if let Some(send) = held_send { let _ = tokio::time::timeout(Duration::from_secs(5),send).await.unwrap().unwrap(); }
    ingress_wait(|| tx.capacity()==8 && shared.lock().unwrap().inflight.is_empty()).await;
    // FIFO sentinel's ACK proves the old queue item has completed, even if its
    // revoked connection disappeared before the dispatcher answered it.
    entry.send(phone.addr(),1,&ingress_manifest("successor")).await.unwrap();
    let (old_path, operations, replay, received) = {
        let s = shared.lock().unwrap();
        (s.stage_node.as_ref().unwrap().path("queued-old").is_some(),
         s.stage.as_ref().unwrap().applied_operation_count(),
         s.stage.as_ref().unwrap().replay_result_count(),s.status.frames_received)
    };
    assert!(!old_path,"retired transport-queued manifest recreated path state (resume={resume}, after_dequeue={after_dequeue})");
    assert_eq!(operations,Some(1),"retired work cannot commit operations");
    assert_eq!(replay,Some(0),"retired work cannot recreate replay");
    assert_eq!(received,if after_dequeue {2} else {3},"stale admission must not enter stage dispatch");
    assert!(entry_rx.try_recv().is_err() && last_rx.try_recv().is_err(),"no stale publication");

    // Identical formerly queued manifest now newly admitted works. Its hop
    // must compute and actually publish to the final node, not just ACK.
    entry.send(phone.addr(),1,&old_manifest).await.unwrap();
    let mut hop = wire::decode_frame(&golden("02-hop-prefill-in.bin")).unwrap();
    hop.body["path_id"] = json!("queued-old"); hop.body["request_id"] = json!("queued-old");
    // The identity key binds both renamed fields; an unchanged golden key is
    // correctly ACKed as REJECTED before compute, not a successful hop.
    hop.body["idempotency_key"] = json!(crate::stage_wire::idempotency_key(
        "queued-old", "queued-old", hop.body["path_attempt"].as_i64().unwrap(),
        hop.body["phase"].as_str().unwrap(), hop.body["token_index"].as_i64().unwrap(),
        hop.body["hop_index"].as_i64().unwrap()));
    let bytes = wire::encode_frame(&hop.message_type,&hop.body,&hop.payload).unwrap();
    release_tx.send(()).unwrap();
    entry.send(phone.addr(),1,&bytes).await.unwrap();
    let forwarded = tokio::time::timeout(Duration::from_secs(5),last_rx.recv()).await.unwrap_or_else(|_| {
        let s = shared.lock().unwrap();
        panic!("new admission did not publish: error={:?}, executed={}, operations={:?}, replay={:?}, entry={:?}",
            s.status.last_error, s.status.frames_executed, s.stage.as_ref().unwrap().applied_operation_count(),
            s.stage.as_ref().unwrap().replay_result_count(), entry_rx.try_recv());
    }).unwrap();
    assert_eq!(forwarded.frame.body["request_id"],"queued-old");
    assert_eq!(shared.lock().unwrap().stage.as_ref().unwrap().applied_operation_count(),Some(2));
    assert_eq!(shared.lock().unwrap().stage.as_ref().unwrap().replay_result_count(),Some(1));
    // Same-session replay retains its prior no-recompute/no-republication semantics.
    entry.send(phone.addr(),1,&bytes).await.unwrap();
    assert_eq!(shared.lock().unwrap().stage.as_ref().unwrap().applied_operation_count(),Some(2));
    assert!(last_rx.try_recv().is_err());
    inbox.abort(); control.abort(); accept.abort(); entry_accept.abort(); last_accept.abort();
    phone.close().await; entry.close().await; last.close().await; controller.close().await;
    drop(dispatch_runtime);
}

#[tokio::test(flavor="multi_thread",worker_threads=2)]
async fn ingress_queued_manifest_retired_by_accepted_resume() { ingress_retirement_case(true,false).await; }
#[tokio::test(flavor="multi_thread",worker_threads=2)]
async fn ingress_queued_manifest_retired_by_stop() { ingress_retirement_case(false,false).await; }
#[tokio::test(flavor="multi_thread",worker_threads=2)]
async fn ingress_dequeued_manifest_retired_by_accepted_resume() { ingress_retirement_case(true,true).await; }
#[tokio::test(flavor="multi_thread",worker_threads=2)]
async fn ingress_dequeued_manifest_retired_by_stop() { ingress_retirement_case(false,true).await; }

#[tokio::test(flavor="multi_thread",worker_threads=2)]
async fn ingress_revoked_before_async_peer_withdrawal() {
    use crate::node_control::ControlEnv;
    use crate::transport::PeerBinding;
    for action in ["stop", "resume", "route"] {
        let phone = NodeTransport::bind(&[101;32],Reachability::LocalOnly).await.unwrap();
        let entry = NodeTransport::bind(&[102;32],Reachability::LocalOnly).await.unwrap();
        let shared = lifecycle_shared(Box::new(tiny_qwen()));
        let admission = shared.lock().unwrap().admission.clone();
        phone.configure_route_peers_authorized(vec![PeerBinding {
            generation:1, node_id:"node-mac-entry".into(), addr:entry.addr()
        }],admission).await;
        let (tx,mut rx) = mpsc::channel(8);
        let accept = phone.spawn_accept(tx);
        entry.send(phone.addr(),1,&ingress_manifest("pre-await")).await.unwrap();
        let inbound = rx.recv().await.unwrap();
        assert!(!inbound.admission.revoked());
        {
            let mut s = shared.lock().unwrap();
            match action {
                "stop" => NodeControlEnv {s:&mut s,endpoint_addr:Value::Null,
                    transport_snapshot:json!({}),install:None,stop:false}.stop(),
                "resume" => install_resumed_membership(&mut s,test_membership("next",2),Some(now_s()+60.0)),
                _ => { clear_route(&mut s); }
            }
            // No transport operation or await has occurred since retirement.
            assert!(inbound.admission.revoked(),"{action}");
        }
        // Even a late RequestWork/child cannot borrow the current state root.
        let work = RequestWork::admitted(&shared,"pre-await",inbound.admission.clone());
        assert!(work.blocked() && work.child().blocked());
        assert_eq!(stage_step(&shared,&inbound).unwrap_err(),"request_cancelled_or_expired");
        assert_eq!(shared.lock().unwrap().status.frames_received,0);
        assert!(shared.lock().unwrap().stage_node.as_ref().unwrap().path("pre-await").is_none());
        // The entry is still physically installed: the synchronous root revoke,
        // not later peer-table removal or a current-peer lookup, did the fencing.
        assert_eq!(phone.snapshot().await["peer_node_ids"],json!(["node-mac-entry"]));
        drop(work);
        accept.abort(); phone.close().await; entry.close().await;
    }
}

#[tokio::test(flavor="multi_thread",worker_threads=2)]
async fn ingress_stop_capability_interrupts_kernel_before_revision_teardown() {
    use crate::node_control::ControlEnv;
    let (entered_tx,entered_rx) = std::sync::mpsc::channel();
    let (release_tx,release_rx) = std::sync::mpsc::channel();
    let mut runtime = tiny_qwen();
    runtime.execute(&StageCall::prefill("old-replay",1,&[0.0;8])).unwrap();
    let shared = lifecycle_shared(Box::new(HeldQwen {inner:runtime,entered:entered_tx,release:release_rx}));
    let admission = {
        let mut s = shared.lock().unwrap();
        s.status.stage_loaded = Some("stage-golden".into());
        let manifest = wire::decode_frame(&golden("01-manifest-locked.bin")).unwrap();
        assert!(s.stage_node.as_mut().unwrap().on_frame("node-mac-entry",&manifest,&mut |_,_,_| unreachable!()).ack);
        s.admission.clone()
    };
    let inbound = Inbound {admission:admission.clone(),from:iroh::SecretKey::from_bytes(&[103;32]).public(),
        from_node:"node-mac-entry".into(),generation:1,
        frame:wire::decode_frame(&golden("02-hop-prefill-in.bin")).unwrap(),reply:None};
    let work = RequestWork::admitted(&shared,"request-phone-golden",admission);
    let owner = shared.clone();
    let worker = tokio::task::spawn_blocking(move || stage_step(&owner,&inbound));
    ingress_wait(|| entered_rx.try_recv().is_ok()).await;
    {
        let mut s = shared.lock().unwrap();
        NodeControlEnv {s:&mut s,endpoint_addr:Value::Null,transport_snapshot:json!({}),install:None,stop:false}.stop();
        // Deliberately do not call teardown_control: only the capability has
        // changed, so revision checks alone cannot make this test pass.
        assert_eq!(s.stage_revision,0);
        assert!(s.stage_busy && s.inflight_for_request("request-phone-golden") == 1);
    }
    release_tx.send(()).unwrap();
    let step = worker.await.unwrap().unwrap();
    assert!(!step.ack && step.sends.is_empty());
    assert!(work.blocked() && work.child().blocked());
    let s = shared.lock().unwrap();
    assert_eq!(s.stage_revision,0);
    assert_eq!(s.stage.as_ref().unwrap().applied_operation_count(),Some(1));
    assert_eq!(s.stage.as_ref().unwrap().replay_result_count(),Some(0));
    assert!(s.stage_node.as_ref().unwrap().path("path-phone-golden").is_none());
    assert!(!s.stage_busy);
}
