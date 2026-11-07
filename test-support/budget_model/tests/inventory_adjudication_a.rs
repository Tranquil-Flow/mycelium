// SPDX-License-Identifier: AGPL-3.0-only
//! Partition A of the manual adjudication (L1b round 13): what lane L1b assigned itself and decided. A lane
//! pins only its OWN partition here; partition B's lane writes `python-adjudication-b.tsv`,
//! `python-oracle-line-pins-b.tsv` and its own `inventory_adjudication_b.rs`, and touches nothing of A.
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared test helper: each test binary uses a subset"
)]
mod oracle_blobs;

#[path = "common/r29_retry_admission.rs"]
#[allow(dead_code, reason = "exact additive discovery cohort")]
mod r29_retry_admission;
use budget_model::inventory::{adjudication as adj, candidates, checked_in};
use std::collections::{BTreeMap, BTreeSet};

/// SHA-256 of the sorted partition-A keys joined by `\n` (375 rows: the 369 of round 13 plus the six round-14
/// `manual-bound` census rows, appended to the partitions table).
const A_KEYS_SHA256: &str = "6f3d8a3d8928ab59032fea13fdc847a8b840bef515439bf43aab9b4f3d2ba4ab";
const A_ASSIGNED: usize = 375;
/// Round 13 decided 76 rows, round 14 decided 275 more, round 15 decided 22 more (24 were open).
const R13_ADJUDICATED: usize = 76;
const R14_ADJUDICATED: usize = 275;
const R15_ADJUDICATED: usize = 22;
const A_ADJUDICATED: usize = R13_ADJUDICATED + R14_ADJUDICATED + R15_ADJUDICATED + 2; // R16 legacy domains

fn r17_manual_keys() -> BTreeSet<&'static str> {
    let keys: BTreeSet<_> = include_str!("../data/python-r17-tuple.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(keys.len(), 12);
    keys
}

fn a_entries() -> Vec<adj::Entry> {
    let supplemental = r17_manual_keys();
    let later = r29_retry_admission::keys();
    adj::parse(&adj::TABLES)
        .unwrap()
        .entries
        .into_iter()
        .filter(|e| {
            e.partition == 'A'
                && !e.key.contains(":validator-keyword:")
                && !supplemental.contains(e.key.as_str())
                && !later.contains(e.key.as_str())
        })
        .collect()
}

#[test]
fn partition_a_assignment_is_the_pinned_key_list() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let supplemental = r17_manual_keys();
    let later = r29_retry_admission::keys();
    let keys: BTreeSet<&String> = parsed
        .partitions
        .iter()
        .filter(|(k, v)| {
            v.0 == 'A'
                && !k.contains(":validator-keyword:")
                && !supplemental.contains(k.as_str())
                && !later.contains(k.as_str())
        })
        .map(|(k, _)| k)
        .collect();
    assert_eq!(keys.len(), A_ASSIGNED);
    let joined = keys
        .iter()
        .map(|k| k.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    assert_eq!(oracle_blobs::sha256_hex(joined.as_bytes()), A_KEYS_SHA256);
    // the top-level modules of A (RU-044 rows are the only exception to the module rule)
    let modules: BTreeSet<&str> = parsed
        .partitions
        .values()
        .filter(|v| v.0 == 'A')
        .filter_map(|v| v.1.strip_prefix("module:"))
        .collect();
    assert_eq!(
        modules,
        BTreeSet::from([
            "mycelium_a13_macos",
            "mycelium_live",
            "mycelium_node",
            "mycelium_platform_neutral",
            "physical_inference_node",
            "scripts",
        ])
    );
    let basis = |b: &str| {
        parsed
            .partitions
            .values()
            .filter(|v| v.0 == 'A' && v.1 == b)
            .count()
    };
    assert_eq!((basis("ru-044"), basis("ru-044-discovery")), (8, 3));
    // the six round-14 census rows carry their module basis
    assert_eq!(
        keys.iter().filter(|k| k.contains(":manual-bound:")).count(),
        3 + 6
    );
}

#[test]
fn r17_supplement_is_separate_from_the_unchanged_original_a_cohort() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    for key in r17_manual_keys() {
        assert_eq!(parsed.partitions[key].0, 'A');
        let e = parsed.entries.iter().find(|e| e.key == key).unwrap();
        assert_eq!(
            (&*e.verdict, &*e.reviewer),
            ("bounded", "L1b-r17-tuple-floor")
        );
        assert!(e.target.starts_with("PyR17A4TupleFloor"));
    }
}

