// SPDX-License-Identifier: AGPL-3.0-only
//! Source-specific STATIC provenance, not product runtime enforcement.
use budgets::{
    BudgetId,
    table::{Provenance, Purpose, Value},
};
#[test]
fn exact_forwarded_stats_policy_identity_value_and_origin() {
    let e = BudgetId::from_name("PySourceUnboundedR36StatsLowL21C30")
        .expect("own source-specific ID")
        .entry();
    assert_eq!(e.value, Value::Blocked);
    assert_eq!(e.purpose, Purpose::Policy);
    assert_eq!(
        e.provenance,
        Provenance::Blocked(
            "v0.1.4 ships no ceiling at mycelium_swarm_directory/stats_docs.py:21 @ f4e366e9; field low; caller-supplied threshold operand, not validated values or JSON envelopes; V03 any new ceiling deferred"
        )
    );
    let e = BudgetId::from_name("PySourceUnboundedR36StatsHighL21C40")
        .expect("own source-specific ID")
        .entry();
    assert_eq!(e.value, Value::Blocked);
    assert_eq!(e.purpose, Purpose::Policy);
    assert_eq!(
        e.provenance,
        Provenance::Blocked(
            "v0.1.4 ships no ceiling at mycelium_swarm_directory/stats_docs.py:21 @ f4e366e9; field high; caller-supplied threshold operand, not validated values or JSON envelopes; V03 any new ceiling deferred"
        )
    );
    let e = BudgetId::from_name("PySourceUnboundedR36StatsMaxNL31C26")
        .expect("own source-specific ID")
        .entry();
    assert_eq!(e.value, Value::Blocked);
    assert_eq!(e.purpose, Purpose::Policy);
    assert_eq!(
        e.provenance,
        Provenance::Blocked(
            "v0.1.4 ships no ceiling at mycelium_swarm_directory/stats_docs.py:31 @ f4e366e9; field max_n; caller-supplied threshold operand, not validated values or JSON envelopes; V03 any new ceiling deferred"
        )
    );
}
