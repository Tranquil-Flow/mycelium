// SPDX-License-Identifier: AGPL-3.0-only
//! R50 two-key STATIC source-unbounded admission, not a runtime ceiling.
use budget_model::inventory::{self, adjudication as adj};
use budgets::table::{Provenance, Purpose, Value};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/r50_recovery_forward_admission.rs"]
#[allow(dead_code, reason = "historical projection used by predecessor suites")]
mod cohort;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared immutable source reader")]
mod oracle_blobs;
const FX: &str = include_str!("../data/fx-l1b-r50-recovery-forward.tsv");
const FILE: &str = "mycelium_a6_recovery.py";

#[test]
fn exact_two_forwarded_recovery_operands_have_own_blocked_reviews() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(cohort::validate(&parsed, &cohort::lookup), Ok(()));
    let cs = cohort::census();
    for key in cohort::KEYS {
        let entries: Vec<_> = parsed.entries.iter().filter(|e| e.key == key).collect();
        assert_eq!(
            entries.len(),
            1,
            "R50 STATIC missing ordinary own-site review: {key}"
        );
        assert_eq!(entries[0].verdict, "source-unbounded");
        assert!(adj::registry(&entries[0].target).unwrap().blocked.is_some());
        assert!(cohort::exact(entries[0]));
        let row = adj::row_of(&parsed, entries[0], &cs, &adj::registry).unwrap();
        assert_eq!(row.disposition, "source-unbounded");
        assert_eq!(row.value, None);
        assert_eq!(row.relation, entries[0].target);
        assert!(adj::validate(&row, &cs));
    }
}

#[test]
fn exact_key_site_kind_threshold_and_canonical_review_refusals() {
    let cs = cohort::census();
    for key in cohort::KEYS {
        for fault in [
            "key",
            "site",
            "column",
            "kind",
            "threshold",
            "expression",
            "source",
        ] {
            let mut bad = cs.clone();
            let c = bad.get_mut(key).unwrap();
            match fault {
                "key" => c.key.push_str(":wrong"),
                "site" => c.line += 1,
                "column" => c.column += 1,
                "kind" => c.kind = "keyword".into(),
                "threshold" => c.symbol = "other_threshold".into(),
                "expression" => c.expression = "256".into(),
                _ => c.source.push_str(" # forged"),
            }
            let p = adj::parse(&adj::TABLES).unwrap();
            assert!(
                cohort::validate_with(&p, &bad, &cohort::lookup, cohort::SOURCE).is_err(),
                "{fault}"
            );
            println!("R51-REFUSED {key} candidate-{fault}");
        }
        for fault in [
            "key",
            "verdict",
            "target",
            "citations",
            "rationale",
            "reviewer",
            "partition",
        ] {
            let mut p = adj::parse(&adj::TABLES).unwrap();
            let e = p.entries.iter_mut().find(|e| e.key == key).unwrap();
            match fault {
                "key" => e.key.push_str(":wrong"),
                "verdict" => e.verdict = "bounded".into(),
                "target" => e.target = cohort::IDS[1].into(),
                "citations" => e.citations.push_str(" forged"),
                "rationale" => e.rationale.push_str(" forged"),
                "reviewer" => e.reviewer.push_str("-neighbor"),
                _ => e.partition = 'A',
            }
            if fault == "target" && key == cohort::KEYS[1] {
                e.target = cohort::IDS[0].into();
            }
            assert!(!cohort::exact(e), "{fault}");
            assert!(
                cohort::validate_with(&p, &cs, &cohort::lookup, cohort::SOURCE).is_err(),
                "{fault}"
            );
            println!("R51-REFUSED {key} review-{fault}");
        }
    }
}

