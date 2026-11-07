// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/pairing.py; O:path:mycelium_live/route.py.
use budget_model::inventory::{candidates, categories, checked_in};
#[test]
fn exact_nonscalar_slots_do_not_waive_enforcement_or_waits() {
    let rows = candidates().unwrap();
    for (path, line, symbol) in [
        ("mycelium_a13_macos/pairing.py", 680, "window"),
        ("mycelium_node/process.py", 990, "lease"),
        ("mycelium_a13_macos/chat.py", 213, "sleep"),
        (
            "mycelium_a13_macos/standing_consent.py",
            279,
            "max_rss_bytes",
        ),
        (
            "mycelium_a13_macos/concurrent_pump.py",
            121,
            "future.result",
        ),
        ("scripts/bench_stage_decode.py", 140, "window"),
        (
            "mycelium_live/router_port.py",
            501,
            "self._retained_source_failures",
        ),
        ("mycelium_a13_macos/pairing.py", 643, "window.handoff"),
        ("numpy_runtime.py", 1491, "window"),
        ("mycelium_deadlines.py", 1093, "DEADLINES_BY_NAME"),
        ("mycelium_a13_macos/worker.py", 751, "self._deadline_timer"),
        ("mycelium_speculative_decode_execution.py", 1344, "deadline"),
        ("mycelium_a6_product_fault_seams.py", 464, "self.wait"),
        ("scripts/run_a5_product_gate.py", 409, "wait_for"),
    ] {
        let c = rows
            .iter()
            .find(|r| r.path == path && r.line == line && r.symbol == symbol)
            .unwrap();
        assert!(
            categories::accepts("reviewed-nonscalar-slot", c),
            "{}",
            c.key
        );
    }
    for c in rows.iter().filter(|r| {
        (r.path == "mycelium_a13_macos/pairing.py" && r.symbol == "expires_at")
            || r.symbol == "stalled_windows"
            || (r.path == "mycelium_live/route.py" && r.line == 3790)
            || (r.path == "mycelium_a13_macos/worker.py" && r.line == 790)
    }) {
        assert!(
            !categories::accepts("reviewed-nonscalar-slot", c),
            "{}",
            c.key
        );
    }
}
#[test]
fn observation_slots_cannot_hide_consumption_or_deadline_caps() {
    let all = candidates().unwrap();
    for b in checked_in()
        .unwrap()
        .iter()
        .filter(|b| b.review.starts_with("category:reviewed-observation-slot;"))
    {
        let c = all.iter().find(|c| c.key == b.key).unwrap();
        assert!(categories::accepts("reviewed-observation-slot", c));
        let mut bad = c.clone();
        bad.expression = "request_deadline_ms".into();
        assert!(!categories::accepts("reviewed-observation-slot", &bad));
    }
    let observed = all
        .iter()
        .find(|c| c.path == "scripts/pilot_readiness_gate.py" && c.line == 216)
        .unwrap();
    assert!(categories::accepts("reviewed-observation-slot", observed));
    for c in all.iter().filter(|c| {
        c.symbol == "stalled_windows"
            || c.symbol == "deadline_ms"
            || (c.path == "mycelium_speculative_decode_execution.py" && c.line == 1361)
            || (c.path == "scripts/run_a11_route_benchmark.py" && c.line == 505)
    }) {
        assert!(
            !categories::accepts("reviewed-observation-slot", c),
            "{}",
            c.key
        );
    }
}
#[test]
fn retention_handle_review_keeps_every_consuming_guard_visible() {
    let all = candidates().unwrap();
    let reviewed = checked_in().unwrap();
    for (path, line) in [
        ("mycelium_live/router_port.py", 1352),
        ("mycelium_live/router_port.py", 1370),
        ("mycelium_live/router_port.py", 1384),
        ("mycelium_live/router_port.py", 1397),
        ("mycelium_live/router_port.py", 3334),
        ("mycelium_live/supervisor.py", 1061),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.kind == "comparison")
            .expect("consuming guard must be inventoried");
        assert!(!categories::accepts("reviewed-nonscalar-slot", c));
        let b = reviewed
            .iter()
            .find(|b| b.key == c.key)
            .expect("guard must bind its capacity");
        assert_eq!(b.unit, "entries");
        assert!(!b.budget.is_empty() || b.disposition == "derived");
    }
}

