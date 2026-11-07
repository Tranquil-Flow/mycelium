// SPDX-License-Identifier: AGPL-3.0-only
//! Exact additive r19 cohort; the historical A169/B359 manifests are immutable.
use super::oracle_blobs;
use budget_model::inventory::adjudication as adj;
use std::collections::BTreeSet;
pub const KEYS: [&str; 2] = [
    "mycelium_swarm_artifacts.py:1079:16:validator-keyword:maximum",
    "mycelium_swarm_artifacts.py:1087:16:validator-keyword:maximum",
];
const MANIFEST: &str = include_str!("../../data/python-r19-ratio-admissions.tsv");
pub fn keys() -> BTreeSet<&'static str> {
    assert_eq!(
        oracle_blobs::sha256_hex(MANIFEST.as_bytes()),
        "56354d113eaecd1f10bbe2c250354cca797fb4f73b729141a7a346515f98766a"
    );
    let keys: BTreeSet<_> = MANIFEST
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(MANIFEST.lines().skip(1).count(), 2);
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
        .filter(|e| e.reviewer == "L1b-KB-r2-ratio")
        .map(|e| e.key.as_str())
        .collect();
    assert_eq!(actual, expected);
    for key in &expected {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == *key)
            .expect("missing exact r19 admission");
        assert!(exact(e), "{key}");
    }
    expected
}
