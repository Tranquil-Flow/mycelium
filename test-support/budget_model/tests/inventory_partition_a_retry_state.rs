// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/managed_control.py; O:path:mycelium_a13_macos/managed_runtime.py; O:path:mycelium_a13_macos/demo_target.py.
use budget_model::inventory::{checked_in, validate};

#[test]
fn retry_phase_edges_keep_their_own_caps_and_counter_unit() {
    let rows = checked_in().unwrap();
    for (path, line, symbol, expected, unit) in [
        (
            "mycelium_a13_macos/managed_runtime.py",
            469,
            "self._stop.wait",
            "PyFinalMyceliumA13MacosManagedRuntimeL463C53CeilingOperand",
            "milliseconds",
        ),
        (
            "mycelium_a13_macos/managed_control.py",
            2231,
            "time.sleep",
            "PyMyceliumA13MacosManagedControlHttpRetryMaxSecondsL318",
            "milliseconds",
        ),
        (
            "mycelium_a13_macos/managed_control.py",
            2236,
            "wake.wait",
            "PyMyceliumA13MacosManagedControlHttpRetryMaxSecondsL318",
            "milliseconds",
        ),
        (
            "mycelium_a13_macos/managed_control.py",
            2208,
            "deadline",
            "PyMyceliumA13MacosManagedControlHttpRetryWindowSecondsL316",
            "milliseconds",
        ),
        (
            "mycelium_a13_macos/managed_control.py",
            2226,
            "attempt + 1",
            "PyMyceliumA13MacosManagedControlHttpRetryAttemptsL315",
            "entries",
        ),
        (
            "mycelium_a13_macos/demo_target.py",
            591,
            "self._retry_delay",
            "PyMyceliumA13MacosDemoTargetRetryMaxSecondsL170",
            "milliseconds",
        ),
        (
            "mycelium_a13_macos/demo_target.py",
            593,
            "self._retry_at",
            "PyMyceliumA13MacosDemoTargetRetryMaxSecondsL170",
            "milliseconds",
        ),
    ] {
        let row = rows
            .iter()
            .find(|r| r.path == path && r.line == line && r.symbol == symbol)
            .unwrap_or_else(|| panic!("missing reviewed retry edge {path}:{line}:{symbol}"));
        assert_eq!(row.relation, expected);
        assert_eq!(row.unit, unit);
        assert_eq!(row.value, None);
        assert!(row.budget.is_empty());
        assert_eq!(row.disposition, "derived");
        assert!(
            row.review
                .starts_with("binding-category:reviewed-retry-state;")
        );
        let index = rows.iter().position(|r| r.key == row.key).unwrap();
        for change_expression in [false, true] {
            let mut bad = rows.clone();
            if change_expression {
                bad[index].expression.push_str(" + unreviewed_override");
            } else {
                bad[index].relation = if unit == "entries" {
                    "PyFinalMyceliumA13MacosClockOffsetL21C0Assignment"
                } else {
                    "RouterClose"
                }
                .into();
                assert_ne!(bad[index].relation, row.relation);
            }
            assert!(
                validate(&bad).is_err(),
                "must retain retry operand provenance: {}",
                row.key
            );
        }
    }
    assert_eq!(validate(&rows), Ok(()));
}

#[test]
fn retry_witnesses_cover_only_the_exact_accepted_rows() {
    use std::collections::BTreeSet;
    let rows = checked_in().unwrap();
    let mut witnessed = BTreeSet::new();
    for line in include_str!("../data/python-retry-state-edges.tsv")
        .lines()
        .skip(1)
    {
        let fields: Vec<_> = line.split('\t').collect();
        assert_eq!(fields.len(), 20);
        assert!(witnessed.insert(fields[0]));
        assert!(!fields[19].is_empty());
    }
    let accepted: BTreeSet<_> = rows
        .iter()
        .filter(|r| {
            r.review
                .starts_with("binding-category:reviewed-retry-state;")
        })
        .map(|r| r.key.as_str())
        .collect();
    assert_eq!(accepted, witnessed);
    let candidates = budget_model::inventory::candidates().unwrap();
    let target = candidates
        .iter()
        .find(|c| c.path == "mycelium_a13_macos/managed_control.py" && c.line == 318)
        .unwrap();
    for (path, line) in [
        ("mycelium_a13_macos/worker.py", 540),
        ("mycelium_a13_macos/control_pump.py", 381),
        ("mycelium_a13_macos/demo_chain.py", 1249),
    ] {
        let source = candidates
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(
            !budget_model::inventory::bindings::accepts("reviewed-retry-state", source, target),
            "required/unchecked inputs cannot inherit the retry caps: {path}:{line}"
        );
    }
}
