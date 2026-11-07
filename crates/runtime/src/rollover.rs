// SPDX-License-Identifier: AGPL-3.0-only
//! One-shot rollover descriptor finalization, faithful to demo_chain.py:698-718
//! and 1113-1123 @ f4e366e9. A lost report is NOT retried in v0.2. The owner-run
//! driver publishes `rollover_members_changed` as failed rather than backoff
//! (owner_run.py:366). V03-PLAN lane 3 owns the proposed behavior change.
use std::collections::BTreeMap;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Collecting,
    Ready,
    Failed(&'static str),
}
/// I is the host's already-authenticated identity tuple, not an arriving claim.
/// Keep the four original pins (generation, incarnation, verification-key digest,
/// endpoint ID); this generic seam does not invent another membership schema.
pub struct DescriptorGate<I> {
    expected: BTreeMap<String, I>,
    state: State,
}
impl<I: PartialEq> DescriptorGate<I> {
    pub fn new(expected: BTreeMap<String, I>) -> Self {
        Self {
            expected,
            state: State::Collecting,
        }
    }
    pub fn state(&self) -> State {
        self.state
    }
    pub fn may_compile(&self) -> bool {
        self.state == State::Ready
    }
    /// Invoke once when the original descriptor-collection phase finishes. The
    /// collector's timeout/staleness/error policy is upstream and unchanged.
    /// `participants` contains only usable signed descriptor targets.
    pub fn finish_collection(
        &mut self,
        current: &BTreeMap<String, I>,
        participants: &[String],
    ) -> Result<(), &'static str> {
        match self.state {
            State::Failed(code) => return Err(code),
            State::Ready => return Ok(()),
            State::Collecting => {}
        }
        let mut reports: Vec<_> = participants.iter().collect();
        reports.sort();
        let expected: Vec<_> = self.expected.keys().collect();
        if current != &self.expected || reports != expected {
            self.state = State::Failed("rollover_members_changed");
            return Err("rollover_members_changed");
        }
        self.state = State::Ready;
        Ok(())
    }
}
