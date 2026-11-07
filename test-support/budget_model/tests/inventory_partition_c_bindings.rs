// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_demo/public_origin_proxy.py; O:path:scripts/run_a9_gate.py.
use budget_model::inventory::{bindings, candidates, checked_in};
use std::collections::BTreeMap;

#[test]
fn c_lexical_edges_bind_distinct_declarations_and_preserve_dynamic_overrides() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let cases = [
        (
            "mycelium_demo/public_origin_proxy.py:136:4:assignment:directory_max_body_bytes",
            "mycelium_swarm_directory/hosted_defaults.py:25:0:assignment:DIRECTORY_MAX_BODY_BYTES",
            "imported-constant",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:149:4:assignment:socket_timeout_seconds",
            "mycelium_swarm_directory/hosted_defaults.py:22:0:assignment:PROXY_SOCKET_TIMEOUT_SECONDS",
            "imported-constant",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:840:64:cli-default:--socket-timeout-seconds",
            "mycelium_swarm_directory/hosted_defaults.py:22:0:assignment:PROXY_SOCKET_TIMEOUT_SECONDS",
            "imported-constant",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:851:48:cli-default:--directory-max-body-bytes",
            "mycelium_swarm_directory/hosted_defaults.py:25:0:assignment:DIRECTORY_MAX_BODY_BYTES",
            "imported-constant",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:775:12:keyword:socket_timeout_seconds",
            "mycelium_demo/public_origin_proxy.py:840:64:cli-default:--socket-timeout-seconds",
            "argparse-option",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:858:16:dict-field:directory_max_body_bytes",
            "mycelium_demo/public_origin_proxy.py:851:48:cli-default:--directory-max-body-bytes",
            "argparse-option",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:871:8:dict-field:directory_max_body_bytes",
            "mycelium_demo/public_origin_proxy.py:851:48:cli-default:--directory-max-body-bytes",
            "argparse-option",
        ),
        (
            "mycelium_demo/public_origin_proxy.py:387:16:keyword:max_bytes",
            "mycelium_demo/public_origin_proxy.py:136:4:assignment:directory_max_body_bytes",
            "typed-config-field",
        ),
        (
            "mycelium_platform_neutral/activation_transport.py:322:11:comparison:len(header_bytes) + 1 + len(payload)",
            "mycelium_platform_neutral/activation_transport.py:218:8:assignment:self._max_bytes",
            "comparison-reference-constructor-field",
        ),
        (
            "mycelium_demo/gateway_control_runtime.py:261:20:positional-call:time.sleep",
            "mycelium_demo/gateway_control_runtime.py:228:8:assignment:self.token_delay_seconds",
            "constructor-field",
        ),
        (
            "scripts/run_a5_benchmark_gate.py:800:12:keyword:deadline",
            "scripts/run_a5_benchmark_gate.py:124:0:assignment:WINDOW_WORKER_CLEANUP_TIMEOUT_SECONDS",
            "monotonic-offset-module-constant",
        ),
        (
            "scripts/run_a5_benchmark_gate.py:806:12:keyword:deadline",
            "scripts/run_a5_benchmark_gate.py:124:0:assignment:WINDOW_WORKER_CLEANUP_TIMEOUT_SECONDS",
            "monotonic-offset-module-constant",
        ),
        (
            "scripts/bench_stage_decode.py:80:30:dict-field:windows",
            "scripts/bench_stage_decode.py:170:47:cli-default:--windows",
            "argparse-option",
        ),
        (
            "scripts/m14_transport_probe_host.py:56:16:keyword:timeout",
            "scripts/m14_transport_probe_host.py:121:50:cli-default:--timeout",
            "argparse-option",
        ),
        (
            "scripts/run_a9_gate.py:709:8:keyword:paired_windows",
            "scripts/run_a9_gate.py:975:57:cli-default:--paired-windows",
            "argparse-option",
        ),
        (
            "scripts/run_a9_gate.py:806:46:keyword:paired_windows",
            "scripts/run_a9_gate.py:985:49:cli-default:--paired-windows",
            "argparse-option",
        ),
    ];
    for (source, target, rule) in cases {
        let source = &all[source];
        let target = &all[target];
        assert!(bindings::accepts(rule, source, target), "{}", source.key);
        let b = rows.iter().find(|b| b.key == source.key).unwrap();
        assert!(bindings::validate(b, &rows, &all));
        assert!(b.value.is_none());
        assert_eq!(b.disposition, "derived");
        let alternative = rows
            .iter()
            .find(|r| !r.budget.is_empty() && r.unit == b.unit && r.budget != b.relation)
            .unwrap();
        let mut changed = b.clone();
        changed.relation.clone_from(&alternative.budget);
        assert_ne!(changed.relation, b.relation);
        assert!(
            !bindings::validate(&changed, &rows, &all),
            "{} wrong same-unit ID",
            b.key
        );
        let mut changed = source.clone();
        changed.expression.push_str(" + foreign_policy");
        assert!(!bindings::accepts(rule, &changed, target));
    }
}
