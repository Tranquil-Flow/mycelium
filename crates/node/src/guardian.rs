// SPDX-License-Identifier: AGPL-3.0-only
//! Persistent process ownership: a separate guardian retains the exact Child
//! handle across application death. An exclusive inherited pipe is the lifetime
//! capability; EOF requests cleanup. Saved PIDs NEVER grant signal authority.
//! Trusted group-contained executables only; combine independent device owners
//! with CleanupDomains. Guardian death itself remains fail-closed, not recovered.
use crate::{Residue, Supervisor, Ticket, WorkerAdapter, WorkerError, recovery::GroupChild};
use budgets::{BudgetId as B, Entries};
use control_runtime::Store;
use serde::{Deserialize, Serialize};
use std::{
    ffi::OsString,
    io::{Read, Write},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    time::{Duration, Instant},
};
type Result<T> = std::result::Result<T, WorkerError>;
fn duration(id: B) -> Duration {
    Duration::from_millis(
        id.span(Entries::ZERO)
            .expect("registered duration")
            .millis(),
    )
}
fn open(path: &Path) -> Result<Store> {
    Store::open_with_policy(
        path,
        None,
        B::PyMyceliumA13MacosManagedControlManagedcontrolmailboxConnectTimeoutL823
            .span(Entries::ZERO)
            .expect("registered SQLite wait"),
    )
    .map_err(WorkerError::Storage)
}
fn encode<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| WorkerError::Refused("guardian_document_invalid"))
}
#[derive(Serialize, Deserialize, Clone, PartialEq, Eq)]
enum Phase {
    Prepared,
    Running { guardian: u32, worker: u32 },
    Complete,
    Failed,
}
#[derive(Serialize, Deserialize, Clone)]
struct Record {
    ticket: Ticket,
    root: PathBuf,
    phase: Phase,
}
fn load(store: &Store, name: &str, ticket: &Ticket) -> Result<(u64, Record)> {
    let (revision, bytes) = store
        .load_document(name)
        .map_err(WorkerError::Storage)?
        .ok_or(WorkerError::Refused("guardian_journal_missing"))?;
    let record: Record = serde_json::from_slice(&bytes)
        .map_err(|_| WorkerError::Refused("guardian_journal_invalid"))?;
    if &record.ticket != ticket {
        return Err(WorkerError::Refused("worker_generation_stale"));
    }
    Ok((revision, record))
}
fn save(store: &mut Store, name: &str, revision: u64, record: &Record) -> Result<u64> {
    store
        .save_document(name, Some(revision), &encode(record)?)
        .map_err(WorkerError::Storage)
}
#[derive(Serialize, Deserialize)]
struct Launch {
    db: PathBuf,
    name: String,
    ticket: Ticket,
    root: PathBuf,
    program: PathBuf,
    args: Vec<OsString>,
    env: Vec<(OsString, Option<OsString>)>,
    /// Descriptors the worker inherits (also inherited by the guardian itself).
    #[serde(default)]
    pass_fds: Vec<i32>,
}
/// App-local handle. Drop/Stop closes only our lifetime pipe; the independent
/// guardian performs and journals cleanup before exiting. Never clone the pipe.
pub struct PersistentChild {
    guardian: Child,
    lifetime: Option<ChildStdin>,
    store: Store,
    name: String,
    ticket: Ticket,
    reaped: bool,
}
/// Deliberately smaller than std::process::Command: no silently discarded
/// env_clear, pre_exec, stdio or cwd options. Environment is inherited with these
/// explicit overrides/removals; resource cwd and null stdio belong to the guardian.
pub struct WorkerCommand {
    pub program: PathBuf,
    pub arguments: Vec<OsString>,
    pub environment: Vec<(OsString, Option<OsString>)>,
}
impl PersistentChild {
    /// Executable is the packaged `node-guardian` of this same build. `command`
    /// supplies an absolute program, argv and explicit environment overrides;
    /// guardian forces an owned cwd and null worker stdio. No shell is inserted.
    pub fn spawn(
        executable: &Path,
        command: &WorkerCommand,
        root: &Path,
        db: &Path,
        name: &str,
        ticket: Ticket,
    ) -> Result<Self> {
        Self::spawn_passing(executable, command, root, db, name, ticket, &[])
    }
    /// As [`Self::spawn`], with the descriptors in `pass` inherited by the guardian *and*
    /// passed on to the worker (the guardian re-validates and re-confines). Numbers are
    /// the same in all three processes; give the worker its number out of band
    /// (argument/environment). The guardian keeps its own copies for its lifetime. A
    /// refusal happens before the Prepared journal exists.
    pub fn spawn_passing(
        executable: &Path,
        command: &WorkerCommand,
        root: &Path,
        db: &Path,
        name: &str,
        ticket: Ticket,
        pass: &[std::os::fd::RawFd],
    ) -> Result<Self> {
        if !executable.is_absolute()
            || !root.is_absolute()
            || !db.is_absolute()
            || !command.program.is_absolute()
        {
            return Err(WorkerError::Refused("guardian_path_invalid"));
        }
        let mut launcher = Command::new(executable);
        // Before the Prepared journal exists: a refusal leaves no residue.
        crate::fds::confine_descriptors_except(&mut launcher, pass)?;
        let launch = Launch {
            db: db.into(),
            name: name.into(),
            ticket: ticket.clone(),
            root: root.into(),
            program: command.program.clone(),
            args: command.arguments.clone(),
            env: command.environment.clone(),
            pass_fds: pass.to_vec(),
        };
        let bytes = encode(&launch)?;
        if bytes.len() as u64
            > B::PyMyceliumA13MacosManagedControlMaxFrameBytesL95
                .bytes()
                .expect("registered control frame")
                .get()
        {
            return Err(WorkerError::Refused("guardian_frame_oversized"));
        }
        let mut store = open(db)?;
        store
            .save_document(
                name,
                None,
                &encode(&Record {
                    ticket: ticket.clone(),
                    root: root.into(),
                    phase: Phase::Prepared,
                })?,
            )
            .map_err(WorkerError::Storage)?;
        let mut guardian = launcher
            .process_group(0)
            .arg(bytes.len().to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()?;
        let mut lifetime = guardian
            .stdin
            .take()
            .ok_or(WorkerError::Refused("guardian_pipe_missing"))?;
        if let Err(error) = lifetime.write_all(&bytes) {
            drop(lifetime);
            guardian.wait()?;
            return Err(WorkerError::Io(error));
        }
        let mut owned = Self {
            guardian,
            lifetime: Some(lifetime),
            store,
            name: name.into(),
            ticket,
            reaped: false,
        };
        let end = Instant::now() + duration(B::WorkerStartup);
        loop {
            match load(&owned.store, &owned.name, &owned.ticket)?.1.phase {
                Phase::Running { .. } => return Ok(owned),
                Phase::Failed => return Err(WorkerError::Refused("guardian_start_failed")),
                Phase::Complete => {
                    return Err(WorkerError::Refused("guardian_exited_before_handoff"));
                }
                Phase::Prepared => {}
            }
            if owned.guardian.try_wait()?.is_some() {
                owned.reaped = true;
                return Err(WorkerError::Refused("guardian_exited_before_handoff"));
            }
            if Instant::now() >= end {
                return Err(WorkerError::Refused("guardian_start_timeout"));
            }
            std::thread::sleep(duration(B::OwnerPoll));
        }
    }
    /// Observability only. Neither PID can be used to recover signal authority.
    pub fn process_ids(&self) -> Result<(u32, u32)> {
        match load(&self.store, &self.name, &self.ticket)?.1.phase {
            Phase::Running { guardian, worker } => Ok((guardian, worker)),
            _ => Err(WorkerError::Refused("guardian_not_running")),
        }
    }
}
impl WorkerAdapter for PersistentChild {
    fn request_stop(&mut self) -> Result<()> {
        drop(self.lifetime.take());
        Ok(())
    }
    fn observe(&mut self) -> Result<Residue> {
        let record = load(&self.store, &self.name, &self.ticket)?.1;
        let residue = observation(&record)?;
        if !self.reaped
            && let Some(status) = self.guardian.try_wait()?
        {
            self.reaped = true;
            if !status.success() {
                return Err(WorkerError::Refused("guardian_cleanup_failed"));
            }
        }
        Ok(Residue {
            process_gone: residue.process_gone && self.reaped,
            resources_gone: residue.resources_gone,
        })
    }
}
impl Drop for PersistentChild {
    fn drop(&mut self) {
        drop(self.lifetime.take());
        let end = Instant::now() + duration(B::PreviousClose);
        while !self.reaped {
            match self.guardian.try_wait() {
                Ok(Some(status)) => {
                    self.reaped = true;
                    if !status.success() {
                        eprintln!("guardian_cleanup_failed: {status}");
                    }
                }
                Ok(None) => {
                    if Instant::now() >= end {
                        eprintln!("guardian_reap_timeout: durable ownership retained");
                        break;
                    }
                    std::thread::sleep(duration(B::OwnerPoll));
                }
                Err(error) => {
                    eprintln!("guardian_reap_failed: {error}");
                    break;
                }
            }
        }
    }
}
fn observation(record: &Record) -> Result<Residue> {
    match record.phase {
        Phase::Complete => match std::fs::symlink_metadata(&record.root) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Residue {
                process_gone: true,
                resources_gone: true,
            }),
            Ok(_) => Err(WorkerError::Refused("worker_resource_identity_changed")),
            Err(e) => Err(WorkerError::Io(e)),
        },
        Phase::Failed => Err(WorkerError::Refused("guardian_cleanup_failed")),
        _ => Ok(Residue {
            process_gone: false,
            resources_gone: false,
        }),
    }
}
/// Read-only recovery. The still-running guardian, not the restarted app, owns
/// signal authority. Original-owner death closes its exclusive lifetime pipe.
pub struct PersistentRecovery {
    store: Store,
    name: String,
    ticket: Ticket,
}
pub fn recover(db: &Path, name: &str, ticket: &Ticket) -> Result<Supervisor<PersistentRecovery>> {
    let store = open(db)?;
    load(&store, name, ticket)?;
    Ok(Supervisor::new(
        ticket.clone(),
        PersistentRecovery {
            store,
            name: name.into(),
            ticket: ticket.clone(),
        },
    ))
}
impl WorkerAdapter for PersistentRecovery {
    fn request_stop(&mut self) -> Result<()> {
        if observation(&load(&self.store, &self.name, &self.ticket)?.1)?.process_gone {
            Ok(())
        } else {
            Err(WorkerError::Refused(
                "guardian_lifetime_capability_not_owned",
            ))
        }
    }
    fn observe(&mut self) -> Result<Residue> {
        observation(&load(&self.store, &self.name, &self.ticket)?.1)
    }
}
/// Entry point for the same-build helper. Launch bytes travel only through the
/// private pipe, never the journal or argv. EOF is the exclusive Stop capability.
pub fn run(length: usize) -> Result<()> {
    if length == 0
        || length as u64
            > B::PyMyceliumA13MacosManagedControlMaxFrameBytesL95
                .bytes()
                .expect("registered control frame")
                .get()
    {
        return Err(WorkerError::Refused("guardian_frame_oversized"));
    }
    let mut stdin = std::io::stdin().lock();
    let mut bytes = vec![0; length];
    stdin.read_exact(&mut bytes)?;
    let launch: Launch = serde_json::from_slice(&bytes)
        .map_err(|_| WorkerError::Refused("guardian_launch_invalid"))?;
    let mut store = open(&launch.db)?;
    let (mut revision, mut record) = load(&store, &launch.name, &launch.ticket)?;
    if record.phase != Phase::Prepared || record.root != launch.root {
        return Err(WorkerError::Refused("guardian_launch_stale"));
    }
    let mut command = Command::new(&launch.program);
    command.args(&launch.args);
    for (k, v) in launch.env {
        if let Some(v) = v {
            command.env(k, v);
        } else {
            command.env_remove(k);
        }
    }
    let result = (|| {
        let mut child = GroupChild::spawn_passing(
            &mut command,
            &launch.root,
            &mut store,
            &format!("{}/group", launch.name),
            launch.ticket.clone(),
            &launch.pass_fds,
        )?;
        record.phase = Phase::Running {
            guardian: std::process::id(),
            worker: child.pid(),
        };
        revision = match save(&mut store, &launch.name, revision, &record) {
            Ok(next) => next,
            Err(error) => {
                child.request_stop()?;
                cleanup(&mut child)?;
                return Err(error);
            }
        };
        let mut stop = [0u8; 1];
        let input = stdin.read(&mut stop);
        // EOF/any byte revokes the exclusive lifetime capability. The guardian
        // still has the unreaped Child, so signal authority survives owner death.
        child.request_stop()?;
        cleanup(&mut child)?;
        input?;
        record.phase = Phase::Complete;
        save(&mut store, &launch.name, revision, &record)?;
        Ok(())
    })();
    if result.is_err() {
        record.phase = Phase::Failed;
        save(&mut store, &launch.name, revision, &record)?;
    }
    result
}
fn cleanup(child: &mut GroupChild) -> Result<()> {
    let end = Instant::now() + duration(B::PreviousClose);
    loop {
        let residue = child.observe()?;
        if residue.process_gone && residue.resources_gone {
            return Ok(());
        }
        if Instant::now() >= end {
            return Err(WorkerError::Refused("guardian_cleanup_timeout"));
        }
        std::thread::sleep(duration(B::OwnerPoll));
    }
}
