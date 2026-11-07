// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/router_port.py; O:path:mycelium_swarm_directory/ratelimit.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};

#[test]
fn explicit_units_distinguish_time_windows_counts_and_absolute_clock_results() {
    let rows = candidates().unwrap();
    for (path, line, mode, expected) in [
        (
            "mycelium_swarm_directory/ratelimit.py",
            10,
            "literal",
            ("milliseconds", 60000),
        ),
        (
            "scripts/bench_stage_decode.py",
            170,
            "literal",
            ("entries", 3),
        ),
        (
            "scripts/a8_run_physical_gate.py",
            736,
            "literal",
            ("bytes", 32),
        ),
        (
            "mycelium_a13_macos/run_exchange.py",
            60,
            "literal",
            ("milliseconds", 60000),
        ),
        (
            "mycelium_live/router_port.py",
            847,
            "clock-offset",
            ("milliseconds", 3600000),
        ),
        (
            "mycelium_a13_macos/installation_descriptor.py",
            59,
            "clock-offset",
            ("milliseconds", 300000),
        ),
        (
            "mycelium_internet/activation.py",
            303,
            "clock-offset",
            ("milliseconds", 90000),
        ),
    ] {
        let c = rows
            .iter()
            .find(|r| {
                r.path == path
                    && r.line == line
                    && match path {
                        "mycelium_swarm_directory/ratelimit.py" => r.symbol == "window",
                        "scripts/bench_stage_decode.py" => r.symbol == "--windows",
                        "scripts/a8_run_physical_gate.py" => r.symbol == "maximum_size",
                        "mycelium_a13_macos/run_exchange.py" => r.symbol == "MAX_CLOCK_RTT_SECONDS",
                        "mycelium_live/router_port.py" => r.symbol == "absolute_deadline_ms",
                        "mycelium_a13_macos/installation_descriptor.py" => r.symbol == "expires_at",
                        "mycelium_internet/activation.py" => {
                            r.symbol == "evidence_lifetime_until_unix_ms"
                        }
                        _ => false,
                    }
            })
            .unwrap();
        assert_eq!(
            categories::reviewed_quantity(c, mode),
            Some(expected),
            "{}",
            c.key
        );
        let mut bad = c.clone();
        bad.scope = "unreviewed".into();
        assert_eq!(categories::reviewed_quantity(&bad, mode), None);
        bad = c.clone();
        bad.expression = "now + 0.0001".into();
        assert_eq!(categories::reviewed_quantity(&bad, mode), None);
        assert_eq!(categories::reviewed_quantity(c, "unknown-mode"), None);
    }
    for c in rows.iter().filter(|c| {
        c.symbol == "propagation_km_per_ms" || c.expression == "time.monotonic() + timeout"
    }) {
        assert_eq!(categories::reviewed_quantity(c, "literal"), None);
        assert_eq!(categories::reviewed_quantity(c, "clock-offset"), None);
    }
}

#[test]
fn reviewed_quantities_reject_unrelated_same_unit_policy() {
    let rows = checked_in().unwrap();
    let claims: Vec<_> = rows
        .iter()
        .filter(|r| r.review.starts_with("quantity-category:"))
        .collect();
    assert!(!claims.is_empty());
    let mut mutations = 0;
    for c in claims {
        let id = budgets::BudgetId::from_name(&c.budget).unwrap();
        if let Some(other) = budgets::table::BUDGET_TABLE
            .iter()
            .find(|e| e.value == id.entry().value && e.provenance != id.entry().provenance)
        {
            let mut bad = rows.clone();
            bad.iter_mut().find(|r| r.key == c.key).unwrap().budget = other.id.name().into();
            assert!(validate(&bad).is_err(), "{}", c.key);
            mutations += 1;
        }
    }
    assert!(mutations > 0);
}

