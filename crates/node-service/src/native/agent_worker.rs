// SPDX-License-Identifier: AGPL-3.0-only
//! The member AGENT's handle on its NATIVE WORKER (RU-020). The agent is the signer: it
//! survives its worker and, once the worker is reaped, signs the cleanup receipt, so a session
//! whose worker died still reaches `Terminal::Failed` through a real quorum (v0.1.4: the app
//! process signs; the compute worker is its own session-leader child, `r9-oracle/`).
//!
//! The worker is spawned through the same supervised, descriptor-confined path as every other
//! child (`spawn::spawn_worker`). Its private link is one end of a socketpair inherited as the
//! worker's descriptor 1. The supervisor keeps a copy of the worker's end for as long as it
//! lives, so a dead worker is not seen as end-of-stream: the agent polls the link at the
//! registered `_watch` cadence and asks the exact-pid [`Watch`] between polls, like v0.1.4.
use super::*;
use crate::{
    liveness::Watch,
    worker_link::{Link, LinkError},
};
use backend_mlx::process::OwnedWorker;
use stage_contract::Input;
use std::{
    os::{fd::OwnedFd, unix::net::UnixStream, unix::process::ExitStatusExt},
    process::Stdio,
};

/// How the worker ended, as its supervisor reported it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerExit {
    pub pid: u32,
    pub exit_status: Option<i32>,
    pub signal: Option<i32>,
    /// The exact owned process was reaped and its reservation released without errors.
    pub reaped: bool,
}
impl WorkerExit {
    pub fn json(&self) -> Value {
        json!({"code":crate::liveness::NATIVE_EXITED,"pid":self.pid,"exit_status":self.exit_status,"signal":self.signal,"reaped":self.reaped})
    }
}
/// Why a worker command failed.
#[derive(Debug)]
pub enum Fault {
    /// The worker's process is gone (killed, crashed, exited).
    Died(WorkerExit),
    Other(String),
}
impl std::fmt::Display for Fault {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Died(_) => f.write_str(crate::liveness::NATIVE_EXITED),
            Self::Other(code) => f.write_str(code),
        }
    }
}
impl std::error::Error for Fault {}

