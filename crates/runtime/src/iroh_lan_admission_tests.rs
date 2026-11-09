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
/// decided without it.
#[test]
fn the_inbound_table_is_bounded_expires_and_fails_closed() {
    let lan = admission(
        loopback_policy()
            .with_peer_hosts(vec![ip("127.0.0.1")])
            .unwrap(),
    );
    let slots = count(B::RelayConnectionSlots);
    let initial = initial_like();
    let now = Instant::now();
    for i in 0..slots {
        assert!(lan.admit_inbound(at(10_000 + i), &initial, now));
    }
    assert!(
        lan.admit_inbound(at(10_000), &short_header(), now),
        "an admitted source keeps its handshake window"
    );
    assert!(
        !lan.admit_inbound(at(20_000), &initial, now),
        "a full provisional table must fail closed"
    );
    let _pin = lan.pin([at(30_000)]).unwrap();
    assert!(
        lan.admit_inbound(at(30_000), &short_header(), now),
        "a pinned source is never throttled"
    );
    let later = now + span(B::SidecarLocalHandshake);
    assert!(
        !lan.admit_inbound(at(10_000), &short_header(), later),
        "an expired entry admits nothing but a new Initial"
    );
    assert!(
        lan.admit_inbound(at(20_000), &initial, later),
        "expired entries are evicted"
    );
    assert!(
        !lan.admit_inbound(SocketAddr::new(ip("127.0.0.2"), 9), &initial, later),
        "a source outside the signed peer hosts is never admitted"
    );
    assert!(
        !lan.admit_inbound(SocketAddr::new(ip("127.0.0.3"), 9), &initial, later),
        "a source outside the allow-list is never admitted"
    );
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
