// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded iroh address map across route epochs (lane B11, gap row 19,
//! REVIEW-laneB9f (3)).
//!
//! iroh 1.0.2 maps every remote custom address an endpoint ever sees into an
//! `AddrMap` that never evicts (`socket.rs:654-660`, `mapped_addrs.rs:
//! 348-359`). A member host serving route epochs for days would grow it by the
//! owner's fresh socket every epoch (the owner binds a new endpoint per epoch,
//! `m16_route.rs`) plus whatever a LAN host floods into the receive filter.
//! `LoopbackHost::recycle_lan` at each epoch boundary closes the endpoint
//! (dropping that map) and rebinds the SAME socket and key, so the signed
//! carrier address stays valid. `lan_sources_admitted` is the transport's
//! upper bound on that map's growth for the live endpoint. Loopback only
//! (127.0.0.1, explicitly allowed); no other host is contacted.
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::SecretKey;
use runtime::iroh_host::{LanPolicy, LoopbackHost};
use std::{
    net::{IpAddr, SocketAddr, UdpSocket},
    time::Instant,
};
use transport::{Channel, Clock, Error};

/// Route epochs served by one member host.
const EPOCHS: usize = 8;

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
fn loopback() -> IpAddr {
    "127.0.0.1".parse().unwrap()
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
fn host<'a>(rt: &'a tokio::runtime::Runtime, clock: &Mono, secret: SecretKey) -> LoopbackHost<'a> {
    LoopbackHost::bind_lan(
        rt,
        clock,
        deadline(clock, B::RouterClose),
        secret,
        &[Channel::Router],
        loopback(),
        LanPolicy::with_explicit(vec![loopback()]).unwrap(),
    )
    .unwrap()
}
/// A QUIC v1 long-header Initial, padded to the 1200-byte minimum.
fn initial_like() -> Vec<u8> {
    let mut datagram = vec![0u8; 1200];
    datagram[0] = 0xc0;
    datagram[1..5].copy_from_slice(&1u32.to_be_bytes());
    datagram
}

/// Each epoch: a LAN host sends Initials from fresh ports (fewer than its
/// share, so the owner still fits), then the route owner dials from a fresh
/// socket under its one key and the member accepts it (key admitted from the
/// signed route, as `joined_shard::serve` does). The member recycles at every
/// epoch boundary: its address never changes, the owner is accepted in every
/// epoch, and the live endpoint's map bound stays at ONE epoch's sources
/// instead of growing with every epoch served.
#[test]
fn recycling_the_lan_endpoint_per_epoch_keeps_its_address_map_bounded() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut member = host(&rt, &clock, SecretKey::generate());
    let addr = member.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let owner_key = SecretKey::generate();
    member.admit_peer_ids(&[owner_key.public()]).unwrap();
    let flood_ports = count(B::RelayConnectionSlots) / 4 - 1;
    let epoch_sources = u64::try_from(flood_ports + 1).unwrap();
    let initial = initial_like();
    let mut bounds = Vec::with_capacity(EPOCHS);
    for epoch in 0..EPOCHS {
        let flood: Vec<UdpSocket> = (0..flood_ports)
            .map(|_| {
                let socket = UdpSocket::bind(SocketAddr::new(loopback(), 0)).unwrap();
                socket.send_to(&initial, target).unwrap();
                socket
            })
            .collect();
        let mut owner = host(&rt, &clock, owner_key.clone());
        let (dialed, accepted) = std::thread::scope(|scope| {
            let accepting = scope.spawn(|| {
                let window = deadline(&clock, B::WorkerStartup);
                loop {
                    match member.accept(&clock, window) {
                        Err(Error::Authentication) => continue,
                        other => return other,
                    }
                }
            });
            let dialed = owner.connect(&addr, &clock, deadline(&clock, B::WorkerStartup));
            (dialed, accepting.join().unwrap())
        });
        let accepted_id = accepted.as_ref().map(|c| c.remote_id()).map_err(|e| *e);
        for connection in dialed.iter().chain(accepted.iter()) {
            connection.close(0u32.into(), b"epoch done");
        }
        owner
            .close(&clock, deadline(&clock, B::RouterClose))
            .unwrap();
        drop(flood);
        assert_eq!(accepted_id, Ok(owner_key.public()), "epoch {epoch}: owner");
        bounds.push(member.lan_sources_admitted().unwrap());
        member
            .recycle_lan(&clock, deadline(&clock, B::RouterClose))
            .unwrap();
        assert_eq!(
            member.address().unwrap(),
            addr,
            "a recycle must keep the signed carrier address"
        );
    }
    member
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    eprintln!("address-map bound per epoch (one epoch = {epoch_sources}): {bounds:?}");
    assert!(
        bounds.iter().all(|bound| *bound <= epoch_sources),
        "the live endpoint's address map grew past one epoch's {epoch_sources} sources \
         across {EPOCHS} epochs: {bounds:?}"
    );
}

/// `recycle_lan` is a direct-LAN operation only, and a closed host stays
/// closed (no silent rebind of an endpoint the caller already released).
#[test]
fn recycle_is_refused_outside_direct_lan_and_after_close() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut local = LoopbackHost::bind(&rt, &clock, deadline(&clock, B::RouterClose)).unwrap();
    assert_eq!(
        local.recycle_lan(&clock, deadline(&clock, B::RouterClose)),
        Err(Error::Unavailable)
    );
    local
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    let mut member = host(&rt, &clock, SecretKey::generate());
    member
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    assert!(member.address().is_err());
    assert!(
        member
            .recycle_lan(&clock, deadline(&clock, B::RouterClose))
            .is_err(),
        "a closed host must not be rebound"
    );
}
