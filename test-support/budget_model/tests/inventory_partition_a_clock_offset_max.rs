// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_m19_recovery.py.
//! A round 4: `max(retained_state, now + <integer literal>)` is a relative allowance, not a ceiling.
use budget_model::inventory::{candidates, categories, checked_in, validate};

const MODE: &str = "maximum-clock-offset";
const PATH: &str = "mycelium_m19_recovery.py";

#[test]
fn breaker_open_offset_maps_only_its_relative_allowance() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let c = all
        .iter()
        .find(|c| c.path == PATH && c.line == 354 && c.kind == "assignment")
        .expect("breaker open-until assignment");
    assert_eq!(c.symbol, "self._breaker_open_until_unix_ms");
    assert_eq!(
        c.expression,
        "max(self._breaker_open_until_unix_ms, now + 30000)"
    );
    assert_eq!(
        categories::reviewed_quantity(c, MODE),
        Some(("milliseconds", 30_000))
    );
    let row = rows.iter().find(|r| r.key == c.key).expect("reviewed row");
    assert_eq!(
        (row.unit.as_str(), row.value),
        ("milliseconds", Some(30_000))
    );
    assert!(
        row.review
            .starts_with("quantity-category:maximum-clock-offset;")
    );
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
        assert_ne!(bad, *c);
        assert_eq!(
            categories::reviewed_quantity(&bad, MODE),
            None,
            "field {field}"
        );
    }
    // Same-unit wrong ID: another millisecond budget must not satisfy the site provenance.
    let wrong = rows
        .iter()
        .find(|r| !r.budget.is_empty() && r.unit == "milliseconds" && r.budget != row.budget)
        .unwrap();
    let mut bad = rows.clone();
    bad.iter_mut()
        .find(|r| r.key == c.key)
        .unwrap()
        .budget
        .clone_from(&wrong.budget);
    assert!(validate(&bad).is_err());
    // Wrong unit and a count claim are refused.
    for unit in ["bytes", "entries"] {
        let mut bad = rows.clone();
        let r = bad.iter_mut().find(|r| r.key == c.key).unwrap();
        r.unit = unit.into();
        assert!(validate(&bad).is_err(), "{unit}");
    }
    // Other time-offset families do not accept this expression, and it accepts none of theirs.
    for other in [
        "minimum-clock-offset",
        "clock-offset",
        "clock-integer-offset",
        "literal-floor",
    ] {
        assert_eq!(categories::reviewed_quantity(c, other), None, "{other}");
    }
}

#[test]
fn maximum_offset_family_does_not_discharge_restored_state_or_unrelated_waits() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let mut accepted = 0;
    for c in &all {
        if categories::reviewed_quantity(c, MODE).is_some() {
            accepted += 1;
            assert_eq!((c.path.as_str(), c.line), (PATH, 354), "{}", c.key);
        }
    }
    assert_eq!(accepted, 1, "exactly the one reviewed site");
    // The persisted/restored open-until (:342; validated only as an integer, so no ceiling) and
    // the elapsed-time reading (:140) are separate policies and stay unreviewed here.
    for (line, kind) in [(342, "assignment"), (140, "assignment")] {
        let c = all
            .iter()
            .find(|c| c.path == PATH && c.line == line && c.kind == kind)
            .unwrap_or_else(|| panic!("{line}"));
        assert_eq!(categories::reviewed_quantity(c, MODE), None, "{line}");
        assert!(
            !rows.iter().any(|r| r.key == c.key),
            "{line} must stay pending"
        );
    }
    // Min-style ceilings and unrelated waits never take this arm.
    for c in all.iter().filter(|c| {
        c.expression.starts_with("min(now + ")
            || c.symbol == "time.sleep"
            || c.symbol == "thread.join"
    }) {
        assert_eq!(categories::reviewed_quantity(c, MODE), None, "{}", c.key);
    }
}
