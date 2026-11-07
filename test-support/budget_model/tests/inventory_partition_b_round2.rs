// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/transports/iroh.py.
use budget_model::inventory::{bindings, candidates, categories, checked_in};
use std::collections::BTreeMap;

#[test]
fn b_stall_reports_and_terminal_reuse_flags_do_not_waive_numeric_enforcement() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, lines, category) in [
        (
            "mycelium_router/transports/iroh.py",
            &[1956, 1964, 2131, 2148][..],
            "reviewed-observation-slot",
        ),
        (
            "mycelium_router/mlx_runtime.py",
            &[1792][..],
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_router/numpy_runtime.py",
            &[964][..],
            "reviewed-nonscalar-slot",
        ),
    ] {
        for line in lines {
            let c = all
                .iter()
                .find(|c| c.path == path && c.line == *line)
                .unwrap();
            assert!(categories::accepts(category, c), "{}", c.key);
            let b = rows
                .iter()
                .find(|b| b.key == c.key)
                .expect("reviewed exact slot");
            assert_eq!(b.disposition, "not-a-bound");
            assert!(b.value.is_none());
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
                assert!(!categories::accepts(category, &bad));
            }
        }
    }
    for (path, line) in [
        ("mycelium_router/transports/iroh.py", 1917),
        ("mycelium_router/transports/iroh.py", 2115),
        ("mycelium_router/mlx_runtime.py", 1788),
        ("mycelium_router/numpy_runtime.py", 960),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(!categories::accepts("reviewed-observation-slot", c));
        assert!(!categories::accepts("reviewed-nonscalar-slot", c));
    }
}

#[test]
fn b_iroh_timer_and_reconnect_keep_the_existing_tightened_allowance() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (source, target, rule) in [
        (
            "mycelium_router/transports/iroh.py:1710:27:positional-call:threading.Timer",
            "mycelium_router/transports/iroh.py:1627:8:assignment:deadline",
            "remaining-monotonic-local",
        ),
        (
            "mycelium_router/transports/iroh.py:2219:20:keyword:deadline",
            "mycelium_router/transports/iroh.py:2200:8:assignment:timeout",
            "upper-cap-reference",
        ),
    ] {
        let b = rows
            .iter()
            .find(|b| b.key == source)
            .expect("reviewed derived edge");
        assert!(bindings::accepts(rule, &all[source], &all[target]));
        assert!(bindings::validate(b, &rows, &all));
        assert_eq!(b.disposition, "derived");
        assert!(b.value.is_none());
        assert_eq!(b.unit, "milliseconds");
        let mut bad = b.clone();
        bad.relation = "CancelAndCleanup".into();
        assert_ne!(bad.relation, b.relation);
        assert!(!bindings::validate(&bad, &rows, &all));
        let mut expression = all[source].clone();
        expression.expression.push_str(" + 1");
        assert!(!bindings::accepts(rule, &expression, &all[target]));
    }
}
