// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:physical_inference_node.py; O:path:mycelium_a13_macos/session_factory.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};
#[test]
fn imported_cap_uses_exact_import_and_is_not_a_floor() {
    let all = candidates().unwrap();
    let s = all
        .iter()
        .find(|c| {
            c.path == "mycelium_a13_macos/session_factory.py"
                && c.line == 44
                && c.symbol == "timeout_seconds"
        })
        .unwrap();
    let t = all
        .iter()
        .find(|c| c.path == "mycelium_a13_macos/control_session.py" && c.line == 124)
        .unwrap();
    assert!(bindings::accepts("imported-upper-cap-reference", s, t));
    assert!(!bindings::accepts("upper-cap-reference", s, t));
    let mut bad = s.clone();
    bad.expression = bad.expression.replacen("min(", "max(", 1);
    assert!(!bindings::accepts("imported-upper-cap-reference", &bad, t));
    let wrong = all
        .iter()
        .find(|c| c.path == "physical_inference_node.py" && c.line == 110)
        .unwrap();
    assert!(!bindings::accepts("imported-upper-cap-reference", s, wrong));
}
#[test]
fn outer_monotonic_clock_caps_only_the_relative_allowance() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let s = all
        .iter()
        .find(|c| c.path == "physical_inference_node.py" && c.line == 5316)
        .unwrap();
    let t = all
        .iter()
        .find(|c| c.path == s.path && c.line == 110)
        .unwrap();
    assert!(bindings::accepts("monotonic-upper-cap-reference", s, t));
    for expression in [
        s.expression.replace("time.monotonic()", "time.time()"),
        s.expression.replace("min(", "max("),
        s.expression.replace("0.8", "1.8"),
    ] {
        let mut bad = s.clone();
        bad.expression = expression;
        assert!(!bindings::accepts("monotonic-upper-cap-reference", &bad, t));
    }
    let r = rows.iter().find(|r| r.key == s.key).unwrap();
    assert!(r.value.is_none());
    assert_eq!(r.unit, "milliseconds");
    assert_eq!(validate(&rows), Ok(()));
}
