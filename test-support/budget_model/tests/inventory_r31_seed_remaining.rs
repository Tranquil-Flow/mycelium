// SPDX-License-Identifier: AGPL-3.0-only
//! STATIC source-review accounting; no new numeric ceiling or runtime enforcement.
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact historical four-discovery/five-review projection"
)]
mod r32_verifier_admission;
#[path = "common/r36_stats_admission.rs"]
#[allow(dead_code, reason = "exact three-key historical projection")]
mod r36_stats_admission;
#[path = "common/r50_recovery_forward_admission.rs"]
#[allow(dead_code, reason = "exact two-key historical projection")]
mod r50_recovery_forward_admission;
use budget_model::inventory::{adjudication as adj, candidates};
use std::collections::BTreeMap;
const KEYS: [&str; 3] = [
    "mycelium_seed/state.py:827:16:dict-field:lease_expires_at",
    "mycelium_seed/operator.py:641:19:comparison:member['lease_expires_at']",
    "mycelium_seed/state.py:830:16:dict-field:next_heartbeat_due_at",
];
#[test]
fn exact_persisted_seed_decoder_review_requires_own_origin() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in KEYS {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == key)
            .expect("R31 STATIC missing exact persisted seed review");
        assert_eq!(e.verdict, "source-unbounded");
        let r = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
        assert_eq!(r.unit, "not-applicable");
        assert!(r.value.is_none());
        assert!(r.budget.is_empty());
    }
}

#[path = "common/oracle_blobs.rs"]
mod oracle_blobs;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;
use std::collections::BTreeSet;
const HOLD: &str = "mycelium_seed/plan_binding.py:105:12:keyword:expires_at";

#[test]
fn exact_cohort_preserves_plan105_and_all_unselected_pending() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let own = r31_seed_admission::admitted(&p);
    assert_eq!(own, BTreeSet::from(KEYS));
    let prior = include_str!("../data/python-r31-prior-pending.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(prior.as_bytes()),
        "bda9cb8048677c7631a2922ff6042adba6bea8a16ec377db62f0d911a97b30fb"
    );
    let before: BTreeSet<_> = prior.lines().skip(1).collect();
    assert_eq!(before.len(), 69);
    assert!(own.is_subset(&before));
    let mut all = budget_model::inventory::candidate_keys().unwrap();
    let rows = budget_model::inventory::all_reviewed().unwrap();
    let mut reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    assert_eq!(rows.len(), reviewed.len());
    r32_verifier_admission::remove_from(&mut all, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    let after: BTreeSet<_> = all.difference(&reviewed).map(String::as_str).collect();
    assert_eq!(after, before.difference(&own).copied().collect());
    assert_eq!((all.len(), reviewed.len(), after.len()), (8258, 8192, 66));
    assert!(after.contains(HOLD));
    assert!(!p.entries.iter().any(|e| e.key == HOLD));
    for key in KEYS {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        for field in [
            "key",
            "target",
            "reviewer",
            "verdict",
            "citations",
            "rationale",
        ] {
            let mut bad = e.clone();
            match field {
                "key" => bad.key = HOLD.into(),
                "target" => bad.target = "PySourceUnboundedR28SeedHttpExpiryL968C8".into(),
                "reviewer" => bad.reviewer.push_str(" forged"),
                "verdict" => bad.verdict = "derived".into(),
                "citations" => bad.citations.push_str(" forged"),
                _ => bad.rationale.push_str(" forged"),
            }
            assert!(!r31_seed_admission::exact(&bad), "{key} {field}");
        }
    }
}

#[test]
fn all_writer_proof_roles_and_finite_decoder_are_exact() {
    let p = adj::parse(&adj::TABLES).unwrap();
    for (i, key) in KEYS.into_iter().enumerate() {
        let proof: String = adj::TABLES
            .proofs_b
            .lines()
            .filter(|l| l.split('\t').next() == Some(key))
            .map(|l| format!("{l}\n"))
            .collect();
        assert_eq!(
            oracle_blobs::sha256_hex(proof.as_bytes()),
            PROOF_HASHES[i],
            "R31 STATIC exact writer proof {key}"
        );
        let origins: BTreeSet<_> = p.proofs[key]
            .iter()
            .filter(|h| {
                h.kind == "hop" && h.verdict == "origin" && h.path == "mycelium_seed/state.py"
            })
            .map(|h| h.line)
            .collect();
        let expected = if i == 2 {
            BTreeSet::from([240, 1592, 1738, 1878, 1915, 2215])
        } else {
            BTreeSet::from([1589, 1735, 1875, 1912, 2212])
        };
        assert_eq!(
            origins, expected,
            "all public save, renewal and both resume writers"
        );
        for line in [804, 805, 806, 809] {
            assert!(
                p.proofs[key]
                    .iter()
                    .any(|h| h.path == "mycelium_seed/state.py"
                        && h.line == line
                        && h.kind == "hop")
            );
        }
    }
}

