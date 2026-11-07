// SPDX-License-Identifier: AGPL-3.0-only
//! Exact two-key r19 endpoint admission; no accepted-container or quantization claim.
//! The exact ratio endpoint is representable already; full policy strictness is separate.
#[path = "common/r25_count_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r25_count_admission;
#[path = "common/r27_delivery_wake_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r27_delivery_wake_admission;
#[path = "common/r29_retry_admission.rs"]
#[allow(dead_code, reason = "exact additive discovery cohort")]
mod r29_retry_admission;
#[path = "common/r30_workflow_admission.rs"]
#[allow(dead_code, reason = "exact additive source discovery cohort")]
mod r30_workflow_admission;
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
use budget_model::inventory::{Candidate, adjudication as adj, candidates};
use budgets::table::{Provenance, Value};
use budgets::{BudgetId, Permille};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
#[path = "common/r19_ratio_admission.rs"]
mod r19_ratio_admission;
#[path = "common/r21_ratio_admission.rs"]
mod r21_ratio_admission;
#[path = "common/r23_seed_invite_admission.rs"]
mod r23_seed_invite_admission;
#[path = "common/r24_member_lease_admission.rs"]
mod r24_member_lease_admission;
#[path = "common/r26_seed_inputs_admission.rs"]
mod r26_seed_inputs_admission;
#[path = "common/r28_seed_http_admission.rs"]
mod r28_seed_http_admission;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;

const CASES: [(&str, &str, usize); 2] = [
    (
        "mycelium_swarm_artifacts.py:1079:16:validator-keyword:maximum",
        "PyKbR2SwarmRatioUpperL1079C16",
        1079,
    ),
    (
        "mycelium_swarm_artifacts.py:1087:16:validator-keyword:maximum",
        "PyKbR2SwarmRatioUpperL1087C16",
        1087,
    ),
];
fn exact(
    p: &adj::Parsed,
    e: &adj::Entry,
    cs: &BTreeMap<String, Candidate>,
    lookup: adj::Lookup,
) -> bool {
    let Some((_, id, line)) = CASES.iter().find(|c| c.0 == e.key) else {
        return false;
    };
    let path = "mycelium_swarm_artifacts.py";
    let own = format!("{path}:{line} @ f4e366e9");
    e.partition == 'B'
        && e.reviewer == "L1b-KB-r2-ratio"
        && e.verdict == "bounded"
        && e.target == *id
        && e.rationale.contains("direction=upper;")
        && e.rationale
            .contains("literal validator operand=1.0 exactly1000 permille;")
        && lookup(id).is_some_and(|v| {
            v.unit == "permille"
                && v.value == Some(1000)
                && v.source.as_deref() == Some(own.as_str())
        })
        && adj::row_of(p, e, cs, lookup).is_ok_and(|r| {
            r.unit == "permille"
                && r.value == Some(1000)
                && r.budget == *id
                && r.disposition.is_empty()
        })
}
#[test]
fn exact_two_ratio_ids_preserve_original_source_identity_and_unit() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<String, Candidate> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let own: BTreeSet<_> = p
        .entries
        .iter()
        .filter(|e| e.reviewer == "L1b-KB-r2-ratio")
        .map(|e| e.key.as_str())
        .collect();
    assert_eq!(own, CASES.iter().map(|c| c.0).collect());
    for (key, id, line) in CASES {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        assert!(exact(&p, e, &cs, &adj::registry));
        let entry = BudgetId::from_name(id).unwrap().entry();
        assert_eq!(entry.value, Value::Ratio(Permille::new(1000)));
        assert_eq!(
            entry.provenance,
            Provenance::Source(match line {
                1079 => "mycelium_swarm_artifacts.py:1079 @ f4e366e9",
                1087 => "mycelium_swarm_artifacts.py:1087 @ f4e366e9",
                _ => unreachable!(),
            })
        );
        assert_eq!(cs[key].expression, "1.0");
        assert_eq!(cs[key].symbol, "maximum");
        assert_eq!(cs[key].kind, "validator-keyword");
    }
}
#[test]
fn exact_source_helper_and_strict_full_policy_are_not_conflated() {
    let path = "mycelium_swarm_artifacts.py";
    let blobs = oracle_blobs::blobs(&[path.into()]);
    let lines: Vec<_> = blobs[path].lines().collect();
    assert!(lines[322].contains("type(value) not in {int, float}"));
    assert!(lines[323].contains("not math.isfinite(value)"));
    assert!(lines[324].contains("not minimum <= value <= maximum"));
    assert!(lines[1109].contains("serving_reserve >= 1.0"));
    assert!(lines[1110].contains("improvement <= 0.0"));
    let p = adj::parse(&adj::TABLES).unwrap();
    for (key, _, line) in CASES {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        for n in (319..=328)
            .chain((line - 3)..=(line + 1))
            .chain([1110, 1111])
        {
            let digest = oracle_blobs::sha256_hex(lines[n - 1].as_bytes());
            assert!(
                e.citations
                    .contains(&format!("{path}:{n} @ f4e366e9 sha256={digest}"))
            );
        }
    }
}
#[test]
fn each_ratio_proposal_refuses_wrong_key_direction_id_unit_value_and_source() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<String, Candidate> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for (key, _, _) in CASES {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        assert!(exact(&p, e, &cs, &adj::registry));
        for fault in ["key", "direction", "id", "verdict", "reviewer", "citation"] {
            let mut bad = e.clone();
            match fault {
                "key" => bad.key.push_str(":other"),
                "direction" => {
                    bad.rationale = bad
                        .rationale
                        .replace("direction=upper;", "direction=lower;")
                }
                "id" => {
                    bad.target = if bad.target == CASES[0].1 {
                        CASES[1].1.into()
                    } else {
                        CASES[0].1.into()
                    }
                }
                "verdict" => bad.verdict = "derived".into(),
                "reviewer" => bad.reviewer.push_str("-other"),
                _ => bad.citations = bad.citations.replace("sha256=", "sha256=0"),
            }
            assert!(!exact(&p, &bad, &cs, &adj::registry), "{key}: {fault}");
        }
        for fault in ["unit", "value", "source"] {
            let forged = |id: &str| {
                adj::registry(id).map(|mut v| {
                    match fault {
                        "unit" => v.unit = "entries",
                        "value" => v.value = Some(999),
                        _ => v.source = Some("mycelium_swarm_artifacts.py:1 @ f4e366e9".into()),
                    }
                    v
                })
            };
            assert!(!exact(&p, e, &cs, &forged), "{key}: {fault}");
        }
    }
}

