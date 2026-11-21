// SPDX-License-Identifier: AGPL-3.0-only
//! STATIC registry/source contract, not release authentication or runtime enforcement.
#[path = "common/r36_stats_admission.rs"]
#[allow(dead_code, reason = "exact three-key historical projection")]
mod r36_stats_admission;
#[path = "common/r50_recovery_forward_admission.rs"]
#[allow(dead_code, reason = "exact two-key historical projection")]
mod r50_recovery_forward_admission;
use budgets::{
    BudgetId,
    table::{Provenance, Purpose, Value},
};
#[path = "common/r32_verifier_admission.rs"]
#[allow(dead_code)]
mod cohort;
#[test]
fn exact_four_discoveries_and_five_manual_reviews() {
    assert_eq!(cohort::keys().len(), 4);
    assert_eq!(cohort::reviews().len(), 5);
}
#[test]
fn exact_verifier_numeric_source_contract() {
    for (name, value, source) in [
        (
            "PyReleaseBundleVerifierMaxFileCountL24C0",
            4096,
            "mycelium_release_bundle/verifier.py:24 @ f4e366e9",
        ),
        (
            "PyReleaseBundleVerifierMaxPathComponentsL26C0",
            32,
            "mycelium_release_bundle/verifier.py:26 @ f4e366e9",
        ),
        (
            "PyReleaseBundleVerifierInventoryShippedCeilingL529C4",
            131073,
            "mycelium_release_bundle/verifier.py:529 @ f4e366e9",
        ),
    ] {
        let id =
            BudgetId::from_name(name).expect("R32 STATIC missing source-specific numeric target");
        let e = id.entry();
        assert!(
            matches!(e.value,Value::Count(n) if n.get()==value),
            "R32 STATIC exact numeric Count {name}"
        );
        assert_eq!(e.purpose, Purpose::Policy, "R32 STATIC Policy {name}");
        assert_eq!(
            e.provenance,
            Provenance::Source(source),
            "R32 STATIC exact source {name}"
        );
    }
}

