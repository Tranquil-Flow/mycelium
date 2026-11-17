// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB4 (AB-PLAN B2 item 2; ruling 29: "paths are per-peer, not per-connection"): a single-use
//! epoch opens a FRESH connection to the same peer. iroh keeps a peer's paths in one actor per
//! endpoint id (`remote_state.rs`, idle for `ACTOR_MAX_IDLE_TIMEOUT` = 60 s before it stops), so
//! a second epoch on the SAME endpoint should find the path the first one established, with no
//! rendezvous and no new punch. Characterised here offline: the second dial names no address at
//! all, only the endpoint id and a relay nothing listens on, so it can only succeed by reusing
//! the first epoch's path. The cost of a re-punch on a real network (the relay round trip plus
//! the probe rounds) only the hotspot run measures.
use super::{LanPolicy, LoopbackHost, NetworkMode, selected_path};
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{EndpointAddr, SecretKey};
use std::{
    net::{IpAddr, Ipv4Addr},
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
        panic!("the carrier must not consult wall time")
    }
}
const LOOPBACK: IpAddr = IpAddr::V4(Ipv4Addr::LOCALHOST);
fn deadline(clock: &Mono, id: B) -> MonoInstant {
    clock
        .monotonic()
        .checked_add(id.span(Entries::ZERO).unwrap())
        .unwrap()
}

#[test]
fn a_second_epoch_to_the_same_peer_reuses_the_established_path_without_a_rendezvous() {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let clock = Mono(Instant::now());
    let relay: iroh::RelayUrl = "http://127.0.0.1:1".parse().unwrap();
    let policy = LanPolicy::private_only().across_networks(relay.clone());
    let (server_key, client_key) = (SecretKey::generate(), SecretKey::generate());
    let bind = |key: &SecretKey| {
        LoopbackHost::bind_lan(
            &rt,
            &clock,
            deadline(&clock, B::RouterClose),
            key.clone(),
            &[Channel::Router],
            LOOPBACK,
            policy.clone(),
        )
        .unwrap()
    };
    let mut server = bind(&server_key);
    let mut client = bind(&client_key);
    assert_eq!(client.mode(), NetworkMode::CrossNetwork);
    server.admit_peer_ids(&[client_key.public()]).unwrap();
    let address = server.address().unwrap();
    let signed = EndpointAddr::from_parts(
        address.id,
        address.ip_addrs().map(|s| iroh::TransportAddr::Ip(*s)),
    );
    let socket = *signed.ip_addrs().next().unwrap();
    let handshake = Duration::from_millis(
        B::SidecarLocalHandshake
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    );
    // Epoch 1: dialled at the peer's signed address (across real networks: through the relay,
    // then a punched path).
    let began = Instant::now();
    let (first, first_ms, served) = std::thread::scope(|scope| {
        let accepted = scope.spawn(|| {
            server
                .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
                .unwrap()
        });
        let c = client
            .connect(&signed, &clock, deadline(&clock, B::SidecarLocalHandshake))
            .unwrap();
        let ms = began.elapsed().as_millis();
        (c, ms, accepted.join().unwrap())
    });
    assert_eq!(
        selected_path(&first),
        Some("direct"),
        "epoch 1 runs on the IP path"
    );
    drop(served);
    // The single-use epoch ends: its connection closes (the endpoint and its peer state stay).
    first.close(0u32.into(), b"epoch_done");
    rt.block_on(first.closed());
    drop(first);
    // The epoch boundary: a long-lived host recycles there (B11, `recycle_lan`, so iroh's
    // never-evicted address map stays bounded on the LAN). Across networks that must keep the
    // endpoint, or the next epoch re-punches from scratch.
    for host in [&mut client, &mut server] {
        host.recycle_lan(&clock, deadline(&clock, B::RouterClose))
            .expect("across networks an epoch boundary keeps the endpoint and its paths");
    }
    // Epoch 2: NO address, only the id and the dead relay. Only a reused path can carry it.
    let bare = EndpointAddr::new(server_key.public()).with_relay_url(relay);
    let began = Instant::now();
    let (second, second_ms, served) = std::thread::scope(|scope| {
        let accepted = scope.spawn(|| {
            server
                .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
                .unwrap()
        });
        let endpoint = client.endpoint().unwrap().clone();
        let c = rt
            .block_on(async {
                tokio::time::timeout(handshake, endpoint.connect(bare, Channel::Router.alpn()))
                    .await
            })
            .expect("epoch 2 completes inside one handshake budget")
            .expect("epoch 2 reuses the established path (no rendezvous was possible)");
        let ms = began.elapsed().as_millis();
        (c, ms, accepted.join().unwrap())
    });
    assert_eq!(selected_path(&second), Some("direct"));
    let remote: Vec<_> = second
        .paths()
        .iter()
        .filter(|p| p.is_selected())
        .map(|p| p.remote_addr().clone())
        .collect();
    assert_eq!(
        remote,
        vec![iroh::TransportAddr::Ip(socket)],
        "the same path as epoch 1"
    );
    eprintln!("xb4_path_reuse epoch1_ms={first_ms} epoch2_ms={second_ms} path={socket}");
    second.close(0u32.into(), b"done");
    drop((second, served));
    client
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    server
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
}
