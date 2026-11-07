// SPDX-License-Identifier: AGPL-3.0-only
use backend_mlx::process::OwnedWorker;
use node::{Residue, WorkerAdapter, WorkerError};
pub struct LeafAdapter {
    worker: OwnedWorker,
    stopped: bool,
}
impl LeafAdapter {
    pub fn new(worker: OwnedWorker) -> Self {
        Self {
            worker,
            stopped: false,
        }
    }
    pub fn pid(&self) -> u32 {
        self.worker.pid()
    }
}
impl WorkerAdapter for LeafAdapter {
    fn request_stop(&mut self) -> Result<(), WorkerError> {
        self.worker
            .cancel()
            .map_err(|_| WorkerError::Refused("leaf_cancel_failed"))?;
        self.stopped = true;
        Ok(())
    }
    fn observe(&mut self) -> Result<Residue, WorkerError> {
        if !self.stopped {
            return Ok(Residue {
                process_gone: false,
                resources_gone: false,
            });
        }
        let report = self
            .worker
            .wait()
            .map_err(|_| WorkerError::Refused("leaf_reap_failed"))?;
        // This adapter is ONLY for a trusted nonforking, no-staging leaf. An OS
        // reap releases process-local GPU/RAM, not arbitrary files/descendants.
        let proven = report.cleanup_proven && report.status.is_some() && report.errors.is_empty();
        Ok(Residue {
            process_gone: proven,
            resources_gone: proven,
        })
    }
}
