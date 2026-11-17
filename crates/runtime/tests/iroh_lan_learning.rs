// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticated host learning on the member side (lane B14, owner ruling 12
//! option A, journey gap row 20).
//!
//! The member cannot pin the route owner's host from signed data (the owner's
//! address is in no signed document), so before B14 the owner's every new
//! socket waited in the receive filter's provisional table with everybody
//! else, and a source at the owner's IP needed only its B11 share (4 entries
//! per handshake budget) to starve it. Now the member learns the host of an
//! ADMITTED peer key only after that key was verified on a completed
//! handshake; that host's Initials then have their own lane, and the waiting
//! room for every other source shrinks. Real handshakes only; 127.0.0.1 is
//! explicitly allowed and the real-LAN cases use this host's own interface.
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{Endpoint, EndpointAddr, RelayMode, SecretKey, endpoint::presets};
use runtime::iroh_host::{LanPolicy, LoopbackHost, private_lan};
use std::{
    net::{IpAddr, SocketAddr, UdpSocket},
    time::{Duration, Instant},
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
fn loopback() -> IpAddr {
    ip("127.0.0.1")
}
fn runtime(workers: usize) -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers)
        .enable_all()
        .build()
        .unwrap()
}
fn count(id: B) -> usize {
    usize::try_from(id.count().unwrap().get()).unwrap()
}
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
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
/// 127.0.0.1 explicitly allowed (an in-range host on every machine).
fn policy() -> LanPolicy {
    LanPolicy::with_explicit(vec![loopback()]).unwrap()
}
fn host<'a>(
    rt: &'a tokio::runtime::Runtime,
    clock: &Mono,
    interface: IpAddr,
    secret: SecretKey,
) -> LoopbackHost<'a> {
    LoopbackHost::bind_lan(
        rt,
        clock,
        deadline(clock, B::RouterClose),
        secret,
        &[Channel::Router],
        interface,
        policy(),
    )
    .unwrap()
}
/// One B11 per-host share of the provisional table.
fn share() -> usize {
    count(B::RelayConnectionSlots) / 4
}
/// What a QUIC v1 client's first datagram looks like on the wire: a
/// long-header Initial (RFC 9000 17.2.2), padded to the 1200-byte minimum. A
/// host that can only spoof a source address can send exactly this.
fn initial_like() -> Vec<u8> {
    let mut datagram = vec![0u8; 1200];
    datagram[0] = 0xc0;
    datagram[1..5].copy_from_slice(&1u32.to_be_bytes());
    datagram
}
/// One Initial-shaped datagram to `target` from each of `ports` fresh sockets
/// on `from`; the sockets are kept so no port is reused while they live.
fn bare_initials(from: IpAddr, ports: usize, target: SocketAddr) -> Vec<UdpSocket> {
    let initial = initial_like();
    (0..ports)
        .map(|_| {
            let socket = UdpSocket::bind(SocketAddr::new(from, 0)).unwrap();
            socket.send_to(&initial, target).unwrap();
            socket
        })
        .collect()
}
/// Plain iroh endpoints under throwaway keys on fresh sockets at `at`, each
/// dialing `target` once and holding whatever it gets: a host at the owner's
/// IP that completes real handshakes under the wrong key.
struct WrongKeys {
    endpoints: Vec<Endpoint>,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}
