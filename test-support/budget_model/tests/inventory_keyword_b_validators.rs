// SPDX-License-Identifier: AGPL-3.0-only
//! Exact helper-invocation operands, never transitive constructor or caller caps.
use budget_model::inventory::{Candidate, adjudication as adj, candidates};
use std::collections::BTreeMap;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only source helper")]
mod oracle_blobs;
const FX: &str = include_str!("../data/fx-kb-validators-r1.tsv");
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
        && e.reviewer == "L1b-KB-r1-validators"
        && e.rationale.contains(&format!("direction={};", f[4]))
        && adj::row_of(p, e, cs, lookup).is_ok_and(|r| {
            r.unit == f[2] && r.value == Some(f[3].parse().unwrap()) && r.budget == f[1]
        })
}
#[test]
fn validators_fifty_operands_equal_independent_executed_fixture() {
    assert_eq!(
        oracle_blobs::sha256_hex(FX.as_bytes()),
        "64acf1f32ced1be8485175c6e82c377690bcac81745483a76d27b42f6fe068a6"
    );
    let fs = cases();
    assert_eq!(fs.len(), 50);
    let cs = census();
    let p = adj::parse(&adj::TABLES).unwrap();
    for f in fs {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing KB validators proposal");
        assert!(matches_fixture(e, &f, &p, &cs, &adj::registry), "{}", f[0]);
        let r = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
        assert!(adj::validate(&r, &cs));
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
fn validator_helper_call_and_symbol_witnesses_match_pinned_sources() {
    let fs = cases();
    let paths: std::collections::BTreeSet<String> = fs
        .iter()
        .map(|f| f[0].split(':').next().unwrap().to_string())
        .collect();
    let blobs = oracle_blobs::blobs(&paths.into_iter().collect::<Vec<_>>());
    for f in fs {
        let path = f[0].split(':').next().unwrap();
        let lines: Vec<_> = blobs[path].lines().collect();
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
fn every_validators_key_rejects_wrong_id_direction_value_unit_and_source_hash() {
    let cs = census();
    let p = adj::parse(&adj::TABLES).unwrap();
    let fs = cases();
    for f in &fs {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing KB validators proposal");
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
fn mixed_dimensions_and_caller_dependent_endpoints_remain_unadjudicated() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let proposed: Vec<_> = p
        .entries
        .iter()
        .filter(|e| e.reviewer == "L1b-KB-r1-validators")
        .collect();
    assert_eq!(proposed.len(), 50);
    for key in [
        "mycelium_m21_heterogeneous.py:244:66:validator-keyword:minimum",
        "mycelium_mobile/qwen_stage_manifest.py:235:72:validator-keyword:maximum",
        "mycelium_mobile/qwen_stage_manifest.py:236:75:validator-keyword:maximum",
        "mycelium_mobile/qwen_stage_manifest.py:237:64:validator-keyword:maximum",
    ] {
        assert!(!proposed.iter().any(|e| e.key == key));
    }
}
