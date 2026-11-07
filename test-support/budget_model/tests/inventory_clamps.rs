// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/control_session.py; O:path:mycelium_node/process.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};

#[test]
fn literal_minimum_caps_preserve_units_without_fixing_runtime_values() {
    let rows = candidates().unwrap();
    for (path, line, millis) in [
        ("mycelium_a13_macos/control_session.py", 72, 50),
        ("mycelium_node/process.py", 2215, 10),
        ("mycelium_a13_macos/model_resources.py", 568, 3_600_000),
        ("mycelium_a13_macos/controller_staging.py", 50, 300_000),
    ] {
        let c = rows
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        assert_eq!(categories::clamped_allowance(c), Some(millis), "{}", c.key);
    }
    let c = rows
        .iter()
        .find(|r| r.path == "mycelium_a13_macos/control_session.py" && r.line == 72)
        .unwrap();
    for expression in [
        "max(0.05, remaining)",
        "min(remaining, 0.0001)",
        "min(remaining, -1)",
        "min(remaining, 0.05, another)",
        "min(remaining, 0.05",
        "min(remaining, float('inf'))",
        "min(0.05, max(0.1, remaining))",
        "time.time() + min(remaining, 0.05)",
    ] {
        let mut bad = c.clone();
        bad.expression = expression.into();
        assert_eq!(categories::clamped_allowance(&bad), None, "{expression}");
    }
}

#[test]
fn each_clamp_claim_binds_its_source_and_budget_provenance() {
    let rows = checked_in().unwrap();
    let claims: Vec<_> = rows
        .iter()
        .filter(|r| r.review.starts_with("clamp-category:"))
        .collect();
    assert!(!claims.is_empty());
    for row in claims {
        assert_eq!(row.unit, "milliseconds");
        for field in ["budget", "expression", "value"] {
            let mut bad = rows.clone();
            let target = bad.iter_mut().find(|r| r.key == row.key).unwrap();
            match field {
                "budget" => target.budget = "RegistryPoll".into(),
                "expression" => target.expression = "min(0.05, unrelated)".into(),
                "value" => target.value = target.value.map(|v| v + 1),
                _ => unreachable!(),
            }
            assert!(validate(&bad).is_err(), "{} {field}", row.key);
        }
    }
}
