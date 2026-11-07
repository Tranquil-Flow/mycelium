// SPDX-License-Identifier: AGPL-3.0-only
//! L16 forwarding decision effects. Persistence and native IO are external L6
//! seams; cancellation retirement must never authorize native receipt eviction.
use protocol::{
    ContractError,
    progress::{PendingForward, VerifiedDecision},
};
use serde_json::Value;
pub trait ForwardEffects {
    fn persist(&mut self, row: &Value) -> Result<(), ContractError>;
    fn stop_reader(&mut self) -> Result<(), ContractError>;
    fn clear_outbound(&mut self) -> Result<(), ContractError>;
    fn native_ack(&mut self, ack: &Value) -> Result<(), ContractError>;
}
pub struct Forwarding {
    pending: PendingForward,
    reader_closed: bool,
    outbound_cleared: bool,
}
impl Forwarding {
    pub fn new(command: &str, receipt: &Value) -> Result<Self, ContractError> {
        Ok(Self {
            pending: PendingForward::new(command, receipt)?,
            reader_closed: false,
            outbound_cleared: false,
        })
    }
    pub fn apply(
        &mut self,
        decision: VerifiedDecision,
        e: &mut impl ForwardEffects,
    ) -> Result<(), ContractError> {
        let cancelled = decision.is_cancelled();
        self.pending.record(decision, |row| e.persist(row))?;
        if cancelled {
            if !self.reader_closed {
                e.stop_reader()?;
                self.reader_closed = true;
            }
        } else if let Some(ack) = self.pending.native_ack() {
            e.native_ack(ack)?;
        }
        if !self.outbound_cleared {
            e.clear_outbound()?;
            self.outbound_cleared = true;
        }
        Ok(())
    }
    pub fn ready_to_ack(&self, claim_phase: Option<&str>) -> bool {
        self.reader_closed && self.outbound_cleared && self.pending.ready_to_ack(claim_phase)
    }
    pub fn snapshot(&self) -> &Value {
        self.pending.snapshot()
    }
}
