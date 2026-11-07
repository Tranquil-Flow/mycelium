// SPDX-License-Identifier: AGPL-3.0-only
//! Exact source-specific STATIC admission; the generic manual engine is unchanged.
use budget_model::inventory::adjudication as adj;
use budgets::{
    BudgetId,
    table::{BudgetEntry, Provenance, Purpose, Value},
};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
fn hash(b: &[u8]) -> String {
    format!("{:x}", Sha256::digest(b))
}
pub const OLD: &str = "mycelium_release_bundle/verifier.py:529:4:assignment:maximum_entries";
pub const PATH: &str = "mycelium_release_bundle/verifier.py";
pub const IDS: [(&str, u64, &str); 3] = [
    (
        "PyReleaseBundleVerifierMaxFileCountL24C0",
        4096,
        "mycelium_release_bundle/verifier.py:24 @ f4e366e9",
    ),
    (
        "PyReleaseBundleVerifierMaxPathComponentsL26C0",
        32,
        "mycelium_release_bundle/verifier.py:26 @ f4e366e9",
    ),
    (
        "PyReleaseBundleVerifierInventoryShippedCeilingL529C4",
        131073,
        "mycelium_release_bundle/verifier.py:529 @ f4e366e9",
    ),
];
const MANIFEST: &str = include_str!("../../data/python-r32-verifier-cohort.tsv");
pub fn lookup(s: &str) -> Option<BudgetEntry> {
    BudgetId::from_name(s).map(BudgetId::entry)
}
pub fn validate(
    census: &str,
    p: &adj::Parsed,
    lookup: &dyn Fn(&str) -> Option<BudgetEntry>,
) -> Result<(), &'static str> {
    if hash(MANIFEST.as_bytes())
        != "a0df304554a5c1001c59da50255e79f5c611da340a2fe45c87fd17826155c6af"
    {
        return Err("manifest");
    }
    for (id, value, source) in IDS {
        let e = lookup(id).ok_or("missing target")?;
        if e.id.name() != id {
            return Err("registry identity");
        }
        if !matches!(e.value,Value::Count(n) if n.get()==value) {
            return Err("numeric Count");
        }
        if e.purpose != Purpose::Policy {
            return Err("Policy");
        }
        if e.provenance != Provenance::Source(source) {
            return Err("source origin/pin");
        }
    }
    let expected: BTreeSet<_> = MANIFEST
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    let actual: BTreeSet<_> = p
        .entries
        .iter()
        .filter(|e| e.reviewer == "L1b-r34-verifier-source-proposal")
        .map(|e| e.key.as_str())
        .collect();
    if actual != expected {
        return Err("exact review union");
    }
    for row in MANIFEST.lines().skip(1) {
        let f: Vec<_> = row.split('\t').collect();
        let cs: Vec<_> = census
            .lines()
            .filter(|l| l.split('\t').next() == Some(f[0]))
            .collect();
        if cs.len() != 1 || hash(cs[0].as_bytes()) != f[2] {
            return Err("exact discovery record");
        }
        let es: Vec<_> = p.entries.iter().filter(|e| e.key == f[0]).collect();
        if es.len() != 1 {
            return Err("exact review occurrence");
        }
        let e = es[0];
        let record = [
            &*e.key,
            &*e.verdict,
            &*e.target,
            &*e.citations,
            &*e.rationale,
            &*e.reviewer,
        ]
        .join("\t");
        if e.partition != 'B' || hash(record.as_bytes()) != f[3] {
            return Err("exact review record");
        }
    }
    Ok(())
}
fn compute_reviews() -> BTreeSet<&'static str> {
    let p = adj::parse(&adj::TABLES).unwrap();
    validate(
        include_str!("../../data/python-candidates.tsv"),
        &p,
        &lookup,
    )
    .expect("R32 STATIC source-specific cohort");
    let keys: BTreeSet<_> = MANIFEST
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(keys.len(), 5);
    keys
}
pub fn reviews() -> BTreeSet<&'static str> {
    // Inputs are immutable include_str!/registry declarations in each executable.
    // Injection controls call validate directly, never this projection cache.
    static REVIEWS: std::sync::OnceLock<BTreeSet<&'static str>> = std::sync::OnceLock::new();
    REVIEWS.get_or_init(compute_reviews).clone()
}
pub fn keys() -> BTreeSet<&'static str> {
    let mut keys = reviews();
    assert!(keys.remove(OLD));
    assert_eq!(keys.len(), 4);
    keys
}
pub fn remove_from(census: &mut BTreeSet<String>, reviewed: &mut BTreeSet<String>) {
    for key in keys() {
        assert!(census.remove(key));
        assert!(reviewed.remove(key));
    }
    assert!(census.contains(OLD));
    assert!(reviewed.remove(OLD));
}
