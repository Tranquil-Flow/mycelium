// SPDX-License-Identifier: AGPL-3.0-only
//! In-crate characterization tests that need a private seam of the pooled driver.
//!
//! The wire peer is a second real Iroh endpoint over direct loopback addresses.
//! Everything the peer observes (accepted connection, close code and reason, timing
//! of the close) is observed on its side of real QUIC; only the *ordering* of the
//! parent's rotation against its own dial completion is forced through the driver's
//! private `Shared::reusable_connection`, because that ordering is a scheduler race
//! in the shipped sidecar (`tokio::select!` between the item's cancellation and the
//! dial) that cannot be reproduced on demand from outside.
use super::*;
use crate::iroh_host::LoopbackHost;
use budgets::{BudgetId as B, ClockDomain, Entries};
use iroh::endpoint::ConnectionError;
use std::time::Instant;

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(32), self.0.elapsed().as_millis() as u64)
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the pooled driver must not consult wall time")
    }
}
/// A scenario pause, explicit at the call site (never a hidden default).
fn pause(units: u64) -> Duration {
    Duration::from_millis(units)
}
fn ms(value: u64) -> Span {
    Span::from_millis(value)
}
fn slice() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis() * 2)
}
fn limits() -> DriverLimits {
    DriverLimits {
        pool: PoolLimits {
            capacity: 4,
            seen_factor: 8,
            seen_floor: 64,
            initial_retry: ms(200),
            max_retry: ms(400),
            lifetime: ms(20_000),
        },
        connect: ms(5_000),
        stream_io: ms(5_000),
    }
}
fn close_text(error: &ConnectionError) -> String {
    match error {
        ConnectionError::ApplicationClosed(close) => format!(
            "code={} reason={}",
            close.error_code.into_inner(),
            String::from_utf8_lossy(&close.reason)
        ),
        other => format!("other={other}"),
    }
}

/// v0.1.4 (`sidecar.rs:2027-2048`): a dial that completes after its binding was
/// rotated away is inserted into the pool under the DEAD `(EndpointId, generation)`
/// key and stays open; nothing closes it until the next rotation
/// (`close_outbound_connections`, `peer_rotated`) or shutdown. The remote peer
/// therefore observes an open, idle connection (and keeps its inbound connection
/// slot) instead of an immediate code-8 close. Integrator ruling: this is
/// wire-observable (close frame, timing of the close, peer slot exhaustion), so the
/// port matches v0.1.4. V03: parking under a dead key is a leak the next
/// generation should close; this test pins today's behaviour until then.
#[test]
fn v03_a_dial_finishing_after_rotation_is_parked_under_the_dead_key_and_stays_open() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let clock = Arc::new(Mono(Instant::now()));
    let deadline = clock.monotonic().checked_add(ms(10_000)).unwrap();
    let mut local = LoopbackHost::bind(&rt, &*clock, deadline).unwrap();
    let mut remote = LoopbackHost::bind(&rt, &*clock, deadline).unwrap();
    let router = PooledRouter::start(&local, clock.clone(), limits()).unwrap();
    let remote_address = remote.address().unwrap();
    let remote_key = *remote_address.id.as_bytes();
    router
        .configure_peers(vec![(remote_address.clone(), 1)])
        .unwrap();

    // The dial starts while the peer has not accepted anything yet.
    let shared = router.shared.clone();
    let dial = rt.spawn(async move {
        shared
            .reusable_connection(PeerBinding {
                endpoint: remote_key,
                generation: 1,
            })
            .await
            .is_ok()
    });
    rt.block_on(async {
        let end = tokio::time::Instant::now() + slice();
        while router.stats().dials == 0 {
            assert!(tokio::time::Instant::now() < end, "dial never started");
            tokio::time::sleep(pause(5)).await;
        }
    });

    // Rotation lands while that dial is still pending: the binding is now gen 2.
    let outcome = router
        .configure_peers(vec![(remote_address.clone(), 2)])
        .unwrap();
    assert!(outcome.changed);
    assert_eq!(router.pooled_connections(), 0);

    // The peer now completes the handshake: the dial finishes after the rotation.
    let accepted = remote.accept(&*clock, deadline).unwrap();
    assert!(
        rt.block_on(async { tokio::time::timeout(slice(), dial).await })
            .expect("the dial finishes once the peer accepts")
            .unwrap(),
        "v0.1.4 hands the connection to the attempt"
    );
    assert_eq!(
        router.pooled_connections(),
        1,
        "parked under the dead (id, 1) key"
    );
    let closed_early = rt.block_on(async {
        tokio::time::timeout(pause(600), accepted.closed())
            .await
            .map(|error| close_text(&error))
    });
    assert!(
        closed_early.is_err(),
        "the peer must see an open, idle connection, got {closed_early:?}"
    );

    // The next effective rotation closes everything still in the pool.
    router.configure_peers(vec![(remote_address, 3)]).unwrap();
    let closed = rt.block_on(async { tokio::time::timeout(slice(), accepted.closed()).await });
    assert_eq!(
        close_text(&closed.expect("closed by the next rotation")),
        "code=8 reason=peer_rotated"
    );
    assert_eq!(router.pooled_connections(), 0);

    rt.block_on(router.shutdown());
    drop(router);
    local.close(&*clock, deadline).unwrap();
    remote.close(&*clock, deadline).unwrap();
}

