// SPDX-License-Identifier: AGPL-3.0-only
//! `source-unbounded` verdicts of the real tables (round 15, RU-033): the negative proof is a scan, and a scan is
//! only a proof if no oracle line holding the needle is missing from the table. The occurrence sets are
//! re-derived here from the pinned oracle blobs (read-only `git cat-file --batch`), for both partitions.
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared test helper: each test binary uses a subset"
)]
mod oracle_blobs;

use budget_model::inventory::adjudication as adj;
use std::collections::{BTreeMap, BTreeSet};

type Scan = (String, String, String, usize, BTreeSet<(String, usize)>);

/// Every scan of the real proof tables: `(key, needle, scan path)` and the occurrence rows it must equal.
fn scans() -> Vec<Scan> {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let mut out = Vec::new();
    for (key, rows) in &parsed.proofs {
        for scan in rows.iter().filter(|p| p.kind == "scan") {
            let occurrences = rows
                .iter()
                .filter(|p| {
                    p.kind == "occurrence"
                        && p.needle == scan.needle
                        && (scan.path == "*" || p.path == scan.path)
                })
                .map(|p| (p.path.clone(), p.line))
                .collect();
            out.push((
                key.clone(),
                scan.needle.clone(),
                scan.path.clone(),
                scan.line,
                occurrences,
            ));
        }
    }
    out
}

#[test]
fn every_real_scan_equals_the_oracle_lines_that_hold_its_needle() {
    if !oracle_blobs::oracle_present() {
        panic!("the pinned oracle checkout is required for the scan re-derivation");
    }
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let paths: Vec<String> = parsed.scope.keys().cloned().collect();
    let blobs: BTreeMap<String, String> = oracle_blobs::blobs(&paths);
    let mut checked = 0;
    for (key, needle, path, count, listed) in scans() {
        let mut found = BTreeSet::new();
        for (file, text) in &blobs {
            if path != "*" && *file != path {
                continue;
            }
            for (index, line) in text.split('\n').enumerate() {
                if line.contains(&needle) {
                    found.insert((file.clone(), index + 1));
                }
            }
        }
        assert_eq!(
            found, listed,
            "{key}: needle {needle:?} in {path}: oracle lines vs the table"
        );
        assert_eq!(found.len(), count, "{key}: {needle:?}");
        checked += 1;
    }
    println!("scans re-derived from the oracle: {checked}");
}

#[test]
fn every_source_unbounded_verdict_has_a_proof_and_every_proof_has_a_verdict() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let verdict_keys: BTreeSet<&str> = parsed
        .entries
        .iter()
        .chain(&parsed.forwarded_thresholds)
        .filter(|e| e.verdict == adj::SOURCE_UNBOUNDED)
        .map(|e| e.key.as_str())
        .collect();
    let proof_keys: BTreeSet<&str> = parsed.proofs.keys().map(String::as_str).collect();
    assert_eq!(verdict_keys, proof_keys);
}
