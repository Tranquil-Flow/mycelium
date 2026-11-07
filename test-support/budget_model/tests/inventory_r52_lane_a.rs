// SPDX-License-Identifier: AGPL-3.0-only
//! DELTA52 static dispositions; no product ceiling or source predicate deletion.
use budget_model::inventory::{self, adjudication as adj};
use std::collections::BTreeSet;
#[path = "common/r50_recovery_forward_admission.rs"]
#[allow(dead_code, reason = "shared exact historical projections")]
mod cohort;
use cohort::lane_a;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "immutable oracle reader")]
mod oracle_blobs;

#[test]
fn parent_selected_two_dispositions_are_present() {
    let p = adj::parse(&adj::TABLES).unwrap();
    for key in [
        "mycelium_replica_contracts.py:146:42:validator-keyword:minimum",
        "mycelium_qualification/qualifier.py:777:42:validator-keyword:minimum",
    ] {
        let entries: Vec<_> = p.entries.iter().filter(|e| e.key == key).collect();
        assert_eq!(entries.len(), 1, "DELTA52 missing selected review {key}");
        assert_eq!(entries[0].verdict, "not-a-bound");
        assert_eq!(entries[0].target, "-");
        assert!(entries[0].rationale.len() >= adj::MIN_RATIONALE);
        assert!(lane_a::exact(entries[0]));
    }
    assert_eq!(lane_a::admitted(&p), BTreeSet::from(lane_a::KEYS));
}

#[test]
fn live_inventory_is_8262_8204_58() {
    let all = inventory::candidate_keys().unwrap();
    let rows = inventory::all_reviewed().unwrap();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    assert_eq!(reviewed.len(), rows.len());
    assert!(reviewed.is_subset(&all));
    assert_eq!(
        (all.len(), reviewed.len(), all.difference(&reviewed).count()),
        (8262, 8204, 58)
    );
    let mut historical = reviewed.clone();
    lane_a::remove_from_reviewed(&mut historical);
    let prior: BTreeSet<_> = all.difference(&historical).cloned().collect();
    let pending: BTreeSet<_> = all.difference(&reviewed).cloned().collect();
    assert_eq!(prior.len(), 60);
    assert_eq!(
        reviewed
            .difference(&historical)
            .map(String::as_str)
            .collect::<BTreeSet<_>>(),
        BTreeSet::from(lane_a::KEYS)
    );
    let selected: BTreeSet<_> = lane_a::KEYS.into_iter().map(str::to_string).collect();
    assert_eq!(pending, prior.difference(&selected).cloned().collect());
}

