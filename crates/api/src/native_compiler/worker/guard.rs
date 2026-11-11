// SPDX-License-Identifier: AGPL-3.0-only
//! Member-side resource guard for the owned native worker: the port of v0.1.4's
//! `ManagedWorker._watch` / `_check` memory guard (mycelium_a13_macos/worker.py:786-862,
//! 864-929, 1459-1486 @ f4e366e9). The MLX memory limit is advisory; this guard measures
//! the worker's real memory and stops it on a breach of the member's consented cap.
use serde_json::{Value, json};

#[cfg(test)]
#[path = "guard_tests.rs"]
mod tests;

/// v0.1.4 `WorkerError("worker_memory_limit_exceeded")` (worker.py:848).
pub(crate) const MEMORY_LIMIT_EXCEEDED: &str = "worker_memory_limit_exceeded";
/// v0.1.4 `WorkerError("worker_memory_observation_failed")` (worker.py:907, 921, 923).
pub(crate) const MEMORY_OBSERVATION_FAILED: &str = "worker_memory_observation_failed";

/// One reading of the worker's resident memory. Production reads the host;
/// tests inject readings through `Worker::from_child_guarded` only.
pub(crate) trait MemoryProbe: Send {
    fn resident_bytes(&mut self, pid: u32) -> std::result::Result<u64, &'static str>;
}

/// The host's own accounting of the exact worker pid.
pub(crate) struct HostProbe;
impl MemoryProbe for HostProbe {
    fn resident_bytes(&mut self, _pid: u32) -> std::result::Result<u64, &'static str> {
        Err(MEMORY_OBSERVATION_FAILED)
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
