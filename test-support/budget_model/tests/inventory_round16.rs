// SPDX-License-Identifier: AGPL-3.0-only
//! R16 source-bound additions. Forecast is not acceptance; floors are not ceilings.
use budget_model::inventory::{self, adjudication as adj};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared pinned oracle helper")]
mod oracle_blobs;
const EXPECTED: &str = include_str!("../data/python-r16-reviews.tsv");
const GAPS: &str = include_str!("../data/python-r16-census.tsv");
const FORECAST: &str = include_str!("../data/python-keyword-forecast.tsv");
fn records(text: &str) -> Vec<Vec<&str>> {
    text.lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect()
}
fn review_check(entries: &[adj::Entry]) -> Result<(), String> {
    for f in records(EXPECTED) {
        let e = entries
            .iter()
            .find(|e| e.key == f[0])
            .ok_or_else(|| format!("missing:{}", f[0]))?;
        if (e.verdict.as_str(), e.target.as_str(), e.reviewer.as_str()) != (f[1], f[2], f[3]) {
            return Err(format!("wrong-review:{}", f[0]));
        }
        let view = adj::registry(f[2]).ok_or("missing-id")?;
        if f[1] == "source-unbounded" {
            if view.blocked.is_none() || view.value.is_some() {
                return Err("invented-cap".into());
            }
        } else if f[1] == "bounded"
            && (view.unit
                != match f[4] {
                    "count" => "entries",
                    "ms" => "milliseconds",
                    u => u,
                }
                || view.value != Some(f[5].parse().unwrap()))
        {
            return Err(format!("wrong-quantity:{}", f[0]));
        }
        if f[6] == "lower"
            && (!e.target.contains("Floor") || !e.rationale.contains("NOT an upper ceiling"))
        {
            return Err("floor-laundered".into());
        }
    }
    Ok(())
}
#[test]
fn exact_legacy_gap_and_keyword_reviews_are_present_and_source_bound() {
    assert_eq!(
        oracle_blobs::sha256_hex(EXPECTED.as_bytes()),
        "81651926b02e2a0a3f59b5676c28395cad8e52131ed2ae6728ba53f97e201bea"
    );
    assert_eq!(records(EXPECTED).len(), 30);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(review_check(&parsed.entries), Ok(()));
    let rows = inventory::all_reviewed().unwrap();
    for f in records(EXPECTED) {
        assert!(rows.iter().any(|r| r.key == f[0]));
    }
}
#[test]
fn seven_missing_lines_are_eight_separate_exact_operands() {
    assert_eq!(
        oracle_blobs::sha256_hex(GAPS.as_bytes()),
        "7e70d7e85ea581dc0f87d6a5ff9b4e3d2154db13595db05b5194673fbc9f08e7"
    );
    let gaps = records(GAPS);
    assert_eq!(gaps.len(), 8);
    assert_eq!(
        gaps.iter()
            .map(|f| (f[1], f[2]))
            .collect::<BTreeSet<_>>()
            .len(),
        7
    );
    let census = inventory::candidates().unwrap();
    let paths = gaps
        .iter()
        .map(|f| f[1].to_owned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let blobs = oracle_blobs::blobs(&paths);
    for f in gaps {
        let c = census
            .iter()
            .find(|c| c.key == f[0])
            .expect("missing nonkeyword census operand");
        assert_eq!(
            (
                &*c.path,
                c.line,
                c.column,
                &*c.scope,
                &*c.symbol,
                &*c.expression,
                &*c.kind,
                &*c.source
            ),
            (
                f[1],
                f[2].parse().unwrap(),
                f[3].parse().unwrap(),
                f[4],
                f[5],
                f[6],
                f[7],
                f[8]
            )
        );
        assert_eq!(oracle_blobs::sha256_hex(blobs[f[1]].as_bytes()), f[9]);
        assert_eq!(
            oracle_blobs::sha256_hex(
                oracle_blobs::line_of(&blobs[f[1]], c.line)
                    .unwrap()
                    .as_bytes()
            ),
            f[10]
        );
    }
}
#[test]
fn every_new_keyword_has_one_source_hashed_forecast_and_disjoint_partition() {
    assert_eq!(
        oracle_blobs::sha256_hex(FORECAST.as_bytes()),
        "bafcf3c2a2351c7f836f959df80ed2feaf7ddbb596aacc7c70e1bef89717aa9d"
    );
    let forecast = records(FORECAST);
    assert_eq!(forecast.len(), 570);
    let keys: BTreeSet<_> = forecast.iter().map(|f| f[0]).collect();
    assert_eq!(keys.len(), 570);
    let discovered: BTreeSet<_> = records(include_str!("../data/python-keyword-discovery.tsv"))
        .into_iter()
        .filter(|f| f[7] == "added")
        .map(|f| f[0])
        .collect();
    assert_eq!(keys, discovered);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let census = inventory::candidates().unwrap();
    let paths = forecast
        .iter()
        .map(|f| f[0].split(':').next().unwrap().to_owned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let blobs = oracle_blobs::blobs(&paths);
    let mut parts = BTreeMap::new();
    let mut families = BTreeMap::new();
    for f in forecast {
        assert_eq!(f.len(), 12);
        assert!(matches!(f[2], "R1" | "R2" | "R3" | "O" | "X"));
        assert_eq!(parsed.partitions[f[0]].0.to_string(), f[1]);
        *parts.entry(f[1]).or_insert(0usize) += 1;
        *families.entry(f[3]).or_insert(0usize) += 1;
        let c = census.iter().find(|c| c.key == f[0]).unwrap();
        assert_eq!(f[8], oracle_blobs::ORACLE);
        assert_eq!(oracle_blobs::sha256_hex(blobs[&c.path].as_bytes()), f[9]);
        assert_eq!(
            oracle_blobs::sha256_hex(
                oracle_blobs::line_of(&blobs[&c.path], c.line)
                    .unwrap()
                    .as_bytes()
            ),
            f[10]
        );
        assert_eq!(c.expression, f[7]);
        assert_eq!(
            f[4],
            if matches!(c.symbol.as_str(), "minimum" | "low") {
                "lower"
            } else {
                "upper"
            }
        );
        if f[2] == "R1" {
            assert_eq!(parsed.launch[&c.path].0, "unreached");
        }
    }
    assert_eq!(parts, BTreeMap::from([("A", 184), ("B", 386)]));
    assert_eq!(
        families,
        BTreeMap::from([
            ("validator-upper-dynamic", 9),
            ("validator-upper-literal", 104),
            ("validator-lower-literal", 345),
            ("validator-upper-symbol", 38),
            ("validator-lower-symbol", 13),
            ("validator-lower-constant-arithmetic", 4),
            ("existing-adjudication-not-executed", 10),
            ("validator-upper-constant-arithmetic", 47)
        ])
    );
}
#[test]
fn exact_review_checks_reject_same_line_floor_ceiling_swaps_and_producer_caps() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(review_check(&parsed.entries), Ok(()));
    for f in records(EXPECTED) {
        let mut entries = parsed.entries.clone();
        let e = entries.iter_mut().find(|e| e.key == f[0]).unwrap();
        e.target = if f[1] == "source-unbounded" {
            "PyR16M16BuilderReservations".into()
        } else {
            "CancelAndCleanup".into()
        };
        assert!(review_check(&entries).is_err(), "wrong id {}", f[0]);
    }
    let mut entries = parsed.entries.clone();
    let e = entries
        .iter_mut()
        .find(|e| e.target == "PyR16CodecFloorL200C77")
        .unwrap();
    e.target = "PyR16CodecCeilingL200C88".into();
    assert!(
        review_check(&entries)
            .unwrap_err()
            .starts_with("wrong-review:")
    );
}
#[test]
fn new_negative_proofs_refuse_missing_reads_and_numeric_cap_substitution() {
    let census = inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for f in records(EXPECTED)
        .into_iter()
        .filter(|f| f[1] == "source-unbounded")
    {
        let mut parsed = adj::parse(&adj::TABLES).unwrap();
        let e = parsed
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing negative verdict")
            .clone();
        assert!(adj::row_of(&parsed, &e, &census, &adj::registry).is_ok());
        parsed
            .proofs
            .get_mut(f[0])
            .unwrap()
            .retain(|p| p.kind != "hop" || p.verdict != "read");
        assert!(
            adj::row_of(&parsed, &e, &census, &adj::registry)
                .unwrap_err()
                .starts_with("unbounded-proof-read:")
        );
        let parsed = adj::parse(&adj::TABLES).unwrap();
        let mut wrong = e;
        wrong.target = "CancelAndCleanup".into();
        assert!(
            adj::row_of(&parsed, &wrong, &census, &adj::registry)
                .unwrap_err()
                .starts_with("unbounded-target-not-blocked-here:")
        );
    }
}
#[test]
fn executed_fixtures_preserve_dynamic_domains_and_literal_values() {
    let text = include_str!("../data/fx-l1b-r16-executed.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(text.as_bytes()),
        "bf39836e308766a2b08daa7c9757dca353ff35f640d2da21518c68a33ae25e0b"
    );
    let fx = records(text);
    assert_eq!(fx.iter().filter(|r| r[0] == "dynamic").count(), 14);
    assert!(
        fx.iter()
            .any(|r| r.as_slice() == ["dynamic", "7", "-", "1000000000.0", "accepted"])
    );
    assert!(
        fx.iter()
            .any(|r| r.as_slice() == ["dynamic", "13", "-", "6000000000000", "accepted"])
    );
    assert!(
        fx.iter()
            .any(|r| r.as_slice() == ["constructor", "65", "count", "65", "accepted"])
    );
    assert_eq!(fx.iter().filter(|r| r[0] == "operand").count(), 26);
    for f in fx.iter().filter(|r| r[0] == "operand") {
        let expected = records(EXPECTED)
            .into_iter()
            .find(|r| r[0] == f[1])
            .unwrap();
        assert_eq!(
            adj::registry(expected[2]).unwrap().value,
            Some(f[3].parse().unwrap())
        );
    }
    assert_eq!(fx.iter().filter(|r| r[0] == "edge").count(), 100);
    for f in fx.iter().filter(|r| r[0] == "edge") {
        assert_eq!(
            f[4],
            if f[1].ends_with(":below") || f[1].ends_with(":above") || f[1].ends_with(":boolean") {
                "refused"
            } else {
                "accepted"
            }
        );
    }
}
