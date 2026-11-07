// SPDX-License-Identifier: AGPL-3.0-only
//! Environmental worker seam. A signed remote assertion is NOT this observation.
use crate::Ticket;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Residue {
    pub process_gone: bool,
    pub resources_gone: bool,
}
#[derive(Debug)]
pub enum WorkerError {
    Domains(Vec<crate::domains::DomainFailure>),
    Storage(control_runtime::Error),
    Io(std::io::Error),
    Refused(&'static str),
    SpawnCleanup {
        spawn: std::io::Error,
        cleanup: std::io::Error,
    },
}
impl From<std::io::Error> for WorkerError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}
pub trait WorkerAdapter {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError>;
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError>;
}
/// Only an environmental supervisor can produce this. Callers cannot turn a
/// peer-signed receipt into local process/resource evidence by setting booleans.
#[derive(Debug)]
pub struct CleanupObservation {
    ticket: Ticket,
    residue: Residue,
}
impl CleanupObservation {
    pub fn ticket(&self) -> &Ticket {
        &self.ticket
    }
    pub fn proven(&self) -> bool {
        self.residue.process_gone && self.residue.resources_gone
    }
}
pub struct Supervisor<W: WorkerAdapter> {
    ticket: Ticket,
    worker: W,
}
impl<W: WorkerAdapter> Supervisor<W> {
    pub fn new(ticket: Ticket, worker: W) -> Self {
        Self { ticket, worker }
    }
    pub fn worker_mut(&mut self) -> &mut W {
        &mut self.worker
    }
    pub fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        self.worker.request_stop()
    }
    pub fn observe_cleanup(&mut self) -> std::result::Result<CleanupObservation, WorkerError> {
        Ok(CleanupObservation {
            ticket: self.ticket.clone(),
            residue: self.worker.observe()?,
        })
    }
}
