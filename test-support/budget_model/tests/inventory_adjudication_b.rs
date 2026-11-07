// SPDX-License-Identifier: AGPL-3.0-only
//! Partition B of the manual adjudication (lane L1b-B, round 1): pinned counts, the oracle-executed fixture values behind the
//! partition-B registry ids, the "wrong citation is refused" control, and per-family structural checks. Partition A's lane owns
//! `inventory_adjudication_a.rs`; this file touches only B's rows (`python-adjudication-b.tsv`, `python-oracle-line-pins-b.tsv`).
//! GENERATED COUNTS: regenerate with the lane tool when verdicts are added (every count below is re-derived from the TSV by the
//! test, never from the implementation's own output).
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared test helper: each test binary uses a subset"
)]
mod oracle_blobs;
#[path = "common/r21_ratio_admission.rs"]
mod r21_ratio_admission;
#[path = "common/r23_seed_invite_admission.rs"]
mod r23_seed_invite_admission;
#[path = "common/r24_member_lease_admission.rs"]
mod r24_member_lease_admission;
#[path = "common/r26_seed_inputs_admission.rs"]
mod r26_seed_inputs_admission;
#[path = "common/r28_seed_http_admission.rs"]
mod r28_seed_http_admission;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact historical four-discovery/five-review projection"
)]
mod r32_verifier_admission;

use budget_model::inventory::{adjudication as adj, candidates};
use std::collections::{BTreeMap, BTreeSet};

/// SHA-256 of the sorted partition-B keys joined by `\n` (334 rows).
const B_KEYS_SHA256: &str = "225a0830ed2ecea102f46ed92335da44257e82eaa6b92a2fae729aedb167bc51";
const B_ASSIGNED: usize = 334;
/// 30 `not-executed` rows by L1b round 13, plus this lane's verdicts.
const B_ADJUDICATED: usize = 281;
const B_BY_VERDICT: &[(&str, usize)] = &[
    ("bounded", 4),
    ("derived", 43),
    ("not-a-bound", 117),
    ("not-executed", 30),
    ("source-unbounded", 87),
];
const B_BY_REVIEWER: &[(&str, usize)] = &[("L1b-B-r1", 160), ("L1b-B-r2", 91), ("L1b-r13", 30)];
/// Registry target -> number of `bounded`/`derived` verdicts naming it.
const B_TARGETS: &[(&str, usize)] = &[
    ("CancelAndCleanup", 1),
    ("PyFinalMyceliumA13LinuxOnboardingRequestTtlMaxL65C0", 1),
    ("PyHelperMyceliumRouterA10MemberIsolationL310C9", 1),
    ("PyMyceliumA7CheckpointChannelSealedCeilingL260", 1),
    ("PyMyceliumA7FenceStoreWin32ReadOwnerFileMaxBytesL154", 1),
    ("PyMyceliumA7NodeWiringMaxCheckpointBytesL64", 17),
    ("PyMyceliumInteractiveSwarmMaxLongPollSecondsL43", 1),
    ("PyMyceliumProductSpineStateMaxStateBytesL18", 1),
    ("PyMyceliumRouterTokenPathAuthorityNonReceiptMaxBytesL78", 1),
    (
        "PyMyceliumRouterTransportsIrohPrefillstallpolicyStallCeilingSecondsL174",
        7,
    ),
    ("PyMyceliumSeedRouteAssignmentMaxAssignmentTtlSecondsL55", 1),
    ("PyMyceliumSpeculativeDecodeDeadlineMaximumMsL76", 2),
    ("PyMyceliumSwarmArtifactsMaxClockSkewMsL27", 1),
    (
        "PyMyceliumSwarmDirectoryDirectoryLimitsMaxSwarmsPerOwnerL40",
        1,
    ),
    ("PyMyceliumUiGatewayUpstreamQueueChunksCeilingL184", 1),
    (
        "PyShiftMyceliumRouterA10TargetVerificationScaffoldMaximumVerificationBytesL58C0",
        2,
    ),
    ("PyThresholdMyceliumGossipSchemaL453C7", 2),
    ("PyUpperGuardMyceliumA7NodeWiringL171C68", 2),
    ("PyUpperGuardMyceliumInteractiveSwarmL857C11", 1),
    ("PyUpperGuardMyceliumRequestGatewayServiceL318C19", 2),
];
/// Judgement-tagged rows: J-B1 = typed instants without a row-by-row producer pin, J-B2 = carrier edges closed as not-a-bound.
const J_B1_ROWS: usize = 0;
const J_B2_ROWS: usize = 39;
const FIXTURE: &str = include_str!("../data/fx-l1b-b-oracle-values.tsv");

