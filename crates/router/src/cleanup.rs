// SPDX-License-Identifier: AGPL-3.0-only
//! Exact cleanup quorum and terminal ownership. Host-supplied pins are trusted
//! admission inputs, never copied from arriving observations. Local residue proof
//! is an injected fallible effect; this crate does not observe child processes.
use budgets::MonoInstant;
use protocol::{
    ContractError,
    observation::{self, ExpectedObservation, VerifiedCancelReceipt},
};
use serde_json::Value;
use std::collections::BTreeMap;
const SUBJECT_FIELDS: &[&str] = &[
    "request_id",
    "deployment_id",
    "deployment_epoch",
    "qualification_digest",
    "command_id",
    "publisher_generation",
    "absolute_deadline_ms",
    "request_attempt",
    "path_id",
    "path_attempt",
    "path_digest",
    "topology_generation",
    "cancellation_generation",
];
#[derive(Clone)]
pub struct CleanupPeer {
    pub node: String,
    pub host: String,
    pub process: Value,
    pub run: String,
    pub deployment: String,
    pub endpoint: String,
    pub key: Value,
    pub generation: Value,
}
pub struct CleanupRound {
    subject: Value,
    peers: BTreeMap<String, CleanupPeer>,
    receipts: BTreeMap<String, VerifiedCancelReceipt>,
    deadline: MonoInstant,
}
pub struct CleanupProof {
    subject: Value,
    deadline: MonoInstant,
}
impl CleanupProof {
    /// Recheck the complete subject and authority at the consuming settlement.
    pub fn verify(
        &self,
        subject: &Value,
        now: MonoInstant,
        authorized: bool,
    ) -> Result<(), ContractError> {
        if &self.subject != subject {
            return Err(ContractError("cleanup_subject_stale"));
        }
        current(self.deadline, now, authorized)
    }
}
fn subject_valid(subject: &Value) -> bool {
    subject.as_object().is_some_and(|o| {
        o.len() == SUBJECT_FIELDS.len() && SUBJECT_FIELDS.iter().all(|f| o.contains_key(*f))
    })
}
fn current(deadline: MonoInstant, now: MonoInstant, authorized: bool) -> Result<(), ContractError> {
    if !authorized {
        return Err(ContractError("cleanup_authority_stale"));
    }
    now.same_domain(deadline)
        .map_err(|_| ContractError("cleanup_clock_domain"))?;
    if now.ticks() >= deadline.ticks() {
        return Err(ContractError("cleanup_deadline_expired"));
    }
    Ok(())
}
impl CleanupRound {
    pub fn new(
        subject: Value,
        peers: Vec<CleanupPeer>,
        deadline: MonoInstant,
    ) -> Result<Self, ContractError> {
        if !subject_valid(&subject) || peers.is_empty() {
            return Err(ContractError("cleanup_arguments_invalid"));
        }
        let count = peers.len();
        let peers: BTreeMap<_, _> = peers.into_iter().map(|p| (p.node.clone(), p)).collect();
        if peers.len() != count {
            return Err(ContractError("cleanup_arguments_invalid"));
        }
        Ok(Self {
            subject,
            peers,
            receipts: BTreeMap::new(),
            deadline,
        })
    }
    pub fn accept(
        &mut self,
        node: &str,
        response: &Value,
        observe: impl FnOnce() -> (MonoInstant, bool),
    ) -> Result<(), ContractError> {
        let p = self
            .peers
            .get(node)
            .ok_or(ContractError("cleanup_participant_unknown"))?;
        let o = observation::verify(
            response,
            &ExpectedObservation {
                event: "inference_cancelled",
                node_id: &p.node,
                host_id: &p.host,
                process_id: &p.process,
                run_id: &p.run,
                deployment_id: &p.deployment,
                endpoint_id: &p.endpoint,
                key: &p.key,
            },
        )?;
        if response["result"]["observation"]["peer_generation"] != p.generation {
            return Err(ContractError("cleanup_peer_generation_stale"));
        }
        let proof = o.cancel_receipt(&self.subject)?;
        let (now, authorized) = observe();
        current(self.deadline, now, authorized)?;
        if let Some(old) = self.receipts.get(node) {
            if old.observation_digest() != proof.observation_digest() {
                return Err(ContractError("cleanup_receipt_conflict"));
            }
            return Ok(());
        }
        self.receipts.insert(node.to_owned(), proof);
        Ok(())
    }
    pub fn finish(
        self,
        local_residue: impl FnOnce() -> Result<(), ContractError>,
        mut observe: impl FnMut() -> (MonoInstant, bool),
    ) -> Result<CleanupProof, ContractError> {
        let (now, authorized) = observe();
        current(self.deadline, now, authorized)?;
        if self.receipts.len() != self.peers.len() {
            return Err(ContractError("cleanup_quorum_missing"));
        }
        local_residue()?;
        let (now, authorized) = observe();
        current(self.deadline, now, authorized)?;
        Ok(CleanupProof {
            subject: self.subject,
            deadline: self.deadline,
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Terminal {
    Completed,
    Cancelled,
    Failed,
}
/// Exactly one owner publishes terminal state after cleanup. Native and progress
/// completion are independent; a native reply must never become another token source.
pub struct RequestOwner {
    subject: Value,
    terminal: Option<Terminal>,
    cancelled: bool,
    progress_completed: bool,
    native_completed: bool,
}
impl RequestOwner {
    pub fn new(subject: Value) -> Result<Self, ContractError> {
        if !subject_valid(&subject) {
            return Err(ContractError("cleanup_arguments_invalid"));
        }
        Ok(Self {
            subject,
            terminal: None,
            cancelled: false,
            progress_completed: false,
            native_completed: false,
        })
    }
    pub fn cancel(&mut self) -> bool {
        if self.terminal.is_some() {
            false
        } else {
            self.cancelled = true;
            true
        }
    }
    pub fn progress_completed(&mut self) {
        self.progress_completed = true;
    }
    pub fn native_completed(&mut self) {
        self.native_completed = true;
    }
    pub fn terminal(&self) -> Option<Terminal> {
        self.terminal
    }
    pub fn settle(
        &mut self,
        proof: CleanupProof,
        outcome: Terminal,
        observe: impl FnOnce() -> (MonoInstant, bool),
    ) -> Result<(), ContractError> {
        if proof.subject != self.subject {
            return Err(ContractError("cleanup_subject_stale"));
        }
        if self.terminal.is_some() {
            return Err(ContractError("request_already_terminal"));
        }
        if outcome == Terminal::Completed
            && (!self.progress_completed || !self.native_completed || self.cancelled)
        {
            return Err(ContractError("request_completion_unproven"));
        }
        if outcome == Terminal::Cancelled && !self.cancelled {
            return Err(ContractError("request_cancellation_unproven"));
        }
        let (now, authorized) = observe();
        current(proof.deadline, now, authorized)?;
        self.terminal = Some(outcome);
        Ok(())
    }
}