pub struct AgentWorker {
    process: OwnedWorker,
    pid: u32,
    link: Link,
    watch: Watch,
    deadline: Instant,
    exit: Option<WorkerExit>,
}
impl AgentWorker {
    /// Spawn the worker and have it load its stage. `memory` is the planner's assigned share.
    pub fn start(
        checkpoint: &Path,
        range: std::ops::Range<u32>,
        total: u32,
        memory: u64,
        tokens: u32,
        id: &str,
        generation: u64,
    ) -> Result<Self> {
        crate::native_selection::select(None, range.clone(), total, memory, tokens)?;
        let head = json!({"command":"prepare","checkpoint":checkpoint.to_str().ok_or("checkpoint_path")?,
            "start":range.start,"end":range.end,"total":total,"memory":memory,"tokens":tokens,"id":id,"generation":generation});
        Self::spawn_prepared(head, memory)
    }
    /// Explicit authenticated source metadata, carried only on the private link.
    /// The caller owns source provenance/immutability; this is not an authority type.
    pub fn start_assigned(
        checkpoint: &Path,
        image: &crate::native_selection::AssignedImage,
        memory: u64,
        tokens: u32,
        id: &str,
        generation: u64,
    ) -> Result<Self> {
        crate::native_selection::select(
            Some(image),
            image.layers.clone(),
            image.total_layers,
            memory,
            tokens,
        )?;
        let head = json!({"command":"prepare","checkpoint":checkpoint.to_str().ok_or("checkpoint_path")?,
            "start":image.layers.start,"end":image.layers.end,"total":image.total_layers,
            "memory":memory,"tokens":tokens,"id":id,"generation":generation,"stage_image":image});
        Self::spawn_prepared(head, memory)
    }
    fn spawn_prepared(head: Value, memory: u64) -> Result<Self> {
        backend_mlx::admission::digest_hex(text(&head, "id")?)?;
        let deadline =
            Instant::now() + std::time::Duration::from_millis(span(B::MemberChosenWall).millis());
        let (agent_end, worker_end) = UnixStream::pair()?;
        let mut command = Command::new(std::env::current_exe()?);
        command
            .arg("native-worker")
            .stdout(Stdio::from(OwnedFd::from(worker_end)));
        let reservation = budgets::Reservation {
            resident: Bytes::new(memory),
            staged: Bytes::ZERO,
            staging_overhead: Bytes::ZERO,
            scratch: Bytes::ZERO,
            kv: Bytes::ZERO,
        };
        let pool = ResourcePool::new(
            reservation
                .total()
                .map_err(|e| format!("worker reservation: {e:?}"))?,
        );
        let process = spawn::spawn_worker(
            command,
            pool,
            reservation,
            ProcessLimits {
                deadline,
                close: span(B::RouterClose),
                reap: span(B::WorkerCleanupReserve),
                poll: span(B::RegistryPoll),
            },
        )?;
        let pid = process.pid();
        let watch = Watch::start(deadline)?;
        watch.add("native_worker", pid);
        phase::mark("native_worker_spawned", &format!("pid={pid}"));
        let mut worker = Self {
            process,
            pid,
            link: Link::new(agent_end, service::duration(B::MemberChosenWall))?,
            watch,
            deadline,
            exit: None,
        };
        worker.request(&head, &[])?;
        Ok(worker)
    }
    /// One command; the reply is polled at the `_watch` cadence so a death is noticed at once.
    fn request(
        &mut self,
        head: &Value,
        payload: &[u8],
    ) -> std::result::Result<(Value, Vec<u8>), Fault> {
        if let Some(exit) = &self.exit {
            return Err(Fault::Died(exit.clone()));
        }
        match self.link.send(head, payload) {
            Ok(()) => (),
            Err(LinkError::Closed) => return Err(self.died()),
            Err(error) => return Err(Fault::Other(error.to_string())),
        }
        let poll = service::duration(B::RegistryPoll);
        loop {
            match self.link.poll(poll) {
                Ok(Some((reply, body))) => {
                    return match reply.get("error").and_then(Value::as_str) {
                        Some(code) => Err(Fault::Other(code.to_owned())),
                        None => Ok((reply, body)),
                    };
                }
                Ok(None) => {
                    if self.watch.death().is_some() {
                        return Err(self.died());
                    }
                    if Instant::now() >= self.deadline {
                        return Err(Fault::Other("worker_wall_limit_exceeded".into()));
                    }
                }
                Err(LinkError::Closed) => return Err(self.died()),
                Err(error) => return Err(Fault::Other(error.to_string())),
            }
        }
    }
    /// Reap the dead worker (its supervisor already has, or does now) and keep how it ended.
    fn died(&mut self) -> Fault {
        if let Some(exit) = &self.exit {
            return Fault::Died(exit.clone());
        }
        // Stop our own watch first: from here the supervisor, not a death, ends the child.
        self.watch.quiesce();
        let cause = self.watch.death();
        let report = match self
            .process
            .cancel()
            .and_then(|()| self.process.wait().cloned())
        {
            Ok(report) => Some(report),
            Err(error) => {
                eprintln!("{error:?}; dead native worker reap failed");
                None
            }
        };
        let exit = WorkerExit {
            pid: self.pid,
            exit_status: report
                .as_ref()
                .and_then(|r| r.status)
                .and_then(|s| s.code())
                .or(cause.as_ref().and_then(|c| c.exit_status)),
            signal: report
                .as_ref()
                .and_then(|r| r.status)
                .and_then(|s| s.signal())
                .or(cause.as_ref().and_then(|c| c.signal)),
            reaped: report
                .as_ref()
                .is_some_and(|r| r.cleanup_proven && r.errors.is_empty()),
        };
        phase::mark(
            "native_worker_died",
            &format!(
                "pid={},signal={:?},reaped={}",
                exit.pid, exit.signal, exit.reaped
            ),
        );
        self.exit = Some(exit.clone());
        Fault::Died(exit)
    }
    /// Run one stage step; the result is the L5 activation payload (MYAC), unframed.
    pub fn step(&mut self, input: Input) -> std::result::Result<stage_contract::Activation, Fault> {
        let (head, payload) = match input {
            Input::Tokens(ids) => (json!({"command":"step","tokens":ids}), Vec::new()),
            Input::Hidden(a) => (
                json!({"command":"step"}),
                activation::encode(&a).map_err(|e| Fault::Other(e.0.into()))?,
            ),
        };
        let (_, body) = self.request(&head, &payload)?;
        activation::decode(&body).map_err(|e| Fault::Other(e.0.into()))
    }
    /// Close the stage normally: the worker reports its resource stats and exits, then it is
    /// reaped. Returns the stats and the reaped exit.
    pub fn close(mut self) -> std::result::Result<(Value, WorkerExit), Fault> {
        let (reply, _) = self.request(&json!({"command":"close"}), &[])?;
        self.watch.quiesce();
        let report = self
            .process
            .wait()
            .map_err(|e| Fault::Other(format!("worker_reap_failed:{e:?}")))?
            .clone();
        let exit = WorkerExit {
            pid: self.pid,
            exit_status: report.status.and_then(|s| s.code()),
            signal: report.status.and_then(|s| s.signal()),
            reaped: report.cleanup_proven && report.errors.is_empty(),
        };
        if !exit.reaped || exit.exit_status != Some(0) {
            return Err(Fault::Other("worker_reap_unproven".into()));
        }
        Ok((reply["stats"].clone(), exit))
    }
}
