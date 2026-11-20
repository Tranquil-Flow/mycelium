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
        // Both ends are on this Mac: a validated private/loopback path is preferred over the
        // relay (iroh_cross::RelayFirst; public IP paths never are, see its unit test).
    }
    client.close(&clock, deadline(&clock, 5)).unwrap();
    server.close(&clock, deadline(&clock, 5)).unwrap();
}

/// Lane XC root cause, on one Mac: a SECOND endpoint bound with the same key registers at the
/// relay after the first; the relay keeps only the newest connection per key active
/// (`iroh-relay-1.0.2/src/server/clients.rs:84-98`), so the server's reply to the first one,
/// which it sends through the relay, is delivered to the second and lost. This is the packaged
/// owner on hardware (rv52f: two owner control endpoints `id=3cda344136`, one per joined Mac).
#[test]
#[ignore = "network: registers at relay.mycelium.now"]
fn probe_a_second_endpoint_with_the_same_key_steals_the_relayed_reply() {
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
    server.relay_ready(&clock, deadline(&clock, 15)).ok();
    client.relay_ready(&clock, deadline(&clock, 15)).ok();
    // The same key, bound second: now the relay's active client for that key.
    let mut twin = bind(&client_key, iface);
    twin.relay_ready(&clock, deadline(&clock, 15)).ok();
    std::thread::sleep(Duration::from_secs(1));
    let signed = EndpointAddr::from_parts(
        server_key.public(),
        [TransportAddr::Ip(SocketAddr::new(
            "192.0.2.1".parse().unwrap(),
            9,
        ))],
    );
    let reply = std::thread::scope(|scope| {
        scope.spawn(|| {
            if let Ok(c) = server.accept(&clock, deadline(&clock, 30)) {
                rt.block_on(async {
                    if let Ok((mut send, mut recv)) = c.accept_bi().await {
                        let _cmd = recv.read_to_end(1 << 16).await;
                        send.write_all(&[b'r'; 2_500]).await.ok();
                        send.finish().ok();
                        tokio::time::timeout(Duration::from_secs(4), c.closed())
                            .await
                            .ok();
                    }
                });
            }
        });
        match client.connect_channel(&signed, Channel::NodeControl, &clock, deadline(&clock, 20)) {
            Err(e) => format!("connect {e:?}"),
            Ok(c) => {
                let r = rt.block_on(async {
                    let (mut send, mut recv) = c.open_bi().await.unwrap();
                    send.write_all(&[b'c'; 900]).await.unwrap();
                    send.finish().unwrap();
                    tokio::time::timeout(Duration::from_secs(10), recv.read_to_end(1 << 16)).await
                });
                c.close(0u32.into(), b"done");
                format!("{:?}", r.map(|r| r.map(|b| b.len())))
            }
        }
    });
    println!("with a same-key twin at the relay: {reply}");
    twin.close(&clock, deadline(&clock, 5)).unwrap();
    client.close(&clock, deadline(&clock, 5)).unwrap();
    server.close(&clock, deadline(&clock, 5)).unwrap();
    assert_ne!(
        reply, "Ok(Ok(2500))",
        "expected the twin to steal the relayed reply"
    );
}

/// Lane XC rv52h: the owner Mac's OWN member fetches its slice from the owner's OWN source, both
/// across networks on this Mac's interface. At the source's signed (real, private) address the
/// connection must carry the bytes on that direct path, never hairpinned through the relay (on
/// hardware 872 MiB took 339 s over the relay and the run's 600 s fetch wall ran out).
#[test]
#[ignore = "network: registers at relay.mycelium.now"]
fn probe_a_same_mac_peer_at_its_private_address_is_served_directly() {
    let relay: iroh::RelayUrl = MYCELIUM_RELAY_URL.parse().unwrap();
    let iface: IpAddr = std::env::var("PROBE_IFACE")
        .expect("PROBE_IFACE")
        .parse()
        .unwrap();
    assert!(
        runtime::iroh_host::private_lan(iface),
        "PROBE_IFACE must be a private address"
    );
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let clock = Mono(Instant::now());
    let (server_key, client_key) = (SecretKey::generate(), SecretKey::generate());
    let bind = |key: &SecretKey| {
        LoopbackHost::bind_lan(
            &rt,
            &clock,
            deadline(&clock, 15),
            key.clone(),
            &[Channel::NodeControl],
            iface,
            LanPolicy::private_only().across_networks(relay.clone()),
        )
        .expect("bind across networks")
    };
    let mut server = bind(&server_key);
    let mut client = bind(&client_key);
    server.relay_ready(&clock, deadline(&clock, 15)).unwrap();
    client.relay_ready(&clock, deadline(&clock, 15)).unwrap();
    let signed: Vec<SocketAddr> = server.address().unwrap().ip_addrs().copied().collect();
    assert!(
        signed.iter().any(|a| a.ip() == iface),
        "the source signs its bound address"
    );
    let dial = signed
        .iter()
        .fold(EndpointAddr::new(server_key.public()), |a, s| {
            a.with_ip_addr(*s)
        });
    const BYTES: usize = 64 << 20;
    let (elapsed, report, selected) = std::thread::scope(|scope| {
        scope.spawn(|| {
            let c = server.accept(&clock, deadline(&clock, 30)).expect("accept");
            rt.block_on(async {
                let (mut send, mut recv) = c.accept_bi().await.unwrap();
                recv.read_to_end(1 << 10).await.unwrap();
                send.write_all(&vec![b'r'; BYTES]).await.unwrap();
                send.finish().unwrap();
                tokio::time::timeout(Duration::from_secs(10), c.closed())
                    .await
                    .ok();
            });
        });
        let c = client
            .connect_channel(&dial, Channel::NodeControl, &clock, deadline(&clock, 20))
            .expect("connect");
        let began = Instant::now();
        let got = rt.block_on(async {
            let (mut send, mut recv) = c.open_bi().await.unwrap();
            send.write_all(b"go").await.unwrap();
            send.finish().unwrap();
            tokio::time::timeout(Duration::from_secs(120), recv.read_to_end(BYTES + 1)).await
        });
        let elapsed = began.elapsed();
        assert_eq!(got.unwrap().unwrap().len(), BYTES);
        let out = (
            elapsed,
            runtime::iroh_host::path_report(&c),
            runtime::iroh_host::selected_path(&c),
        );
        c.close(0u32.into(), b"done");
        out
    });
    println!(
        "same-Mac 64 MiB at the signed private address: {elapsed:?} selected={selected:?} {report}"
    );
    client.close(&clock, deadline(&clock, 5)).unwrap();
    server.close(&clock, deadline(&clock, 5)).unwrap();
    assert_eq!(
        selected,
        Some("direct"),
        "the bytes went through the relay: {report}"
    );
    assert!(
        elapsed < Duration::from_secs(10),
        "64 MiB on one Mac took {elapsed:?}"
    );
}
