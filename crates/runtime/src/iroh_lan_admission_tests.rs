// SPDX-License-Identifier: AGPL-3.0-only
//! Pin classes and the receive filter's provisional table (lane B9f,
//! REVIEW-laneB9 F1/F2), on [`Admission`] directly: no sockets.
use super::{Admission, Pin, quic_initial};
use crate::iroh_host::LanPolicy;
use budgets::{BudgetId as B, Entries};
use std::{
    net::{IpAddr, SocketAddr},
    sync::Arc,
    time::{Duration, Instant},
};
use transport::Error;

fn count(id: B) -> usize {
    usize::try_from(id.count().unwrap().get()).unwrap()
}
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn ip(text: &str) -> IpAddr {
    text.parse().unwrap()
}
/// A QUIC v1 long-header Initial, padded to the 1200-byte minimum.
fn initial_like() -> Vec<u8> {
    let mut datagram = vec![0u8; 1200];
    datagram[0] = 0xc0;
    datagram[1..5].copy_from_slice(&1u32.to_be_bytes());
    datagram
}
/// A short-header (1-RTT) datagram.
fn short_header() -> Vec<u8> {
    let mut datagram = vec![0u8; 64];
    datagram[0] = 0x40;
    datagram
}
fn admission(policy: LanPolicy) -> Arc<Admission> {
    Admission::new(
        policy,
        count(B::SidecarQueueCapacity),
        count(B::ReplayCapacity),
    )
}
fn loopback_policy() -> LanPolicy {
    LanPolicy::with_explicit(vec![ip("127.0.0.1"), ip("127.0.0.2")]).unwrap()
}
fn at(port: usize) -> SocketAddr {
    SocketAddr::new(ip("127.0.0.1"), u16::try_from(port).unwrap())
}
/// Distinct LAN hosts for the per-host share (B11): `127.0.0.<host>`, each
/// explicitly allowed. Pure `Admission` cases, so no such address is bound.
const HOSTS: usize = 6;
fn host_ip(host: usize) -> IpAddr {
    IpAddr::from([127, 0, 0, u8::try_from(host).unwrap()])
}
fn on(host: usize, port: usize) -> SocketAddr {
    SocketAddr::new(host_ip(host), u16::try_from(port).unwrap())
}
fn hosts_policy() -> LanPolicy {
    LanPolicy::with_explicit((1..=HOSTS).map(host_ip).collect()).unwrap()
}
/// One source host's share of the provisional table: a quarter of it
/// (REVIEW-laneB9f (1)), so three other hosts always find room.
fn share() -> usize {
    count(B::RelayConnectionSlots) / 4
}

/// F2: accept-side (provisional) pins have their own capacity and fail closed
/// there; the whole authenticated capacity stays available to dials.
#[test]
fn provisional_accept_pins_never_take_or_block_authenticated_pins() {
    let lan = admission(loopback_policy());
    let slots = count(B::RelayConnectionSlots);
    let capacity = count(B::SidecarQueueCapacity);
    let provisional: Vec<Pin> = (0..slots)
        .map(|i| lan.pin_provisional([at(10_000 + i)]).unwrap())
        .collect();
    assert_eq!(
        lan.pin_provisional([at(20_000)]).unwrap_err(),
        Error::Unavailable,
        "the provisional table must fail closed at its own bound"
    );
    let authenticated: Vec<Pin> = (0..capacity)
        .map(|i| lan.pin([at(30_000 + i)]).unwrap())
        .collect();
    assert_eq!(lan.pin([at(40_000)]).unwrap_err(), Error::Unavailable);
    assert!(lan.may_send(at(10_000)) && lan.may_send(at(30_000)));
    drop(provisional);
    assert!(!lan.may_send(at(10_000)) && lan.may_send(at(30_000)));
    drop(authenticated);
    assert!(!lan.may_send(at(30_000)));
}

/// F2: an accept whose key the host verified moves to the authenticated class
/// and frees its provisional slot; a refused one leaves no trace in the
/// address history the path monitor trusts.
#[test]
fn a_verified_accept_moves_to_the_authenticated_class() {
    let lan = admission(loopback_policy());
    let slots = count(B::RelayConnectionSlots);
    let mut held: Vec<Pin> = (0..slots)
        .map(|i| lan.pin_provisional([at(10_000 + i)]).unwrap())
        .collect();
    assert!(lan.pin_provisional([at(20_000)]).is_err());
    held[0].promote().unwrap();
    let next = lan.pin_provisional([at(20_000)]).unwrap();
    assert!(
        lan.may_send(at(10_000)),
        "a promoted pin still admits sends"
    );
    assert!(
        lan.admits_seen(at(10_000)),
        "a verified accept is remembered"
    );
    drop(next);
    assert!(
        !lan.admits_seen(at(20_000)),
        "an unverified, released provisional pin must not be remembered"
    );
    drop(held);
    assert!(!lan.may_send(at(10_000)));
}

