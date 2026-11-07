// SPDX-License-Identifier: AGPL-3.0-only
//! Prompt native-worker death detection: the port of v0.1.4's `ManagedWorker._watch`
//! (`mycelium_a13_macos/worker.py:1459-1479 @ f4e366e9`). That loop wakes every 0.25 s,
//! tests whether the owned native process exited and, if so, records the cause as the
//! worker's terminal code `worker_native_exited`, writes a bounded exit diagnostic and
//! closes the worker, which fails the job. The owner here supervises two children (the
//! remote member and the co-located owner leaf), so each is watched under a role name.
//!
//! The probe is `waitid(WEXITED|WNOWAIT|WNOHANG)` on the exact owned child: it observes
//! an exit without reaping, so the supervisor's own `try_wait` still collects the status
//! and the pid cannot be recycled while the zombie exists. `ECHILD` means the supervisor
//! already reaped the child, which it only does after the child ended.
use crate::loopback::Result;
use budgets::{BudgetId as B, Entries};
use rustix::{
    io::Errno,
    process::{Pid, WaitId, WaitIdOptions, waitid},
};
use std::{
    sync::{
        Arc, Condvar, Mutex, MutexGuard, PoisonError,
        atomic::{AtomicU64, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

/// v0.1.4's recorded terminal code for a worker whose native process is gone.
pub const NATIVE_EXITED: &str = "worker_native_exited";
/// v0.1.4's code for a guard that itself failed (`worker.py:1481`).
pub const GUARD_FAILED: &str = "worker_guard_failed";

/// The worker that ended and how.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Death {
    pub code: &'static str,
    pub role: &'static str,
    pub pid: u32,
    /// Exit status when the kernel still held it; `None` if the supervisor reaped first.
    pub exit_status: Option<i32>,
    pub signal: Option<i32>,
}
/// The job's terminal error: its text is exactly v0.1.4's bare code.
#[derive(Debug)]
pub struct WorkerExited(pub Death);
impl std::fmt::Display for WorkerExited {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0.code)
    }
}
impl std::error::Error for WorkerExited {}

type Wake = Arc<dyn Fn() + Send + Sync>;
struct State {
    workers: Vec<(&'static str, u32)>,
    death: Option<Death>,
    wakers: Vec<(u64, Wake)>,
    stopped: bool,
}
struct Shared {
    state: Mutex<State>,
    changed: Condvar,
    next: AtomicU64,
}
fn lock(shared: &Shared) -> MutexGuard<'_, State> {
    shared.state.lock().unwrap_or_else(PoisonError::into_inner)
}
/// Result of looking at one owned child.
enum Probe {
    Alive,
    Ended(Death),
}
fn probe(role: &'static str, pid: u32) -> Probe {
    let Some(raw) = i32::try_from(pid).ok().and_then(Pid::from_raw) else {
        return Probe::Ended(Death {
            code: GUARD_FAILED,
            role,
            pid,
            exit_status: None,
            signal: None,
        });
    };
    let ended = |code, exit_status, signal| {
        Probe::Ended(Death {
            code,
            role,
            pid,
            exit_status,
            signal,
        })
    };
    match waitid(
        WaitId::Pid(raw),
        WaitIdOptions::EXITED | WaitIdOptions::NOWAIT | WaitIdOptions::NOHANG,
    ) {
        Ok(None) => Probe::Alive,
        Ok(Some(status)) => ended(
            NATIVE_EXITED,
            status.exit_status(),
            status.terminating_signal(),
        ),
        Err(Errno::CHILD) => ended(NATIVE_EXITED, None, None),
        Err(Errno::INTR) => Probe::Alive,
        Err(_) => ended(GUARD_FAILED, None, None),
    }
}

/// One job's watch over its owned native children.
pub struct Watch {
    shared: Arc<Shared>,
    thread: Mutex<Option<JoinHandle<()>>>,
}
/// A callback registration removed on drop (unless it was made permanent).
pub struct Registration<'a> {
    watch: &'a Watch,
    id: u64,
}
impl Drop for Registration<'_> {
    fn drop(&mut self) {
        lock(&self.watch.shared)
            .wakers
            .retain(|(id, _)| *id != self.id);
    }
}
impl Registration<'_> {
    /// Keep the callback until the watch itself ends.
    pub fn keep(self) {
        std::mem::forget(self);
    }
}
/// Stops the watch when dropped. Declared after the children it covers, so the supervisor's
/// own cancellation of a still-running child is never mistaken for that child's death.
pub struct Quiesce<'a>(&'a Watch);
impl Drop for Quiesce<'_> {
    fn drop(&mut self) {
        self.0.quiesce();
    }
}

