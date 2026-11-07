// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/route.py; O:path:mycelium_router/analyze.py; O:path:mycelium_router/scheduler.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

fn find<'a>(rows: &'a [Bound], path: &str, line: usize) -> &'a Bound {
    rows.iter()
        .find(|r| r.path == path && r.line == line)
        .unwrap_or_else(|| panic!("missing reviewed row {path}:{line}"))
}

#[test]
fn b_test_fault_deadline_minimum_stays_under_the_construction_window_not_the_active_deadline() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in [1643, 1872] {
        let r = find(&rows, "mycelium_live/route.py", line);
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(r.relation, "PyMyceliumLiveRouteTransportFaultWindowL1373");
        assert!(r.semantic.contains("construction"));
        assert!(proof::validate(r, &rows, &all), "line {line}");
        let mut bad = r.clone();
        bad.relation = "CancelAndCleanup".into();
        assert!(!proof::validate(&bad, &rows, &all));
        // Dropping the min() or swapping its operand is a different expression.
        bad = r.clone();
        bad.expression = bad
            .expression
            .replace("min(fault.deadline_monotonic_ms, ", "min(");
        assert_ne!(bad.expression, r.expression);
        assert!(!proof::validate(&bad, &rows, &all));
    }
    // The router-port test hook has no construction-time window: it compares directly with a
    // command deadline, so it must not inherit the route fault window.
    // Round 7 (RU-013) may review it, but only as an absolute-instant exclusion with its own producer, never with
    // the route fault window relation.
    assert!(
        rows.iter()
            .filter(|r| r.path == "mycelium_live/router_port.py" && r.line == 1440)
            .all(|r| r.review.starts_with("x-exclusion:")
                && r.relation != "PyMyceliumLiveRouteTransportFaultWindowL1373")
    );
}

#[test]
fn b_peer_cleanup_reserve_default_is_the_configured_constant_not_a_cap_on_overrides() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let r = find(&rows, "mycelium_live/route.py", 208);
    assert_eq!(
        r.relation,
        "PyMyceliumDeadlinesCleanupFallbackReserveSecondsL193"
    );
    assert_eq!(r.unit, "milliseconds");
    assert!(r.semantic.contains("default"));
    assert!(proof::validate(r, &rows, &all));
    let id = budgets::BudgetId::from_name(&r.relation).unwrap();
    let budgets::table::Value::Duration(span) = id.entry().value else {
        panic!("reserve default must be a plain duration");
    };
    assert_eq!(span.millis(), 3500);
    let mut bad = r.clone();
    bad.relation = "CancelAndCleanup".into();
    assert!(!proof::validate(&bad, &rows, &all));
    // The window subtraction and the caller-supplied millisecond override are not defaults.
    assert!(
        !rows
            .iter()
            .any(|x| x.path == "mycelium_live/route.py" && x.line == 213)
    );
}

#[test]
fn b_report_row_limit_and_byte_accumulator_seed_are_not_runtime_waits() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let r = find(&rows, "mycelium_router/analyze.py", 283);
    assert_eq!(r.unit, "entries");
    assert_eq!(r.value, Some(8));
    assert_eq!(r.budget, "PyMyceliumRouterAnalyzeBottleneckRowsL283");
    assert!(proof::validate(r, &rows, &all));
    for unit in ["bytes", "milliseconds"] {
        let mut bad = r.clone();
        bad.unit = unit.into();
        assert!(!proof::validate(&bad, &rows, &all));
    }
    let mut bad = r.clone();
    bad.budget = "PyPrefillMaximumResends".into();
    bad.value = Some(2);
    assert!(!proof::validate(&bad, &rows, &all));
    let s = find(&rows, "mycelium_router/scheduler.py", 190);
    assert_eq!(s.disposition, "not-a-bound");
    assert!(s.review.starts_with("category:reviewed-inactive-state"));
    assert_eq!(s.expression, "0");
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