impl WrongKeys {
    fn start(rt: &tokio::runtime::Runtime, at: IpAddr, target: &EndpointAddr, n: usize) -> Self {
        let endpoints: Vec<Endpoint> = rt.block_on(async {
            let mut endpoints = Vec::with_capacity(n);
            for _ in 0..n {
                endpoints.push(
                    Endpoint::builder(presets::Minimal)
                        .portmapper_config(iroh::endpoint::PortmapperConfig::Disabled)
                        .relay_mode(RelayMode::Disabled)
                        .clear_address_lookup()
                        .clear_ip_transports()
                        .bind_addr(SocketAddr::new(at, 0))
                        .unwrap()
                        .alpns(vec![Channel::Router.alpn().to_vec()])
                        .bind()
                        .await
                        .unwrap(),
                );
            }
            endpoints
        });
        let tasks = endpoints
            .iter()
            .map(|endpoint| {
                let (endpoint, target) = (endpoint.clone(), target.clone());
                rt.spawn(async move {
                    if let Ok(connection) = endpoint.connect(target, Channel::Router.alpn()).await {
                        let _reason = connection.closed().await;
                    }
                })
            })
            .collect();
        Self { endpoints, tasks }
    }
    fn stop(self, rt: &tokio::runtime::Runtime) {
        for task in &self.tasks {
            task.abort();
        }
        let wait = span(B::RouterClose);
        rt.block_on(async {
            let mut closing = tokio::task::JoinSet::new();
            for endpoint in self.endpoints {
                closing.spawn(async move { endpoint.close().await });
            }
            if tokio::time::timeout(wait, closing.join_all())
                .await
                .is_err()
            {
                eprintln!("wrong-key endpoints did not close within RouterClose");
            }
        });
    }
}
/// The member accepts until the `owner` key's connection arrives (refused and
/// foreign handshakes are skipped, as `joined_shard::serve` does) or `window`
/// ends. Returns the owner's connection and how many inbounds were refused.
fn accept_owner(
    member: &LoopbackHost<'_>,
    clock: &Mono,
    owner: iroh::EndpointId,
    window: MonoInstant,
) -> (Result<iroh::endpoint::Connection, Error>, usize) {
    let mut refused = 0;
    loop {
        match member.accept(clock, window) {
            Err(Error::Authentication) => refused += 1,
            Ok(c) if c.remote_id() != owner => {
                refused += 1;
                c.close(2u32.into(), b"unpinned route owner");
            }
            other => return (other, refused),
        }
    }
}
/// One owner connection from a FRESH owner socket at `at` (the owner binds a
/// new endpoint per route epoch, `m16_route.rs`), dialed with one handshake
/// budget as its deadline. Returns the dial outcome, its latency, the member's
/// accepted key and the owner's socket.
fn owner_epoch(
    rt: &tokio::runtime::Runtime,
    clock: &Mono,
    member: &LoopbackHost<'_>,
    at: IpAddr,
    owner_key: &SecretKey,
) -> (
    Result<iroh::EndpointId, Error>,
    Duration,
    Result<iroh::EndpointId, Error>,
    SocketAddr,
) {
    let mut owner = host(rt, clock, at, owner_key.clone());
    let socket = *owner.address().unwrap().ip_addrs().next().unwrap();
    let target = member.address().unwrap();
    let (dialed, waited, accepted) = std::thread::scope(|scope| {
        let accepting = scope.spawn(|| {
            accept_owner(
                member,
                clock,
                owner_key.public(),
                deadline(clock, B::RouterClose),
            )
            .0
        });
        let began = Instant::now();
        let dialed = owner.connect(&target, clock, deadline(clock, B::SidecarLocalHandshake));
        let waited = began.elapsed();
        (dialed, waited, accepting.join().unwrap())
    });
    let outcome = dialed.as_ref().map(|c| c.remote_id()).map_err(|e| *e);
    let accepted_id = accepted.as_ref().map(|c| c.remote_id()).map_err(|e| *e);
    for connection in dialed.iter().chain(accepted.iter()) {
        connection.close(0u32.into(), b"epoch done");
    }
    owner.close(clock, deadline(clock, B::RouterClose)).unwrap();
    (outcome, waited, accepted_id, socket)
}
fn learned_ips(member: &LoopbackHost<'_>) -> Vec<IpAddr> {
    member
        .lan_learned_hosts()
        .unwrap()
        .iter()
        .map(SocketAddr::ip)
        .collect()
}

