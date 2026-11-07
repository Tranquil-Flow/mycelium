// SPDX-License-Identifier: AGPL-3.0-only
//! Exact additive r30 discovery AND review cohort, never a reviewer-prefix exemption.
use std::collections::BTreeSet;
fn hash(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
pub fn keys() -> BTreeSet<&'static str> {
    let manifest = include_str!("../../data/python-r30-workflow-tokens.tsv");
    assert_eq!(
        hash(manifest.as_bytes()),
        "58f3d10316b0fd33bd99fcbcb526441b6d0348a9e3303329d94ef0bfec593114"
    );
    let expected = BTreeSet::from([
        "mycelium_a13_macos/workflow.py:206:66:manual-bound:verified_challenge_token_limit.maximum",
    ]);
    let mut actual = BTreeSet::new();
    for row in manifest.lines().skip(1) {
        let f: Vec<_> = row.split('\t').collect();
        assert_eq!(f.len(), 8);
        assert!(actual.insert(f[0]));
        for (text, digest) in [
            (include_str!("../../data/python-candidates.tsv"), f[2]),
            (include_str!("../../data/python-bounds.tsv"), f[3]),
        ] {
            let rows: Vec<_> = text
                .lines()
                .filter(|l| l.split('\t').next() == Some(f[0]))
                .collect();
            assert_eq!(rows.len(), 1);
            assert_eq!(
                hash(rows[0].as_bytes()),
                digest,
                "r30 exact record: {}",
                f[0]
            );
        }
    }
    assert_eq!(actual, expected);
    actual
}
pub fn remove_from(census: &mut BTreeSet<String>, reviewed: &mut BTreeSet<String>) {
    for key in keys() {
        assert!(census.remove(key));
        assert!(reviewed.remove(key));
    }
}
