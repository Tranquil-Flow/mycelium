// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticated host learning of the direct-LAN receive filter (lane B14,
//! owner ruling 12 option A), on [`Admission`] directly: no sockets.
//!
//! A host learns the address of an ADMITTED peer key only after that key was
//! verified on a completed handshake. A learned host's Initials then use their
//! own lane of the provisional table, which no other source can fill, and the
//! waiting room for every other source shrinks once every admitted key's host
//! is learned. One learned host per admitted key; a key verified at a new
//! address replaces its old one at once.
use super::{Admission, Pin};
use crate::iroh_host::LanPolicy;
use budgets::{BudgetId as B, Entries};
use iroh::SecretKey;
use std::{
    net::{IpAddr, SocketAddr},
    sync::Arc,
    time::{Duration, Instant},
};

fn count(id: B) -> usize {
    usize::try_from(id.count().unwrap().get()).unwrap()
}
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// A QUIC v1 long-header Initial, padded to the 1200-byte minimum.
fn initial_like() -> Vec<u8> {
    let mut datagram = vec![0u8; 1200];
    datagram[0] = 0xc0;
    datagram[1..5].copy_from_slice(&1u32.to_be_bytes());
    datagram
}
/// Distinct LAN hosts `127.0.0.<host>`, each explicitly allowed. Pure
/// `Admission` cases, so no such address is bound.
const HOSTS: usize = 8;
fn host_ip(host: usize) -> IpAddr {
    IpAddr::from([127, 0, 0, u8::try_from(host).unwrap()])
}
fn on(host: usize, port: usize) -> SocketAddr {
    SocketAddr::new(host_ip(host), u16::try_from(port).unwrap())
}
fn admission() -> Arc<Admission> {
    Admission::new(
        LanPolicy::with_explicit((1..=HOSTS).map(host_ip).collect()).unwrap(),
        count(B::SidecarQueueCapacity),
        count(B::ReplayCapacity),
    )
}
fn key() -> iroh::EndpointId {
    SecretKey::generate().public()
}
fn slots() -> usize {
    count(B::RelayConnectionSlots)
}
/// A source host's share of the full provisional table (B11).
fn share() -> usize {
    slots() / 4
}
/// Fresh-port Initials `host` gets admitted out of `tries`, all at `now`.
fn flood(lan: &Admission, host: usize, base: usize, tries: usize, now: Instant) -> usize {
    let initial = initial_like();
    (0..tries)
        .filter(|i| lan.admit_inbound(on(host, base + i), &initial, now))
        .count()
}

/// Learning needs an admitted key: nothing without keys, nothing for a key
/// outside them, nothing for a source outside the allow-list.
#[test]
fn a_host_is_learned_only_for_an_admitted_key() {
    let lan = admission();
    let (owner, stranger) = (key(), key());
    lan.learn(on(1, 9), &owner);
    assert!(
        lan.learned_hosts().is_empty(),
        "learned without any admitted key (the caller verifies keys itself)"
    );
    lan.set_keys(&[owner]).unwrap();
    lan.learn(on(1, 9), &stranger);
    assert!(
        lan.learned_hosts().is_empty(),
        "learned the host of an unadmitted key"
    );
    lan.learn(SocketAddr::new("127.0.0.99".parse().unwrap(), 9), &owner);
    assert!(
        lan.learned_hosts().is_empty(),
        "learned a host outside the allow-list"
    );
    lan.learn(on(1, 9), &owner);
    assert_eq!(lan.learned_hosts(), vec![on(1, 9)]);
}

/// One learned host per admitted key: a key verified at a new address
/// replaces the old one at once (IP change), so the table never holds more
/// hosts than admitted keys; a key no longer admitted loses its host.
#[test]
fn learned_hosts_are_one_per_admitted_key_and_follow_the_keys() {
    let lan = admission();
    let keys: Vec<iroh::EndpointId> = (0..3).map(|_| key()).collect();
    lan.set_keys(&keys).unwrap();
    for round in 0..4 * HOSTS {
        for (k, admitted) in keys.iter().enumerate() {
            lan.learn(on(1 + (round + k) % HOSTS, 10_000 + round), admitted);
            assert!(
                lan.learned_hosts().len() <= keys.len(),
                "{} learned hosts for {} admitted keys",
                lan.learned_hosts().len(),
                keys.len()
            );
        }
    }
    lan.learn(on(1, 9), &keys[0]);
    lan.learn(on(2, 9), &keys[0]);
    assert!(
        lan.learned_hosts().contains(&on(2, 9)) && !lan.learned_hosts().contains(&on(1, 9)),
        "the old host of a re-learned key did not expire: {:?}",
        lan.learned_hosts()
    );
    lan.set_keys(&keys[1..]).unwrap();
    assert!(
        !lan.learned_hosts().contains(&on(2, 9)),
        "a key no longer admitted kept its learned host"
    );
    assert_eq!(lan.learned_hosts().len(), keys.len() - 1);
    assert_eq!(
        lan.successor(Instant::now()).unwrap().learned_hosts(),
        lan.learned_hosts(),
        "a recycled endpoint must keep the learned hosts of its admitted keys"
    );
}

