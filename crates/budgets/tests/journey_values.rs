// SPDX-License-Identifier: AGPL-3.0-only
use budgets::table::{Provenance, Purpose};
use budgets::{BudgetId, Entries};

#[test]
fn journey_session_renew_after_retains_harness_policy() {
    let id = BudgetId::from_name("JourneySessionRenewAfter").expect("named journey renewal policy");
    assert_eq!(id.span(Entries::new(0)).unwrap().millis(), 3_300_000);
    assert_eq!(id.entry().purpose, Purpose::Policy);
    assert!(matches!(id.entry().provenance, Provenance::Source(source)
        if source.contains("test-support/journeys/src/twohost.rs:756-758")
        && source.contains("not a product grant or measured p99")));
}

#[test]
fn journey_read_timeout_overshoot_remains_an_injected_fault() {
    let id = BudgetId::from_name("JourneyReadTimeoutOvershoot").expect("named injected overshoot");
    assert_eq!(id.span(Entries::new(0)).unwrap().millis(), 700);
    assert_eq!(id.entry().purpose, Purpose::HistoricalFault);
    assert!(matches!(id.entry().provenance, Provenance::Source(source)
        if source.contains("test-support/journeys/tests/twohost.rs:978-990")
        && source.contains("not a product allowance or measured p99")));
}