#[test]
fn proof_scan_read_origin_producer_and_key_refusals() {
    let cs = cohort::census();
    for key in cohort::KEYS {
        for fault in [
            "scan",
            "read",
            "origin",
            "occurrence",
            "producer",
            "proof-key",
        ] {
            let mut p = adj::parse(&adj::TABLES).unwrap();
            let e = p.entries.iter().find(|e| e.key == key).unwrap().clone();
            if fault == "proof-key" {
                let proof = p.proofs.remove(key).unwrap();
                p.proofs.insert(format!("{key}:wrong"), proof);
            } else {
                let rows = p.proofs.get_mut(key).unwrap();
                match fault {
                    "scan" => rows.retain(|r| r.kind != "scan"),
                    "read" | "origin" => rows.retain(|r| r.kind != "hop" || r.verdict != fault),
                    "occurrence" => {
                        let at = rows.iter().position(|r| r.kind == "occurrence").unwrap();
                        rows.remove(at);
                    }
                    _ => {
                        let mut r = rows[0].clone();
                        r.line += 1;
                        r.verdict = "origin".into();
                        rows.push(r);
                    }
                }
            }
            assert!(
                cohort::validate_with(&p, &cs, &cohort::lookup, cohort::SOURCE).is_err(),
                "{fault}"
            );
            if fault != "producer" {
                assert!(adj::row_of(&p, &e, &cs, &adj::registry).is_err(), "{fault}");
            }
            println!("R51-REFUSED {key} proof-{fault}");
        }
    }
}

#[test]
fn target_value_purpose_provenance_and_pin_union_refusals() {
    let p = adj::parse(&adj::TABLES).unwrap();
    for id in cohort::IDS {
        for fault in ["missing", "identity", "value", "purpose", "provenance"] {
            let lookup = |name: &str| {
                let mut e = cohort::lookup(name)?;
                if name == id {
                    match fault {
                        "missing" => return None,
                        "identity" => {
                            e.id = cohort::lookup("PyReleaseBundleVerifierMaxFileCountL24C0")
                                .unwrap()
                                .id
                        }
                        "value" => e.value = Value::Count(budgets::Entries::new(256)),
                        "purpose" => e.purpose = Purpose::Operational(budgets::Phase::Unclassified),
                        _ => e.provenance = Provenance::Blocked("borrowed foreign site"),
                    }
                }
                Some(e)
            };
            assert!(cohort::validate(&p, &lookup).is_err(), "{id} {fault}");
            println!("R51-REFUSED {id} target-{fault}");
        }
    }
    for fault in ["missing", "extra", "changed"] {
        let mut p = adj::parse(&adj::TABLES).unwrap();
        let own = (FILE.to_string(), 150);
        match fault {
            "missing" => {
                p.pins.remove(&own).unwrap();
            }
            "extra" => {
                p.pins.insert((FILE.to_string(), 1), "0".repeat(64));
            }
            _ => {
                p.pins.insert(own, "0".repeat(64));
            }
        }
        assert!(cohort::validate(&p, &cohort::lookup).is_err(), "{fault}");
        println!("R51-REFUSED citation-pin-{fault}");
    }
    for fault in ["missing", "changed", "extra"] {
        let mut p = adj::parse(&adj::TABLES).unwrap();
        let own = cohort::lane_a::added_pins().into_keys().next().unwrap();
        match fault {
            "missing" => {
                p.pins.remove(&own).unwrap();
            }
            "changed" => {
                p.pins.insert(own, "0".repeat(64));
            }
            _ => {
                p.pins.insert(("r52-forged.py".into(), 1), "0".repeat(64));
            }
        }
        assert!(
            cohort::validate(&p, &cohort::lookup).is_err(),
            "R52 additive {fault}"
        );
        println!("R53-REFUSED additive-citation-pin-{fault}");
    }
    let missing = cohort::SOURCE
        .lines()
        .skip(1)
        .collect::<Vec<_>>()
        .join("\n");
    let extra = format!("{}unexpected.py\t{}\n", cohort::SOURCE, "0".repeat(64));
    assert!(cohort::source_pins(&missing).is_err());
    assert!(cohort::source_pins(&extra).is_err());
}

