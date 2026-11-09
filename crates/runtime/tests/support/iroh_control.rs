// SPDX-License-Identifier: AGPL-3.0-only
use runtime::iroh_control::{ControlRequest, NodeControlClient};
fn control_vector(name: &str) -> Vec<u8> {
    let rows: serde_json::Value = serde_json::from_str(include_str!("../../../protocol/fixtures/continuation-native-vectors.json")).unwrap();
    rows.as_array().unwrap().iter().find(|r| r["name"] == name).unwrap()["canonical_utf8"].as_str().unwrap().as_bytes().to_vec()
}
fn control_max() -> usize {
    B::PyMyceliumIrohSidecarClientControlCallMaxBytesL79.bytes().unwrap().get() as usize
}
fn control_deadline(clock: &Time) -> MonoInstant {
    clock.monotonic().checked_add(B::PyMyceliumIrohSidecarClientControlCallTimeoutCeilingSecondsL80.span(Entries::ZERO).unwrap()).unwrap()
}
fn control_child(expected: EndpointId, mode: &str) {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host_protocols(&rt,&clock,clock.deadline(),iroh::SecretKey::from_bytes(&[93;32]),&[Channel::NodeControl]).unwrap();
    let address=host.address().unwrap();
    let socket=*address.ip_addrs().next().unwrap();
    println!("L9_READY {}",serde_json::json!({"id":address.id.to_string(),"socket":socket.to_string(),"pid":std::process::id()}));
    std::io::stdout().flush().unwrap();
    let count=if mode=="control-signed" {2} else {1};
    for index in 0..count {
        let conn=host.accept(&clock,clock.deadline()).unwrap();
        assert_eq!(conn.alpn(),Channel::NodeControl.alpn());
        assert_eq!(conn.remote_id(),expected);
        if mode=="control-signed" {
            let fence=ConnectionFence{endpoint_id:*expected.as_bytes(),local_generation:3,remote_generation:2};
            let mut receiver=runtime::iroh_control_receive::NodeControlReceiver::new(conn.clone(),&rt,&clock,fence,||fence).unwrap();
            let delivery=receiver.receive(clock.deadline()).unwrap();
            assert_eq!(delivery.authenticated_peer(),fence);
            let command:serde_json::Value=serde_json::from_slice(delivery.bytes()).unwrap();
            protocol::native::validate_command(&command).unwrap();
            let fixtures:serde_json::Value=serde_json::from_str(include_str!("../../../protocol/fixtures/managed-vectors.json")).unwrap();
            let key=&fixtures[0]["context"]["expected_key_record"];
            let policy=&command["payload"]["policy"];
            let valid=protocol::identity::verify(&policy["statement"],&policy["signature"],key).is_ok();
            assert_eq!(valid,index==0,"signed policy reaches actual receiver");
            delivery.respond(&control_vector(if valid {"completed"} else {"failed"})).unwrap();
            rt.block_on(async {tokio::time::timeout(allowance(),conn.closed()).await.unwrap();});
            drop(receiver);
            drop(conn);
            continue;
        }
        rt.block_on(async {
            let (mut send,mut recv)=tokio::time::timeout(allowance(),conn.accept_bi()).await.unwrap().unwrap();
            let raw=recv.read_to_end(control_max()).await.unwrap();
            let command:serde_json::Value=serde_json::from_slice(&raw).expect("raw JSON, no Router header/generation/length wrapper");
            protocol::native::validate_command(&command).unwrap();
            let fixtures:serde_json::Value=serde_json::from_str(include_str!("../../../protocol/fixtures/managed-vectors.json")).unwrap();
            let key=&fixtures[0]["context"]["expected_key_record"];
            let policy=&command["payload"]["policy"];
            assert!(protocol::identity::verify(&policy["statement"],&policy["signature"],key).is_ok());
            if mode=="control-empty" {
                send.finish().unwrap();
            } else if mode=="control-oversize" {
                // The client stops reading and closes its call-owned connection.
                let out=vec![b'x';control_max()+1];
                match send.write_all(&out).await { Ok(())=>{ if let Err(e)=send.finish(){ eprintln!("expected early close: {e}"); } }, Err(e)=>eprintln!("expected bounded-reader close: {e}") }
            } else if mode=="control-close-observed" {
                send.write_all(&control_vector("completed")).await.unwrap();
                send.finish().unwrap();
            } else {
                println!("L9_CONTROL_RECEIVED true");
                std::io::stdout().flush().unwrap();
            }
            let reason=tokio::time::timeout(allowance(),conn.closed()).await.unwrap();
            let iroh::endpoint::ConnectionError::ApplicationClosed(close)=reason else { panic!("call must close explicitly: {reason}"); };
            assert_eq!(close.error_code,7u32.into());
            assert_eq!(close.reason.as_ref(),b"control call ended");
            if mode=="control-close-observed" {
                println!("L9_CALL_CLOSED true");
                std::io::stdout().flush().unwrap();
            }
        });
        drop(conn);
    }
    host.close(&clock,clock.deadline()).unwrap();
    drop(host);drop(rt);
    assert!(UdpSocket::bind(socket).is_ok());
    println!("L9_CLOSED true");
}
#[test]
fn real_quic_node_control_carries_signed_policy_and_uses_one_connection_per_call() {
    let rt=runtime();let clock=Time::new();
    let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();
    let mut peer=Peer::spawn(host.address().unwrap().id,"control-signed");
    let fence=ConnectionFence {endpoint_id:*peer.address.id.as_bytes(),local_generation:2,remote_generation:3};
    let (_tx,mut authority)=tokio::sync::watch::channel(Some(fence));
    // Fixture capacity is explicitly one; product's four-slot registry binding is separate.
    let client=NodeControlClient::new(&host,&clock,Entries::new(1)).unwrap();
    let command=control_vector("install_token_path-0");
    let response=client.call(ControlRequest{peer:&peer.address,fence,command:&command,deadline:control_deadline(&clock)},&mut authority).expect("NodeControl must carry raw signed command");
    assert_eq!(response,control_vector("completed"));
    let mut bad:serde_json::Value=serde_json::from_slice(&command).unwrap();
    bad["payload"]["policy"]["statement"]["max_new_tokens"]=5.into();
    let bad=protocol::canonical::to_bytes(&bad).unwrap();
    let response=client.call(ControlRequest{peer:&peer.address,fence,command:&bad,deadline:control_deadline(&clock)},&mut authority).unwrap();
    assert_eq!(response,control_vector("failed"),"transport success is not command acceptance");
    drop(client);host.close(&clock,clock.deadline()).unwrap();peer.finish();
}
#[test]
fn real_quic_node_control_empty_and_oversized_responses_never_succeed_or_retry() {
    for (mode,code) in [("control-empty","empty_control_call_result"),("control-oversize","control_response_too_large")] {
        let rt=runtime();let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();
        let mut peer=Peer::spawn(host.address().unwrap().id,mode);
        let fence=ConnectionFence{endpoint_id:*peer.address.id.as_bytes(),local_generation:2,remote_generation:3};
        let (_tx,mut authority)=tokio::sync::watch::channel(Some(fence));
        let client=NodeControlClient::new(&host,&clock,Entries::new(1)).unwrap();
        assert_eq!(client.call(ControlRequest{peer:&peer.address,fence,command:&control_vector("install_token_path-0"),deadline:control_deadline(&clock)},&mut authority).unwrap_err().0,code);
        drop(client);host.close(&clock,clock.deadline()).unwrap();peer.finish();
    }
}
#[test]
fn real_quic_node_control_cancellation_and_rotation_interrupt_held_response() {
    for cancel in [true,false] {
        let rt=runtime();let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();
        let mut peer=Peer::spawn(host.address().unwrap().id,"control-hold");
        let fence=ConnectionFence{endpoint_id:*peer.address.id.as_bytes(),local_generation:2,remote_generation:3};
        let (tx,mut authority)=tokio::sync::watch::channel(Some(fence));
        let lines=std::mem::replace(&mut peer.lines,mpsc::channel().1);
        let controller=std::thread::spawn(move || {
            loop { let line=lines.recv_timeout(allowance()).unwrap();if line=="L9_CONTROL_RECEIVED true"{break;} }
            tx.send_replace(if cancel {None} else {Some(ConnectionFence{remote_generation:4,..fence})});
            (lines,tx)
        });
        let client=NodeControlClient::new(&host,&clock,Entries::new(1)).unwrap();
        let result=client.call(ControlRequest{peer:&peer.address,fence,command:&control_vector("install_token_path-0"),deadline:control_deadline(&clock)},&mut authority);
        let (lines,_tx)=controller.join().unwrap();peer.lines=lines;
        assert_eq!(result.unwrap_err().0,if cancel {"cancelled"} else {"peer_rotated"});
        drop(client);host.close(&clock,clock.deadline()).unwrap();peer.finish();
    }
}
#[test]
fn real_quic_node_control_puts_its_explicit_close_on_the_wire_before_the_call_returns() {
    // An owner may stop driving its current-thread runtime right after a call:
    // a pending result poll waits on the thread. The call-owned connection's
    // close(7) must already be on the wire then. Before B20 it was only queued;
    // the close timer could drain the connection before the next block_on sent
    // it, and the member saw a stateless reset (the journal load flake). This
    // thread drives nothing until the peer reports what it observed.
    let rt=runtime();let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();
    let mut peer=Peer::spawn(host.address().unwrap().id,"control-close-observed");
    let fence=ConnectionFence{endpoint_id:*peer.address.id.as_bytes(),local_generation:2,remote_generation:3};
    let (_tx,mut authority)=tokio::sync::watch::channel(Some(fence));
    let client=NodeControlClient::new(&host,&clock,Entries::new(1)).unwrap();
    let response=client.call(ControlRequest{peer:&peer.address,fence,command:&control_vector("install_token_path-0"),deadline:control_deadline(&clock)},&mut authority).unwrap();
    assert_eq!(response,control_vector("completed"));
    assert_eq!(peer.line("L9_CALL_CLOSED "),"true","the peer must see the explicit close while the caller's runtime is idle");
    drop(client);host.close(&clock,clock.deadline()).unwrap();peer.finish();
}
/// More always-ready tasks than one current-thread scheduler tick runs (tokio
/// polls at most 61 queued tasks before it polls the `block_on` future again):
/// a connection driver woken inside a call's `block_on` is queued behind them,
/// so it runs only if the call keeps driving the runtime. Stands for any other
/// work a host runtime carries. Stops (each task finishes) when dropped.
struct ReadyLoad(std::sync::Arc<std::sync::atomic::AtomicBool>, Vec<tokio::task::JoinHandle<()>>);
impl ReadyLoad {
    fn on(rt: &tokio::runtime::Runtime) -> Self {
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let tasks = (0..256)
            .map(|_| {
                let stop = stop.clone();
                rt.spawn(std::future::poll_fn(move |cx| {
                    if stop.load(std::sync::atomic::Ordering::Relaxed) {
                        return std::task::Poll::Ready(());
                    }
                    cx.waker().wake_by_ref();
                    std::task::Poll::Pending
                }))
            })
            .collect();
        Self(stop, tasks)
    }
    fn stop(mut self, rt: &tokio::runtime::Runtime) {
        self.0.store(true, std::sync::atomic::Ordering::Relaxed);
        for task in std::mem::take(&mut self.1) {
            rt.block_on(task).unwrap();
        }
    }
}
impl Drop for ReadyLoad {
    fn drop(&mut self) {
        self.0.store(true, std::sync::atomic::Ordering::Relaxed);
    }
}
#[test]
fn real_quic_node_control_close_is_on_the_wire_when_a_loaded_current_thread_call_returns() {
    // B20f. One `yield_now` after the call gives the woken connection driver a
    // turn only when it is among the next 61 queued tasks; with more ready work
    // on the host runtime the call returns with close(7) still queued. This
    // thread then drives nothing until the peer reports the explicit close: an
    // unsent close never arrives (the peer's wait elapses), a drained one would
    // earn the peer a reset. Must hold whatever else the runtime carries.
    let rt=runtime();let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();
    let mut peer=Peer::spawn(host.address().unwrap().id,"control-close-observed");
    let fence=ConnectionFence{endpoint_id:*peer.address.id.as_bytes(),local_generation:2,remote_generation:3};
    let (_tx,mut authority)=tokio::sync::watch::channel(Some(fence));
    let client=NodeControlClient::new(&host,&clock,Entries::new(1)).unwrap();
    let load=ReadyLoad::on(&rt);
    let response=client.call(ControlRequest{peer:&peer.address,fence,command:&control_vector("install_token_path-0"),deadline:control_deadline(&clock)},&mut authority);
    let observed=peer.lines.recv_timeout(allowance()+allowance());
    load.stop(&rt);
    assert_eq!(response.unwrap(),control_vector("completed"));
    assert_eq!(observed.as_deref(),Ok("L9_CALL_CLOSED true"),"the peer must see close(7) while the caller's runtime is idle");
    drop(client);host.close(&clock,clock.deadline()).unwrap();peer.finish();
}
#[test]
fn real_quic_node_control_close_needs_no_flush_on_a_self_driven_runtime() {
    // Control: a multi-thread host runtime's worker sends the queued close(7)
    // while the caller's thread is idle, with or without any flush in the call.
    let rt=tokio::runtime::Builder::new_multi_thread().worker_threads(1).enable_all().build().unwrap();
    let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();
    let mut peer=Peer::spawn(host.address().unwrap().id,"control-close-observed");
    let fence=ConnectionFence{endpoint_id:*peer.address.id.as_bytes(),local_generation:2,remote_generation:3};
    let (_tx,mut authority)=tokio::sync::watch::channel(Some(fence));
    let client=NodeControlClient::new(&host,&clock,Entries::new(1)).unwrap();
    let response=client.call(ControlRequest{peer:&peer.address,fence,command:&control_vector("install_token_path-0"),deadline:control_deadline(&clock)},&mut authority);
    let observed=peer.lines.recv_timeout(allowance()+allowance());
    assert_eq!(response.unwrap(),control_vector("completed"));
    assert_eq!(observed.as_deref(),Ok("L9_CALL_CLOSED true"));
    drop(client);host.close(&clock,clock.deadline()).unwrap();peer.finish();
}
#[test]
fn real_quic_node_control_response_is_on_the_wire_when_a_current_thread_respond_returns() {
    // B20f. `respond` used to return once write_all + finish had only queued
    // the response and its FIN. A receiver whose current-thread runtime then
    // goes idle (it waits for its next command outside the runtime) never sent
    // them: the caller read nothing until its own deadline. The caller here has
    // its own multi-thread runtime; this thread drives nothing after respond.
    let rt=runtime();let clock=Time::new();
    let mut host=bind_host_protocols(&rt,&clock,clock.deadline(),iroh::SecretKey::from_bytes(&[94;32]),&[Channel::NodeControl]).unwrap();
    let address=host.address().unwrap();
    let (caller_tx,caller_rx)=mpsc::channel();
    let (result_tx,result_rx)=mpsc::channel();
    let caller=std::thread::spawn(move || {
        let crt=tokio::runtime::Builder::new_multi_thread().worker_threads(1).enable_all().build().unwrap();
        let cclock=Time::new();let mut chost=bind_host(&crt,&cclock,cclock.deadline()).unwrap();
        caller_tx.send(chost.address().unwrap().id).unwrap();
        let fence=ConnectionFence{endpoint_id:*address.id.as_bytes(),local_generation:2,remote_generation:3};
        let (_tx,mut authority)=tokio::sync::watch::channel(Some(fence));
        let client=NodeControlClient::new(&chost,&cclock,Entries::new(1)).unwrap();
        let result=client.call(ControlRequest{peer:&address,fence,command:&control_vector("install_token_path-0"),deadline:cclock.deadline()},&mut authority);
        result_tx.send(result).unwrap();
        drop(client);chost.close(&cclock,cclock.deadline()).unwrap();
    });
    let caller_id=caller_rx.recv_timeout(allowance()).unwrap();
    let conn=host.accept(&clock,clock.deadline()).unwrap();
    let fence=ConnectionFence{endpoint_id:*caller_id.as_bytes(),local_generation:3,remote_generation:2};
    let mut receiver=runtime::iroh_control_receive::NodeControlReceiver::new(conn.clone(),&rt,&clock,fence,||fence).unwrap();
    let delivery=receiver.receive(clock.deadline()).unwrap();
    assert_eq!(delivery.bytes(),control_vector("install_token_path-0"));
    let load=ReadyLoad::on(&rt);
    delivery.respond(&control_vector("completed")).unwrap();
    let observed=result_rx.recv_timeout(allowance()+allowance());
    load.stop(&rt);
    assert_eq!(observed.expect("caller finished").expect("caller read the whole response"),control_vector("completed"));
    caller.join().unwrap();
    drop(receiver);drop(conn);host.close(&clock,clock.deadline()).unwrap();
}
