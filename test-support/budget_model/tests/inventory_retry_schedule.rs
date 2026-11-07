// SPDX-License-Identifier: AGPL-3.0-only
// Obligation: O:path:mycelium_host_platform/_win32.py.
use budget_model::inventory::{bindings, candidates, categories, checked_in, validate};
#[test]
fn bounded_index_selects_a_schedule_member_not_an_operation_duration() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let p = all
        .iter()
        .find(|c| {
            c.path == "mycelium_host_platform/_win32.py" && c.line == 40 && c.kind == "assignment"
        })
        .unwrap();
    assert!(categories::accepts("reviewed-nonscalar-slot", p));
    for (index, ms) in [5, 10, 20, 50, 100, 200].into_iter().enumerate() {
        let s = all
            .iter()
            .find(|c| c.path == p.path && c.symbol == format!("REPLACE_BACKOFF_SECONDS[{index}]"))
            .unwrap();
        assert!(!categories::accepts("reviewed-nonscalar-slot", s));
        let r = rows.iter().find(|r| r.key == s.key).unwrap();
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(r.value, Some(ms));
    }
    let source = all
        .iter()
        .find(|c| c.path == p.path && c.line == 497)
        .unwrap();
    let maximum = all
        .iter()
        .find(|c| c.path == p.path && c.symbol == "REPLACE_BACKOFF_SECONDS[5]")
        .unwrap();
    assert!(bindings::accepts("schedule-maximum", source, maximum));
    let wrong = all
        .iter()
        .find(|c| c.path == p.path && c.symbol == "REPLACE_BACKOFF_SECONDS[0]")
        .unwrap();
    assert!(!bindings::accepts("schedule-maximum", source, wrong));
    for expression in [
        source.expression.replace("min(", "max("),
        source.expression.replace(" - 1", " + 1"),
        source.expression.replace("attempt", "other_index"),
    ] {
        let mut bad = source.clone();
        bad.expression = expression;
        assert!(!bindings::accepts("schedule-maximum", &bad, maximum));
    }
    let r = rows.iter().find(|r| r.key == source.key).unwrap();
    assert!(r.value.is_none());
    let attempts = rows
        .iter()
        .find(|r| r.path == p.path && r.symbol == "REPLACE_ATTEMPTS")
        .unwrap();
    assert_eq!(attempts.unit, "entries");
    assert_eq!(attempts.value, Some(12));
    assert_eq!(validate(&rows), Ok(()));
}
