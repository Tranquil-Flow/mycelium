// SPDX-License-Identifier: AGPL-3.0-only
use budgets::table::{Provenance, Purpose, Value};
use budgets::{BudgetId, Entries};
const CASES: [(&str, &str); 3] = [
    (
        "PySourceUnboundedR24SeedMemberLeaseL280C24",
        "v0.1.4 ships no ceiling at mycelium_seed/coordinator.py:280 @ f4e366e9; field lease_expires_at; persisted finite member lease and max-renewal accepted domain, not bounded mint success or invite expiry; V03: no invented ceiling",
    ),
    (
        "PySourceUnboundedR24SeedMemberLeaseL1118C20",
        "v0.1.4 ships no ceiling at mycelium_seed/coordinator.py:1118 @ f4e366e9; field lease_expires_at; persisted finite member lease and max-renewal accepted domain, not bounded mint success or invite expiry; V03: no invented ceiling",
    ),
    (
        "PySourceUnboundedR24SeedMemberLeaseL1243C12",
        "v0.1.4 ships no ceiling at mycelium_seed/coordinator.py:1243 @ f4e366e9; field member.lease_expires_at; persisted finite member lease and max-renewal accepted domain, not bounded mint success or invite expiry; V03: no invented ceiling",
    ),
];
#[test]
fn member_lease_ids_refuse_numeric_values_and_wrong_origins() {
    for (name, origin) in CASES {
        let id = BudgetId::from_name(name).expect("exact member lease ID");
        assert_eq!(id.name(), name);
        assert_eq!(id.entry().value, Value::Blocked);
        assert_eq!(id.entry().purpose, Purpose::Policy);
        assert_eq!(id.entry().provenance, Provenance::Blocked(origin));
        assert!(id.span(Entries::new(0)).is_err());
        assert!(id.count().is_err());
    }
}
