// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded full-context replay coordinator, driven by completed host effects.
//! Source: router_port.py:1651-2035 @ f4e366e9. This is not a scheduler or
//! checkpoint store. The host serializes cancellation/currentness with cutover;
//! qualification/fence/cleanup calls are trusted adapter results, not wire claims.
use crate::cleanup::CleanupProof;
use budgets::{BudgetId, Entries, MonoInstant, Span};
use protocol::ContractError;
use serde_json::{Value, json};
use std::collections::BTreeSet;
type Result<T> = std::result::Result<T, ContractError>;
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub request: String,
    pub deployment: String,
    pub epoch: u64,
    pub topology: u64,
    pub request_attempt: u64,
    pub path: String,
    pub path_attempt: u64,
    pub publisher_generation: u64,
    pub cancellation_generation: u64,
    pub command: String,
    pub qualification_digest: String,
    pub path_digest: String,
    pub absolute_deadline_ms: u64,
}
impl Identity {
    /// Detached full cleanup subject, not an authentication operation.
    pub fn from_subject(v: &Value) -> Result<Self> {
        let text = |k: &str| {
            v[k].as_str()
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .ok_or(ContractError("recovery_identity_invalid"))
        };
        let number = |k: &str| {
            v[k].as_u64()
                .ok_or(ContractError("recovery_identity_invalid"))
        };
        let id = Self {
            request: text("request_id")?,
            deployment: text("deployment_id")?,
            epoch: number("deployment_epoch")?,
            topology: number("topology_generation")?,
            request_attempt: number("request_attempt")?,
            path: text("path_id")?,
            path_attempt: number("path_attempt")?,
            publisher_generation: number("publisher_generation")?,
            cancellation_generation: number("cancellation_generation")?,
            command: text("command_id")?,
            qualification_digest: text("qualification_digest")?,
            path_digest: text("path_digest")?,
            absolute_deadline_ms: number("absolute_deadline_ms")?,
        };
        if id.subject() != *v {
            return Err(ContractError("recovery_identity_invalid"));
        }
        Ok(id)
    }
    pub fn subject(&self) -> Value {
        json!({
            "request_id":self.request,"deployment_id":self.deployment,"deployment_epoch":self.epoch,
            "topology_generation":self.topology,"request_attempt":self.request_attempt,
            "path_id":self.path,"path_attempt":self.path_attempt,"publisher_generation":self.publisher_generation,
            "cancellation_generation":self.cancellation_generation,"command_id":self.command,
            "qualification_digest":self.qualification_digest,"path_digest":self.path_digest,
            "absolute_deadline_ms":self.absolute_deadline_ms
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Pending,
    Qualification,
    Qualified,
    Prefill,
    Progress,
    Cutover,
    Cleanup,
    Completed,
    Failed,
    Cancelled,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    Execution,
    Authority,
    Timeout,
    Cancelled,
    Output,
    Cleanup,
}
#[derive(Clone, Copy)]
pub struct Policy {
    qualification: Span,
    prefill: Span,
    progress: Span,
    cutover: Span,
    total: Span,
    attempts: u64,
}
impl Policy {
    pub fn registry() -> std::result::Result<Self, budgets::Refusal> {
        Ok(Self {
            qualification:
                BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesQualificationMsL259
                    .span(Entries::ZERO)?,
            prefill: BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesPrefillMsL260
                .span(Entries::ZERO)?,
            progress: BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesProgressMsL261
                .span(Entries::ZERO)?,
            cutover: BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesCutoverMsL262
                .span(Entries::ZERO)?,
            total: BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesTotalMsL263
                .span(Entries::ZERO)?,
            // Shared full-context recovery cap from mycelium_a6_recovery.py:80.
            attempts: BudgetId::PyMyceliumA6RecoveryMaxRecoveryAttemptsL80
                .count()?
                .get(),
        })
    }
}
/// Private replay material held by the caller; intentionally not Debug/Serialize.
/// Checkpoint authenticity/storage bounds belong to its admission owner.
pub struct Checkpoint {
    pub source: Identity,
    pub prompt: Vec<u32>,
    pub committed: Vec<u32>,
    pub output_limit: usize,
    pub source_placements: Vec<String>,
    pub expires: MonoInstant,
}
pub struct Recovery {
    source: Identity,
    successor: Option<Identity>,
    context: Vec<u32>,
    committed: usize,
    limit: usize,
    excluded: BTreeSet<String>,
    placements: Vec<String>,
    policy: Policy,
    total: MonoInstant,
    phase_deadline: MonoInstant,
    last: MonoInstant,
    attempts: u64,
    phase: Phase,
    fenced: bool,
    cleaned: Option<CleanupProof>,
    failure: Option<Failure>,
    buffer: Vec<u32>,
    published: Vec<(usize, u32)>,
}
fn budget<T>(r: std::result::Result<T, budgets::Refusal>) -> Result<T> {
    r.map_err(|_| ContractError("recovery_clock_or_budget"))
}
impl Recovery {
    pub fn new(checkpoint: Checkpoint, now: MonoInstant, policy: Policy) -> Result<Self> {
        let Checkpoint {
            source,
            mut prompt,
            committed,
            output_limit: limit,
            source_placements: placements,
            expires: request_deadline,
        } = checkpoint;
        budget(request_deadline.remaining(now))?;
        if prompt.is_empty() || placements.is_empty() || limit == 0 {
            return Err(ContractError("recovery_arguments_invalid"));
        }
        let count = committed.len();
        prompt.extend(committed);
        let total = budget(budget(now.checked_add(policy.total))?.earliest(request_deadline))?;
        Ok(Self {
            source,
            successor: None,
            context: prompt,
            committed: count,
            limit,
            excluded: placements.into_iter().collect(),
            placements: Vec::new(),
            policy,
            total,
            phase_deadline: total,
            last: now,
            attempts: 0,
            phase: Phase::Pending,
            fenced: false,
            cleaned: None,
            failure: None,
            buffer: Vec::new(),
            published: Vec::new(),
        })
    }
    pub fn phase(&self) -> Phase {
        self.phase
    }
    pub fn attempts(&self) -> u64 {
        self.attempts
    }
    pub fn total_deadline(&self) -> MonoInstant {
        self.total
    }
    pub fn deadline(&self) -> MonoInstant {
        self.phase_deadline
    }
    pub fn source(&self) -> &Identity {
        &self.source
    }
    pub fn successor(&self) -> Option<&Identity> {
        self.successor.as_ref()
    }
    pub fn replay_context(&self) -> Vec<u32> {
        self.context.clone()
    }
    pub fn published(&self) -> &[(usize, u32)] {
        &self.published
    }
    pub fn excluded(&self) -> &BTreeSet<String> {
        &self.excluded
    }
    fn arm(&mut self, now: MonoInstant, span: Span) -> Result<()> {
        self.phase_deadline = budget(budget(now.checked_add(span))?.earliest(self.total))?;
        Ok(())
    }
    pub fn check(&mut self, now: MonoInstant) -> Result<()> {
        let valid = now
            .elapsed_since(self.last)
            .and_then(|_| self.phase_deadline.remaining(now));
        if valid.is_err() {
            self.fail(Failure::Timeout);
            return Err(ContractError("recovery_deadline_or_clock"));
        }
        self.last = now;
        Ok(())
    }
    fn current(&self, current: &Identity) -> Result<()> {
        if current != &self.source {
            Err(ContractError("recovery_source_stale"))
        } else {
            Ok(())
        }
    }
    fn successor_matches(&self, identity: &Identity) -> Result<()> {
        if self.successor.as_ref() != Some(identity) {
            Err(ContractError("recovery_successor_stale"))
        } else {
            Ok(())
        }
    }
    pub fn reserve(
        &mut self,
        next: Identity,
        placements: Vec<String>,
        now: MonoInstant,
    ) -> Result<()> {
        if self.phase != Phase::Pending || self.attempts >= self.policy.attempts {
            return Err(ContractError("recovery_not_pending"));
        }
        self.check(now)?;
        if self.successor.as_ref().is_some_and(|previous| {
            next.request_attempt <= previous.request_attempt
                || next.path == previous.path
                || next.command == previous.command
                || next.path_digest == previous.path_digest
        }) {
            return Err(ContractError("recovery_successor_generation_reused"));
        }
        if next.request != self.source.request
            || next.deployment != self.source.deployment
            || next.epoch != self.source.epoch
            || next.topology != self.source.topology
            || next.request_attempt <= self.source.request_attempt
            || next.path == self.source.path
            || next.publisher_generation != self.source.publisher_generation
            || next.cancellation_generation != self.source.cancellation_generation
            || next.qualification_digest != self.source.qualification_digest
            || next.absolute_deadline_ms != self.source.absolute_deadline_ms
            || next.command == self.source.command
            || next.path_digest == self.source.path_digest
            || placements.is_empty()
            || placements.iter().any(|p| self.excluded.contains(p))
        {
            return Err(ContractError("recovery_candidate_binding"));
        }
        self.arm(now, self.policy.qualification)?;
        self.attempts += 1;
        self.successor = Some(next);
        self.placements = placements;
        self.phase = Phase::Qualification;
        self.cleaned = None;
        self.failure = None;
        self.fenced = false;
        self.buffer.clear();
        Ok(())
    }
    pub fn qualified(&mut self, now: MonoInstant, current: &Identity) -> Result<()> {
        if self.phase != Phase::Qualification {
            return Err(ContractError("recovery_phase_invalid"));
        }
        self.check(now)?;
        self.current(current)?;
        self.phase = Phase::Qualified;
        Ok(())
    }
    pub fn fenced(&mut self, now: MonoInstant, current: &Identity) -> Result<()> {
        if self.phase != Phase::Qualified {
            return Err(ContractError("recovery_phase_invalid"));
        }
        self.check(now)?;
        self.current(current)?;
        self.arm(now, self.policy.prefill)?;
        self.fenced = true;
        self.phase = Phase::Prefill;
        Ok(())
    }
    pub fn token(
        &mut self,
        identity: &Identity,
        index: usize,
        token: u32,
        now: MonoInstant,
    ) -> Result<()> {
        self.successor_matches(identity)?;
        if !matches!(self.phase, Phase::Prefill | Phase::Progress) {
            return Err(ContractError("recovery_phase_invalid"));
        }
        self.check(now)?;
        if index != self.buffer.len() || index >= self.limit {
            self.fail(Failure::Output);
            return Err(ContractError("recovery_output_noncontiguous_or_overflow"));
        }
        self.arm(now, self.policy.progress)?;
        self.buffer.push(token);
        self.phase = Phase::Progress;
        Ok(())
    }
    pub fn executed(&mut self, identity: &Identity, now: MonoInstant) -> Result<()> {
        self.successor_matches(identity)?;
        if !matches!(self.phase, Phase::Prefill | Phase::Progress) {
            return Err(ContractError("recovery_phase_invalid"));
        }
        self.check(now)?;
        self.arm(now, self.policy.cutover)?;
        self.phase = Phase::Cutover;
        Ok(())
    }
    /// Call only after exact signed quorum AND environmental cleanup complete.
    /// May run after expiry: expiration revokes execution, never cleanup ownership.
    pub fn cleanup(
        &mut self,
        identity: &Identity,
        proof: CleanupProof,
        now: MonoInstant,
    ) -> Result<()> {
        self.successor_matches(identity)?;
        if !matches!(self.phase, Phase::Cleanup | Phase::Cutover) {
            return Err(ContractError("recovery_phase_invalid"));
        }
        proof.verify(&identity.subject(), now, true)?;
        if self.cleaned.is_none() {
            self.cleaned = Some(proof);
        }
        Ok(())
    }
    pub fn fail(&mut self, failure: Failure) {
        if matches!(
            self.phase,
            Phase::Completed | Phase::Failed | Phase::Cancelled
        ) {
            return;
        }
        // Cancellation dominates; nonretryable failures cannot later become an
        // execution retry when a delayed native result arrives.
        if self.failure.is_none()
            || self.failure == Some(Failure::Execution)
            || failure == Failure::Cancelled
        {
            self.failure = Some(failure);
        }
        self.phase = Phase::Cleanup;
        self.buffer.clear();
    }
    pub fn cancel(&mut self) {
        self.fail(Failure::Cancelled);
    }
    pub fn retry(&mut self, current: &Identity, now: MonoInstant) -> Result<()> {
        if self.phase != Phase::Cleanup
            || self.cleaned.is_none()
            || !self.fenced
            || self.failure != Some(Failure::Execution)
            || self.attempts >= self.policy.attempts
        {
            return Err(ContractError("recovery_retry_refused"));
        }
        // Failed execution's idle deadline is no longer active, but the total
        // checkpoint/request authority is immutable across attempts.
        budget(now.elapsed_since(self.last))?;
        budget(self.total.remaining(now))?;
        self.current(current)?;
        let identity = self
            .successor
            .as_ref()
            .ok_or(ContractError("recovery_successor_missing"))?;
        self.cleaned
            .as_ref()
            .ok_or(ContractError("recovery_cleanup_unproven"))?
            .verify(&identity.subject(), now, true)?;
        self.excluded.extend(self.placements.iter().cloned());
        self.phase = Phase::Pending;
        self.phase_deadline = self.total;
        self.last = now;
        Ok(())
    }
    /// `qualified_now` must be a freshly reverified owner qualification; caller
    /// holds its currentness/CAS lock across this call and the publication handoff.
    pub fn commit(
        &mut self,
        current: &Identity,
        qualified_now: bool,
        now: MonoInstant,
    ) -> Result<()> {
        if self.phase != Phase::Cutover || self.cleaned.is_none() || !qualified_now {
            return Err(ContractError("recovery_cutover_unproven"));
        }
        self.check(now)?;
        self.current(current)?;
        let identity = self
            .successor
            .as_ref()
            .ok_or(ContractError("recovery_successor_missing"))?;
        self.cleaned
            .as_ref()
            .ok_or(ContractError("recovery_cleanup_unproven"))?
            .verify(&identity.subject(), now, true)?;
        let end = self
            .committed
            .checked_add(self.buffer.len())
            .ok_or(ContractError("recovery_output_overflow"))?;
        self.published = (self.committed..end).zip(self.buffer.drain(..)).collect();
        self.phase = Phase::Completed;
        Ok(())
    }
    pub fn settle_failure(&mut self, current: &Identity, now: MonoInstant) -> Result<Phase> {
        if self.phase != Phase::Cleanup || self.cleaned.is_none() {
            return Err(ContractError("recovery_cleanup_unproven"));
        }
        self.current(current)?;
        let identity = self
            .successor
            .as_ref()
            .ok_or(ContractError("recovery_successor_missing"))?;
        self.cleaned
            .as_ref()
            .ok_or(ContractError("recovery_cleanup_unproven"))?
            .verify(&identity.subject(), now, true)?;
        self.phase = if self.failure == Some(Failure::Cancelled) {
            Phase::Cancelled
        } else {
            Phase::Failed
        };
        Ok(self.phase)
    }
}
