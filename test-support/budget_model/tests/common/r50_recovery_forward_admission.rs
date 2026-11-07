// SPDX-License-Identifier: AGPL-3.0-only
//! Exact two-key, canonical-record-bound R50 historical projection. No inventory engine.
use budget_model::inventory::{self, adjudication as adj};
use budgets::{
    BudgetId,
    table::{BudgetEntry, Provenance, Purpose, Value},
};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

pub const KEYS: [&str; 2] = [
    "mycelium_a6_recovery.py:150:31:validator-keyword:maximum",
    "mycelium_a6_recovery.py:158:34:validator-keyword:minimum",
];
pub const IDS: [&str; 2] = [
    "PySourceUnboundedR50RecoveryMaximumL150C31",
    "PySourceUnboundedR50RecoveryMinimumL158C34",
];
pub const REVIEWER: &str = "L1b-R50-recon-proposal";
pub const PROVENANCE: [&str; 2] = [
    "v0.1.4 ships no ceiling at mycelium_a6_recovery.py:150 @ f4e366e9; field maximum; caller-supplied threshold operand, not the validated value itself; V03 any new ceiling deferred",
    "v0.1.4 ships no ceiling at mycelium_a6_recovery.py:158 @ f4e366e9; field minimum; caller-supplied threshold operand, not the validated value itself; V03 any new ceiling deferred",
];
pub const MANIFEST: &str = include_str!("../../data/python-r50-recovery-forward-cohort.tsv");
pub const SOURCE: &str = include_str!("../../data/python-r50-recovery-source-pins.tsv");
pub fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn lookup(name: &str) -> Option<BudgetEntry> {
    BudgetId::from_name(name).map(BudgetId::entry)
}
fn record(e: &adj::Entry) -> String {
    [
        &*e.key,
        &*e.verdict,
        &*e.target,
        &*e.citations,
        &*e.rationale,
        &*e.reviewer,
    ]
    .join("\t")
}
pub fn exact(e: &adj::Entry) -> bool {
    let Some(index) = KEYS.iter().position(|key| *key == e.key) else {
        return false;
    };
    let fields: Vec<_> = MANIFEST
        .lines()
        .skip(1)
        .nth(index)
        .unwrap()
        .split('\t')
        .collect();
    e.partition == 'B'
        && e.verdict == "source-unbounded"
        && e.target == IDS[index]
        && e.reviewer == REVIEWER
        && fields[0] == e.key
        && hash(record(e).as_bytes()) == fields[3]
}
pub fn census() -> BTreeMap<String, inventory::Candidate> {
    inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}