#[test]
fn source_pins_and_full_scope_occurrences_are_rederived_from_f4_blobs() {
    assert!(
        oracle_blobs::oracle_present(),
        "source qualification requires the pinned oracle"
    );
    let p = adj::parse(&adj::TABLES).unwrap();
    let sources = oracle_blobs::blobs(&p.scope.keys().cloned().collect::<Vec<_>>());
    let files = include_str!("../data/python-r31-source-files.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(files.as_bytes()),
        "ba74fe27074337136badb3ca93a5c0d741364b2228132b71aca30e56dbd83b4a"
    );
    for row in files.lines().skip(1) {
        let (path, digest) = row.split_once('\t').unwrap();
        assert_eq!(oracle_blobs::sha256_hex(sources[path].as_bytes()), digest);
    }
    for key in KEYS {
        for h in &p.proofs[key] {
            if h.kind != "scan" {
                let line = oracle_blobs::line_of(&sources[&h.path], h.line).unwrap();
                assert_eq!(
                    oracle_blobs::sha256_hex(line.as_bytes()),
                    p.pins[&(h.path.clone(), h.line)]
                );
            } else {
                let found: BTreeSet<_> = sources
                    .iter()
                    .filter(|(path, _)| h.path == "*" || **path == h.path)
                    .flat_map(|(path, text)| {
                        text.lines()
                            .enumerate()
                            .filter(|(_, l)| l.contains(&h.needle))
                            .map(move |(i, _)| (path.as_str(), i + 1))
                    })
                    .collect();
                let expected: BTreeSet<_> = p.proofs[key]
                    .iter()
                    .filter(|r| {
                        r.kind == "occurrence"
                            && r.needle == h.needle
                            && (h.path == "*" || r.path == h.path)
                    })
                    .map(|r| (r.path.as_str(), r.line))
                    .collect();
                assert_eq!(found, expected, "{key} {}", h.needle);
                assert_eq!(found.len(), h.line);
            }
        }
    }
    let state = &sources["mycelium_seed/state.py"];
    assert!(
        oracle_blobs::line_of(state, 806)
            .unwrap()
            .contains("math.isfinite")
    );
    assert!(
        oracle_blobs::line_of(state, 827)
            .unwrap()
            .contains("lease_expires_at")
    );
    assert!(
        oracle_blobs::line_of(state, 830)
            .unwrap()
            .contains("next_heartbeat_due_at")
    );
    let operator = &sources["mycelium_seed/operator.py"];
    assert!(
        oracle_blobs::line_of(operator, 640)
            .unwrap()
            .contains("load_members")
    );
    assert!(
        oracle_blobs::line_of(operator, 641)
            .unwrap()
            .contains("lease_expires_at")
    );
    assert!(
        oracle_blobs::line_of(operator, 605)
            .unwrap()
            .contains("math.isfinite")
    );
}

