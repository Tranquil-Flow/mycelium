// SPDX-License-Identifier: AGPL-3.0-only
//! L1b round 5: the 43 reviewed round-4 discovery sites (35 literal upper acceptance limits of
//! chained/negated range checks, 8 numeric CLI duration defaults) and the 28 deliberately pending
//! neighbours. Values are re-derived by the validator from the candidate expression; this file
//! pins the expected key -> (unit, value, id) table independently.
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact four-discovery addition to the bounds-only gap"
)]
mod r32_verifier_admission;

#[path = "common/r29_retry_admission.rs"]
#[allow(
    dead_code,
    reason = "exact additive manual review outside bounds-only count"
)]
mod r29_retry_admission;
use budget_model::inventory::{
    Candidate, candidate_keys, candidates, categories, checked_in, validate,
};
use std::collections::BTreeMap;

/// (key, family, unit, value, registry id)
const REVIEWED: &[(&str, &str, &str, u64, &str)] = &[
    (
        "mycelium_a13_macos/app.py:704:59:comparison:30",
        "literal-upper-guard",
        "milliseconds",
        1800000,
        "PyUpperGuardMyceliumA13MacosAppL704C59",
    ),
    (
        "mycelium_a13_macos/app.py:1449:67:comparison:30",
        "literal-upper-guard",
        "milliseconds",
        1800000,
        "PyUpperGuardMyceliumA13MacosAppL1449C67",
    ),
    (
        "mycelium_a13_macos/clock_offset.py:49:45:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        60000,
        "PyUpperGuardMyceliumA13MacosClockOffsetL49C45",
    ),
    (
        "mycelium_a13_macos/demo_target.py:363:44:comparison:_CLEANUP_RESERVE_SECONDS",
        "literal-upper-guard",
        "milliseconds",
        3600000,
        "PyUpperGuardMyceliumA13MacosDemoTargetL363C44",
    ),
    (
        "mycelium_a13_macos/public_relay.py:324:47:comparison:30",
        "literal-upper-guard",
        "milliseconds",
        3600000,
        "PyUpperGuardMyceliumA13MacosPublicRelayL324C47",
    ),
    (
        "mycelium_demo/gateway_control_runtime.py:217:16:comparison:0.0",
        "literal-upper-guard",
        "milliseconds",
        30000,
        "PyUpperGuardMyceliumDemoGatewayControlRuntimeL217C16",
    ),
    (
        "mycelium_internet/activation.py:252:19:comparison:0.0",
        "literal-upper-guard",
        "milliseconds",
        86400000,
        "PyUpperGuardMyceliumInternetActivationL252C19",
    ),
    (
        "mycelium_internet/enrollment.py:139:19:comparison:0.0",
        "literal-upper-guard",
        "milliseconds",
        86400000,
        "PyUpperGuardMyceliumInternetEnrollmentL139C19",
    ),
    (
        "mycelium_internet/physical.py:2605:23:comparison:0.0",
        "literal-upper-guard",
        "milliseconds",
        3600000,
        "PyUpperGuardMyceliumInternetPhysicalL2605C23",
    ),
    (
        "mycelium_service_restart_evidence.py:104:16:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        300000,
        "PyUpperGuardMyceliumServiceRestartEvidenceL104C16",
    ),
    (
        "scripts/a13_operator/packaged_app_daemon.py:5:237:comparison:30",
        "literal-upper-guard",
        "milliseconds",
        1800000,
        "PyUpperGuardScriptsA13OperatorPackagedAppDaemonL5C237",
    ),
    (
        "scripts/a13_operator/packaged_app_daemon.py:12:69:comparison:0.05",
        "literal-upper-guard",
        "milliseconds",
        300000,
        "PyUpperGuardScriptsA13OperatorPackagedAppDaemonL12C69",
    ),
    (
        "scripts/a8_run_physical_gate.py:213:15:comparison:1",
        "literal-upper-guard",
        "milliseconds",
        300000,
        "PyUpperGuardScriptsA8RunPhysicalGateL213C15",
    ),
    (
        "scripts/qualify_a5_replica_track.py:613:11:comparison:300",
        "literal-upper-guard",
        "milliseconds",
        604800000,
        "PyUpperGuardScriptsQualifyA5ReplicaTrackL613C11",
    ),
    (
        "scripts/reconcile_a5_interrupted_attempt.py:365:53:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        86400000,
        "PyUpperGuardScriptsReconcileA5InterruptedAttemptL365C53",
    ),
    (
        "scripts/run_a5_benchmark_supervisor.py:416:46:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        86400000,
        "PyUpperGuardScriptsRunA5BenchmarkSupervisorL416C46",
    ),
    (
        "scripts/run_transport_probe_sidecar.py:78:11:comparison:1.0",
        "literal-upper-guard",
        "milliseconds",
        3600000,
        "PyUpperGuardScriptsRunTransportProbeSidecarL78C11",
    ),
    (
        "mycelium_router/token_path.py:63:11:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        50,
        "PyUpperGuardMyceliumRouterTokenPathL63C11",
    ),
    (
        "mycelium_live/swarm_metrics.py:54:24:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        90000,
        "PyUpperGuardMyceliumLiveSwarmMetricsL54C24",
    ),
    (
        "mycelium_live/swarm_metrics.py:38:28:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        90000,
        "PyUpperGuardMyceliumLiveSwarmMetricsL38C28",
    ),
    (
        "mycelium_live/swarm_metrics.py:53:60:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        86400000,
        "PyUpperGuardMyceliumLiveSwarmMetricsL53C60",
    ),
    (
        "mycelium_node/token_provisioning.py:130:62:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        60000,
        "PyUpperGuardMyceliumNodeTokenProvisioningL130C62",
    ),
    (
        "mycelium_node/token_provisioning.py:183:55:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        60000,
        "PyUpperGuardMyceliumNodeTokenProvisioningL183C55",
    ),
    (
        "scripts/bench_entry_token_path.py:87:11:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        100,
        "PyUpperGuardScriptsBenchEntryTokenPathL87C11",
    ),
    (
        "scripts/bench_entry_token_path.py:87:42:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        100,
        "PyUpperGuardScriptsBenchEntryTokenPathL87C42",
    ),
    (
        "scripts/capture_member_capacity_snapshot.py:157:15:comparison:120000",
        "literal-upper-guard",
        "milliseconds",
        3600000,
        "PyUpperGuardScriptsCaptureMemberCapacitySnapshotL157C15",
    ),
    (
        "scripts/experimental_candidate_isolation.py:165:49:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        300000,
        "PyUpperGuardScriptsExperimentalCandidateIsolationL165C49",
    ),
    (
        "scripts/run_a4_product_gate.py:260:11:comparison:0",
        "literal-upper-guard",
        "milliseconds",
        2000,
        "PyUpperGuardScriptsRunA4ProductGateL260C11",
    ),
    (
        "mycelium_a13_macos/managed_offers.py:58:52:comparison:0",
        "literal-upper-guard",
        "bytes",
        4294967296,
        "PyUpperGuardMyceliumA13MacosManagedOffersL58C52",
    ),
    (
        "mycelium_a13_macos/managed_staging.py:72:52:comparison:0",
        "literal-upper-guard",
        "bytes",
        1024,
        "PyUpperGuardMyceliumA13MacosManagedStagingL72C52",
    ),
    (
        "mycelium_a13_windows/managed_host/staging.py:218:19:comparison:0",
        "literal-upper-guard",
        "bytes",
        1024,
        "PyUpperGuardMyceliumA13WindowsManagedHostStagingL218C19",
    ),
    (
        "mycelium_a13_macos/work_authorization.py:167:49:comparison:0",
        "literal-upper-guard",
        "bytes",
        2097152,
        "PyUpperGuardMyceliumA13MacosWorkAuthorizationL167C49",
    ),
    (
        "mycelium_internet/physical.py:238:51:comparison:0",
        "literal-upper-guard",
        "bytes",
        67108864,
        "PyUpperGuardMyceliumInternetPhysicalL238C51",
    ),
    (
        "mycelium_live/stage_pack_builder.py:158:15:comparison:65536",
        "literal-upper-guard",
        "bytes",
        67108864,
        "PyUpperGuardMyceliumLiveStagePackBuilderL158C15",
    ),
    (
        "mycelium_live/local_preparer.py:674:19:comparison:1",
        "literal-upper-guard",
        "bytes-per-second",
        1000000000,
        "PyUpperGuardMyceliumLiveLocalPreparerL674C19",
    ),
    (
        "scripts/a13_operator/packaged_app_daemon.py:5:152:cli-default:--max-seconds",
        "cli-numeric-default",
        "milliseconds",
        1800000,
        "PyCliScriptsA13OperatorPackagedAppDaemonL5C152",
    ),
    (
        "scripts/a13_operator/packaged_owner_daemon.py:32:47:cli-default:--max-seconds",
        "cli-numeric-default",
        "milliseconds",
        1800000,
        "PyCliScriptsA13OperatorPackagedOwnerDaemonL32C47",
    ),
    (
        "scripts/a13_operator/packaged_target_daemon.py:34:47:cli-default:--max-seconds",
        "cli-numeric-default",
        "milliseconds",
        1800000,
        "PyCliScriptsA13OperatorPackagedTargetDaemonL34C47",
    ),
    (
        "scripts/a13_operator/stream_step_watch.py:37:52:cli-default:--report-seconds",
        "cli-numeric-default",
        "milliseconds",
        120000,
        "PyCliScriptsA13OperatorStreamStepWatchL37C52",
    ),
    (
        "scripts/bench_entry_token_path.py:81:48:cli-default:--hop-ms",
        "cli-numeric-default",
        "milliseconds",
        1,
        "PyCliScriptsBenchEntryTokenPathL81C48",
    ),
    (
        "scripts/bench_entry_token_path.py:82:52:cli-default:--control-ms",
        "cli-numeric-default",
        "milliseconds",
        10,
        "PyCliScriptsBenchEntryTokenPathL82C52",
    ),
    (
        "scripts/qualify_a5_replica_track.py:610:56:cli-default:--validity-seconds",
        "cli-numeric-default",
        "milliseconds",
        86400000,
        "PyCliScriptsQualifyA5ReplicaTrackL610C56",
    ),
    (
        "scripts/run_transport_probe_sidecar.py:76:61:cli-default:--max-runtime-seconds",
        "cli-numeric-default",
        "milliseconds",
        300000,
        "PyCliScriptsRunTransportProbeSidecarL76C61",
    ),
];