/// F1: the receive filter's provisional table holds at most one slot per
/// concurrent handshake, each for one handshake budget, and fails closed when
/// full; pinned sources and sources outside the allow-list / peer hosts are
/// decided without it. B11: filled by several hosts (one host alone may take
/// only its share, see the next cases).
#[test]
fn the_inbound_table_is_bounded_expires_and_fails_closed() {
    let lan = admission(
        hosts_policy()
            .with_peer_hosts((1..HOSTS).map(host_ip).collect())
            .unwrap(),
    );
    let slots = count(B::RelayConnectionSlots);
    let initial = initial_like();
    let now = Instant::now();
    let filling: Vec<SocketAddr> = (0..slots)
        .map(|i| on(1 + i / share(), 10_000 + i))
        .collect();
    for source in &filling {
        assert!(lan.admit_inbound(*source, &initial, now), "{source}");
    }
    assert!(
        lan.admit_inbound(filling[0], &short_header(), now),
        "an admitted source keeps its handshake window"
    );
    let waiting = on(HOSTS - 1, 20_000);
    assert!(
        !lan.admit_inbound(waiting, &initial, now),
        "a full provisional table must fail closed"
    );
    let _pin = lan.pin([on(1, 30_000)]).unwrap();
    assert!(
        lan.admit_inbound(on(1, 30_000), &short_header(), now),
        "a pinned source is never throttled"
    );
    let later = now + span(B::SidecarLocalHandshake);
    assert!(
        !lan.admit_inbound(filling[0], &short_header(), later),
        "an expired entry admits nothing but a new Initial"
    );
    assert!(
        lan.admit_inbound(waiting, &initial, later),
        "expired entries are evicted"
    );
    assert!(
        !lan.admit_inbound(on(HOSTS, 9), &initial, later),
        "a source outside the signed peer hosts is never admitted"
    );
    assert!(
        !lan.admit_inbound(SocketAddr::new(ip("127.0.0.99"), 9), &initial, later),
        "a source outside the allow-list is never admitted"
    );
}

/// B11 (REVIEW-laneB9f (1)): one LAN host sending Initials from many ports
/// takes at most its share of the provisional table, so a peer on another
/// host is still admitted while it floods.
#[test]
fn one_host_flooding_from_many_ports_takes_only_its_share() {
    let lan = admission(hosts_policy());
    let slots = count(B::RelayConnectionSlots);
    let initial = initial_like();
    let now = Instant::now();
    let flooded = (0..4 * slots)
        .filter(|i| lan.admit_inbound(on(1, 10_000 + i), &initial, now))
        .count();
    assert_eq!(
        flooded,
        share(),
        "one host took {flooded} of the {slots} provisional slots"
    );
    for host in 2..=4 {
        assert!(
            lan.admit_inbound(on(host, 9), &initial, now),
            "host {host}'s Initial was refused while host 1 floods"
        );
    }
}

/// B17 (REVIEW-laneB11 (1)): the waiting-room share is per host, not per
/// address. An IPv6 host can rotate through its whole /64 (SLAAC privacy
/// addresses), so a share is counted per /64: one host sending Initials from
/// every address of its /64 takes one share, and a host in another /64 (or on
/// IPv4) is still admitted. Link-local IPv6 keeps the whole address, because
/// every link-local host shares fe80::/64.
#[test]
fn a_host_rotating_through_its_ipv6_64_takes_only_one_share() {
    let lan = admission(LanPolicy::private_only());
    let slots = count(B::RelayConnectionSlots);
    let initial = initial_like();
    let now = Instant::now();
    let rotating = |i: usize| {
        let low = u16::try_from(i + 1).unwrap();
        SocketAddr::new(IpAddr::from([0xfd00, 0xb17, 0, 1, 0x1234, 0, 0, low]), 9)
    };
    let flooded = (0..4 * slots)
        .filter(|i| lan.admit_inbound(rotating(*i), &initial, now))
        .count();
    assert_eq!(
        flooded,
        share(),
        "one /64 took {flooded} of the {slots} provisional slots"
    );
    for other in [
        SocketAddr::new(IpAddr::from([0xfd00, 0xb17, 0, 2, 0, 0, 0, 1]), 9),
        SocketAddr::new(ip("192.168.17.2"), 9),
        SocketAddr::new(ip("fe80::1"), 9),
        SocketAddr::new(ip("fe80::2"), 9),
    ] {
        assert!(
            lan.admit_inbound(other, &initial, now),
            "{other}'s Initial was refused while one /64 floods"
        );
    }
    // Two link-local hosts are two hosts: each keeps a share of its own.
    let link_local = |host: u16, port: usize| {
        SocketAddr::new(
            IpAddr::from([0xfe80, 0, 0, 0, 0, 0, 0, host]),
            u16::try_from(port).unwrap(),
        )
    };
    let first = (0..slots)
        .filter(|i| lan.admit_inbound(link_local(3, 10_000 + i), &initial, now))
        .count();
    assert_eq!(first, share(), "one link-local host's share");
    assert!(
        lan.admit_inbound(link_local(4, 9), &initial, now),
        "a second link-local host shares the first one's share"
    );
}