#[test]
fn exact_additive_cohort_refuses_omissions_and_every_unapproved_neighbor() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let own = r19_ratio_admission::admitted(&p);
    let queue: BTreeSet<_> = include_str!("../data/python-kb-r1-queue.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(queue.len(), 386);
    for e in &p.entries {
        if own.contains(e.key.as_str()) {
            assert!(r19_ratio_admission::exact(e));
            for fault in [
                "key",
                "reviewer",
                "verdict",
                "target",
                "citation",
                "rationale",
                "partition",
            ] {
                let mut bad = e.clone();
                match fault {
                    "key" => bad.key.push_str(":other"),
                    "reviewer" => bad.reviewer.push_str("-other"),
                    "verdict" => bad.verdict = "derived".into(),
                    "target" => {
                        bad.target = if bad.target == CASES[0].1 {
                            CASES[1].1.into()
                        } else {
                            CASES[0].1.into()
                        }
                    }
                    "citation" => bad.citations.push_str("-other"),
                    "rationale" => bad.rationale.push_str("-other"),
                    _ => bad.partition = 'A',
                }
                assert!(!r19_ratio_admission::exact(&bad), "{fault}: {}", e.key);
            }
            for key in queue.difference(&own) {
                let mut bad = e.clone();
                bad.key = (*key).into();
                assert!(!r19_ratio_admission::exact(&bad), "unapproved key {key}");
            }
            let mut omitted = own.clone();
            assert!(omitted.remove(e.key.as_str()));
            assert_ne!(omitted, own);
        } else {
            assert!(!r19_ratio_admission::exact(e));
        }
    }
    let reviewed = budget_model::inventory::all_reviewed().unwrap();
    let mut keys = budget_model::inventory::candidate_keys().unwrap();
    let mut actual: BTreeSet<_> = reviewed.iter().map(|r| r.key.clone()).collect();
    r25_count_admission::remove_from(&mut keys, &mut actual);
    r27_delivery_wake_admission::remove_from(&mut keys, &mut actual);
    r30_workflow_admission::remove_from(&mut keys, &mut actual);
    r32_verifier_admission::remove_from(&mut keys, &mut actual);
    r36_stats_admission::remove_from_reviewed(&mut actual);
    r50_recovery_forward_admission::remove_from_reviewed(&mut actual);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut actual);
    r29_retry_admission::remove_from(&mut keys, &mut actual);
    assert_eq!(keys.len(), 8252);
    let mut incremental = r21_ratio_admission::admitted(&p);
    let r23 = r23_seed_invite_admission::admitted(&p);
    assert!(incremental.is_disjoint(&r23));
    incremental.extend(r23);
    let r24 = r24_member_lease_admission::admitted(&p);
    assert!(incremental.is_disjoint(&r24));
    incremental.extend(r24);
    let mut r26 = r26_seed_inputs_admission::admitted(&p);
    let r28 = r28_seed_http_admission::admitted(&p);
    assert!(r26.is_disjoint(&r28));
    r26.extend(r28);
    let r31 = r31_seed_admission::admitted(&p);
    assert!(r26.is_disjoint(&r31));
    r26.extend(r31);
    assert!(incremental.is_disjoint(&r26));
    incremental.extend(r26);
    assert_eq!(actual.len(), 8174 + incremental.len());
    assert_eq!(keys.difference(&actual).count(), 78 - incremental.len());
}