/// Ruling 12 / test (2): learning happens strictly after the key check. Bare
/// Initials (a spoofed source), and a completed handshake under the wrong key
/// from the owner's very IP, teach the member nothing; the admitted owner key's
/// verified handshake teaches it exactly the socket that handshake came from,
/// and a later wrong key at the same IP does not replace it.
#[test]
fn only_a_key_verified_accept_teaches_the_member_its_peers_host() {
    let rt = runtime(1);
    let peers = runtime(2);
    let clock = Mono(Instant::now());
    let mut member = host(&rt, &clock, loopback(), SecretKey::generate());
    let addr = member.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let owner_key = SecretKey::generate();
    member.admit_peer_ids(&[owner_key.public()]).unwrap();
    // A spoofed source: an Initial-shaped datagram, never a handshake (one, so
    // the wrong key below still finds room in 127.0.0.1's B11 share).
    let spoofed = bare_initials(loopback(), 1, target);
    let quiet = member.accept(&clock, deadline(&clock, B::SidecarReceivePollWait));
    let after_spoof = member.lan_learned_hosts().unwrap();
    // The owner's IP, the wrong key, a completed handshake.
    let wrong = WrongKeys::start(&peers, loopback(), &addr, 1);
    let refused = member.accept(&clock, deadline(&clock, B::SidecarLocalHandshake));
    let refused = refused.map(|c| c.remote_id());
    let after_wrong_key = member.lan_learned_hosts().unwrap();
    wrong.stop(&peers);
    let (dialed, _, accepted, owner_socket) =
        owner_epoch(&rt, &clock, &member, loopback(), &owner_key);
    let after_owner = member.lan_learned_hosts().unwrap();
    let wrong = WrongKeys::start(&peers, loopback(), &addr, 1);
    let refused_again = member
        .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
        .map(|c| c.remote_id());
    let after_second_wrong_key = member.lan_learned_hosts().unwrap();
    wrong.stop(&peers);
    drop(spoofed);
    member
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    eprintln!(
        "spoof accept {:?}; learned after spoof {after_spoof:?}, after wrong key \
         {after_wrong_key:?}, after owner {after_owner:?} (owner {owner_socket}), after a \
         second wrong key {after_second_wrong_key:?}",
        quiet.map(|c| c.remote_id())
    );
    assert!(after_spoof.is_empty(), "a bare Initial taught a host");
    assert_eq!(refused, Err(Error::Authentication), "the wrong key");
    assert!(
        after_wrong_key.is_empty(),
        "a refused key taught its host: {after_wrong_key:?}"
    );
    assert_eq!(dialed, Ok(addr.id), "the owner's dial");
    assert_eq!(accepted, Ok(owner_key.public()), "the owner was accepted");
    assert_eq!(
        after_owner,
        vec![owner_socket],
        "the verified owner handshake must teach exactly its source"
    );
    assert_eq!(refused_again, Err(Error::Authentication));
    assert_eq!(
        after_second_wrong_key,
        vec![owner_socket],
        "a wrong key at the owner's IP replaced the learned host"
    );
}

