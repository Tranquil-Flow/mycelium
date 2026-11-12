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
/// v0.1.4 has no depth bound (`os.walk`; its entry count bounds depth at 10000).
/// Here every level holds one open directory descriptor while it is read, so
/// depth is bounded far below the process's descriptor limit; the custody
/// folder itself is flat (`stage-shards/<shard>`).
pub(crate) const WALK_DEPTH: usize = 32;

/// The staging folder the guard walks and the storage it may hold (lane MG2):
/// v0.1.4 `ManagedWorker.artifact_root` and `limits.max_disk_bytes`.
#[derive(Clone, Debug)]
pub(crate) struct Disk {
    root: PathBuf,
    /// `(st_dev, st_ino)` when the guard was given the folder (worker.py:708-709).
    identity: (u64, u64),
    max_bytes: u64,
}
impl Disk {
    /// v0.1.4 `ManagedWorker.__init__` (worker.py:705-709): the folder must be
    /// a real directory (not a symlink) given by an absolute path; its identity
    /// is kept, so a swapped folder is never walked in its place.
    pub(crate) fn open(root: &Path, max_bytes: u64) -> Result<Self> {
        use std::os::unix::fs::MetadataExt;
        if max_bytes == 0 {
            return Err(Error("worker_limits_invalid"));
        }
        let meta = std::fs::symlink_metadata(root)
            .ok()
            .filter(|m| m.file_type().is_dir() && root.is_absolute())
            .ok_or(Error("worker_artifact_root_invalid"))?;
        Ok(Self {
            root: root.into(),
            identity: (meta.dev(), meta.ino()),
            max_bytes,
        })
    }
}

