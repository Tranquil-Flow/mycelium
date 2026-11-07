// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/route.py; O:path:mycelium_live/local_preparer.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};

#[test]
fn upper_caps_lower_floors_and_relative_offsets_are_not_interchangeable() {
    let all = candidates().unwrap();
    for (rule, path, line, target_line) in [
        ("upper-cap-reference", "mycelium_live/route.py", 3619, 141),
        ("upper-cap-reference", "mycelium_live/route.py", 3694, 102),
        (
            "lower-floor-reference",
            "mycelium_live/local_preparer.py",
            1147,
            41,
        ),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        let t = all
            .iter()
            .find(|r| r.path == path && r.line == target_line)
            .unwrap();
        assert!(bindings::accepts(rule, c, t), "{}", c.key);
        let other = if rule == "upper-cap-reference" {
            "lower-floor-reference"
        } else {
            "upper-cap-reference"
        };
        assert!(!bindings::accepts(other, c, t));
        let mut bad = c.clone();
        bad.expression = if rule == "upper-cap-reference" {
            bad.expression.replacen("min(", "max(", 1)
        } else {
            bad.expression.replacen("max(", "min(", 1)
        };
        assert!(!bindings::accepts(rule, &bad, t));
        bad = c.clone();
        bad.scope = "foreign".into();
        assert!(!bindings::accepts(rule, &bad, t));
        let mut wrong = t.clone();
        wrong.symbol.push_str("_other");
        assert!(!bindings::accepts(rule, c, &wrong));
    }
}

#[test]
fn clamp_reference_relationships_cannot_retarget_a_valid_policy() {
    let rows = checked_in().unwrap();
    for rule in ["upper-cap-reference", "lower-floor-reference"] {
        let selected: Vec<_> = rows
            .iter()
            .filter(|r| r.review.starts_with(&format!("binding-category:{rule};")))
            .collect();
        assert!(!selected.is_empty());
        for c in selected {
            let mut bad = rows.clone();
            bad.iter_mut().find(|b| b.key == c.key).unwrap().relation = "RegistryPoll".into();
            assert!(validate(&bad).is_err(), "{}", c.key);
        }
    }
}
