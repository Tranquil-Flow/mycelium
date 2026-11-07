// SPDX-License-Identifier: AGPL-3.0-only
//! Discovery-scanner extension (L1b round 4): comparison/default shapes that blocked partition C.
//! The scanner is scratch-only (never committed or run by Rust); these tests pin its output:
//! the pre-extension census is a byte-identical prefix, the added sites are exactly the pinned
//! set, each in a named shape; the scanner reviews nothing (reviews are pinned separately).
#[path = "common/r25_count_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r25_count_admission;
#[path = "common/r27_delivery_wake_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r27_delivery_wake_admission;
#[path = "common/r29_retry_admission.rs"]
#[allow(dead_code, reason = "exact additive discovery cohort")]
mod r29_retry_admission;
#[path = "common/r30_workflow_admission.rs"]
#[allow(dead_code, reason = "exact additive source discovery cohort")]
mod r30_workflow_admission;
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact historical four-discovery/five-review projection"
)]
mod r32_verifier_admission;
use budget_model::inventory::{candidates, checked_in};
use std::collections::{BTreeMap, BTreeSet};

const CENSUS: &str = include_str!("../data/python-candidates.tsv");
const SITES: &str = include_str!("../data/python-discovery-r4-sites.tsv");
/// SHA-256 of `python-candidates.tsv` at f0756be (7,582 rows + header), before the extension.
const PRE_R4_SHA256: &str = "e20063bb5a05efe92cf0659c9c66ef8339e23e3101f79aecb0e80485318e7053";
const PRE_R4_LINES: usize = 7_583;
const PRE_R4_PENDING: usize = 853;
const NEW_SITES: usize = 71;
/// Round 13 (RU-044) appended these three `manual-bound` census rows after the r4 sites: the bounds v0.1.4
/// enforces that had no census row (node_wiring.py:182, standing_consent.py:103, gate_requirements/spec.py:213).
const R13_ROWS: [&str; 3] = [
    "mycelium_a7_node_wiring.py:182:114:manual-bound:maximum_entry_timeout_seconds",
    "mycelium_a13_macos/standing_consent.py:103:53:manual-bound:maximum_max_commands",
    "mycelium_gate_requirements/spec.py:213:56:manual-bound:maximum_max_report_age_seconds",
];

/// Round 14 (L1b, RU-042) appended these six `manual-bound` census rows after the round-13 rows: bounds v0.1.4
/// enforces that had no census row (a general rule: any v0.1.4 bound found gets a census row).
const R14_ROWS: [&str; 6] = [
    "mycelium_a13_macos/model_resources.py:406:11:manual-bound:maximum_storage_consent_bytes",
    "mycelium_a13_macos/standing_consent.py:102:58:manual-bound:maximum_max_wall_seconds",
    "mycelium_a13_macos/standing_consent.py:53:14:manual-bound:maximum_max_memory_bytes",
    "mycelium_node/token_provisioning.py:148:59:manual-bound:maximum_process_ceiling_seconds",
    "mycelium_a13_macos/demo_chain.py:836:58:manual-bound:maximum_member_wall_cap_seconds",
    "physical_inference_node.py:6576:30:manual-bound:maximum_progress_wait_slice_seconds",
];

fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(data)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn sites() -> Vec<(&'static str, &'static str)> {
    let mut lines = SITES.lines();
    assert_eq!(lines.next(), Some("key	shape"));
    lines
        .map(|line| line.split_once('\t').expect("key<TAB>shape"))
        .collect()
}

