// SPDX-License-Identifier: AGPL-3.0-only
//! Exact additive r31 cohort; the historical cohorts and old69 remain immutable.
use super::oracle_blobs;
use budget_model::inventory::adjudication as adj;
use std::collections::BTreeSet;
pub const KEYS: [&str; 3] = [
    "mycelium_seed/state.py:827:16:dict-field:lease_expires_at",
    "mycelium_seed/operator.py:641:19:comparison:member['lease_expires_at']",
    "mycelium_seed/state.py:830:16:dict-field:next_heartbeat_due_at",
];
const MANIFEST: &str = include_str!("../../data/python-r31-seed-admission.tsv");
pub fn keys() -> BTreeSet<&'static str> {
    assert_eq!(
        oracle_blobs::sha256_hex(MANIFEST.as_bytes()),
        "d4bdffae9b3628faccc9e74cc290cd4c2845f6db7f3f02906d56dac16d6b3b24"
    );
    let keys: BTreeSet<_> = MANIFEST
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(MANIFEST.lines().skip(1).count(), 3);
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
        .filter(|e| e.reviewer == "L1b-r31-seed-source")
        .map(|e| e.key.as_str())
        .collect();
    assert_eq!(actual, expected);
    for key in &expected {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == *key)
            .expect("missing exact r31 admission");
        assert!(exact(e), "{key}");
    }
    expected
}
