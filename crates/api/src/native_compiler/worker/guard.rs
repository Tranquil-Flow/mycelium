// SPDX-License-Identifier: AGPL-3.0-only
//! Member-side resource guard for the owned native worker: the port of v0.1.4's
//! `ManagedWorker._watch` / `_check` memory guard (mycelium_a13_macos/worker.py:786-862,
//! 864-929, 1459-1486 @ f4e366e9). The MLX memory limit is advisory; this guard measures
//! the worker's real memory and stops it on a breach of the member's consented cap.
//!
//! One thread per guarded worker samples at v0.1.4's 0.25 s cadence. On a reading over
//! the limit, or a reading that failed while the worker still runs (fail closed, as
//! v0.1.4), it kills and reaps the EXACT owned child under the shared child lock: the
//! child is signalled only while unreaped, so its pid cannot have been reused.
use super::{Error, Result, duration};
use budgets::BudgetId;
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Child,
    sync::{Arc, Condvar, Mutex},
    thread::JoinHandle,
    time::Duration,
};

#[cfg(test)]
#[path = "guard_tests.rs"]
mod tests;

/// v0.1.4 `WorkerError("worker_memory_limit_exceeded")` (worker.py:848).
pub(crate) const MEMORY_LIMIT_EXCEEDED: &str = "worker_memory_limit_exceeded";
/// v0.1.4 `WorkerError("worker_memory_observation_failed")` (worker.py:907, 921, 923).
pub(crate) const MEMORY_OBSERVATION_FAILED: &str = "worker_memory_observation_failed";

/// v0.1.4 `WorkerError("worker_disk_limit_exceeded")` (worker.py:842).
pub(crate) const DISK_LIMIT_EXCEEDED: &str = "worker_disk_limit_exceeded";
/// v0.1.4 `WorkerError("worker_storage_observation_failed")` (worker.py:827, 836, 840, 844).
pub(crate) const STORAGE_OBSERVATION_FAILED: &str = "worker_storage_observation_failed";
/// Directory levels the storage walk may descend below the staging root.
pub(crate) const WALK_DEPTH: usize = 32;

/// The staging folder the guard walks and the storage it may hold (lane MG2).
#[derive(Clone, Debug)]
pub(crate) struct Disk {
    root: PathBuf,
    max_bytes: u64,
}
impl Disk {
    /// NOT BUILT YET (lane MG2 RED): records the folder; nothing walks it.
    pub(crate) fn open(root: &Path, max_bytes: u64) -> Result<Self> {
        Ok(Self {
            root: root.into(),
            max_bytes,
        })
    }
}

/// The owned child, shared by the worker and its guard.
pub(crate) type Slot = Arc<Mutex<Option<Child>>>;

/// One reading of the worker's resident memory. Production reads the host;
/// tests inject readings through `Worker::from_child_guarded` only.
pub(crate) trait MemoryProbe: Send {
    fn resident_bytes(&mut self, pid: u32) -> std::result::Result<u64, &'static str>;
}

/// The host's own accounting of the exact worker pid: the larger of its resident
/// size (what v0.1.4's `ps rss` sums) and its physical footprint (which also counts
/// compressed and GPU-driver memory that MLX allocates through Metal).
pub(crate) struct HostProbe;
impl MemoryProbe for HostProbe {
    fn resident_bytes(&mut self, pid: u32) -> std::result::Result<u64, &'static str> {
        let pid = libc::c_int::try_from(pid).map_err(|_| MEMORY_OBSERVATION_FAILED)?;
        #[allow(unsafe_code)] // rusage_info_v2 is plain integers; all-zero is a valid value
        let mut info: libc::rusage_info_v2 = unsafe { std::mem::zeroed() };
        #[allow(unsafe_code)]
        // proc_pid_rusage(2) fills the caller-owned rusage_info_v2 for RUSAGE_INFO_V2
        let status = unsafe {
            libc::proc_pid_rusage(
                pid,
                libc::RUSAGE_INFO_V2,
                (&raw mut info).cast::<libc::rusage_info_t>(),
            )
        };
        if status != 0 {
            return Err(MEMORY_OBSERVATION_FAILED);
        }
        Ok(info.ri_resident_size.max(info.ri_phys_footprint))
    }
}

/// The guard's verdict: the closed code, v0.1.4's three `limit_detail` numbers
/// (worker.py:855-859; null for an observation failure) and the sample count.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Failure {
    pub(crate) code: &'static str,
    pub(crate) limit_detail: Value,
    pub(crate) samples: u64,
}
impl Failure {
    pub(crate) fn document(&self) -> Value {
        json!({"code":self.code,"limit_detail":self.limit_detail})
    }
}

#[derive(Default)]
struct State {
    stopped: bool,
    /// Set before the kill, so an exchange broken by it waits for the verdict.
    tripped: bool,
    samples: u64,
    failure: Option<Failure>,
}
type Shared = Arc<(Mutex<State>, Condvar)>;

