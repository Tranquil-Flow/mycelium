// SPDX-License-Identifier: AGPL-3.0-only
//! Direct-LAN pin-table availability (lane B9f, REVIEW-laneB9 F2).
//!
//! An unauthenticated LAN host that completes handshakes with throwaway keys
//! must not exhaust the exact-pin table this host needs for its authenticated
//! peers: not the destinations it dials from signed data, and not the peer keys
//! it admits from signed data. Every case runs on 127.0.0.1 (explicitly allowed)
//! and never contacts another host.
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{Endpoint, EndpointAddr, RelayMode, SecretKey, endpoint::presets};
use runtime::iroh_host::{LanPolicy, LoopbackHost};
use std::{
    net::{IpAddr, SocketAddr},
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
fn loopback_ip() -> IpAddr {
    "127.0.0.1".parse().unwrap()
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
fn host<'a>(rt: &'a tokio::runtime::Runtime, clock: &Mono) -> LoopbackHost<'a> {
    LoopbackHost::bind_lan(
        rt,
        clock,
        deadline(clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        loopback_ip(),
        LanPolicy::with_explicit(vec![loopback_ip()]).unwrap(),
    )
    .unwrap()
}
/// A plain iroh endpoint on its own fresh 127.0.0.1 socket and key.
async fn raw_endpoint() -> (Endpoint, SocketAddr) {
    let endpoint = Endpoint::builder(presets::Minimal)
        .portmapper_config(iroh::endpoint::PortmapperConfig::Disabled)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .clear_ip_transports()
        .bind_addr(SocketAddr::new(loopback_ip(), 0))
        .unwrap()
        .alpns(vec![Channel::Router.alpn().to_vec()])
        .bind()
        .await
        .unwrap();
    let socket = endpoint.bound_sockets()[0];
    (endpoint, socket)
}
/// Throwaway-key dialers, each from its own socket, all dialing `target` at
/// once and holding whatever they get. Dropped (aborted + closed) by `stop`.
struct Churn {
    endpoints: Vec<Endpoint>,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}
impl Churn {
    fn start(rt: &tokio::runtime::Runtime, target: &EndpointAddr, n: usize) -> Self {
        let endpoints: Vec<Endpoint> = rt.block_on(async {
            let mut endpoints = Vec::with_capacity(n);
            for _ in 0..n {
                endpoints.push(raw_endpoint().await.0);
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
                eprintln!("throwaway endpoints still closing after one RouterClose");
            }
        });
    }
}
/// The product callers' loop (`joined_shard/serve.rs`): accept, close any
/// connection whose key is not the expected one, keep accepting. Runs for one
/// `RouterClose` window or until `n` connections were rejected; returns how
/// many the caller rejected and the first error that was not one refused
/// inbound (those are counted and printed).
fn reject_churn(
    host: &LoopbackHost<'_>,
    clock: &Mono,
    expected: iroh::EndpointId,
    n: usize,
) -> (usize, Option<Error>) {
    let window = deadline(clock, B::RouterClose);
    let (mut rejected, mut refused) = (0, 0);
    let error = loop {
        if rejected >= n {
            break None;
        }
        match host.accept(clock, window) {
            Ok(connection) if connection.remote_id() != expected => {
                connection.close(2u32.into(), b"unpinned route owner");
                rejected += 1;
            }
            Ok(_) => panic!("the expected peer is not dialing yet"),
            Err(Error::Authentication) => refused += 1,
            Err(Error::Expired) => break None,
            Err(other) => break Some(other),
        }
    };
    eprintln!("host refused {refused} inbound handshakes itself");
    (rejected, error)
}
fn churn_size() -> usize {
    count(B::SidecarQueueCapacity) + count(B::RelayConnectionSlots)
}

/// F2 (dial side): after a burst of throwaway-key handshakes the caller
/// rejected, this host must still dial an authenticated peer at its signed
/// address.
#[test]
fn throwaway_key_handshakes_do_not_block_an_authenticated_dial() {
    let rt = runtime(1);
    let peers = runtime(2);
    let clock = Mono(Instant::now());
    let mut ours = host(&rt, &clock);
    let (legit, legit_socket) = peers.block_on(raw_endpoint());
    let serving = legit.clone();
    let served = peers.spawn(async move {
        let connection = serving.accept().await.unwrap().await.unwrap();
        let _reason = connection.closed().await;
    });
    let churn = Churn::start(&peers, &ours.address().unwrap(), churn_size());
    let (rejected, error) = reject_churn(&ours, &clock, legit.id(), churn_size());
    eprintln!("caller rejected {rejected} throwaway handshakes; accept error {error:?}");
    let dialed = ours.connect(
        &EndpointAddr::new(legit.id()).with_ip_addr(legit_socket),
        &clock,
        deadline(&clock, B::SidecarLocalHandshake),
    );
    let outcome = dialed.as_ref().map(|c| c.remote_id()).map_err(|e| *e);
    churn.stop(&peers);
    if let Ok(connection) = dialed {
        connection.close(0u32.into(), b"done");
    }
    served.abort();
    ours.close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    peers.block_on(legit.close());
    assert_eq!(
        outcome,
        Ok(legit.id()),
        "authenticated dial after {rejected} rejected throwaway handshakes"
    );
}

/// F2 (accept side): with the expected peer key admitted from signed data, a
/// handshake under any other key is refused by the host itself (never handed to
/// the caller) and its pin is released at once, without a close drain.
#[test]
fn a_handshake_under_an_unadmitted_key_is_refused_and_unpinned_at_once() {
    let rt = runtime(1);
    let peers = runtime(2);
    let clock = Mono(Instant::now());
    let mut ours = host(&rt, &clock);
    let addr = ours.address().unwrap();
    let (legit, legit_socket) = peers.block_on(raw_endpoint());
    ours.admit_peer_ids(&[legit.id()]).unwrap();
    let (stranger, stranger_socket) = peers.block_on(raw_endpoint());
    let dialing = stranger.clone();
    let target = addr.clone();
    let stranger_dial = peers.spawn(async move {
        if let Ok(connection) = dialing.connect(target, Channel::Router.alpn()).await {
            let _reason = connection.closed().await;
        }
    });
    let first = ours.accept(&clock, deadline(&clock, B::SidecarLocalHandshake));
    let first = first.map(|c| {
        let id = c.remote_id();
        c.close(2u32.into(), b"unpinned route owner");
        id
    });
    let stranger_pinned = ours.lan_admits(stranger_socket);
    let dialing = legit.clone();
    let legit_dial = peers.spawn(async move {
        let connection = dialing.connect(addr, Channel::Router.alpn()).await.unwrap();
        let _reason = connection.closed().await;
    });
    let admitted = loop {
        match ours.accept(&clock, deadline(&clock, B::WorkerStartup)) {
            Err(Error::Authentication) => continue,
            other => break other,
        }
    };
    let admitted_id = admitted.as_ref().map(|c| c.remote_id()).map_err(|e| *e);
    let legit_pinned = ours.lan_admits(legit_socket);
    if let Ok(connection) = admitted {
        connection.close(0u32.into(), b"done");
    }
    stranger_dial.abort();
    legit_dial.abort();
    ours.close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    peers.block_on(async {
        stranger.close().await;
        legit.close().await;
    });
    assert_eq!(
        first,
        Err(Error::Authentication),
        "a handshake under an unadmitted key reached the caller"
    );
    assert_eq!(
        stranger_pinned,
        Some(false),
        "the refused handshake's source is still pinned (close drain)"
    );
    assert_eq!(
        admitted_id,
        Ok(legit.id()),
        "the admitted key was not accepted"
    );
    assert_eq!(legit_pinned, Some(true), "the admitted peer is not pinned");
}

/// F2 (accept side, availability): right after a burst of throwaway-key
/// handshakes, the admitted peer key is still accepted.
#[test]
fn throwaway_key_handshakes_do_not_block_an_admitted_accept() {
    let rt = runtime(1);
    let peers = runtime(2);
    let clock = Mono(Instant::now());
    let mut ours = host(&rt, &clock);
    let addr = ours.address().unwrap();
    let (legit, _) = peers.block_on(raw_endpoint());
    ours.admit_peer_ids(&[legit.id()]).unwrap();
    let churn = Churn::start(&peers, &addr, churn_size());
    let (rejected, error) = reject_churn(&ours, &clock, legit.id(), churn_size());
    eprintln!("caller rejected {rejected} throwaway handshakes; accept error {error:?}");
    churn.stop(&peers);
    let dialing = legit.clone();
    let legit_dial = peers.spawn(async move {
        let connection = dialing.connect(addr, Channel::Router.alpn()).await.unwrap();
        let _reason = connection.closed().await;
    });
    let window = deadline(&clock, B::WorkerStartup);
    let admitted = loop {
        match ours.accept(&clock, window) {
            Err(Error::Authentication) => continue,
            Ok(c) if c.remote_id() != legit.id() => {
                c.close(2u32.into(), b"unpinned route owner");
                continue;
            }
            other => break other,
        }
    };
    let outcome = admitted.as_ref().map(|c| c.remote_id()).map_err(|e| *e);
    if let Ok(connection) = admitted {
        connection.close(0u32.into(), b"done");
    }
    legit_dial.abort();
    ours.close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    peers.block_on(legit.close());
    assert_eq!(
        outcome,
        Ok(legit.id()),
        "admitted accept right after {rejected} rejected throwaway handshakes"
    );
}
