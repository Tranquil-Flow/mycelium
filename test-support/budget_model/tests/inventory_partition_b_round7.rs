// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_deadlines.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

#[test]
fn b_cleanup_default_composition_uses_code_not_stale_comments() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let fanout = budgets::BudgetId::from_name("PyRouteCleanupFanout").unwrap();
    assert_eq!(
        fanout.span(budgets::Entries::new(0)).unwrap().millis(),
        4500
    );
    for (line, expected) in [
        (410, "PyRouteCleanupFanout"),
        (1226, "PyRouteCleanupFanout"),
        (1227, "CancelAndCleanup"),
    ] {
        let r = rows
            .iter()
            .find(|r| r.path == "mycelium_deadlines.py" && r.line == line)
            .expect("explicit configured composition default");
        assert_eq!(r.relation, expected);
        assert_eq!(r.unit, "milliseconds");
        assert!(r.value.is_none() && r.budget.is_empty());
        assert!(r.semantic.contains("default"));
        assert!(proof::validate(r, &rows, &all));
        let mut bad = r.clone();
        bad.relation = "RouterClose".into();
        assert_ne!(bad.relation, r.relation);
        assert!(!proof::validate(&bad, &rows, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_required_and_explicit_override_inputs_cannot_borrow_composition_defaults() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let r = rows
        .iter()
        .find(|r| r.path == "mycelium_deadlines.py" && r.line == 410)
        .unwrap();
    for line in [422, 1154, 1181, 1182, 1242] {
        let c = all
            .values()
            .find(|c| c.path == "mycelium_deadlines.py" && c.line == line)
            .unwrap();
        let mut bad = r.clone();
        bad.key.clone_from(&c.key);
        bad.line = c.line;
        bad.column = c.column;
        bad.scope.clone_from(&c.scope);
        bad.symbol.clone_from(&c.symbol);
        bad.expression.clone_from(&c.expression);
        assert!(
            !proof::validate(&bad, &rows, &all),
            "unbounded override/required operand {}",
            c.key
        );
    }
}
