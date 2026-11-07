// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/relay.py; O:path:mycelium_router/scheduler.py.
use budget_model::inventory::{Candidate, bindings, candidates};
fn at(path: &str, line: usize, symbol: &str) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.line == line && r.symbol == symbol)
        .unwrap()
}
#[test]
fn retention_clamps_require_reviewed_receiver_default_and_exact_zero_floor() {
    let target = at(
        "mycelium_router/contracts.py",
        157,
        "idempotency_retention_seconds",
    );
    for (path, line) in [
        ("mycelium_router/relay.py", 3558),
        ("mycelium_router/scheduler.py", 283),
    ] {
        let source = at(path, line, "retention");
        assert!(bindings::accepts("reviewed-config-clamp", &source, &target));
        for expression in [
            "max(0.0, other.idempotency_retention_seconds)",
            "min(0.0, self.config.idempotency_retention_seconds)",
            "max(1.0, self.config.idempotency_retention_seconds)",
        ] {
            let mut bad = source.clone();
            bad.expression = expression.into();
            assert!(!bindings::accepts("reviewed-config-clamp", &bad, &target));
        }
        let mut wrong = target.clone();
        wrong.scope = "OtherConfig".into();
        assert!(!bindings::accepts("reviewed-config-clamp", &source, &wrong));
    }
}
#[test]
fn expiry_guard_requires_exact_local_threshold_and_preserves_strictness() {
    for (path, guard, declaration, symbol) in [
        ("mycelium_router/relay.py", 3565, 3558, "now - created_at"),
        ("mycelium_router/scheduler.py", 287, 283, "now - seen_at"),
    ] {
        let source = at(path, guard, symbol);
        let target = at(path, declaration, "retention");
        assert!(bindings::accepts(
            "reviewed-local-comparison",
            &source,
            &target
        ));
        for expression in [
            "now - seen_at >= retention",
            "now - created_at < retention",
            "now - created_at > unrelated",
        ] {
            let mut bad = source.clone();
            bad.expression = expression.into();
            assert!(!bindings::accepts(
                "reviewed-local-comparison",
                &bad,
                &target
            ));
        }
        let mut bad = target.clone();
        bad.scope = "Other.method".into();
        assert!(!bindings::accepts(
            "reviewed-local-comparison",
            &source,
            &bad
        ));
    }
}
#[test]
fn terminal_kv_absolute_expiry_is_not_a_fixed_duration_or_disabled_default() {
    let source = at(
        "mycelium_router/terminal_kv_retention.py",
        106,
        "float(now)",
    );
    for target in [
        at(
            "mycelium_router/terminal_kv_retention.py",
            44,
            "self.seconds",
        ),
        at(
            "mycelium_router/terminal_kv_retention.py",
            36,
            "DEFAULT_TERMINAL_KV_RETENTION_SECONDS",
        ),
    ] {
        assert!(!bindings::accepts(
            "reviewed-local-comparison",
            &source,
            &target
        ));
        assert!(!bindings::accepts(
            "reviewed-config-clamp",
            &source,
            &target
        ));
    }
}