/// B11: a host's share also counts its provisional (accept-side) pins, so it
/// cannot re-take fresh ports once its receive-filter entries expire while
/// its handshakes are still held.
#[test]
fn a_hosts_held_provisional_pins_count_against_its_share() {
    let lan = admission(hosts_policy());
    let initial = initial_like();
    let now = Instant::now();
    let held: Vec<Pin> = (0..share())
        .map(|i| {
            assert!(lan.admit_inbound(on(1, 10_000 + i), &initial, now));
            lan.pin_provisional([on(1, 10_000 + i)]).unwrap()
        })
        .collect();
    let later = now + span(B::SidecarLocalHandshake);
    assert!(
        !lan.admit_inbound(on(1, 20_000), &initial, later),
        "host 1 re-took a fresh port while holding its whole share in pins"
    );
    drop(held);
    assert!(
        lan.admit_inbound(on(1, 20_000), &initial, later),
        "released pins must give the share back"
    );
}

/// B11 (REVIEW-laneB9f (1)): a handshake the host refused (unadmitted key)
/// frees its table slot AT ONCE for every other host, not after the entry's
/// handshake budget, but keeps counting against the refused host's own share
/// until then (it cannot cycle ports faster by being refused).
#[test]
fn a_refused_handshake_frees_its_slot_at_once_but_not_its_hosts_share() {
    let lan = admission(hosts_policy());
    let slots = count(B::RelayConnectionSlots);
    let initial = initial_like();
    let now = Instant::now();
    let filling: Vec<SocketAddr> = (0..slots)
        .map(|i| on(1 + i / share(), 10_000 + i))
        .collect();
    for source in &filling {
        assert!(lan.admit_inbound(*source, &initial, now), "{source}");
    }
    let waiting = on(HOSTS, 9);
    assert!(!lan.admit_inbound(waiting, &initial, now), "table full");
    let refused = lan.pin_provisional([filling[0]]).unwrap();
    refused.refuse();
    assert!(
        lan.admit_inbound(waiting, &initial, now),
        "the refused handshake's slot was not freed at once"
    );
    assert!(
        !lan.admit_inbound(filling[0], &short_header(), now),
        "a refused source must not keep its receive window"
    );
    assert!(
        !lan.admit_inbound(on(1, 20_000), &initial, now),
        "the refused host re-took its slot from a fresh port at once"
    );
    let later = now + span(B::SidecarLocalHandshake);
    assert!(lan.admit_inbound(on(1, 20_000), &initial, later));
}

/// What counts as a connection-opening datagram (RFC 9000 17.2 / 14.1).
#[test]
fn only_a_full_size_quic_initial_opens_a_provisional_entry() {
    let initial = initial_like();
    assert!(quic_initial(&initial));
    let mut handshake = initial.clone();
    handshake[0] = 0xe0;
    let mut zero_rtt = initial.clone();
    zero_rtt[0] = 0xd0;
    let mut negotiation = initial.clone();
    negotiation[1..5].copy_from_slice(&0u32.to_be_bytes());
    let mut unsupported = initial.clone();
    unsupported[1..5].copy_from_slice(&0x6b33_43cfu32.to_be_bytes());
    for (name, datagram) in [
        ("short", initial[..1199].to_vec()),
        ("short header", short_header()),
        ("handshake", handshake),
        ("0-rtt", zero_rtt),
        ("version negotiation", negotiation),
        ("unsupported version", unsupported),
        ("empty", Vec::new()),
    ] {
        assert!(!quic_initial(&datagram), "{name} must not open an entry");
    }
}