pub fn source_pins(pins: &str) -> Result<(), &'static str> {
    if hash(pins.as_bytes()) != "3402a7d0c544340950f0538ff13e88c894616bd2c39acb56e072fc6abb518b9d" {
        return Err("exact source pins");
    }
    Ok(())
}
pub fn source_blobs(blobs: &BTreeMap<String, String>, pins: &str) -> Result<(), &'static str> {
    source_pins(pins)?;
    let expected: BTreeMap<_, _> = pins
        .lines()
        .skip(1)
        .map(|line| line.split_once('\t').unwrap())
        .collect();
    if expected.keys().copied().collect::<BTreeSet<_>>()
        != blobs.keys().map(String::as_str).collect()
    {
        return Err("exact source path union");
    }
    if expected
        .iter()
        .any(|(p, h)| hash(blobs[*p].as_bytes()) != *h)
    {
        return Err("source bytes including producer bodies");
    }
    Ok(())
}
pub fn validate_with(
    p: &adj::Parsed,
    candidates: &BTreeMap<String, inventory::Candidate>,
    lookup: &dyn Fn(&str) -> Option<BudgetEntry>,
    pins: &str,
) -> Result<(), &'static str> {
    source_pins(pins)?;
    if hash(MANIFEST.as_bytes())
        != "bf23108957366bf8aac2d84f3e3a693d5f97e5528d8e00069fbacfb8d0de1ff0"
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
    let actual: BTreeSet<_> = p
        .entries
        .iter()
        .filter(|e| e.reviewer == REVIEWER)
        .map(|e| e.key.as_str())
        .collect();
    if actual != manifest_keys {
        return Err("exact review union");
    }
    if p.forwarded_thresholds
        .iter()
        .any(|e| KEYS.contains(&e.key.as_str()) || e.reviewer == REVIEWER)
    {
        return Err("ordinary B only");
    }
    // DELTA52: verify the entire live union, then project only exact added triples.
    let historical_pins = lane_a::historical_pins(p)?;
    let pin_union: String = historical_pins
        .iter()
        .map(|((path, line), digest)| format!("{path}\t{line}\t{digest}\n"))
        .collect();
    if hash(pin_union.as_bytes())
        != "f7c5468baaa8777bfddb495c652b7173c0f7a3b8dda689c9da01fcdd04ffad7c"
    {
        return Err("exact citation pin union");
    }
    for (i, line) in MANIFEST.lines().skip(1).enumerate() {
        let f: Vec<_> = line.split('\t').collect();
        let entries: Vec<_> = p.entries.iter().filter(|e| e.key == f[0]).collect();
        if entries.len() != 1 || !exact(entries[0]) {
            return Err("exact review");
        }
        let target = lookup(IDS[i]).ok_or("missing own-site target")?;
        if target.id.name() != IDS[i] {
            return Err("registry identity");
        }
        if target.value != Value::Blocked {
            return Err("Blocked not numeric");
        }
        if target.purpose != Purpose::Policy {
            return Err("Policy");
        }
        if target.provenance != Provenance::Blocked(PROVENANCE[i]) {
            return Err("exact provenance");
        }
        let c = candidates.get(f[0]).ok_or("missing candidate")?;
        let candidate = format!(
            "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
            c.key, c.path, c.line, c.column, c.scope, c.symbol, c.expression, c.kind, c.source
        );
        if hash(candidate.as_bytes()) != f[2] {
            return Err("exact candidate site/kind/threshold");
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
            return Err("exact proof scans/hops/citation union");
        }
        if adj::row_of(p, entries[0], candidates, &adj::registry).is_err() {
            return Err("generic source-unbounded validation");
        }
    }
    Ok(())
}
pub fn validate(
    p: &adj::Parsed,
    lookup: &dyn Fn(&str) -> Option<BudgetEntry>,
) -> Result<(), &'static str> {
    validate_with(p, &census(), lookup, SOURCE)
}
pub fn admitted(p: &adj::Parsed) -> BTreeSet<&'static str> {
    validate(p, &lookup).expect("R50 exact source-specific canonical-row-hash cohort");
    BTreeSet::from(KEYS)
}
/// Subtract only both validated canonical reviews; never a prefix or mutable count.
pub fn remove_from_reviewed(reviewed: &mut BTreeSet<String>) {
    let p = adj::parse(&adj::TABLES).unwrap();
    for key in admitted(&p) {
        assert!(reviewed.remove(key));
    }
}

/// DELTA52 exact dispositions. Kept in this authorized helper, not a product engine.
pub mod lane_a {
    use super::{adj, census, hash, inventory, record};
    use std::collections::{BTreeMap, BTreeSet};

    pub const KEYS: [&str; 2] = [
        "mycelium_replica_contracts.py:146:42:validator-keyword:minimum",
        "mycelium_qualification/qualifier.py:777:42:validator-keyword:minimum",
    ];
    pub const REVIEWER: &str = "L1b-r52-lane-a";
    pub const PINS: &str = include_str!("../../data/python-r52-lane-a-pins.tsv");
    pub type PinMap = BTreeMap<(String, usize), String>;

