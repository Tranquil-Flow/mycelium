// SPDX-License-Identifier: AGPL-3.0-only
//! SE-SLICE1-L8-OWNEDWORKER-COMMAND-DROP. v0.1.4 closes the parent's copy of every descriptor it
//! handed the child right after `Popen` (`mycelium_node/process.py:2125-2143`, `:2175-2177` @
//! f4e366e9; executed: `L8-evidence/round8/oracle_eof_probe.out.json`, EOF after SIGKILL in 36 us,
//! and no EOF in the control that keeps the copy). `OwnedWorker::spawn` kept the `Command`, and with
//! it every `Stdio::from(OwnedFd)` the caller gave it, alive until the leaf was reaped, so a parent
//! that holds the other end of a stdio socket never saw end-of-stream when the worker died.
use backend_mlx::{
    process::{OwnedWorker, ProcessLimits},
    resources::ResourcePool,
};
use budgets::{BudgetId, Bytes, Entries, Reservation, Resources, Span};
use rustix::process::{Pid, Signal, kill_process};
use std::{
    io::{ErrorKind, Read},
    os::{fd::OwnedFd, unix::net::UnixStream},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

/// The v0.1.4 `_watch` cadence (`mycelium_a13_macos/worker.py:1460 @ f5bfb709`): the bound the
/// SLICE-1 agent had to fall back on while the parent copy kept the socket open.
const DETECTION: BudgetId = BudgetId::PyMyceliumA13MacosWorkerManagedworkerWatchSelfClosedWaitL1460;

fn span(id: BudgetId) -> Span {
    id.span(Entries::ZERO).unwrap()
}
fn limits() -> ProcessLimits {
    ProcessLimits {
        deadline: Instant::now() + Duration::from_millis(span(BudgetId::WorkerStartup).millis()),
        close: span(BudgetId::RouterClose),
        reap: span(BudgetId::WorkerCleanupReserve),
        poll: span(BudgetId::RegistryPoll),
    }
}
fn amount() -> Reservation {
    Reservation {
        staged: Bytes::ZERO,
        staging_overhead: Bytes::ZERO,
        resident: Bytes::new(1),
        scratch: Bytes::ZERO,
        kv: Bytes::ZERO,
    }
}
fn pool() -> ResourcePool {
    ResourcePool::new(Resources {
        storage: Bytes::ZERO,
        memory: Bytes::new(1),
    })
}
/// A non-forking leaf that stays alive until killed. Its stdout is the given socket end.
fn sleeper(stdout: OwnedFd) -> Command {
    let mut command = Command::new("/bin/sleep");
    command.arg("600").stdout(Stdio::from(stdout));
    command
}
fn pid_gone(pid: u32) -> bool {
    let pid = Pid::from_raw(i32::try_from(pid).unwrap()).unwrap();
    // Signal 0 is the existence probe; it is exact-pid and sends nothing.
    let probe = rustix::process::test_kill_process(pid);
    probe.err() == Some(rustix::io::Errno::SRCH)
}
fn kill_exact(pid: u32) {
    let pid = Pid::from_raw(i32::try_from(pid).unwrap()).unwrap();
    kill_process(pid, Signal::KILL).unwrap();
}
/// `Some(true)` = end-of-stream, `Some(false)` = data, `None` = nothing within the budget.
/// Non-blocking polling: macOS refuses `set_read_timeout` (EINVAL) on a socket whose peer is gone.
fn eof_within(stream: &mut UnixStream, budget: Duration) -> Option<bool> {
    stream.set_nonblocking(true).unwrap();
    let started = Instant::now();
    let mut byte = [0_u8; 1];
    loop {
        match stream.read(&mut byte) {
            Ok(0) => return Some(true),
            Ok(_) => return Some(false),
            Err(error) => assert_eq!(
                error.kind(),
                ErrorKind::WouldBlock,
                "unexpected read error: {error}"
            ),
        }
        if started.elapsed() >= budget {
            return None;
        }
        std::thread::yield_now();
    }
}
fn budget() -> Duration {
    Duration::from_millis(span(DETECTION).millis())
}

#[test]
fn parent_sees_eof_within_the_budget_after_the_worker_is_killed_by_pid() {
    let (mut parent, child_end) = UnixStream::pair().unwrap();
    let mut worker = OwnedWorker::spawn(
        sleeper(OwnedFd::from(child_end)),
        pool(),
        amount(),
        limits(),
    )
    .unwrap();
    let pid = worker.pid();
    assert_eq!(
        eof_within(&mut parent, Duration::ZERO),
        None,
        "the live worker holds the socket open"
    );
    kill_exact(pid);
    let killed = Instant::now();
    let seen = eof_within(&mut parent, budget());
    let elapsed = killed.elapsed();
    let report = worker.wait().unwrap();
    assert!(report.cleanup_proven, "{:?}", report.errors);
    assert_eq!(report.pid, pid);
    assert!(pid_gone(pid), "the exact worker pid must be reaped");
    assert_eq!(
        seen,
        Some(true),
        "no end-of-stream within {budget:?} (waited {elapsed:?}): the parent still holds a copy of \
         the child's stdout",
        budget = budget()
    );
}

#[test]
fn control_a_parent_held_copy_of_the_child_end_suppresses_eof() {
    // The probe must be able to see a leak, or the test above proves nothing (descriptor-confinement
    // style control, and the v0.1.4 `control-parent-keeps-child-end` row).
    let (mut parent, child_end) = UnixStream::pair().unwrap();
    let held = child_end.try_clone().unwrap();
    let mut worker = OwnedWorker::spawn(
        sleeper(OwnedFd::from(child_end)),
        pool(),
        amount(),
        limits(),
    )
    .unwrap();
    let pid = worker.pid();
    kill_exact(pid);
    let report_ok = worker.wait().unwrap().cleanup_proven;
    assert!(report_ok);
    assert!(pid_gone(pid));
    assert_eq!(
        eof_within(&mut parent, budget()),
        None,
        "a copy held by the parent must keep the stream open"
    );
    drop(held);
    assert_eq!(eof_within(&mut parent, budget()), Some(true));
}

#[test]
fn a_failed_spawn_also_releases_the_parent_copy() {
    let (mut parent, child_end) = UnixStream::pair().unwrap();
    let mut command = Command::new("/nonexistent/mycelium-l8-no-such-leaf");
    command.stdout(Stdio::from(OwnedFd::from(child_end)));
    let error = OwnedWorker::spawn(command, pool(), amount(), limits())
        .err()
        .expect("spawning a missing executable must fail");
    assert!(
        matches!(
            error,
            backend_mlx::process::ProcessError::Io { phase: "spawn", .. }
        ),
        "{error:?}"
    );
    assert_eq!(eof_within(&mut parent, budget()), Some(true));
}

#[test]
fn natural_exit_also_gives_eof_and_reaps() {
    let (mut parent, child_end) = UnixStream::pair().unwrap();
    let mut command = Command::new("/usr/bin/true");
    command.stdout(Stdio::from(OwnedFd::from(child_end)));
    let mut worker = OwnedWorker::spawn(command, pool(), amount(), limits()).unwrap();
    let pid = worker.pid();
    let report = worker.wait().unwrap();
    assert_eq!(report.status.and_then(|status| status.code()), Some(0));
    assert!(report.cleanup_proven, "{:?}", report.errors);
    assert!(pid_gone(pid));
    assert_eq!(eof_within(&mut parent, budget()), Some(true));
}
