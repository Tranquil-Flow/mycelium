// SPDX-License-Identifier: AGPL-3.0-only
//! L6/L8 seam composition. Controlled implementations of the real traits are
//! useful tests, not evidence that a model subprocess or device was exercised.
use budgets::MonoInstant;
use node::{Supervisor, Ticket, WorkerAdapter};
use protocol::ContractError;
use router::cleanup::{CleanupProof, CleanupRound};
use router::recovery::{Identity, Phase, Recovery};
use stage_contract::{Binding, Error, ErrorCode, Limits, Output, StageBackend, Step};
/// A single admitted L8 session; failed cleanup permanently fences its reuse.
pub struct StageAttempt<B: StageBackend> {
    backend: B,
    session: Option<B::Session>,
    binding: Binding,
    state_generation: u64,
    recovery_identity: Option<Identity>,
    position: u32,
    poisoned: bool,
    closed: bool,
    errors: Vec<Error>,
    close_failure_index: Option<usize>,
}
impl<B: StageBackend> StageAttempt<B> {
    pub fn new(
        mut backend: B,
        binding: Binding,
        limits: Limits,
        state_generation: u64,
    ) -> Result<Self, Error> {
        let session = backend.new_session(binding.clone(), limits)?;
        Ok(Self {
            backend,
            session: Some(session),
            binding,
            state_generation,
            recovery_identity: None,
            position: 0,
            poisoned: false,
            closed: false,
            errors: Vec::new(),
            close_failure_index: None,
        })
    }
    /// Host-admitted mapping of exact command subject to immutable L8 binding.
    pub fn new_recovery(
        backend: B,
        binding: Binding,
        limits: Limits,
        state_generation: u64,
        identity: Identity,
    ) -> Result<Self, Error> {
        let mut attempt = Self::new(backend, binding, limits, state_generation)?;
        attempt.recovery_identity = Some(identity);
        Ok(attempt)
    }
    pub fn replay_prefill(
        &mut self,
        recovery: &Recovery,
        payload: [u8; 32],
    ) -> Result<Output, Error> {
        if recovery.phase() != Phase::Prefill
            || recovery.successor() != self.recovery_identity.as_ref()
        {
            return Err(Error::new(ErrorCode::Binding, "router_replay_identity"));
        }
        self.run(
            Step {
                binding: self.binding.clone(),
                state_generation: self.state_generation,
                position: 0,
                payload,
                input: stage_contract::Input::Tokens(recovery.replay_context()),
            },
            true,
        )
    }
    pub fn run(&mut self, step: Step, prefill: bool) -> Result<Output, Error> {
        if self.poisoned
            || self.closed
            || step.binding != self.binding
            || step.state_generation != self.state_generation
            || step.position != self.position
        {
            return Err(Error::new(ErrorCode::Binding, "router_stage_binding"));
        }
        let count = match &step.input {
            stage_contract::Input::Tokens(t) => u32::try_from(t.len()).ok(),
            stage_contract::Input::Hidden(a) => Some(a.shape[1]),
        }
        .filter(|n| *n > 0)
        .ok_or_else(|| Error::new(ErrorCode::Position, "router_stage_input"))?;
        let expected = self
            .position
            .checked_add(count)
            .ok_or_else(|| Error::new(ErrorCode::Position, "router_stage_position"))?;
        let session = self
            .session
            .as_mut()
            .ok_or_else(|| Error::new(ErrorCode::Closed, "router_stage_session"))?;
        let result = if prefill {
            self.backend.prefill(session, step)
        } else {
            self.backend.decode(session, step)
        };
        match result {
            Ok(output)
                if output.state_generation == self.state_generation
                    && output.committed_position == expected =>
            {
                self.position = expected;
                Ok(output)
            }
            Ok(_) => {
                self.poisoned = true;
                Err(Error::new(ErrorCode::Binding, "router_stage_output"))
            }
            Err(error) => {
                self.poisoned = true;
                Err(error)
            }
        }
    }
    pub fn cleanup_errors(&self) -> &[Error] {
        &self.errors
    }
    pub fn clean(&self) -> bool {
        self.closed && self.session.is_none() && self.errors.is_empty()
    }
    /// Attempt every independently owned cleanup even if cancellation fails.
    /// A consumed release error cannot later be rewritten as proof of absence.
    pub fn close(&mut self) -> Result<(), Error> {
        self.poisoned = true;
        if !self.closed {
            if let Some(mut session) = self.session.take() {
                if let Err(e) = self.backend.cancel(&mut session) {
                    self.errors.push(e);
                }
                if let Err(e) = self.backend.synchronize() {
                    self.errors.push(e);
                }
                if let Err(e) = self.backend.release_session(session) {
                    self.errors.push(e);
                }
            }
            match self.backend.close() {
                Ok(()) => self.closed = true,
                Err(e) => {
                    if let Some(index) = self.close_failure_index {
                        self.errors[index] = e;
                    } else {
                        self.close_failure_index = Some(self.errors.len());
                        self.errors.push(e);
                    }
                }
            }
        }
        if self.clean() {
            Ok(())
        } else {
            Err(Error::new(
                ErrorCode::Cleanup,
                "router_stage_cleanup_unproven",
            ))
        }
    }
}
#[derive(Debug)]
pub enum SettlementError {
    Proof(ContractError),
    Worker(node::WorkerError),
    Lifecycle(node::Error),
}
/// Release a durable L6 reservation only after exact signed request cleanup,
/// local L8 release and L6 environmental proof. Core::cleaned performs its own
/// persisted revision CAS; a stale in-memory owner cannot settle after reopen.
/// The ticket/subject mapping is independently held admission state, not wire data.
/// This releases a worker reservation, NOT the request's publication generation.
pub fn settle_worker<B: StageBackend, W: WorkerAdapter>(
    core: &mut node::Core,
    stage: &StageAttempt<B>,
    supervisor: &mut Supervisor<W>,
    expected: &Ticket,
    proof: &CleanupProof,
    subject: &serde_json::Value,
    now: node::Now,
) -> Result<(), SettlementError> {
    let matches = core
        .state()
        .workers
        .get(&expected.node)
        .is_some_and(|w| &w.ticket == expected);
    proof
        .verify(subject, now.mono, matches)
        .map_err(SettlementError::Proof)?;
    if !stage.clean() {
        return Err(SettlementError::Proof(ContractError(
            "worker_stage_cleanup_unproven",
        )));
    }
    let local = supervisor
        .observe_cleanup()
        .map_err(SettlementError::Worker)?;
    if local.ticket() != expected {
        return Err(SettlementError::Proof(ContractError(
            "worker_cleanup_generation_stale",
        )));
    }
    core.cleaned(&local, now)
        .map_err(SettlementError::Lifecycle)
}
/// Collect genuine L6 environmental observation and L5 signed quorum through
/// the existing router verifier. No command ACK or peer assertion substitutes
/// for stage release, OS/resource observation, or the current generation ticket.
/// `observe` must read the current L6 reservation under its owner lock.
pub fn finish_cleanup<B: StageBackend, W: WorkerAdapter>(
    round: CleanupRound,
    stage: &StageAttempt<B>,
    supervisor: &mut Supervisor<W>,
    expected: &Ticket,
    mut observe: impl FnMut() -> (MonoInstant, Option<Ticket>),
) -> Result<CleanupProof, ContractError> {
    let local = supervisor
        .observe_cleanup()
        .map_err(|_| ContractError("worker_cleanup_observation_failed"))?;
    round.finish(
        || {
            if !stage.clean() || local.ticket() != expected || !local.proven() {
                return Err(ContractError("worker_cleanup_unproven"));
            }
            Ok(())
        },
        || {
            let (now, current) = observe();
            (now, current.as_ref() == Some(expected))
        },
    )
}
