// SPDX-License-Identifier: AGPL-3.0-only
// L5's native remote-codec corpus is different from the 18+14 inner Router vectors.
fn native_corpus() -> Vec<Vec<&'static str>> {
    transport::qualification::ROUTER_NATIVE_CORPUS_TSV.lines()
        .filter(|l| !l.starts_with('#') && !l.is_empty())
        .map(|l| l.split('\t').collect()).collect()
}
fn native_corpus_child(expected: EndpointId) {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let address = host.address().unwrap();
    let socket = *address.ip_addrs().next().unwrap();
    println!("L9_READY {}", serde_json::json!({"id":address.id.to_string(),"socket":socket.to_string(),"pid":std::process::id()}));
    std::io::stdout().flush().unwrap();
    let conn = host.accept(&clock, clock.deadline()).unwrap();
    assert_eq!(conn.remote_id(), expected);
    assert_eq!(conn.alpn(), Channel::Router.alpn());
    rt.block_on(async {
        for case in native_corpus() {
            let (mut send, mut recv) = tokio::time::timeout(allowance(), conn.accept_bi()).await.unwrap().unwrap();
            let request = recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES).await.unwrap();
            let frame = remote::decode_remote_frame(&request).unwrap();
            assert_eq!(frame.message_id, [5;16]);
            assert_eq!(frame.kind, RemoteKind::Transfer);
            assert_eq!(&frame.payload[..remote::REMOTE_GENERATION_BYTES], &2u64.to_be_bytes());
            send.write_all(&unhex(case[1])).await.unwrap();
            send.finish().unwrap();
        }
        tokio::time::timeout(allowance(), conn.closed()).await.unwrap();
    });
    drop(conn);
    host.close(&clock, clock.deadline()).unwrap();
    drop(host);
    drop(rt);
    assert!(UdpSocket::bind(socket).is_ok());
    println!("L9_CLOSED true");
}
#[test]
fn real_quic_all_42_native_l5_vectors_match_codec_and_attempt_verdicts() {
    use runtime::iroh_wire::{self, SendAttempt};
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let mut peer = Peer::spawn(host.address().unwrap().id, "native-corpus");
    let conn = host.connect(&peer.address, &clock, clock.deadline()).unwrap();
    let request = Exchange { peer: ConnectionFence { endpoint_id:*peer.address.id.as_bytes(), local_generation:2, remote_generation:3 }, channel:Channel::Router, message_id:[5;16], payload:payload(), deadline:clock.deadline(), maximum_response_bytes:0 };
    let transfer = iroh_wire::transfer(&request).unwrap();
    let mut verdicts = Vec::new();
    for case in native_corpus() {
        let raw = rt.block_on(async {
            tokio::time::timeout(allowance(), async {
                let (mut send, mut recv) = conn.open_bi().await.unwrap();
                send.write_all(&transfer).await.unwrap();
                send.finish().unwrap();
                recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES).await.unwrap()
            }).await.unwrap()
        });
        assert_eq!(raw, unhex(case[1]), "{} real carrier bytes", case[0]);
        let decoded = remote::decode_remote_frame(&raw);
        let decode = match &decoded { Ok(_) => "ok", Err(e) => e.code() };
        assert_eq!(decode, case[2], "{} codec", case[0]);
        if let Ok(frame) = &decoded { assert_eq!((frame.kind as u8).to_string(),case[3]); }
        let attempt = iroh_wire::classify_response(&raw, request.message_id);
        let label = match attempt { SendAttempt::Delivered=>"Delivered", SendAttempt::PeerRotated=>"PeerRotated", SendAttempt::ReplayCollision=>"ReplayCollision", SendAttempt::Retry{..}=>"Retry" };
        // Retain all verdicts before asserting, so even the RED crosses all 42 streams.
        verdicts.push((case[0],label,case[4]));
        if let SendAttempt::Retry { evict_connection } = attempt {
            assert_eq!(evict_connection, decoded.is_err() || decoded.as_ref().is_ok_and(|f| f.message_id != request.message_id), "{} eviction",case[0]);
        }
    }
    conn.close(0u32.into(), b"corpus_done");
    drop(conn);
    host.close(&clock, clock.deadline()).unwrap();
    peer.finish();
    assert_eq!(verdicts.len(),42);
    for (name,actual,expected) in verdicts { assert_eq!(actual,expected,"{name} source attempt"); }
    println!("L9_NATIVE_CORPUS vectors=42 real_quic=true");
}