#[test]
fn partition_a_verdicts_are_the_pinned_set_by_kind() {
    let entries = a_entries();
    assert_eq!(entries.len(), A_ADJUDICATED);
    let kinds = |reviewer: &str| {
        let mut by: BTreeMap<&str, usize> = BTreeMap::new();
        for e in entries.iter().filter(|e| e.reviewer == reviewer) {
            *by.entry(e.verdict.as_str()).or_default() += 1;
        }
        by
    };
    assert_eq!(
        kinds("L1b-r13"),
        BTreeMap::from([
            ("bounded", 15),
            ("derived", 7),
            ("not-a-bound", 3),
            ("not-executed", 51)
        ])
    );
    assert_eq!(
        kinds("L1b-r14"),
        BTreeMap::from([
            ("bounded", 17),
            ("derived", 227),
            ("not-a-bound", 26),
            ("test-only", 5)
        ])
    );
    // round 15: 15 derived (11 evidence-validity instants -> the 120 s node snapshot, the 7200 s edge observation and the
    // 21600 s stage-pack lease ceiling; the 2 A10 route deadlines; the 2 extra rate and close-wait rows), 1 bounded
    // (the executed close-wait allowance), 3 not-a-bound (observation / carrier sinks) and 3 `source-unbounded`
    // (the first users of the round-15 verdict kind).
    assert_eq!(
        kinds("L1b-r15"),
        BTreeMap::from([
            ("bounded", 1),
            ("derived", 15),
            ("not-a-bound", 3),
            ("source-unbounded", 3)
        ])
    );
    assert!(entries.iter().all(|e| matches!(
        e.reviewer.as_str(),
        "L1b-r13" | "L1b-r14" | "L1b-r15" | "L1b-r16-legacy"
    )));
}

/// The manual verdicts, with the independent reason each is right (oracle lines are pinned by the citations).
const MANUAL: &[(&str, &str, &str)] = &[
    (
        "mycelium_platform_neutral/migration.py:622:46:",
        "not-a-bound",
        "-",
    ),
    (
        "mycelium_platform_neutral/thermal.py:38:0:",
        "not-a-bound",
        "-",
    ),
    ("mycelium_a13_macos/worker.py:1516:64:", "not-a-bound", "-"),
    (
        "mycelium_a13_macos/swarm_publish.py:397:16:",
        "derived",
        "PyMyceliumA13MacosSwarmPublishHeartbeatSecondsL42",
    ),
    (
        "mycelium_a13_macos/endpoint_recovery.py:32:15:",
        "bounded",
        "PyMyceliumA13MacosEndpointRecoveryIssuedSkewSecondsL32",
    ),
    (
        "mycelium_a13_macos/swarm_publish.py:296:45:",
        "bounded",
        "PyMyceliumA13MacosSwarmPublishGrantHoursMaximumL296",
    ),
    (
        "mycelium_a13_macos/app.py:811:",
        "derived",
        "PyFinalMyceliumA13MacosPairingWindowTtlMaxL453C0",
    ),
    (
        "mycelium_a13_macos/app.py:832:",
        "derived",
        "PyFinalMyceliumA13MacosPairingWindowTtlMaxL453C0",
    ),
    (
        "mycelium_a13_macos/app.py:1715:",
        "derived",
        "PyFinalMyceliumA13MacosPairingWindowTtlMaxL453C0",
    ),
    (
        "mycelium_a13_macos/app.py:1597:",
        "derived",
        "PyUpperGuardMyceliumA13MacosAppL704C59",
    ),
    (
        "mycelium_a13_macos/swarm_stats.py:114:",
        "bounded",
        "PyMyceliumA13MacosSwarmStatsWindowEndMaximumAgeSecondsL114",
    ),
    (
        "physical_inference_node.py:4183:",
        "bounded",
        "PyMyceliumPhysicalInferenceNodeMaximumObservedWorkUnitMsUpperL4183",
    ),
    (
        "mycelium_node/token_provisioning.py:169:8:",
        "derived",
        "PyUpperGuardMyceliumNodeTokenProvisioningL183C55",
    ),
    (
        "mycelium_node/token_provisioning.py:289:12:",
        "derived",
        "PyUpperGuardMyceliumNodeTokenProvisioningL183C55",
    ),
];