/// The learned host's Initials have their own lane: a flood from any number of
/// other hosts or ports cannot fill it, and once every admitted key's host is
/// learned the waiting room for all other sources shrinks (re-learning after
/// an IP change still finds a slot; one host takes at most one).
#[test]
fn a_learned_host_has_its_own_lane_and_the_waiting_room_shrinks() {
    let lan = admission();
    let owner = key();
    lan.set_keys(&[owner]).unwrap();
    let now = Instant::now();
    assert_eq!(
        flood(&lan, 2, 10_000, 4 * slots(), now),
        share(),
        "before learning, one host takes its B11 share"
    );
    lan.learn(on(1, 9), &owner);
    let later = now + span(B::SidecarLocalHandshake);
    let waiting = [3, 4, 5, 6]
        .iter()
        .map(|host| flood(&lan, *host, 20_000, 4 * slots(), later))
        .sum::<usize>();
    assert!(
        (1..=2).contains(&waiting),
        "{waiting} slots for other hosts once the owner's host is learned \
         (ruling 12: one or two, for re-learning after an IP change)"
    );
    assert_eq!(
        flood(&lan, 7, 30_000, 4 * slots(), later),
        0,
        "the shrunken waiting room is full"
    );
    assert_eq!(
        flood(&lan, 1, 40_000, slots(), later),
        slots(),
        "the learned host's own lane must be untouched by the others' flood"
    );
}

/// A handshake the host refuses (an unadmitted key, or a failed handshake)
/// from the learned host's address frees its lane slot at once and does not
/// count against that lane: a wrong key at the owner's IP cannot starve it.
#[test]
fn refused_handshakes_at_a_learned_host_do_not_hold_its_lane() {
    let lan = admission();
    let owner = key();
    lan.set_keys(&[owner]).unwrap();
    lan.learn(on(1, 9), &owner);
    let now = Instant::now();
    assert_eq!(flood(&lan, 1, 10_000, slots(), now), slots());
    assert_eq!(flood(&lan, 1, 20_000, 1, now), 0, "lane full");
    let refused: Vec<Pin> = (0..share())
        .map(|i| lan.pin_provisional([on(1, 10_000 + i)]).unwrap())
        .collect();
    for pin in refused {
        pin.refuse();
    }
    assert_eq!(
        flood(&lan, 1, 20_000, share(), now),
        share(),
        "refused handshakes at the learned host still hold its lane"
    );
}

/// Fresh-port Initials every host outside the learned one (`3..=6`) gets
/// admitted at `now`: the waiting room's size (each host keeps its B11 share).
fn waiting_room(lan: &Admission, now: Instant) -> usize {
    (3..=6)
        .map(|host| flood(lan, host, 50_000 + 1_000 * host, 4 * slots(), now))
        .sum()
}

/// B15 (REVIEW-laneB14 F2): a learned host expires one `MembershipTtl` after
/// its last authenticated connection ended (or after it was learned, if none
/// is live): the waiting room is restored to the whole B11 table and the old
/// address loses its own lane. A host with a live authenticated connection
/// never expires; one inside the window still keeps the room shrunk.
#[test]
fn a_learned_host_expires_one_membership_ttl_after_its_last_connection() {
    let (ttl, hs) = (span(B::MembershipTtl), span(B::SidecarLocalHandshake));
    let owner = key();
    // Inside the window (no live connection): still shrunk.
    let fresh = admission();
    fresh.set_keys(&[owner]).unwrap();
    let learned_at = Instant::now();
    fresh.learn(on(1, 9), &owner);
    let inside = waiting_room(&fresh, learned_at + ttl - hs);
    assert!(
        (1..=2).contains(&inside),
        "{inside} waiting-room slots inside the window (the host is not stale yet)"
    );
    // A live authenticated connection (the accept path's promoted pin).
    let lan = admission();
    lan.set_keys(&[owner]).unwrap();
    let mut live = lan.pin_provisional([on(1, 9)]).unwrap();
    live.promote().unwrap();
    lan.learn(on(1, 9), &owner);
    let held = Instant::now() + ttl + hs;
    let while_live = waiting_room(&lan, held);
    assert!(
        (1..=2).contains(&while_live),
        "{while_live} waiting-room slots: a host with a live connection expired"
    );
    drop(live);
    let closed = Instant::now();
    let stale = (closed + ttl + hs).max(held + hs);
    assert_eq!(
        waiting_room(&lan, stale),
        4 * share(),
        "a stale learned host still keeps the waiting room shrunk"
    );
    assert_eq!(
        flood(&lan, 1, 60_000, slots(), stale + hs),
        share(),
        "a stale learned address keeps its own lane"
    );
}

