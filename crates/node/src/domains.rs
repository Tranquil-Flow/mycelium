// SPDX-License-Identifier: AGPL-3.0-only
//! Cleanup composition for explicitly inventoried process, staging and device
//! owners. This is not process discovery, a sandbox, or a device driver.
use crate::{Residue, Ticket, WorkerAdapter, WorkerError};
use budgets::Entries;
use std::collections::BTreeMap;
#[derive(Debug)]
pub struct DomainFailure {
    pub domain: String,
    pub phase: &'static str,
    pub error: WorkerError,
}
struct Domain {
    owner: Option<Box<dyn WorkerAdapter>>,
    complete: bool,
}
pub struct CleanupDomains {
    ticket: Ticket,
    domains: BTreeMap<String, Domain>,
}
impl CleanupDomains {
    pub fn new(ticket: Ticket, required: &[&str], capacity: Entries) -> Result<Self, WorkerError> {
        if required.is_empty() || required.len() as u64 > capacity.get() {
            return Err(WorkerError::Refused("cleanup_inventory_invalid"));
        }
        let mut domains = BTreeMap::new();
        for id in required {
            if id.is_empty()
                || domains
                    .insert(
                        (*id).to_owned(),
                        Domain {
                            owner: None,
                            complete: false,
                        },
                    )
                    .is_some()
            {
                return Err(WorkerError::Refused("cleanup_inventory_invalid"));
            }
        }
        Ok(Self { ticket, domains })
    }
    /// An environmental capability from the exact generation, never a saved PID
    /// or a peer's cleanup boolean. Missing domains remain fail-closed.
    pub fn bind(
        &mut self,
        ticket: &Ticket,
        id: &str,
        owner: Box<dyn WorkerAdapter>,
    ) -> Result<(), WorkerError> {
        if ticket != &self.ticket {
            return Err(WorkerError::Refused("cleanup_generation_stale"));
        }
        let d = self
            .domains
            .get_mut(id)
            .ok_or(WorkerError::Refused("cleanup_domain_unknown"))?;
        if d.owner.is_some() || d.complete {
            return Err(WorkerError::Refused("cleanup_domain_already_bound"));
        }
        d.owner = Some(owner);
        Ok(())
    }
}
impl WorkerAdapter for CleanupDomains {
    fn request_stop(&mut self) -> Result<(), WorkerError> {
        let mut failures = Vec::new();
        for (id, d) in &mut self.domains {
            if !d.complete {
                let result = match d.owner.as_mut() {
                    Some(owner) => owner.request_stop(),
                    None => Err(WorkerError::Refused("cleanup_owner_missing")),
                };
                if let Err(error) = result {
                    failures.push(DomainFailure {
                        domain: id.clone(),
                        phase: "stop",
                        error,
                    });
                }
            }
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(WorkerError::Domains(failures))
        }
    }
    fn observe(&mut self) -> Result<Residue, WorkerError> {
        let mut result = Residue {
            process_gone: true,
            resources_gone: true,
        };
        let mut failures = Vec::new();
        for (id, d) in &mut self.domains {
            if !d.complete {
                let observation = match d.owner.as_mut() {
                    Some(owner) => owner.observe(),
                    None => Err(WorkerError::Refused("cleanup_owner_missing")),
                };
                match observation {
                    Ok(r) => {
                        d.complete = r.process_gone && r.resources_gone;
                        result.process_gone &= r.process_gone;
                        result.resources_gone &= r.resources_gone;
                    }
                    Err(error) => failures.push(DomainFailure {
                        domain: id.clone(),
                        phase: "observe",
                        error,
                    }),
                }
            }
        }
        if failures.is_empty() {
            Ok(result)
        } else {
            Err(WorkerError::Domains(failures))
        }
    }
}
