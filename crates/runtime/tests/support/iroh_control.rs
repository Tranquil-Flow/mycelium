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
