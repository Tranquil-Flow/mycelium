// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_speculative_decode.py; O:path:mycelium_a13_macos/owner_run.py.
use budget_model::inventory::{candidates, categories, checked_in};
#[test]
fn inactive_policy_state_is_not_permission_for_an_unbounded_wait() {
    let all = candidates().unwrap();
    for (path, line, symbol) in [
        (
            "mycelium_speculative_decode.py",
            323,
            "admitted_deadline_ms",
        ),
        ("mycelium_a13_macos/owner_run.py", 360, "retry_at"),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.symbol == symbol)
            .unwrap();
        assert!(categories::accepts("reviewed-inactive-state", c));
    }
    for (path, line, symbol) in [
        ("mycelium_request_gateway/client.py", 114, "timeout"),
        ("mycelium_a13_macos/worker.py", 643, "timeout_seconds"),
        ("mycelium_router/transports/iroh.py", 1912, "windows"),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.symbol == symbol)
            .unwrap();
        assert!(!categories::accepts("reviewed-inactive-state", c));
    }
}
#[test]
fn inactive_states_require_exact_source_identity_and_empty_scalar() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let mut count = 0;
    for r in rows
        .iter()
        .filter(|r| r.review.starts_with("category:reviewed-inactive-state;"))
    {
        count += 1;
        assert!(r.value.is_none());
        assert!(r.budget.is_empty());
        let c = all.iter().find(|c| c.key == r.key).unwrap();
        assert!(categories::accepts("reviewed-inactive-state", c));
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
            assert!(!categories::accepts("reviewed-inactive-state", &bad));
        }
    }
    assert!(count > 0);
}
