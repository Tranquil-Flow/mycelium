// SPDX-License-Identifier: AGPL-3.0-only
//! Exact helper-invocation operands, never transitive constructor or caller caps.
use budget_model::inventory::{Candidate, adjudication as adj, candidates};
use std::collections::BTreeMap;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only source helper")]
mod oracle_blobs;
const FX: &str = include_str!("../data/fx-kb-recovery-r1.tsv");
fn census() -> BTreeMap<String, Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}
fn cases() -> Vec<Vec<&'static str>> {
    FX.lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect()
}
fn matches_fixture(
    e: &adj::Entry,
    f: &[&str],
    p: &adj::Parsed,
    cs: &BTreeMap<String, Candidate>,
    lookup: adj::Lookup,
) -> bool {
    e.key == f[0]
        && e.target == f[1]
        && e.verdict == "bounded"
        && e.reviewer == "L1b-KB-r1-recovery"
        && e.rationale.contains(&format!("direction={};", f[4]))
        && adj::row_of(p, e, cs, lookup).is_ok_and(|r| {
            r.unit == f[2] && r.value == Some(f[3].parse().unwrap()) && r.budget == f[1]
        })
}
#[test]
fn recovery_seventy_operands_equal_independent_executed_fixture() {
    assert_eq!(
        oracle_blobs::sha256_hex(FX.as_bytes()),
        "58e7b8be1d104c4e4ead4829bf999edde7c282385af5c2cb97d1a757c54a934b"
    );
    let fs = cases();
    assert_eq!(fs.len(), 70);
    let cs = census();
    let p = adj::parse(&adj::TABLES).unwrap();
    for f in fs {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing KB recovery proposal");
        assert!(matches_fixture(e, &f, &p, &cs, &adj::registry), "{}", f[0]);
        let r = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
        assert!(adj::validate(&r, &cs));
        assert_eq!(cs[f[0]].expression, f[3]);
        assert_eq!(
            cs[f[0]].symbol,
            if f[4] == "lower" {
                "minimum"
            } else {
                "maximum"
            }
        );
    }
}
#[test]
fn recovery_helper_and_call_spans_are_exact_pinned_source() {
    let path = "mycelium_a6_recovery.py".to_string();
    let blobs = oracle_blobs::blobs(std::slice::from_ref(&path));
    let source = &blobs[&path];
    assert_eq!(
        oracle_blobs::sha256_hex(source.as_bytes()),
        "54e970d57103066027d47f64e0ab3892634a2262ec60a63a1710d67cfb85cce3"
    );
    let lines: Vec<_> = source.lines().collect();
    for f in cases() {
        for (start, end, digest) in [(6, 7, 8), (9, 10, 11)] {
            let start: usize = f[start].parse().unwrap();
            let end: usize = f[end].parse().unwrap();
            assert_eq!(
                oracle_blobs::sha256_hex(lines[start - 1..end].join("\n").as_bytes()),
                f[digest],
                "{}",
                f[0]
            );
        }
    }
}
#[test]
fn every_recovery_key_rejects_wrong_id_direction_value_unit_and_source_hash() {
    let cs = census();
    let p = adj::parse(&adj::TABLES).unwrap();
    let fs = cases();
    for f in &fs {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing KB recovery proposal");
        let other = fs.iter().find(|g| g[2] == f[2] && g[1] != f[1]).unwrap();
        let mut wrong = e.clone();
        wrong.target = other[1].into();
        assert!(
            !matches_fixture(&wrong, f, &p, &cs, &adj::registry),
            "wrong ID {}",
            f[0]
        );
        wrong = e.clone();
        wrong.rationale = wrong.rationale.replace(
            &format!("direction={};", f[4]),
            if f[4] == "lower" {
                "direction=upper;"
            } else {
                "direction=lower;"
            },
        );
        assert_ne!(wrong.rationale, e.rationale);
        assert!(
            !matches_fixture(&wrong, f, &p, &cs, &adj::registry),
            "swapped direction {}",
            f[0]
        );
        for fault in ["value", "unit", "provenance"] {
            let lookup = |name: &str| {
                let mut v = adj::registry(name)?;
                if name == f[1] {
                    match fault {
                        "value" => v.value = v.value.map(|v| v + 1),
                        "unit" => v.unit = "wrong-dimension",
                        _ => v.source = Some("wrong.py:1 @ f4e366e9".into()),
                    }
                }
                Some(v)
            };
            assert!(
                !matches_fixture(e, f, &p, &cs, &lookup),
                "{fault}: {}",
                f[0]
            );
        }
        wrong = e.clone();
        wrong.citations = wrong.citations.replace("sha256=", "sha256=0");
        assert!(
            adj::row_of(&p, &wrong, &cs, &adj::registry)
                .unwrap_err()
                .starts_with("invalid-citation:")
        );
    }
}
#[test]
fn unrelated_dynamic_negative_floor_and_unchecked_constructor_rows_are_not_borrowed() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let proposed: Vec<_> = p
        .entries
        .iter()
        .filter(|e| e.reviewer == "L1b-KB-r1-recovery")
        .collect();
    assert_eq!(proposed.len(), 70);
    for line in [150, 158, 300, 468, 472, 590, 591] {
        assert!(!proposed.iter().any(|e| {
            e.key
                .starts_with(&format!("mycelium_a6_recovery.py:{line}:"))
        }));
    }
    let bypass = p
        .entries
        .iter()
        .find(|e| e.key == "mycelium_m16_runtime.py:230:27:manual-bound:maximum_reservations")
        .unwrap();
    assert_eq!(bypass.verdict, "source-unbounded");
    assert_eq!(
        bypass.target,
        "PySourceUnboundedR16M16AcceptedReservationCapacityUnbounded"
    );
    assert_ne!(bypass.target, "PyR16M16BuilderReservations");
}
