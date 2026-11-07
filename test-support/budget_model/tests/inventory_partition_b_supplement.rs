// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/a10_live_publication.py; O:path:mycelium_router/transports/iroh.py.
use budget_model::inventory::{bindings, candidates, categories, checked_in, validate};
#[test]
fn reviewed_interval_container_does_not_hide_its_newly_discovered_guard() {
    let cs = candidates().unwrap();
    let rows = checked_in().unwrap();
    let guard = cs
        .iter()
        .find(|c| {
            c.path == "mycelium_router/a10_live_publication.py"
                && c.line == 292
                && c.kind == "comparison"
        })
        .expect("B review discovered a real omitted guard");
    let target = cs
        .iter()
        .find(|c| c.path == guard.path && c.line == 44)
        .unwrap();
    let row = rows.iter().find(|r| r.key == guard.key).unwrap();
    assert_eq!(guard.expression, "len(intervals) > MAXIMUM_INTERVALS");
    assert!(bindings::accepts(
        "comparison-reference-module-constant",
        guard,
        target
    ));
    assert_eq!(row.unit, "entries");
    assert!(row.value.is_none());
    assert_eq!(
        budgets::BudgetId::from_name(&row.relation)
            .unwrap()
            .count()
            .unwrap()
            .get(),
        256
    );
    for category in [
        "reviewed-nonscalar-slot",
        "reviewed-observation-slot",
        "reviewed-policy-readout",
        "reviewed-structural-slot",
    ] {
        assert!(!categories::accepts(category, guard));
    }
    let mut bad = rows.clone();
    let index = bad.iter().position(|r| r.key == row.key).unwrap();
    bad[index].relation = "PyFinalMyceliumA13MacosClockOffsetL21C0Assignment".into();
    assert!(validate(&bad).is_err());
    assert_eq!(validate(&rows), Ok(()));
}

#[test]
fn boolean_stall_results_do_not_hide_their_independent_numeric_comparisons() {
    let cs = candidates().unwrap();
    for line in [1915, 2108] {
        let result = cs
            .iter()
            .find(|c| {
                c.path == "mycelium_router/transports/iroh.py"
                    && c.line == line
                    && c.kind == "assignment"
            })
            .unwrap();
        assert!(categories::accepts("reviewed-nonscalar-slot", result));
        let predicate = cs
            .iter()
            .find(|c| c.path == result.path && c.line == line && c.kind == "comparison")
            .expect("comparison operand must remain independently inventoried");
        assert_eq!(predicate.expression, "stall_seconds < remaining");
        assert_ne!(predicate.key, result.key);
        for category in [
            "reviewed-nonscalar-slot",
            "reviewed-observation-slot",
            "reviewed-state-comparison",
        ] {
            assert!(!categories::accepts(category, predicate));
        }
    }
}
