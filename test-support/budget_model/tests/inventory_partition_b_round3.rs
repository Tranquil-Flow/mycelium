// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/router_port.py; O:path:mycelium_router/transports/iroh.py.
use budget_model::inventory::{bindings, candidates, categories, checked_in};

#[test]
fn b_record_and_boolean_results_preserve_numeric_enforcement_sites() {
    let all = candidates().unwrap();
    for (path, line, category) in [
        (
            "mycelium_router/transports/iroh.py",
            286,
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_router/transports/iroh.py",
            1915,
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_router/transports/iroh.py",
            2108,
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_live/router_port.py",
            485,
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_live/router_port.py",
            1690,
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_live/router_port.py",
            1966,
            "reviewed-nonscalar-slot",
        ),
        ("mycelium_live/route.py", 3541, "reviewed-observation-slot"),
    ] {
        let source = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(categories::accepts(category, source), "{}", source.key);
        let mut wrong = source.clone();
        wrong.expression.push_str(" + 1");
        assert!(!categories::accepts(category, &wrong));
    }
    for (path, line) in [
        ("mycelium_router/transports/iroh.py", 1917),
        ("mycelium_router/transports/iroh.py", 2115),
        ("mycelium_live/router_port.py", 298),
        ("mycelium_live/router_port.py", 1925),
        ("mycelium_live/route.py", 3546),
    ] {
        let source = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(!categories::accepts("reviewed-nonscalar-slot", source));
        assert!(!categories::accepts("reviewed-observation-slot", source));
    }
}

#[test]
fn b_deadline_forwarding_and_close_joins_keep_one_original_clock() {
    let all = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, line, symbol) in [
        ("mycelium_iroh_sidecar/client.py", 509, "deadline"),
        ("mycelium_iroh_sidecar/client.py", 651, "deadline"),
        ("mycelium_iroh_sidecar/client.py", 954, "deadline"),
        ("mycelium_live/router_port.py", 704, "deadline_monotonic_s"),
        ("mycelium_live/router_port.py", 3381, "timeout"),
        ("mycelium_live/router_port.py", 3386, "timeout"),
        ("mycelium_live/router_port.py", 3394, "timeout"),
    ] {
        let row = rows
            .iter()
            .find(|r| r.path == path && r.line == line && r.symbol == symbol)
            .expect("reviewed exact B forwarding site");
        assert!(bindings::validate(row, &rows, &all), "{}", row.key);
        let mut wrong = row.clone();
        wrong.relation = "CancelAndCleanup".into();
        assert!(
            !bindings::validate(&wrong, &rows, &all),
            "same-unit substitution {}",
            row.key
        );
    }
}
