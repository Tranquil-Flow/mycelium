// SPDX-License-Identifier: AGPL-3.0-only
use budgets::table::{Provenance, Purpose, Value};
use budgets::{BudgetId, Entries};
const CASES: [(&str, &str); 2] = [
    (
        "PySourceUnboundedR26SeedRequestExpiryL1310C20",
        "v0.1.4 ships no ceiling at mycelium_seed/coordinator.py:1310 @ f4e366e9; field request_expiry; raw input domain, not successful bounded renewal output; V03 new ceiling deferred",
    ),
    (
        "PySourceUnboundedR26SeedPreviousDeadlineL1385C20",
        "v0.1.4 ships no ceiling at mycelium_seed/coordinator.py:1385 @ f4e366e9; field previous_deadline; raw input domain, not successful bounded renewal output; V03 new ceiling deferred",
    ),
];
#[test]
fn raw_seed_inputs_have_no_numerical_budget_credit() {
    for (name, origin) in CASES {
        let id = BudgetId::from_name(name).expect("exact raw input ID");
        assert_eq!(id.name(), name);
        assert_eq!(id.entry().value, Value::Blocked);
        assert_eq!(id.entry().purpose, Purpose::Policy);
        assert_eq!(id.entry().provenance, Provenance::Blocked(origin));
        assert!(id.span(Entries::new(0)).is_err());
        assert!(id.count().is_err());
    }
}
