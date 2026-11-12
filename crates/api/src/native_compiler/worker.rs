// SPDX-License-Identifier: AGPL-3.0-only
//! Native compute stays in the child; this client only speaks the existing link.
use super::*;
use crate::native_worker_link::Link;
use budgets::{BudgetId, Entries};
use std::{
    os::{fd::OwnedFd, unix::net::UnixStream},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

pub(crate) mod guard;
#[cfg(test)]
mod tests;

fn duration(id: BudgetId) -> Result<Duration> {
    Ok(Duration::from_millis(
        id.span(Entries::ZERO)
            .map_err(|_| Error("budget_unavailable"))?
            .millis(),
    ))
}
/// A failed serving-worker start: the error, and the guard's verdict when the
/// guard stopped the worker during prepare (lane MGF F4: its numbers are kept).
#[derive(Debug)]
pub(crate) struct StartError {
    pub(crate) error: Error,
    pub(crate) guard: Option<guard::Failure>,
}
impl From<Error> for StartError {
    fn from(error: Error) -> Self {
        Self { error, guard: None }
    }
}
pub(crate) struct Worker {
    child: guard::Slot,
    link: Link,
    cancel: Arc<AtomicBool>,
    guard: Option<guard::Guard>,
}
impl Worker {
    pub(crate) fn start(
        config: &Config,
        prepare: &Value,
        cancel: Arc<AtomicBool>,
        source: &Path,
    ) -> Result<Self> {
        Self::start_until(
            config,
            prepare,
            cancel,
            source,
            Instant::now() + duration(BudgetId::MemberChosenWall)?,
        )
    }
    pub(crate) fn start_until(
        config: &Config,
        prepare: &Value,
        cancel: Arc<AtomicBool>,
        source: &Path,
        deadline: Instant,
    ) -> Result<Self> {
        config.verify()?;
        if Instant::now() >= deadline {
            return Err(Error("native_worker_deadline"));
        }
        if cancel.load(Ordering::SeqCst) {
            return Err(Error("compiler_stopped"));
        }
        let (client, server) = UnixStream::pair().map_err(|_| Error("native_socket_failed"))?;
        let mut command = if prepare["command"] == stage_contract::native_shard::COMMAND {
            let home = std::env::var_os("HOME").ok_or(Error("native_shard_path_invalid"))?;
            let profile = super::shard_sandbox::profile(
                Path::new(text(prepare, "checkpoint")?),
                source,
                &PathBuf::from(home).join(".cache/huggingface"),
            )
            .map_err(Error)?;
            let mut command = Command::new("/usr/bin/sandbox-exec");
            command.args(["-p", &profile]).arg(&config.binary);
            command
        } else {
            Command::new(&config.binary)
        };
        let child = command
            .arg("native-worker")
            .stdin(Stdio::null())
            .stdout(Stdio::from(OwnedFd::from(server)))
            .spawn()
            .map_err(|_| Error("native_worker_start_failed"))?;
        // Release the parent's copy of the child's socket end now: a child that
        // dies during prepare must surface as EOF, not wait out the deadline.
        drop(command);
        let mut worker = Self::from_child(child, client, cancel)?;
        let (reply, body) = worker.request_until(prepare, &[], deadline)?;
        if reply != json!({"ok":true}) || !body.is_empty() {
            return Err(Error("native_prepare_reply_invalid"));
        }
        Ok(worker)
    }
    /// Serving-worker start: the operation's flag governs ONLY the prepare
    /// exchange (the worker keeps its own flag for later close-class commands),
    /// and a failed start kills and reaps the exact child before returning. A
    /// returned error therefore proves no child remains, unless the code is
    /// `native_cleanup_unproven` (kill/reap itself failed). The guard holds the
    /// worker to the prepare's `memory` and its staging folder to `disk`.
    pub(crate) fn start_scoped_until(
        config: &Config,
        prepare: &Value,
        cancel: Arc<AtomicBool>,
        source: &Path,
        deadline: Instant,
        disk: guard::Disk,
    ) -> std::result::Result<Self, StartError> {
        Self::start_scoped_probed_until(
            config,
            prepare,
            cancel,
            source,
            deadline,
            Some(disk),
            Box::new(guard::HostProbe),
        )
    }
    /// As `start_scoped_until`, reading the worker's memory through `probe`
    /// (the guard's constructor-only seam; production passes `HostProbe`).
    fn start_scoped_probed_until(
        config: &Config,
        prepare: &Value,
        cancel: Arc<AtomicBool>,
        source: &Path,
        deadline: Instant,
        disk: Option<guard::Disk>,
        probe: Box<dyn guard::MemoryProbe>,
    ) -> std::result::Result<Self, StartError> {
        config.verify()?;
        if Instant::now() >= deadline {
            return Err(Error("native_worker_deadline").into());
        }
        if cancel.load(Ordering::SeqCst) {
            return Err(Error("compiler_stopped").into());
        }
        // The guard holds the worker to the prepare's `memory`: the member's
        // consent cap or a tighter share (NativeShard::memory_limit).
        let memory = prepare["memory"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or(Error("worker_limits_invalid"))?;
        let (client, server) = UnixStream::pair().map_err(|_| Error("native_socket_failed"))?;
        let home = std::env::var_os("HOME").ok_or(Error("native_shard_path_invalid"))?;
        let profile = super::shard_sandbox::profile(
            Path::new(text(prepare, "checkpoint")?),
            source,
            &PathBuf::from(home).join(".cache/huggingface"),
        )
        .map_err(Error)?;
        let child = Command::new("/usr/bin/sandbox-exec")
            .args(["-p", &profile])
            .arg(&config.binary)
            .arg("native-worker")
            .stdin(Stdio::null())
            .stdout(Stdio::from(OwnedFd::from(server)))
            .spawn()
            .map_err(|_| Error("native_worker_start_failed"))?;
        let mut worker = Self::from_child_watched(
            child,
            client,
            Arc::new(AtomicBool::new(false)),
            memory,
            disk,
            probe,
        )
        .map_err(|e| match e.0 {
            "native_worker_kill_failed" | "native_worker_reap_failed" => {
                Error("native_cleanup_unproven")
            }
            _ => e,
        })?;
        let started = worker
            .request_controlled(prepare, &[], deadline, cancel)
            .and_then(|(reply, body)| {
                if reply != json!({"ok":true}) || !body.is_empty() {
                    Err(Error("native_prepare_reply_invalid"))
                } else {
                    Ok(())
                }
            });
        match started {
            Ok(()) => Ok(worker),
            Err(error) => {
                let guard = worker.guard_failure();
                match worker.kill_and_reap() {
                    Ok(_) => Err(StartError { error, guard }),
                    Err(_) => Err(StartError {
                        error: Error("native_cleanup_unproven"),
                        guard,
                    }),
                }
            }
        }
    }
    fn from_child(child: Child, client: UnixStream, cancel: Arc<AtomicBool>) -> Result<Self> {
        Self::from_child_within(child, client, cancel, duration(BudgetId::MemberChosenWall))
    }
    /// `wall` is the link's frame wait; it is a parameter so the failure branch
    /// for an unavailable budget is reachable from a test. Every failure kills
    /// and reaps the exact child before returning.
    fn from_child_within(
        child: Child,
        client: UnixStream,
        cancel: Arc<AtomicBool>,
        wall: Result<Duration>,
    ) -> Result<Self> {
        let linked = wall.and_then(|wall| {
            Link::new(client, wall).map_err(|error| {
                eprintln!("native_socket_failed: {error}");
                Error("native_socket_failed")
            })
        });
        let link = match linked {
            Ok(link) => link,
            Err(error) => {
                let mut child = child;
                child
                    .kill()
                    .map_err(|_| Error("native_worker_kill_failed"))?;
                child
                    .wait()
                    .map_err(|_| Error("native_worker_reap_failed"))?;
                return Err(error);
            }
        };
        Ok(Self {
            child: Arc::new(std::sync::Mutex::new(Some(child))),
            link,
            cancel,
            guard: None,
        })
    }
    pub(crate) fn pid(&self) -> Result<u32> {
        self.child
            .lock()
            .map_err(|_| Error("native_worker_closed"))?
            .as_ref()
            .map(Child::id)
            .ok_or(Error("native_worker_closed"))
    }
    pub(crate) fn request(&mut self, head: &Value, body: &[u8]) -> Result<(Value, Vec<u8>)> {
        self.request_until(
            head,
            body,
            Instant::now() + duration(BudgetId::MemberChosenWall)?,
        )
    }
    pub(crate) fn request_controlled(
        &mut self,
        head: &Value,
        body: &[u8],
        deadline: Instant,
        cancel: Arc<AtomicBool>,
    ) -> Result<(Value, Vec<u8>)> {
        // The caller's flag governs THIS exchange only; later commands (close)
        // keep the worker's own flag, so a route cancellation cannot cancel them.
        let own = std::mem::replace(&mut self.cancel, cancel);
        let result = self.request_until(head, body, deadline);
        self.cancel = own;
        result
    }
    pub(crate) fn request_until(
        &mut self,
        head: &Value,
        body: &[u8],
        deadline: Instant,
    ) -> Result<(Value, Vec<u8>)> {
        use crate::native_worker_link::LinkError;
        if let Some(failure) = self.guard_failure() {
            return Err(Error(failure.code));
        }
        let (reply, body) = self
            .link
            .request_until(
                head,
                body,
                deadline,
                &self.cancel,
                duration(BudgetId::RegistryPoll)?,
            )
            .map_err(|error| {
                eprintln!("native_worker_exchange_failed: {error}");
                Error(match error {
                    LinkError::Cancelled => "compiler_stopped",
                    LinkError::Deadline => "native_worker_deadline",
                    _ => "native_worker_exchange_failed",
                })
            })
            // An exchange broken by the guard's kill reports the guard's code.
            .map_err(|error| {
                self.guard_failure()
                    .map_or(error, |failure| Error(failure.code))
            })?;
        if let Some(error) = reply.get("error") {
            eprintln!("native_worker_refused: {error}");
            use stage_contract::native_shard as shard;
            let code = reply["code"].as_str();
            for known in [
                shard::HASH,
                shard::LENGTH,
                shard::RECIPE,
                shard::BINDING,
                shard::TENSORS,
                shard::PATH,
                shard::READ,
            ] {
                if code == Some(known) {
                    return Err(Error(known));
                }
            }
            return Err(Error("native_worker_refused"));
        }
        Ok((reply, body))
    }
    pub(crate) fn close(mut self) -> Result<Value> {
        self.close_in_place()
    }
    /// Reset the prepared worker's stage session between requests of one
    /// route epoch (private worker-link `session_reset_v1`, lane EP0). The
    /// worker's OWN cancel flag governs this exchange, so a route's request
    /// cancellation can never interrupt it. Returns the worker's session object
    /// only when its reply has exactly the contract's shape and reports no
    /// residue (active bytes back at or below the post-prepare baseline).
    pub(crate) fn reset_session(&mut self, deadline: Instant) -> Result<Value> {
        use stage_contract::native_session as contract;
        let (reply, body) =
            self.request_until(&json!({"command":contract::COMMAND}), &[], deadline)?;
        let exact = |v: &Value, fields: &[&str]| {
            v.as_object().is_some_and(|o| {
                o.len() == fields.len() && fields.iter().all(|k| o.contains_key(*k))
            })
        };
        let session = &reply["session"];
        let within = matches!(
            (session["active_bytes"].as_u64(), session["baseline_bytes"].as_u64()),
            (Some(active), Some(baseline)) if active <= baseline
        );
        if !body.is_empty()
            || !exact(&reply, &["ok", "session"])
            || reply["ok"] != true
            || !exact(session, contract::REPLY_FIELDS)
            || session["protocol"] != contract::REPLY_PROTOCOL
            || session["state_generation"].as_u64().is_none()
            || !within
        {
            return Err(Error("native_session_reset_invalid"));
        }
        Ok(session.clone())
    }
    /// Graceful close that leaves the worker owned on failure, so the caller can
    /// still kill and reap the exact child.
    pub(crate) fn close_in_place(&mut self) -> Result<Value> {
        let (reply, body) = self.request(&json!({"command":"close"}), &[])?;
        if !body.is_empty()
            || reply["ok"] != true
            || reply["stats"]["active_bytes"] != 0
            || reply["stats"]["reserved_memory_bytes"] != 0
            || reply["stats"]["context_closed"] != true
        {
            return Err(Error("native_cleanup_unproven"));
        }
        let deadline = Instant::now() + duration(BudgetId::RouterClose)?;
        loop {
            let mut child = self
                .child
                .lock()
                .map_err(|_| Error("native_worker_reap_failed"))?;
            if let Some(status) = child
                .as_mut()
                .ok_or(Error("native_worker_closed"))?
                .try_wait()
                .map_err(|_| Error("native_worker_reap_failed"))?
            {
                child.take();
                if !status.success() {
                    return Err(Error("native_worker_exit_failed"));
                }
                return Ok(reply["stats"].clone());
            }
            drop(child);
            if Instant::now() >= deadline {
                return Err(Error("native_cleanup_unproven"));
            }
            std::thread::yield_now();
        }
    }
}
impl Worker {
    /// Interrupted framing cannot carry a graceful close. Kill and wait for the
    /// exact owned child; return reap evidence only after the OS reported its exit.
    /// This is process-release evidence, NOT native-context-closed evidence.
    pub(crate) fn kill_and_reap(self) -> Result<Value> {
        let mut child = self
            .child
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
            .ok_or(Error("native_worker_closed"))?;
        let pid = child.id();
        let killed = match child
            .try_wait()
            .map_err(|_| Error("native_worker_reap_failed"))?
        {
            Some(_) => false,
            None => {
                child
                    .kill()
                    .map_err(|_| Error("native_worker_kill_failed"))?;
                true
            }
        };
        let status = child
            .wait()
            .map_err(|_| Error("native_worker_reap_failed"))?;
        use std::os::unix::process::ExitStatusExt;
        Ok(
            json!({"worker_pid":pid,"context_closed":false,"worker_reaped":true,"killed":killed,
            "exit_code":status.code(),"exit_signal":status.signal()}),
        )
    }
}
impl Worker {
    /// A worker whose memory is guarded from now on (lane MG): over
    /// `memory_limit` the guard kills and reaps the exact child; this and every
    /// later exchange then answers the guard's code.
    pub(crate) fn from_child_guarded(
        child: Child,
        client: UnixStream,
        cancel: Arc<AtomicBool>,
        memory_limit: u64,
        probe: Box<dyn guard::MemoryProbe>,
    ) -> Result<Self> {
        Self::from_child_watched(child, client, cancel, memory_limit, None, probe)
    }
    /// As `from_child_guarded`; with `disk`, the guard also walks the staging
    /// folder every sample (lane MG2, v0.1.4 worker.py:820-844).
    pub(crate) fn from_child_watched(
        child: Child,
        client: UnixStream,
        cancel: Arc<AtomicBool>,
        memory_limit: u64,
        disk: Option<guard::Disk>,
        probe: Box<dyn guard::MemoryProbe>,
    ) -> Result<Self> {
        let mut worker = Self::from_child(child, client, cancel)?;
        let started = worker.pid().and_then(|pid| {
            guard::Guard::start(worker.child.clone(), pid, memory_limit, disk, probe)
        });
        match started {
            Ok(guard) => {
                worker.guard = Some(guard);
                Ok(worker)
            }
            // An unguarded worker never runs: kill and reap the exact child first.
            Err(error) => match worker.kill_and_reap() {
                Ok(_) => Err(error),
                Err(e) => Err(Error(match e.0 {
                    "native_worker_reap_failed" => "native_worker_reap_failed",
                    _ => "native_worker_kill_failed",
                })),
            },
        }
    }
    /// The guard's verdict, once it stopped the worker.
    pub(crate) fn guard_failure(&self) -> Option<guard::Failure> {
        self.guard.as_ref().and_then(guard::Guard::failure)
    }
    /// Readings the guard has taken so far.
    #[cfg(test)]
    pub(crate) fn guard_samples(&self) -> u64 {
        self.guard.as_ref().map_or(0, guard::Guard::samples)
    }
    /// NOT BUILT (lane MG item 4, design in REPORT-laneMG.md): v0.1.4's disk
    /// guard (worker.py:820-844) has no v0.2 counterpart yet. Test-only stub so
    /// the ignored RED compiles; it watches nothing.
    #[cfg(test)]
    pub(crate) fn guard_disk(&mut self, _root: &Path, _max_disk_bytes: u64) -> Result<()> {
        Ok(())
    }
    /// Test-only: wait (bounded) for the guard's verdict.
    #[cfg(test)]
    pub(crate) fn guard_wait_until(&self, deadline: Instant) -> Option<guard::Failure> {
        self.guard.as_ref().and_then(|g| g.wait_until(deadline))
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        if let Some(guard) = self.guard.as_mut() {
            guard.stop();
        }
        let owned = self
            .child
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(mut child) = owned {
            match child.try_wait() {
                Ok(Some(_)) => (),
                Ok(None) => {
                    if let Err(e) = child.kill() {
                        eprintln!("native_worker_kill_failed: {e}");
                    }
                }
                Err(e) => {
                    eprintln!("native_worker_status_failed: {e}");
                    if let Err(e) = child.kill() {
                        eprintln!("native_worker_kill_failed: {e}");
                    }
                }
            }
            if let Err(e) = child.wait() {
                eprintln!("native_worker_reap_failed: {e}");
            }
        }
    }
}

pub(crate) fn greedy(raw: &[u8]) -> Result<u32> {
    if raw.get(..8) != Some(b"MYAC\x01\x03\x03\x01") || raw.len() < 28 {
        return Err(Error("native_logits_invalid"));
    }
    let dim = |i| u32::from_be_bytes(raw[i..i + 4].try_into().expect("fixed header"));
    let size = u64::from_be_bytes(raw[8..16].try_into().expect("fixed header"));
    if dim(16) != 1
        || dim(20) != 1
        || size != u64::from(dim(24)) * 4
        || size != (raw.len() - 28) as u64
    {
        return Err(Error("native_logits_invalid"));
    }
    let mut best = None;
    for (i, bytes) in raw[28..].chunks_exact(4).enumerate() {
        let value = f32::from_le_bytes(bytes.try_into().expect("fixed scalar"));
        if !value.is_finite() {
            return Err(Error("native_logits_nonfinite"));
        }
        if best.is_none_or(|(_, prior)| value > prior) {
            best = Some((i as u32, value));
        }
    }
    best.map(|(i, _)| i).ok_or(Error("native_logits_empty"))
}
