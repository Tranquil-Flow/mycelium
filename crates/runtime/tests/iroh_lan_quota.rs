// SPDX-License-Identifier: AGPL-3.0-only
//! Per-host share of the direct-LAN provisional table (lane B11,
//! REVIEW-laneB9f (1)).
//!
//! The receive filter admits an unpinned source only with a QUIC Initial,
//! into a table of `RelayConnectionSlots` entries held for one
//! `SidecarLocalHandshake` each. Keyed by socket address alone, ONE LAN host
//! sending Initial-shaped datagrams from many ports could hold every slot for a
//! whole handshake budget, i.e. for the whole dial deadline of a real peer.
//! The flooding host here never answers: an Initial-shaped datagram is all a
//! host needs to send to occupy a slot. Real interfaces of this host only:
//! the server and the real peer on `MYCELIUM_LAN_IP`, the flooding host on
//! 127.0.0.1 (explicitly allowed, so it is an in-range host).
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::SecretKey;
use runtime::iroh_host::{LanPolicy, LoopbackHost, private_lan};
use std::{
    net::{IpAddr, SocketAddr, UdpSocket},
    time::Instant,
};
use transport::{Channel, Clock, Error};

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            ClockDomain(2),
            u64::try_from(self.0.elapsed().as_millis()).unwrap_or(u64::MAX),
        )
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the LAN carrier must not consult wall time")
    }
}
fn ip(text: &str) -> IpAddr {
    text.parse().unwrap()
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap()
}
fn count(id: B) -> usize {
    usize::try_from(id.count().unwrap().get()).unwrap()
}
fn deadline(clock: &Mono, id: B) -> MonoInstant {
    clock
        .monotonic()
        .checked_add(id.span(Entries::ZERO).unwrap())
        .unwrap()
}
fn lan_ip() -> IpAddr {
    let value = std::env::var("MYCELIUM_LAN_IP").expect("MYCELIUM_LAN_IP = this host's LAN IP");
    let address = ip(&value);
    assert!(
        private_lan(address),
        "{address} is not a private LAN address"
    );
    address
}
fn host<'a>(
    rt: &'a tokio::runtime::Runtime,
    clock: &Mono,
    interface: IpAddr,
    policy: LanPolicy,
) -> LoopbackHost<'a> {
    LoopbackHost::bind_lan(
        rt,
        clock,
        deadline(clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        interface,
        policy,
    )
    .unwrap()
}
/// What a QUIC v1 client's first datagram looks like on the wire: a
/// long-header Initial (RFC 9000 17.2.2), padded to the 1200-byte minimum.
fn initial_like() -> Vec<u8> {
    let mut datagram = vec![0u8; 1200];
    datagram[0] = 0xc0;
    datagram[1..5].copy_from_slice(&1u32.to_be_bytes());
    datagram
}

/// One in-range host sends Initials from four tables' worth of ports right
/// before a real peer on another host dials. The real peer, whose key the
/// server admitted from signed data (as `joined_shard::serve` does), must
/// complete its dial within ONE handshake budget: the flooding host holds at
/// most its share of the slots, for that whole budget.
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_a_peer_is_admitted_within_its_dial_deadline_while_one_host_floods() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let lan = lan_ip();
    let flooder = ip("127.0.0.1");
    let mut server = host(
        &rt,
        &clock,
        lan,
        LanPolicy::with_explicit(vec![flooder]).unwrap(),
    );
    let addr = server.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let mut peer = host(&rt, &clock, lan, LanPolicy::private_only());
    let peer_id = peer.address().unwrap().id;
    server.admit_peer_ids(&[peer_id]).unwrap();
    let initial = initial_like();
    let ports = 4 * count(B::RelayConnectionSlots);
    let flood: Vec<UdpSocket> = (0..ports)
        .map(|_| {
            let socket = UdpSocket::bind(SocketAddr::new(flooder, 0)).unwrap();
            socket.send_to(&initial, target).unwrap();
            socket
        })
        .collect();
    let (dialed, accepted) = std::thread::scope(|scope| {
        let accepting = scope.spawn(|| {
            let window = deadline(&clock, B::RouterClose);
            loop {
                match server.accept(&clock, window) {
                    Err(Error::Authentication) => continue,
                    other => return other,
                }
            }
        });
        let began = Instant::now();
        let dialed = peer.connect(&addr, &clock, deadline(&clock, B::SidecarLocalHandshake));
        let waited = began.elapsed();
        eprintln!(
            "peer dial after a {ports}-port flood: {:?} in {waited:?}",
            dialed.as_ref().map(|c| c.remote_id()).map_err(|e| *e)
        );
        (dialed, accepting.join().unwrap())
    });
    let dropped = server.lan_dropped_datagrams().unwrap();
    eprintln!("receive filter dropped {dropped} datagrams");
    drop(flood);
    let outcome = dialed.as_ref().map(|c| c.remote_id()).map_err(|e| *e);
    let accepted_id = accepted.as_ref().map(|c| c.remote_id()).map_err(|e| *e);
    for connection in dialed.iter().chain(accepted.iter()) {
        connection.close(0u32.into(), b"done");
    }
    for h in [&mut peer, &mut server] {
        h.close(&clock, deadline(&clock, B::RouterClose)).unwrap();
    }
    assert!(
        dropped > 0,
        "the flood never met a full share (nothing was dropped)"
    );
    assert_eq!(
        outcome,
        Ok(addr.id),
        "the real peer's dial within one handshake budget"
    );
    assert_eq!(accepted_id, Ok(peer_id), "the server accepted the peer");
}