#[test]
fn the_fourteen_manual_verdicts_are_exactly_these() {
    let entries: Vec<_> = a_entries()
        .into_iter()
        .filter(|e| e.reviewer == "L1b-r13")
        .collect();
    assert_eq!(entries.len(), R13_ADJUDICATED);
    for (prefix, verdict, target) in MANUAL {
        let found: Vec<_> = entries
            .iter()
            .filter(|e| e.key.starts_with(prefix))
            .collect();
        assert_eq!(found.len(), 1, "{prefix}");
        assert_eq!(
            (found[0].verdict.as_str(), found[0].target.as_str()),
            (*verdict, *target),
            "{prefix}"
        );
    }
    // everything else in A is RU-044 (11 bounded rows) or RU-046 (51 not-executed)
    let manual_keys: BTreeSet<_> = entries
        .iter()
        .filter(|e| MANUAL.iter().any(|m| e.key.starts_with(m.0)))
        .map(|e| &e.key)
        .collect();
    assert_eq!(manual_keys.len(), MANUAL.len());
    let rest = entries.len() - manual_keys.len();
    assert_eq!(rest, 8 + 3 + 51);
}

#[test]
fn a_not_executed_row_cites_only_its_own_line_and_its_file_is_unreached() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let census: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for e in a_entries().iter().filter(|e| e.verdict == "not-executed") {
        let c = &census[&e.key];
        assert_eq!(parsed.launch[&c.path].0, "unreached", "{}", e.key);
        assert_eq!(e.citations.split(" | ").count(), 1, "{}", e.key);
        assert!(
            e.citations
                .starts_with(&format!("{}:{} @ f4e366e9 sha256=", c.path, c.line)),
            "{}",
            e.key
        );
    }
}

#[test]
fn partition_a_remaining_is_assigned_minus_adjudicated() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let supplemental = r17_manual_keys();
    let later = r29_retry_admission::keys();
    let done: BTreeSet<_> = a_entries().into_iter().map(|e| e.key).collect();
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|b| b.key).collect();
    let remaining = parsed
        .partitions
        .iter()
        .filter(|(k, v)| {
            v.0 == 'A'
                && !k.contains(":validator-keyword:")
                && !supplemental.contains(k.as_str())
                && !later.contains(k.as_str())
                && !done.contains(*k)
                && !reviewed.contains(*k)
        })
        .count();
    assert_eq!(remaining, A_ASSIGNED - A_ADJUDICATED);
    assert_eq!(remaining, 0); // R16: both legacy dynamic domains now have negative proofs.
}

/// Round-14 registry ids: `(id, value)`, each the figure v0.1.4 enforces at the cited site. Values that are
/// computed rather than literal are pinned against the FX files recorded from EXECUTING the oracle (below).
fn r14_ids() -> Vec<(&'static str, budgets::table::Value)> {
    use budgets::table::Value::{Duration, Storage};
    use budgets::{Bytes, Span};
    let d = |ms| Duration(Span::from_millis(ms));
    let s = |bytes| Storage(Bytes::new(bytes));
    vec![
        (
            "PyMyceliumA13MacosStandingConsentMaxWallSecondsMaximumL102",
            d(3_600_000),
        ),
        (
            "PyMyceliumA13MacosStandingConsentMaxMemoryBytesMaximumL53",
            s(512 * 1024 * 1024 * 1024),
        ),
        (
            "PyMyceliumA13MacosModelResourcesStorageConsentCeilingL406",
            s(32_568_709_325),
        ),
        (
            "PyMyceliumA13MacosAppConsentValidStorageBytesFloorL251",
            s(100_000_000),
        ),
        (
            "PyMyceliumNodeTokenProvisioningProcessCeilingSecondsL148",
            d(1_800_000),
        ),
        (
            "PyPhysicalInferenceNodeCommandInterruptionAndCleanupMaxMsExecutedL315",
            d(8_000),
        ),
        (
            "PyPhysicalInferenceNodeMaximumImportedLeaseSecondsExecutedL3378",
            d(425_000),
        ),
        (
            "PyPhysicalInferenceNodeProgressWriteWaitSliceSecondsMaximumL6576",
            d(50),
        ),
        (
            "PyMyceliumA13MacosDemoChainMemberWallCapSecondsMaximumL836",
            d(3_600_000),
        ),
        (
            "PyMyceliumA13MacosModelResourcesMemberRssBytesMaximumExecutedL326",
            s(36_813_347_745),
        ),
        (
            "PyMyceliumA13MacosModelResourcesMemberDiskBytesMaximumExecutedL327",
            s(32_568_709_325),
        ),
        (
            "PyMyceliumA13MacosModelResourcesProposalRssBytesMaximumExecutedL343",
            s(73_626_695_490),
        ),
        (
            "PyMyceliumA13MacosChainCompileApprovalAdditionalDiskBytesMaximumExecutedL257",
            s(65_137_418_650),
        ),
        (
            "PyMyceliumA13MacosChainCompileApprovalExecutionDownloadBytesL256",
            s(0),
        ),
        (
            "PyMyceliumA13MacosCapabilityLinkProbeBulkBytesMaximumL166",
            s(262_144),
        ),
        (
            "PyMyceliumA13MacosAdaptiveHostPublicationLifetimeMaximumL907",
            d(3_600_000),
        ),
        (
            "PyMyceliumLiveRouteMembershipSnapshotValidUntilSecondsMaximumL1310",
            d(3_600_000),
        ),
    ]
}

