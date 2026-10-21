// SPDX-License-Identifier: AGPL-3.0-only
//! Real Python client -> sidecar process/UDS -> QUIC control ALPN (loopback).
//! The peer is a protocol fixture, not a physical phone or inference engine.
#![cfg(unix)]
use iroh::endpoint::presets;
use iroh::{Endpoint, RelayMode};
use mycelium_iroh_transport::sidecar::CONTROL_CALL_MAX_BYTES;
use mycelium_mobile_core::node_control::CONTROL_ALPN;
use std::net::{Ipv4Addr, SocketAddrV4};
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::AsyncWriteExt;
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
    let mut responder = tokio::spawn(async move {
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
            timeout(Duration::from_secs(3), connection.closed())
                .await
                .expect("host closes each call while its endpoint is alive");
        }
        identities
    });
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let python = std::env::var("MYCELIUM_TEST_PYTHON").unwrap_or_else(|_| "python3".into());
    let mut child = tokio::process::Command::new(python)
        .arg(root.join("tests/iroh_conformance/control_call_e2e_client.py"))
        .arg(env!("CARGO_BIN_EXE_mycelium-iroh-sidecar"))
        .arg(serde_json::to_string(&phone.addr()).unwrap())
        .env("PYTHONPATH", root)
        .env_remove("PYTHONHOME")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    // Join the remote cleanup witness BEFORE the helper tears down the
    // endpoint. A successful RPC does not imply its close datagram has left.
    let witness = timeout(Duration::from_secs(25), &mut responder).await;
    let mut input = child.stdin.take().unwrap();
    if matches!(&witness, Ok(Ok(_))) {
        input.write_all(b"d").await.unwrap();
    }
    drop(input); // EOF also lets a failed witness reach helper cleanup.
    let result = timeout(Duration::from_secs(10), child.wait_with_output())
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
    let identities = witness.expect("remote cleanup witness deadline").unwrap();
    assert_eq!(report["calls"], 2);
    for identity in identities {
        assert_eq!(identity, report["controller_endpoint_id"].as_str().unwrap());
    }
    phone.close().await;
}
