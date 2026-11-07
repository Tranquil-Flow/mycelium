// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_node/__main__.py; O:path:mycelium_demo/public_origin_proxy.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};

#[test]
fn cli_defaults_bind_exact_namespace_slots_not_arbitrary_attributes() {
    let rows = candidates().unwrap();
    let source = rows
        .iter()
        .find(|c| c.path == "mycelium_node/__main__.py" && c.line == 630)
        .unwrap();
    let target = rows
        .iter()
        .find(|c| {
            c.path == source.path && c.kind == "cli-default" && c.symbol == "--heartbeat-interval"
        })
        .unwrap();
    assert!(bindings::accepts("argparse-option", source, target));
    for field in ["expression", "scope", "path", "key"] {
        let mut bad = source.clone();
        match field {
            "expression" => bad.expression = "other.heartbeat_interval".into(),
            "scope" => bad.scope = "unreviewed".into(),
            "path" => bad.path = "other.py".into(),
            "key" => bad.key.push_str("-other"),
            _ => unreachable!(),
        }
        assert!(
            !bindings::accepts("argparse-option", &bad, target),
            "{field}"
        );
    }
    let mut bad = target.clone();
    bad.symbol = "--another-interval".into();
    assert!(!bindings::accepts("argparse-option", source, &bad));
    bad = target.clone();
    bad.kind = "assignment".into();
    assert!(!bindings::accepts("argparse-option", source, &bad));
}

#[test]
fn caller_override_is_dynamic_and_unrelated_valid_budget_is_rejected() {
    let rows = checked_in().unwrap();
    let claims: Vec<_> = rows
        .iter()
        .filter(|r| r.review.starts_with("binding-category:argparse-option;"))
        .collect();
    assert!(!claims.is_empty());
    for row in claims {
        assert_eq!(row.disposition, "derived");
        assert!(row.value.is_none());
        let mut bad = rows.clone();
        bad.iter_mut().find(|r| r.key == row.key).unwrap().relation = "RegistryPoll".into();
        assert!(validate(&bad).is_err(), "{}", row.key);
    }
}