/// The 24 rows of partition A that stay pending, by file, with the reason (round 14 report):
/// evidence-validity instants minted by signed observations (v0.1.4 checks only valid_until > observed, no
/// ceiling: local_preparer 3, preparation 1, stage_pack_builder 2, assemble_m17 5), a rate-limited artifact wait
/// and a response rate (a rate >= 1 B/s with no upper bound: artifact_provisioner 1, artifact_transport 3),
/// the peer-count dependent close-wait formula (chain_compile 2, owner_run 1 with float('inf') budgets,
/// demo_chain 2), route command deadlines with ~25 producers (route 1, supervisor 2) and one measured-size cache
/// cap (build_qwen 1).
const LEGACY_R16_CLOSED_BY_FILE: [(&str, usize); 2] = [
    // the command deadline forwarded by PhysicalNodeProcess.send_before: ~25 producers, process.command() checks only > 0
    ("mycelium_live/route.py", 1),
    // a cache cap that is 2 x the measured size of the staged files: no static ceiling, an owner question (class O)
    ("scripts/build_qwen_live_route.py", 1),
];

const FX_STORAGE: &str = include_str!("../data/fx-l1b-storage-consent-ceiling.tsv");
const FX_RESOURCES: &str = include_str!("../data/fx-l1b-model-resource-ceilings.tsv");
const FX_NODE: &str = include_str!("../data/fx-l1b-node-computed-ceilings.tsv");

/// Proves: B-LIM-01 (one typed table holds every source-bound value with the oracle's number and unit; here the
/// 16 ids round 14 adds for partition A, six of them computed values recorded from EXECUTING the v0.1.4 oracle
/// at f4e366e9) and, for the deadline registry, B-LIM-04 (the executed COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS
/// = 8000 ms equals the table). B-LIM-02 and B-LIM-03 are mapping and calibration behaviours that a
/// source-value table test cannot prove.
#[test]
fn round_14_ids_equal_the_oracle_values_and_the_executed_probe_records() {
    use budgets::table::{BUDGET_TABLE, Value};
    use budgets::{Bytes, Span};
    let table: BTreeMap<&str, Value> = BUDGET_TABLE
        .iter()
        .map(|e| (e.id.name(), e.value))
        .collect();
    let ids = r14_ids();
    assert_eq!(ids.len(), 17);
    for (id, want) in &ids {
        assert_eq!(table.get(id), Some(want), "{id}");
    }
    let fx = |text: &'static str, key: &str| -> u64 {
        text.lines()
            .find_map(|l| {
                let mut c = l.split('\t');
                (c.next() == Some(key)).then(|| c.next().expect("value column").to_string())
            })
            .unwrap_or_else(|| panic!("{key} missing from the fx file"))
            .parse::<f64>()
            .expect("number") as u64
    };
    // storage consent ceiling = max over the registered models of the executed model_storage_ceiling
    let storage = fx(FX_STORAGE, "MAX(storage_consent_ceiling)");
    assert_eq!(
        table["PyMyceliumA13MacosModelResourcesStorageConsentCeilingL406"],
        Value::Storage(Bytes::new(storage))
    );
    // per-member and proposal ceilings: MAX row of the executed model_resources probe
    let max_row: Vec<u64> = FX_RESOURCES
        .lines()
        .find(|l| l.starts_with("MAX\t"))
        .expect("MAX row")
        .split('\t')
        .skip(1)
        .take(5)
        .map(|v| v.parse().expect("integer"))
        .collect();
    for (id, bytes) in [
        (
            "PyMyceliumA13MacosModelResourcesMemberRssBytesMaximumExecutedL326",
            max_row[0],
        ),
        (
            "PyMyceliumA13MacosModelResourcesMemberDiskBytesMaximumExecutedL327",
            max_row[1],
        ),
        (
            "PyMyceliumA13MacosModelResourcesProposalRssBytesMaximumExecutedL343",
            max_row[2],
        ),
        (
            "PyMyceliumA13MacosChainCompileApprovalAdditionalDiskBytesMaximumExecutedL257",
            max_row[3],
        ),
    ] {
        assert_eq!(table[id], Value::Storage(Bytes::new(bytes)), "{id}");
    }
    assert_eq!(max_row[4], 1_800); // every registered model proposes 1800 s; WorkerDefaultWall is 1_800_000 ms
    assert_eq!(
        table["WorkerDefaultWall"],
        Value::Duration(Span::from_millis(max_row[4] * 1_000))
    );
    // executed deadline constants
    assert_eq!(
        table["PyPhysicalInferenceNodeCommandInterruptionAndCleanupMaxMsExecutedL315"],
        Value::Duration(Span::from_millis(fx(
            FX_NODE,
            "COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS"
        )))
    );
    assert_eq!(
        table["PyPhysicalInferenceNodeMaximumImportedLeaseSecondsExecutedL3378"],
        Value::Duration(Span::from_millis(
            fx(FX_NODE, "maximum_imported_lease_seconds") * 1_000
        ))
    );
    // the 420 s lease ceiling plus the 5 s clock skew is the 425 s above
    assert_eq!(fx(FX_NODE, "kv_lease_ceiling_seconds(30.0)") + 5, 425);
}