fn frozen_b_keys() -> BTreeSet<&'static str> {
    let keys: BTreeSet<_> = include_str!("../data/python-r17-frozen-b334.tsv")
        .lines()
        .skip(1)
        .collect();
    assert_eq!(keys.len(), B_ASSIGNED);
    assert_eq!(
        oracle_blobs::sha256_hex(
            keys.iter()
                .copied()
                .collect::<Vec<_>>()
                .join("\n")
                .as_bytes()
        ),
        B_KEYS_SHA256
    );
    keys
}

fn b_entries() -> Vec<adj::Entry> {
    let frozen = frozen_b_keys();
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let mut incremental = r21_ratio_admission::admitted(&parsed);
    let r23 = r23_seed_invite_admission::admitted(&parsed);
    assert!(incremental.is_disjoint(&r23));
    incremental.extend(r23);
    let r24 = r24_member_lease_admission::admitted(&parsed);
    assert!(incremental.is_disjoint(&r24));
    incremental.extend(r24);
    let mut r26 = r26_seed_inputs_admission::admitted(&parsed);
    let r28 = r28_seed_http_admission::admitted(&parsed);
    assert!(r26.is_disjoint(&r28));
    r26.extend(r28);
    let r31 = r31_seed_admission::admitted(&parsed);
    assert!(r26.is_disjoint(&r31));
    r26.extend(r31);
    assert!(incremental.is_disjoint(&r26));
    incremental.extend(r26);
    assert!(incremental.insert(r32_verifier_admission::OLD));
    assert!(r32_verifier_admission::reviews().contains(r32_verifier_admission::OLD));
    parsed
        .entries
        .into_iter()
        .filter(|e| {
            e.partition == 'B'
                && frozen.contains(e.key.as_str())
                && !incremental.contains(e.key.as_str())
        })
        .collect()
}

#[test]
fn partition_b_assignment_is_the_pinned_key_list() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let keys: BTreeSet<&String> = parsed
        .partitions
        .iter()
        .filter(|(k, v)| {
            v.0 == 'B'
                && !r32_verifier_admission::keys().contains(k.as_str())
                && !k.contains(":validator-keyword:")
                && !k.contains(":manual-bound:")
        })
        .map(|(k, _)| k)
        .collect();
    assert_eq!(keys.len(), B_ASSIGNED);
    let joined = keys
        .iter()
        .map(|k| k.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    assert_eq!(oracle_blobs::sha256_hex(joined.as_bytes()), B_KEYS_SHA256);
    assert_eq!(
        keys.iter().map(|k| k.as_str()).collect::<BTreeSet<_>>(),
        frozen_b_keys()
    );
    // B takes only module-basis rows: the RU-044 exceptions all went to A
    assert!(
        parsed
            .partitions
            .values()
            .filter(|v| v.0 == 'B')
            .all(|v| v.1.starts_with("module:"))
    );
}

