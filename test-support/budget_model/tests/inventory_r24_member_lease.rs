// SPDX-License-Identifier: AGPL-3.0-only
//! Three exact member-lease sites; original invite expiry is not their producer.
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
#[path = "common/r36_stats_admission.rs"]
#[allow(dead_code, reason = "exact three-key historical projection")]
mod r36_stats_admission;
#[path = "common/r50_recovery_forward_admission.rs"]
#[allow(dead_code, reason = "exact two-key historical projection")]
mod r50_recovery_forward_admission;
use budget_model::inventory::{adjudication as adj, candidates};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
#[path = "common/r24_member_lease_admission.rs"]
mod r24_member_lease_admission;
#[path = "common/r26_seed_inputs_admission.rs"]
mod r26_seed_inputs_admission;
#[path = "common/r28_seed_http_admission.rs"]
mod r28_seed_http_admission;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;
const KEYS: [&str; 3] = [
    "mycelium_seed/coordinator.py:280:24:keyword:lease_expires_at",
    "mycelium_seed/coordinator.py:1118:20:keyword:lease_expires_at",
    "mycelium_seed/coordinator.py:1243:12:assignment:member.lease_expires_at",
];
#[test]
fn exact_member_lease_contracts_are_independently_proven() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in KEYS {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == key)
            .expect("r24 exact member lease needs its independent proof");
        assert_eq!(e.verdict, "source-unbounded");
        let r = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
        assert_eq!(r.unit, "not-applicable");
        assert!(r.value.is_none());
        assert!(r.budget.is_empty());
    }
}

