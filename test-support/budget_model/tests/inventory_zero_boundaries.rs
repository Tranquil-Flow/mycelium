// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/chain_compile.py; O:path:mycelium_assignment_cache.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};
#[test]
fn zero_validation_thresholds_are_not_zero_wait_allowances() {
    let all = candidates().unwrap();
    for (path, line, unit) in [
        ("mycelium_a13_macos/chain_compile.py", 461, "milliseconds"),
        ("mycelium_assignment_cache.py", 99, "bytes"),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == "comparison")
            .unwrap();
        assert_eq!(
            categories::reviewed_quantity(c, "zero-boundary"),
            Some((unit, 0))
        );
        let mut bad = c.clone();
        bad.expression.push_str(" + 1");
        assert_eq!(categories::reviewed_quantity(&bad, "zero-boundary"), None);
        bad = c.clone();
        bad.kind = "keyword".into();
        assert_eq!(categories::reviewed_quantity(&bad, "zero-boundary"), None);
    }
    let lower = all
        .iter()
        .find(|r| r.path == "mycelium_router/terminal_kv_retention.py" && r.line == 62)
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(lower, "positive-boundary"),
        Some(("entries", 1))
    );
    assert_eq!(categories::reviewed_quantity(lower, "upper-guard"), None);
    let mut reversed = lower.clone();
    reversed.expression = "max_entries > 1".into();
    assert_eq!(
        categories::reviewed_quantity(&reversed, "positive-boundary"),
        None
    );
    let zero_state = all
        .iter()
        .find(|r| r.path == "mycelium_router/terminal_kv_retention.py" && r.line == 44)
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(zero_state, "zero-boundary"),
        None
    );
}
#[test]
fn every_zero_boundary_binds_exact_source_and_cannot_be_given_an_unrelated_zero_id() {
    let rows = checked_in().unwrap();
    let selected: Vec<_> = rows
        .iter()
        .filter(|r| r.review.starts_with("quantity-category:zero-boundary;"))
        .collect();
    assert!(!selected.is_empty());
    for row in selected {
        assert_eq!(row.value, Some(0));
        assert!(row.semantic.contains("threshold"));
        let mut bad = rows.clone();
        let changed = bad.iter_mut().find(|r| r.key == row.key).unwrap();
        changed.expression.push_str(" + 1");
        assert!(validate(&bad).is_err());
        let other = rows
            .iter()
            .find(|r| {
                r.key != row.key
                    && r.unit == row.unit
                    && r.value == Some(0)
                    && !r.budget.is_empty()
                    && (r.path != row.path || r.line != row.line)
            })
            .unwrap();
        let mut bad = rows.clone();
        bad.iter_mut().find(|r| r.key == row.key).unwrap().budget = other.budget.clone();
        assert!(
            validate(&bad).is_err(),
            "same-unit zero is not interchangeable provenance"
        );
    }
}
