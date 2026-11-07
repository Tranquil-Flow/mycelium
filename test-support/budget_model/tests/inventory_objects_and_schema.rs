// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_seed/operator.py; O:path:physical_inference_node.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};
#[test]
fn reviewed_objects_and_schema_patterns_do_not_waive_policy_values() {
    let all = candidates().unwrap();
    for (path, line, symbol) in [
        (
            "mycelium_seed/operator.py",
            319,
            "(lease, coordinator, binding)",
        ),
        (
            "mycelium_router/mlx_runtime.py",
            1145,
            "self._terminal_retention",
        ),
        (
            "mycelium_router/live_ports.py",
            706,
            "node_capacity_kv_bytes",
        ),
        ("mycelium_a13_macos/app.py", 1234, "join_window"),
        ("physical_inference_node.py", 356, "_A10_CANDIDATE_WINDOW"),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        assert!(categories::accepts("reviewed-nonscalar-slot", c));
    }
    for (path, line, symbol) in [
        ("mycelium_a13_macos/worker.py", 953, "(command_timeout, _)"),
        ("mycelium_a13_macos/demo_chain.py", 259, "self.budgets"),
        (
            "physical_inference_node.py",
            355,
            "A10_MAXIMUM_COLLECTION_WINDOW_MS",
        ),
        (
            "mycelium_router/terminal_kv_retention.py",
            106,
            "float(now)",
        ),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        assert!(!categories::accepts("reviewed-nonscalar-slot", c));
    }
}
#[test]
fn classification_sets_and_protocol_tags_do_not_supply_numeric_allowances() {
    let all = candidates().unwrap();
    for (path, line) in [
        ("mycelium_a13_macos/demo_target.py", 165),
        ("mycelium_a13_macos/managed_control.py", 183),
        ("scripts/a13_operator/round14_retry_policy.py", 20),
        ("scripts/build_a13_linux.py", 246),
        ("mycelium_router/payloads.py", 23),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(categories::accepts("reviewed-nonscalar-slot", c));
        let mut wrong = c.clone();
        wrong.symbol = "max_bytes.unreviewed".into();
        assert!(!categories::accepts("reviewed-nonscalar-slot", &wrong));
    }
    let cap = all
        .iter()
        .find(|c| c.path == "mycelium_router/payloads.py" && c.line == 25)
        .unwrap();
    assert!(!categories::accepts("reviewed-nonscalar-slot", cap));
}
#[test]
fn a10_parser_schema_and_successful_value_keep_the_250ms_guard_independent() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let cap = rows
        .iter()
        .find(|r| r.path == "physical_inference_node.py" && r.line == 355)
        .unwrap();
    assert_eq!(cap.value, Some(250));
    assert_eq!(cap.unit, "milliseconds");
    for line in [371, 372, 373] {
        let c = all
            .iter()
            .find(|c| c.path == cap.path && c.line == line)
            .expect("each parser value/guard/validated output remains inventoried");
        assert!(!categories::accepts("reviewed-nonscalar-slot", c));
        let row = rows.iter().find(|r| r.key == c.key).unwrap();
        assert_eq!(row.relation, cap.budget);
        assert_eq!(row.disposition, "derived");
    }
    let mut bad = rows.clone();
    let guard = bad
        .iter_mut()
        .find(|r| r.path == cap.path && r.line == 372)
        .unwrap();
    guard.expression = "window_ms < A10_MAXIMUM_COLLECTION_WINDOW_MS".into();
    assert!(validate(&bad).is_err());
}
