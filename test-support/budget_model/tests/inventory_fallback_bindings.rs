// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_node/process.py; O:path:mycelium_iroh_sidecar/client.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};

#[test]
fn validated_values_and_optional_defaults_keep_exact_source_edges() {
    let all = candidates().unwrap();
    for (rule, path, source_line, target_line) in [
        (
            "resolved-optional-value",
            "mycelium_iroh_sidecar/client.py",
            466,
            434,
        ),
        (
            "validated-parameter-default",
            "mycelium_node/process.py",
            1988,
            1964,
        ),
        (
            "validated-parameter-default",
            "mycelium_interactive/swarm.py",
            349,
            312,
        ),
        (
            "optional-parameter-default",
            "mycelium_iroh_sidecar/client.py",
            434,
            173,
        ),
        (
            "optional-parameter-default",
            "mycelium_deadlines.py",
            1198,
            250,
        ),
    ] {
        let source = all
            .iter()
            .find(|r| r.path == path && r.line == source_line)
            .unwrap();
        let target = all
            .iter()
            .find(|r| r.path == path && r.line == target_line)
            .unwrap();
        assert!(bindings::accepts(rule, source, target), "{}", source.key);
        let mut bad = source.clone();
        bad.scope = "foreign".into();
        assert!(!bindings::accepts(rule, &bad, target));
        bad = source.clone();
        bad.expression = "arbitrary_runtime_default".into();
        assert!(!bindings::accepts(rule, &bad, target));
        let mut wrong = target.clone();
        wrong.symbol = "unrelated".into();
        assert!(!bindings::accepts(rule, source, &wrong));
    }
    let unbounded = all
        .iter()
        .find(|r| {
            r.path == "mycelium_iroh_sidecar/client.py" && r.line == 978 && r.kind == "parameter"
        })
        .unwrap();
    let target = all
        .iter()
        .find(|r| r.path == "mycelium_iroh_sidecar/client.py" && r.line == 173)
        .unwrap();
    assert!(!bindings::accepts(
        "optional-parameter-default",
        unbounded,
        target
    ));
}

#[test]
fn fallback_edges_do_not_authorize_other_valid_budgets() {
    let rows = checked_in().unwrap();
    for rule in ["validated-parameter-default", "optional-parameter-default"] {
        let selected: Vec<_> = rows
            .iter()
            .filter(|r| r.review.starts_with(&format!("binding-category:{rule};")))
            .collect();
        assert!(!selected.is_empty());
        for r in selected {
            let mut bad = rows.clone();
            bad.iter_mut().find(|b| b.key == r.key).unwrap().relation = "RegistryPoll".into();
            assert!(validate(&bad).is_err(), "{}", r.key);
        }
    }
}
