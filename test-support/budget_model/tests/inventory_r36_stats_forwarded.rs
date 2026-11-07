// SPDX-License-Identifier: AGPL-3.0-only
//! R36 STATIC manual source admission. No product-runtime or JSON magnitude claim.
#[path = "common/r50_recovery_forward_admission.rs"]
#[allow(dead_code, reason = "exact two-key historical projection")]
mod r50_recovery_forward_admission;
use budget_model::inventory::{self, adjudication as adj};
use budgets::table::{Provenance, Purpose, Value};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/r36_stats_admission.rs"]
#[allow(dead_code, reason = "historical projection used by sibling suites")]
mod cohort;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared immutable oracle reader")]
mod oracle_blobs;
const SOURCE: &str = include_str!("../data/python-r36-source-pins.tsv");
const FX: &str = include_str!("../data/fx-l1b-r36-stats.tsv");
fn census() -> BTreeMap<String, inventory::Candidate> {
    inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}
#[test]
fn exact_three_forwarded_operands_have_own_blocked_reviews() {
    let p = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(
        cohort::validate(&p, &cohort::lookup),
        Ok(()),
        "R36 STATIC exact three-key admission"
    );
    let cs = census();
    for key in cohort::admitted(&p) {
        let e = p
            .forwarded_thresholds
            .iter()
            .find(|e| e.key == key)
            .unwrap();
        let row = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
        assert_eq!(row.disposition, "source-unbounded");
        assert_eq!(row.relation, e.target);
        assert_eq!(row.value, None);
        assert!(e.rationale.contains("not the validated value itself"));
        assert!(
            e.rationale
                .contains("Parameter defaults do not cap overrides")
        );
    }
}
#[test]
fn separate_table_union_and_negative_controls() {
    let cs = census();
    let good = adj::TABLES;
    let rows = adj::rows_with(&good, &cs, &adj::registry).unwrap();
    let p = adj::parse(&good).unwrap();
    let base: BTreeSet<_> = p.entries.iter().map(|e| e.key.as_str()).collect();
    let separate: BTreeSet<_> = p
        .forwarded_thresholds
        .iter()
        .map(|e| e.key.as_str())
        .collect();
    assert_eq!(separate, BTreeSet::from(cohort::KEYS));
    assert!(base.is_disjoint(&separate));
    assert_eq!(
        rows.iter().map(|r| r.key.as_str()).collect::<BTreeSet<_>>(),
        base.union(&separate).copied().collect()
    );
    assert_eq!(rows.len(), base.len() + separate.len());
    for key in cohort::KEYS {
        let row = rows.iter().find(|r| r.key == key).unwrap();
        assert!(adj::validate_with(row, &cs, &good, &adj::registry));
        let raw = good
            .forwarded_thresholds
            .lines()
            .find(|l| l.split('\t').next() == Some(key))
            .unwrap();
        let refuse = |bad: &adj::Tables, code: &str| {
            let errors = adj::rows_with(bad, &cs, &adj::registry).unwrap_err();
            assert!(errors.contains(&format!("{code}: {key}")), "{errors:?}");
            assert!(!adj::validate_with(row, &cs, bad, &adj::registry));
            println!("R40-REFUSED {key} {code}");
        };
        let both = format!("{}{raw}\n", good.b);
        refuse(&adj::Tables { b: &both, ..good }, "duplicate-verdict");
        let duplicate = format!("{}{raw}\n", good.forwarded_thresholds);
        refuse(
            &adj::Tables {
                forwarded_thresholds: &duplicate,
                ..good
            },
            "duplicate-verdict",
        );
        let missing_proof = good
            .proofs_b
            .lines()
            .filter(|l| l.split('\t').next() != Some(key))
            .collect::<Vec<_>>()
            .join("\n");
        refuse(
            &adj::Tables {
                proofs_b: &missing_proof,
                ..good
            },
            "unbounded-proof-missing",
        );
        let missing_pin_a = good
            .pins_a
            .lines()
            .filter(|l| !l.starts_with("mycelium_swarm_directory/stats_docs.py\t"))
            .collect::<Vec<_>>()
            .join("\n");
        let missing_pin_b = good
            .pins_b
            .lines()
            .filter(|l| !l.starts_with("mycelium_swarm_directory/stats_docs.py\t"))
            .collect::<Vec<_>>()
            .join("\n");
        refuse(
            &adj::Tables {
                pins_a: &missing_pin_a,
                pins_b: &missing_pin_b,
                ..good
            },
            "invalid-citation",
        );
        let mut fields: Vec<_> = raw.split('\t').map(str::to_string).collect();
        fields[3].clear();
        let missing_citation = good.forwarded_thresholds.replace(raw, &fields.join("\t"));
        refuse(
            &adj::Tables {
                forwarded_thresholds: &missing_citation,
                ..good
            },
            "invalid-citation",
        );
        fields = raw.split('\t').map(str::to_string).collect();
        let digest = fields[3]
            .split("sha256=")
            .nth(1)
            .unwrap()
            .split(' ')
            .next()
            .unwrap()
            .to_string();
        fields[3] = fields[3].replace(&digest, &"0".repeat(64));
        let forged = good.forwarded_thresholds.replace(raw, &fields.join("\t"));
        refuse(
            &adj::Tables {
                forwarded_thresholds: &forged,
                ..good
            },
            "invalid-citation",
        );
        // Mutations are injected strings: original table bytes never change.
        assert_eq!(adj::rows_with(&good, &cs, &adj::registry).unwrap(), rows);
        assert!(adj::validate_with(row, &cs, &good, &adj::registry));
        println!("R40-RESTORED {key}");
    }
    // Two valid same-line proofs must not borrow the same Blocked target across views.
    let moved = good
        .forwarded_thresholds
        .lines()
        .find(|l| l.starts_with(cohort::KEYS[1]))
        .unwrap();
    let changed = moved.replace(cohort::IDS[1], cohort::IDS[0]);
    let b = format!("{}{changed}\n", good.b);
    let separate = good.forwarded_thresholds.replace(&format!("{moved}\n"), "");
    let bad = adj::Tables {
        b: &b,
        forwarded_thresholds: &separate,
        ..good
    };
    let errors = adj::rows_with(&bad, &cs, &adj::registry).unwrap_err();
    assert!(
        errors
            .iter()
            .any(|e| e.starts_with("duplicate-unbounded-target:")),
        "{errors:?}"
    );
    assert!(!adj::validate_with(
        rows.iter().find(|r| r.key == cohort::KEYS[0]).unwrap(),
        &cs,
        &bad,
        &adj::registry
    ));
    let outside = format!("{}\n{}\n", adj::HEADER, good.b.lines().nth(1).unwrap());
    assert!(
        adj::parse(&adj::Tables {
            forwarded_thresholds: &outside,
            ..good
        })
        .err()
        .unwrap()
        .starts_with("forwarded-threshold-scope:")
    );
    assert_eq!(adj::rows_with(&good, &cs, &adj::registry).unwrap(), rows);
}
fn source_check(blobs: &BTreeMap<String, String>) -> bool {
    SOURCE.lines().skip(1).all(|l| {
        let f: Vec<_> = l.split('\t').collect();
        blobs
            .get(f[0])
            .is_some_and(|b| oracle_blobs::sha256_hex(b.as_bytes()) == f[1])
    })
}
#[test]
fn original_bodies_pins_scans_and_hops_are_exhaustive() {
    assert_eq!(
        oracle_blobs::sha256_hex(SOURCE.as_bytes()),
        "8001bac3616f05642edb788392e2b0577537ba4192a7161b45e0f0dfd569e6f7"
    );
    let p = adj::parse(&adj::TABLES).unwrap();
    cohort::admitted(&p);
    let paths = p.scope.keys().cloned().collect::<Vec<_>>();
    let blobs = oracle_blobs::blobs(&paths);
    assert!(source_check(&blobs));
    for key in cohort::KEYS {
        let proof = &p.proofs[key];
        for r in proof.iter().filter(|r| r.kind != "scan") {
            let line = oracle_blobs::line_of(&blobs[&r.path], r.line).unwrap();
            assert_eq!(
                oracle_blobs::sha256_hex(line.as_bytes()),
                p.pins[&(r.path.clone(), r.line)]
            );
        }
        for scan in proof.iter().filter(|r| r.kind == "scan") {
            let actual: BTreeSet<_> = blobs
                .iter()
                .filter(|(path, _)| scan.path == "*" || **path == scan.path)
                .flat_map(|(path, b)| {
                    b.lines()
                        .enumerate()
                        .filter(|(_, l)| l.contains(&scan.needle))
                        .map(|(i, _)| (path.as_str(), i + 1))
                })
                .collect();
            let expected: BTreeSet<_> = proof
                .iter()
                .filter(|r| {
                    r.kind == "occurrence"
                        && r.needle == scan.needle
                        && (scan.path == "*" || r.path == scan.path)
                })
                .map(|r| (r.path.as_str(), r.line))
                .collect();
            assert_eq!(actual, expected, "{key} {} {}", scan.path, scan.needle);
            assert_eq!(actual.len(), scan.line);
        }
    }
    let mut bad = blobs.clone();
    bad.get_mut("mycelium_swarm_directory/wire.py")
        .unwrap()
        .push_str("\n# tampered body\n");
    assert!(!source_check(&bad));
    bad = blobs.clone();
    bad.get_mut("mycelium_swarm_directory/stats_docs.py")
        .unwrap()
        .push_str("\n# tampered forwarding\n");
    assert!(!source_check(&bad));
    assert!(source_check(&blobs));
}
#[test]
fn independent_same_kind_targets_origins_and_proofs_are_refused() {
    let p = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(cohort::validate(&p, &cohort::lookup), Ok(()));
    for field in ["identity", "numeric", "purpose", "provenance"] {
        let lookup = |id: &str| {
            let mut e = cohort::lookup(id)?;
            if id == cohort::IDS[0] {
                match field {
                    "identity" => e = cohort::lookup(cohort::IDS[1]).unwrap(),
                    "numeric" => e.value = Value::Count(budgets::Entries::new(1)),
                    "purpose" => e.purpose = Purpose::Resource,
                    _ => e.provenance = Provenance::Blocked(cohort::PROVENANCE[1]),
                }
            }
            Some(e)
        };
        assert!(cohort::validate(&p, &lookup).is_err(), "{field}");
    }
    let cs = census();
    for (i, key) in cohort::KEYS.iter().enumerate() {
        let e = p
            .forwarded_thresholds
            .iter()
            .find(|e| e.key == *key)
            .unwrap();
        for field in ["target", "rationale", "citation", "reviewer"] {
            let mut bad = e.clone();
            match field {
                "target" => bad.target = cohort::IDS[(i + 1) % cohort::IDS.len()].into(),
                "rationale" => bad.rationale.push_str(" wrong domain"),
                "citation" => bad.citations = bad.citations.replace("f4e366e9", "00000000"),
                _ => bad.reviewer.push_str("-wrong"),
            }
            assert!(!cohort::exact(&bad), "{key} {field}");
        }
        for field in ["scan", "occurrence", "origin", "read"] {
            let mut bad = adj::parse(&adj::TABLES).unwrap();
            bad.proofs.get_mut(*key).unwrap().retain(|r| {
                if field == "origin" || field == "read" {
                    r.kind != "hop" || r.verdict != field
                } else {
                    r.kind != field
                }
            });
            assert_eq!(cohort::validate(&bad, &cohort::lookup), Err("exact proof"));
            assert!(
                adj::row_of(&bad, e, &cs, &adj::registry).is_err(),
                "{key} {field}"
            );
        }
    }
}
#[test]
fn exactly_three_old_closures_and_every_neighbor_remains_pending() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let own = cohort::admitted(&p);
    let all = inventory::candidate_keys().unwrap();
    let rows = inventory::all_reviewed().unwrap();
    let mut reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    assert_eq!(reviewed.len(), rows.len());
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    let pending: BTreeSet<_> = all.difference(&reviewed).map(String::as_str).collect();
    let prior: BTreeSet<_> = include_str!("../data/python-r36-prior-pending.tsv")
        .lines()
        .skip(1)
        .collect();
    assert_eq!(prior.len(), 65);
    assert!(own.is_subset(&prior));
    assert_eq!(pending, prior.difference(&own).copied().collect());
    assert_eq!((all.len(), reviewed.len(), pending.len()), (8262, 8200, 62));
    for key in [
        "mycelium_swarm_directory/stats_docs.py:45:41:validator-keyword:high",
        "mycelium_swarm_directory/stats_docs.py:76:35:validator-keyword:high",
        "mycelium_swarm_directory/stats_docs.py:80:41:validator-keyword:high",
    ] {
        assert!(pending.contains(key));
    }
}
#[test]
fn reused_real_overrides_are_distinct_from_json_and_signed_domains() {
    assert_eq!(
        oracle_blobs::sha256_hex(FX.as_bytes()),
        "a9760955784c9dec2e5fc2382a73493bf19198b1589f62d93e81f447868c1457"
    );
    let rows: BTreeMap<_, _> = FX
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            (f[0], f)
        })
        .collect();
    assert_eq!(rows.len(), 53);
    for r in rows.values() {
        assert_eq!(r[2], r[3]);
    }
    for name in [
        "upper_override_plain",
        "lower_override",
        "metric_direct_override",
    ] {
        assert_eq!((rows[name][1], rows[name][2]), ("component", "accept"));
    }
    for name in [
        "int_-1_int",
        "int_86400001_int",
        "int_True_bool",
        "int_1_IntSub",
    ] {
        assert_eq!(rows[name][2], "stats_number_invalid");
    }
    assert!(
        rows.values()
            .any(|r| r[1] == "plain-json-values" && r[2] != "accept")
    );
}