#[test]
fn literal_floors_are_not_upper_limits_and_retention_counts_are_not_time() {
    let rows = candidates().unwrap();
    for (path, line, mode, expected) in [
        (
            "mycelium_a7_physical_measurement.py",
            690,
            "literal-floor",
            ("milliseconds", 200),
        ),
        (
            "mycelium_router/transports/iroh.py",
            4979,
            "literal-floor",
            ("milliseconds", 1000),
        ),
        (
            "mycelium_a13_macos/swarm_stats.py",
            105,
            "retention-tail",
            ("entries", 512),
        ),
        (
            "mycelium_live/route.py",
            421,
            "retention-tail",
            ("entries", 64),
        ),
    ] {
        let c = rows
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        assert_eq!(categories::reviewed_quantity(c, mode), Some(expected));
        let mut bad = c.clone();
        bad.expression.push_str(" + extra");
        assert_eq!(categories::reviewed_quantity(&bad, mode), None);
        assert_eq!(categories::reviewed_quantity(c, "literal"), None);
    }
}

#[test]
fn select_payload_and_fourth_timeout_operand_are_distinct_and_zero_is_explicit() {
    let all = candidates().unwrap();
    let timeout = all
        .iter()
        .find(|r| {
            r.path == "mycelium_demo/gateway_control_runtime.py"
                && r.line == 636
                && r.kind == "positional-timeout"
        })
        .unwrap();
    let payload = all
        .iter()
        .find(|r| r.path == timeout.path && r.line == timeout.line && r.kind == "positional-call")
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(timeout, "nonblocking-seconds"),
        Some(("milliseconds", 0))
    );
    assert_eq!(categories::reviewed_quantity(timeout, "literal"), None);
    assert_eq!(
        categories::reviewed_quantity(payload, "nonblocking-seconds"),
        None
    );
    assert!(categories::accepts("reviewed-nonscalar-slot", payload));
    let mut bad = timeout.clone();
    bad.expression = "None".into();
    assert_eq!(
        categories::reviewed_quantity(&bad, "nonblocking-seconds"),
        None
    );
    bad = timeout.clone();
    bad.expression = "1".into();
    assert_eq!(
        categories::reviewed_quantity(&bad, "nonblocking-seconds"),
        None
    );
    let duration = all
        .iter()
        .find(|r| {
            r.path == "scripts/a8_endpoint_mismatch_probe.py"
                && r.line == 85
                && r.kind == "positional-timeout"
        })
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(duration, "literal"),
        Some(("milliseconds", 20000))
    );
}

#[test]
fn comparison_ceilings_and_positive_floors_are_separate_policies() {
    let rows = candidates().unwrap();
    for (path, line, kind, mode, unit, value) in [
        (
            "mycelium_gateway/asgi.py",
            106,
            "comparison",
            "upper-guard",
            "milliseconds",
            60000,
        ),
        (
            "mycelium_gateway/asgi.py",
            106,
            "comparison-lower",
            "range-floor",
            "milliseconds",
            10,
        ),
        (
            "mycelium_a13_macos/endpoint_recovery.py",
            32,
            "comparison",
            "upper-guard",
            "milliseconds",
            300000,
        ),
        (
            "mycelium_a7_entry_controller.py",
            560,
            "comparison",
            "upper-guard",
            "entries",
            32,
        ),
        (
            "mycelium_live/artifact_transport.py",
            135,
            "comparison",
            "upper-guard",
            "entries",
            1000000,
        ),
    ] {
        let c = rows
            .iter()
            .find(|r| {
                r.path == path
                    && r.line == line
                    && r.kind == kind
                    && (line != 32 || r.expression.ends_with("<= 300"))
            })
            .unwrap();
        assert_eq!(
            categories::reviewed_quantity(c, mode),
            Some((unit, value)),
            "{}",
            c.key
        );
        assert_eq!(categories::reviewed_quantity(c, "literal"), None);
        let mut bad = c.clone();
        bad.expression = bad.expression.replace("<=", ">=");
        if bad.expression == c.expression {
            bad.expression.push_str(" + unreviewed");
        }
        assert_eq!(categories::reviewed_quantity(&bad, mode), None);
        bad = c.clone();
        bad.scope = "other_scope".into();
        assert_eq!(categories::reviewed_quantity(&bad, mode), None);
    }
    for c in rows
        .iter()
        .filter(|r| r.path == "mycelium_gossip/registry.py" && r.line == 102)
    {
        assert_eq!(categories::reviewed_quantity(c, "upper-guard"), None);
    }
}