use budget_model::inventory::{self, adjudication as adj};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
mod oracle_blobs;
const CENSUS: &str = include_str!("../data/python-candidates.tsv");
#[test]
fn lookup_injection_rejects_value_unit_purpose_origin_pin_and_identity() {
    let p = adj::parse(&adj::TABLES).unwrap();
    assert_eq!(cohort::validate(CENSUS, &p, &cohort::lookup), Ok(()));
    for (name, _, _) in cohort::IDS {
        for field in [
            "value", "unit", "purpose", "origin", "pin", "identity", "blocked",
        ] {
            let lookup = |s: &str| {
                let mut e = cohort::lookup(s)?;
                if s == name {
                    match field {
                        "value" => e.value = Value::Count(budgets::Entries::new(131072)),
                        "unit" => e.value = Value::Storage(budgets::Bytes::new(131073)),
                        "purpose" => e.purpose = Purpose::IncidentObservation,
                        "origin" => {
                            e.provenance = Provenance::Source(
                                "mycelium_release_bundle/verifier.py:26 @ f4e366e9 forged",
                            )
                        }
                        "pin" => {
                            e.provenance = Provenance::Source(
                                "mycelium_release_bundle/verifier.py:529 @ ef9ae61d",
                            )
                        }
                        "identity" => {
                            e.id = BudgetId::from_name("PyManagedControlMaxActiveWork").unwrap()
                        }
                        _ => e.value = Value::Blocked,
                    }
                }
                Some(e)
            };
            let expected = match field {
                "value" | "unit" | "blocked" => "numeric Count",
                "purpose" => "Policy",
                "identity" => "registry identity",
                _ => "source origin/pin",
            };
            assert_eq!(
                cohort::validate(CENSUS, &p, &lookup),
                Err(expected),
                "{name} {field}"
            );
        }
    }
}
#[test]
fn cited_wrong_id_passes_generic_manual_but_fails_exact_source_cohort() {
    let mut p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let e = p.entries.iter_mut().find(|e| e.key == cohort::OLD).unwrap();
    e.target = cohort::IDS[0].0.into();
    let e = e.clone();
    assert!(
        adj::row_of(&p, &e, &cs, &adj::registry).is_ok(),
        "generic manual accepts another cited numeric Count; this is not the exact-source guard"
    );
    assert_eq!(
        cohort::validate(CENSUS, &p, &cohort::lookup),
        Err("exact review record")
    );
}
#[test]
fn missing_root_or_guard_and_forged_review_are_rejected() {
    for key in cohort::reviews() {
        let mut p = adj::parse(&adj::TABLES).unwrap();
        p.entries.retain(|e| e.key != key);
        assert_eq!(
            cohort::validate(CENSUS, &p, &cohort::lookup),
            Err("exact review union")
        );
        let census = CENSUS
            .lines()
            .filter(|l| l.split('\t').next() != Some(key))
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(
            cohort::validate(&census, &adj::parse(&adj::TABLES).unwrap(), &cohort::lookup),
            Err("exact discovery record")
        );
    }
    for field in ["citations", "reviewer", "rationale", "verdict"] {
        let mut p = adj::parse(&adj::TABLES).unwrap();
        let e = p.entries.iter_mut().find(|e| e.key == cohort::OLD).unwrap();
        match field {
            "citations" => e.citations.clear(),
            "reviewer" => e.reviewer.push_str(" forged"),
            "verdict" => e.verdict = "bounded".into(),
            _ => e.rationale.push_str(" forged"),
        }
        assert!(
            cohort::validate(CENSUS, &p, &cohort::lookup).is_err(),
            "{field}"
        );
    }
}
fn source_contract(text: &str) -> Result<(), &'static str> {
    let line = |n: usize| oracle_blobs::line_of(text, n).unwrap_or("");
    if line(24) != "MAX_FILE_COUNT = 4096" || line(26) != "MAX_BUNDLE_PATH_COMPONENTS = 32" {
        return Err("literal roots");
    }
    if line(615) != "        or len(pure.parts) > MAX_BUNDLE_PATH_COMPONENTS"
        || line(676)
            != "    if not isinstance(entries, list) or not entries or len(entries) > MAX_FILE_COUNT:"
    {
        return Err("root guards");
    }
    if line(1163)
        != "        expected_files = {entry[\"path\"] for entry in entries} | {MANIFEST_FILENAME}"
    {
        return Err("manifest allowance");
    }
    if oracle_blobs::sha256_hex(text.as_bytes())
        != "4104abdfe13188d4de18eae61e7188a156abf418b4c2cc2cb217afc31a3968ad"
    {
        return Err("full pinned source");
    }
    Ok(())
}
#[test]
fn shipped_json_source_formula_and_all_citations_are_pinned() {
    assert!(oracle_blobs::oracle_present());
    let sources = oracle_blobs::blobs(&[cohort::PATH.into()]);
    let text = &sources[cohort::PATH];
    assert_eq!(source_contract(text), Ok(()));
    let line = |n: usize| text.lines().nth(n - 1).unwrap();
    assert!(line(334).contains("json.loads("));
    assert!(line(336).contains("object_pairs_hook=_unique_object"));
    assert!(line(1161).contains("_validate_body(body, result)"));
    assert!(line(1164).contains("_expected_directories"));
    assert!(line(1165).contains("_inventory("));
    assert!(line(1234).contains("_inventory("));
    // Source-derived ordinary-JSON ceiling; NOT a private-helper universal bound.
    let files = 4096u64;
    let components = 32u64;
    let manifest_allowance = 1u64;
    let ceiling = (files + manifest_allowance) + files * (components - 1);
    assert_eq!(ceiling, 131073);
    assert!(
        matches!(cohort::lookup(cohort::IDS[2].0).unwrap().value,Value::Count(n) if n.get()==ceiling)
    );
    let p = adj::parse(&adj::TABLES).unwrap();
    for ((path, n), digest) in &p.pins {
        if path == cohort::PATH {
            assert_eq!(oracle_blobs::sha256_hex(line(*n).as_bytes()), *digest);
        }
    }
    for (needle, code) in [
        ("MAX_FILE_COUNT = 4096", "literal roots"),
        (
            "or len(pure.parts) > MAX_BUNDLE_PATH_COMPONENTS",
            "root guards",
        ),
        ("len(entries) > MAX_FILE_COUNT:", "root guards"),
        (" | {MANIFEST_FILENAME}", "manifest allowance"),
    ] {
        assert!(text.contains(needle));
        assert_eq!(
            source_contract(&text.replacen(needle, "REMOVED", 1)),
            Err(code)
        );
    }
}
#[test]
fn synthesized_shapes_and_only_one_old_pending_closure() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in cohort::reviews() {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        let row = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
        assert_eq!(row.unit, "entries");
        if e.verdict == "derived" {
            assert_eq!(row.disposition, "derived");
            assert_eq!(row.relation, e.target);
            assert!(row.value.is_none());
            assert!(row.budget.is_empty());
        } else {
            assert_eq!(row.budget, e.target);
            assert!(row.value.is_some());
        }
    }
    let all = inventory::candidate_keys().unwrap();
    let rows = inventory::all_reviewed().unwrap();
    let mut reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    assert_eq!(rows.len(), reviewed.len());
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    let pending: BTreeSet<_> = all.difference(&reviewed).map(String::as_str).collect();
    let prior: BTreeSet<_> = include_str!("../data/python-r32-prior-pending.tsv")
        .lines()
        .skip(1)
        .collect();
    assert_eq!(prior.len(), 66);
    assert!(prior.contains(cohort::OLD));
    assert!(prior.is_disjoint(&cohort::keys()));
    assert_eq!(
        pending,
        prior
            .difference(&BTreeSet::from([cohort::OLD]))
            .copied()
            .collect()
    );
    assert_eq!((all.len(), reviewed.len(), pending.len()), (8262, 8197, 65));
    for key in [
        "mycelium_seed/plan_binding.py:105:12:keyword:expires_at",
        "mycelium_mobile/qwen_stage_manifest.py:235:72:validator-keyword:maximum",
        "mycelium_mobile/qwen_stage_manifest.py:236:75:validator-keyword:maximum",
        "mycelium_mobile/qwen_stage_manifest.py:237:64:validator-keyword:maximum",
    ] {
        assert!(pending.contains(key));
    }
}
#[test]
fn inherited_metadata_probes_are_hash_pinned_not_full_release_acceptance() {
    let count = include_str!("../data/fx-l1b-r32-count-guard.json");
    assert_eq!(
        oracle_blobs::sha256_hex(count.as_bytes()),
        "87e76539a4fe35abb99f39bb357b2f732d453167f019731df5bfe2745e586908"
    );
    let proof = include_str!("../data/python-r32-source-proof.json");
    assert_eq!(
        oracle_blobs::sha256_hex(proof.as_bytes()),
        "1c28d94f3011d67a0c698cd78a349b4f1b7305d97b549679152e5730b6025c71"
    );
    assert!(proof.contains("(4096+1)+4096*(32-1)"));
    assert!(proof.contains("\"waived\": false"));
}

