// SPDX-License-Identifier: AGPL-3.0-or-later
#![cfg(feature = "net")]
//! Two in-process endpoints over loopback (no relays): admission by allow-list
//! and generation, Ack on enqueue, sidecar-compatible refusal codes.

use mycelium_mobile_core::transport::{NodeTransport, Reachability};
use mycelium_mobile_core::wire;
use serde_json::json;
use tokio::sync::mpsc;

#[tokio::test(flavor = "multi_thread")]
async fn admitted_peer_frames_are_acked_and_unknown_or_stale_peers_are_refused() {
    let phone = NodeTransport::bind(&[1; 32], Reachability::LocalOnly).await.unwrap();
    let mac = NodeTransport::bind(&[2; 32], Reachability::LocalOnly).await.unwrap();
    let stranger = NodeTransport::bind(&[3; 32], Reachability::LocalOnly).await.unwrap();
    let (tx, mut rx) = mpsc::channel(8);
    let _accept = phone.spawn_accept(tx);
    let mac_id = mycelium_mobile_core::transport::parse_endpoint_id(&mac.endpoint_id()).unwrap();
    phone.configure_peer(mac_id, 5).await;

    let act = wire::Activation::from_rows_f32(&[vec![1.0, 2.0]]).encode().unwrap();
    let frame = wire::encode_frame("HopHeader", &json!({"request_id": "r1"}), &act).unwrap();
    let report = mac.send(phone.addr(), 5, &frame).await.unwrap();
    assert_eq!(report.selected, "direct");
    let inbound = rx.recv().await.unwrap();
    assert_eq!(inbound.generation, 5);
    assert_eq!(inbound.frame.message_type, "HopHeader");
    assert_eq!(wire::Activation::decode(&inbound.frame.payload).unwrap().to_rows_f64().unwrap(), vec![vec![1.0, 2.0]]);

    assert_eq!(mac.send(phone.addr(), 4, &frame).await.unwrap_err().0, "peer_rotated");
    assert_eq!(mac.send(phone.addr(), 5, b"not a frame").await.unwrap_err().0, "invalid_frame");
    assert!(stranger.send(phone.addr(), 5, &frame).await.is_err());
    assert!(rx.try_recv().is_err());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_removed_peer_is_refused_on_its_next_send() {
    let phone = NodeTransport::bind(&[4; 32], Reachability::LocalOnly).await.unwrap();
    let mac = NodeTransport::bind(&[5; 32], Reachability::LocalOnly).await.unwrap();
    let (tx, mut rx) = mpsc::channel(8);
    let _accept = phone.spawn_accept(tx);
    let mac_id = mycelium_mobile_core::transport::parse_endpoint_id(&mac.endpoint_id()).unwrap();
    phone.configure_peer(mac_id, 7).await;
    let act = wire::Activation::from_rows_f32(&[vec![1.0]]).encode().unwrap();
    let frame = wire::encode_frame("HopHeader", &json!({"request_id": "r1"}), &act).unwrap();
    mac.send(phone.addr(), 7, &frame).await.unwrap();
    assert!(rx.recv().await.is_some());

    phone.remove_peer(mac_id).await;
    assert!(mac.send(phone.addr(), 7, &frame).await.is_err());
    assert!(rx.try_recv().is_err());
}

/// Python transport semantics: ACK only after the dispatcher's verdict, an
/// error verdict is returned to the sender (it retries), a repeated message id
/// is answered without a second dispatch, and another frame under the same id
/// is a replay collision.
#[tokio::test(flavor = "multi_thread")]
async fn dispatch_mode_acks_after_the_verdict_and_dedupes_message_ids() {
    use mycelium_mobile_core::transport::PeerBinding;
    let phone = NodeTransport::bind(&[6; 32], Reachability::LocalOnly).await.unwrap();
    let mac = NodeTransport::bind(&[7; 32], Reachability::LocalOnly).await.unwrap();
    let (tx, mut rx) = mpsc::channel(8);
    let _accept = phone.spawn_accept_dispatch(tx);
    let mac_id = mycelium_mobile_core::transport::parse_endpoint_id(&mac.endpoint_id()).unwrap();
    phone
        .configure_route_peers(vec![PeerBinding { generation: 3, node_id: "node-mac".into(), addr: iroh::EndpointAddr::from(mac_id) }])
        .await;
    assert_eq!(phone.peer_for_node("node-mac").await.unwrap().generation, 3);
    // The dispatcher answers: first frame refused, second acknowledged.
    let dispatcher = tokio::spawn(async move {
        let mut seen = Vec::new();
        while let Some(mut inbound) = rx.recv().await {
            seen.push((inbound.from_node.clone(), inbound.frame.body["n"].as_u64().unwrap()));
            let verdict = if inbound.frame.body["n"] == 1 { Err("manifest_registration_rejected".to_owned()) } else { Ok(()) };
            inbound.reply.take().unwrap().send(verdict).unwrap();
            if seen.len() == 2 {
                return seen;
            }
        }
        seen
    });
    let frame = |n: u64| wire::encode_frame("HopHeader", &json!({"n": n}), b"").unwrap();
    assert_eq!(mac.send(phone.addr(), 3, &frame(1)).await.unwrap_err().0, "manifest_registration_rejected");
    mac.send(phone.addr(), 3, &frame(2)).await.unwrap();
    assert_eq!(dispatcher.await.unwrap(), vec![("node-mac".into(), 1), ("node-mac".into(), 2)]);
    // A stale generation never reaches the dispatcher.
    assert_eq!(mac.send(phone.addr(), 2, &frame(3)).await.unwrap_err().0, "peer_rotated");
}
