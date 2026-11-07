// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/chat.py; O:path:mycelium_evidence.py.
use budget_model::inventory::{Candidate, bindings, candidates};
fn at(path: &str, line: usize) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.line == line)
        .unwrap()
}
#[test]
fn reviewed_clock_offsets_bind_relative_allowances_not_absolute_timestamps() {
    for (path, line, target) in [
        ("mycelium_a13_macos/chat.py", 270, 51),
        ("mycelium_evidence.py", 267, 216),
        ("mycelium_a13_macos/pairing.py", 195, 161),
    ] {
        let source = at(path, line);
        let target = at(path, target);
        assert!(bindings::accepts("reviewed-clock-offset", &source, &target));
        for changed in [
            source.expression.replace(" + ", " - "),
            format!("foreign_clock() + {}", target.symbol),
        ] {
            assert_ne!(changed, source.expression);
            let mut bad = source.clone();
            bad.expression = changed;
            assert!(!bindings::accepts("reviewed-clock-offset", &bad, &target));
        }
        let mut bad = target.clone();
        bad.symbol.push_str(".foreign");
        assert!(!bindings::accepts("reviewed-clock-offset", &source, &bad));
    }
}
#[test]
fn duration_sums_and_computed_admission_costs_are_not_clock_offsets() {
    for (path, line, target) in [
        ("mycelium_router/relay.py", 108, 89),
        ("mycelium_router/batching.py", 114, 113),
    ] {
        assert!(!bindings::accepts(
            "reviewed-clock-offset",
            &at(path, line),
            &at(path, target)
        ));
    }
}
