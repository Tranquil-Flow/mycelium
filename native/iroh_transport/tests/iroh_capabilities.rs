// SPDX-License-Identifier: AGPL-3.0-or-later

use std::net::{Ipv4Addr, SocketAddrV4};

use iroh::{
    Endpoint,
    endpoint::{VarInt, presets},
};
use mycelium_iroh_transport::sidecar::IROH_ALPN;

#[tokio::test]
async fn pinned_iroh_proves_identity_alpn_bidi_and_reset() {
    let server = Endpoint::builder(presets::N0DisableRelay)
        .alpns(vec![IROH_ALPN.to_vec()])
        .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
        .expect("configure server bind")
        .bind()
        .await
        .expect("bind server");
    let client = Endpoint::builder(presets::N0DisableRelay)
        .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
        .expect("configure client bind")
        .bind()
        .await
        .expect("bind client");

    let expected_client = client.id();
    let accept = tokio::spawn({
        let server = server.clone();
        async move {
            let incoming = server.accept().await.expect("incoming connection");
            let connection = incoming.await.expect("accept connection");
            assert_eq!(connection.remote_id(), expected_client);

            let (mut send, mut recv) = connection.accept_bi().await.expect("accept bidi");
            let request = recv.read_to_end(64).await.expect("read request");
            assert_eq!(request, b"phase-7");
            send.write_all(b"ack").await.expect("write ack");
            send.finish().expect("finish ack");

            let (_send, mut recv) = connection.accept_bi().await.expect("accept reset stream");
            assert!(recv.read_to_end(64).await.is_err());
        }
    });

    let connection = client
        .connect(server.addr(), IROH_ALPN)
        .await
        .expect("connect with pinned endpoint address and custom ALPN");
    assert_eq!(connection.remote_id(), server.id());

    let (mut send, mut recv) = connection.open_bi().await.expect("open bidi");
    send.write_all(b"phase-7").await.expect("write request");
    send.finish().expect("finish request");
    assert_eq!(recv.read_to_end(64).await.expect("read ack"), b"ack");

    let (mut send, mut recv) = connection.open_bi().await.expect("open reset stream");
    send.write_all(b"cancelled")
        .await
        .expect("write before reset");
    send.reset(VarInt::from_u32(7)).expect("reset send stream");
    recv.stop(VarInt::from_u32(7)).expect("stop receive stream");

    accept.await.expect("join accept task");
    client.close().await;
    server.close().await;
}

#[test]
fn cargo_manifest_pins_exact_stable_iroh_release() {
    let manifest = include_str!("../Cargo.toml");
    assert!(manifest.contains("iroh = \"=1.0.2\""));
}