/// Round-4 sites left pending on purpose, with the reason class.
const PENDING: &[(&str, &str)] = &[
    (
        "mycelium_a13_macos/app.py:251:52:comparison:100000000",
        "comparison-not-reviewed",
    ),
    (
        "mycelium_a13_macos/app.py:811:47:comparison:_WINDOW_TTL_MIN",
        "comparison-not-reviewed",
    ),
    (
        "mycelium_a13_macos/swarm_publish.py:296:45:comparison:1",
        "comparison-not-reviewed",
    ),
    (
        "mycelium_a13_macos/swarm_stats.py:114:39:comparison:0",
        "comparison-not-reviewed",
    ),
    (
        "mycelium_a13_macos/worker.py:1516:64:comparison:0",
        "comparison-not-reviewed",
    ),
    (
        "mycelium_a15_release_decision.py:257:0:assignment:MAX_EXCLUSION_LIFETIME",
        "timedelta-constant",
    ),
    (
        "mycelium_a15_release_decision.py:294:0:assignment:MAX_REGISTRY_LIFETIME",
        "timedelta-constant",
    ),
    (
        "mycelium_a15_release_decision.py:832:33:comparison:trust.expires_at - issued_at",
        "comparison-not-reviewed",
    ),
    (
        "mycelium_a15_release_decision.py:1683:15:comparison:expires_at - issued_at",
        "comparison-not-reviewed",
    ),
    (
        "mycelium_mobile/artifact_grant.py:416:15:comparison:0",
        "comparison-not-reviewed",
    ),
    (
        "mycelium_seed/route_assignment.py:899:16:cli-default:--wait-applied",
        "cli-default-not-reviewed",
    ),
    (
        "physical_inference_node.py:4183:24:comparison:0",
        "comparison-not-reviewed",
    ),
    (
        "scripts/a12_gate_membership.py:141:77:comparison:10",
        "comparison-not-reviewed",
    ),
];

