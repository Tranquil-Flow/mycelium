// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId, Entries, MonoInstant, Refusal, RelayAction, RelayBudget, ServingProbe};
/// Resource already started by the host. stop must prove all owned resources
/// closed; a successful signal/send alone is not proof.
pub trait RelayResource {
    fn stop(&mut self) -> Result<(), &'static str>;
}
#[derive(Debug, PartialEq, Eq)]
pub enum RelayError {
    Budget(Refusal),
    Cleanup(&'static str),
}
pub struct QuickRelay<R: RelayResource> {
    budget: RelayBudget,
    origin: String,
    resource: R,
    clean: bool,
    serving: bool,
    stopped: bool,
}
impl<R: RelayResource> QuickRelay<R> {
    pub fn new(now: MonoInstant, origin: String, resource: R) -> Result<Self, Refusal> {
        Ok(Self {
            budget: RelayBudget::new(now, BudgetId::RelayWindow.span(Entries::ZERO)?)?,
            origin,
            resource,
            clean: false,
            serving: false,
            stopped: false,
        })
    }
    pub fn origin(&self) -> &str {
        &self.origin
    }
    pub fn deadline(&self) -> MonoInstant {
        self.budget.deadline()
    }
    pub fn granted(&self) -> budgets::Span {
        self.budget.granted()
    }
    pub fn clean(&self) -> bool {
        self.clean
    }
    /// Probe this exact origin, then sample the clock again. A probe started
    /// before expiry cannot retroactively renew an expired window.
    pub fn refresh(
        &mut self,
        mut clock: impl FnMut() -> MonoInstant,
        probe: impl FnOnce(&str) -> ServingProbe,
    ) -> Result<RelayAction, RelayError> {
        let before = clock();
        if self.stopped || self.deadline().remaining(before).is_err() {
            return self.step(before, ServingProbe::NotServing);
        }
        let observed = probe(&self.origin);
        let after = clock();
        if let Err(error) = after.elapsed_since(before) {
            self.serving = false;
            return Err(RelayError::Budget(error));
        }
        self.step(after, observed)
    }
    pub fn admitting(&self, now: MonoInstant) -> bool {
        self.serving && !self.stopped && self.deadline().remaining(now).is_ok()
    }
    pub fn step(
        &mut self,
        now: MonoInstant,
        probe: ServingProbe,
    ) -> Result<RelayAction, RelayError> {
        self.serving = false;
        let action = self.budget.step(now, probe).map_err(RelayError::Budget)?;
        self.serving = probe == ServingProbe::Serving
            && !self.stopped
            && !matches!(action, RelayAction::Closed | RelayAction::CapReached);
        if action == RelayAction::Closed {
            self.stop()?;
        }
        Ok(action)
    }
    pub fn stop(&mut self) -> Result<(), RelayError> {
        self.stopped = true;
        self.serving = false;
        self.budget.stop();
        if !self.clean {
            self.resource.stop().map_err(RelayError::Cleanup)?;
            self.clean = true;
        }
        Ok(())
    }
}