/// B15 (REVIEW-laneB14 F1): the owner's IP changes while ANOTHER host floods
/// the shrunken waiting room from fresh ports. The flooder holds only its B11
/// share of that room (one slot), so the owner's first Initial from its new
/// host finds the other; its verified handshake re-learns it at once, and its
/// new host gets its own lane. (Documented limit, ruling 12: two flooding
/// hosts can fill the two slots until the old host goes stale, B15 item 1.)
#[test]
fn an_ip_change_is_relearned_while_another_host_floods_the_waiting_room() {
    let lan = admission();
    let owner = key();
    lan.set_keys(&[owner]).unwrap();
    let mut first = lan.pin_provisional([on(1, 9)]).unwrap();
    first.promote().unwrap();
    lan.learn(on(1, 9), &owner);
    drop(first);
    let now = Instant::now();
    let flooded = flood(&lan, 3, 10_000, 4 * slots(), now);
    assert_eq!(flooded, 1, "one flooding host takes one waiting-room slot");
    let moved = on(2, 9);
    assert!(
        lan.admit_inbound(moved, &initial_like(), now),
        "the owner's first Initial from its new host found no waiting-room slot"
    );
    let mut pin = lan.pin_provisional([moved]).unwrap();
    pin.promote().unwrap();
    lan.learn(moved, &owner);
    assert_eq!(
        lan.learned_hosts(),
        vec![moved],
        "re-learned at its new host"
    );
    assert_eq!(
        flood(&lan, 2, 20_000, slots(), now),
        slots(),
        "the new host's own lane"
    );
    assert_eq!(
        flood(&lan, 3, 30_000, 4 * slots(), now),
        0,
        "the flooder still holds no more than its share"
    );
    drop(pin);
}

/// B15f (REVIEW-laneB15 F1): a host whose authenticated connection is still
/// live when the endpoint recycles (B11, every route epoch) stays current in
/// the successor, however long it was connected: the recycle closes the old
/// endpoint, whose pins then never mark the host as seen, so the successor
/// counts its staleness from the recycle. A host idle at the recycle keeps its
/// old instant (a recycle never renews a silent address), and a carried host
/// still expires one `MembershipTtl` after the recycle without a new one.
#[test]
fn a_host_connected_past_the_ttl_stays_learned_across_a_recycle() {
    let (ttl, hs) = (span(B::MembershipTtl), span(B::SidecarLocalHandshake));
    let owner = key();
    let lan = admission();
    lan.set_keys(&[owner]).unwrap();
    let mut live = lan.pin_provisional([on(1, 9)]).unwrap();
    live.promote().unwrap();
    lan.learn(on(1, 9), &owner);
    // Connected for longer than the TTL when the route epoch recycles.
    let recycled = Instant::now() + ttl + hs;
    let next = lan.successor(recycled).unwrap();
    // `recycle_lan`: the old endpoint closes after its successor is taken.
    lan.close();
    drop(live);
    assert_eq!(next.learned_hosts(), vec![on(1, 9)]);
    let after = recycled + hs;
    let room = waiting_room(&next, after);
    assert!(
        (1..=2).contains(&room),
        "{room} waiting-room slots in the next epoch: an owner connected past the TTL \
         lost its learned host at the recycle"
    );
    assert_eq!(
        flood(&next, 1, 60_000, slots(), after),
        slots(),
        "the carried host's own lane"
    );
    assert_eq!(
        waiting_room(&next, recycled + ttl + hs),
        4 * share(),
        "a carried host must still expire one TTL after the recycle"
    );
    // Idle at the recycle: the successor keeps its old instant (stale).
    let idle = admission();
    idle.set_keys(&[owner]).unwrap();
    idle.learn(on(1, 9), &owner);
    let quiet = idle.successor(Instant::now() + ttl + hs).unwrap();
    idle.close();
    assert_eq!(
        waiting_room(&quiet, Instant::now() + ttl + 2 * hs),
        4 * share(),
        "a recycle renewed a host with no live connection"
    );
}
