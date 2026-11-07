// SPDX-License-Identifier: AGPL-3.0-only
//! The one place a node-service process is spawned. Every child (native worker, owner
//! leaf, host probe) is built here so L6's canonical descriptor confinement is the last
//! `pre_exec` registered and the child holds exactly descriptors 0-2.
use crate::loopback::Result;
use backend_mlx::process::{OwnedWorker, ProcessLimits};
use backend_mlx::resources::ResourcePool;
use std::{
    io,
    process::{Command, Output},
};
/// Confine `command`, called after every other customization of it.
pub fn confine(command: &mut Command) -> io::Result<()> {
    node::process::confine_descriptors(command)
}
/// Run a short host program (`sysctl`, `lsof`) and capture its output.
pub fn confined_output(program: &str, args: &[&str]) -> io::Result<Output> {
    let mut command = Command::new(program);
    command.args(args);
    confine(&mut command)?;
    command.output()
}
/// Spawn a supervised native worker from a fully configured `command`.
pub fn spawn_worker(
    mut command: Command,
    pool: ResourcePool,
    amount: budgets::Reservation,
    limits: ProcessLimits,
) -> Result<OwnedWorker> {
    confine(&mut command)?;
    Ok(OwnedWorker::spawn(command, pool, amount, limits)?)
}

/// A supervised worker whose reaping is entered in a [`Ledger`] when it is dropped (or already
/// waited): `cancel` + `wait` on the exact owned child, exactly what `OwnedWorker`'s own drop
/// does, but its [`ProcessReport`](backend_mlx::process::ProcessReport) is kept as evidence.
pub struct Tracked {
    worker: Option<OwnedWorker>,
    pid: u32,
    role: &'static str,
    ledger: crate::reap::Ledger,
}
impl Tracked {
    pub fn new(worker: OwnedWorker, role: &'static str, ledger: &crate::reap::Ledger) -> Self {
        Self {
            pid: worker.pid(),
            worker: Some(worker),
            role,
            ledger: ledger.clone(),
        }
    }
}
impl Tracked {
    /// Hand the worker on to another owner (the success path's cleanup supervisor, after the
    /// child was waited). Tracking ends here: a later failure finds no ledger entry and is
    /// therefore never proven clean.
    pub fn into_inner(mut self) -> OwnedWorker {
        self.worker.take().expect("worker present until drop")
    }
}
impl std::ops::Deref for Tracked {
    type Target = OwnedWorker;
    fn deref(&self) -> &OwnedWorker {
        self.worker.as_ref().expect("worker present until drop")
    }
}
impl std::ops::DerefMut for Tracked {
    fn deref_mut(&mut self) -> &mut OwnedWorker {
        self.worker.as_mut().expect("worker present until drop")
    }
}
impl Drop for Tracked {
    fn drop(&mut self) {
        let Some(mut worker) = self.worker.take() else {
            return;
        };
        if let Err(error) = worker.cancel() {
            eprintln!("{error}; tracked worker cancel failed");
        }
        let proven = match worker.wait() {
            Ok(report) => report.cleanup_proven && report.errors.is_empty(),
            Err(error) => {
                eprintln!("{error}; tracked worker cleanup unproven");
                false
            }
        };
        self.ledger.record(self.role, self.pid, proven);
    }
}
