// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_node/process.py; O:path:mycelium_a13_macos/managed_runtime.py; O:path:mycelium_a13_macos/owner_run.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};
#[test]
fn composed_ceilings_keep_small_caps_and_relative_offsets_exact() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, value) in [
        ("mycelium_node/process.py", 2923, 250),
        ("mycelium_node/process.py", 2963, 50),
        ("mycelium_node/process.py", 3323, 500),
        ("mycelium_a13_macos/managed_runtime.py", 463, 1000),
        ("mycelium_a13_macos/owner_run.py", 379, 60000),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.kind == "assignment")
            .unwrap();
        let b = rows
            .iter()
            .find(|b| b.key == c.key)
            .expect("composed ceiling must be reviewed");
        assert_eq!(b.unit, "milliseconds");
        assert!(b.value.is_none());
        let operand = all
            .iter()
            .find(|t| t.path == path && t.scope == c.scope && t.kind == "ceiling-operand")
            .unwrap();
        let target = rows.iter().find(|b| b.key == operand.key).unwrap();
        assert_eq!(target.value, Some(value));
        assert_eq!(b.relation, target.budget);
        assert!(bindings::accepts("reviewed-composed-ceiling", c, operand));
        for field in 0..9 {
            let mut bad = c.clone();
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
                "reviewed-composed-ceiling",
                &bad,
                operand
            ));
        }
        let mut wrong = operand.clone();
        wrong.expression = "9999".into();
        assert!(!bindings::accepts("reviewed-composed-ceiling", c, &wrong));
        let unrelated = all
            .iter()
            .find(|t| t.kind == "ceiling-operand" && t.key != operand.key)
            .unwrap();
        assert!(!bindings::accepts(
            "reviewed-composed-ceiling",
            c,
            unrelated
        ));
        let unrelated_bound = rows.iter().find(|r| r.key == unrelated.key).unwrap();
        let mut changed = rows.clone();
        let index = changed.iter().position(|r| r.key == b.key).unwrap();
        changed[index].relation = unrelated_bound.budget.clone();
        assert_ne!(changed[index].relation, b.relation);
        assert!(validate(&changed).is_err());
    }
    assert_eq!(validate(&rows), Ok(()));
}

#[test]
fn composed_caps_do_not_classify_an_unrelated_required_wait() {
    let all = candidates().unwrap();
    let wait = all
        .iter()
        .find(|c| {
            c.path == "mycelium_a13_macos/control_pump.py"
                && c.line == 381
                && c.kind == "positional-call"
        })
        .unwrap();
    let cap = all
        .iter()
        .find(|c| c.path == "mycelium_node/process.py" && c.kind == "ceiling-operand")
        .unwrap();
    assert!(!bindings::accepts("reviewed-composed-ceiling", wait, cap));
}
