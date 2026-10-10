// SPDX-License-Identifier: AGPL-3.0-or-later
//! Real Python client -> sidecar process/UDS -> QUIC control ALPN (loopback).
//! The peer is a protocol fixture, not a physical phone or inference engine.
#![cfg(unix)]
use iroh::endpoint::presets;
use iroh::{Endpoint, RelayMode};
use mycelium_iroh_transport::sidecar::CONTROL_CALL_MAX_BYTES;
use mycelium_mobile_core::node_control::CONTROL_ALPN;
use std::net::{Ipv4Addr, SocketAddrV4};
use std::path::Path;
use std::time::Duration;
use tokio::time::timeout;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn python_control_call_crosses_authenticated_ipc_and_real_quic() {
    let phone = Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_ip_transports()
        .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .alpns(vec![CONTROL_ALPN.to_vec()])
        .bind()
        .await
        .unwrap();
    let server = phone.clone();
    let responder = tokio::spawn(async move {
        let mut identities = Vec::new();
        for index in 0..2 {
            let connection = server.accept().await.unwrap().await.unwrap();
            assert_eq!(connection.alpn(), CONTROL_ALPN);
            identities.push(connection.remote_id().to_string());
            let (mut send, mut recv) = connection.accept_bi().await.unwrap();
            let bytes = recv.read_to_end(CONTROL_CALL_MAX_BYTES).await.unwrap();
            assert_eq!(
                bytes,
                format!("{{\"command\":\"health\",\"command_id\":\"{index}\"}}").as_bytes()
            );
            send.write_all(b"{\"ok\":true}").await.unwrap();
            send.finish().unwrap();
            // The host owns and closes each call connection after its reply.
            let _ = connection.closed().await;
        }
        identities
    });
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let python = std::env::var("MYCELIUM_TEST_PYTHON").unwrap_or_else(|_| "python3".into());
    let result = timeout(
        Duration::from_secs(30),
        tokio::process::Command::new(python)
            .arg(root.join("tests/iroh_conformance/control_call_e2e_client.py"))
            .arg(env!("CARGO_BIN_EXE_mycelium-iroh-sidecar"))
            .arg(serde_json::to_string(&phone.addr()).unwrap())
            .env("PYTHONPATH", root)
            .env_remove("PYTHONHOME")
            .kill_on_drop(true)
            .output(),
    )
    .await
    .unwrap()
    .unwrap();
    if !result.status.success() {
        responder.abort();
        phone.close().await;
        panic!(
            "Python ControlCall failed: {}",
            String::from_utf8_lossy(&result.stderr)
        );
    }
    let report: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    let identities = timeout(Duration::from_secs(3), responder)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(report["calls"], 2);
    for identity in identities {
        assert_eq!(identity, report["controller_endpoint_id"].as_str().unwrap());
    }
    phone.close().await;
}
