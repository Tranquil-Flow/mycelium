// SPDX-License-Identifier: AGPL-3.0-only
//! Canonical exact A/B union. Manifest identity does not replace the family source/edge tests.
use budget_model::inventory::adjudication as adj;
use std::collections::BTreeSet;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
fn fixture(name: &str) -> Option<&'static str> {
    match name {
        "fx-kb-derived-r1.tsv" => Some(include_str!("../data/fx-kb-derived-r1.tsv")),
        "fx-kb-directory-r1.tsv" => Some(include_str!("../data/fx-kb-directory-r1.tsv")),
        "fx-kb-mobile-r1.tsv" => Some(include_str!("../data/fx-kb-mobile-r1.tsv")),
        "fx-kb-predicates-r1.tsv" => Some(include_str!("../data/fx-kb-predicates-r1.tsv")),
        "fx-kb-recovery-r1.tsv" => Some(include_str!("../data/fx-kb-recovery-r1.tsv")),
        "fx-kb-small-r1.tsv" => Some(include_str!("../data/fx-kb-small-r1.tsv")),
        "fx-kb-swarm-r1.tsv" => Some(include_str!("../data/fx-kb-swarm-r1.tsv")),
        "fx-kb-timezero-r1.tsv" => Some(include_str!("../data/fx-kb-timezero-r1.tsv")),
        "fx-kb-validators-r1.tsv" => Some(include_str!("../data/fx-kb-validators-r1.tsv")),
        "fx-l1b-r17-acquisition.tsv" => Some(include_str!("../data/fx-l1b-r17-acquisition.tsv")),
        "fx-l1b-r17-finite.tsv" => Some(include_str!("../data/fx-l1b-r17-finite.tsv")),
        "fx-l1b-r17-forward-domain.tsv" => {
            Some(include_str!("../data/fx-l1b-r17-forward-domain.tsv"))
        }
        "fx-l1b-r17-integers.tsv" => Some(include_str!("../data/fx-l1b-r17-integers.tsv")),
        "fx-l1b-r17-misc.tsv" => Some(include_str!("../data/fx-l1b-r17-misc.tsv")),
        "fx-l1b-r17-numeric.tsv" => Some(include_str!("../data/fx-l1b-r17-numeric.tsv")),
        "fx-l1b-r17-rate.tsv" => Some(include_str!("../data/fx-l1b-r17-rate.tsv")),
        "fx-l1b-r17-tuple.tsv" => Some(include_str!("../data/fx-l1b-r17-tuple.tsv")),
        "python-kb-r1-queue.tsv" => Some(include_str!("../data/python-kb-r1-queue.tsv")),
        _ => None,
    }
}
fn manifests() -> Vec<(&'static str, char)> {
    let a = include_str!("../data/python-r17-admissions.tsv");
    let b = include_str!("../data/python-kb-r1-proposals.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(a.as_bytes()),
        "200e6016b4d4580fb5e84fcd20c62a73de929285094206372311af5e737c2171"
    );
    assert_eq!(
        oracle_blobs::sha256_hex(b.as_bytes()),
        "088e9e8caaa91606d1790ecf9102cd5c355ab853f7b2353fa1a3a9cedcf182d2"
    );
    assert_eq!(a.lines().skip(1).count(), 169);
    assert_eq!(b.lines().skip(1).count(), 359);
    a.lines()
        .skip(1)
        .map(|l| (l, 'A'))
        .chain(b.lines().skip(1).map(|l| (l, 'B')))
        .collect()
}
fn exact(e: &adj::Entry, f: &[&str], side: char) -> bool {
    let line = [
        &*e.key,
        &*e.verdict,
        &*e.target,
        &*e.citations,
        &*e.rationale,
        &*e.reviewer,
    ]
    .join("\t");
    f.len() == 7
        && e.partition == side
        && e.key == f[0]
        && e.reviewer == f[1]
        && e.verdict == f[2]
        && e.target == f[3]
        && oracle_blobs::sha256_hex(line.as_bytes()) == f[6]
        && fixture(f[4]).is_some_and(|text| oracle_blobs::sha256_hex(text.as_bytes()) == f[5])
}
#[test]
fn exact_union_preserves_every_entry_and_its_independent_fixture() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let mut keys = BTreeSet::new();
    for (line, side) in manifests() {
        let f: Vec<_> = line.split('\t').collect();
        assert!(keys.insert(f[0]), "cross-lane duplicate {}", f[0]);
        let e = p.entries.iter().find(|e| e.key == f[0]).unwrap();
        assert!(exact(e, &f, side), "{}", f[0]);
        assert_eq!(p.partitions[f[0]].0, side);
    }
    assert_eq!(keys.len(), 528);
}
#[test]
fn every_manifest_field_and_cross_lane_identity_are_fail_closed() {
    let p = adj::parse(&adj::TABLES).unwrap();
    for (line, side) in manifests() {
        let f: Vec<_> = line.split('\t').collect();
        let e = p.entries.iter().find(|e| e.key == f[0]).unwrap();
        assert!(exact(e, &f, side));
        assert!(!exact(e, &f, if side == 'A' { 'B' } else { 'A' }));
        for column in 0..7 {
            let mut wrong: Vec<_> = f.iter().map(|s| s.to_string()).collect();
            wrong[column].push_str("-unadmitted");
            let fields: Vec<_> = wrong.iter().map(String::as_str).collect();
            assert!(!exact(e, &fields, side), "column {column}: {}", f[0]);
        }
    }
}
