// SPDX-License-Identifier: AGPL-3.0-only
//! Static harness-policy contract, not Python policy or measured latency evidence.
use budgets::table::{Provenance, Purpose, Value};
use budgets::{BudgetId, Entries, Span};

const SESSION_ORIGIN: &str = "crates/runtime/tests/support/managed_owner_factory.rs:166; crates/runtime/tests/support/managed_owner_sessions.rs:220,259,262,294,363 @ 9f37ca8be3d9c9b9921de6df903dbaf6adf41416; retained HelloSessions and held-client fixture session timeout; not a product ceiling or measured p99";
const BARRIER_ORIGIN: &str = "crates/runtime/tests/support/managed_owner_sessions.rs:225,267,299,368 @ 9f37ca8be3d9c9b9921de6df903dbaf6adf41416; held IO-entry signal wait before interrupt, concurrent progress, session close or owner close; not a product ceiling or measured p99";

#[test]
fn managed_owner_factory_fixture_timeout_retains_exact_static_contract() {
    let id = BudgetId::from_name("ManagedOwnerFactoryFixtureSessionTimeout")
        .expect("source-specific managed owner fixture session policy");
    assert_eq!(id.name(), "ManagedOwnerFactoryFixtureSessionTimeout");
    assert_eq!(id.entry().value, Value::Duration(Span::from_millis(30_000)));
    assert_eq!(id.entry().purpose, Purpose::Policy);
    assert_eq!(id.entry().provenance, Provenance::Source(SESSION_ORIGIN));
    let span: Span = id.span(Entries::new(0)).unwrap();
    assert_eq!(span.millis(), 30_000);
    assert_eq!(
        std::time::Duration::from_millis(span.millis()).as_secs_f64(),
        30.0
    );
}

#[test]
fn managed_owner_fixture_barrier_wait_retains_exact_static_contract() {
    let id = BudgetId::from_name("ManagedOwnerFixtureBarrierWait")
        .expect("source-specific held IO-entry barrier policy");
    assert_eq!(id.name(), "ManagedOwnerFixtureBarrierWait");
    assert_eq!(id.entry().value, Value::Duration(Span::from_millis(5_000)));
    assert_eq!(id.entry().purpose, Purpose::Policy);
    assert_eq!(id.entry().provenance, Provenance::Source(BARRIER_ORIGIN));
    let span: Span = id.span(Entries::new(0)).unwrap();
    assert_eq!(span.millis(), 5_000);
    assert_eq!(std::time::Duration::from_millis(span.millis()).as_secs(), 5);
}
