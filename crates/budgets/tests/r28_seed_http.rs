// SPDX-License-Identifier: AGPL-3.0-only
use budgets::table::{Provenance, Purpose, Value};
use budgets::{BudgetId, Entries};
const CASES: [(&str, &str); 1] = [(
    "PySourceUnboundedR28SeedHttpExpiryL968C8",
    "v0.1.4 ships no ceiling at mycelium_seed/http.py:968 @ f4e366e9; field expires is an authenticated absolute expiry; freshness lower checks are not a horizon cap; V03 new ceiling deferred",
)];
#[test]
fn authenticated_http_expiry_has_no_numerical_budget_credit() {
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
