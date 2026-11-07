// SPDX-License-Identifier: AGPL-3.0-only
//! Endpoint and independently observed native cleanup. Transport close alone
//! never authorizes reservation release. NativeChild only proves a nonforking
//! leaf and its owned directory; this is not MLX/device/descendant cleanup.
use crate::iroh_host::{LoopbackHost, allowance};
use budgets::{BudgetId, Entries, MonoInstant};
use node::{CleanupObservation, Supervisor, WorkerAdapter, WorkerError};
use transport::{Clock, Error};
#[derive(Debug)]
pub struct HostCloseReport {
    pub endpoint_error: Option<Error>,
    pub worker_error: Option<WorkerError>,
    pub observation_error: Option<WorkerError>,
    pub deadline_error: Option<Error>,
    observation: Option<CleanupObservation>,
}
impl HostCloseReport {
    pub fn proven(&self) -> bool {
        self.endpoint_error.is_none()
            && self.worker_error.is_none()
            && self.observation_error.is_none()
            && self.deadline_error.is_none()
            && self
                .observation
                .as_ref()
                .is_some_and(CleanupObservation::proven)
    }
    pub fn into_native_proof(mut self) -> Result<CleanupObservation, Box<Self>> {
        if self.proven()
            && let Some(proof) = self.observation.take()
        {
            return Ok(proof);
        }
        Err(Box::new(self))
    }
}
pub fn close_endpoint_and_native<W: WorkerAdapter>(
    host: &mut LoopbackHost<'_>,
    worker: &mut Supervisor<W>,
    clock: &impl Clock,
    deadline: MonoInstant,
) -> HostCloseReport {
    let mut report = HostCloseReport {
        endpoint_error: None,
        worker_error: None,
        observation_error: None,
        deadline_error: None,
        observation: None,
    };
    if let Err(error) = host.close(clock, deadline) {
        report.endpoint_error = Some(error);
    }
    // Independent cleanup still runs if endpoint close failed or expired.
    if let Err(error) = worker.request_stop() {
        report.worker_error = Some(error);
    }
    loop {
        match worker.observe_cleanup() {
            Ok(observation) => {
                report.observation = Some(observation);
            }
            Err(error) => {
                report.observation_error = Some(error);
                break;
            }
        }
        let remaining = match allowance(clock, deadline) {
            Ok(remaining) => remaining,
            Err(error) => {
                report.deadline_error = Some(error);
                break;
            }
        };
        if report.worker_error.is_some()
            || report
                .observation
                .as_ref()
                .is_some_and(CleanupObservation::proven)
        {
            break;
        }
        let poll =
            match BudgetId::PyMyceliumDeadlinesCleanupReceiptPollSecondsL217.span(Entries::ZERO) {
                Ok(poll) => std::time::Duration::from_millis(poll.millis()),
                Err(_) => {
                    report.worker_error = Some(WorkerError::Refused("cleanup_poll_budget"));
                    break;
                }
            };
        std::thread::sleep(poll.min(remaining));
    }
    if let Err(error) = allowance(clock, deadline) {
        report.deadline_error = Some(error);
    }
    report
}