#[test]
fn every_nonscalar_claim_is_exact_not_a_name_only_exception() {
    let all = candidates().unwrap();
    let mut count = 0;
    for b in checked_in()
        .unwrap()
        .iter()
        .filter(|b| b.review.starts_with("category:reviewed-nonscalar-slot;"))
    {
        count += 1;
        let c = all.iter().find(|c| c.key == b.key).unwrap();
        assert!(categories::accepts("reviewed-nonscalar-slot", c));
        for field in ["kind", "scope", "path", "expression", "symbol"] {
            let mut changed = c.clone();
            match field {
                "kind" => changed.kind = "unreviewed-kind".into(),
                "scope" => changed.scope = "unreviewed-scope".into(),
                "path" => changed.path = "other.py".into(),
                "expression" => changed.expression = "dynamic_timeout".into(),
                "symbol" => {
                    // A real schema label can itself be named `timeout`.
                    changed.symbol.push_str(".unreviewed");
                    assert_ne!(changed.symbol, c.symbol);
                }
                _ => unreachable!(),
            }
            assert!(
                !categories::accepts("reviewed-nonscalar-slot", &changed),
                "{} {field}",
                c.key
            );
        }
    }
    assert!(count > 0);
}

#[test]
fn literal_retention_ceiling_rejects_changed_guard_and_same_unit_policy() {
    let rows = checked_in().unwrap();
    let row = rows
        .iter()
        .find(|r| r.path == "mycelium_live/supervisor.py" && r.line == 1061)
        .expect("retention guard must have its own capacity");
    assert_eq!(row.value, Some(256));
    for field in ["expression", "budget", "scope"] {
        let mut bad = rows.clone();
        let changed = bad.iter_mut().find(|r| r.key == row.key).unwrap();
        match field {
            "expression" => changed.expression = "len(settled) > 257".into(),
            "budget" => {
                changed.budget =
                    "PyFinalMyceliumLiveRouterPortRetainedSourceFailuresMaxL78C0".into()
            }
            "scope" => changed.scope = "unreviewed".into(),
            _ => unreachable!(),
        }
        assert!(budget_model::inventory::validate(&bad).is_err(), "{field}");
    }
}

#[test]
fn expiry_selection_collections_do_not_exempt_their_nested_enforcement_comparisons() {
    let rows = candidates().unwrap();
    for (path, line) in [
        ("mycelium_router/relay.py", 3562),
        ("mycelium_router/scheduler.py", 284),
        ("mycelium_router/terminal_kv_retention.py", 106),
    ] {
        let c = rows
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == "assignment")
            .unwrap();
        assert!(
            categories::accepts("reviewed-nonscalar-slot", c),
            "{}",
            c.key
        );
        let guards: Vec<_> = rows
            .iter()
            .filter(|r| {
                r.path == path
                    && r.scope == c.scope
                    && r.kind == "comparison"
                    && (r.expression.ends_with(" > retention")
                        || r.expression.ends_with(" >= deadline"))
            })
            .collect();
        assert_eq!(guards.len(), 1);
        assert!(!categories::accepts("reviewed-nonscalar-slot", guards[0]));
        assert!(!categories::accepts("reviewed-observation-slot", guards[0]));
    }
    let policy_record = rows
        .iter()
        .find(|r| r.path == "mycelium_a13_macos/demo_chain.py" && r.line == 259)
        .unwrap();
    assert!(!categories::accepts(
        "reviewed-nonscalar-slot",
        policy_record
    ));
}
