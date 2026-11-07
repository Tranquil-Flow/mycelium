// SPDX-License-Identifier: AGPL-3.0-only
//! Durable ownership of a **group-contained** native worker and descendants.
//! A worker admitted here must not call setsid/setpgid or delegate device resources
//! outside the group. This is not a sandbox for arbitrary/untrusted executables.
//! Recovery never signals a saved PID/group: PID reuse cannot kill a successor.
//! A surviving recovered group remains cleanup-unproven until independently gone.
//! Resource directories must have a privately owned, quiescent parent; foreign or
//! replaced contents are never recursively removed. L8 must account device residue.
use crate::{Residue, Supervisor, Ticket, WorkerAdapter, WorkerError};
use control_runtime::Store;
use rustix::process::{
    Pid, Signal, WaitId, WaitIdOptions, kill_process_group, test_kill_process_group, waitid,
};
use serde::{Deserialize, Serialize};
use std::{
    os::unix::{
        fs::{DirBuilderExt, MetadataExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
};
type Result<T> = std::result::Result<T, WorkerError>;
fn io(e: rustix::io::Errno) -> WorkerError {
    WorkerError::Io(e.into())
}
fn storage(e: control_runtime::Error) -> WorkerError {
    WorkerError::Storage(e)
}
#[derive(Serialize, Deserialize)]
struct Record {
    ticket: Ticket,
    root: PathBuf,
    device: u64,
    inode: u64,
    group: Option<u32>,
}
impl Record {
    fn encode(&self) -> Result<Vec<u8>> {
        serde_json::to_vec(self).map_err(|_| WorkerError::Refused("worker_journal_invalid"))
    }
    fn pid(&self) -> Result<Pid> {
        self.group
            .filter(|p| *p > 1)
            .and_then(|p| i32::try_from(p).ok())
            .and_then(Pid::from_raw)
            .ok_or(WorkerError::Refused("worker_spawn_outcome_unknown"))
    }
    fn group_gone(&self) -> Result<bool> {
        match test_kill_process_group(self.pid()?) {
            Ok(()) => Ok(false),
            Err(rustix::io::Errno::SRCH) => Ok(true),
            Err(e) => Err(io(e)),
        }
    }
    fn remove(&self) -> Result<()> {
        let m = std::fs::symlink_metadata(&self.root)?;
        if !m.is_dir() || (m.dev(), m.ino()) != (self.device, self.inode) {
            return Err(WorkerError::Refused("worker_resource_identity_changed"));
        }
        std::fs::remove_dir(&self.root)?;
        if let Some(parent) = self.root.parent() {
            std::fs::File::open(parent)?.sync_all()?;
        }
        Ok(())
    }
}
pub struct GroupChild {
    child: Child,
    record: Record,
    reaped: bool,
    removed: bool,
    signalled: bool,
}
impl GroupChild {
    /// Persist Prepared before spawn, then the new process-group identity before
    /// returning. A crash in between refuses recovery rather than inventing absence.
    pub fn spawn(
        command: &mut Command,
        root: &Path,
        store: &mut Store,
        name: &str,
        ticket: Ticket,
    ) -> Result<Self> {
        Self::spawn_passing(command, root, store, name, ticket, &[])
    }
    /// Spawn with the descriptors in `pass` (and only those above stderr) inherited; see
    /// [`crate::fds::confine_descriptors_except`] for the contract and refusals. A refusal
    /// happens before any directory or journal exists.
    pub fn spawn_passing(
        command: &mut Command,
        root: &Path,
        store: &mut Store,
        name: &str,
        ticket: Ticket,
        pass: &[std::os::fd::RawFd],
    ) -> Result<Self> {
        if !root.is_absolute() {
            return Err(WorkerError::Refused("worker_resource_path_invalid"));
        }
        // Before the Prepared journal or directory exist: a refusal leaves no residue.
        crate::fds::confine_descriptors_except(command, pass)?;
        // Name is a caller-owned stable reservation identity, not an arriving PID.
        if store.load_document(name).map_err(storage)?.is_some() {
            return Err(WorkerError::Refused("worker_journal_exists"));
        }
        std::fs::DirBuilder::new().mode(0o700).create(root)?;
        let m = std::fs::symlink_metadata(root)?;
        let mut record = Record {
            ticket,
            root: root.into(),
            device: m.dev(),
            inode: m.ino(),
            group: None,
        };
        let revision = match store.save_document(name, None, &record.encode()?) {
            Ok(r) => r,
            Err(e) => {
                record.remove()?;
                return Err(storage(e));
            }
        };
        let mut child = match command
            .process_group(0)
            .current_dir(root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                record.remove()?;
                return Err(WorkerError::Io(e));
            }
        };
        record.group = Some(child.id());
        if let Err(error) = store.save_document(name, Some(revision), &record.encode()?) {
            // Child handle is still unreaped, pinning this exact newly created group.
            kill_process_group(record.pid()?, Signal::KILL)
                .or_else(|error| {
                    if error == rustix::io::Errno::SRCH {
                        Ok(())
                    } else {
                        Err(error)
                    }
                })
                .map_err(io)?;
            child.wait()?;
            // Prepared remains durable and fail-closed. Do not erase residue on error.
            return Err(storage(error));
        }
        Ok(Self {
            child,
            record,
            reaped: false,
            removed: false,
            signalled: false,
        })
    }
    /// WNOWAIT retains the zombie's PID allocation while descendants are stopped.
    /// External/global child reapers are forbidden; ECHILD revokes signalling.
    fn exit_ready(&self) -> Result<bool> {
        match waitid(
            WaitId::Pid(self.record.pid()?),
            WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
        ) {
            Ok(status) => Ok(status.is_some()),
            Err(rustix::io::Errno::CHILD) => {
                Err(WorkerError::Refused("worker_exact_child_ownership_lost"))
            }
            Err(error) => Err(io(error)),
        }
    }
    pub fn ticket(&self) -> &Ticket {
        &self.record.ticket
    }
    /// Observation only; ownership remains the unreaped Child, never this number.
    pub fn pid(&self) -> u32 {
        self.child.id()
    }
}
impl WorkerAdapter for GroupChild {
    fn request_stop(&mut self) -> Result<()> {
        if self.signalled || self.record.group_gone()? {
            return Ok(());
        }
        // Reaping permits PID reuse. Never signal the numeric group after that point.
        if self.reaped {
            return Err(WorkerError::Refused("worker_group_handle_lost"));
        }
        self.exit_ready()?;
        match kill_process_group(self.record.pid()?, Signal::KILL) {
            Ok(()) => self.signalled = true,
            Err(rustix::io::Errno::SRCH) => self.signalled = true,
            Err(e) => return Err(io(e)),
        }
        Ok(())
    }
    fn observe(&mut self) -> Result<Residue> {
        if !self.reaped && self.exit_ready()? {
            // Stop the remaining owned group while the leader is still unreaped.
            // Only then may the kernel recycle its numeric group leader identity.
            let denied = match self.request_stop() {
                Ok(()) => None,
                Err(WorkerError::Io(error))
                    if error.kind() == std::io::ErrorKind::PermissionDenied =>
                {
                    Some(error)
                }
                Err(error) => return Err(error),
            };
            self.child.wait()?;
            self.reaped = true;
            // Darwin can deny a probe of an all-zombie group. EPERM is NOT
            // absence: after reaping our exact exited child, independently
            // require ESRCH. Any surviving/unknown group retains the error.
            if let Some(error) = denied
                && !self.record.group_gone()?
            {
                return Err(WorkerError::Io(error));
            }
        }
        let gone = self.reaped && self.record.group_gone()?;
        if gone && !self.removed {
            self.record.remove()?;
            self.removed = true;
        }
        Ok(Residue {
            process_gone: gone,
            resources_gone: self.removed,
        })
    }
}
pub struct RecoveredGroup {
    record: Record,
    removed: bool,
}
/// Restore only for the exact durable reservation ticket. A stored claimed command
/// or peer-signed cleanup receipt is never an input to this environmental proof.
pub fn recover(store: &Store, name: &str, expected: &Ticket) -> Result<Supervisor<RecoveredGroup>> {
    let (_, bytes) = store
        .load_document(name)
        .map_err(storage)?
        .ok_or(WorkerError::Refused("worker_journal_missing"))?;
    let record: Record = serde_json::from_slice(&bytes)
        .map_err(|_| WorkerError::Refused("worker_journal_invalid"))?;
    if record.ticket != *expected {
        return Err(WorkerError::Refused("worker_generation_stale"));
    }
    record.pid()?;
    Ok(Supervisor::new(
        record.ticket.clone(),
        RecoveredGroup {
            record,
            removed: false,
        },
    ))
}
impl WorkerAdapter for RecoveredGroup {
    fn request_stop(&mut self) -> Result<()> {
        if self.record.group_gone()? {
            Ok(())
        } else {
            Err(WorkerError::Refused("worker_group_handle_lost"))
        }
    }
    fn observe(&mut self) -> Result<Residue> {
        let gone = self.record.group_gone()?;
        if gone && !self.removed {
            self.record.remove()?;
            self.removed = true;
        }
        Ok(Residue {
            process_gone: gone,
            resources_gone: self.removed,
        })
    }
}
