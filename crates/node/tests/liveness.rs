// SPDX-License-Identifier: AGPL-3.0-only
// BACKLOG 8 (live v0.1.4 incident 2026-10-27 ~21:41): a wedged operation must not stop the
// status, renew, policy and standing-consent paths, and cleanup that is slow or failing must
// stay bounded and recoverable instead of poisoning a successor. Fake clock; real threads.
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, WallExpiry};
use node::*;
use std::{
    path::PathBuf,
    sync::{
        Arc, Mutex, MutexGuard,
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
fn temp() -> Temp {
    let p = std::env::temp_dir().join(format!(
        "v02-l6-live-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    std::fs::create_dir(&p).unwrap();
    Temp(p)
}
fn ms(b: B) -> u64 {
    b.span(Entries::ZERO).unwrap().millis()
}
fn now(t: u64) -> Now {
    Now {
        mono: MonoInstant::new(ClockDomain(7), t),
        wall: WallExpiry::from_unix_millis(t),
    }
}
fn binding(node: &str) -> Binding {
    Binding {
        swarm_id: "swarm-fixture".into(),
        node_id: node.into(),
        seed_key_digest: "fixture-seed".into(),
    }
}
fn caps(owner: bool) -> Caps {
    Caps {
        storage_bytes: if owner {
            B::FreshStorageConsent
        } else {
            B::SavedStorageConsent
        }
        .bytes()
        .unwrap()
        .get(),
        memory_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
        wall_ms: ms(if owner {
            B::WorkerDefaultWall
        } else {
            B::MemberChosenWall
        }),
        commands: B::ReplayKeep.count().unwrap().get(),
    }
}
fn bounds() -> ConsentBounds {
    ConsentBounds {
        minimum_storage: budgets::Bytes::ZERO,
        policy_storage: B::FreshStorageConsent.bytes().unwrap(),
        minimum_memory: budgets::Bytes::ZERO,
        maximum_memory: B::FreshStorageConsent.bytes().unwrap(),
        device_memory_ceiling: B::FreshStorageConsent.bytes().unwrap(),
        minimum_wall: budgets::Span::ZERO,
        maximum_wall: B::WorkerMaximumWall.span(Entries::ZERO).unwrap(),
        maximum_commands: B::ReplayCapacity.count().unwrap(),
    }
}
fn member() -> control::MemberBinding {
    control::MemberBinding {
        target_node_id: "member".into(),
        target_endpoint_id: "endpoint-member".into(),
        target_key_digest: "fixture-key".into(),
        membership_generation: "2".into(),
        member_incarnation: "incarnation-2".into(),
        run_id: "fixture".into(),
        deployment_id: "fixture".into(),
    }
}
fn grant() -> Consent {
    Consent::record(
        true,
        &disclosure_digest(),
        caps(false),
        binding("member"),
        &bounds(),
        0,
    )
    .unwrap()
}
struct StubPlanner;
impl Planner for StubPlanner {
    fn plan(&mut self, m: &[PlanningMember]) -> Result<Plan> {
        Ok(Plan {
            assignments: m.iter().map(|m| (m.node.clone(), m.caps.clone())).collect(),
            stop_ms: ms(B::ObservedStop),
            start_ms: ms(B::WorkerStartup),
            safety_ms: ms(B::WorkerCleanupReserve),
        })
    }
}
fn report(c: &mut Core, t: u64) -> Result<()> {
    c.report(
        "member",
        "2",
        c.state().epoch,
        WallExpiry::from_unix_millis(t + ms(B::MembershipTtl)),
        now(t),
    )
}
fn serving(c: &mut Core, t: u64) -> Vec<Ticket> {
    c.plan(&mut StubPlanner, now(t)).unwrap();
    let tickets: Vec<_> = c
        .start(now(t))
        .unwrap()
        .into_iter()
        .map(|e| match e {
            Effect::Start { ticket, .. } => ticket,
            _ => panic!("unexpected effect"),
        })
        .collect();
    for ticket in &tickets {
        c.started(ticket, now(t).mono, now(t)).unwrap();
    }
    assert_eq!(c.state().phase, RunPhase::Serving);
    tickets
}
fn joined_serving(t: &Temp) -> (Core, Vec<Ticket>) {
    let (mut c, e) =
        Core::open(&t.0.join("node.db"), binding("owner"), caps(true), now(0)).unwrap();
    assert!(e.is_empty());
    c.join(&member()).unwrap();
    c.consent(grant()).unwrap();
    report(&mut c, 0).unwrap();
    let tickets = serving(&mut c, 0);
    (c, tickets)
}
/// Environmental observation whose outcome the test controls.
enum Cleanup {
    /// Blocks until the sender is dropped, then proves absence.
    Hung(mpsc::Receiver<()>),
    Failing,
    /// Process exited but the owned resource is still present.
    Residue,
    Gone,
}
impl WorkerAdapter for Cleanup {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        Ok(())
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        match self {
            Self::Hung(gate) => {
                // Err means the sender dropped: the wedged operation finally returned.
                assert!(gate.recv().is_err());
                Ok(Residue {
                    process_gone: true,
                    resources_gone: true,
                })
            }
            Self::Failing => Err(WorkerError::Refused("fixture_cleanup_observation_failed")),
            Self::Residue => Ok(Residue {
                process_gone: true,
                resources_gone: false,
            }),
            Self::Gone => Ok(Residue {
                process_gone: true,
                resources_gone: true,
            }),
        }
    }
}
/// The Core lock is held only for synchronous transitions. A caller that kept it across a
/// slow operation would starve every other path; fail by deadline instead of hanging.
fn core(shared: &Mutex<Core>) -> MutexGuard<'_, Core> {
    let end = Instant::now() + Duration::from_millis(ms(B::WorkerStartup));
    loop {
        match shared.try_lock() {
            Ok(guard) => return guard,
            Err(std::sync::TryLockError::Poisoned(p)) => return p.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => {
                assert!(
                    Instant::now() < end,
                    "Core lock held across a slow operation: status/renew/policy starved"
                );
                std::thread::sleep(Duration::from_millis(ms(B::OwnerPoll)));
            }
        }
    }
}
/// The documented driver: observe outside the lock, take the lock only to record.
fn drive_cleanup(
    shared: &Arc<Mutex<Core>>,
    ticket: Ticket,
    adapter: Cleanup,
    at: u64,
) -> std::thread::JoinHandle<std::result::Result<(), String>> {
    let shared = Arc::clone(shared);
    std::thread::spawn(move || {
        let observation = Supervisor::new(ticket, adapter)
            .observe_cleanup()
            .map_err(|e| format!("{e:?}"))?;
        core(&shared)
            .cleaned(&observation, now(at))
            .map_err(|e| format!("{e:?}"))
    })
}
fn other_paths_progress(shared: &Mutex<Core>, from: u64) {
    // status snapshot, lease report (renew input), tick (renew cadence/timeouts), and
    // standing-consent policy all answer immediately while cleanup is wedged.
    let status = core(shared).state().clone();
    assert_ne!(status.phase, RunPhase::Idle);
    core(shared).tick(now(from)).unwrap();
    report(&mut core(shared), from).unwrap();
    core(shared).consent(grant()).unwrap();
}
#[test]
fn hung_cleanup_never_starves_status_renew_policy_and_is_bounded_by_the_stop_deadline() {
    let t = temp();
    let (mut c, tickets) = joined_serving(&t);
    let stops = c.worker_failed(&tickets[0], now(1)).unwrap();
    assert_eq!(stops.len(), 2);
    let stop_deadline = c.state().workers[&tickets[0].node]
        .stop_deadline
        .expect("stop deadline")
        .ticks;
    let shared = Arc::new(Mutex::new(c));
    let (release, gate) = mpsc::channel::<()>();
    let hung = drive_cleanup(
        &shared,
        tickets[0].clone(),
        Cleanup::Hung(gate),
        stop_deadline + 5,
    );
    let prompt = drive_cleanup(&shared, tickets[1].clone(), Cleanup::Gone, 2);
    prompt.join().unwrap().unwrap();
    assert!(!hung.is_finished(), "wedged operation is still wedged");
    // Fake time crosses the stop deadline while the observation is still hung.
    other_paths_progress(&shared, 3);
    other_paths_progress(&shared, stop_deadline + 1);
    let during = core(&shared).state().clone();
    assert_eq!(
        during.workers.len(),
        1,
        "unproven cleanup keeps its reservation"
    );
    let kept = &during.workers[&tickets[0].node];
    assert_eq!(kept.phase, WorkerPhase::CleanupUnproven);
    assert_eq!(kept.failure.as_deref(), Some("worker_stop_timeout"));
    assert_eq!(during.phase, RunPhase::CleanupUnproven);
    assert!(!hung.is_finished());
    // The wedge clears: late proof settles exactly that ticket and a successor can run.
    drop(release);
    hung.join().unwrap().unwrap();
    let mut c = Arc::try_unwrap(shared).ok().unwrap().into_inner().unwrap();
    assert!(c.state().workers.is_empty());
    assert_eq!(c.state().phase, RunPhase::Idle);
    let later = stop_deadline + 10;
    report(&mut c, later).unwrap();
    serving(&mut c, later);
}
#[test]
fn failing_cleanup_observation_keeps_reservation_and_other_paths_keep_progressing() {
    let t = temp();
    let (mut c, tickets) = joined_serving(&t);
    c.worker_failed(&tickets[0], now(1)).unwrap();
    let shared = Arc::new(Mutex::new(c));
    let failed = drive_cleanup(&shared, tickets[0].clone(), Cleanup::Failing, 2);
    let prompt = drive_cleanup(&shared, tickets[1].clone(), Cleanup::Gone, 2);
    assert!(failed.join().unwrap().is_err());
    prompt.join().unwrap().unwrap();
    other_paths_progress(&shared, 3);
    let during = core(&shared).state().clone();
    assert!(during.workers.contains_key(&tickets[0].node));
    assert!(!during.workers.contains_key(&tickets[1].node));
    assert_ne!(
        during.phase,
        RunPhase::Idle,
        "failed proof is not a release"
    );
    // A stale or unrelated ticket cannot settle the held reservation.
    let stale = Ticket {
        incarnation: "other".into(),
        ..tickets[0].clone()
    };
    let observation = Supervisor::new(stale, Cleanup::Gone)
        .observe_cleanup()
        .unwrap();
    assert!(core(&shared).cleaned(&observation, now(4)).is_err());
    assert!(core(&shared).state().workers.contains_key(&tickets[0].node));
    // Exit without resource absence is residue, never release.
    let observation = Supervisor::new(tickets[0].clone(), Cleanup::Residue)
        .observe_cleanup()
        .unwrap();
    assert!(matches!(
        core(&shared).cleaned(&observation, now(4)),
        Err(Error::Refused("rollover_cleanup_unproven"))
    ));
    assert!(core(&shared).state().workers.contains_key(&tickets[0].node));
    // The exact ticket's real proof still releases it.
    let observation = Supervisor::new(tickets[0].clone(), Cleanup::Gone)
        .observe_cleanup()
        .unwrap();
    core(&shared).cleaned(&observation, now(5)).unwrap();
    assert_eq!(core(&shared).state().phase, RunPhase::Idle);
}
/// Core transitions are synchronous state changes whose effects are returned, never run.
/// Keep blocking primitives and effect adapters out of them: any worker, network or wait
/// would run under the application's Core lock and starve status/renew/policy loops.
#[test]
fn core_transition_modules_contain_no_locks_waits_sockets_or_process_effects() {
    const FORBIDDEN: &[&str] = &[
        "Mutex",
        "RwLock",
        "Condvar",
        "mpsc",
        "thread::sleep",
        "thread::park",
        "std::thread",
        "std::process",
        "std::net",
        "TcpStream",
        "TcpListener",
        "UdpSocket",
        "Command::new",
        "WorkerAdapter",
        "Supervisor",
        ".recv(",
        ".join()",
        "reqwest",
        "tokio",
    ];
    for (file, source) in [
        ("lifecycle.rs", include_str!("../src/lifecycle.rs")),
        ("continuation.rs", include_str!("../src/continuation.rs")),
        ("types.rs", include_str!("../src/types.rs")),
    ] {
        for token in FORBIDDEN {
            assert!(
                !source.contains(token),
                "{file} must not use `{token}`: effects are returned, never executed under the Core lock"
            );
        }
    }
}
