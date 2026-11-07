// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/worker.py; O:path:mycelium_a13_macos/public_https.py; O:path:mycelium_layer_planner/contracts.py.
use budget_model::inventory::{bindings, candidates, categories};
#[test]
fn nonintegral_dimensionless_factors_cannot_masquerade_as_durations() {
    let cs = candidates().unwrap();
    for (path, line, value) in [
        ("mycelium_a13_macos/worker.py", 309, "0.7"),
        ("mycelium_a13_macos/public_https.py", 46, "0.5"),
        ("mycelium_layer_planner/contracts.py", 219, "0.1"),
    ] {
        let c = cs
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert_eq!(c.expression, value);
        assert_eq!(categories::numeric_literal(c), None);
        assert_eq!(categories::reviewed_quantity(c, "literal"), None);
        for category in [
            "reviewed-policy-readout",
            "reviewed-observation-slot",
            "reviewed-nonscalar-slot",
        ] {
            assert!(!categories::accepts(category, c));
        }
    }
}
#[test]
fn unchecked_native_constructor_cannot_borrow_builder_timeout_validation() {
    let cs = candidates().unwrap();
    let c = cs
        .iter()
        .find(|c| c.path == "mycelium_a13_macos/worker.py" && c.line == 540)
        .unwrap();
    let builder = cs
        .iter()
        .find(|c| {
            c.path == "mycelium_a13_macos/worker.py" && c.line == 347 && c.kind == "validated-field"
        })
        .unwrap();
    assert_eq!(c.scope, "_ConcurrentNativeSession.__init__");
    assert_eq!(c.expression, "timeout_seconds");
    assert!(!bindings::accepts("reviewed-helper-field", c, builder));
    assert!(!categories::accepts("reviewed-nonscalar-slot", c));
}