/// Round-5 pending-on-purpose comparisons (6 of the 7 symbolic-upper rows; 1196 was never a pin) that round 8 closed with the existing `comparison-reference-module-constant`
/// edge (its upper operand is a reviewed module constant): asserted reviewed, with only that review prefix, in
/// `round8_closed_pins`.
const ROUND8_CLOSED: &[&str] = &[
    "mycelium_a13_macos/pairing.py:666:23:comparison:_WINDOW_TTL_MIN",
    "mycelium_a13_macos/serving.py:77:12:comparison:SLICE_FETCH_MIN_SOURCE_BYTES",
    "mycelium_live/command_controller.py:95:37:comparison:minimum",
    "mycelium_live/liveness.py:81:37:comparison:minimum",
    "mycelium_seed/invite_batch.py:122:15:comparison:MIN_INVITE_TTL_SECONDS",
    "scripts/build_qwen_live_route.py:1203:11:comparison:-_MANAGED_OBSERVATIONS_MAX_SKEW_SECONDS",
];

/// Round-5 pending-on-purpose CLI defaults that round 7 closed with the `cli-literal:` rules
/// (`inventory_cli_literals`): they are asserted reviewed, with that review prefix, in `round7_closed_pins`.
const ROUND7_CLOSED: &[&str] = &[
    "scripts/a13_operator/packaged_app_daemon.py:12:16:cli-default:A13_DAEMON_KEEPALIVE_SECONDS",
    "scripts/a13_operator/round14_rpc.py:80:40:cli-default:A13_HANDSHAKE_DEADLINE_S",
    "scripts/a13_operator/round14_rpc.py:663:40:cli-default:A13_HANDSHAKE_DEADLINE_S",
    "scripts/build_a3_capacity_evidence.py:381:55:cli-default:--workspace-bytes",
    "scripts/build_m13_physical_control_plane.py:423:60:cli-default:--memory-ab-fast-bytes",
    "scripts/build_m17_model_feasibility.py:91:55:cli-default:--workspace-bytes",
    "scripts/external_reviewer_preflight.py:31:61:cli-default:--required-memory-bytes",
    "scripts/external_reviewer_preflight.py:32:59:cli-default:--required-disk-bytes",
    "scripts/run_a5_benchmark_supervisor.py:380:54:cli-default:--maximum-lifetime-seconds",
];

