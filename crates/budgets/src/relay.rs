// SPDX-License-Identifier: AGPL-3.0-only
use crate::*;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ServingProbe {
    Serving,
    NotServing,
    Failed,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RelayAction {
    Waiting,
    Renewed,
    CapReached,
    Closed,
}
/// Faithful quick-relay window accounting; this owns no URL, socket or process.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RelayBudget {
    window: Span,
    granted: Span,
    deadline: MonoInstant,
    last: MonoInstant,
    closed: bool,
    cap_reached: bool,
}
impl RelayBudget {
    pub fn new(now: MonoInstant, window: Span) -> Result<Self, Refusal> {
        if window < BudgetId::RelayMinimumWindow.span(Entries::ZERO)?
            || window > BudgetId::QuickRelayMaximumWindow.span(Entries::ZERO)?
        {
            return Err(Refusal::InvalidRelayWindow);
        }
        Ok(Self {
            window,
            granted: window,
            deadline: now.checked_add(window)?,
            last: now,
            closed: false,
            cap_reached: false,
        })
    }
    pub fn deadline(self) -> MonoInstant {
        self.deadline
    }
    pub fn granted(self) -> Span {
        self.granted
    }
    pub fn is_closed(self) -> bool {
        self.closed
    }
    pub fn stop(&mut self) -> RelayAction {
        self.closed = true;
        RelayAction::Closed
    }
    pub fn step(&mut self, now: MonoInstant, probe: ServingProbe) -> Result<RelayAction, Refusal> {
        now.elapsed_since(self.last)?;
        if self.closed {
            return Ok(RelayAction::Closed);
        }
        let left = match self.deadline.remaining(now) {
            Ok(left) => left,
            Err(Refusal::Expired) => {
                self.last = now;
                return Ok(self.stop());
            }
            Err(error) => return Err(error),
        };
        let lead = BudgetId::RelayRenewalLead.span(Entries::ZERO)?.min(
            self.window
                .checked_div(BudgetId::RenewalDivisor.count()?.get())?,
        );
        if !self.cap_reached && left <= lead && probe == ServingProbe::Serving {
            let next = self.granted.checked_add(self.window)?;
            if next > BudgetId::RelayGrantedWindowCap.span(Entries::ZERO)? {
                self.cap_reached = true;
                self.last = now;
                return Ok(RelayAction::CapReached);
            }
            let deadline = now.checked_add(self.window)?;
            self.granted = next;
            self.deadline = deadline;
            self.last = now;
            return Ok(RelayAction::Renewed);
        }
        self.last = now;
        Ok(RelayAction::Waiting)
    }
}
