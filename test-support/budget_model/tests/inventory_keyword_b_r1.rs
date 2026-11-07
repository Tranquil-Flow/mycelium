// SPDX-License-Identifier: AGPL-3.0-only
//! Supplemental B proposals; no canonical admission or frozen-B cohort change.
use budget_model::inventory::{Candidate, adjudication as adj, candidates};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;

const QUEUE: &str = include_str!("../data/python-kb-r1-queue.tsv");
const SCOPE_KEYS: [&str; 3] = [
    "mycelium_gateway/event_adapter.py:250:50:validator-keyword:maximum",
    "mycelium_gateway/event_adapter.py:253:64:validator-keyword:maximum",
    "mycelium_physical_runner/laptop_inventory.py:226:83:validator-keyword:maximum",
];
fn census() -> BTreeMap<String, Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}
#[test]
fn exact_b386_queue_and_all_source_hashes_are_pinned() {
    assert_eq!(
        oracle_blobs::sha256_hex(QUEUE.as_bytes()),
        "9184c707f3414b70e497912787a9cb293dd52e3e9f0f37ceb2514022a0bc73a7"
    );
    let rows: Vec<Vec<&str>> = QUEUE
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect();
    let keys: BTreeSet<_> = rows.iter().map(|r| r[0]).collect();
    assert_eq!(keys.len(), 386);
    assert_eq!(
        oracle_blobs::sha256_hex(keys.into_iter().collect::<Vec<_>>().join("\n").as_bytes()),
        "8a7481ff070ce706850aff35f29f33c89fdd8e869b71105756da2e13a170f1b3"
    );
    let paths: BTreeSet<String> = rows
        .iter()
        .map(|r| r[0].split(':').next().unwrap().into())
        .collect();
    let blobs = oracle_blobs::blobs(&paths.into_iter().collect::<Vec<_>>());
    let cs = census();
    let parsed = adj::parse(&adj::TABLES).unwrap();
    for r in rows {
        let c = &cs[r[0]];
        assert_eq!(parsed.partitions[r[0]].0, 'B');
        assert_eq!(c.expression, r[7]);
        assert_eq!(oracle_blobs::sha256_hex(blobs[&c.path].as_bytes()), r[9]);
        assert_eq!(
            oracle_blobs::sha256_hex(
                oracle_blobs::line_of(&blobs[&c.path], c.line)
                    .unwrap()
                    .as_bytes()
            ),
            r[10]
        );
    }
}
#[test]
fn three_launch_scope_proposals_require_exact_unreached_witnesses() {
    let cs = census();
    let parsed = adj::parse(&adj::TABLES).unwrap();
    for key in SCOPE_KEYS {
        let e = parsed
            .entries
            .iter()
            .find(|e| e.key == key)
            .expect("missing KB scope proposal");
        assert_eq!(e.reviewer, "L1b-KB-r1-scope");
        assert_eq!(e.verdict, "not-executed");
        assert_eq!(e.target, "launch-graph");
        let good = adj::row_of(&parsed, e, &cs, &adj::registry).unwrap();
        assert!(adj::validate(&good, &cs));
        let mut wrong = e.clone();
        wrong.target = "-".into();
        assert!(
            adj::row_of(&parsed, &wrong, &cs, &adj::registry)
                .unwrap_err()
                .starts_with("not-executed-without-proof:")
        );
        wrong = e.clone();
        wrong.citations = wrong.citations.replace("sha256=", "sha256=0");
        assert!(
            adj::row_of(&parsed, &wrong, &cs, &adj::registry)
                .unwrap_err()
                .starts_with("invalid-citation:")
        );
        for status in ["frozen", "entry", "reached"] {
            let mut changed = adj::parse(&adj::TABLES).unwrap();
            changed.launch.get_mut(&cs[key].path).unwrap().0 = status.into();
            assert!(
                adj::row_of(&changed, e, &cs, &adj::registry)
                    .unwrap_err()
                    .starts_with("not-executed-without-proof:")
            );
        }
    }
}
