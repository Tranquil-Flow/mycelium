// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/build_a13_linux.py; O:path:scripts/run_a5_benchmark_gate.py.
use budget_model::inventory::{candidates, categories, checked_in};

#[test]
fn c_operator_slots_are_reviewed_individually_not_directory_exemptions() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let cases = [
        (
            "scripts/a13_operator/single_mac_rehearsal.py:155:38:positional-call:acquire",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/a13_operator/single_mac_rehearsal.py:171:39:positional-call:acquire",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/a13_operator/single_mac_ui_rehearsal.py:158:38:positional-call:acquire",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/run_a11_route_benchmark.py:63:0:assignment:WINDOW_ORDER",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/build_a13_linux.py:97:0:assignment:_INSTALL_SH",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/build_a13_windows.py:398:0:assignment:_INSTALL_CMD",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/finalize_a13_macos.py:47:0:assignment:_INSTALL_COMMAND",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/run_a4_physical_idle_staleness_gate.py:52:0:assignment:RESOLVE_STALL_NODES",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/inspect_local_speculation_compatibility.py:171:8:assignment:semantics['sliding_window']",
            "reviewed-structural-slot",
        ),
        (
            "scripts/run_a5_benchmark_supervisor.py:560:23:comparison:termination_deadline[0]",
            "reviewed-structural-slot",
        ),
        (
            "scripts/run_a5_benchmark_supervisor.py:694:11:comparison:termination_deadline[0]",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_a7_node_agent.py:573:18:keyword:allow_terminal_retained",
            "reviewed-structural-slot",
        ),
        (
            "scripts/report_a11_request_bootstrap.py:76:16:slice:records",
            "reviewed-structural-slot",
        ),
        (
            "scripts/report_a11_request_bootstrap.py:102:15:slice:window_walls",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_product_spine/state.py:357:49:slice:events",
            "reviewed-structural-slot",
        ),
        (
            "scripts/run_a5_benchmark_gate.py:118:0:assignment:RETAINED_OUTCOMES",
            "reviewed-nonscalar-slot",
        ),
    ];
    for (key, category) in cases {
        let c = all.iter().find(|c| c.key == key).unwrap();
        assert!(categories::accepts(category, c), "{}", c.key);
        let bound = rows.iter().find(|r| r.key == key).unwrap();
        assert_eq!(bound.disposition, "not-a-bound");
        assert_eq!(bound.unit, "not-applicable");
        let mut changed = c.clone();
        changed.expression = "time.sleep(1)".into();
        assert!(!categories::accepts(category, &changed));
        changed = c.clone();
        changed.source.push_str(" # different blob");
        assert!(!categories::accepts(category, &changed));
    }
    // Measured warm latency becomes deadline/admission policy at this consumer;
    // it is deliberately not accepted as a non-enforcing observation slot.
    for c in all
        .iter()
        .filter(|c| c.path == "scripts/run_a11_route_benchmark.py" && c.line == 505)
    {
        assert!(!categories::accepts("reviewed-observation-slot", c));
    }
    // This other embedded script includes Select-Object -First and JSON -Depth;
    // it has not received this review, despite being a literal in the C queue.
    let c = all
        .iter()
        .find(|c| c.path == "mycelium_platform_neutral/thermal.py" && c.line == 38)
        .unwrap();
    assert!(!categories::accepts("reviewed-nonscalar-slot", c));
    for c in all.iter().filter(|c| {
        c.path == "scripts/run_a5_benchmark_supervisor.py" && [561, 567, 695].contains(&c.line)
    }) {
        assert!(!categories::accepts("reviewed-structural-slot", c));
    }
}
