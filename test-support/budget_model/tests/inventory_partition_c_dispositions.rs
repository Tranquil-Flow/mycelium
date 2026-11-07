// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_seed/state.py; O:path:scripts/run_m23_kv_gate.py.
use budget_model::inventory::{candidates, categories, checked_in};

#[test]
fn c_source_witnesses_separate_results_and_identities_from_policy() {
    let all = candidates().unwrap();
    let reviewed = checked_in().unwrap();
    let cases: &[(&str, &str)] = &[
        (
            "mycelium_a6_recovery.py:898:4:assignment:retained",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_a7_physical_measurement.py:1079:12:assignment:settled",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_a7_physical_measurement.py:1224:4:assignment:settled",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_a7_physical_measurement.py:1223:4:assignment:trace['settle_reads']",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_a7_physical_measurement.py:1275:47:comparison:event.received_monotonic_s",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_internet/physical.py:2293:8:assignment:retained",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_model_catalog.py:1820:12:assignment:(feasible, stages, bottleneck, diagnostics, rejected_candidates)",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_physical_runner/remote_probe.py:119:12:assignment:tick_count_ms",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_platform_neutral/capability_report.py:149:8:dict-field:throttle_behavior",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_platform_neutral/stage_performance.py:104:8:assignment:time_ms",
            "reviewed-observation-slot",
        ),
        (
            "mycelium_platform_neutral/stage_performance.py:120:19:keyword:time_ms",
            "reviewed-observation-slot",
        ),
        (
            "mycelium_platform_neutral/stage_performance.py:202:12:keyword:end_to_end_ms",
            "reviewed-observation-slot",
        ),
        (
            "mycelium_platform_neutral/stage_performance.py:244:12:keyword:uncertainty_ms",
            "reviewed-observation-slot",
        ),
        (
            "mycelium_product_spine/app.py:279:16:assignment:retained",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_qualification/physical_deployment.py:913:4:assignment:ordered_intervals",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_qualification/replica_track_backend.py:311:16:assignment:cancel_with_deadline",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_qualification/replica_track_backend.py:495:8:assignment:cancel_with_deadline",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_seed/coordinator.py:1242:12:assignment:member.last_heartbeat_sequence",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_seed/coordinator.py:1411:16:assignment:persisted_member['last_heartbeat_sequence']",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_seed/coordinator.py:1257:8:assignment:member.latest_messages[HEARTBEAT_PROTOCOL]",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_seed/http.py:258:8:assignment:numbers['seconds_after_expires']",
            "reviewed-observation-slot",
        ),
        (
            "mycelium_seed/http.py:261:4:assignment:error.window",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_seed/http.py:987:61:keyword:expires",
            "reviewed-observation-slot",
        ),
        (
            "mycelium_seed/state.py:791:19:comparison:heartbeat_sequence",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_seed/state.py:1113:19:comparison:heartbeat_sequence",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_seed/state.py:1388:15:comparison:heartbeat_sequence",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_seed/state.py:1105:12:assignment:current_heartbeat_sequence",
            "reviewed-structural-slot",
        ),
        (
            "mycelium_seed/state.py:1153:12:assignment:heartbeat_raw",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_seed/state.py:1376:12:assignment:heartbeat_json",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_seed/state.py:1273:12:assignment:(stored_request_digest, stored_heartbeat_sequence, _stored_heartbeat, renewal)",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_seed/state.py:1342:12:assignment:(_request_digest, _heartbeat_sequence, _heartbeat, renewal)",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_seed/state.py:1460:16:assignment:(stored_request_digest, stored_heartbeat_sequence, stored_heartbeat, stored_renewal)",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_seed/state.py:1496:12:assignment:heartbeat_message",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_speculative_decode_execution.py:1111:27:dict-field:admitted_deadline_ms",
            "reviewed-policy-readout",
        ),
        (
            "mycelium_speculative_decode_execution.py:1329:23:dict-field:admitted_deadline_ms",
            "reviewed-policy-readout",
        ),
        (
            "mycelium_speculative_decode_execution.py:1767:8:keyword:deadline_outcome",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_speculative_decode_execution.py:1794:30:keyword:deadline_outcome",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_swarm_directory/directory.py:115:74:dict-field:/heartbeat",
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_swarm_directory/directory.py:225:44:keyword:heartbeat_received",
            "reviewed-observation-slot",
        ),
        (
            "mycelium_swarm_directory/entry.py:573:12:assignment:settled",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/a13_operator/single_mac_ui_rehearsal.py:161:8:assignment:(window_id, a, b)",
            "reviewed-structural-slot",
        ),
        (
            "scripts/a8_run_rehearsal_peer.py:129:4:assignment:lease",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/run_a10_physical_gate.py:778:6:assignment:stalled_session",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/run_a10_physical_gate.py:779:6:assignment:stalled",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/run_a12_mobile_physical_gate.py:235:6:assignment:self.sleep",
            "reviewed-nonscalar-slot",
        ),
        (
            "scripts/run_a4_physical_idle_staleness_gate.py:242:4:assignment:stalled_at",
            "reviewed-observation-slot",
        ),
        (
            "scripts/run_m23_kv_gate.py:150:55:slice:events",
            "reviewed-nonscalar-slot",
        ),
    ];
    for (key, category) in cases {
        let c = all.iter().find(|c| c.key == *key).unwrap();
        assert!(categories::accepts(category, c), "{} {category}", c.key);
        let b = reviewed.iter().find(|b| b.key == c.key).unwrap();
        assert!(b.review.starts_with(&format!("category:{category};")));
        assert_eq!(b.disposition, "not-a-bound");
        assert!(b.value.is_none());
        for field in 0..9 {
            let mut changed = c.clone();
            match field {
                0 => changed.key.push('!'),
                1 => changed.path.push('!'),
                2 => changed.line += 1,
                3 => changed.column += 1,
                4 => changed.scope.push('!'),
                5 => changed.symbol.push('!'),
                6 => changed.expression.push('!'),
                7 => changed.kind.push('!'),
                8 => changed.source.push('!'),
                _ => unreachable!(),
            }
            assert_ne!(changed, *c);
            assert!(
                !categories::accepts(category, &changed),
                "{} field {field}",
                c.key
            );
        }
    }
}

#[test]
fn c_adjacent_deadline_and_storage_policies_are_not_diagnostic_exemptions() {
    let all = candidates().unwrap();
    for (path, line) in [
        ("mycelium_a7_physical_measurement.py", 1550),
        ("mycelium_product_spine/app.py", 281),
        ("mycelium_speculative_decode_execution.py", 1462),
        ("mycelium_seed/coordinator.py", 1243),
        ("mycelium_seed/coordinator.py", 1248),
        ("mycelium_qualification/replica_track_backend.py", 366),
        ("scripts/a13_operator/packaged_app_daemon.py", 24),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        for category in [
            "reviewed-nonscalar-slot",
            "reviewed-structural-slot",
            "reviewed-observation-slot",
            "reviewed-policy-readout",
        ] {
            assert!(!categories::accepts(category, c), "{} {category}", c.key);
        }
    }
}