const FX_CLOSE_WAIT: &str = include_str!("../data/fx-l1b-close-wait-sum.tsv");
const BLOCKED_IDS_R15: [(&str, &str); 3] = [
    (
        "mycelium_live/artifact_transport.py:72:12:positional-call:time.sleep",
        "PySourceUnboundedAdjArtifactTransportL72",
    ),
    (
        "mycelium_live/artifact_provisioner.py:82:4:assignment:deadline",
        "PySourceUnboundedAdjArtifactProvisionerL82",
    ),
    (
        "mycelium_a13_macos/owner_run.py:116:8:assignment:deadline",
        "PySourceUnboundedAdjOwnerRunL116",
    ),
];

/// Proves: B-LIM-01 (the table holds the stop_serving close-wait allowance at the host-count ceiling with the
/// oracle's number: 13.5 + 1890 x 8 peers = 15133.5 s, recorded from EXECUTING `close_wait_seconds` in the oracle
/// at f4e366e9, fx-l1b-close-wait-sum.tsv) and B-LIM-04 only for that one deadline value.
#[test]
fn round_15_close_wait_id_equals_the_executed_formula_at_the_host_ceiling() {
    use budgets::Span;
    use budgets::table::{BUDGET_TABLE, Value};
    let fx = |key: &str| -> f64 {
        FX_CLOSE_WAIT
            .lines()
            .find(|l| l.starts_with(&format!("{key}\t")))
            .and_then(|l| l.split('\t').nth(1))
            .unwrap_or_else(|| panic!("{key}"))
            .parse()
            .expect("number")
    };
    // the executed formula is linear in the peer count: 0.5 + 4 + 8 + 1 + peers * (2 * 930 + 30)
    for peers in 0..=8u32 {
        let want = 0.5 + 4.0 + 8.0 + 1.0 + f64::from(peers) * (2.0 * 930.0 + 30.0);
        assert_eq!(fx(&format!("close_wait_seconds(peers={peers})")), want);
    }
    assert_eq!(fx("MAX_HOSTS"), 8.0);
    let max_ms = (fx("close_wait_seconds(peers=8)") * 1_000.0) as u64;
    let entry = BUDGET_TABLE
        .iter()
        .find(|e| {
            e.id.name() == "PyMyceliumA13MacosChainCompileStopServingCloseWaitMaxHostsExecutedL460"
        })
        .expect("registered");
    assert_eq!(entry.value, Value::Duration(Span::from_millis(max_ms)));
    assert_eq!(max_ms, 15_133_500);
}

