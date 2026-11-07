// SPDX-License-Identifier: AGPL-3.0-only
//! R17 numeric operand proofs. Floors do not cap the raw input or whole operation.
use budget_model::inventory::{self, adjudication as adj};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
const WITNESS: &str = include_str!("../data/python-r17-rate.tsv");
const FX: &str = include_str!("../data/fx-l1b-r17-rate.tsv");
fn records(text: &str) -> Vec<Vec<&str>> {
    text.lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect()
}
fn check(entries: &[adj::Entry], lookup: adj::Lookup<'_>) -> Result<(), String> {
    for f in records(WITNESS) {
        let e = entries
            .iter()
            .find(|e| e.key == f[0])
            .ok_or("missing-review")?;
        if (
            e.verdict.as_str(),
            e.target.as_str(),
            e.reviewer.as_str(),
            e.partition,
        ) != ("bounded", f[1], "L1b-r17-rate", 'A')
        {
            return Err("wrong-review".into());
        }
        let view = lookup(&e.target).ok_or("missing-id")?;
        if view.unit != f[2]
            || view.value != Some(f[3].parse().unwrap())
            || view.source != Some(format!("{}:{} @ f4e366e9", f[5], f[6]))
            || view.blocked.is_some()
        {
            return Err("wrong-quantity".into());
        }
        if f[4] == "lower"
            && (!e.target.contains("Floor") || !e.rationale.contains("NOT an upper ceiling"))
        {
            return Err("floor-laundered".into());
        }
        if !e.rationale.contains("successful helper return") {
            return Err("raw-input-domain".into());
        }
    }
    Ok(())
}
#[test]
fn exact_numeric_reviews_match_independent_executed_values() {
    assert_eq!(
        oracle_blobs::sha256_hex(WITNESS.as_bytes()),
        "e8c8738e98766709ab700254b13a960d38dc1425242360acfaa8b84c6d61bb89"
    );
    assert_eq!(records(WITNESS).len(), 1);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(check(&parsed.entries, &adj::registry), Ok(()));
    let reviewed = inventory::all_reviewed().unwrap();
    for f in records(WITNESS) {
        assert!(reviewed.iter().any(|r| r.key == f[0]));
    }
}
#[test]
fn every_operand_has_exact_source_scope_and_helper_witnesses() {
    let census = inventory::candidates().unwrap();
    let paths = records(WITNESS)
        .iter()
        .flat_map(|f| [f[5].to_owned(), f[15].to_owned()])
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let blobs = oracle_blobs::blobs(&paths);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    for f in records(WITNESS) {
        assert_eq!(f.len(), 23);
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
                f[5],
                f[6].parse().unwrap(),
                f[7].parse().unwrap(),
                f[8],
                f[9],
                f[10],
                f[11],
                f[12]
            )
        );
        assert_eq!(parsed.partitions[f[0]].0, 'A');
        assert_eq!(oracle_blobs::sha256_hex(blobs[f[5]].as_bytes()), f[13]);
        assert_eq!(
            oracle_blobs::sha256_hex(
                oracle_blobs::line_of(&blobs[f[5]], c.line)
                    .unwrap()
                    .as_bytes()
            ),
            f[14]
        );
        let start: usize = f[16].parse().unwrap();
        let end: usize = f[17].parse().unwrap();
        let helper = blobs[f[15]]
            .lines()
            .skip(start - 1)
            .take(end - start + 1)
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(oracle_blobs::sha256_hex(helper.as_bytes()), f[18]);
        assert!(helper.starts_with(&format!("def {}(", f[19])));
        assert_eq!(
            f[4],
            if c.symbol == "minimum" {
                "lower"
            } else {
                "upper"
            }
        );
    }
}
#[test]
fn every_numeric_review_refuses_wrong_ids_values_units_and_source() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(check(&parsed.entries, &adj::registry), Ok(()));
    let witness = records(WITNESS);
    for f in &witness {
        // There is only one rate id. Other registered identities are still refused;
        // the injected same-dimension source control below isolates provenance.
        let wrong_id = "CancelAndCleanup";
        let mut entries = parsed.entries.clone();
        entries.iter_mut().find(|e| e.key == f[0]).unwrap().target = wrong_id.into();
        assert_eq!(check(&entries, &adj::registry), Err("wrong-review".into()));
        for field in ["value", "unit", "source", "blocked"] {
            let lookup = |id: &str| {
                let mut v = adj::registry(id)?;
                if id == f[1] {
                    match field {
                        "value" => v.value = v.value.map(|n| n + 1),
                        "unit" => v.unit = "wrong-unit",
                        "source" => v.source = Some("wrong:1 @ f4e366e9".into()),
                        _ => v.blocked = Some("invented".into()),
                    }
                }
                Some(v)
            };
            assert_eq!(
                check(&parsed.entries, &lookup),
                Err("wrong-quantity".into()),
                "{} {field}",
                f[0]
            );
        }
        let mut entries = parsed.entries.clone();
        entries
            .iter_mut()
            .find(|e| e.key == f[0])
            .unwrap()
            .rationale = "generic numeric claim without domain proof".into();
        assert!(check(&entries, &adj::registry).is_err());
    }
}
#[test]
fn canonical_engine_refuses_wrong_ids_and_same_dimension_uncited_sources() {
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
            .expect("review missing")
            .clone();
        assert!(adj::row_of(&parsed, &e, &census, &adj::registry).is_ok());
        let mut bad = e.clone();
        bad.target = match f[2] {
            "bytes" => "PyR16FenceOwnerFileBytes",
            "milliseconds" => "CancelAndCleanup",
            "permille" => "PyRatioMyceliumA13MacosPublicHttpsDnsRetryFractionL46C0",
            _ => "PyR16M16BuilderReservations",
        }
        .into();
        assert!(
            adj::row_of(&parsed, &bad, &census, &adj::registry).is_err(),
            "{}",
            f[0]
        );
        let forged_same_dimension = |name: &str| {
            let mut view = adj::registry(name)?;
            if name == bad.target {
                view.unit = "entries-per-second";
                view.value = Some(0);
                view.source =
                    Some("mycelium_platform_neutral/runtime_efficiency.py:1 @ f4e366e9".into());
            }
            Some(view)
        };
        assert!(
            adj::row_of(&parsed, &bad, &census, &forged_same_dimension)
                .unwrap_err()
                .starts_with("target-not-bound-by-citation:")
        );
        bad = e;
        bad.citations.clear();
        assert!(adj::row_of(&parsed, &bad, &census, &adj::registry).is_err());
    }
}
#[test]
fn actual_helper_edges_preserve_bool_null_and_opposite_bounds() {
    assert_eq!(
        oracle_blobs::sha256_hex(FX.as_bytes()),
        "65ba978ca22a996121202c5f7f8815c655c84ebfc982229b5ee6198b997fd3f6"
    );
    let witness: BTreeMap<_, _> = records(WITNESS).into_iter().map(|f| (f[0], f)).collect();
    let mut counts = BTreeMap::new();
    for f in records(FX) {
        let w = &witness[f[0]];
        assert_eq!((f[1], f[2]), (w[2], w[3]));
        assert_eq!(
            adj::registry(w[1]).unwrap().value,
            Some(f[2].parse().unwrap())
        );
        let accepted = matches!(
            f[3],
            "floor" | "ceiling" | "within" | "large-unbounded" | "larger-unbounded"
        ) || (f[3] == "none" && w[19] == "nullable_finite");
        assert_eq!(f[5], if accepted { "accepted" } else { "refused" });
        if accepted {
            assert_eq!(f[4], f[6]);
        }
        *counts.entry(f[0]).or_insert(0usize) += 1;
    }
    assert_eq!(counts.len(), 1);
    assert!(counts.values().all(|n| *n == 9));
}

#[test]
fn rate_floor_is_not_an_entry_count_duration_or_capacity() {
    let w = records(WITNESS);
    let id = budgets::BudgetId::from_name(w[0][1]).unwrap();
    assert!(id.count().is_err());
    assert!(id.span(budgets::Entries::new(0)).is_err());
    assert_eq!(adj::registry(id.name()).unwrap().unit, "entries-per-second");
    assert_eq!(adj::registry(id.name()).unwrap().value, Some(0));
}
