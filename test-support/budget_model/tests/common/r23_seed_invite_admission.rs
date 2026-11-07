// SPDX-License-Identifier: AGPL-3.0-only
//! Exact additive r23 cohort; the historical A169/B359/B334 manifests are immutable.
use super::oracle_blobs;
use budget_model::inventory::adjudication as adj;
use std::collections::BTreeSet;
pub const KEYS: [&str; 1] = ["mycelium_seed/coordinator.py:690:20:keyword:invite_expires_at"];
const MANIFEST: &str = include_str!("../../data/python-r23-seed-invite-admission.tsv");
pub fn keys() -> BTreeSet<&'static str> {
    assert_eq!(
        oracle_blobs::sha256_hex(MANIFEST.as_bytes()),
        "db4feda148813f51e7913435e48cf12911de83ad52b89ea2d4adfba83015c9ff"
    );
    let keys: BTreeSet<_> = MANIFEST
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(MANIFEST.lines().skip(1).count(), 1);
    assert_eq!(keys, BTreeSet::from(KEYS));
    keys
}
pub fn exact(e: &adj::Entry) -> bool {
    let Some(line) = MANIFEST
        .lines()
        .skip(1)
        .find(|l| l.split('\t').next() == Some(e.key.as_str()))
    else {
        return false;
    };
    let f: Vec<_> = line.split('\t').collect();
    let record = [
        &*e.key,
        &*e.verdict,
        &*e.target,
        &*e.citations,
        &*e.rationale,
        &*e.reviewer,
    ]
    .join("\t");
    e.partition == 'B'
        && f.len() == 5
        && e.reviewer == f[1]
        && e.verdict == f[2]
        && e.target == f[3]
        && oracle_blobs::sha256_hex(record.as_bytes()) == f[4]
}
pub fn admitted(p: &adj::Parsed) -> BTreeSet<&'static str> {
    let expected = keys();
    let actual: BTreeSet<_> = p
        .entries
        .iter()
        .filter(|e| e.reviewer == "L1b-r23-seed-invite")
        .map(|e| e.key.as_str())
        .collect();
    assert_eq!(actual, expected);
    for key in &expected {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == *key)
            .expect("missing exact r23 admission");
        assert!(exact(e), "{key}");
    }
    expected
}
