// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XC probe (NETWORK, ignored): the rv52 hotspot fault on one Mac. Two CROSS-NETWORK hosts
//! (`LanPolicy::across_networks`, Mycelium's relay) on different local addresses: the server on
//! 127.0.0.1, the client on this Mac's LAN address (`PROBE_IFACE`). The client dials the server
//! at an UNREACHABLE signed address (TEST-NET-1) plus its own relay, so the connection starts on
//! the relay exactly as on hardware; hole punching may then open direct paths on its own. The
//! exchange mirrors a member-control call: a small command, a signed-reply-sized answer, the
//! server waiting `RouterClose` for the client's close. Run:
//! `PROBE_IFACE=$(ipconfig getifaddr en0) cargo test -p runtime --features iroh-adapter --test iroh_cross_relay_probe -- --ignored --nocapture`
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{ClockDomain, MonoInstant};
use iroh::{EndpointAddr, SecretKey, TransportAddr};
use runtime::iroh_host::{LanPolicy, LoopbackHost, MYCELIUM_RELAY_URL};
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr},
    time::{Duration, Instant},
};
use transport::{Channel, Clock};

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            ClockDomain(2),
            u64::try_from(self.0.elapsed().as_millis()).unwrap_or(u64::MAX),
        )
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the probe must not consult wall time")
    }
}
fn deadline(clock: &Mono, seconds: u64) -> MonoInstant {
    clock
        .monotonic()
        .checked_add(budgets::Span::from_millis(seconds * 1000))
        .unwrap()
}

#[test]
#[ignore = "network: registers at relay.mycelium.now"]
fn probe_a_cross_network_reply_returns_over_a_relay_started_connection() {
    let relay: iroh::RelayUrl = MYCELIUM_RELAY_URL.parse().unwrap();
    let iface: IpAddr = std::env::var("PROBE_IFACE")
        .expect("PROBE_IFACE")
        .parse()
        .unwrap();
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let clock = Mono(Instant::now());
    let policy = || LanPolicy::private_only().across_networks(relay.clone());
    let (server_key, client_key) = (SecretKey::generate(), SecretKey::generate());
    let bind = |key: &SecretKey, ip: IpAddr| {
        LoopbackHost::bind_lan(
            &rt,
            &clock,
            deadline(&clock, 15),
            key.clone(),
            &[Channel::NodeControl],
            ip,
            policy(),
        )
        .expect("bind across networks")
    };
    let mut server = bind(&server_key, IpAddr::V4(Ipv4Addr::LOCALHOST));
    let mut client = bind(&client_key, iface);
    server.admit_peer_ids(&[client_key.public()]).unwrap();
    client.admit_peer_ids(&[server_key.public()]).unwrap();
    let online = Instant::now();
    server.relay_ready(&clock, deadline(&clock, 15)).ok();
    client.relay_ready(&clock, deadline(&clock, 15)).ok();
    println!("online_ms={}", online.elapsed().as_millis());
    // Signed address: unreachable (TEST-NET-1), so only the relay starts the connection.
    let signed = EndpointAddr::from_parts(
        server_key.public(),
        [TransportAddr::Ip(SocketAddr::new(
            "192.0.2.1".parse().unwrap(),
            9,
        ))],
    );
    for round in 0..3 {
        let began = Instant::now();
        let outcome = std::thread::scope(|scope| {
            let served = scope.spawn(|| {
                let c = server.accept(&clock, deadline(&clock, 30)).expect("accept");
                rt.block_on(async {
                    let (mut send, mut recv) = c.accept_bi().await.unwrap();
                    let cmd = recv.read_to_end(1 << 16).await.unwrap();
                    send.write_all(&vec![b'r'; 2_500]).await.unwrap();
                    send.finish().unwrap();
                    let closed = tokio::time::timeout(Duration::from_secs(4), c.closed())
                        .await
                        .is_ok();
                    (cmd.len(), closed, runtime::iroh_host::selected_path(&c))
                })
            });
            let c = client
                .connect_channel(&signed, Channel::NodeControl, &clock, deadline(&clock, 30))
                .expect("connect over the relay");
            let connect_ms = began.elapsed().as_millis();
            let reply = rt.block_on(async {
                let (mut send, mut recv) = c.open_bi().await.unwrap();
                send.write_all(&[b'c'; 900]).await.unwrap();
                send.finish().unwrap();
                tokio::time::timeout(Duration::from_secs(20), recv.read_to_end(1 << 16)).await
            });
            let path = runtime::iroh_host::selected_path(&c);
            c.close(0u32.into(), b"done");
            let (cmd, closed_by_peer, server_path) = served.join().unwrap();
            (
                connect_ms,
                reply.map(|r| r.map(|b| b.len())),
                path,
                cmd,
                closed_by_peer,
                server_path,
            )
        });
        println!(
            "round {round}: connect_ms={} reply={:?} client_path={:?} server_got={} closed_by_peer={} server_path={:?} total_ms={}",
            outcome.0,
            outcome.1,
            outcome.2,
            outcome.3,
            outcome.4,
            outcome.5,
            began.elapsed().as_millis()
        );
        assert!(
            matches!(outcome.1, Ok(Ok(2_500))),
            "round {round}: the reply did not come back"
        );
    }
    client.close(&clock, deadline(&clock, 5)).unwrap();
    server.close(&clock, deadline(&clock, 5)).unwrap();
}
