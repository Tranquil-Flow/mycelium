// SPDX-License-Identifier: AGPL-3.0-only
//! Port of token_receipt_mailbox.MailboxReceiptStream @ f4e366e9:25–104.
use protocol::{
    ContractError, canonical,
    token::{AdmittedGrant, ReceiptCursor, VerifiedReceipt},
};
use serde_json::Value;
use std::{collections::VecDeque, sync::Arc};

/// Owner-local inbox, not cleanup authority. Serialize calls in the owning actor.
/// `None` retains the oracle's token-budget-bounded default. A pull driver should
/// honor `can_pull`; a producer violating the cap gets the original sticky overflow.
pub struct ReceiptStream {
    cursor: ReceiptCursor,
    deadline: f64,
    token_capacity: u64,
    capacity: Option<usize>,
    queue: VecDeque<Arc<VerifiedReceipt>>,
    queued_tokens: u64,
    last: Option<Arc<VerifiedReceipt>>,
    error: Option<ContractError>,
    seal_error: Option<ContractError>,
    terminal_read: bool,
}
impl ReceiptStream {
    pub fn new(
        grant: AdmittedGrant,
        key: Value,
        capacity: Option<usize>,
    ) -> Result<Self, ContractError> {
        if capacity.is_some_and(|n| !(1..=64).contains(&n)) {
            return Err(ContractError("token_progress_arguments_invalid"));
        }
        let deadline = grant.deadline_monotonic_s();
        let token_capacity = grant.statement()["max_new_tokens"]
            .as_u64()
            .ok_or(ContractError("token_path_grant_invalid"))?;
        Ok(Self {
            cursor: ReceiptCursor::new(grant, key),
            deadline,
            token_capacity,
            capacity,
            queue: VecDeque::new(),
            queued_tokens: 0,
            last: None,
            error: None,
            seal_error: None,
            terminal_read: false,
        })
    }
    pub fn check(&self, observe: impl FnOnce() -> (f64, bool)) -> Result<(), ContractError> {
        if let Some(error) = &self.error {
            return Err(error.clone());
        }
        let (now, current) = observe();
        if !current {
            return Err(ContractError("token_path_grant_stale"));
        }
        if !now.is_finite() || now < 0.0 || now >= self.deadline {
            return Err(ContractError("token_path_grant_expired"));
        }
        Ok(())
    }
    pub fn accept(
        &mut self,
        envelope: &Value,
        mut observe: impl FnMut() -> (f64, bool),
        seal: impl FnOnce(&[u8]) -> Result<(), ContractError>,
    ) -> Result<Arc<VerifiedReceipt>, ContractError> {
        let bytes = canonical::to_bytes(envelope)
            .map_err(|_| ContractError("token_progress_frame_invalid"))?;
        if bytes.len() > protocol::native::MAX_RECEIPT_BYTES {
            return Err(ContractError("token_progress_frame_invalid"));
        }
        self.check(&mut observe)?;
        if let Some(last) = &self.last
            && last.envelope_bytes() == bytes
        {
            return Ok(last.clone());
        }
        if self.capacity.is_some_and(|n| self.queue.len() >= n) {
            self.close("token_progress_overflow");
            return Err(ContractError("token_progress_overflow"));
        }
        let receipt = Arc::new(self.cursor.accept_verified(envelope, &mut observe)?);
        let count = receipt.token_ids().len() as u64;
        if count > self.token_capacity.saturating_sub(self.queued_tokens) {
            self.close("token_progress_overflow");
            return Err(ContractError("token_progress_overflow"));
        }
        if let Err(error) = seal(receipt.envelope_bytes()) {
            self.seal_error = Some(error);
            self.close("token_progress_seal_failed");
            return Err(ContractError("token_progress_seal_failed"));
        }
        self.check(&mut observe)?;
        self.queued_tokens += count;
        self.last = Some(receipt.clone());
        self.queue.push_back(receipt.clone());
        Ok(receipt)
    }
    /// Nonblocking poll; None means wait, not EOF. Driver supplies registry budgets.
    pub fn read(
        &mut self,
        observe: impl FnOnce() -> (f64, bool),
    ) -> Result<Option<Arc<VerifiedReceipt>>, ContractError> {
        self.check(observe)?;
        if self.terminal_read {
            return Err(ContractError("token_progress_terminal"));
        }
        let receipt = self.queue.pop_front();
        if let Some(receipt) = &receipt {
            self.queued_tokens -= receipt.token_ids().len() as u64;
            self.terminal_read = receipt.status() != "DECODING";
        }
        Ok(receipt)
    }
    pub fn can_pull(&self) -> bool {
        self.error.is_none()
            && self.cursor.terminal().is_none()
            && self.capacity.is_none_or(|n| self.queue.len() < n)
    }
    pub fn close(&mut self, code: &'static str) {
        self.error.get_or_insert(ContractError(code));
        self.queue.clear();
        self.queued_tokens = 0;
    }
    /// Safe typed cause retained separately from the stable public seal error.
    pub fn seal_error(&self) -> Option<&ContractError> {
        self.seal_error.as_ref()
    }
}
