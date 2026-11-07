// SPDX-License-Identifier: AGPL-3.0-only
//! Local reap proof for a run whose native worker died.
//!
//! v0.1.4 (`mycelium_a13_macos/worker.py:1590-1620 @ f4e366e9`) calls a worker's cleanup
//! proven when `close()` reaped its owned process (`_cleanup_complete`) and no process group
//! is left (`process_group_present()` is `killpg(pid, 0)`); after a death that is all it takes
//! for the run to end `failed` and the next run to be allowed (executed oracle
//! `FX-SLICE1-post-worker-death`). Here the owner supervises both native children itself, so
//! the same fact is: every child it spawned was reaped by its supervisor (`ProcessReport::
//! cleanup_proven`, no errors), no spawned pid is still in the process table, and the member's
//! staged model copy is gone.
//!
//! What this is NOT: it is not a signed cleanup quorum. Since round 9 (RU-020) the member and the
//! owner leaf are AGENTS that survive their native workers and sign, so a dead WORKER settles
//! the session as `Failed` from a real quorum and never needs this proof. It remains the
//! fallback for the case the signer itself is gone (an agent process killed): then no receipt can
//! exist, the request session stays without a `Terminal` (L6 invariant: only a real quorum
//! produces one), and this proof only lets the host verify the Quit.
use rustix::{io::Errno, process::Pid};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

/// One spawned child as its supervisor reported it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reaped {
    pub role: &'static str,
    pub pid: u32,
    /// The supervisor reaped the exact process and released its reservation without errors.
    pub proven: bool,
}
/// Why a proof was not given. Closed set; the code is for logs, never for the public API.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    NothingSpawned,
    UnknownWorker(u32),
    NotReaped(u32),
    PidPresent(u32),
    StagedCopyRemains,
}
impl Refusal {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NothingSpawned => "reap_nothing_spawned",
            Self::UnknownWorker(_) => "reap_unknown_worker",
            Self::NotReaped(_) => "reap_child_not_reaped",
            Self::PidPresent(_) => "reap_pid_present",
            Self::StagedCopyRemains => "reap_staged_copy_remains",
        }
    }
}
/// The children one run spawned, entered as they are reaped.
#[derive(Clone, Default)]
pub struct Ledger(Arc<Mutex<Vec<Reaped>>>);
/// Proof that a failed run left no process and no staged copy. Only [`Ledger::prove`] makes one.
#[derive(Clone, Debug)]
pub struct ReapProof {
    children: Vec<Reaped>,
}
impl ReapProof {
    pub fn children(&self) -> &[Reaped] {
        &self.children
    }
}
/// True once the process table holds no process with this pid (a zombie still counts as
/// present, like v0.1.4's `killpg(pid, 0)`; the pid cannot have been recycled while we hold it).
fn pid_gone(pid: u32) -> bool {
    i32::try_from(pid)
        .ok()
        .and_then(Pid::from_raw)
        .is_some_and(|raw| rustix::process::test_kill_process(raw) == Err(Errno::SRCH))
}
impl Ledger {
    fn rows(&self) -> MutexGuard<'_, Vec<Reaped>> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }
    pub fn record(&self, role: &'static str, pid: u32, proven: bool) {
        let mut rows = self.rows();
        // A child is entered once; a later report for the same pid can only keep it unproven.
        if let Some(row) = rows.iter_mut().find(|row| row.pid == pid) {
            row.proven &= proven;
        } else {
            rows.push(Reaped { role, pid, proven });
        }
    }
    pub fn entries(&self) -> Vec<Reaped> {
        self.rows().clone()
    }
    /// `died_pid` is the worker the watch saw end; `staged_discarded` is the outcome of the
    /// staged-copy discard (a copy that could not be removed is a residue).
    pub fn prove(&self, died_pid: u32, staged_discarded: bool) -> Result<ReapProof, Refusal> {
        let children = self.entries();
        if children.is_empty() {
            return Err(Refusal::NothingSpawned);
        }
        if !children.iter().any(|child| child.pid == died_pid) {
            return Err(Refusal::UnknownWorker(died_pid));
        }
        if let Some(child) = children.iter().find(|child| !child.proven) {
            return Err(Refusal::NotReaped(child.pid));
        }
        if let Some(child) = children.iter().find(|child| !pid_gone(child.pid)) {
            return Err(Refusal::PidPresent(child.pid));
        }
        if !staged_discarded {
            return Err(Refusal::StagedCopyRemains);
        }
        Ok(ReapProof { children })
    }
}
