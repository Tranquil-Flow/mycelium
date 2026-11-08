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

#[cfg(test)]
mod tests;

fn duration(id: BudgetId) -> Result<Duration> {
    Ok(Duration::from_millis(
        id.span(Entries::ZERO)
            .map_err(|_| Error("budget_unavailable"))?
            .millis(),
    ))
}
pub(crate) struct Worker {
    child: Option<Child>,
    link: Link,
    cancel: Arc<AtomicBool>,
}
impl Worker {
    pub(crate) fn start(
        config: &Config,
        prepare: &Value,
        cancel: Arc<AtomicBool>,
        source: &Path,
    ) -> Result<Self> {
        config.verify()?;
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
        let mut worker = Self::from_child(child, client, cancel)?;
        let (reply, body) = worker.request(prepare, &[])?;
        if reply != json!({"ok":true}) || !body.is_empty() {
            return Err(Error("native_prepare_reply_invalid"));
        }
        Ok(worker)
    }
    fn from_child(child: Child, client: UnixStream, cancel: Arc<AtomicBool>) -> Result<Self> {
        let link = match Link::new(client, duration(BudgetId::MemberChosenWall)?) {
            Ok(link) => link,
            Err(error) => {
                let mut child = child;
                child
                    .kill()
                    .map_err(|_| Error("native_worker_kill_failed"))?;
                child
                    .wait()
                    .map_err(|_| Error("native_worker_reap_failed"))?;
                eprintln!("native_socket_failed: {error}");
                return Err(Error("native_socket_failed"));
            }
        };
        Ok(Self {
            child: Some(child),
            link,
            cancel,
        })
    }
    pub(crate) fn pid(&self) -> Result<u32> {
        self.child
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
    fn request_until(
        &mut self,
        head: &Value,
        body: &[u8],
        deadline: Instant,
    ) -> Result<(Value, Vec<u8>)> {
        use crate::native_worker_link::LinkError;
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
            if let Some(status) = self
                .child
                .as_mut()
                .ok_or(Error("native_worker_closed"))?
                .try_wait()
                .map_err(|_| Error("native_worker_reap_failed"))?
            {
                self.child.take();
                if !status.success() {
                    return Err(Error("native_worker_exit_failed"));
                }
                return Ok(reply["stats"].clone());
            }
            if Instant::now() >= deadline {
                return Err(Error("native_cleanup_unproven"));
            }
            std::thread::yield_now();
        }
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
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