#[test]
fn partition_b_verdicts_are_the_pinned_set_by_kind_and_reviewer() {
    let entries = b_entries();
    assert_eq!(entries.len(), B_ADJUDICATED);
    let mut by: BTreeMap<&str, usize> = BTreeMap::new();
    let mut who: BTreeMap<&str, usize> = BTreeMap::new();
    for e in &entries {
        *by.entry(e.verdict.as_str()).or_default() += 1;
        *who.entry(e.reviewer.as_str()).or_default() += 1;
    }
    assert_eq!(by, B_BY_VERDICT.iter().copied().collect());
    assert_eq!(who, B_BY_REVIEWER.iter().copied().collect());
    // every verdict key is a partition-B key (the engine also refuses wrong-partition keys)
    let parsed = adj::parse(&adj::TABLES).unwrap();
    assert!(entries.iter().all(|e| parsed.partitions[&e.key].0 == 'B'));
}

#[test]
fn partition_b_remaining_is_assigned_minus_adjudicated() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let done: BTreeSet<String> = b_entries().into_iter().map(|e| e.key).collect();
    let remaining: Vec<&String> = parsed
        .partitions
        .iter()
        .filter(|(k, v)| {
            v.0 == 'B'
                && !k.contains(":validator-keyword:")
                && !k.contains(":manual-bound:")
                && !r32_verifier_admission::keys().contains(k.as_str())
                && !done.contains(*k)
        })
        .map(|(k, _)| k)
        .collect();
    assert_eq!(remaining.len(), B_ASSIGNED - B_ADJUDICATED);
    // every remaining key is still a census candidate (nothing is adjudicated by deleting its candidate)
    let rows = candidates().unwrap();
    let census: BTreeSet<&str> = rows.iter().map(|c| c.key.as_str()).collect();
    assert!(remaining.iter().all(|k| census.contains(k.as_str())));
}

#[test]
fn bounded_and_derived_targets_are_exactly_the_pinned_registry_ids() {
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    for e in b_entries() {
        if e.verdict == "bounded" || e.verdict == "derived" {
            *seen.entry(e.target).or_default() += 1;
        }
    }
    let want: BTreeMap<String, usize> = B_TARGETS
        .iter()
        .map(|(k, v)| ((*k).to_string(), *v))
        .collect();
    assert_eq!(seen, want);
}

/// RED control: a wrong citation digest, a shifted line and a missing own-line citation in a B verdict are each refused by
/// the engine with the intended code (the table-level pin tests re-derive the digests from the oracle blobs).
#[test]
fn a_wrong_citation_in_a_b_verdict_is_refused() {
    let census = candidates().unwrap();
    let map: BTreeMap<String, budget_model::inventory::Candidate> =
        census.iter().map(|c| (c.key.clone(), c.clone())).collect();
    let first = b_entries()
        .into_iter()
        .find(|e| e.reviewer == "L1b-B-r1" && e.verdict == "not-a-bound")
        .expect("a B-lane verdict");
    let digest = first
        .citations
        .split(" sha256=")
        .nth(1)
        .unwrap()
        .split(' ')
        .next()
        .unwrap()
        .to_string();
    let forged_digest = format!("{}{}", "0".repeat(64 - 1), "1");
    assert_ne!(digest, forged_digest);
    let b_text = adj::TABLES.b;
    for (label, from, to, code) in [
        (
            "wrong digest",
            digest.clone(),
            forged_digest,
            "invalid-citation",
        ),
        (
            "wrong tag",
            " @ f4e366e9 sha256=".to_string() + &digest,
            " @ ef9ae61d sha256=".to_string() + &digest,
            "invalid-citation",
        ),
    ] {
        let forged = b_text.replacen(&from, &to, 1);
        assert_ne!(forged, b_text, "{label} must change the table");
        let tables = adj::Tables {
            b: &forged,
            ..adj::TABLES
        };
        let errors = adj::rows_with(&tables, &map, &adj::registry).unwrap_err();
        assert!(
            errors
                .iter()
                .any(|e| e.starts_with(code) && e.ends_with(&first.key)),
            "{label}: {errors:?}"
        );
    }
}

