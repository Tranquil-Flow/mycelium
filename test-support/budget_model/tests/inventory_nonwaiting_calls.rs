// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/__main__.py; O:path:mycelium_physical_runner/lock.py.
use budget_model::inventory::{candidates, categories, checked_in};
#[test]
fn named_poll_or_acquire_is_not_automatically_an_unbounded_wait() {
    let all = candidates().unwrap();
    for (path, line) in [
        ("mycelium_a13_macos/__main__.py", 195),
        ("mycelium_physical_runner/lock.py", 99),
        ("mycelium_physical_runner/runner.py", 84),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.kind == "unbounded-call")
            .unwrap();
        assert!(categories::accepts("reviewed-nonwaiting-operation", c));
    }
    for (path, line) in [
        ("mycelium_a13_macos/worker.py", 790),
        ("mycelium_live/route.py", 3790),
        ("mycelium_seed/http.py", 420),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.kind == "unbounded-call")
            .unwrap();
        assert!(!categories::accepts("reviewed-nonwaiting-operation", c));
    }
}
#[test]
fn nonwaiting_review_does_not_claim_zero_duration_or_port_completion() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let mut count = 0;
    for r in rows.iter().filter(|r| {
        r.review
            .starts_with("category:reviewed-nonwaiting-operation;")
    }) {
        count += 1;
        assert!(r.value.is_none());
        assert!(r.budget.is_empty());
        let c = all.iter().find(|c| c.key == r.key).unwrap();
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
            assert!(!categories::accepts("reviewed-nonwaiting-operation", &bad));
        }
    }
    assert_eq!(count, 3);
}
