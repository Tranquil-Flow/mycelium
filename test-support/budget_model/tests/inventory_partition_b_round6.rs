// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/a10_fault_negatives.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

#[test]
fn b_reference_double_epochs_are_not_operational_duration_defaults() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in [352, 510] {
        let r = rows
            .iter()
            .find(|r| r.path == "mycelium_router/a10_fault_negatives.py" && r.line == line)
            .expect("source-reviewed CPU fault scenario input");
        assert_eq!(r.disposition, "fixture-only");
        assert_eq!(r.unit, "not-applicable");
        assert!(r.budget.is_empty() && r.value.is_none());
        assert!(proof::validate(r, &rows, &all));
        let mut bad = r.clone();
        bad.expression.push('!');
        assert!(!proof::validate(&bad, &rows, &all));
        bad = r.clone();
        bad.value = Some(1000);
        bad.unit = "milliseconds".into();
        assert!(!proof::validate(&bad, &rows, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_cpu_fixture_rule_refuses_live_matrix_arbitration_and_timeouts() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let original = rows
        .iter()
        .find(|r| r.path == "mycelium_router/a10_fault_negatives.py" && r.line == 352)
        .unwrap();
    for (path, line) in [
        ("mycelium_a11_route_speculation.py", 222),
        ("mycelium_router/a10_verifier_matrix.py", 83),
        ("mycelium_router/transports/iroh.py", 1917),
    ] {
        let c = all
            .values()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        let mut bad = original.clone();
        bad.key.clone_from(&c.key);
        bad.path.clone_from(&c.path);
        bad.line = c.line;
        bad.column = c.column;
        bad.scope.clone_from(&c.scope);
        bad.symbol.clone_from(&c.symbol);
        bad.expression.clone_from(&c.expression);
        assert!(
            !proof::validate(&bad, &rows, &all),
            "real runtime/clock site {}",
            c.key
        );
    }
}
