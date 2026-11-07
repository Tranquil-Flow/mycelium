// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-FAIL-11 (F13: a delayed probe cannot renew past the old deadline or admit after stop; a failed probe and the final cap refuse admission without replacing the URL); B-ROLL-08 (partial).
//!
//! O:F13 simulated probe completion and route admission, no process or URL change.
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant, RelayAction, ServingProbe};
use runtime::relay::*;
use std::cell::Cell;
struct Resource;
impl RelayResource for Resource {
    fn stop(&mut self) -> Result<(), &'static str> {
        Ok(())
    }
}
fn at(t: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), t)
}
#[test]
fn delayed_probe_cannot_renew_past_old_deadline_or_admit_after_stop() {
    let mut r = QuickRelay::new(at(0), "https://fixture.invalid".into(), Resource).unwrap();
    let deadline = r.deadline();
    let now = Cell::new(
        deadline
            .checked_sub(BudgetId::RelayRenewalLead.span(Entries::ZERO).unwrap())
            .unwrap(),
    );
    let action = r
        .refresh(
            || now.get(),
            |origin| {
                assert_eq!(origin, "https://fixture.invalid");
                now.set(deadline);
                ServingProbe::Serving
            },
        )
        .unwrap();
    assert_eq!(action, RelayAction::Closed);
    assert!(!r.admitting(deadline));
    assert_eq!(r.deadline(), deadline);
    assert!(r.clean());
}
#[test]
fn failed_probe_and_final_cap_refuse_new_admission_without_url_replacement() {
    let mut r = QuickRelay::new(at(0), "https://fixture.invalid".into(), Resource).unwrap();
    assert!(!r.admitting(at(0))); // no serving proof yet
    r.refresh(|| at(0), |_| ServingProbe::Serving).unwrap();
    assert!(r.admitting(at(0)));
    r.refresh(|| at(0), |_| ServingProbe::Failed).unwrap();
    assert!(!r.admitting(at(0)));
    let lead = BudgetId::RelayRenewalLead.span(Entries::ZERO).unwrap();
    loop {
        let now = r.deadline().checked_sub(lead).unwrap();
        let a = r.refresh(|| now, |_| ServingProbe::Serving).unwrap();
        assert_eq!(r.origin(), "https://fixture.invalid");
        if a == RelayAction::CapReached {
            assert!(!r.admitting(now));
            break;
        }
        assert_eq!(a, RelayAction::Renewed);
    }
    r.stop().unwrap();
    assert!(!r.admitting(at(0)));
}