#[test]
fn inherited_prefixes_and_manual_only_crosswalk_are_unchanged() {
    for (text, lines, digest, delta) in [
        (
            CENSUS,
            8259,
            "03fd7e3c529fd2b1202fa2f53913f52f09633be50e095d1b05ca10ec2d8c2992",
            4,
        ),
        (
            adj::TABLES.b,
            663,
            "292454bdcba5a4ba88c58c1a566b55899accb62e0695e4ba24e67629b1739206",
            5 + r36_stats_admission::admitted(&adj::parse(&adj::TABLES).unwrap()).len()
                + r50_recovery_forward_admission::admitted(&adj::parse(&adj::TABLES).unwrap())
                    .len()
                + r50_recovery_forward_admission::lane_a::admitted(
                    &adj::parse(&adj::TABLES).unwrap(),
                )
                .len(),
        ),
        (
            adj::TABLES.partitions,
            1301,
            "69aa28a909509d5ddcc08511598338d664490ea4f20cfca60fcf7b14fce9dfb4",
            4,
        ),
        (
            include_str!("../data/python-bounds.tsv"),
            6959,
            "75845da1d67e918aff14865911132f4f64347ca625af59aac66cb9b0095614fa",
            0,
        ),
        (
            include_str!("../data/python-final-review-sites.tsv"),
            153,
            "4b28619b6861e9d1e97c3160e350bb35c39640a710712f1961369fd77a8243ff",
            0,
        ),
        (
            include_str!("../data/python-current-relocations.tsv"),
            482,
            "b6474e542eec30bff1e72ea65b9d5842e740f9764c20be81421a274181b9db05",
            0,
        ),
    ] {
        let prefix: String = text.split_inclusive('\n').take(lines).collect();
        assert_eq!(oracle_blobs::sha256_hex(prefix.as_bytes()), digest);
        let forwarded_lines = if text == adj::TABLES.b {
            adj::TABLES.forwarded_thresholds.lines().skip(1).count()
        } else {
            0
        };
        assert_eq!(text.lines().count() + forwarded_lines, lines + delta);
    }
    assert_eq!(
        oracle_blobs::sha256_hex(include_str!("../data/python-r32-prior-pending.tsv").as_bytes()),
        "74c2f668f10f338b8b1fed3bd7a838e6c174b4fd83a1592470e36621e43b37c4"
    );
}