    pub fn pins(text: &str) -> Result<Vec<Vec<&str>>, &'static str> {
        if hash(text.as_bytes())
            != "4b39747c02c22b4b52558bfcbb3eac4d001f407ae166a0ac7d37c97159040cf8"
        {
            return Err("R52 exact manifest");
        }
        Ok(text
            .lines()
            .skip(1)
            .map(|l| l.split('\t').collect())
            .collect())
    }

    pub fn exact(e: &adj::Entry) -> bool {
        KEYS.contains(&e.key.as_str())
            && e.partition == 'B'
            && e.verdict == "not-a-bound"
            && e.target == "-"
            && e.reviewer == REVIEWER
            && pins(PINS)
                .unwrap()
                .iter()
                .any(|f| f[0] == "record" && f[1] == e.key && f[3] == hash(record(e).as_bytes()))
    }

    pub fn validate_with(
        p: &adj::Parsed,
        cs: &BTreeMap<String, inventory::Candidate>,
        manifest: &str,
    ) -> Result<(), &'static str> {
        let fs = pins(manifest)?;
        let keys: BTreeSet<_> = fs
            .iter()
            .filter(|f| f[0] == "record")
            .map(|f| f[1])
            .collect();
        if keys != BTreeSet::from(KEYS) {
            return Err("R52 exact keys");
        }
        if p.entries
            .iter()
            .any(|e| e.reviewer == REVIEWER && !KEYS.contains(&e.key.as_str()))
            || p.forwarded_thresholds
                .iter()
                .any(|e| KEYS.contains(&e.key.as_str()) || e.reviewer == REVIEWER)
        {
            return Err("R52 ordinary exact cohort only");
        }
        for key in KEYS {
            let es: Vec<_> = p.entries.iter().filter(|e| e.key == key).collect();
            if es.len() != 1 || !exact(es[0]) {
                return Err("R52 full canonical record");
            }
            let c = cs.get(key).ok_or("R52 missing candidate")?;
            let raw = format!(
                "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
                c.key, c.path, c.line, c.column, c.scope, c.symbol, c.expression, c.kind, c.source
            );
            if !fs
                .iter()
                .any(|f| f[0] == "candidate" && f[1] == key && f[3] == hash(raw.as_bytes()))
            {
                return Err("R52 exact candidate");
            }
            let row = adj::row_of(p, es[0], cs, &adj::registry)
                .map_err(|_| "R52 generic source validation")?;
            if row.disposition != "not-a-bound" || row.value.is_some() || !row.budget.is_empty() {
                return Err("R52 disposition not numeric ceiling");
            }
        }
        Ok(())
    }

    pub fn admitted(p: &adj::Parsed) -> BTreeSet<&'static str> {
        validate_with(p, &census(), PINS).expect("R52 exact-key and full-record-hash cohort");
        BTreeSet::from(KEYS)
    }

    pub fn remove_from_reviewed(reviewed: &mut BTreeSet<String>) {
        let p = adj::parse(&adj::TABLES).unwrap();
        for key in admitted(&p) {
            assert!(reviewed.remove(key));
        }
    }

    pub fn added_pins() -> PinMap {
        pins(PINS)
            .unwrap()
            .iter()
            .filter(|f| f[0] == "added-pin")
            .map(|f| ((f[1].to_string(), f[2].parse().unwrap()), f[3].to_string()))
            .collect()
    }

    pub fn historical_pins(p: &adj::Parsed) -> Result<PinMap, &'static str> {
        pins(PINS)?;
        let union: String = p
            .pins
            .iter()
            .map(|((path, line), digest)| format!("{path}\t{line}\t{digest}\n"))
            .collect();
        // Derived from complete selected-row citations, never from a guessed count.
        if hash(union.as_bytes())
            != "4c28fcc08f9531db31cf62d22ef384b6b008f03a7b62058f6defb0302697d35a"
        {
            return Err("R52 exact live citation union");
        }
        let mut historical = p.pins.clone();
        for (key, digest) in added_pins() {
            if historical.remove(&key) != Some(digest) {
                return Err("R52 exact added citation triple");
            }
        }
        Ok(historical)
    }
}
