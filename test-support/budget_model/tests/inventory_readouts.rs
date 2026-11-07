// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:physical_inference_node.py; O:path:mycelium_a13_macos/public_relay.py.
use budget_model::inventory::{candidates, categories, checked_in};
#[test]
fn readout_does_not_classify_policy_writers_or_waits() {
    let all = candidates().unwrap();
    for (path, line, symbol) in [
        (
            "mycelium_speculative_decode_execution.py",
            521,
            "transfer_ms",
        ),
        ("mycelium_router/rtt_measurement.py", 196, "fallback_ms"),
        ("mycelium_layer_planner/network_cost.py", 68, "total_ms"),
        ("physical_inference_node.py", 715, "deadline_ms"),
        (
            "mycelium_a13_macos/public_relay.py",
            429,
            "renewal_cap_seconds",
        ),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        assert!(categories::accepts("reviewed-policy-readout", c));
    }
    for (path, line, symbol) in [
        (
            "mycelium_speculative_decode_execution.py",
            521,
            "maximum_bytes",
        ),
        (
            "mycelium_speculative_decode_execution.py",
            523,
            "self._maximum_bytes",
        ),
        ("mycelium_live/route.py", 213, "window_ms"),
        (
            "scripts/run_a4_physical_idle_staleness_gate.py",
            360,
            "stale_at_quarantine_ms",
        ),
        ("mycelium_a13_macos/app.py", 713, "max_seconds"),
        ("mycelium_router/terminal_kv_retention.py", 79, "deadline"),
        ("mycelium_a13_macos/worker.py", 643, "timeout_seconds"),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        assert!(!categories::accepts("reviewed-policy-readout", c));
    }
}
#[test]
fn snapshot_and_digest_copies_do_not_exempt_active_policy_or_grants() {
    let all = candidates().unwrap();
    for (path, line, symbol) in [
        (
            "mycelium_product_spine/projector.py",
            887,
            "cancellation_cleanup_bound_ms",
        ),
        (
            "mycelium_a13_macos/managed_provisioning.py",
            359,
            "max_wall_seconds",
        ),
        ("mycelium_m16_runtime.py", 326, "reserved_memory_bytes"),
        ("mycelium_router/mlx_runtime.py", 1518, "lease_expires_at"),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        assert!(categories::accepts("reviewed-policy-readout", c));
    }
    for (path, line, symbol) in [
        ("mycelium_a13_macos/demo_chain.py", 259, "self.budgets"),
        ("mycelium_a13_macos/demo_chain.py", 193, "expires_at"),
        ("mycelium_gossip/schema.py", 453, "ttl"),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        assert!(!categories::accepts("reviewed-policy-readout", c));
    }
}
#[test]
fn readout_witnesses_bind_all_source_fields_and_claim_no_scalar() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let mut count = 0;
    for r in rows
        .iter()
        .filter(|r| r.review.starts_with("category:reviewed-policy-readout;"))
    {
        count += 1;
        assert!(r.value.is_none());
        assert!(r.budget.is_empty());
        let c = all.iter().find(|c| c.key == r.key).unwrap();
        assert!(categories::accepts("reviewed-policy-readout", c));
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
            assert!(!categories::accepts("reviewed-policy-readout", &bad));
        }
    }
    assert!(count > 0);
}
