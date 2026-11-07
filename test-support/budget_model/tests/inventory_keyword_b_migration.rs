// SPDX-License-Identifier: AGPL-3.0-only
//! Exact canonical B359 plus r19 two-key admission; inherited cohorts stay immutable.
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
use budget_model::inventory::{adjudication as adj, all_reviewed};
use std::collections::BTreeSet;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
#[path = "common/r19_ratio_admission.rs"]
mod r19_ratio_admission;
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
const PROPOSAL: &str = include_str!("../data/python-kb-r1-proposals.tsv");
fn proposal() -> Vec<Vec<&'static str>> {
    let rows: Vec<Vec<_>> = PROPOSAL
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect();
    assert_eq!(rows.len(), 359);
    assert_eq!(
        oracle_blobs::sha256_hex(PROPOSAL.as_bytes()),
        "088e9e8caaa91606d1790ecf9102cd5c355ab853f7b2353fa1a3a9cedcf182d2"
    );
    rows
}
fn exact(e: &adj::Entry, f: &[&str]) -> bool {
    let line = [
        e.key.as_str(),
        e.verdict.as_str(),
        e.target.as_str(),
        e.citations.as_str(),
        e.rationale.as_str(),
        e.reviewer.as_str(),
    ]
    .join("\t");
    e.key == f[0]
        && e.reviewer == f[1]
        && e.verdict == f[2]
        && e.target == f[3]
        && oracle_blobs::sha256_hex(line.as_bytes()) == f[6]
}
fn original() -> BTreeSet<&'static str> {
    let old: BTreeSet<_> = include_str!("../data/python-r16-reviews.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| f[3] == "L1b-r16-keyword")
        .map(|f| f[0])
        .collect();
    let scope: BTreeSet<_> = include_str!("../data/python-r16-scope-reviewed.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(old.len(), 20);
    assert_eq!(scope.len(), 7);
    assert!(old.is_disjoint(&scope));
    old.union(&scope).copied().collect()
}
#[test]
fn canonical_union_keeps_original_27_and_b359_plus_exact_two_ratio_keys() {
    let fs = proposal();
    let p = adj::parse(&adj::TABLES).unwrap();
    let own: BTreeSet<_> = fs.iter().map(|f| f[0]).collect();
    let old = original();
    assert!(own.is_disjoint(&old));
    assert_eq!(own.len(), 359);
    let discovery: BTreeSet<_> = include_str!("../data/python-keyword-discovery.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| f[7] == "added")
        .map(|f| f[0])
        .collect();
    // Scope this proposal to B386 plus the original 27. Coordinator A has a
    // disjoint manifest; the unchanged shared discovery test still owns global admission.
    let queue: BTreeSet<_> = include_str!("../data/python-kb-r1-queue.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    let protected: BTreeSet<_> = old.union(&queue).copied().collect();
    assert!(protected.is_subset(&discovery));
    let r50 = r50_recovery_forward_admission::admitted(&p);
    let r52 = r50_recovery_forward_admission::lane_a::admitted(&p);
    let reviewed = all_reviewed().unwrap();
    let actual: BTreeSet<_> = reviewed
        .iter()
        .filter(|r| protected.contains(r.key.as_str()))
        .filter(|r| !r36_stats_admission::admitted(&p).contains(r.key.as_str()))
        .filter(|r| !r50.contains(r.key.as_str()))
        .filter(|r| !r52.contains(r.key.as_str()))
        .map(|r| r.key.as_str())
        .collect();
    let ratio = r19_ratio_admission::admitted(&p);
    assert!(ratio.is_disjoint(&own));
    assert!(ratio.is_disjoint(&old));
    assert!(ratio.is_subset(&queue));
    let expected: BTreeSet<_> = old.union(&own).copied().chain(ratio).collect();
    assert_eq!(actual, expected);
    for f in fs {
        let e = p.entries.iter().find(|e| e.key == f[0]).unwrap();
        assert!(exact(e, &f), "{}", f[0]);
        assert_eq!(p.partitions[f[0]].0, 'B');
    }
}
#[test]
fn every_proposed_key_rejects_omission_and_identity_changes_and_remaining_25_are_not_allowed() {
    let fs = proposal();
    let p = adj::parse(&adj::TABLES).unwrap();
    let own: BTreeSet<_> = fs.iter().map(|f| f[0]).collect();
    let all: BTreeSet<_> = include_str!("../data/python-kb-r1-queue.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert!(own.is_subset(&all));
    assert_eq!(all.difference(&own).count(), 27); // Frozen predecessor remainder.
    let ratio = r19_ratio_admission::admitted(&p);
    assert!(ratio.is_disjoint(&own));
    assert!(ratio.is_subset(&all));
    let admitted: BTreeSet<_> = own.union(&ratio).copied().collect();
    assert_eq!(admitted.len(), 361);
    assert_eq!(all.difference(&admitted).count(), 25);
    let r50 = r50_recovery_forward_admission::admitted(&p);
    let r52 = r50_recovery_forward_admission::lane_a::admitted(&p);
    let reviewed = all_reviewed().unwrap();
    let actual: BTreeSet<_> = reviewed
        .iter()
        .filter(|r| all.contains(r.key.as_str()))
        .filter(|r| !r36_stats_admission::admitted(&p).contains(r.key.as_str()))
        .filter(|r| !r50.contains(r.key.as_str()))
        .filter(|r| !r52.contains(r.key.as_str()))
        .map(|r| r.key.as_str())
        .collect();
    assert_eq!(actual, admitted);
    for f in &fs {
        let e = p.entries.iter().find(|e| e.key == f[0]).unwrap();
        let mut without = admitted.clone();
        assert!(without.remove(f[0]));
        assert_ne!(actual, without, "omission {}", f[0]);
        for fault in ["key", "reviewer", "target", "citation"] {
            let mut bad = e.clone();
            match fault {
                "key" => bad.key.push_str(":wrong"),
                "reviewer" => bad.reviewer.push_str("-wrong"),
                "target" => bad.target.push_str("-wrong"),
                _ => bad.citations.push_str("-wrong"),
            };
            assert!(!exact(&bad, f), "{fault}: {}", f[0]);
        }
    }
    for key in all.difference(&admitted) {
        let mut widened = admitted.clone();
        widened.insert(key);
        assert_ne!(actual, widened, "unreviewed key {key}");
    }
}
#[test]
fn frozen_original_b334_stays_281_reviewed_and_53_pending() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let frozen: BTreeSet<_> = p
        .partitions
        .iter()
        .filter(|(key, v)| {
            v.0 == 'B'
                && !r32_verifier_admission::keys().contains(key.as_str())
                && !key.contains(":validator-keyword:")
                && !key.contains(":manual-bound:")
        })
        .map(|(key, _)| key.as_str())
        .collect();
    assert_eq!(frozen.len(), 334);
    assert_eq!(
        oracle_blobs::sha256_hex(
            frozen
                .iter()
                .copied()
                .collect::<Vec<_>>()
                .join("\n")
                .as_bytes()
        ),
        "225a0830ed2ecea102f46ed92335da44257e82eaa6b92a2fae729aedb167bc51"
    );
    let reviewed: BTreeSet<_> = p
        .entries
        .iter()
        .filter(|e| frozen.contains(e.key.as_str()))
        .map(|e| e.key.as_str())
        .collect();
    let mut incremental = r21_ratio_admission::admitted(&p);
    let r23 = r23_seed_invite_admission::admitted(&p);
    assert!(incremental.is_disjoint(&r23));
    incremental.extend(r23);
    let r24 = r24_member_lease_admission::admitted(&p);
    assert!(incremental.is_disjoint(&r24));
    incremental.extend(r24);
    let mut r26 = r26_seed_inputs_admission::admitted(&p);
    let r28 = r28_seed_http_admission::admitted(&p);
    assert!(r26.is_disjoint(&r28));
    r26.extend(r28);
    let r31 = r31_seed_admission::admitted(&p);
    assert!(r26.is_disjoint(&r31));
    r26.extend(r31);
    assert!(incremental.is_disjoint(&r26));
    incremental.extend(r26);
    assert!(incremental.insert(r32_verifier_admission::OLD));
    assert!(r32_verifier_admission::reviews().contains(r32_verifier_admission::OLD));
    assert!(incremental.is_subset(&frozen));
    let historical: BTreeSet<_> = reviewed.difference(&incremental).copied().collect();
    assert_eq!(historical.len(), 281);
    assert_eq!(frozen.difference(&historical).count(), 53);
    assert_eq!(reviewed, historical.union(&incremental).copied().collect());
    assert_eq!(frozen.difference(&reviewed).count(), 53 - incremental.len());
}
