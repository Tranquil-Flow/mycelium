// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/control_origin.py; O:path:mycelium_a13_macos/managed_staging.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};
#[test]
fn imported_threshold_is_the_declared_import_not_an_equal_unit() {
    let all = candidates().unwrap();
    let source = all
        .iter()
        .find(|c| {
            c.path == "mycelium_a13_macos/control_origin.py"
                && c.line == 221
                && c.kind == "comparison"
        })
        .unwrap();
    let target = all
        .iter()
        .find(|c| c.path == "mycelium_a13_macos/managed_control.py" && c.line == 95)
        .unwrap();
    assert!(bindings::accepts(
        "comparison-imported-constant",
        source,
        target
    ));
    let mut bad = source.clone();
    bad.expression.push_str(" + 1");
    assert!(!bindings::accepts(
        "comparison-imported-constant",
        &bad,
        target
    ));
    let wrong = all
        .iter()
        .find(|c| c.path == "mycelium_mobile/pixel_stage.py" && c.line == 32)
        .unwrap();
    assert!(!bindings::accepts(
        "comparison-imported-constant",
        source,
        wrong
    ));
    let rows = checked_in().unwrap();
    assert_eq!(validate(&rows), Ok(()));
}
#[test]
fn required_input_uses_its_rejecting_ceiling_not_an_invented_default() {
    let all = candidates().unwrap();
    let source = all
        .iter()
        .find(|c| c.path == "mycelium_a13_macos/managed_staging.py" && c.line == 99)
        .unwrap();
    let target = all
        .iter()
        .find(|c| c.path == source.path && c.line == 94)
        .unwrap();
    assert!(bindings::accepts(
        "validated-required-ceiling",
        source,
        target
    ));
    assert!(!bindings::accepts("same-scope-parameter", source, target));
    for field in 0..9 {
        let mut bad = target.clone();
        match field {
            0 => bad.key.push('!'),
            1 => bad.path.push('!'),
            2 => bad.line += 1,
            3 => bad.column += 1,
            4 => bad.scope.push('!'),
            5 => bad.symbol.push('!'),
            6 => bad.expression.push('!'),
            7 => bad.kind.push('!'),
            8 => bad.source.push('!'),
            _ => unreachable!(),
        }
        assert!(!bindings::accepts(
            "validated-required-ceiling",
            source,
            &bad
        ));
    }
    let rows = checked_in().unwrap();
    let r = rows.iter().find(|r| r.key == source.key).unwrap();
    assert!(r.value.is_none());
    assert!(r.budget.is_empty());
    assert_eq!(r.disposition, "derived");
}
