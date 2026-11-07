// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/demo_chain.py; O:path:mycelium_a13_macos/execution_admission.py.
use budget_model::inventory::{Candidate, candidates, categories};
fn at(path: &str, line: usize, symbol: &str) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.line == line && r.symbol == symbol)
        .unwrap()
}
#[test]
fn dictionary_fallback_is_separate_from_owner_override_and_policy_record() {
    let row = at("mycelium_a13_macos/demo_chain.py", 325, "silence");
    assert_eq!(
        categories::reviewed_quantity(&row, "get-default"),
        Some(("milliseconds", 30_000))
    );
    for expression in [
        "self.budgets.get('report_silence', 300.0)",
        "foreign.get('report_silence', 30.0)",
        "self.budgets.get('other', 30.0)",
    ] {
        let mut bad = row.clone();
        bad.expression = expression.into();
        assert_eq!(categories::reviewed_quantity(&bad, "get-default"), None);
    }
    let record = at("mycelium_a13_macos/demo_chain.py", 259, "self.budgets");
    assert!(!categories::accepts("reviewed-nonscalar-slot", &record));
}
#[test]
fn explicit_zero_default_is_not_absent_unknown_or_a_generic_zero_wait() {
    for (path, line, symbol, unit) in [
        (
            "mycelium_a13_macos/adaptive_host.py",
            68,
            "reserve_bytes",
            "bytes",
        ),
        (
            "mycelium_a13_macos/execution_admission.py",
            12,
            "max_execution_download_bytes",
            "bytes",
        ),
        (
            "mycelium_demo/gateway_control_runtime.py",
            209,
            "token_delay_seconds",
            "milliseconds",
        ),
    ] {
        let row = at(path, line, symbol);
        assert_eq!(
            categories::reviewed_quantity(&row, "explicit-zero-default"),
            Some((unit, 0))
        );
        assert_eq!(categories::reviewed_quantity(&row, "literal"), None);
        assert!(!categories::accepts("reviewed-absent-quantity", &row));
        let mut bad = row.clone();
        bad.expression = "None".into();
        assert_eq!(
            categories::reviewed_quantity(&bad, "explicit-zero-default"),
            None
        );
    }
}
