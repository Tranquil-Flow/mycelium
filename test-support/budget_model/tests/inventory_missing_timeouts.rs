// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_iroh_sidecar/client.py; O:path:mycelium_live/route.py.
use budget_model::inventory::{Candidate, candidates, categories};
fn site(path: &str, line: usize) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|c| c.path == path && c.line == line && c.kind == "unbounded-call")
        .unwrap()
}
#[test]
fn absent_local_timeout_is_an_explicit_gap_not_a_nonblocking_or_finite_wait() {
    let row = site("mycelium_iroh_sidecar/client.py", 981);
    assert!(categories::accepts("reviewed-missing-local-timeout", &row));
    assert!(!categories::accepts("reviewed-nonwaiting-operation", &row));
    assert_eq!(categories::numeric_literal(&row), None);
    for expression in ["0", "None if other else 1", "30.0"] {
        let mut bad = row.clone();
        bad.expression = expression.into();
        assert!(!categories::accepts("reviewed-missing-local-timeout", &bad));
    }
    let mut bad = row.clone();
    bad.symbol.push_str(".unreviewed");
    assert!(!categories::accepts("reviewed-missing-local-timeout", &bad));
}
#[test]
fn method_names_do_not_establish_native_wait_semantics() {
    for (path, line) in [
        ("mycelium_seed/http.py", 420),
        ("scripts/run_a12_mobile_physical_gate.py", 526),
    ] {
        let row = site(path, line);
        assert!(!categories::accepts("reviewed-missing-local-timeout", &row));
    }
}
