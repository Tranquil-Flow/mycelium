// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/acquisition.py; O:path:scripts/finalize_a13_macos.py.
use budget_model::inventory::{Candidate, candidates, categories};
fn at(path: &str, line: usize) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.line == line)
        .unwrap()
}
#[test]
fn signed_integer_range_ceiling_has_exact_units_without_becoming_a_deadline() {
    for (path, line, unit) in [
        ("mycelium_a13_macos/acquisition.py", 85, "bytes"),
        ("mycelium_live/liveness.py", 27, "milliseconds"),
        ("mycelium_live/command_controller.py", 34, "milliseconds"),
    ] {
        let row = at(path, line);
        assert_eq!(
            categories::reviewed_quantity(&row, "signed-i64-ceiling"),
            Some((unit, 9_223_372_036_854_775_807))
        );
        for expression in ["(1 << 64) - 1", "(1 << 63) + 1", "(2 << 63) - 1", "1 << 63"] {
            let mut bad = row.clone();
            bad.expression = expression.into();
            assert_eq!(
                categories::reviewed_quantity(&bad, "signed-i64-ceiling"),
                None
            );
        }
        assert_eq!(categories::numeric_literal(&row), None);
    }
}
#[test]
fn compound_relative_expiry_and_argument_capacity_require_exact_source_witness() {
    let expiry = at("scripts/finalize_a13_macos.py", 192);
    assert_eq!(
        categories::reviewed_quantity(&expiry, "clock-integer-offset"),
        Some(("milliseconds", 7_776_000_000))
    );
    let size = at("scripts/a8_run_physical_gate.py", 730);
    assert_eq!(
        categories::reviewed_quantity(&size, "integer-argument"),
        Some(("bytes", 268_435_456))
    );
    let mut bad = expiry.clone();
    bad.expression = "foreign_clock + 90 * 86400".into();
    assert_eq!(
        categories::reviewed_quantity(&bad, "clock-integer-offset"),
        None
    );
    assert_eq!(
        categories::reviewed_quantity(&size, "clock-integer-offset"),
        None
    );
}