#[test]
fn immutable_root_path_full_bodies_and_all_scans_are_rederived() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let paths: Vec<_> = p.scope.keys().cloned().collect();
    let mut blobs = oracle_blobs::blobs(&paths);
    assert_eq!(cohort::source_blobs(&blobs, cohort::SOURCE), Ok(()));
    assert!(blobs.contains_key(FILE));
    assert!(!blobs.contains_key("build/0.1.0-preview.2/compiler-sources/mycelium_a6_recovery.py"));
    assert_eq!(
        cohort::hash(blobs[FILE].as_bytes()),
        "54e970d57103066027d47f64e0ab3892634a2262ec60a63a1710d67cfb85cce3"
    );
    assert!(blobs[FILE].contains("def _require_text(value: object, name: str, *, maximum: int = 256) -> None:\n    _text(value, name, maximum=maximum)"));
    assert!(blobs[FILE].contains("def _require_integer(value: object, name: str, *, minimum: int = 0) -> None:\n    _integer(value, name, minimum=minimum)"));
    for key in cohort::KEYS {
        let rows = &p.proofs[key];
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        let cited: BTreeSet<_> = e
            .citations
            .split(" | ")
            .map(|part| {
                let (site, rest) = part.split_once(" @ f4e366e9 sha256=").unwrap();
                let (path, line) = site.rsplit_once(':').unwrap();
                let line: usize = line.parse().unwrap();
                assert_eq!(
                    cohort::hash(
                        oracle_blobs::line_of(&blobs[path], line)
                            .unwrap()
                            .as_bytes()
                    ),
                    rest
                );
                (path.to_string(), line)
            })
            .collect();
        let proof_union: BTreeSet<_> = rows
            .iter()
            .filter(|r| r.kind != "scan")
            .map(|r| (r.path.clone(), r.line))
            .collect();
        assert_eq!(cited, proof_union);
        for scan in rows.iter().filter(|r| r.kind == "scan") {
            let mut found = BTreeSet::new();
            for (file, text) in &blobs {
                if scan.path != "*" && scan.path != *file {
                    continue;
                }
                for (i, line) in text.split('\n').enumerate() {
                    if line.contains(&scan.needle) {
                        found.insert((file.clone(), i + 1));
                    }
                }
            }
            let listed: BTreeSet<_> = rows
                .iter()
                .filter(|r| {
                    r.kind == "occurrence"
                        && r.needle == scan.needle
                        && (scan.path == "*" || scan.path == r.path)
                })
                .map(|r| (r.path.clone(), r.line))
                .collect();
            assert_eq!(found, listed, "{key} {}", scan.needle);
            assert_eq!(found.len(), scan.line);
        }
    }
    let original = blobs[FILE].clone();
    let forged = original.replace(
        "_text(value, name, maximum=maximum)",
        "_text(value, name, maximum=min(maximum, 256))",
    );
    assert_ne!(forged, original);
    blobs.insert(FILE.into(), forged);
    assert!(
        cohort::source_blobs(&blobs, cohort::SOURCE).is_err(),
        "producer backdoor"
    );
    blobs.insert(FILE.into(), original);
    assert_eq!(cohort::source_blobs(&blobs, cohort::SOURCE), Ok(()));
}

#[test]
fn real_oracle_overrides_do_not_erase_concrete_serialized_guards() {
    assert_eq!(
        cohort::hash(FX.as_bytes()),
        "121f0b594f13aea0a7192b092ca6b767f5b12dc212fa65b31dd1591bd702a5d6"
    );
    let rows: BTreeMap<_, _> = FX
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            (f[0], f)
        })
        .collect();
    assert_eq!(rows.len(), 26);
    for name in [
        "text-huge-maximum",
        "text-above-default",
        "integer-huge-minimum",
        "integer-negative",
        "serialized-identity-positive",
        "serialized-identity-integer-huge",
    ] {
        assert_eq!(rows[name][4], "accepted");
    }
    for name in [
        "text-over-maximum",
        "integer-below-huge",
        "integer-bool-value",
        "integer-float-value",
        "serialized-identity-text-over-default",
        "serialized-identity-integer-below-concrete-floor",
        "serialized-identity-integer-bool",
    ] {
        assert_eq!(rows[name][4], "ValueError");
    }
    for name in ["text-none-operand", "integer-none-operand"] {
        assert_eq!(rows[name][4], "TypeError");
    }
}