#[test]
fn exact_record_candidate_manifest_and_cohort_mutations_are_refused() {
    let cs = cohort::census();
    for key in lane_a::KEYS {
        for fault in [
            "key",
            "verdict",
            "target",
            "citations",
            "rationale",
            "reviewer",
            "partition",
            "missing",
            "duplicate",
            "forwarded",
            "neighbor",
        ] {
            let mut p = adj::parse(&adj::TABLES).unwrap();
            let index = p.entries.iter().position(|e| e.key == key).unwrap();
            match fault {
                "missing" => {
                    p.entries.remove(index);
                }
                "duplicate" => {
                    p.entries.push(p.entries[index].clone());
                }
                "forwarded" => {
                    let e = p.entries.remove(index);
                    p.forwarded_thresholds.push(e);
                }
                "neighbor" => {
                    let mut e = p.entries[index].clone();
                    e.key.push_str(":neighbor");
                    p.entries.push(e);
                }
                _ => {
                    let e = &mut p.entries[index];
                    match fault {
                        "key" => e.key.push_str(":wrong"),
                        "verdict" => e.verdict = "bounded".into(),
                        "target" => e.target = "PyKbQualifierLowerL779C65".into(),
                        "citations" => e.citations.push_str(" forged"),
                        "rationale" => e.rationale.push_str(" forged"),
                        "reviewer" => e.reviewer.push_str("-neighbor"),
                        _ => e.partition = 'A',
                    }
                    assert!(!lane_a::exact(e));
                }
            }
            assert!(
                lane_a::validate_with(&p, &cs, lane_a::PINS).is_err(),
                "{key} {fault}"
            );
            println!("R53-REFUSED {key} record-{fault}");
        }
        for fault in [
            "key",
            "path",
            "line",
            "column",
            "scope",
            "symbol",
            "expression",
            "kind",
            "source",
        ] {
            let mut bad = cs.clone();
            let c = bad.get_mut(key).unwrap();
            match fault {
                "key" => c.key.push_str(":wrong"),
                "path" => c.path.push_str(".wrong"),
                "line" => c.line += 1,
                "column" => c.column += 1,
                "scope" => c.scope.push_str("wrong"),
                "symbol" => c.symbol.push_str("wrong"),
                "expression" => c.expression.push_str("wrong"),
                "kind" => c.kind.push_str("wrong"),
                _ => c.source.push_str("wrong"),
            }
            let p = adj::parse(&adj::TABLES).unwrap();
            assert!(
                lane_a::validate_with(&p, &bad, lane_a::PINS).is_err(),
                "{key} {fault}"
            );
            println!("R53-REFUSED {key} candidate-{fault}");
        }
    }
    let p = adj::parse(&adj::TABLES).unwrap();
    for bad in [
        lane_a::PINS.replacen("record", "wrong", 1),
        format!("{}extra\n", lane_a::PINS),
        lane_a::PINS.lines().skip(1).collect::<Vec<_>>().join("\n"),
    ] {
        assert!(lane_a::validate_with(&p, &cs, &bad).is_err());
        println!("R53-REFUSED manifest-mutation");
    }
    assert_eq!(lane_a::validate_with(&p, &cs, lane_a::PINS), Ok(()));
}

#[test]
fn complete_citations_and_site_local_source_domains_remain_exact() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let fs = lane_a::pins(lane_a::PINS).unwrap();
    let paths: Vec<_> = fs
        .iter()
        .filter(|f| f[0] == "source")
        .map(|f| f[1].to_string())
        .collect();
    let blobs = oracle_blobs::blobs(&paths);
    for f in fs.iter().filter(|f| f[0] == "source") {
        assert_eq!(cohort::hash(blobs[f[1]].as_bytes()), f[3]);
    }
    for key in lane_a::KEYS {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        for part in e.citations.split(" | ") {
            let (site, digest) = part.split_once(" @ f4e366e9 sha256=").unwrap();
            let (path, line) = site.rsplit_once(':').unwrap();
            assert_eq!(
                cohort::hash(
                    oracle_blobs::line_of(&blobs[path], line.parse().unwrap())
                        .unwrap()
                        .as_bytes()
                ),
                digest
            );
        }
    }
    let replica = &blobs["mycelium_replica_contracts.py"];
    assert!(replica.contains("return type(value) is int and minimum <= value <= (1 << 63) - 1"));
    assert!(replica.contains("_integer(item, minimum=-(1 << 63))"));
    // The signed endpoint is not a policy cap; separate field floors remain intact.
    assert!(replica.contains("minimum=1"));
    let qualifier = &blobs["mycelium_qualification/qualifier.py"];
    assert!(qualifier.contains("_integer(elapsed, minimum=1)"));
    assert!(qualifier.contains("elapsed == completed - started"));
    assert!(qualifier.contains("completed > started"));
    let g19 = p.entries.iter().find(|e| e.key == lane_a::KEYS[1]).unwrap();
    assert!(g19.rationale.contains("floor(1 ns / 1000000)=0 ms"));
    assert!(g19.rationale.contains("refuses zero elapsed nanoseconds"));
    assert!(adj::registry("PyKbQualifierLowerL777C42").is_none());
    assert_eq!(budgets::Span::from_millis(0).millis(), 0);
}
