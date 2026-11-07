// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/layer_split.py; O:path:mycelium_iroh_sidecar/client.py.
use budget_model::inventory::{Candidate, candidates, categories, checked_in};
fn at(path: &str, line: usize) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.line == line && r.expression == "None")
        .unwrap()
}
#[test]
fn absent_quantities_preserve_disabled_unknown_and_fallback_semantics() {
    let reviews = checked_in().unwrap();
    for (path, line) in [
        ("mycelium_a13_macos/layer_split.py", 316),
        ("mycelium_a13_macos/token_owner.py", 28),
        ("mycelium_iroh_sidecar/client.py", 978),
        ("mycelium_a13_macos/acquisition.py", 533),
    ] {
        let row = at(path, line);
        assert!(categories::accepts("reviewed-absent-quantity", &row));
        assert_eq!(categories::numeric_literal(&row), None);
        let review = reviews.iter().find(|r| r.key == row.key).unwrap();
        assert!(review.budget.is_empty());
        assert_eq!(review.disposition, "not-a-bound");
        for expression in ["0", "30", "float('inf')", "None if other else 1"] {
            let mut bad = row.clone();
            bad.expression = expression.into();
            assert!(!categories::accepts("reviewed-absent-quantity", &bad));
        }
        let mut bad = row.clone();
        bad.source.push_str(" # altered");
        assert!(!categories::accepts("reviewed-absent-quantity", &bad));
        bad = row.clone();
        bad.scope.push_str(".foreign");
        assert!(!categories::accepts("reviewed-absent-quantity", &bad));
    }
    let primitive = candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == "mycelium_iroh_sidecar/client.py" && r.line == 981)
        .unwrap();
    assert!(!categories::accepts("reviewed-absent-quantity", &primitive));
    assert!(categories::accepts(
        "reviewed-missing-local-timeout",
        &primitive
    ));
}
#[test]
fn similarly_named_domain_actions_are_not_standard_wait_primitives() {
    for (path, line) in [
        ("mycelium_seed/http.py", 420),
        ("scripts/run_a12_mobile_physical_gate.py", 526),
    ] {
        let row = at(path, line);
        assert!(categories::accepts("reviewed-domain-action", &row));
        assert!(!categories::accepts("reviewed-nonwaiting-operation", &row));
        assert!(!categories::accepts("reviewed-missing-local-timeout", &row));
        let mut bad = row.clone();
        bad.symbol = "thread.join".into();
        assert!(!categories::accepts("reviewed-domain-action", &bad));
    }
}
