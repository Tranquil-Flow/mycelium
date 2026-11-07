// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/relay.py; O:path:scripts/run_a5_benchmark_gate.py.
use budget_model::inventory::{candidates, categories};
#[test]
fn constant_compositions_are_exact_and_do_not_borrow_equal_number_policies() {
    let all = candidates().unwrap();
    for (path, line, mode, millis) in [
        ("mycelium_router/relay.py", 108, "constant-sum", 4_800_000),
        (
            "scripts/run_a5_benchmark_gate.py",
            124,
            "constant-product",
            140_000,
        ),
    ] {
        let row = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert_eq!(
            categories::reviewed_quantity(row, mode),
            Some(("milliseconds", millis))
        );
        assert_eq!(categories::reviewed_quantity(row, "literal"), None);
        for expression in [
            "STREAM_TIMEOUT_SECONDS * 3",
            "STREAM_TIMEOUT_SECONDS + 2",
            "_REQUEST_ABSOLUTE_DEADLINE_SECONDS - _LIVE_PATH_IDLE_SECONDS",
            "FOREIGN + _LIVE_PATH_IDLE_SECONDS",
        ] {
            let mut bad = row.clone();
            bad.expression = expression.into();
            assert_eq!(categories::reviewed_quantity(&bad, mode), None);
        }
        let mut bad = row.clone();
        bad.scope = "caller_override".into();
        assert_eq!(categories::reviewed_quantity(&bad, mode), None);
    }
}