pub(crate) struct Guard {
    shared: Shared,
    thread: Option<JoinHandle<()>>,
}
impl Guard {
    pub(crate) fn start(
        child: Slot,
        pid: u32,
        limit: u64,
        disk: Option<Disk>,
        probe: Box<dyn MemoryProbe>,
    ) -> Result<Self> {
        drop(disk);
        let cadence =
            duration(BudgetId::PyMyceliumA13MacosWorkerManagedworkerWatchSelfClosedWaitL1460)?;
        let shared: Shared = Arc::default();
        let watched = shared.clone();
        let thread = std::thread::Builder::new()
            .name("worker-memory-guard".into())
            .spawn(move || watch(&watched, &child, pid, limit, cadence, probe))
            .map_err(|_| Error("worker_guard_failed"))?;
        Ok(Self {
            shared,
            thread: Some(thread),
        })
    }
    #[cfg(test)]
    pub(crate) fn samples(&self) -> u64 {
        self.shared
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .samples
    }
    /// The verdict. When the guard tripped but is still killing, wait (bounded)
    /// for it, so an exchange broken by the kill reports the guard's code.
    pub(crate) fn failure(&self) -> Option<Failure> {
        let (lock, changed) = &*self.shared;
        let state = lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if !state.tripped || state.failure.is_some() {
            return state.failure.clone();
        }
        let settle = match duration(BudgetId::RouterClose) {
            Ok(settle) => settle,
            Err(e) => {
                eprintln!("worker_guard_settle_unavailable: {}", e.0);
                return Some(Failure {
                    code: "worker_guard_failed",
                    limit_detail: Value::Null,
                    samples: state.samples,
                });
            }
        };
        let (state, _) = changed
            .wait_timeout_while(state, settle, |s| s.tripped && s.failure.is_none())
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.failure.clone()
    }
    #[cfg(test)]
    pub(crate) fn wait_until(&self, deadline: std::time::Instant) -> Option<Failure> {
        let (lock, changed) = &*self.shared;
        let state = lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let wait = deadline.saturating_duration_since(std::time::Instant::now());
        let (state, _) = changed
            .wait_timeout_while(state, wait, |s| s.failure.is_none())
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.failure.clone()
    }
    /// Stop sampling and join the thread (idempotent).
    pub(crate) fn stop(&mut self) {
        self.shared
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .stopped = true;
        self.shared.1.notify_all();
        if let Some(thread) = self.thread.take()
            && thread.join().is_err()
        {
            eprintln!("worker_guard_panicked");
        }
    }
}
impl Drop for Guard {
    fn drop(&mut self) {
        self.stop();
    }
}

enum Ended {
    /// The guard killed and reaped the running child.
    Killed,
    /// The child had already exited (or was released): not the guard's doing.
    Gone,
    /// Kill or reap failed; the worker's own cleanup still owns the child.
    Unproven,
}
fn kill_and_reap(child: &Slot) -> Ended {
    let Ok(mut slot) = child.lock() else {
        eprintln!("worker_guard_poisoned");
        return Ended::Unproven;
    };
    let Some(child) = slot.as_mut() else {
        return Ended::Gone;
    };
    match child.try_wait() {
        Ok(Some(_)) => Ended::Gone,
        Ok(None) => match child.kill().and_then(|()| child.wait()) {
            Ok(_) => Ended::Killed,
            Err(e) => {
                eprintln!("worker_guard_kill_failed: {e}");
                Ended::Unproven
            }
        },
        Err(e) => {
            eprintln!("worker_guard_status_failed: {e}");
            Ended::Unproven
        }
    }
}

fn watch(
    shared: &Shared,
    child: &Slot,
    pid: u32,
    limit: u64,
    cadence: Duration,
    mut probe: Box<dyn MemoryProbe>,
) {
    let (lock, changed) = &**shared;
    loop {
        let samples = {
            let Ok(state) = lock.lock() else {
                eprintln!("worker_guard_poisoned");
                return;
            };
            let Ok((mut state, _)) = changed.wait_timeout_while(state, cadence, |s| !s.stopped)
            else {
                eprintln!("worker_guard_poisoned");
                return;
            };
            if state.stopped {
                return;
            }
            state.samples += 1;
            state.samples
        };
        let verdict = match probe.resident_bytes(pid) {
            Ok(bytes) if bytes > limit => Failure {
                code: MEMORY_LIMIT_EXCEEDED,
                limit_detail: json!({"observed_group_rss_bytes":bytes,"max_rss_bytes":limit,
                    "overshoot_bytes":bytes - limit}),
                samples,
            },
            Ok(_) => continue,
            Err(code) => {
                eprintln!("worker_guard_observation_failed: {code}");
                Failure {
                    code: MEMORY_OBSERVATION_FAILED,
                    limit_detail: Value::Null,
                    samples,
                }
            }
        };
        match lock.lock() {
            Ok(mut state) if !state.stopped => state.tripped = true,
            Ok(_) => return,
            Err(_) => {
                eprintln!("worker_guard_poisoned");
                return;
            }
        }
        let ended = kill_and_reap(child);
        let Ok(mut state) = lock.lock() else {
            eprintln!("worker_guard_poisoned");
            return;
        };
        match ended {
            // A worker that exited by itself is reported by its link, not here.
            Ended::Gone => state.tripped = false,
            Ended::Killed | Ended::Unproven => state.failure = Some(verdict),
        }
        changed.notify_all();
        return;
    }
}