impl Watch {
    /// Poll cadence is the registered v0.1.4 `_watch` wait (`worker.py:1460`); the watch
    /// ends by itself at the job's own deadline, where the wall limit (not an exit) owns
    /// the outcome.
    pub fn start(deadline: Instant) -> Result<Self> {
        let poll = Duration::from_millis(
            B::PyMyceliumA13MacosWorkerManagedworkerWatchSelfClosedWaitL1460
                .span(Entries::ZERO)
                .map_err(|e| format!("worker watch cadence: {e:?}"))?
                .millis(),
        );
        let shared = Arc::new(Shared {
            state: Mutex::new(State {
                workers: Vec::new(),
                death: None,
                wakers: Vec::new(),
                stopped: false,
            }),
            changed: Condvar::new(),
            next: AtomicU64::new(0),
        });
        let background = Arc::clone(&shared);
        let thread = thread::Builder::new()
            .name("slice-worker-watch".into())
            .spawn(move || watch_loop(&background, poll, deadline))?;
        Ok(Self {
            shared,
            thread: Mutex::new(Some(thread)),
        })
    }
    /// Begin watching one exact owned child.
    pub fn add(&self, role: &'static str, pid: u32) {
        lock(&self.shared).workers.push((role, pid));
    }
    /// Run `wake` when a watched worker ends (at once if one already has). It must not block
    /// for long and must be safe to run from the watch thread and more than once.
    pub fn on_death(&self, wake: impl Fn() + Send + Sync + 'static) -> Registration<'_> {
        let wake: Wake = Arc::new(wake);
        let id = self.shared.next.fetch_add(1, Ordering::Relaxed);
        let already = {
            let mut state = lock(&self.shared);
            state.wakers.push((id, Arc::clone(&wake)));
            state.death.is_some()
        };
        if already {
            wake();
        }
        Registration { watch: self, id }
    }
    /// The first worker death observed, if any.
    pub fn death(&self) -> Option<Death> {
        lock(&self.shared).death.clone()
    }
    /// `Err(WorkerExited)` once a worker has ended: the loop check of a blocked driver.
    pub fn check(&self) -> Result<()> {
        match self.death() {
            Some(death) => Err(Box::new(WorkerExited(death))),
            None => Ok(()),
        }
    }
    /// From now on the supervisor, not a worker, ends the children; keep any recorded death.
    pub fn quiesce(&self) {
        lock(&self.shared).stopped = true;
        self.shared.changed.notify_all();
    }
    pub fn quiesce_on_drop(&self) -> Quiesce<'_> {
        Quiesce(self)
    }
    /// Quiesce and join the watch thread.
    pub fn finish(&self) {
        self.quiesce();
        let thread = self
            .thread
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        if let Some(thread) = thread
            && thread.join().is_err()
        {
            eprintln!("worker watch thread panicked");
        }
    }
}
impl Drop for Watch {
    fn drop(&mut self) {
        self.finish();
    }
}

/// `while not self._closed.wait(0.25)`: wake each period, test the children, stop once
/// closed. A death is recorded before any waker runs so a woken waiter always sees it.
fn watch_loop(shared: &Shared, poll: Duration, deadline: Instant) {
    let mut state = lock(shared);
    loop {
        let (next, _) = shared
            .changed
            .wait_timeout(state, poll)
            .unwrap_or_else(PoisonError::into_inner);
        state = next;
        if state.stopped || Instant::now() >= deadline {
            return;
        }
        let Some(death) = state
            .workers
            .iter()
            .find_map(|(role, pid)| match probe(role, *pid) {
                Probe::Alive => None,
                Probe::Ended(death) => Some(death),
            })
        else {
            continue;
        };
        crate::phase::mark(
            "worker_exit_observed",
            &format!("role={},pid={},code={}", death.role, death.pid, death.code),
        );
        state.death = Some(death);
        let wakers: Vec<Wake> = state
            .wakers
            .iter()
            .map(|(_, wake)| Arc::clone(wake))
            .collect();
        drop(state);
        for wake in wakers {
            wake();
        }
        return;
    }
}
