// SPDX-License-Identifier: AGPL-3.0-only
// Included by the real two-process carrier test; every vector is sent over QUIC.
use runtime::iroh_receive::RouterReceiver;
fn unhex(hex: &str) -> Vec<u8> {
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect()
}
fn wire_corpus() -> Vec<(Vec<u8>, bool)> {
    let positive: serde_json::Value = serde_json::from_str(include_str!(
        "../../../protocol/fixtures/continued-wire-vectors.json"
    ))
    .unwrap();
    let negative: serde_json::Value = serde_json::from_str(include_str!(
        "../../../protocol/fixtures/continued-wire-framing.json"
    ))
    .unwrap();
    positive
        .as_array()
        .unwrap()
        .iter()
        .map(|case| (unhex(case["encoded_hex"].as_str().unwrap()), true))
        .chain(
            negative
                .as_array()
                .unwrap()
                .iter()
                .map(|case| (unhex(case["frame_hex"].as_str().unwrap()), false)),
        )
        .collect()
}
fn receiver_child(expected: EndpointId) {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let address = host.address().unwrap();
    let socket = *address.ip_addrs().next().unwrap();
    println!(
        "L9_READY {}",
        serde_json::json!({"id":address.id.to_string(),"socket":socket.to_string(),"pid":std::process::id()})
    );
    std::io::stdout().flush().unwrap();
    let conn = host.accept(&clock, clock.deadline()).unwrap();
    let fence = ConnectionFence {
        endpoint_id: *expected.as_bytes(),
        local_generation: 3,
        remote_generation: 2,
    };
    let wrong = ConnectionFence {
        endpoint_id: [0; 32],
        ..fence
    };
    assert!(matches!(
        RouterReceiver::new(conn.clone(), &rt, &clock, wrong, || wrong),
        Err(Error::Authentication)
    ));
    let rotated = ConnectionFence {
        remote_generation: fence.remote_generation + 1,
        ..fence
    };
    assert!(matches!(
        RouterReceiver::new(conn.clone(), &rt, &clock, fence, || rotated),
        Err(Error::Generation)
    ));
    let mut receiver = RouterReceiver::new(conn.clone(), &rt, &clock, fence, || fence).unwrap();
    for (index, (bytes, valid)) in wire_corpus().into_iter().enumerate() {
        let result = receiver.receive(clock.deadline());
        if valid {
            let delivery = result.unwrap();
            assert_eq!(delivery.authenticated_peer(), fence);
            assert_eq!(delivery.message_id(), (index as u128).to_be_bytes());
            assert_eq!(delivery.payload(), bytes, "actual wire bytes");
            delivery.acknowledge().unwrap();
        } else {
            assert!(
                matches!(result, Err(Error::Unavailable)),
                "invalid L5 corpus bytes reached dispatch"
            );
        }
    }
    for _ in 0..2 {
        assert!(matches!(
            receiver.receive(clock.deadline()),
            Err(Error::Generation)
        ));
    }
    rt.block_on(async {
        tokio::time::timeout(allowance(), conn.closed())
            .await
            .unwrap();
    });
    drop(receiver);
    drop(conn);
    host.close(&clock, clock.deadline()).unwrap();
    drop(host);
    drop(rt);
    assert!(
        UdpSocket::bind(socket).is_ok(),
        "receiving endpoint socket leaked"
    );
    println!("L9_CLOSED true");
}
#[test]
fn real_quic_runs_shared_l5_wire_corpus_through_dispatch_ack_adapter() {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let mut peer = Peer::spawn(host.address().unwrap().id, "corpus");
    let conn = host
        .connect(&peer.address, &clock, clock.deadline())
        .unwrap();
    let corpus = wire_corpus();
    let accepted = corpus.iter().filter(|(_, valid)| *valid).count();
    let rejected = corpus.len() - accepted;
    for (index, (bytes, valid)) in corpus.into_iter().enumerate() {
        let id = (index as u128).to_be_bytes();
        let data = [2u64.to_be_bytes().as_slice(), &bytes].concat();
        let encoded = remote::encode_remote_frame(RemoteKind::Transfer, id, &data).unwrap();
        let response = rt.block_on(async {
            tokio::time::timeout(allowance(), async {
                let (mut send, mut recv) = conn.open_bi().await.unwrap();
                send.write_all(&encoded).await.unwrap();
                send.finish().unwrap();
                recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES)
                    .await
                    .unwrap()
            })
            .await
            .unwrap()
        });
        let ack = remote::decode_remote_frame(&response).unwrap();
        assert_eq!(ack.message_id, id);
        assert_eq!(
            ack.kind,
            if valid {
                RemoteKind::Ack
            } else {
                RemoteKind::Error
            }
        );
        assert_eq!(
            ack.payload,
            if valid {
                b"".as_slice()
            } else {
                b"invalid_frame"
            }
        );
    }
    // Source-derived precedence: a wrong generation returns peer_rotated even
    // when the enclosed Router frame is also malformed (sidecar.rs:2840-2846).
    for bytes in [payload(), b"malformed-router-frame".to_vec()] {
        let id = [71; 16];
        let data = [3u64.to_be_bytes().as_slice(), &bytes].concat();
        let encoded = remote::encode_remote_frame(RemoteKind::Transfer, id, &data).unwrap();
        let response = rt.block_on(async {
            tokio::time::timeout(allowance(), async {
                let (mut send, mut recv) = conn.open_bi().await.unwrap();
                send.write_all(&encoded).await.unwrap();
                send.finish().unwrap();
                recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES)
                    .await
                    .unwrap()
            })
            .await
            .unwrap()
        });
        let refusal = remote::decode_remote_frame(&response).unwrap();
        assert_eq!(refusal.kind, RemoteKind::Error);
        assert_eq!(refusal.message_id, id);
        assert_eq!(refusal.payload, b"peer_rotated");
    }
    conn.close(0u32.into(), b"corpus_done");
    drop(conn);
    host.close(&clock, clock.deadline()).unwrap();
    peer.finish();
    println!("L9_SHARED_CORPUS accepted={accepted} rejected={rejected}");
}
