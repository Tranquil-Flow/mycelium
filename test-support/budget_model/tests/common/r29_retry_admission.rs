// SPDX-License-Identifier: AGPL-3.0-only
//! Exact additive r29 discovery AND review cohort, never a reviewer-prefix exemption.
use std::collections::BTreeSet;
fn hash(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
pub fn keys() -> BTreeSet<&'static str> {
    let manifest = include_str!("../../data/python-r29-retry-multiplier.tsv");
    assert_eq!(
        hash(manifest.as_bytes()),
        "5e2a0d17e0e57f3f3777a0c05b930da1f24cce7fa809219e750912b69a08eb2b"
    );
    let expected = BTreeSet::from([
        "mycelium_a13_macos/managed_runtime.py:463:93:manual-bound:outbound_retry_multiplier",
    ]);
    let mut actual = BTreeSet::new();
    for row in manifest.lines().skip(1) {
        let f: Vec<_> = row.split('\t').collect();
        assert_eq!(f.len(), 8);
        assert!(actual.insert(f[0]));
        for (text, digest) in [
            (include_str!("../../data/python-candidates.tsv"), f[2]),
            (include_str!("../../data/python-adjudication-a.tsv"), f[3]),
        ] {
            let rows: Vec<_> = text
                .lines()
                .filter(|l| l.split('\t').next() == Some(f[0]))
                .collect();
            assert_eq!(rows.len(), 1);
            assert_eq!(
                hash(rows[0].as_bytes()),
                digest,
                "r29 exact record: {}",
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
