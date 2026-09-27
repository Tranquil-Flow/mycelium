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
