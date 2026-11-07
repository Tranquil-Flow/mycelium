// SPDX-License-Identifier: AGPL-3.0-only
//! Original invite expiry has no source magnitude ceiling; do not invent a lifetime.
use budgets::table::{Provenance, Purpose, Value};
use budgets::{BudgetId, Entries};
const ORIGIN: &str = "v0.1.4 ships no ceiling at mycelium_seed/coordinator.py:690 @ f4e366e9; field invite_expires_at; original verified invite expiry, persisted unchanged for nonce retention, not member lease or HTTP envelope TTL; finite/ordered/expiry predicates preserved; V03: no invented ceiling";
#[test]
fn seed_original_invite_expiry_refuses_a_numeric_budget() {
    let id = BudgetId::from_name("PySourceUnboundedR23SeedInviteExpiryL690C20").unwrap();
    assert_eq!(id.name(), "PySourceUnboundedR23SeedInviteExpiryL690C20");
    assert_eq!(id.entry().value, Value::Blocked);
    assert_eq!(id.entry().purpose, Purpose::Policy);
    assert_eq!(id.entry().provenance, Provenance::Blocked(ORIGIN));
    assert!(id.span(Entries::new(0)).is_err());
    assert!(id.count().is_err());
}
