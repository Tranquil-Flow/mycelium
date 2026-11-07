// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (partial: the peer-address rule per network mode equals the oracle's).
//!
//! The peer-address rule per network mode (`RuntimeState::validate_peer`, `sidecar.rs:1852-1859
//! @ f4e366e9`): a local-only sidecar accepts only loopback IP peers; a sidecar in a relay mode
//! accepts any address, a routable IP (TEST-NET-3 as in the oracle's own test) or a relay-only
//! one. Expected values are the oracle's own unit tests, executed green
//! (`L9-evidence/r14-oracle-peer-rule-tests.log`): `configure_peers_rejects_duplicates_zero_
//! generations_and_mismatched_ids` (a routable peer is refused when local-only) and
//! `a_send_whose_cancellation_is_lost_still_ends_at_the_deadline` ("peer set is accepted" for the
//! same routable peer when it is not).
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{EndpointAddr, SecretKey};
use runtime::{
    iroh_host::{LoopbackHost, NetworkMode, Reach},
    iroh_pool::PooledRouter,
    sidecar_main::driver_limits,
};
use std::{net::SocketAddr, num::NonZeroUsize, sync::Arc, time::Instant};
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
        panic!("the pooled driver must not consult wall time")
    }
}
fn peer(seed: u8) -> SecretKey {
    SecretKey::from_bytes(&[seed; 32])
}
fn routable(seed: u8) -> EndpointAddr {
    let socket: SocketAddr = format!("203.0.113.{seed}:{}", 20_000 + u16::from(seed))
        .parse()
        .unwrap();
    EndpointAddr::new(peer(seed).public()).with_ip_addr(socket)
}
fn relay_only(seed: u8) -> EndpointAddr {
    EndpointAddr::new(peer(seed).public()).with_relay_url("http://127.0.0.1:1".parse().unwrap())
}
/// Starts a router on a host in `mode`, applies `check`, then closes both.
fn with_router(mode: NetworkMode, check: impl FnOnce(&PooledRouter<Mono>)) {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let clock = Arc::new(Mono(Instant::now()));
    let deadline = clock
        .monotonic()
        .checked_add(B::WorkerStartup.span(Entries::ZERO).unwrap())
        .unwrap();
    let mut host = LoopbackHost::bind_network(
        &runtime,
        &*clock,
        deadline,
        peer(0x77),
        &[Channel::Router],
        mode,
        &Reach::Hermetic("http://127.0.0.1:1".parse().unwrap()),
    )
    .unwrap();
    assert_eq!(host.mode(), mode);
    let router = PooledRouter::start(
        &host,
        clock.clone(),
        driver_limits(NonZeroUsize::new(4).unwrap()).unwrap(),
    )
    .unwrap();
    check(&router);
    runtime.block_on(router.shutdown());
    drop(router);
    let close = clock
        .monotonic()
        .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
        .unwrap();
    host.close(&*clock, close).unwrap();
}

#[test]
fn a_local_only_sidecar_refuses_a_routable_peer_and_a_relay_only_peer() {
    with_router(NetworkMode::LocalOnly, |router| {
        assert!(router.configure_peers(vec![(routable(1), 1)]).is_err());
        assert!(router.configure_peers(vec![(relay_only(2), 1)]).is_err());
    });
}

#[test]
fn a_relay_mode_sidecar_accepts_a_routable_peer_and_a_relay_only_peer() {
    for mode in [NetworkMode::Production, NetworkMode::ForceRelay] {
        with_router(mode, |router| {
            router
                .configure_peers(vec![(routable(1), 1)])
                .unwrap_or_else(|e| panic!("{mode:?}: the routable peer set is accepted: {e:?}"));
            router
                .configure_peers(vec![(relay_only(2), 2)])
                .unwrap_or_else(|e| panic!("{mode:?}: the relay-only peer set is accepted: {e:?}"));
        });
    }
}