#[test]
fn wrong_key_field_pin_origin_ttl_alias_and_dead_citation_refuse() {
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in KEYS {
        let p = adj::parse(&adj::TABLES).unwrap();
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        for alias in [
            "CancelAndCleanup",
            "PySourceUnboundedR28SeedHttpExpiryL968C8",
        ] {
            assert!(
                adj::row_of(&p, e, &cs, &|_| adj::registry(alias))
                    .unwrap_err()
                    .contains("unbounded-target-not-blocked-here")
            );
        }
        for other in KEYS.into_iter().filter(|k| *k != key).chain([
            HOLD,
            "mycelium_seed/state.py:827:17:dict-field:lease_expires_at",
        ]) {
            let mut bad = e.clone();
            bad.key = other.into();
            assert!(adj::row_of(&p, &bad, &cs, &adj::registry).is_err());
            assert!(!r31_seed_admission::exact(&bad));
        }
        let mut bad = e.clone();
        bad.citations = bad.citations.replace("@ f4e366e9", "@ 00000000");
        assert!(
            adj::row_of(&p, &bad, &cs, &adj::registry)
                .unwrap_err()
                .contains("invalid-citation")
        );
        let mut bad = e.clone();
        bad.key = key
            .replace("lease_expires_at", "wrong_field")
            .replace("next_heartbeat_due_at", "wrong_field");
        assert!(
            adj::row_of(&p, &bad, &cs, &adj::registry)
                .unwrap_err()
                .contains("unknown-candidate")
        );
        let mut view = adj::registry(&e.target).unwrap();
        view.blocked = Some("wrong source origin".into());
        assert!(
            adj::row_of(&p, e, &cs, &|_| Some(view.clone()))
                .unwrap_err()
                .contains("unbounded-target-not-blocked-here")
        );
        let original = adj::TABLES.proofs_b;
        for line in if key == KEYS[2] {
            [2215, 1878, 1915, 806]
        } else {
            [2212, 1875, 1912, 806]
        } {
            let needle = format!("{key}\thop\tmycelium_seed/state.py\t{line}\t-\t");
            let row = original.lines().find(|l| l.starts_with(&needle)).unwrap();
            let changed = original.replace(&format!("{row}\n"), "");
            assert_ne!(changed, original);
            let badp = adj::parse(&adj::Tables {
                proofs_b: &changed,
                ..adj::TABLES
            })
            .unwrap();
            // An occurrence may keep the generic citation alive; the exact complete-source proof must still refuse it.
            let exact: String = changed
                .lines()
                .filter(|l| l.split('\t').next() == Some(key))
                .map(|l| format!("{l}\n"))
                .collect();
            assert_ne!(
                oracle_blobs::sha256_hex(exact.as_bytes()),
                PROOF_HASHES[KEYS.iter().position(|k| *k == key).unwrap()]
            );
            if line == 806 {
                assert!(
                    adj::row_of(&badp, e, &cs, &adj::registry)
                        .unwrap_err()
                        .contains("unbounded-proof-dead-citation")
                );
            }
        }
        let changed: String = original
            .lines()
            .filter(|l| !(l.starts_with(&format!("{key}\thop\t")) && l.ends_with("\torigin")))
            .map(|l| format!("{l}\n"))
            .collect();
        let badp = adj::parse(&adj::Tables {
            proofs_b: &changed,
            ..adj::TABLES
        })
        .unwrap();
        assert!(
            adj::row_of(&badp, e, &cs, &adj::registry)
                .unwrap_err()
                .contains("unbounded-proof-origin")
        );
    }
}

#[test]
fn retained_sqlite_affinity_observations_are_not_direct_mapping_validation() {
    // Real extracted-original SQLite capture, not newly executed Python or Rust persistence.
    let fixture = include_str!("../data/fx-l1b-r31-seed-sqlite.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(fixture.as_bytes()),
        "49529f889c97d86a510a413a1e9ede5155c23a647aa3981ee556118e2a60fef8"
    );
    let rows: BTreeMap<_, _> = fixture
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            assert_eq!(f.len(), 10);
            (f[0], f)
        })
        .collect();
    assert_eq!(rows.len(), 11);
    for name in [
        "negative_large",
        "positive_large",
        "fraction",
        "zero",
        "bool_true",
        "numeric_text",
    ] {
        assert_eq!(rows[name][3], "accepted");
        assert_eq!(rows[name][6], "accepted");
        assert_eq!(rows[name][5], "real");
    }
    assert_eq!(rows["bool_true"][7], "1.0");
    assert_eq!(rows["numeric_text"][7], "1e+100");
    assert_eq!(rows["negative_large"][7], "-1e+100");
    for name in [
        "text",
        "positive_inf",
        "negative_inf",
        "direct_mapping_bool",
    ] {
        assert_eq!(rows[name][9], "seed_state_corrupt");
    }
    assert_eq!(rows["nan"][9], "seed_state_unavailable");
    let r24 = include_str!("../data/fx-l1b-r24-member-lease.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(r24.as_bytes()),
        "1b09c033d7fe0be0b56ecf736b603a0fead292598462b5d5de43982a2bf81f7d"
    );
}

const PROOF_HASHES: [&str; 3] = [
    "f0620abdffd38cc2b3f6c95a27d01d8562080aed4d2000b4b59d7de6523f7e34",
    "1fc4933269a607f8e0b1cb1d9d4adc6090c3f4c58d897150aa51df142de99403",
    "c520f9e36686ef673fff756c990cfac86d269a14061db1cae4e1a74a1e4898e0",
];
