// SPDX-License-Identifier: AGPL-3.0-only
//! Caller-supplied threshold domains are not ceilings on the validated value.
use budget_model::inventory::{self, adjudication as adj};
use std::collections::BTreeMap;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
const WITNESS: &str = include_str!("../data/python-r17-forward-domain.tsv");
const FX: &str = include_str!("../data/fx-l1b-r17-forward-domain.tsv");
fn records(text: &str) -> Vec<Vec<&str>> {
    text.lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect()
}
#[test]
fn six_forwarded_thresholds_have_exact_negative_proofs_not_default_caps() {
    assert_eq!(
        oracle_blobs::sha256_hex(WITNESS.as_bytes()),
        "165aee2eeb8e91005944a4ef0ed912e2b1d8f4451a2798eeb92431b34cf2f8e9"
    );
    assert_eq!(records(WITNESS).len(), 6);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let census = inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for f in records(WITNESS) {
        let e = parsed
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing forwarding-domain review");
        assert_eq!(
            (&*e.target, &*e.verdict, &*e.reviewer, e.partition),
            (f[1], "source-unbounded", "L1b-r17-forward-domain", 'A')
        );
        let v = adj::registry(f[1]).unwrap();
        assert!(v.blocked.is_some() && v.value.is_none());
        assert!(e.rationale.contains("not the validated value itself"));
        assert!(
            e.rationale
                .contains("Parameter defaults do not cap overrides")
        );
        let row = adj::row_of(&parsed, e, &census, &adj::registry).unwrap();
        assert_eq!(row.disposition, "source-unbounded");
        assert_eq!(row.relation, f[1]);
    }
}
#[test]
fn forwarding_sites_and_function_spans_are_exact_oracle_bytes() {
    let census = inventory::candidates().unwrap();
    let blobs = oracle_blobs::blobs(&["mycelium_platform_neutral/primitives.py".to_owned()]);
    for f in records(WITNESS) {
        let c = census.iter().find(|c| c.key == f[0]).unwrap();
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
                f[2],
                f[3].parse().unwrap(),
                f[4].parse().unwrap(),
                f[5],
                f[6],
                f[7],
                f[8],
                f[9]
            )
        );
        assert_eq!(oracle_blobs::sha256_hex(blobs[f[2]].as_bytes()), f[10]);
        assert_eq!(c.scope, f[13]);
        let line = oracle_blobs::line_of(&blobs[f[2]], c.line).unwrap();
        assert!(line.contains(&format!("{}=", f[6])));
        assert!(line.contains(&format!("{}(", f[14])));
    }
}
#[test]
fn forwarding_domain_refuses_numeric_targets_or_missing_scan_evidence() {
    let census = inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for f in records(WITNESS) {
        let mut parsed = adj::parse(&adj::TABLES).unwrap();
        let e = parsed
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing review")
            .clone();
        let mut wrong = e.clone();
        wrong.target = "PyR16M16BuilderReservations".into();
        assert!(
            adj::row_of(&parsed, &wrong, &census, &adj::registry)
                .unwrap_err()
                .starts_with("unbounded-target-not-blocked-here:")
        );
        parsed
            .proofs
            .get_mut(f[0])
            .unwrap()
            .retain(|p| p.kind != "occurrence");
        assert!(adj::row_of(&parsed, &e, &census, &adj::registry).is_err());
        let mut parsed = adj::parse(&adj::TABLES).unwrap();
        parsed
            .proofs
            .get_mut(f[0])
            .unwrap()
            .retain(|p| p.kind != "hop" || p.verdict != "origin");
        assert!(adj::row_of(&parsed, &e, &census, &adj::registry).is_err());
    }
}
#[test]
fn executed_non_null_overrides_exceed_defaults_without_inventing_value_caps() {
    assert_eq!(
        oracle_blobs::sha256_hex(FX.as_bytes()),
        "ab921421c4f5f764ddfd53fbac657076d719636ba580b8e27e738f40bcbc0b85"
    );
    let expected: BTreeMap<_, _> = records(WITNESS).into_iter().map(|f| (f[0], f)).collect();
    let rows = records(FX);
    assert_eq!(rows.len(), 6);
    for f in rows {
        assert_eq!(f[1], expected[f[0]][13]);
        assert_eq!(f[6], "accepted");
        assert_ne!(f[2], "None");
        assert!(f[4].parse::<f64>().unwrap() > 1e90);
        if expected[f[0]][6] == "minimum" {
            assert!(f[3].parse::<f64>().unwrap() > 1e90);
        }
        assert_eq!(adj::registry(expected[f[0]][1]).unwrap().value, None);
    }
}
