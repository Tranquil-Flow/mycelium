// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/managed_staging.py; O:path:mycelium_live/a4_contracts.py; O:path:mycelium_gossip/schema.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};
#[test]
fn compound_thresholds_keep_exact_units_and_full_predicates() {
    let all = candidates().unwrap();
    let chunk = all
        .iter()
        .find(|c| c.path == "mycelium_a13_macos/peer_model_source.py" && c.line == 99)
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(chunk, "integer-expression"),
        Some(("bytes", 2_097_152))
    );
    for (path, line, unit, value) in [
        (
            "mycelium_a13_macos/managed_staging.py",
            94,
            "bytes",
            67_108_864,
        ),
        ("mycelium_live/a4_contracts.py", 127, "bytes", 1_073_741_824),
        (
            "mycelium_gossip/schema.py",
            453,
            "milliseconds",
            604_800_000,
        ),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.kind == "comparison")
            .unwrap();
        assert_eq!(
            categories::reviewed_quantity(c, "upper-integer-expression"),
            Some((unit, value))
        );
        for expression in [
            format!("{} + 1", c.expression),
            c.expression.replace(" > ", " < "),
            c.expression.replace("1024", "1000"),
        ] {
            if expression == c.expression {
                continue;
            }
            let mut bad = c.clone();
            bad.expression = expression;
            assert_eq!(
                categories::reviewed_quantity(&bad, "upper-integer-expression"),
                None
            );
        }
    }
    assert_eq!(validate(&checked_in().unwrap()), Ok(()));
}
#[test]
fn zero_range_floor_is_separate_from_upper_bound_and_not_a_wait() {
    let all = candidates().unwrap();
    let floor = all
        .iter()
        .find(|c| {
            c.path == "mycelium_a13_macos/authority.py"
                && c.line == 148
                && c.kind == "comparison-lower"
        })
        .expect("independent zero lower operand");
    assert_eq!(
        categories::reviewed_quantity(floor, "range-zero-floor"),
        Some(("bytes", 0))
    );
    assert_eq!(categories::reviewed_quantity(floor, "range-floor"), None);
    let mut bad = floor.clone();
    bad.kind = "positional-timeout".into();
    assert_eq!(
        categories::reviewed_quantity(&bad, "range-zero-floor"),
        None
    );
    let upper = all
        .iter()
        .find(|c| c.path == floor.path && c.line == floor.line && c.kind == "comparison")
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(upper, "range-zero-floor"),
        None
    );
    assert_eq!(
        categories::reviewed_quantity(upper, "upper-integer-expression"),
        Some(("bytes", 4_194_304))
    );
}
