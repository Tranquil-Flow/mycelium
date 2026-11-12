// SPDX-License-Identifier: AGPL-3.0-only
//! EP2G: real Work/Dispatcher, custody, consent and signed-route verification.
//! The resident proof is explicitly controlled; NO native process/model is claimed.
use super::*;
use std::sync::mpsc;

impl Member for &Joined {
    fn session(&self) -> &Arc<Mutex<Option<Session>>> {
        (*self).session()
    }
    fn current(&self, signer: &Identity) -> Result<Value, api::compiler_inputs::Error> {
        (*self).current(signer)
    }
}

#[derive(Clone, Copy, Debug)]
enum End {
    Cancel,
    Withdraw,
    Membership,
    Expiry,
}
struct ObservedMember<'a> {
    member: &'a Joined,
    reads: AtomicU64,
    entered: mpsc::Sender<()>,
    proceed: Mutex<mpsc::Receiver<()>>,
    lost: AtomicBool,
}
impl Member for ObservedMember<'_> {
    fn session(&self) -> &Arc<Mutex<Option<Session>>> {
        self.member.session()
    }
    fn current(&self, signer: &Identity) -> Result<Value, api::compiler_inputs::Error> {
        // Work verifies first, then serve verifies. Hold the second read before
        // accept; this milestone exists on the unfixed implementation as well.
        if self.reads.fetch_add(1, Ordering::SeqCst) == 1 {
            let row = self.member.current(signer);
            self.entered.send(()).unwrap();
            self.proceed
                .lock()
                .unwrap()
                .recv_timeout(test_span(B::RouterClose))
                .unwrap();
            return row;
        }
        if self.lost.load(Ordering::SeqCst) {
            return Err(api::compiler_inputs::Error("joined_shard_lease_expired"));
        }
        self.member.current(signer)
    }
}
fn setup(owner_connections: usize, end: End) {
    let member = Joined::new();
    let wall = test_span(B::WorkerStartup) * 2;
    member.grant_wall(wall);
    member.bind();
    let mut work = idle_work(&member);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = api::managed_carrier::HostClock::default();
    let until = || {
        clock
            .monotonic()
            .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
            .unwrap()
    };
    let mut host = LoopbackHost::bind(&rt, &clock, until()).unwrap();
    let address = host.address().unwrap();
    let owner = iroh::SecretKey::generate();
    let mut dialer = LoopbackHost::bind_with_secret(&rt, &clock, until(), owner.clone()).unwrap();
    let lifetime = if matches!(end, End::Expiry) {
        test_span(B::RouterClose)
    } else {
        wall * 2
    };
    let expires = app_host::now() + lifetime.as_secs_f64();
    let route = idle_route(&member, &work, owner.public(), json!(expires));
    let (dispatcher, queue) = work::Dispatcher::new().unwrap();
    dispatcher
        .admit(
            json!({"command":"serve","operation_id":3,"route":route}),
            Origin::Remote,
        )
        .unwrap();
    let operation = queue.recv().unwrap().unwrap();
    let (entered, entry) = mpsc::channel();
    let (release, proceed) = mpsc::channel();
    let observed = ObservedMember {
        member: &member,
        reads: AtomicU64::new(0),
        entered,
        proceed: Mutex::new(proceed),
        lost: AtomicBool::new(false),
    };
    let (done, wait) = mpsc::channel();
    let (in_bound, result, elapsed, first_closed) = std::thread::scope(|scope| {
        let serving = scope.spawn(|| {
            let out = work.serve(
                Some(&observed),
                &Carrier {
                    host: &host,
                    runtime: &rt,
                    clock: &clock,
                    endpoint: address.id,
                    lan: None,
                },
                &member.signer,
                &operation,
            );
            dispatcher
                .complete(&operation, json!({"served":out}), |_| Ok(()))
                .unwrap();
            done.send(()).unwrap();
            out
        });
        entry.recv_timeout(test_span(B::RouterClose)).unwrap();
        release.send(()).unwrap();
        let mut connections = Vec::new();
        for _ in 0..owner_connections {
            connections.push(
                dialer
                    .connect_channel(&address, transport::Channel::Router, &clock, until())
                    .unwrap(),
            );
        }
        // In the one-dial case connect returned only after the authenticated
        // handshake; no second dial is made unless rescuing a failing RED.
        let began = Instant::now();
        match end {
            End::Cancel => {
                assert_eq!(
                    dispatcher.cancel(Some(3)).unwrap()["cancel_requested"],
                    true
                );
            }
            End::Withdraw => {
                standing_consent::withdraw(&member.member_root, app_host::now()).unwrap();
            }
            End::Membership => {
                observed.lost.store(true, Ordering::SeqCst);
            }
            End::Expiry => {}
        }
        let observation = test_span(B::OwnerPoll) * 2;
        let bound = if matches!(end, End::Expiry) {
            lifetime + observation
        } else {
            observation + test_span(B::RouterClose)
        };
        let in_bound = wait.recv_timeout(bound).is_ok();
        let elapsed = began.elapsed();
        let first_closed = connections.first().is_none_or(|c| {
            rt.block_on(async { tokio::time::timeout(observation, c.closed()).await.is_ok() })
        });
        if !in_bound {
            // Rescue solely after the failing observation, never counted as a
            // pass. Keep all threads owned even for removal mutants.
            operation.cancelled.store(true, Ordering::SeqCst);
            while connections.len() < 2 {
                connections.push(
                    dialer
                        .connect_channel(&address, transport::Channel::Router, &clock, until())
                        .unwrap(),
                );
            }
            wait.recv_timeout(test_span(B::CancelAndCleanup)).unwrap();
        }
        let out = serving.join().unwrap();
        (in_bound, out, elapsed, first_closed)
    });
    drop(observed);
    dialer.close(&clock, until()).unwrap();
    host.close(&clock, until()).unwrap();
    // Actual public dispatcher and Work operations, not just fake completed replies.
    dispatcher
        .admit(
            json!({"command":"discard","operation_id":4}),
            Origin::Remote,
        )
        .unwrap();
    let discard = queue.recv().unwrap().unwrap();
    let discarded = work.discard().unwrap();
    dispatcher
        .complete(&discard, discarded, |_| Ok(()))
        .unwrap();
    dispatcher
        .admit(
            json!({"command":"leave","mode":"instant","operation_id":5}),
            Origin::Remote,
        )
        .unwrap();
    let leave = queue.recv().unwrap().unwrap();
    // Keep Joined (which owns the scratch root) alive while leave drops only
    // the membership view; deleting the fixture root at disconnect is not product behavior.
    let mut membership = Some(&member);
    let left = work.leave(&mut membership, &member.signer, &leave);
    dispatcher
        .complete(&leave, json!({"left":left}), |_| Ok(()))
        .unwrap();
    eprintln!(
        "EP2G setup {end:?} dials={owner_connections} elapsed={elapsed:?} result={result:?} leave={left:?}"
    );
    assert!(
        in_bound,
        "setup held public discard/leave beyond short observation+cleanup budget: {elapsed:?}"
    );
    assert!(
        first_closed,
        "first authenticated connection must close before serve returns"
    );
    assert!(
        left.is_ok(),
        "public leave progresses after setup: {left:?}"
    );
    let expected = match end {
        End::Cancel => "member_operation_cancelled",
        End::Withdraw => "joined_shard_consent_required",
        End::Membership => "joined_shard_lease_expired",
        End::Expiry => "route_expired",
    };
    assert_eq!(result, Err(expected.into()));
}
#[test]
fn ep2g_setup_zero_dials_cancel() {
    setup(0, End::Cancel);
}
#[test]
fn ep2g_setup_one_dial_cancel() {
    setup(1, End::Cancel);
}
#[test]
fn ep2g_setup_zero_dials_withdrawal() {
    setup(0, End::Withdraw);
}
#[test]
fn ep2g_setup_one_dial_withdrawal() {
    setup(1, End::Withdraw);
}
#[test]
fn ep2g_setup_zero_dials_membership_loss() {
    setup(0, End::Membership);
}
#[test]
fn ep2g_setup_one_dial_membership_loss() {
    setup(1, End::Membership);
}
#[test]
fn ep2g_setup_zero_dials_signed_expiry() {
    setup(0, End::Expiry);
}
#[test]
fn ep2g_setup_one_dial_signed_expiry() {
    setup(1, End::Expiry);
}