/// Ruling 12 / tests (1) and (4) on loopback: the B11 per-epoch recycle KEEPS
/// the learned host (no re-learning needed), and the owner's next epoch, from
/// a fresh socket at its learned IP, is admitted at its first Initial while
/// the same IP sends bare Initials from twice a B11 share of ports and
/// completes handshakes under the wrong key. Before B14 those entries filled
/// the IP's share and the owner's dial expired.
#[test]
fn a_learned_owner_host_survives_the_recycle_and_its_next_epoch_is_not_starved() {
    let rt = runtime(1);
    let peers = runtime(2);
    let clock = Mono(Instant::now());
    let mut member = host(&rt, &clock, loopback(), SecretKey::generate());
    let addr = member.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let owner_key = SecretKey::generate();
    member.admit_peer_ids(&[owner_key.public()]).unwrap();
    let (first, before, _, first_socket) =
        owner_epoch(&rt, &clock, &member, loopback(), &owner_key);
    let learned_first = member.lan_learned_hosts().unwrap();
    member
        .recycle_lan(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    // `joined_shard::serve` admits the verified route's owner key per epoch.
    member.admit_peer_ids(&[owner_key.public()]).unwrap();
    let kept = member.lan_learned_hosts().unwrap();
    let wrong = WrongKeys::start(&peers, loopback(), &addr, share());
    let spoofed = bare_initials(loopback(), 2 * share(), target);
    let (next, after, accepted, next_socket) =
        owner_epoch(&rt, &clock, &member, loopback(), &owner_key);
    let relearned = member.lan_learned_hosts().unwrap();
    let dropped = member.lan_dropped_datagrams().unwrap();
    wrong.stop(&peers);
    drop(spoofed);
    member
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    eprintln!(
        "owner dial before {before:?} ({first:?}); next epoch under the flood {after:?} \
         ({next:?}); learned {learned_first:?} -> kept {kept:?} -> {relearned:?}; \
         filter dropped {dropped}"
    );
    assert_eq!(first, Ok(addr.id));
    assert_eq!(learned_first, vec![first_socket]);
    assert_eq!(
        kept,
        vec![first_socket],
        "the per-epoch recycle must keep the learned host"
    );
    assert_eq!(
        next,
        Ok(addr.id),
        "the owner's next-epoch dial within one handshake budget"
    );
    assert_eq!(accepted, Ok(owner_key.public()));
    assert_eq!(
        relearned,
        vec![next_socket],
        "the owner's newest verified socket"
    );
}

/// Ruling 12 / test (1) on this host's real interface: after one authenticated
/// owner connection, another host floods from four tables' worth of ports, the
/// owner's own IP sends bare Initials from twice a B11 share of ports, and
/// hosts at the owner's IP complete handshakes under the wrong key. The
/// owner's next connection (a fresh socket, as each route epoch is) must be
/// admitted at its FIRST Initial: a dropped Initial costs at least one QUIC
/// PTO (noq initial RTT 333 ms, so about 1 s), so its latency stays under one
/// `OwnerPoll` like the unflooded one.
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_floods_after_an_authenticated_connection_do_not_delay_the_owners_next_one() {
    let rt = runtime(1);
    let peers = runtime(2);
    let clock = Mono(Instant::now());
    let lan = lan_ip();
    let mut member = host(&rt, &clock, lan, SecretKey::generate());
    let addr = member.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let owner_key = SecretKey::generate();
    member.admit_peer_ids(&[owner_key.public()]).unwrap();
    let (first, before, _, _) = owner_epoch(&rt, &clock, &member, lan, &owner_key);
    let learned = learned_ips(&member);
    let other_host = bare_initials(loopback(), 4 * count(B::RelayConnectionSlots), target);
    let wrong = WrongKeys::start(&peers, lan, &addr, share());
    let owner_ip = bare_initials(lan, 2 * share(), target);
    let (next, after, accepted, _) = owner_epoch(&rt, &clock, &member, lan, &owner_key);
    let dropped = member.lan_dropped_datagrams().unwrap();
    wrong.stop(&peers);
    drop((other_host, owner_ip));
    member
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    eprintln!(
        "owner admission latency before {before:?} ({first:?}), after the flood {after:?} \
         ({next:?}); learned {learned:?}; filter dropped {dropped} datagrams"
    );
    assert_eq!(first, Ok(addr.id));
    assert!(
        dropped > 0,
        "the flood never met a full lane or waiting room"
    );
    assert_eq!(next, Ok(addr.id), "the owner's next dial");
    assert_eq!(accepted, Ok(owner_key.public()));
    assert!(
        after < span(B::OwnerPoll),
        "the owner's next connection waited {after:?} (before the flood: {before:?})"
    );
}

/// Ruling 12 / test (3) on this host's real interfaces: the owner moves from
/// 127.0.0.1 to the LAN address (a DHCP change), while a host now at its old
/// address floods. The owner is re-learned through the shrunken waiting room
/// within its dial deadline, its old host expires at once, and however often
/// it moves the member holds one learned host for its one admitted key.
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_an_owner_ip_change_is_relearned_within_its_dial_deadline() {
    let rt = runtime(1);
    let clock = Mono(Instant::now());
    let lan = lan_ip();
    let mut member = host(&rt, &clock, lan, SecretKey::generate());
    let addr = member.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let owner_key = SecretKey::generate();
    member.admit_peer_ids(&[owner_key.public()]).unwrap();
    let (first, _, _, _) = owner_epoch(&rt, &clock, &member, loopback(), &owner_key);
    let learned_old = learned_ips(&member);
    let old_address = bare_initials(loopback(), 4 * count(B::RelayConnectionSlots), target);
    let (moved, waited, accepted, _) = owner_epoch(&rt, &clock, &member, lan, &owner_key);
    let learned_new = learned_ips(&member);
    drop(old_address);
    let mut sizes = Vec::new();
    let mut outcomes = Vec::new();
    for round in 0..4 {
        let at = if round % 2 == 0 { loopback() } else { lan };
        let (dialed, _, _, _) = owner_epoch(&rt, &clock, &member, at, &owner_key);
        outcomes.push(dialed);
        sizes.push(learned_ips(&member));
    }
    member
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    eprintln!(
        "learned {learned_old:?} -> moved in {waited:?} ({moved:?}) -> {learned_new:?}; \
         later moves {sizes:?}"
    );
    assert_eq!(first, Ok(addr.id));
    assert_eq!(learned_old, vec![loopback()]);
    assert_eq!(
        moved,
        Ok(addr.id),
        "re-learning within the owner's dial deadline"
    );
    assert_eq!(accepted, Ok(owner_key.public()));
    assert_eq!(
        learned_new,
        vec![lan],
        "the new host is learned and the old one expired"
    );
    assert!(outcomes.iter().all(|o| *o == Ok(addr.id)), "{outcomes:?}");
    for (round, learned) in sizes.iter().enumerate() {
        let at = if round % 2 == 0 { loopback() } else { lan };
        assert_eq!(learned, &vec![at], "round {round}: one host for one key");
    }
}

/// A second local address of this host (e.g. a lo0 alias), standing in for a
/// third LAN host.
fn third_ip() -> IpAddr {
    let value = std::env::var("MYCELIUM_LAN_THIRD_IP")
        .expect("MYCELIUM_LAN_THIRD_IP = another local address of this host");
    let address = ip(&value);
    assert!(
        address != loopback() && address != lan_ip(),
        "{address} must differ from 127.0.0.1 and MYCELIUM_LAN_IP"
    );
    address
}

/// B15 (REVIEW-laneB14 F1) on this host's real interfaces: the owner moves
/// from 127.0.0.1 to the LAN address while a THIRD host floods the shrunken
/// waiting room, with bare Initials from four tables' worth of ports and
/// completed handshakes under the wrong key. That host holds only its B11
/// share of the room, so the owner is re-learned within its dial deadline and
/// its new host then has its own lane.
#[test]
#[ignore = "needs MYCELIUM_LAN_IP and a second local address in MYCELIUM_LAN_THIRD_IP"]
fn real_lan_an_ip_change_is_relearned_while_a_third_host_floods_the_waiting_room() {
    let rt = runtime(1);
    let peers = runtime(2);
    let clock = Mono(Instant::now());
    let (lan, third) = (lan_ip(), third_ip());
    let mut member = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        lan,
        LanPolicy::with_explicit(vec![loopback(), third]).unwrap(),
    )
    .unwrap();
    let addr = member.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let owner_key = SecretKey::generate();
    member.admit_peer_ids(&[owner_key.public()]).unwrap();
    let (first, _, _, _) = owner_epoch(&rt, &clock, &member, loopback(), &owner_key);
    let learned_old = learned_ips(&member);
    let flood = bare_initials(third, 4 * count(B::RelayConnectionSlots), target);
    let wrong = WrongKeys::start(&peers, third, &addr, share());
    let (moved, waited, accepted, _) = owner_epoch(&rt, &clock, &member, lan, &owner_key);
    let learned_new = learned_ips(&member);
    let more = bare_initials(lan, 2 * share(), target);
    let (next, after, _, _) = owner_epoch(&rt, &clock, &member, lan, &owner_key);
    let dropped = member.lan_dropped_datagrams().unwrap();
    wrong.stop(&peers);
    drop((flood, more));
    member
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    eprintln!(
        "learned {learned_old:?} -> moved in {waited:?} ({moved:?}) under a {third} flood -> \
         {learned_new:?}; next epoch {after:?} ({next:?}); filter dropped {dropped}"
    );
    assert_eq!(first, Ok(addr.id));
    assert_eq!(learned_old, vec![loopback()]);
    assert!(dropped > 0, "the third host's flood never met a full room");
    assert_eq!(
        moved,
        Ok(addr.id),
        "re-learning within the owner's dial deadline while a third host floods"
    );
    assert_eq!(accepted, Ok(owner_key.public()));
    assert_eq!(learned_new, vec![lan], "the new host is learned");
    assert_eq!(next, Ok(addr.id), "the new host's own lane under a flood");
}