#[test]
fn sha256_helper_matches_known_vectors() {
    assert_eq!(
        sha256_hex(b""),
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
    assert_eq!(
        sha256_hex(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
}

#[test]
fn pre_extension_census_is_a_byte_identical_prefix() {
    let prefix: String = CENSUS
        .split_inclusive('\n')
        .take(PRE_R4_LINES)
        .collect::<String>();
    assert_eq!(prefix.lines().count(), PRE_R4_LINES);
    assert_eq!(sha256_hex(prefix.as_bytes()), PRE_R4_SHA256);
}

#[test]
fn census_diff_is_exactly_the_pinned_new_sites_in_order() {
    let mut r25: BTreeSet<_> = r25_count_admission::keys()
        .union(
            &r27_delivery_wake_admission::keys()
                .union(
                    &r29_retry_admission::keys()
                        .union(&r30_workflow_admission::keys())
                        .copied()
                        .collect(),
                )
                .copied()
                .collect(),
        )
        .copied()
        .collect();
    r25.extend(r32_verifier_admission::keys());
    let rows: Vec<_> = CENSUS
        .lines()
        .skip(PRE_R4_LINES)
        .filter(|r| !r25.contains(r.split('\t').next().unwrap()))
        .collect();
    let pinned = sites();
    assert_eq!(pinned.len(), NEW_SITES);
    let keyword_keys: Vec<_> = include_str!("../data/python-keyword-discovery.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| f[7] == "added")
        .map(|f| f[0])
        .collect();
    assert_eq!(keyword_keys.len(), 570);
    let r16: Vec<_> = include_str!("../data/python-r16-census.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(r16.len(), 8);
    let r17: Vec<_> = include_str!("../data/python-r17-tuple.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(r17.len(), 12);
    assert_eq!(
        rows.len(),
        NEW_SITES + R13_ROWS.len() + R14_ROWS.len() + keyword_keys.len() + r16.len() + r17.len()
    );
    let start = NEW_SITES + R13_ROWS.len() + R14_ROWS.len();
    let keyword_rows = &rows[start..start + keyword_keys.len()];
    assert_eq!(
        rows[start + keyword_keys.len()..]
            .iter()
            .map(|r| r.split('\t').next().unwrap())
            .collect::<Vec<_>>(),
        r16.iter().chain(r17.iter()).copied().collect::<Vec<_>>()
    );
    assert_eq!(
        keyword_rows
            .iter()
            .map(|r| r.split('\t').next().unwrap())
            .collect::<Vec<_>>(),
        keyword_keys
    );
    for (row, (key, _)) in rows.iter().zip(&pinned) {
        assert_eq!(row.split('\t').next(), Some(*key));
    }
    for (row, key) in rows[NEW_SITES..]
        .iter()
        .zip(R13_ROWS.into_iter().chain(R14_ROWS))
    {
        assert_eq!(row.split('\t').next(), Some(key));
        assert_eq!(row.split('\t').nth(7), Some("manual-bound"), "{key}");
    }
    let keys: BTreeSet<_> = pinned.iter().map(|(key, _)| *key).collect();
    assert_eq!(keys.len(), NEW_SITES, "duplicate pinned key");
    let shapes: BTreeMap<&str, usize> = pinned.iter().fold(BTreeMap::new(), |mut m, (_, s)| {
        *m.entry(*s).or_default() += 1;
        m
    });
    assert_eq!(
        shapes,
        BTreeMap::from([
            ("argparse-default", 14),
            ("chain-bare-test", 2),
            ("chain-later-bool-operand", 14),
            ("not-chain", 33),
            ("string-default", 4),
            ("timedelta-ceiling-compare", 2),
            ("timedelta-const", 2),
        ])
    );
    assert_eq!(
        candidates().unwrap().len() - r25.len(),
        PRE_R4_LINES - 1
            + NEW_SITES
            + R13_ROWS.len()
            + R14_ROWS.len()
            + keyword_keys.len()
            + r16.len()
            + r17.len()
    );
}

/// Round 5 reviewed exactly these r4 sites (the discovery commit itself reviewed none).
const R5_REVIEWED: &str = include_str!("../data/python-discovery-r5-reviewed.tsv");

#[test]
fn only_the_pinned_round5_sites_are_reviewed_and_the_rest_stay_pending() {
    let reviewed: BTreeSet<_> = checked_in()
        .unwrap()
        .into_iter()
        .map(|row| row.key)
        .collect();
    let r5: BTreeSet<_> = R5_REVIEWED
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    let new: BTreeSet<_> = sites().into_iter().map(|(key, _)| key).collect();
    assert_eq!(new.len(), NEW_SITES);
    assert!(
        r5.is_subset(&new),
        "round-5 reviewed set is a subset of the r4 sites"
    );
    // Round 7 later closed nine of the r4 CLI defaults with the `cli-literal:` rules (byte quantity / decimal
    // string); nothing else outside the round-5 set is reviewed.
    let r7: BTreeSet<_> = checked_in()
        .unwrap()
        .into_iter()
        .filter(|row| row.review.starts_with("cli-literal:"))
        .map(|row| row.key)
        .collect();
    assert_eq!(r7.len(), 9);
    assert!(
        r7.iter().all(|k| new.contains(k.as_str())),
        "round-7 cli literals are r4 sites"
    );
    // Round 8 closed six more r4 sites (the symbolic-upper module-constant comparisons), with the existing module-constant comparison edge.
    let r8: BTreeSet<_> = checked_in()
        .unwrap()
        .into_iter()
        .filter(|row| {
            row.review
                .starts_with("binding-category:comparison-reference-module-constant;")
                && new.contains(row.key.as_str())
        })
        .map(|row| row.key)
        .collect();
    assert_eq!(r8.len(), 6);
    assert!(r8.contains(
        "scripts/build_qwen_live_route.py:1203:11:comparison:-_MANAGED_OBSERVATIONS_MAX_SKEW_SECONDS"
    ));
    for key in &new {
        assert_eq!(
            reviewed.contains(*key),
            r5.contains(key) || r7.contains(*key) || r8.contains(*key),
            "{key}"
        );
    }
    let pending_new = new.iter().filter(|key| !reviewed.contains(**key)).count();
    assert_eq!(pending_new, NEW_SITES - r5.len() - r7.len() - r8.len());
    // The pre-extension pending count was PRE_R4_PENDING; the whole-census count is pinned
    // in `inventory_round5_review` so one place owns the exact number.
    assert_eq!(PRE_R4_PENDING + NEW_SITES, 924);
}

#[test]
fn the_blocked_partition_c_comparison_shapes_are_found_exactly() {
    let pinned: BTreeMap<_, _> = sites().into_iter().collect();
    for (key, shape) in [
        (
            "scripts/run_a5_benchmark_supervisor.py:416:46:comparison:0",
            "not-chain",
        ),
        (
            "scripts/qualify_a5_replica_track.py:613:11:comparison:300",
            "not-chain",
        ),
        (
            "scripts/a8_run_physical_gate.py:213:15:comparison:1",
            "not-chain",
        ),
        (
            "mycelium_internet/physical.py:2605:23:comparison:0.0",
            "not-chain",
        ),
        (
            "scripts/bench_entry_token_path.py:87:11:comparison:0",
            "not-chain",
        ),
        (
            "scripts/bench_entry_token_path.py:87:42:comparison:0",
            "not-chain",
        ),
        (
            "scripts/a12_gate_membership.py:141:77:comparison:10",
            "chain-later-bool-operand",
        ),
        // one-line argparse guards and defaults
        (
            "scripts/a13_operator/packaged_app_daemon.py:5:152:cli-default:--max-seconds",
            "argparse-default",
        ),
        (
            "scripts/a13_operator/packaged_app_daemon.py:5:237:comparison:30",
            "chain-bare-test",
        ),
        (
            "scripts/a13_operator/packaged_app_daemon.py:12:69:comparison:0.05",
            "chain-bare-test",
        ),
        (
            "scripts/a13_operator/packaged_app_daemon.py:12:16:cli-default:A13_DAEMON_KEEPALIVE_SECONDS",
            "string-default",
        ),
        (
            "scripts/bench_entry_token_path.py:81:48:cli-default:--hop-ms",
            "argparse-default",
        ),
        (
            "scripts/bench_entry_token_path.py:82:52:cli-default:--control-ms",
            "argparse-default",
        ),
        (
            "scripts/qualify_a5_replica_track.py:610:56:cli-default:--validity-seconds",
            "argparse-default",
        ),
        // string default and timedelta ceilings
        (
            "scripts/run_a5_benchmark_supervisor.py:380:54:cli-default:--maximum-lifetime-seconds",
            "string-default",
        ),
        (
            "mycelium_a15_release_decision.py:257:0:assignment:MAX_EXCLUSION_LIFETIME",
            "timedelta-const",
        ),
        (
            "mycelium_a15_release_decision.py:1683:15:comparison:expires_at - issued_at",
            "timedelta-ceiling-compare",
        ),
    ] {
        assert_eq!(pinned.get(key), Some(&shape), "{key}");
    }
    // Rows carry the exact oracle expression and line text.
    let all = candidates().unwrap();
    let by_key: BTreeMap<_, _> = all.iter().map(|c| (c.key.as_str(), c)).collect();
    let site = by_key["scripts/a12_gate_membership.py:141:77:comparison:10"];
    assert_eq!(site.expression, "10 <= wait <= 1800");
    assert_eq!(site.scope, "validate_gate_members");
}

#[test]
fn near_misses_are_not_discovered() {
    let keys: BTreeSet<_> = sites().into_iter().map(|(key, _)| key).collect();
    for (path, line) in [
        // port ranges, indices, token counts and model dimensions are not time/size budgets
        ("mycelium_a10_verify_control.py", 168),
        ("mycelium_live/registry.py", 563),
        ("scripts/run_m23_kv_gate.py", 438),
        ("mycelium_a13_macos/tls.py", 51),
        ("mycelium_interactive/swarm.py", 202),
        ("physical_pixel_host_stage.py", 171),
        ("mycelium_a13_macos/vendor/qrcodegen.py", 229),
        ("scripts/run_a9_gate.py", 999),
        ("scripts/build_m15_workload_comparison.py", 74),
        // already in the census: the first chained operand and the existing --ttl-seconds pair
        ("scripts/inventory_member_models.py", 49),
        ("scripts/inventory_member_models.py", 51),
    ] {
        let prefix = format!("{path}:{line}:");
        assert!(
            !keys.iter().any(|key| key.starts_with(&prefix)),
            "{prefix} must not be a new site"
        );
    }
    // A timedelta zero and single-operand ordering comparisons are not ceilings.
    assert!(!keys.iter().any(|key| key.contains("OTHER")));
}