/// Why a walk stopped short of a total within the limit.
enum Unwalked {
    /// Regular files summed past the limit: the running total.
    Over(u64),
    /// Not observable: fail closed (v0.1.4 `worker_storage_observation_failed`).
    Unobservable,
}
/// v0.1.4 `ManagedWorker._check`'s storage walk (worker.py:820-844): every
/// entry is lstat'ed, never followed. A symlink or any entry other than a
/// regular file or directory, more than `entries` entries, a level deeper than
/// `WALK_DEPTH`, a walk not finished by `deadline`, a swapped root or any I/O
/// error fails closed; regular file lengths are summed and the walk stops at
/// the first total over the limit. Directories are opened relative to their
/// parent's descriptor with `O_NOFOLLOW`, so the walk never leaves the root.
fn walk(
    disk: &Disk,
    entries: u64,
    pace: &mut dyn Pace,
    budget: Duration,
) -> std::result::Result<u64, Unwalked> {
    let started = pace.now();
    use rustix::fs::{AtFlags, CWD, Dir, FileType, Mode, OFlags, openat, statat};
    use std::os::unix::fs::MetadataExt;
    let gone = |_| Unwalked::Unobservable;
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let root = std::fs::File::from(openat(CWD, &disk.root, flags, Mode::empty()).map_err(gone)?);
    let meta = root.metadata().map_err(|_| Unwalked::Unobservable)?;
    if !meta.file_type().is_dir() || (meta.dev(), meta.ino()) != disk.identity {
        return Err(Unwalked::Unobservable);
    }
    let mut stack = vec![Dir::new(std::os::fd::OwnedFd::from(root)).map_err(gone)?];
    let (mut total, mut count) = (0_u64, 0_u64);
    loop {
        let depth = stack.len();
        let Some(dir) = stack.last_mut() else {
            return Ok(total);
        };
        let Some(entry) = dir.read() else {
            stack.pop();
            continue;
        };
        let entry = entry.map_err(gone)?;
        let name = entry.file_name();
        if name == c"." || name == c".." {
            continue;
        }
        count += 1;
        if count > entries || pace.now().saturating_sub(started) > budget {
            return Err(Unwalked::Unobservable);
        }
        let parent = dir.fd().map_err(gone)?;
        let stat = statat(parent, name, AtFlags::SYMLINK_NOFOLLOW).map_err(gone)?;
        match FileType::from_raw_mode(stat.st_mode) {
            FileType::RegularFile => {
                let size = u64::try_from(stat.st_size).map_err(|_| Unwalked::Unobservable)?;
                total = total.saturating_add(size);
            }
            FileType::Directory if depth <= WALK_DEPTH => {
                let child = openat(parent, name, flags, Mode::empty()).map_err(gone)?;
                stack.push(Dir::new(child).map_err(gone)?);
            }
            _ => return Err(Unwalked::Unobservable),
        }
        if total > disk.max_bytes {
            return Err(Unwalked::Over(total));
        }
    }
}
/// The storage verdict for one sample, `None` within the limit.
/// The walk must finish within one sampling period (`cadence`), by `pace`'s clock.
fn storage_verdict(
    disk: &Disk,
    entries: u64,
    pace: &mut dyn Pace,
    cadence: Duration,
    samples: u64,
) -> Option<Failure> {
    match walk(disk, entries, pace, cadence) {
        Ok(_) => None,
        Err(Unwalked::Over(total)) => Some(Failure {
            code: DISK_LIMIT_EXCEEDED,
            limit_detail: json!({"observed_disk_bytes":total,"max_disk_bytes":disk.max_bytes,
                "overshoot_bytes":total - disk.max_bytes}),
            samples,
        }),
        Err(Unwalked::Unobservable) => {
            eprintln!("worker_guard_storage_observation_failed");
            Some(Failure {
                code: STORAGE_OBSERVATION_FAILED,
                limit_detail: Value::Null,
                samples,
            })
        }
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

/// How the guard waits out its sampling period and reads the time that bounds
/// a storage walk (lane MG2). Production (`Clock`) waits on the guard's own
/// condvar for the budgets row's period; tests drive a virtual clock through
/// the constructor-only `Worker::from_child_paced`.
pub(crate) trait Pace: Send {
    /// Wait out one sampling `period`. `rest(d)` waits up to `d`, returning
    /// early once the guard is stopped; the result is whether it was stopped.
    fn period(&mut self, period: Duration, rest: &mut dyn FnMut(Duration) -> bool) -> bool;
    /// Monotonic time since an arbitrary origin.
    fn now(&mut self) -> Duration;
}
pub(crate) struct Clock(std::time::Instant);
impl Clock {
    pub(crate) fn new() -> Self {
        Self(std::time::Instant::now())
    }
}
impl Pace for Clock {
    fn period(&mut self, period: Duration, rest: &mut dyn FnMut(Duration) -> bool) -> bool {
        rest(period)
    }
    fn now(&mut self) -> Duration {
        self.0.elapsed()
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
    /// The staging folder walked every sample before the memory reading.
    disk: Option<Disk>,
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
        pace: Box<dyn Pace>,
    ) -> Result<Self> {
        let cadence =
            duration(BudgetId::PyMyceliumA13MacosWorkerManagedworkerWatchSelfClosedWaitL1460)?;
        let entries = BudgetId::PyMyceliumA13MacosWorkerManagedworkerCheckCountL835
            .count()
            .map_err(|_| Error("budget_unavailable"))?
            .get();
        let shared: Shared = Arc::new((
            Mutex::new(State {
                disk,
                ..State::default()
            }),
            Condvar::new(),
        ));
        let watched = shared.clone();
        let thread = std::thread::Builder::new()
            .name("worker-memory-guard".into())
            .spawn(move || {
                watch(
                    &watched,
                    &child,
                    pid,
                    limit,
                    (cadence, entries),
                    probe,
                    pace,
                )
            })
            .map_err(|_| Error("worker_guard_failed"))?;
        Ok(Self {
            shared,
            thread: Some(thread),
        })
    }
    /// Test-only: walk `disk` from the next sample on.
    #[cfg(test)]
    pub(crate) fn watch_disk(&self, disk: Disk) {
        self.shared
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .disk = Some(disk);
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

/// The memory verdict for one sample, `None` at or under the limit.
fn memory_verdict(
    probe: &mut dyn MemoryProbe,
    pid: u32,
    limit: u64,
    samples: u64,
) -> Option<Failure> {
    match probe.resident_bytes(pid) {
        Ok(bytes) if bytes > limit => Some(Failure {
            code: MEMORY_LIMIT_EXCEEDED,
            limit_detail: json!({"observed_group_rss_bytes":bytes,"max_rss_bytes":limit,
                "overshoot_bytes":bytes - limit}),
            samples,
        }),
        Ok(_) => None,
        Err(code) => {
            eprintln!("worker_guard_observation_failed: {code}");
            Some(Failure {
                code: MEMORY_OBSERVATION_FAILED,
                limit_detail: Value::Null,
                samples,
            })
        }
    }
}

fn watch(
    shared: &Shared,
    child: &Slot,
    pid: u32,
    limit: u64,
    (cadence, entries): (Duration, u64),
    mut probe: Box<dyn MemoryProbe>,
    mut pace: Box<dyn Pace>,
) {
    let (lock, changed) = &**shared;
    // Waits up to `wait` for a stop; true once stopped (or the lock is poisoned).
    let mut rest = |wait: Duration| -> bool {
        let Ok(state) = lock.lock() else {
            eprintln!("worker_guard_poisoned");
            return true;
        };
        match changed.wait_timeout_while(state, wait, |s| !s.stopped) {
            Ok((state, _)) => state.stopped,
            Err(_) => {
                eprintln!("worker_guard_poisoned");
                true
            }
        }
    };
    loop {
        if pace.period(cadence, &mut rest) {
            return;
        }
        let (samples, disk) = {
            let Ok(mut state) = lock.lock() else {
                eprintln!("worker_guard_poisoned");
                return;
            };
            if state.stopped {
                return;
            }
            state.samples += 1;
            (state.samples, state.disk.clone())
        };
        // v0.1.4 `_check` order: the storage walk, then the memory reading.
        let storage = disk
            .as_ref()
            .and_then(|disk| storage_verdict(disk, entries, pace.as_mut(), cadence, samples));
        let verdict = match storage {
            Some(failure) => failure,
            None => match memory_verdict(probe.as_mut(), pid, limit, samples) {
                Some(failure) => failure,
                None => continue,
            },
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
