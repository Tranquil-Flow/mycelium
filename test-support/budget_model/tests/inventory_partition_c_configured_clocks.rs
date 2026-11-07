// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a7_physical_measurement.py; O:path:mycelium_demo/public_origin_proxy.py.
use budget_model::inventory::{bindings, candidates, checked_in};
use std::collections::BTreeMap;

#[test]
fn c_raw_config_witness_does_not_authorize_an_unreviewed_cast() {
    let all = candidates().unwrap();
    let source = all
        .iter()
        .find(|c| c.key == "mycelium_a7_physical_measurement.py:617:8:positional-call:time.sleep")
        .unwrap();
    let target = all
        .iter()
        .find(|c| {
            c.key == "mycelium_a7_physical_measurement.py:594:4:assignment:pause_quiescence_s"
        })
        .unwrap();
    assert!(bindings::accepts("typed-config-field", source, target));
    let mut changed = source.clone();
    changed.expression = format!("float({})", source.expression);
    assert_ne!(changed.expression, source.expression);
    assert!(!bindings::accepts("typed-config-field", &changed, target));
}

#[test]
fn c_configured_clock_edges_and_float_casts_keep_nominal_provenance() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let cases = [
        (
            "scripts/a13_operator/round14_rpc.py:94:12:positional-call:time.sleep",
            "scripts/a13_operator/round14_rpc.py:81:4:assignment:sleep_s",
            "same-scope-binding",
        ),
        (
            "scripts/a13_operator/round14_rpc.py:675:16:positional-call:time.sleep",
            "scripts/a13_operator/round14_rpc.py:664:8:assignment:sleep_s",
            "same-scope-binding",
        ),
        (
            "mycelium_a7_physical_measurement.py:629:4:assignment:deadline",
            "mycelium_a7_physical_measurement.py:586:4:assignment:ack_timeout_s",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_a7_physical_measurement.py:976:4:assignment:release_deadline",
            "mycelium_a7_physical_measurement.py:589:4:assignment:settle_timeout_s",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_a7_physical_measurement.py:1033:4:assignment:deadline",
            "mycelium_a7_physical_measurement.py:586:4:assignment:ack_timeout_s",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_a7_physical_measurement.py:1138:4:assignment:result_deadline",
            "mycelium_a7_physical_measurement.py:587:4:assignment:result_timeout_s",
            "reviewed-clock-offset",
        ),
        (
            "scripts/m14_transport_probe_host.py:79:8:assignment:deadline",
            "scripts/m14_transport_probe_host.py:128:47:cli-default:--timeout",
            "reviewed-clock-offset",
        ),
        (
            "scripts/run_a4_physical_queue_saturation_gate.py:158:4:assignment:drain_deadline",
            "scripts/run_a4_physical_queue_saturation_gate.py:252:55:cli-default:--drain-timeout",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_demo/gateway_control_runtime.py:563:8:assignment:deadline",
            "mycelium_demo/gateway_control_runtime.py:688:8:assignment:self.response_timeout_seconds",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:283:8:positional-call:request.settimeout",
            "mycelium_demo/public_origin_proxy.py:149:4:assignment:socket_timeout_seconds",
            "typed-config-field",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:637:12:keyword:timeout",
            "mycelium_demo/public_origin_proxy.py:131:4:assignment:upstream_timeout_seconds",
            "typed-config-field",
        ),
    ];
    for (key, target, rule) in cases {
        let c = &all[key];
        let target = &all[target];
        assert!(bindings::accepts(rule, c, target), "{key}");
        let bound = rows.iter().find(|r| r.key == key).unwrap();
        assert!(bindings::validate(bound, &rows, &all));
        assert!(bound.value.is_none());
        let id = budgets::BudgetId::from_name(&bound.relation).unwrap();
        let other = budgets::table::BUDGET_TABLE
            .iter()
            .find(|e| e.value == id.entry().value && e.provenance != id.entry().provenance)
            .unwrap();
        let mut changed = bound.clone();
        changed.relation = other.id.name().into();
        assert!(
            !bindings::validate(&changed, &rows, &all),
            "{key}: same-value wrong ID"
        );
        let mut changed = c.clone();
        changed.expression.push_str(" + 1");
        assert!(!bindings::accepts(rule, &changed, target));
        if rule == "reviewed-clock-offset" {
            changed = c.clone();
            changed.expression = changed
                .expression
                .replace("time.monotonic()", "time.time()");
            assert!(!bindings::accepts(rule, &changed, target));
        }
    }
    let send = all
        .values()
        .find(|c| c.path == "scripts/m14_transport_probe_host.py" && c.line == 79)
        .unwrap();
    let receive_default = all
        .values()
        .find(|c| c.path == send.path && c.line == 121 && c.kind == "cli-default")
        .unwrap();
    assert!(!bindings::accepts(
        "reviewed-clock-offset",
        send,
        receive_default
    ));
}
