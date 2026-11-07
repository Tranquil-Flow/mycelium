// SPDX-License-Identifier: AGPL-3.0-only
//! Round 7: the port of v0.1.4's `ManagedWorker._watch` (0.25 s, `worker_native_exited`).
//! Real owned children, killed by exact pid; nothing is simulated. The native and desktop
//! end-to-end behavior is qualified in `worker_death_native.rs` / `worker_death_desktop.rs`.
use backend_mlx::{
    process::{OwnedWorker, ProcessLimits},
    resources::ResourcePool,
};
use budgets::{BudgetId as B, Bytes, Entries, Reservation};
use node_service::{
    liveness::{NATIVE_EXITED, Watch, WorkerExited},
    spawn,
};
use rustix::process::{Pid, Signal, kill_process};
use std::{
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// The registered v0.1.4 `_watch` wait.
fn period() -> Duration {
    span(B::PyMyceliumA13MacosWorkerManagedworkerWatchSelfClosedWaitL1460)
}
fn worker(script: &str) -> OwnedWorker {
    let mut command = Command::new("/bin/sh");
    command.args(["-c", script]);
    let reservation = Reservation {
        resident: Bytes::new(1),
        staged: Bytes::ZERO,
        staging_overhead: Bytes::ZERO,
        scratch: Bytes::ZERO,
        kv: Bytes::ZERO,
    };
    spawn::spawn_worker(
        command,
        ResourcePool::new(reservation.total().unwrap()),
        reservation,
        ProcessLimits {
            deadline: Instant::now() + span(B::MemberChosenWall),
            close: B::RouterClose.span(Entries::ZERO).unwrap(),
            reap: B::WorkerCleanupReserve.span(Entries::ZERO).unwrap(),
            poll: B::RegistryPoll.span(Entries::ZERO).unwrap(),
        },
    )
    .unwrap()
}
/// A worker that stays alive until its stdin (the owner's pipe) closes.
const ALIVE: &str = "exec cat";
fn until(what: &str, limit: Duration, mut done: impl FnMut() -> bool) -> Duration {
    let begun = Instant::now();
    while !done() {
        assert!(begun.elapsed() < limit, "{what}: not within {limit:?}");
        std::thread::sleep(period() / 25);
    }
    begun.elapsed()
}
fn kill(pid: u32) {
    kill_process(Pid::from_raw(pid as i32).unwrap(), Signal::KILL).unwrap();
}

#[test]
fn killed_worker_is_noticed_within_one_registered_watch_period_and_wakes_waiters() {
    let live = Watch::start(Instant::now() + span(B::MemberChosenWall)).unwrap();
    let mut doomed = worker(ALIVE);
    let mut survivor = worker(ALIVE);
    live.add("member", doomed.pid());
    live.add("owner_leaf", survivor.pid());
    let woken = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&woken);
    let _registration = live.on_death(move || {
        counter.fetch_add(1, Ordering::SeqCst);
    });
    assert!(live.death().is_none(), "nothing has died yet");
    live.check().unwrap();
    kill(doomed.pid());
    // One watch period plus the registered supervisor poll as scheduling slack on a shared host.
    let latency = until("death noticed", period() + span(B::RegistryPoll), || {
        live.death().is_some()
    });
    let death = live.death().unwrap();
    assert_eq!(death.code, NATIVE_EXITED);
    assert_eq!(death.role, "member");
    assert_eq!(death.pid, doomed.pid());
    // The supervisor's own poll may reap the child before the watch looks: then the exit is
    // known but its cause is not (both None), never a different cause.
    assert!(
        death.signal == Some(9) || (death.signal.is_none() && death.exit_status.is_none()),
        "{death:?}"
    );
    until("waker ran", span(B::RegistryPoll), || {
        woken.load(Ordering::SeqCst) == 1
    });
    let error = live.check().unwrap_err();
    assert_eq!(error.to_string(), "worker_native_exited");
    assert!(error.downcast_ref::<WorkerExited>().is_some());
    assert!(latency <= period() + span(B::RegistryPoll));
    survivor.cancel().unwrap();
    assert!(survivor.wait().unwrap().cleanup_proven);
    doomed.cancel().unwrap();
    assert!(doomed.wait().unwrap().cleanup_proven);
}
#[test]
fn a_worker_that_exits_by_itself_is_also_a_native_exit_like_v014() {
    // v0.1.4 tests only `returncode is not None`: exit 0 with empty stderr was the real
    // preview-76 failure and must be reported, not treated as success.
    let live = Watch::start(Instant::now() + span(B::MemberChosenWall)).unwrap();
    let mut gone = worker("exit 0");
    live.add("member", gone.pid());
    until("exit noticed", period() + span(B::RegistryPoll), || {
        live.death().is_some()
    });
    let death = live.death().unwrap();
    assert_eq!(death.code, NATIVE_EXITED);
    // The supervisor may already have reaped it, in which case only the exit is known.
    assert!(matches!(death.exit_status, Some(0) | None));
    assert_eq!(death.signal, None);
    gone.cancel().unwrap();
    gone.wait().unwrap();
}
#[test]
fn waker_registered_after_the_death_runs_at_once() {
    let live = Watch::start(Instant::now() + span(B::MemberChosenWall)).unwrap();
    let mut gone = worker("exit 0");
    live.add("owner_leaf", gone.pid());
    until("exit noticed", period() + span(B::RegistryPoll), || {
        live.death().is_some()
    });
    let woken = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&woken);
    let _late = live.on_death(move || {
        counter.fetch_add(1, Ordering::SeqCst);
    });
    assert_eq!(woken.load(Ordering::SeqCst), 1);
    gone.cancel().unwrap();
    gone.wait().unwrap();
}
#[test]
fn the_supervisors_own_cancellation_is_never_reported_as_a_worker_death() {
    let live = Watch::start(Instant::now() + span(B::MemberChosenWall)).unwrap();
    let mut owned = worker(ALIVE);
    live.add("member", owned.pid());
    {
        // Declared after the child it covers, as the owner does: dropped before the child.
        let _quiesce = live.quiesce_on_drop();
    }
    owned.cancel().unwrap();
    assert!(owned.wait().unwrap().cleanup_proven);
    std::thread::sleep(period() * 3);
    assert!(live.death().is_none(), "{:?}", live.death());
    live.check().unwrap();
}
#[test]
fn watch_ends_at_the_job_deadline_so_the_wall_limit_owns_that_outcome() {
    let live = Watch::start(Instant::now() + period()).unwrap();
    let mut owned = worker(ALIVE);
    live.add("member", owned.pid());
    std::thread::sleep(period() * 2);
    owned.cancel().unwrap();
    owned.wait().unwrap();
    std::thread::sleep(period() * 2);
    assert!(live.death().is_none());
}
