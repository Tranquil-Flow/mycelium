// SPDX-License-Identifier: AGPL-3.0-only
//! Setup adversaries over real QUIC; Controlled is NOT native compute/reap evidence.
use super::*;

#[derive(Clone, Copy, Debug)]
enum End {
    Cancel,
    Consent,
    OperationDeadline,
    RouteExpiry,
}
fn traffic(end: End) {
    let seed = Identity::from_secret("test-seed", &[7; 32]).unwrap();
    let rows: Vec<_> = ["node-a", "node-b"].iter().map(|n| json!({"node_id":n,"generation":1,"swarm_id":"swarm","seed_key_digest":seed.verification_key_digest()})).collect();
    let offers: Vec<_> = (0..2).map(|i| json!({"controlled_offer":i})).collect();
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = HostClock::default();
    let until = |duration: Duration| {
        clock
            .monotonic()
            .checked_add(budgets::Span::from_millis(
                duration.as_millis().try_into().unwrap(),
            ))
            .unwrap()
    };
    let mut host = LoopbackHost::bind(&rt, &clock, until(span(B::RouterClose))).unwrap();
    let addr = host.address().unwrap();
    let owner = iroh::SecretKey::generate();
    let mut rescue =
        LoopbackHost::bind_with_secret(&rt, &clock, until(span(B::RouterClose)), owner.clone())
            .unwrap();
    let mut attacker = LoopbackHost::bind(&rt, &clock, until(span(B::RouterClose))).unwrap();
    let lifetime = if matches!(end, End::RouteExpiry) {
        span(B::RouterClose)
    } else {
        span(B::MemberChosenWall)
    };
    let (_, route) = signed_route_as(
        route::ROUTE_V2,
        lifetime,
        &seed,
        "swarm",
        &rows,
        &offers,
        &[addr.id, addr.id],
        &owner,
    );
    let cancelled = Arc::new(AtomicBool::new(false));
    let consent = Arc::new(AtomicBool::new(false));
    let log = Arc::new(Mutex::new(Log::default()));
    let mut compute = Controlled {
        offer: offers[0].clone(),
        last: false,
        owned: true,
        script: Script {
            consent_changed: Some(consent.clone()),
            ..Default::default()
        },
        log: log.clone(),
        position: 0,
    };
    let (entered, entry) = mpsc::channel();
    let (release, proceed) = mpsc::channel();
    let proceed = Mutex::new(proceed);
    let calls = AtomicU64::new(0);
    let current = || {
        if calls.fetch_add(1, Ordering::SeqCst) == 0 {
            entered.send(()).unwrap();
            proceed
                .lock()
                .unwrap()
                .recv_timeout(span(B::RouterClose))
                .unwrap();
        }
        Ok(rows[0].clone())
    };
    let deadline = Instant::now()
        + if matches!(end, End::OperationDeadline) {
            span(B::RouterClose)
        } else {
            span(B::MemberChosenWall)
        };
    let (done, wait) = mpsc::channel();
    let (within, result, accepted) = std::thread::scope(|scope| {
        let serving = scope.spawn(|| {
            let result = serve::serve(
                &serve::Serve {
                    host: &host,
                    runtime: &rt,
                    clock: &clock,
                    current: &current,
                    cancelled: cancelled.clone(),
                    deadline,
                },
                &mut compute,
                &route,
                &proof(0),
            );
            done.send(()).unwrap();
            result
        });
        entry.recv_timeout(span(B::RouterClose)).unwrap();
        release.send(()).unwrap();
        // The first owner connection is authenticated and held. No second one.
        let first = rescue
            .connect_channel(
                &addr,
                transport::Channel::Router,
                &clock,
                until(span(B::RouterClose)),
            )
            .unwrap();
        // Unpinned traffic must NOT itself end the epoch (remote kill switch).
        for _ in 0..3 {
            let stray = attacker
                .connect_channel(
                    &addr,
                    transport::Channel::Router,
                    &clock,
                    until(span(B::RouterClose)),
                )
                .unwrap();
            assert!(
                rt.block_on(async {
                    tokio::time::timeout(span(B::RouterClose), stray.closed()).await
                })
                .is_ok()
            );
            assert!(matches!(wait.try_recv(), Err(mpsc::TryRecvError::Empty)));
        }
        match end {
            End::Cancel => cancelled.store(true, Ordering::SeqCst),
            End::Consent => consent.store(true, Ordering::SeqCst),
            _ => {}
        }
        let observed = Instant::now();
        let bound = if matches!(end, End::OperationDeadline | End::RouteExpiry) {
            span(B::RouterClose) + span(B::OwnerPoll) * 2
        } else {
            span(B::OwnerPoll) * 2
        };
        let mut accepted = 0;
        let within = loop {
            if observed.elapsed() >= bound {
                break false;
            }
            // Explicit hostile traffic cadence BELOW OwnerPoll, not a sleep
            // guessing readiness; the completion channel is the observation.
            if wait.recv_timeout(span(B::OwnerPoll) / 4).is_ok() {
                break true;
            }
            if let Ok(c) = attacker.connect_channel(
                &addr,
                transport::Channel::Router,
                &clock,
                until(span(B::OwnerPoll) / 4),
            ) {
                accepted += 1;
                c.close(2u32.into(), b"test unpinned traffic");
            }
        };
        let first_closed = rt
            .block_on(async { tokio::time::timeout(span(B::OwnerPoll) * 2, first.closed()).await })
            .is_ok();
        if !within {
            cancelled.store(true, Ordering::SeqCst);
            let second = rescue.connect_channel(
                &addr,
                transport::Channel::Router,
                &clock,
                until(span(B::RouterClose)),
            );
            wait.recv_timeout(span(B::CancelAndCleanup)).unwrap();
            drop(second);
        }
        let result = serving.join().unwrap();
        // Host remains bound here: closure must come from serve, not host Drop.
        (within && first_closed, result, accepted)
    });
    for host in [&mut attacker, &mut rescue, &mut host] {
        host.close(&clock, until(span(B::RouterClose))).unwrap();
    }
    let events = log.lock().unwrap().events.clone();
    eprintln!(
        "EP2G traffic {end:?} in_bound={within} accepted={accepted} result={result:?} events={events:?}"
    );
    assert!(
        within,
        "unpinned traffic extended setup or left its first connection open"
    );
    assert!(!compute.owned);
    assert_eq!(events, [("abort", 0)]);
    assert_eq!(
        result,
        Err(match end {
            End::Cancel => "member_operation_cancelled",
            End::Consent => "joined_native_consent_changed",
            End::OperationDeadline => "member_operation_deadline",
            End::RouteExpiry => "route_expired",
        }
        .into())
    );
}
#[test]
fn ep2g_setup_unpinned_traffic_cannot_mask_cancel() {
    traffic(End::Cancel);
}
#[test]
fn ep2g_setup_unpinned_traffic_cannot_mask_consent() {
    traffic(End::Consent);
}
#[test]
fn ep2g_setup_unpinned_traffic_cannot_extend_operation_deadline() {
    traffic(End::OperationDeadline);
}
#[test]
fn ep2g_setup_unpinned_traffic_cannot_extend_signed_expiry() {
    traffic(End::RouteExpiry);
}