/// Whole-census pending count after this round's reviews.
const PENDING_TOTAL: usize = 1299; // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands. // +570 RU-051 keyword operands. // 876 at round 5; -2 round-6 argparse R1 edges; -24 source-unbounded; -8 round-7 forward-callers; -9 round-7 cli literals; -83 round-7 x-exclusions (inventory_x_exclusion) - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)

fn rows() -> Vec<budget_model::inventory::Bound> {
    checked_in().unwrap()
}

#[test]
fn reviewed_sites_map_to_the_exact_unit_value_and_registry_id() {
    let rows = rows();
    assert_eq!(REVIEWED.len(), 43);
    for (key, family, unit, value, id) in REVIEWED {
        let row = rows
            .iter()
            .find(|r| r.key == *key)
            .unwrap_or_else(|| panic!("{key}"));
        assert_eq!(row.unit, *unit, "{key}");
        assert_eq!(row.value, Some(*value), "{key}");
        assert_eq!(row.budget, *id, "{key}");
        assert!(row.disposition.is_empty(), "{key}");
        let expected_prefix = match *family {
            "literal-upper-guard" => "quantity-category:upper-",
            "cli-numeric-default" => "numeric-category:milliseconds;",
            other => panic!("{other}"),
        };
        assert!(
            row.review.starts_with(expected_prefix),
            "{key}: {}",
            row.review
        );
        // The registry id resolves to the same value and a source provenance at the site line.
        let entry = budgets::BudgetId::from_name(id).unwrap().entry();
        let line = row.line;
        let pin = if row.source_sha == budget_model::inventory::CENSUS_SOURCE_SHA {
            "f4e366e9"
        } else {
            assert_eq!(row.source_sha, budget_model::inventory::SOURCE_SHA, "{key}");
            "ef9ae61d"
        };
        assert!(
            matches!(entry.provenance, budgets::table::Provenance::Source(s)
                if s == format!("{}:{line} @ {pin}", row.path)),
            "{key}"
        );
    }
    // Only demo_target.py differs between the two census pins.
    let f4: Vec<_> = REVIEWED
        .iter()
        .filter(|(k, ..)| {
            rows.iter()
                .any(|r| r.key == *k && r.source_sha != budget_model::inventory::SOURCE_SHA)
        })
        .collect();
    assert_eq!(f4.len(), 1);
    assert!(
        f4[0]
            .0
            .starts_with("mycelium_a13_macos/demo_target.py:363:")
    );
    assert_eq!(validate(&rows), Ok(()));
}

