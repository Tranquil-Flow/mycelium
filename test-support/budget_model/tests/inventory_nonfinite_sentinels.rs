// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/contracts.py; O:path:mycelium_router/leases.py.
use budget_model::inventory::{candidates, categories};
#[test]
fn infinity_defaults_and_never_renewed_state_are_not_finite_budgets() {
    let all = candidates().unwrap();
    for (path, line, symbol) in [
        ("mycelium_router/contracts.py", 89, "reservation_expires_at"),
        ("mycelium_router/contracts.py", 314, "deadline_at"),
        ("mycelium_router/contracts.py", 318, "lease_expires_at"),
        ("mycelium_router/mlx_runtime.py", 193, "lease_renewed_until"),
        (
            "mycelium_router/numpy_runtime.py",
            177,
            "lease_renewed_until",
        ),
    ] {
        let row = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        assert!(categories::accepts("reviewed-nonfinite-sentinel", row));
        assert_eq!(categories::numeric_literal(row), None);
        assert!(!categories::accepts("reviewed-absent-quantity", row));
        for expression in [
            "None",
            "0",
            "float('nan')",
            "float('1e12')",
            "foreign('inf')",
        ] {
            let mut bad = row.clone();
            bad.expression = expression.into();
            assert!(!categories::accepts("reviewed-nonfinite-sentinel", &bad));
        }
        let mut wrong = row.clone();
        wrong.expression = if row.expression == "float('inf')" {
            "float('-inf')"
        } else {
            "float('inf')"
        }
        .into();
        assert!(!categories::accepts("reviewed-nonfinite-sentinel", &wrong));
    }
}
