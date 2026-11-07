// SPDX-License-Identifier: AGPL-3.0-only
// BACKLOG 17: e    if std::env::var(ANCHOR).ok().and_then(|v| v.parse::<u32>().ok()).is_none() {ery test fixture child must be reaped on all paths (pass, fail, panic,
// owner killed). Shared by the integration tests that re-exec the test binary as a worker.
#![allow(dead_code)]
use node::{Residue, WorkerAdapter, WorkerError};
use std::{
    ffi::OsString,
    process::Command,
    time::{Duration, Instant},
};

pub const ANCHOR: &str = "L6_FIXTURE_ANCHOR";

/// Environment value that ties a fixture to the process that owns the test.
/// The root owner: an inherited anchor (nested fixtures stay tied to the test process),
/// otherwise this process.
pub fn anchor_value() -> OsString {
    std::env::var_os(ANCHOR).unwrap_or_else(|| std::process::id().to_string().into())
}
/// Tie `command`'s child to this process: it must exit once this process is gone.
pub fn anchored(command: &mut Command) -> &mut Command {
    command.env(ANCHOR, anchor_value())
}
/// Tie `command`'s child to this very process, ignoring any inherited anchor.
pub fn anchored_to_this_process(command: &mut Command) -> &mut Command {
    command.env(ANCHOR, std::process::id().to_string())
}
/// Fixture side: has the process this fixture is anchored to gone away?
pub fn owner_gone() -> bool {
    let Some(anchor) = std::env::var(ANCHOR)
        .ok()
        .and_then(|v| v.parse::<u32>().ok())
    else {
        return false;
    };
    gone(anchor)
}
/// Fixture side: stay alive while the anchor lives. Never returns.
/// Fixture side: refuse to run (before announcing readiness) without a valid owner anchor.
pub fn require_anchor() {
    if std::env::var(ANCHOR)
        .ok()
        .and_then(|v| v.parse::<u32>().ok())
        .is_none()
    {
        eprintln!("fixture launched without {ANCHOR}: refusing to run unowned");
        std::process::exit(2);
    }
}
pub fn hold() -> ! {
    require_anchor();
    while !owner_gone() {
        std::thread::sleep(poll());
    }
    std::process::exit(0);
}
pub fn gone(pid: u32) -> bool {
    match rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()) {
        Err(rustix::io::Errno::SRCH) => true,
        Ok(()) | Err(rustix::io::Errno::PERM) => false,
        Err(e) => panic!("pid observation failed: {e}"),
    }
}
pub fn bounded() -> Duration {
    Duration::from_millis(
        budgets::BudgetId::WorkerStartup
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
pub fn poll() -> Duration {
    Duration::from_millis(
        budgets::BudgetId::OwnerPoll
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    )
}

/// Kill-on-drop owner of a worker adapter and the exact pids it spawned.
pub struct Reaped<W: WorkerAdapter> {
    worker: W,
    pids: Vec<u32>,
}
impl<W: WorkerAdapter> Reaped<W> {
    pub fn new(worker: W, pids: Vec<u32>) -> Self {
        Self { worker, pids }
    }
}
impl<W: WorkerAdapter> std::ops::Deref for Reaped<W> {
    type Target = W;
    fn deref(&self) -> &W {
        &self.worker
    }
}
impl<W: WorkerAdapter> std::ops::DerefMut for Reaped<W> {
    fn deref_mut(&mut self) -> &mut W {
        &mut self.worker
    }
}
impl<W: WorkerAdapter> Drop for Reaped<W> {
    fn drop(&mut self) {
        // Best effort and bounded; Drop must not panic while a test is unwinding.
        if let Err(error) = self.worker.request_stop() {
            eprintln!("reaper: stop refused: {error:?}");
        }
        let end = Instant::now() + bounded();
        loop {
            // Observation also reaps an exited child, which `gone` needs.
            drop(self.worker.observe());
            if self.pids.iter().all(|p| gone(*p)) {
                return;
            }
            if Instant::now() >= end {
                break;
            }
            std::thread::sleep(poll());
        }
        for pid in self.pids.iter().copied().filter(|p| !gone(*p)) {
            eprintln!("reaper: exact-pid kill of unresponsive fixture {pid}");
            if let Err(error) = rustix::process::kill_process(
                rustix::process::Pid::from_raw(pid as i32).unwrap(),
                rustix::process::Signal::KILL,
            ) {
                eprintln!("reaper: kill {pid}: {error}");
            }
        }
    }
}
impl<W: WorkerAdapter> WorkerAdapter for Reaped<W> {
    fn request_stop(&mut self) -> Result<(), WorkerError> {
        self.worker.request_stop()
    }
    fn observe(&mut self) -> Result<Residue, WorkerError> {
        self.worker.observe()
    }
}

/// Kill-on-drop owner of a plain `std::process::Child` (no adapter involved).
pub struct Owned(pub std::process::Child);
impl Drop for Owned {
    fn drop(&mut self) {
        if let Err(error) = self.0.kill() {
            // InvalidInput means it already exited and was reaped.
            if error.kind() != std::io::ErrorKind::InvalidInput {
                eprintln!("owned fixture kill: {error}");
            }
        }
        if let Err(error) = self.0.wait() {
            eprintln!("owned fixture wait: {error}");
        }
    }
}
