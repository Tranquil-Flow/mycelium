// SPDX-License-Identifier: AGPL-3.0-only
//! R16 second A batch: exact existing-rule scope exclusions, no runtime ceiling claim.
use budget_model::inventory::{adjudication as adj, candidates};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared pinned oracle helper")]
mod oracle_blobs;
const REVIEWS: &str = include_str!("../data/python-r16-scope-reviewed.tsv");
fn keys() -> BTreeSet<&'static str> {
    REVIEWS
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect()
}
#[test]
fn seven_a_forecast_r1_rows_have_exact_launch_graph_verdicts() {
    let keys = keys();
    assert_eq!(keys.len(), 7);
    assert_eq!(
        oracle_blobs::sha256_hex(
            keys.iter()
                .copied()
                .collect::<Vec<_>>()
                .join("\n")
                .as_bytes()
        ),
        "d04d7712e321c09e1ca7aa4d49d6a412598b8d59511b651b11e533d1e8cebd84"
    );
    let forecast: BTreeSet<_> = include_str!("../data/python-keyword-forecast.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| f[1] == "A" && f[2] == "R1")
        .map(|f| f[0])
        .collect();
    assert_eq!(keys, forecast);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    for line in REVIEWS.lines().skip(1) {
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f.len(), 6);
        let e = parsed
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing R1 scope verdict");
        assert_eq!(
            (
                &*e.verdict,
                &*e.target,
                &*e.citations,
                &*e.rationale,
                &*e.reviewer
            ),
            (f[1], f[2], f[3], f[4], f[5])
        );
        assert_eq!(e.verdict, "not-executed");
        assert_eq!(e.reviewer, "L1b-r16-scope");
    }
}
#[test]
fn no_scope_verdict_survives_a_reachable_module_or_wrong_partition() {
    let census: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in keys() {
        let mut parsed = adj::parse(&adj::TABLES).unwrap();
        let entry = parsed
            .entries
            .iter()
            .find(|e| e.key == key)
            .expect("missing R1 scope verdict")
            .clone();
        assert!(adj::row_of(&parsed, &entry, &census, &adj::registry).is_ok());
        let path = &census[key].path;
        assert_eq!(parsed.launch[path].0, "unreached");
        parsed.launch.get_mut(path).unwrap().0 = "frozen".into();
        assert!(
            adj::row_of(&parsed, &entry, &census, &adj::registry)
                .unwrap_err()
                .starts_with("not-executed-without-proof:")
        );
        let parsed = adj::parse(&adj::TABLES).unwrap();
        let mut wrong = entry;
        wrong.partition = 'B';
        assert!(
            adj::row_of(&parsed, &wrong, &census, &adj::registry)
                .unwrap_err()
                .starts_with("wrong-partition:")
        );
    }
}