const IDS: [&str; 3] = [
    "PySourceUnboundedR24SeedMemberLeaseL280C24",
    "PySourceUnboundedR24SeedMemberLeaseL1118C20",
    "PySourceUnboundedR24SeedMemberLeaseL1243C12",
];
const PROOF_HASHES: [&str; 3] = [
    "467dc8a7bbda2ca3b8bf9ac18a5d770655c8d2d37e44bcc1d69a9f7f32c06fb0",
    "7fbf3359714582c92a778a85f475dbcdcbf42f4dc07448655d9a770e7e8e7fad",
    "9eb00f60cf204ab44e39f5782ad2630fba7ff6dcd6458a4283e4d3c8c3f68e6e",
];
const SOURCE_PINS: [(&str, &str); 4] = [
    (
        "mycelium_membership/contracts.py",
        "df93138166c4d08fd51c5cedb260e50536b3b3343840caeaab9a1a008d018ab2",
    ),
    (
        "mycelium_seed/coordinator.py",
        "2612a51acce2d43626ec69cd7dc19db080e595915c82030d3ec8d5f7a4456f15",
    ),
    (
        "mycelium_seed/placement.py",
        "be0512753894566e18d98a0664b71b2e04f7e8bebab14e99ef5cbf175769a478",
    ),
    (
        "mycelium_seed/state.py",
        "2d1087ab257241bcc2a130092becbe0ae4748f0df1b89d511c92b2d18e706af5",
    ),
];
#[test]
fn exact_three_record_metadata_and_prior_gap_are_pinned() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let own = r24_member_lease_admission::admitted(&p);
    assert_eq!(own, BTreeSet::from(KEYS));
    for (key, id) in KEYS.into_iter().zip(IDS) {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        assert_eq!(e.target, id);
        for other in KEYS.into_iter().filter(|other| *other != key) {
            let mut bad = e.clone();
            bad.key = other.to_string();
            assert!(!r24_member_lease_admission::exact(&bad));
        }
        for field in ["rationale", "citations", "reviewer", "target"] {
            let mut bad = e.clone();
            match field {
                "rationale" => bad.rationale.push_str(" forged"),
                "citations" => bad.citations.push_str(" forged"),
                "reviewer" => bad.reviewer.push_str(" forged"),
                _ => bad.target.push_str(" forged"),
            };
            assert!(!r24_member_lease_admission::exact(&bad));
        }
    }
    let prior = include_str!("../data/python-r24-prior-pending.tsv");
    assert_eq!(oracle_blobs::sha256_hex(prior.as_bytes()), PRIOR_HASH);
    let before: BTreeSet<_> = prior.lines().skip(1).map(str::to_string).collect();
    assert_eq!(before.len(), 75);
    let mut all = budget_model::inventory::candidate_keys().unwrap();
    let mut reviewed: BTreeSet<_> = budget_model::inventory::all_reviewed()
        .unwrap()
        .into_iter()
        .map(|r| r.key)
        .collect();
    r25_count_admission::remove_from(&mut all, &mut reviewed);
    r27_delivery_wake_admission::remove_from(&mut all, &mut reviewed);
    r30_workflow_admission::remove_from(&mut all, &mut reviewed);
    r32_verifier_admission::remove_from(&mut all, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    r29_retry_admission::remove_from(&mut all, &mut reviewed);
    let after: BTreeSet<_> = all.difference(&reviewed).cloned().collect();
    let mut r26 = r26_seed_inputs_admission::admitted(&p);
    let r28 = r28_seed_http_admission::admitted(&p);
    assert!(r26.is_disjoint(&r28));
    r26.extend(r28);
    let r31 = r31_seed_admission::admitted(&p);
    assert!(r26.is_disjoint(&r31));
    r26.extend(r31);
    assert!(own.is_disjoint(&r26));
    let own: BTreeSet<_> = own
        .into_iter()
        .chain(r26.iter().copied())
        .map(str::to_string)
        .collect();
    assert_eq!(after, before.difference(&own).cloned().collect());
    assert_eq!(
        (all.len(), reviewed.len(), after.len()),
        (8252, 8180 + r26.len(), 72 - r26.len())
    );
    for key in [
        "mycelium_seed/coordinator.py:1310:20:assignment:request_expiry",
        "mycelium_seed/coordinator.py:1385:20:keyword:previous_deadline",
    ] {
        assert!(r26.contains(key) && !after.contains(key));
    }
}
#[test]
fn complete_member_writer_decoder_renewal_and_consumer_source_is_pinned() {
    let p = adj::parse(&adj::TABLES).unwrap();
    for (key, expected) in KEYS.into_iter().zip(PROOF_HASHES) {
        let proof: String = adj::TABLES
            .proofs_b
            .lines()
            .filter(|l| l.split('\t').next() == Some(key))
            .map(|l| format!("{l}\n"))
            .collect();
        assert_eq!(oracle_blobs::sha256_hex(proof.as_bytes()), expected);
    }
    oracle_blobs::oracle_dir_or_panic();
    let paths: Vec<_> = SOURCE_PINS.iter().map(|(p, _)| p.to_string()).collect();
    let src = oracle_blobs::blobs(&paths);
    for (path, expected) in SOURCE_PINS {
        assert_eq!(oracle_blobs::sha256_hex(src[path].as_bytes()), expected);
    }
    for key in KEYS {
        for h in &p.proofs[key] {
            if h.kind != "scan" {
                assert_eq!(
                    oracle_blobs::sha256_hex(
                        oracle_blobs::line_of(&src[&h.path], h.line)
                            .unwrap()
                            .as_bytes()
                    ),
                    p.pins[&(h.path.clone(), h.line)]
                );
            } else {
                let found: BTreeSet<_> = src[&h.path]
                    .lines()
                    .enumerate()
                    .filter(|(_, l)| l.contains(&h.needle))
                    .map(|(i, _)| i + 1)
                    .collect();
                let expected: BTreeSet<_> = p.proofs[key]
                    .iter()
                    .filter(|r| r.kind == "occurrence" && r.path == h.path && r.needle == h.needle)
                    .map(|r| r.line)
                    .collect();
                assert_eq!(found, expected);
                assert_eq!(found.len(), h.line);
            }
        }
    }
}
#[test]
fn wrong_id_unit_value_origin_missing_producer_and_ceiling_are_refused() {
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in KEYS {
        let p = adj::parse(&adj::TABLES).unwrap();
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        assert!(
            adj::row_of(&p, e, &cs, &|_| adj::registry(
                "PySourceUnboundedR23SeedInviteExpiryL690C20"
            ))
            .is_err()
        );
        // Use real registry views: a numeric entry cannot also carry Blocked provenance.
        // Impossible synthetic views are outside registry()'s invariant, not source input.
        assert!(adj::row_of(&p, e, &cs, &|_| adj::registry("CancelCleanup")).is_err());
        let mut view = adj::registry(&e.target).unwrap();
        view.blocked = Some("wrong origin".to_string());
        assert!(adj::row_of(&p, e, &cs, &|_| Some(view.clone())).is_err());
        let mut p = adj::parse(&adj::TABLES).unwrap();
        let e = p.entries.iter().find(|e| e.key == key).unwrap().clone();
        p.proofs
            .get_mut(key)
            .unwrap()
            .retain(|r| r.verdict != "origin");
        assert!(
            adj::row_of(&p, &e, &cs, &adj::registry)
                .unwrap_err()
                .contains("unbounded-proof-origin")
        );
        let old = format!(
            "{key}\toccurrence\tmycelium_seed/placement.py\t113\tlease_expires_at\ttype-guard"
        );
        assert_eq!(adj::TABLES.proofs_b.matches(&old).count(), 1);
        let proofs = adj::TABLES
            .proofs_b
            .replace(&old, &old.replace("\ttype-guard", "\tceiling"));
        assert!(
            adj::parse(&adj::Tables {
                proofs_b: &proofs,
                ..adj::TABLES
            })
            .is_err()
        );
    }
}
#[test]
fn real_saved_lease_replay_max_and_legitimate_consumers_are_distinct() {
    let fixture = include_str!("../data/fx-l1b-r24-member-lease.tsv");
    assert_eq!(oracle_blobs::sha256_hex(fixture.as_bytes()), FIXTURE_HASH);
    let rows: Vec<_> = fixture
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .collect();
    assert_eq!(rows.len(), 17);
    assert!(rows.iter().all(|r| r.len() == 4));
    for i in [0, 1, 2, 3, 4, 12, 13, 14, 16] {
        assert_eq!(rows[i][2], "accepted");
    }
    for i in [5, 6, 7, 8, 9, 10, 11, 15] {
        assert_eq!(rows[i][2], "refused");
    }
    assert!(rows[2][3].contains("\"decoded\":1e+100"));
    assert!(rows[4][3].contains("\"decoded\":-1e+100"));
    assert!(rows[3][3].contains("\"consumer\":1e+100"));
    assert_eq!(rows[5][3], "\"seed_member_lease_expired\"");
    for row in &rows[6..12] {
        assert_eq!(row[3], "\"seed_state_corrupt\"");
    }
    assert!(rows[13][3].contains("\"after_max\":1e+100"));
    assert!(rows[13][3].contains("\"renewal\":1400.0"));
    assert!(rows[14][3].contains("\"after_max\":1400.0"));
    assert!(rows[14][3].contains("\"persisted\":-1e+100"));
    assert_eq!(rows[15][3], "\"seed_deadline_unavailable\"");
    assert!(rows[16][3].contains("\"restored\":1400.0"));
}
const PRIOR_HASH: &str = "f5b5a4eb24b646f4bffb376948e12a4fea3c3c919237c4ad351ebbadac5d1eb7";
const FIXTURE_HASH: &str = "1b09c033d7fe0be0b56ecf736b603a0fead292598462b5d5de43982a2bf81f7d";