#[test]
fn inherited_prefixes_proofs_and_guarded_stats_are_unchanged() {
    let prefix: String = adj::TABLES.b.split_inclusive('\n').take(663).collect();
    assert_eq!(
        cohort::hash(prefix.as_bytes()),
        "292454bdcba5a4ba88c58c1a566b55899accb62e0695e4ba24e67629b1739206"
    );
    let r52 = cohort::lane_a::admitted(&adj::parse(&adj::TABLES).unwrap());
    let prior_b: String = adj::TABLES
        .b
        .split_inclusive('\n')
        .filter(|l| !cohort::KEYS.contains(&l.split('\t').next().unwrap()))
        .filter(|l| !r52.contains(l.split('\t').next().unwrap()))
        .collect();
    assert_eq!(
        cohort::hash(prior_b.as_bytes()),
        "cad1c32643baa0e89742f46ad33afd30ebddc2d586361d88091d9760e2b84fbe"
    );
    let prior_proofs: String = adj::TABLES
        .proofs_b
        .split_inclusive('\n')
        .filter(|l| !cohort::KEYS.contains(&l.split('\t').next().unwrap()))
        .collect();
    assert_eq!(
        cohort::hash(prior_proofs.as_bytes()),
        "5d8f8f5539dc4fdf19782c5c2b405d8d2ea5a9537f3ac97beb6482df43e118df"
    );
    let p = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(p.forwarded_thresholds.len(), 3);
    for key in [
        "mycelium_swarm_directory/stats_docs.py:21:30:validator-keyword:low",
        "mycelium_swarm_directory/stats_docs.py:21:40:validator-keyword:high",
        "mycelium_swarm_directory/stats_docs.py:31:26:validator-keyword:high",
        "mycelium_swarm_directory/stats_docs.py:45:41:validator-keyword:high",
        "mycelium_swarm_directory/stats_docs.py:76:35:validator-keyword:high",
        "mycelium_swarm_directory/stats_docs.py:80:41:validator-keyword:high",
    ] {
        assert!(!p.entries.iter().any(|e| e.key == key));
    }
    for key in cohort::KEYS {
        let raw = adj::TABLES
            .b
            .lines()
            .find(|l| l.split('\t').next() == Some(key))
            .unwrap();
        let duplicate = format!("{}{raw}\n", adj::TABLES.b);
        assert!(
            adj::rows_with(
                &adj::Tables {
                    b: &duplicate,
                    ..adj::TABLES
                },
                &cohort::census(),
                &adj::registry
            )
            .unwrap_err()
            .iter()
            .any(|e| e == &format!("duplicate-verdict: {key}"))
        );
        let mut p = adj::parse(&adj::TABLES).unwrap();
        p.entries.retain(|e| e.key != key);
        assert!(cohort::validate(&p, &cohort::lookup).is_err());
    }
}

#[test]
fn live_inventory_is_8262_8202_60_and_only_these_two_leave_prior_pending() {
    let all = inventory::candidate_keys().unwrap();
    let rows = inventory::all_reviewed().unwrap();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    assert_eq!(reviewed.len(), rows.len());
    let mut reviewed = reviewed;
    cohort::lane_a::remove_from_reviewed(&mut reviewed);
    let pending: BTreeSet<_> = all.difference(&reviewed).map(String::as_str).collect();
    let r36: BTreeSet<_> = include_str!("../data/python-r36-stats-cohort.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    let prior65: BTreeSet<_> = include_str!("../data/python-r36-prior-pending.tsv")
        .lines()
        .skip(1)
        .collect();
    let prior62: BTreeSet<_> = prior65.difference(&r36).copied().collect();
    assert_eq!(prior62.len(), 62);
    let own = cohort::admitted(&adj::parse(&adj::TABLES).unwrap());
    assert!(own.is_subset(&prior62));
    assert_eq!(pending, prior62.difference(&own).copied().collect());
    assert_eq!((all.len(), reviewed.len(), pending.len()), (8262, 8202, 60));
    let mut historical = reviewed;
    cohort::remove_from_reviewed(&mut historical);
    assert_eq!(
        all.difference(&historical)
            .map(String::as_str)
            .collect::<BTreeSet<_>>(),
        prior62
    );
}