fn candidate<'a>(all: &'a BTreeMap<String, Candidate>, key: &str) -> &'a Candidate {
    all.get(key).unwrap_or_else(|| panic!("{key}"))
}

#[test]
fn quantity_arms_refuse_edited_operands_wrong_modes_and_neighbours() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = rows();
    for (key, family, unit, value, _) in REVIEWED {
        if *family != "literal-upper-guard" {
            continue;
        }
        let row = rows.iter().find(|r| r.key == *key).unwrap();
        let mode = row
            .review
            .strip_prefix("quantity-category:")
            .unwrap()
            .split(';')
            .next()
            .unwrap();
        let c = candidate(&all, key);
        assert_eq!(
            categories::reviewed_quantity(c, mode),
            Some((*unit, *value)),
            "{key}"
        );
        // The sibling arm of the other expression class does not apply to this site.
        let other = if mode == "upper-guard" {
            "upper-integer-expression"
        } else {
            "upper-guard"
        };
        assert_eq!(
            categories::reviewed_quantity(c, other),
            None,
            "{key} as {other}"
        );
        for bad_mode in ["literal", "zero-boundary", "range-floor", "clock-offset"] {
            assert_eq!(
                categories::reviewed_quantity(c, bad_mode),
                None,
                "{key} as {bad_mode}"
            );
        }
        // Any edit to the retained predicate breaks the exact-site match.
        let mut edited = c.clone();
        edited.expression.push_str(" + 1");
        assert_eq!(
            categories::reviewed_quantity(&edited, mode),
            None,
            "{key} expression"
        );
        let mut edited = c.clone();
        edited.line += 1;
        assert_eq!(
            categories::reviewed_quantity(&edited, mode),
            None,
            "{key} line"
        );
        let mut edited = c.clone();
        edited.source.push('!');
        assert_eq!(
            categories::reviewed_quantity(&edited, mode),
            None,
            "{key} source"
        );
    }
}

#[test]
fn a_changed_value_unit_or_id_is_refused_for_every_reviewed_site() {
    let base = rows();
    for (key, ..) in REVIEWED {
        let i = base.iter().position(|r| r.key == *key).unwrap();
        let mut bad = base.clone();
        bad[i].value = bad[i].value.map(|v| v + 1);
        assert!(validate(&bad).is_err(), "{key} value");
        let mut bad = base.clone();
        bad[i].unit = if bad[i].unit == "bytes" {
            "milliseconds".into()
        } else {
            "bytes".into()
        };
        assert!(validate(&bad).is_err(), "{key} unit");
        let mut bad = base.clone();
        bad[i].budget = "PyUpperPhysicalInferenceNodeL2511C12".into();
        assert!(validate(&bad).is_err(), "{key} id");
    }
}

#[test]
fn pending_neighbours_stay_pending_and_the_total_is_exact() {
    let rows = rows();
    // 28 round-5 pins; the nine byte/string CLI defaults among them were closed in round 7 (`cli-literal:`), see below.
    assert_eq!(PENDING.len(), 13);
    for (key, why) in PENDING {
        assert!(
            rows.iter().all(|r| r.key != *key),
            "{key} ({why}) must stay pending"
        );
    }
    let reviewed: std::collections::BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        PENDING_TOTAL + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    );
}

#[test]
fn round8_closed_pins() {
    let rows = rows();
    assert_eq!(ROUND8_CLOSED.len(), 6);
    for key in ROUND8_CLOSED {
        let r = rows.iter().find(|r| r.key == *key).unwrap();
        assert!(
            r.review
                .starts_with("binding-category:comparison-reference-module-constant;"),
            "{key}"
        );
        assert_eq!(r.disposition, "derived", "{key}");
    }
}

#[test]
fn round7_closed_pins() {
    let rows = rows();
    assert_eq!(ROUND7_CLOSED.len(), 9);
    for key in ROUND7_CLOSED {
        let r = rows
            .iter()
            .find(|r| r.key == *key)
            .unwrap_or_else(|| panic!("{key} not reviewed"));
        assert!(r.review.starts_with("cli-literal:"), "{key}");
    }
}