impl<C: Clock + Send + Sync + 'static> Shared<C> {
    /// M23x invariant: the state's pool registry and the driver's connection map hold
    /// exactly the same keys. Both are read under the driver's lock order
    /// (connections, then state).
    fn pool_keys_agree(&self) -> bool {
        let connections = locked(&self.connections);
        let state = locked(&self.state);
        connections.keys().copied().collect::<Vec<_>>() == state.pool_keys()
    }
}

/// M23x was recorded as "equivalent" because the registry mirrors the map. It mirrors it
/// only if rotation (state update + map close) and eviction are atomic with respect to a
/// dial registering a connection: this forces a dial that finishes between the state's
/// rotation and the map close, and requires the two containers to agree afterwards.
#[test]
fn m23x_a_dial_landing_between_the_state_rotation_and_the_map_close_keeps_registry_and_map_equal() {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let clock = Arc::new(Mono(Instant::now()));
    let deadline = clock.monotonic().checked_add(ms(10_000)).unwrap();
    let mut local = LoopbackHost::bind(&rt, &*clock, deadline).unwrap();
    let mut remote = LoopbackHost::bind(&rt, &*clock, deadline).unwrap();
    let router = PooledRouter::start(&local, clock.clone(), limits()).unwrap();
    let remote_address = remote.address().unwrap();
    let remote_key = *remote_address.id.as_bytes();
    // The remote accepts and holds every connection so dials complete on their own.
    let held = Arc::new(Mutex::new(Vec::new()));
    let acceptor = {
        let endpoint = remote.endpoint().unwrap().clone();
        let held = held.clone();
        rt.spawn(async move {
            while let Some(incoming) = endpoint.accept().await {
                if let Ok(connection) = incoming.await {
                    locked(&held).push(connection);
                }
            }
        })
    };
    router
        .configure_peers(vec![(remote_address.clone(), 1)])
        .unwrap();

    let shared = router.shared.clone();
    let landed = Arc::new(Mutex::new(None::<tokio::task::JoinHandle<bool>>));
    {
        let (shared, landed, handle) = (shared.clone(), landed.clone(), rt.handle().clone());
        *locked(&router.shared.rotation_gap) = Some(Box::new(move || {
            let dialing = shared.clone();
            let task = handle.spawn(async move {
                dialing
                    .reusable_connection(PeerBinding {
                        endpoint: remote_key,
                        generation: 2,
                    })
                    .await
                    .is_ok()
            });
            *locked(&landed) = Some(task);
            // Give the dial time to finish inside the gap if the driver lets it.
            std::thread::sleep(pause(700));
        }));
    }
    let outcome = router
        .configure_peers(vec![(remote_address.clone(), 2)])
        .unwrap();
    assert!(outcome.changed);
    *locked(&router.shared.rotation_gap) = None;
    let task = locked(&landed).take().unwrap();
    assert!(
        rt.block_on(async { tokio::time::timeout(slice() * 4, task).await })
            .unwrap()
            .unwrap()
    );
    assert!(
        shared.pool_keys_agree(),
        "registry {:?} vs map {:?}",
        locked(&shared.state).pool_keys(),
        locked(&shared.connections).keys().collect::<Vec<_>>()
    );
    assert_eq!(router.pooled_connections(), 1);

    rt.block_on(router.shutdown());
    drop(router);
    acceptor.abort();
    local.close(&*clock, deadline).unwrap();
    remote.close(&*clock, deadline).unwrap();
}

