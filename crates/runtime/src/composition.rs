// SPDX-License-Identifier: AGPL-3.0-only
//! Event-driven live owner composition. L6 supplies authenticated currentness,
//! dispatch/native completion and local cleanup effects; L8 is not invoked here.
//! All methods require exclusive access: consumers may use one actor or mutex,
//! but must not hold their global status lock while performing seal/IO callbacks.
use protocol::{
    ContractError,
    token::{AdmittedGrant, VerifiedReceipt},
};
use router::{
    cleanup::{CleanupProof, RequestOwner, Terminal},
    owner::{OwnerAttempt, RegistrationBook, Retirement},
    stream::ReceiptStream,
};
use serde_json::Value;
use std::sync::Arc;
pub struct Session {
    command: String,
    book: RegistrationBook,
    attempt: OwnerAttempt,
    owner: RequestOwner,
    native: Option<Result<(), ContractError>>,
}
#[derive(Debug, PartialEq, Eq)]
pub enum Pull {
    Backpressure,
    Accepted,
}
impl Session {
    pub fn new(
        grant: AdmittedGrant,
        entry_key: Value,
        cleanup_subject: Value,
        capacity: Option<usize>,
    ) -> Result<Self, ContractError> {
        let scope = &grant.statement()["scope"];
        let control = scope["control"]
            .as_object()
            .ok_or(ContractError("session_binding_invalid"))?;
        if cleanup_subject["request_id"] != scope["request_id"]
            || control
                .iter()
                .any(|(k, v)| k != "cancellation_generation" && cleanup_subject[k] != *v)
        {
            return Err(ContractError("session_binding_invalid"));
        }
        let command = control["command_id"]
            .as_str()
            .ok_or(ContractError("session_binding_invalid"))?
            .to_owned();
        let retirement = Retirement {
            grant_digest: grant.digest().to_owned(),
            target_endpoint_id: scope["entry_endpoint_id"]
                .as_str()
                .ok_or(ContractError("session_binding_invalid"))?
                .to_owned(),
        };
        let owner = RequestOwner::new(cleanup_subject)?;
        let stream = ReceiptStream::new(grant, entry_key, capacity)?;
        let mut book = RegistrationBook::default();
        book.open(command.clone(), stream, retirement)?;
        Ok(Self {
            command,
            book,
            attempt: OwnerAttempt::default(),
            owner,
            native: None,
        })
    }
    /// Dispatch is one-shot even on error; never replay an uncertain infer_start.
    pub fn start(
        &mut self,
        dispatch: impl FnOnce() -> Result<(), ContractError>,
    ) -> Result<(), ContractError> {
        self.attempt.start()?;
        dispatch()
    }
    /// Caller supplies the already authenticated ordinary native result. It is not
    /// a token source and cannot by itself publish a successful request terminal.
    pub fn native_result(
        &mut self,
        result: Result<(), ContractError>,
    ) -> Result<(), ContractError> {
        if self.native.is_some() {
            return Err(ContractError("native_result_duplicate"));
        }
        if result.is_ok() {
            self.owner.native_completed();
        }
        self.native = Some(result);
        Ok(())
    }
    /// Backpressure happens before polling the producer; acceptance overflow still
    /// preserves the original fail-closed inbox semantics for unsolicited receipts.
    pub fn pull(
        &mut self,
        receive: impl FnOnce() -> Result<Value, ContractError>,
        mut observe: impl FnMut() -> (f64, bool),
        seal: impl FnOnce(&[u8]) -> Result<(), ContractError>,
    ) -> Result<Pull, ContractError> {
        let stream = self.book.stream(&self.command)?;
        stream.check(&mut observe)?;
        if !stream.can_pull() {
            return Ok(Pull::Backpressure);
        }
        stream.accept(&receive()?, observe, seal)?;
        Ok(Pull::Accepted)
    }
    pub fn poll(
        &mut self,
        observe: impl FnOnce() -> (f64, bool),
    ) -> Result<Option<Arc<VerifiedReceipt>>, ContractError> {
        let receipt = self.book.stream(&self.command)?.read(observe)?;
        if let Some(receipt) = &receipt {
            if receipt.status() == "COMPLETED" {
                self.owner.progress_completed();
            } else if receipt.status() != "DECODING" {
                return Err(ContractError("token_progress_noncompleted_terminal"));
            }
        } else if let Some(result) = &self.native {
            result.clone()?;
            return Err(ContractError("token_progress_terminal_missing"));
        }
        Ok(receipt)
    }
    pub fn cancel(
        &mut self,
        authorized: impl FnOnce() -> Result<bool, ContractError>,
    ) -> Result<(), ContractError> {
        if self.owner.terminal().is_some() {
            return Ok(());
        }
        match self.attempt.fence(authorized) {
            Err(ContractError("request_cancelled")) => {
                self.owner.cancel();
                self.attempt.finalize(&mut self.book, &self.command, || {
                    Err(ContractError("authorization_reentered"))
                })
            }
            Err(error) => self
                .attempt
                .finalize(&mut self.book, &self.command, || Err(error)),
            Ok(()) => Ok(()),
        }
    }
    pub fn finalize(
        &mut self,
        authorized: impl FnOnce() -> Result<bool, ContractError>,
    ) -> Result<(), ContractError> {
        self.attempt
            .finalize(&mut self.book, &self.command, authorized)
    }
    pub fn retirement(&self) -> Option<&Retirement> {
        self.book.retirement(&self.command)
    }
    pub fn terminal(&self) -> Option<Terminal> {
        self.owner.terminal()
    }
    pub fn settle(
        &mut self,
        proof: CleanupProof,
        outcome: Terminal,
        observe: impl FnOnce() -> (budgets::MonoInstant, bool),
    ) -> Result<(), ContractError> {
        self.owner.settle(proof, outcome, observe)
    }
}