/// Proves (partial): B-LIM-01 - the numbers behind the partition-B registry ids equal EXECUTED v0.1.4 values
/// (fixture FX-L1b-B-oracle-values, probe python3.14 against f4e366e9, sha256 pinned in the fixture).
#[test]
fn registry_values_equal_the_executed_oracle_fixture() {
    let mut lines = FIXTURE.lines();
    assert_eq!(
        lines.next(),
        Some("fixture\tname\tvalue\tregistry_id\tprobe_sha256\toracle")
    );
    let mut count = 0;
    for line in lines {
        let f: Vec<&str> = line.split('\t').collect();
        assert_eq!(f.len(), 6, "{line}");
        assert_eq!(f[0], "FX-L1b-B-oracle-values");
        assert_eq!(f[5], "f4e366e98a1f705dfaf182897044500d290dbd7f");
        assert_eq!(f[4].len(), 64);
        let view = adj::registry(f[3]).unwrap_or_else(|| panic!("registry id {}", f[3]));
        assert_eq!(view.value, Some(f[2].parse().unwrap()), "{}", f[1]);
        count += 1;
    }
    assert_eq!(count, 7);
}

/// Family checks: every B-lane verdict filed under a family names a marker of that family in its symbol, expression or scope
/// (a misfiled row would not), and each family has its pinned size.
const FAMILIES: &[(&str, &str, &[&str], usize)] = &[
    (
        "Typed absolute instant (timestamp",
        "not-a-bound",
        &[
            "expire",
            "due_at",
            "deadline",
            "valid_until",
            "lease",
            "until",
            "expiry",
            "not_before",
            "cooldown",
            "renewed",
        ],
        0,
    ),
    (
        "Pure carrier edge: the row hands on a wait",
        "not-a-bound",
        &[
            "timeout",
            "ttl",
            "deadline",
            "remaining",
            "wait",
            "max(0.0",
            "window",
        ],
        39,
    ),
    (
        "v0.1.4 bounds this stall window",
        "derived",
        &["stall_seconds", "stall"],
        7,
    ),
    (
        "Proposal deadline",
        "derived",
        &["deadline_ms", "budget_s"],
        2,
    ),
    (
        "Typed absolute instant computed as clock",
        "not-a-bound",
        &[
            "deadline", "expire", "until", "lease", "cooldown", "renewed", "expires",
        ],
        29,
    ),
    (
        "v0.1.4 enforces the ceiling where the node config",
        "derived",
        &["max_bytes", "max_checkpoint", "max_entr", "max_"],
        14,
    ),
    (
        "Parameter forward of max_bytes",
        "derived",
        &["max_bytes"],
        3,
    ),
    (
        "Pure carrier edge: tuple unpack",
        "not-a-bound",
        &["limit", "daily", "swarm", "requests", "tokens", "client"],
        4,
    ),
    (
        "Typed absolute instant (monotonic deadline) popped",
        "not-a-bound",
        &["deadline"],
        3,
    ),
];

#[test]
fn judgement_tagged_rows_are_pinned_in_number() {
    let entries = b_entries();
    assert_eq!(
        entries
            .iter()
            .filter(|e| e.rationale.contains("JUDGEMENT J-B1"))
            .count(),
        J_B1_ROWS
    );
    assert_eq!(
        entries
            .iter()
            .filter(|e| e.rationale.contains("JUDGEMENT J-B2"))
            .count(),
        J_B2_ROWS
    );
}

#[test]
fn family_rows_carry_their_family_markers() {
    let census = candidates().unwrap();
    let map: BTreeMap<&str, &budget_model::inventory::Candidate> =
        census.iter().map(|c| (c.key.as_str(), c)).collect();
    let entries = b_entries();
    for (prefix, verdict, marks, expected) in FAMILIES {
        let mut checked = 0;
        for e in entries.iter().filter(|e| {
            e.reviewer == "L1b-B-r1" && e.verdict == *verdict && e.rationale.starts_with(prefix)
        }) {
            let c = map[e.key.as_str()];
            let text = format!("{} {} {}", c.symbol, c.expression, c.scope).to_lowercase();
            assert!(
                marks.iter().any(|m| text.contains(m)),
                "{prefix}: {}",
                e.key
            );
            checked += 1;
        }
        assert_eq!(checked, *expected, "{prefix}");
    }
}