/// Randomized (seeded, so reproducible) dial / stale-dial / evict / rotate / no-op-rotate
/// interleavings over two peers: after every operation the state's pool registry and the
/// driver's connection map hold the same keys.
#[test]
fn m23x_registry_and_map_hold_the_same_keys_across_randomized_dial_evict_rotate_interleavings() {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let clock = Arc::new(Mono(Instant::now()));
    let deadline = clock.monotonic().checked_add(ms(30_000)).unwrap();
    let mut local = LoopbackHost::bind(&rt, &*clock, deadline).unwrap();
    let mut peers = [
        LoopbackHost::bind(&rt, &*clock, deadline).unwrap(),
        LoopbackHost::bind(&rt, &*clock, deadline).unwrap(),
    ];
    let router = PooledRouter::start(&local, clock.clone(), limits()).unwrap();
    let held = Arc::new(Mutex::new(Vec::new()));
    let acceptors: Vec<_> = peers
        .iter()
        .map(|peer| {
            let endpoint = peer.endpoint().unwrap().clone();
            let held = held.clone();
            rt.spawn(async move {
                while let Some(incoming) = endpoint.accept().await {
                    if let Ok(connection) = incoming.await {
                        locked(&held).push(connection);
                    }
                }
            })
        })
        .collect();
    let addresses: Vec<_> = peers.iter().map(|p| p.address().unwrap()).collect();
    let mut generations = [1_u64, 1];
    let configure = |generations: &[u64; 2]| {
        router.configure_peers(vec![
            (addresses[0].clone(), generations[0]),
            (addresses[1].clone(), generations[1]),
        ])
    };
    configure(&generations).unwrap();
    let mut seed = 0x9e37_79b9_7f4a_7c15_u64;
    let mut next = move || {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        seed
    };
    let mut histogram = [0_u32; 5];
    // [refused, served] outcomes of the dial operations, so no dial result goes unobserved.
    let mut dial_results = [0_u32; 2];
    for step in 0..80 {
        let which = (next() % 2) as usize;
        let key = *addresses[which].id.as_bytes();
        let operation = (next() % 5) as usize;
        histogram[operation] += 1;
        match operation {
            0 => {
                let dialed = rt.block_on(router.shared.reusable_connection(PeerBinding {
                    endpoint: key,
                    generation: generations[which],
                }));
                dial_results[usize::from(dialed.is_ok())] += 1;
            }
            1 => router.shared.evict_connection(PeerBinding {
                endpoint: key,
                generation: generations[which],
            }),
            2 => {
                // A changed set must advance every binding (the sidecar's staleness rule).
                generations[0] += 1;
                generations[1] += 1;
                configure(&generations).unwrap();
            }
            3 => {
                configure(&generations).unwrap();
            }
            _ => {
                let stale = generations[which].saturating_sub(1).max(1);
                let dialed = rt.block_on(router.shared.reusable_connection(PeerBinding {
                    endpoint: key,
                    generation: stale,
                }));
                dial_results[usize::from(dialed.is_ok())] += 1;
            }
        }
        assert!(
            router.shared.pool_keys_agree(),
            "step {step} op {operation}: registry {:?} vs map {:?}",
            locked(&router.shared.state).pool_keys(),
            locked(&router.shared.connections)
                .keys()
                .collect::<Vec<_>>()
        );
    }
    assert!(
        histogram.iter().all(|count| *count > 0),
        "every operation kind ran: {histogram:?}"
    );
    assert_eq!(
        dial_results.iter().sum::<u32>(),
        histogram[0] + histogram[4],
        "every dial operation was observed: {dial_results:?} of {histogram:?}"
    );
    rt.block_on(router.shutdown());
    drop(router);
    for acceptor in acceptors {
        acceptor.abort();
    }
    local.close(&*clock, deadline).unwrap();
    for peer in &mut peers {
        peer.close(&*clock, deadline).unwrap();
    }
}
