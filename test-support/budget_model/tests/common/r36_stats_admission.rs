// SPDX-License-Identifier: AGPL-3.0-only
//! Exact three-key STATIC cohort, not a new admission engine or numerical ceiling.
use budget_model::inventory::adjudication as adj;
use budgets::{
    BudgetId,
    table::{BudgetEntry, Provenance, Purpose, Value},
};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
pub const KEYS: [&str; 3] = [
    "mycelium_swarm_directory/stats_docs.py:21:30:validator-keyword:low",
    "mycelium_swarm_directory/stats_docs.py:21:40:validator-keyword:high",
    "mycelium_swarm_directory/stats_docs.py:31:26:validator-keyword:high",
];
pub const IDS: [&str; 3] = [
    "PySourceUnboundedR36StatsLowL21C30",
    "PySourceUnboundedR36StatsHighL21C40",
    "PySourceUnboundedR36StatsMaxNL31C26",
];
pub const PROVENANCE: [&str; 3] = [
    "v0.1.4 ships no ceiling at mycelium_swarm_directory/stats_docs.py:21 @ f4e366e9; field low; caller-supplied threshold operand, not validated values or JSON envelopes; V03 any new ceiling deferred",
    "v0.1.4 ships no ceiling at mycelium_swarm_directory/stats_docs.py:21 @ f4e366e9; field high; caller-supplied threshold operand, not validated values or JSON envelopes; V03 any new ceiling deferred",
    "v0.1.4 ships no ceiling at mycelium_swarm_directory/stats_docs.py:31 @ f4e366e9; field max_n; caller-supplied threshold operand, not validated values or JSON envelopes; V03 any new ceiling deferred",
];
const MANIFEST: &str = include_str!("../../data/python-r36-stats-cohort.tsv");
fn hash(b: &[u8]) -> String {
    format!("{:x}", Sha256::digest(b))
}
pub fn lookup(s: &str) -> Option<BudgetEntry> {
    BudgetId::from_name(s).map(BudgetId::entry)
}
pub fn exact(e: &adj::Entry) -> bool {
    let Some(l) = MANIFEST
        .lines()
        .skip(1)
        .find(|l| l.split('\t').next() == Some(e.key.as_str()))
    else {
        return false;
    };
    let f: Vec<_> = l.split('\t').collect();
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
        && e.verdict == "source-unbounded"
        && e.reviewer == "L1b-r36-stats-forwarded"
        && e.target == f[1]
        && hash(record.as_bytes()) == f[3]
}
pub fn validate(
    p: &adj::Parsed,
    lookup: &dyn Fn(&str) -> Option<BudgetEntry>,
) -> Result<(), &'static str> {
    if hash(MANIFEST.as_bytes())
        != "09524ce27dcc28e97c56fc79c34d8e38d8a22430d3acceb05dba57e9f5284d94"
    {
        return Err("cohort pin");
    }
    let manifest_keys: BTreeSet<_> = MANIFEST
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    if manifest_keys != BTreeSet::from(KEYS) {
        return Err("exact keys");
    }
    for (i, id) in IDS.iter().enumerate() {
        let e = lookup(id).ok_or("missing source-specific target")?;
        if e.id.name() != *id {
            return Err("registry identity");
        }
        if e.value != Value::Blocked {
            return Err("Blocked not numeric");
        }
        if e.purpose != Purpose::Policy {
            return Err("Policy");
        }
        if e.provenance != Provenance::Blocked(PROVENANCE[i]) {
            return Err("exact provenance");
        }
    }
    let actual: BTreeSet<_> = p
        .forwarded_thresholds
        .iter()
        .filter(|e| e.reviewer == "L1b-r36-stats-forwarded")
        .map(|e| e.key.as_str())
        .collect();
    if actual != manifest_keys {
        return Err("exact review union");
    }
    for l in MANIFEST.lines().skip(1) {
        let f: Vec<_> = l.split('\t').collect();
        let entries: Vec<_> = p
            .forwarded_thresholds
            .iter()
            .filter(|e| e.key == f[0])
            .collect();
        if entries.len() != 1 || !exact(entries[0]) {
            return Err("exact review");
        }
        let rows = p.proofs.get(f[0]).ok_or("missing proof")?;
        let proof: String = rows
            .iter()
            .map(|r| {
                format!(
                    "{}\t{}\t{}\t{}\t{}\t{}\n",
                    f[0], r.kind, r.path, r.line, r.needle, r.verdict
                )
            })
            .collect();
        if hash(proof.as_bytes()) != f[4] {
            return Err("exact proof");
        }
        let cs: Vec<_> = include_str!("../../data/python-candidates.tsv")
            .lines()
            .filter(|l| l.split('\t').next() == Some(f[0]))
            .collect();
        if cs.len() != 1 || hash(cs[0].as_bytes()) != f[2] {
            return Err("exact candidate");
        }
    }
    Ok(())
}
pub fn admitted(p: &adj::Parsed) -> BTreeSet<&'static str> {
    validate(p, &lookup).expect("R36 STATIC exact source-specific cohort");
    BTreeSet::from(KEYS)
}

/// Reconstruct a historical reviewed view through the validated exact cohort only.
pub fn remove_from_reviewed(reviewed: &mut BTreeSet<String>) {
    let p = adj::parse(&adj::TABLES).unwrap();
    for key in admitted(&p) {
        assert!(reviewed.remove(key));
    }
}
