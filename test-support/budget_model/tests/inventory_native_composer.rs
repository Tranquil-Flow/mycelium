// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/worker.py; O:path:mycelium_a13_macos/control_session.py.
use budget_model::inventory::{bindings, candidates, categories, checked_in};
#[test]
fn approved_wall_limit_composer_caps_both_native_tuple_members_without_a_fallback() {
    let all = candidates().unwrap();
    let reviewed = checked_in().unwrap();
    let cap = all
        .iter()
        .find(|c| c.path == "mycelium_a13_macos/worker.py" && c.line == 343)
        .expect("derived cap must be inventoried");
    assert_eq!(
        categories::reviewed_quantity(cap, "seconds-ratio-product"),
        Some(("milliseconds", 168_000))
    );
    let bound = reviewed.iter().find(|r| r.key == cap.key).unwrap();
    let output = all
        .iter()
        .find(|c| c.path == cap.path && c.line == 347 && c.kind == "validated-field")
        .unwrap();
    let unrelated = all
        .iter()
        .find(|c| {
            c.path == "mycelium_a13_macos/control_session.py"
                && c.symbol == "MAX_SESSION_TIMEOUT_SECONDS"
        })
        .unwrap();
    for line in [953, 1018] {
        let row = all
            .iter()
            .find(|c| c.path == cap.path && c.line == line && c.kind == "assignment")
            .unwrap();
        assert!(bindings::accepts("reviewed-helper-field", row, output));
        assert!(!bindings::accepts("reviewed-helper-field", row, unrelated));
        assert!(!categories::accepts("reviewed-nonscalar-slot", row));
        let b = reviewed.iter().find(|b| b.key == row.key).unwrap();
        assert_eq!(b.relation, bound.budget);
        assert_eq!(b.unit, "milliseconds");
    }
    let floor = all
        .iter()
        .find(|c| c.path == cap.path && c.line == 345 && c.kind == "comparison-lower")
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(floor, "range-zero-floor"),
        Some(("milliseconds", 0))
    );
    let guard = all
        .iter()
        .find(|c| c.path == cap.path && c.line == 345 && c.kind == "comparison")
        .unwrap();
    assert_eq!(guard.expression, "0 < bounded <= capped");
    assert!(bindings::accepts("reviewed-helper-field", guard, output));
    let mut bad = guard.clone();
    bad.expression = "0 <= bounded <= capped".into();
    assert!(!bindings::accepts("reviewed-helper-field", &bad, output));
}
