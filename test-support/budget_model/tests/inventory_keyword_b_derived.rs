// SPDX-License-Identifier: AGPL-3.0-only
//! One dynamic upper operand with an exact-int, single-assignment writer.
use budget_model::inventory::{Candidate, adjudication as adj, candidates};
use std::collections::BTreeMap;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
const FIXTURE: &str = include_str!("../data/fx-kb-derived-r1.tsv");
const KEY: &str = "mycelium_swarm_artifacts.py:581:20:validator-keyword:maximum";
fn census() -> BTreeMap<String, Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}
fn matches(
    p: &adj::Parsed,
    e: &adj::Entry,
    cs: &BTreeMap<String, Candidate>,
    lookup: adj::Lookup,
) -> bool {
    let f: Vec<_> = FIXTURE.lines().nth(1).unwrap().split('\t').collect();
    e.key == KEY
        && e.target == f[1]
        && e.verdict == "derived"
        && e.reviewer == "L1b-KB-r1-derived"
        && e.rationale.contains("direction=upper;")
        && (504..=509).all(|line| {
            e.citations.contains(&format!(
                "mycelium_swarm_artifacts.py:{line} @ f4e366e9 sha256="
            ))
        })
        && lookup(&e.target).is_some_and(|v| v.unit == "bytes" && v.value == Some(67_108_864))
        && adj::row_of(p, e, cs, lookup).is_ok_and(|r| {
            r.disposition == "derived"
                && r.relation == f[1]
                && r.unit == "bytes"
                && r.value.is_none()
                && r.budget.is_empty()
        })
}
#[test]
fn derived_chunk_ceiling_is_not_an_assertion_of_constant_per_call_maximum() {
    assert_eq!(
        oracle_blobs::sha256_hex(FIXTURE.as_bytes()),
        "bcf27a81bf0082ff54741ff72a385e20284c01ad059121b2b259350dad62e77c"
    );
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs = census();
    let e = p
        .entries
        .iter()
        .find(|e| e.key == KEY)
        .expect("missing derived KB proposal");
    assert!(matches(&p, e, &cs, &adj::registry), "{KEY}");
}
#[test]
fn derived_writer_consumer_and_exact_type_guard_are_pinned() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs = census();
    let e = p
        .entries
        .iter()
        .find(|e| e.key == KEY)
        .expect("missing derived KB proposal");
    let f: Vec<_> = FIXTURE.lines().nth(1).unwrap().split('\t').collect();
    let path = "mycelium_swarm_artifacts.py";
    let blobs = oracle_blobs::blobs(&[path.into()]);
    let source = &blobs[path];
    let lines: Vec<_> = source.lines().collect();
    for (start, end, digest) in [(6, 7, 8), (9, 10, 11)] {
        let a: usize = f[start].parse().unwrap();
        let b: usize = f[end].parse().unwrap();
        assert_eq!(
            oracle_blobs::sha256_hex(lines[a - 1..b].join("\n").as_bytes()),
            f[digest]
        );
    }
    for line in 504..=509 {
        let citation = format!(
            "{path}:{line} @ f4e366e9 sha256={}",
            oracle_blobs::sha256_hex(lines[line - 1].as_bytes())
        );
        assert!(e.citations.contains(&citation));
        let mut bad = e.clone();
        bad.citations = bad
            .citations
            .split(" | ")
            .filter(|s| *s != citation)
            .collect::<Vec<_>>()
            .join(" | ");
        assert_ne!(bad.citations, e.citations);
        assert!(!matches(&p, &bad, &cs, &adj::registry));
    }
    assert!(lines[313].contains("type(value) is not int"));
    assert!(matches(&p, e, &cs, &adj::registry));
}
#[test]
fn derived_operand_rejects_wrong_id_direction_hash_unit_and_numeric_ceiling() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs = census();
    let e = p
        .entries
        .iter()
        .find(|e| e.key == KEY)
        .expect("missing derived KB proposal");
    let mut bad = e.clone();
    bad.target = "PyKbSwarmUpperL508C16".into();
    assert!(!matches(&p, &bad, &cs, &adj::registry));
    bad = e.clone();
    bad.rationale = bad
        .rationale
        .replace("direction=upper;", "direction=lower;");
    assert!(!matches(&p, &bad, &cs, &adj::registry));
    bad = e.clone();
    bad.citations = bad.citations.replace("sha256=", "sha256=0");
    assert!(!matches(&p, &bad, &cs, &adj::registry));
    bad = e.clone();
    bad.verdict = "bounded".into();
    assert!(!matches(&p, &bad, &cs, &adj::registry));
    let wrong_value: adj::Lookup = &|id| {
        adj::registry(id).map(|mut v| {
            v.value = v.value.map(|n| n + 1);
            v
        })
    };
    assert!(!matches(&p, e, &cs, wrong_value));
    let wrong_unit: adj::Lookup = &|id| {
        adj::registry(id).map(|mut v| {
            v.unit = "entries";
            v
        })
    };
    assert!(!matches(&p, e, &cs, wrong_unit));
}
