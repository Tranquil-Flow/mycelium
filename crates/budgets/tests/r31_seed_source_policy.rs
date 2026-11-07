// SPDX-License-Identifier: AGPL-3.0-only
//! STATIC policy provenance; deliberately not numeric or enforcement coverage.
use budgets::{
    BudgetId,
    table::{Provenance, Purpose, Value},
};
const CASES: &[(&str, &str)] = &[
    (
        "PySourceUnboundedR31SeedDecodedMemberLeaseL827C16",
        "v0.1.4 ships no ceiling at mycelium_seed/state.py:827 @ f4e366e9; field lease_expires_at; finite persisted source domain, no policy magnitude ceiling; V03 any new ceiling deferred",
    ),
    (
        "PySourceUnboundedR31SeedRotationMemberLeaseL641C19",
        "v0.1.4 ships no ceiling at mycelium_seed/operator.py:641 @ f4e366e9; field member['lease_expires_at']; finite persisted source domain, no policy magnitude ceiling; V03 any new ceiling deferred",
    ),
    (
        "PySourceUnboundedR31SeedDecodedHeartbeatDueL830C16",
        "v0.1.4 ships no ceiling at mycelium_seed/state.py:830 @ f4e366e9; field next_heartbeat_due_at; finite persisted source domain, no policy magnitude ceiling; V03 any new ceiling deferred",
    ),
];
#[test]
fn exact_seed_source_policy_identity_value_and_origin() {
    for (name, provenance) in CASES {
        let id = BudgetId::from_name(name).expect("own source-specific ID");
        let e = id.entry();
        assert_eq!(
            e.value,
            Value::Blocked,
            "R31 no invented numeric ceiling: {name}"
        );
        assert_eq!(
            e.purpose,
            Purpose::Policy,
            "R31 STATIC policy purpose: {name}"
        );
        assert_eq!(
            e.provenance,
            Provenance::Blocked(provenance),
            "R31 STATIC exact own origin: {name}"
        );
    }
}