#[test]
fn round_15_source_unbounded_verdicts_are_the_pinned_three_with_their_own_blocked_ids() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let verdicts: BTreeMap<&str, &str> = a_entries_ref(&parsed)
        .filter(|e| e.verdict == adj::SOURCE_UNBOUNDED && e.reviewer == "L1b-r15")
        .map(|e| (e.key.as_str(), e.target.as_str()))
        .collect();
    assert_eq!(verdicts, BTreeMap::from(BLOCKED_IDS_R15));
    // every proof row of partition A belongs to one of them: 3 keys, hops + scans + occurrences
    let proofs = partition_a_proofs(&parsed);
    let keys: BTreeSet<&str> = proofs.keys().copied().collect();
    assert_eq!(keys, BLOCKED_IDS_R15.iter().map(|(k, _)| *k).collect());
    let count = |kind: &str| {
        proofs
            .values()
            .flat_map(|rows| rows.iter())
            .filter(|p| p.kind == kind)
            .count()
    };
    assert_eq!(
        (count("hop"), count("scan"), count("occurrence")),
        (15, 13, 45)
    );
    // the Blocked ids are registered exactly once and carry the site, the pin and V03
    for (key, id) in BLOCKED_IDS_R15 {
        let view = adj::registry(id).expect("registered");
        let site = key.split(':').take(2).collect::<Vec<_>>().join(":");
        let text = view.blocked.expect("a Blocked entry");
        assert!(
            text.starts_with(&format!("v0.1.4 ships no ceiling at {site} @ f4e366e9;"))
                && text.contains("V03"),
            "{id}: {text}"
        );
    }
}

fn partition_a_proofs(parsed: &adj::Parsed) -> BTreeMap<&str, &Vec<adj::Proof>> {
    parsed
        .proofs
        .iter()
        .filter(|(key, _)| {
            parsed.partitions[*key].0 == 'A'
                && parsed
                    .entries
                    .iter()
                    .any(|e| &e.key == *key && e.reviewer == "L1b-r15")
        })
        .map(|(key, rows)| (key.as_str(), rows))
        .collect()
}

#[test]
fn partition_a_proof_pin_is_unchanged_when_partition_b_adds_proofs() {
    let mut parsed = adj::parse(&adj::TABLES).unwrap();
    let before = partition_a_proofs(&parsed)
        .keys()
        .map(|k| (*k).to_owned())
        .collect::<BTreeSet<_>>();
    let b_key = parsed
        .partitions
        .iter()
        .find(|(_, p)| p.0 == 'B')
        .unwrap()
        .0
        .clone();
    let proof = parsed.proofs.values().next().unwrap().clone();
    parsed.proofs.insert(b_key, proof);
    let after = partition_a_proofs(&parsed)
        .keys()
        .map(|k| (*k).to_owned())
        .collect::<BTreeSet<_>>();
    assert_eq!(
        after, before,
        "B proof rows must not be charged to A's exact pin"
    );
}

fn a_entries_ref(parsed: &adj::Parsed) -> impl Iterator<Item = &adj::Entry> {
    parsed.entries.iter().filter(|e| e.partition == 'A')
}

/// Every round-14 verdict row of partition A is a rationale-bearing row whose reviewer is the round id, every
/// new id is used (python_inventory checks the orphan rule) and the pending remainder is the listed families.
#[test]
fn round_14_remainder_is_the_pinned_set_of_open_families() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let supplemental = r17_manual_keys();
    let later = r29_retry_admission::keys();
    let done: BTreeSet<_> = a_entries().into_iter().map(|e| e.key).collect();
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|b| b.key).collect();
    let mut families: BTreeMap<String, usize> = BTreeMap::new();
    for (key, v) in &parsed.partitions {
        if v.0 == 'A'
            && !key.contains(":validator-keyword:")
            && !supplemental.contains(key.as_str())
            && !later.contains(key.as_str())
            && !done.contains(key)
            && !reviewed.contains(key)
        {
            let path = key.split(':').next().unwrap();
            *families.entry(path.to_string()).or_default() += 1;
        }
    }
    assert_eq!(families.values().sum::<usize>(), 0);
    let mut closed = BTreeMap::new();
    for e in parsed
        .entries
        .iter()
        .filter(|e| e.reviewer == "L1b-r16-legacy")
    {
        assert_eq!(e.verdict, "source-unbounded");
        *closed
            .entry(e.key.split(':').next().unwrap().to_string())
            .or_insert(0usize) += 1;
    }
    assert_eq!(
        closed,
        BTreeMap::from(LEGACY_R16_CLOSED_BY_FILE.map(|(f, n)| (f.to_string(), n)))
    );
}
