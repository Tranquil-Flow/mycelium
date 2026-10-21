// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "net")]
//! Two in-process endpoints over loopback (no relays): admission by allow-list
//! and generation, Ack on enqueue, sidecar-compatible refusal codes.

use mycelium_mobile_core::transport::{NodeTransport, Reachability};
use mycelium_mobile_core::wire;
use serde_json::json;
use tokio::sync::mpsc;

#[tokio::test(flavor = "multi_thread")]
async fn telemetry_primary_is_configured_role_not_sorted_peer_and_closed_is_fatal() {
    use mycelium_mobile_core::transport::PeerBinding;
    let phone = NodeTransport::bind(&[81;32], Reachability::LocalOnly).await.unwrap();
    let entry = iroh::SecretKey::from_bytes(&[82;32]).public();
    let forward = iroh::SecretKey::from_bytes(&[83;32]).public();
    phone.configure_route_peers(vec![
        PeerBinding { node_id:"z-entry".into(), generation:1, addr:entry.into() },
        PeerBinding { node_id:"a-forward".into(), generation:1, addr:forward.into() },
    ]).await;
    assert_eq!(phone.snapshot().await["peer_node_id"], "z-entry");
    phone.remove_peer(entry).await;
    assert!(phone.snapshot().await["peer_node_id"].is_null());
    phone.close().await;
    assert_eq!(phone.snapshot().await["fatal_error"], "transport_closed");
}

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

/// Open a raw connection on `alpn` and run one request/response stream on it.
async fn stream_once(connection: &iroh::endpoint::Connection, request: &[u8]) -> Result<Vec<u8>, String> {
    let (mut send, mut recv) = connection.open_bi().await.map_err(|e| format!("open:{e}"))?;
    send.write_all(request).await.map_err(|e| format!("write:{e}"))?;
    send.finish().map_err(|e| format!("finish:{e}"))?;
    tokio::time::timeout(std::time::Duration::from_secs(10), recv.read_to_end(1 << 20))
        .await
        .map_err(|_| "timeout".to_owned())?
        .map_err(|e| format!("read:{e}"))
}

/// Revoking a controller closes its live control connection: the next stream
/// on it is refused and nothing more reaches the handler.
#[tokio::test(flavor = "multi_thread")]
async fn a_revoked_controller_loses_its_live_control_connection() {
    use mycelium_mobile_core::node_control::CONTROL_ALPN;
    use mycelium_mobile_core::transport::parse_endpoint_id;
    let phone = NodeTransport::bind(&[41; 32], Reachability::LocalOnly).await.unwrap();
    let controller = NodeTransport::bind(&[42; 32], Reachability::LocalOnly).await.unwrap();
    let (tx, _rx) = mpsc::channel(8);
    let _accept = phone.spawn_accept_dispatch(tx);
    let (control_tx, mut control_rx) = mpsc::channel(8);
    phone.set_control_handler(Some(control_tx)).await;
    let controller_id = parse_endpoint_id(&controller.endpoint_id()).unwrap();
    phone.configure_controllers([controller_id]).await;
    let answered = tokio::spawn(async move {
        let mut seen = Vec::new();
        while let Some(request) = control_rx.recv().await {
            seen.push((request.from, request.command.clone()));
            let _ = request.reply.send(b"answer".to_vec());
        }
        seen
    });
    let connection = controller.endpoint().connect(phone.addr(), CONTROL_ALPN).await.unwrap();
    assert_eq!(stream_once(&connection, b"first").await.unwrap(), b"answer");
    phone.configure_controllers([]).await;
    // The same live connection: closed, so the next stream is refused.
    assert!(stream_once(&connection, b"second").await.is_err());
    assert!(tokio::time::timeout(std::time::Duration::from_secs(5), connection.closed()).await.is_ok(), "connection closed by the phone");
    phone.set_control_handler(None).await;
    let seen = tokio::time::timeout(std::time::Duration::from_secs(5), answered).await.unwrap().unwrap();
    // Only the first command was handled, and `from` is the authenticated remote.
    assert_eq!(seen, vec![(controller_id, b"first".to_vec())]);
}

/// A route peer removed from the allow-list cannot keep delivering Router
/// frames over a connection it opened before the removal.
#[tokio::test(flavor = "multi_thread")]
async fn a_removed_route_peer_cannot_deliver_on_its_live_connection() {
    use mycelium_mobile_core::transport::{IROH_ALPN, PeerBinding, parse_endpoint_id};
    let phone = NodeTransport::bind(&[43; 32], Reachability::LocalOnly).await.unwrap();
    let mac = NodeTransport::bind(&[44; 32], Reachability::LocalOnly).await.unwrap();
    let (tx, mut rx) = mpsc::channel(8);
    let _accept = phone.spawn_accept(tx);
    let mac_id = parse_endpoint_id(&mac.endpoint_id()).unwrap();
    phone.configure_route_peers(vec![PeerBinding { generation: 2, node_id: "node-mac".into(), addr: iroh::EndpointAddr::from(mac_id) }]).await;
    let frame = wire::encode_frame("HopHeader", &json!({"n": 1}), b"").unwrap();
    let transfer = |id: u8| wire::encode_remote(wire::RemoteKind::Transfer, [id; 16], &wire::transfer_payload(2, &frame)).unwrap();
    let connection = mac.endpoint().connect(phone.addr(), IROH_ALPN).await.unwrap();
    let ack = wire::decode_remote(&stream_once(&connection, &transfer(1)).await.unwrap()).unwrap();
    assert_eq!(ack.kind, wire::RemoteKind::Ack);
    assert!(rx.recv().await.is_some());
    // The route changes and node-mac is no longer on it.
    phone.configure_route_peers(vec![]).await;
    assert!(stream_once(&connection, &transfer(2)).await.is_err());
    assert!(tokio::time::timeout(std::time::Duration::from_secs(5), connection.closed()).await.is_ok());
    assert!(rx.try_recv().is_err(), "nothing delivered after the removal");
}
