// SPDX-License-Identifier: AGPL-3.0-only
//! Local serving_reserve only: no returned-container or input quantization claim.
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
use budget_model::inventory::{adjudication as adj, all_reviewed, candidates};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
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
const KEY: &str = "mycelium_swarm_artifacts.py:1076:4:assignment:serving_reserve";
const ID: &str = "PyKbR2SwarmRatioUpperL1079C16";
const THRESHOLD_KEY: &str = "mycelium_swarm_artifacts.py:1110:11:comparison:serving_reserve";
const THRESHOLD_ID: &str = "PyR21ServingReserveStrictThresholdL1110C11";

#[test]
fn strict_threshold_has_its_own_numeric_origin_not_either_keyword_endpoint() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let e = p
        .entries
        .iter()
        .find(|e| e.key == THRESHOLD_KEY)
        .expect("exact strict comparison remains pending");
    assert_eq!(e.verdict, "bounded");
    assert_eq!(e.target, THRESHOLD_ID);
    let row = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
    assert_eq!(row.unit, "permille");
    assert_eq!(row.value, Some(1000));
    assert_eq!(row.budget, THRESHOLD_ID);
    assert!(row.disposition.is_empty());
    assert!(
        e.rationale
            .contains("reject >=1.0; accepted full-policy local [0,1)")
    );
    assert!(all_reviewed().unwrap().iter().any(|r| r == &row));
}

#[test]
fn local_ratio_assignment_is_reviewed_as_derived_from_its_own_helper_endpoint() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let e = p
        .entries
        .iter()
        .find(|e| e.key == KEY)
        .expect("exact local assignment remains pending");
    assert_eq!(e.verdict, "derived");
    assert_eq!(e.target, ID);
    let row = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
    assert_eq!(row.disposition, "derived");
    assert_eq!(row.relation, ID);
    assert_eq!(row.unit, "permille");
    assert_eq!(row.value, None); // endpoint bound, never quantization of accepted floats
    assert!(row.budget.is_empty());
    assert!(all_reviewed().unwrap().iter().any(|r| r == &row));
}

const PATH: &str = "mycelium_swarm_artifacts.py";
const OTHER_ID: &str = "PyKbR2SwarmRatioUpperL1087C16";
// Static source-witness checks, not a port or execution of the Python helper.
fn source_contract(source: &str) -> bool {
    let lines: Vec<_> = source.lines().collect();
    let line = |n: usize| lines.get(n - 1).copied().unwrap_or("");
    line(319) == "def _number("
        && line(320)
            == "    value: object, code: str, *, minimum: float = 0.0, maximum: float = 1e18"
        && line(323) == "        type(value) not in {int, float}"
        && line(324) == "        or not math.isfinite(value)"
        && line(325) == "        or not minimum <= value <= maximum"
        && line(327) == "        _fail(code)"
        && line(328) == "    return float(value)"
        && line(1076) == "    serving_reserve = _number("
        && line(1077) == "        value.get(\"serving_traffic_reserve_ratio\"),"
        && line(1079) == "        maximum=1.0,"
        && line(1110) == "        or serving_reserve >= 1.0"
        && line(1115) == "        _fail(\"artifact_policy_invalid\")"
        && line(1116) == "    return copy.deepcopy(dict(value))"
        && source.matches("serving_reserve =").count() == 1
}
fn exact_numeric(
    p: &adj::Parsed,
    e: &adj::Entry,
    cs: &BTreeMap<String, budget_model::inventory::Candidate>,
    lookup: adj::Lookup,
) -> bool {
    let (id, origin, verdict) = match e.key.as_str() {
        KEY => (ID, 1079, "derived"),
        THRESHOLD_KEY => (THRESHOLD_ID, 1110, "bounded"),
        _ => return false,
    };
    let Some(c) = cs.get(&e.key) else {
        return false;
    };
    let identity = if e.key == KEY {
        c.line == 1076
            && c.column == 4
            && c.kind == "assignment"
            && c.expression
                == "_number(value.get('serving_traffic_reserve_ratio'), 'artifact_policy_invalid', maximum=1.0)"
    } else {
        c.line == 1110
            && c.column == 11
            && c.kind == "comparison"
            && c.expression == "serving_reserve >= 1.0"
    };
    identity
        && c.path == PATH
        && c.scope == "validate_policy"
        && c.symbol == "serving_reserve"
        && e.partition == 'B'
        && e.target == id
        && e.verdict == verdict
        && lookup(id).is_some_and(|v| {
            v.unit == "permille"
                && v.value == Some(1000)
                && v.source == Some(format!("{PATH}:{origin} @ f4e366e9"))
        })
        && adj::row_of(p, e, cs, lookup).is_ok_and(|r| {
            r.unit == "permille"
                && if verdict == "derived" {
                    r.disposition == "derived"
                        && r.relation == ID
                        && r.value.is_none()
                        && r.budget.is_empty()
                } else {
                    r.disposition.is_empty() && r.budget == THRESHOLD_ID && r.value == Some(1000)
                }
        })
}

#[test]
fn exact_numeric_review_refuses_independent_unit_value_origin_and_candidate_changes() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in [KEY, THRESHOLD_KEY] {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        assert!(exact_numeric(&p, e, &cs, &adj::registry), "{key}");
        for wrong in [ID, OTHER_ID, THRESHOLD_ID] {
            if wrong == e.target {
                continue;
            }
            let mut bad = e.clone();
            bad.target = wrong.into();
            assert!(
                !exact_numeric(&p, &bad, &cs, &adj::registry),
                "wrong valid ratio origin {key} {wrong}"
            );
        }
        for fault in ["unit", "value", "origin"] {
            let forged = |id: &str| {
                adj::registry(id).map(|mut v| {
                    match fault {
                        "unit" => v.unit = "entries",
                        "value" => v.value = Some(999),
                        _ => v.source = Some(format!("{PATH}:1076 @ f4e366e9")),
                    }
                    v
                })
            };
            assert!(
                !exact_numeric(&p, e, &cs, &forged),
                "independent {fault}: {key}"
            );
        }
        for fault in [
            "path",
            "line",
            "column",
            "scope",
            "symbol",
            "kind",
            "expression",
        ] {
            let mut bad = cs.clone();
            let c = bad.get_mut(key).unwrap();
            match fault {
                "path" => c.path.push_str("-wrong"),
                "line" => c.line += 1,
                "column" => c.column += 1,
                "scope" => c.scope.push_str("-wrong"),
                "symbol" => c.symbol.push_str("-wrong"),
                "kind" => c.kind.push_str("-wrong"),
                _ => c.expression.push_str("-wrong"),
            }
            assert!(
                !exact_numeric(&p, e, &bad, &adj::registry),
                "candidate {fault}: {key}"
            );
        }
        let mut bad = e.clone();
        bad.citations = bad.citations.replace("sha256=", "sha256=0");
        assert!(
            !exact_numeric(&p, &bad, &cs, &adj::registry),
            "citation: {key}"
        );
    }
}

#[test]
fn source_proof_binds_helper_guards_sole_local_store_and_strict_rejection() {
    let blobs = oracle_blobs::blobs(&[PATH.into()]);
    let source = &blobs[PATH];
    assert_eq!(
        oracle_blobs::sha256_hex(source.as_bytes()),
        "b9aa0b6db800eba1395ef15b4c1fa71d91be25e1fe8f3e007b6b54f520092514"
    );
    assert!(source_contract(source));
    let p = adj::parse(&adj::TABLES).unwrap();
    let lines: Vec<_> = source.lines().collect();
    for key in [KEY, THRESHOLD_KEY] {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        for n in (319..=328).chain(1076..=1080).chain(1101..=1116) {
            let digest = oracle_blobs::sha256_hex(lines[n - 1].as_bytes());
            assert!(
                e.citations
                    .contains(&format!("{PATH}:{n} @ f4e366e9 sha256={digest}")),
                "{key} :{n}"
            );
        }
    }
    for (before, after) in [
        (
            "type(value) not in {int, float}",
            "isinstance(value, (int, float))",
        ),
        ("or not math.isfinite(value)", "or False"),
        ("or not minimum <= value <= maximum", "or False"),
        ("    return float(value)", "    return value"),
        (
            "    serving_reserve = _number(",
            "    serving_reserve = callback(",
        ),
        (
            "        or serving_reserve >= 1.0",
            "        or serving_reserve > 1.0",
        ),
        (
            "    return copy.deepcopy(dict(value))",
            "    return {\"serving_traffic_reserve_ratio\": serving_reserve}",
        ),
    ] {
        assert!(source.contains(before));
        let bad = source.replace(before, after);
        assert_ne!(&bad, source);
        assert!(!source_contract(&bad), "{before}");
    }
    assert!(!source_contract(&format!(
        "{source}\nserving_reserve = 2.0\n"
    )));
}

#[test]
fn exact_cohort_refuses_returned_container_claim_and_all_unapproved_keys() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let own = r21_ratio_admission::admitted(&p);
    assert_eq!(own, BTreeSet::from([KEY, THRESHOLD_KEY]));
    for key in &own {
        let mut omitted = adj::parse(&adj::TABLES).unwrap();
        omitted.entries.retain(|e| e.key != *key);
        assert!(
            std::panic::catch_unwind(|| r21_ratio_admission::admitted(&omitted)).is_err(),
            "omission {key}"
        );
    }
    for key in &own {
        let e = p.entries.iter().find(|e| e.key == *key).unwrap();
        for fault in [
            "key",
            "reviewer",
            "verdict",
            "target",
            "citation",
            "returned-container",
            "partition",
        ] {
            let mut bad = e.clone();
            match fault {
                "key" => bad.key.push_str(":wrong"),
                "reviewer" => bad.reviewer.push_str("-wrong"),
                "verdict" => bad.verdict = "not-a-bound".into(),
                "target" => bad.target = OTHER_ID.into(),
                "citation" => bad.citations.push_str("-wrong"),
                "returned-container" => bad.rationale = "Returned mapping bounded and arbitrary accepted inputs quantized into permille".into(),
                _ => bad.partition = 'A',
            }
            assert!(!r21_ratio_admission::exact(&bad), "{fault}: {key}");
        }
        for other in p.partitions.keys().filter(|k| !own.contains(k.as_str())) {
            let mut bad = e.clone();
            bad.key.clone_from(other);
            assert!(!r21_ratio_admission::exact(&bad), "unapproved: {other}");
        }
    }
    let mut reviewed: BTreeSet<_> = all_reviewed().unwrap().into_iter().map(|r| r.key).collect();
    let mut census = budget_model::inventory::candidate_keys().unwrap();
    r25_count_admission::remove_from(&mut census, &mut reviewed);
    r27_delivery_wake_admission::remove_from(&mut census, &mut reviewed);
    r30_workflow_admission::remove_from(&mut census, &mut reviewed);
    r32_verifier_admission::remove_from(&mut census, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    r29_retry_admission::remove_from(&mut census, &mut reviewed);
    assert_eq!(census.len(), 8252);
    let r23 = r23_seed_invite_admission::admitted(&p);
    let r24 = r24_member_lease_admission::admitted(&p);
    let mut r26 = r26_seed_inputs_admission::admitted(&p);
    let r28 = r28_seed_http_admission::admitted(&p);
    assert!(r26.is_disjoint(&r28));
    r26.extend(r28);
    let r31 = r31_seed_admission::admitted(&p);
    assert!(r26.is_disjoint(&r31));
    r26.extend(r31);
    assert!(own.is_disjoint(&r26) && r23.is_disjoint(&r26) && r24.is_disjoint(&r26));
    assert!(own.is_disjoint(&r24));
    assert!(r23.is_disjoint(&r24));
    assert!(own.is_disjoint(&r23));
    assert_eq!(
        reviewed.len(),
        8174 + own.len() + r23.len() + r24.len() + r26.len()
    );
    assert_eq!(
        census.difference(&reviewed).count(),
        78 - own.len() - r23.len() - r24.len() - r26.len()
    );
}

#[test]
fn retained_original_oracle_controls_distinguish_helper_and_full_policy_domains() {
    // Reused FX controls, not a new Python execution on the product/test path.
    let text = include_str!("../data/fx-l1b-r21-ratio-controls.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(text.as_bytes()),
        "87df29445e3f19bca63ebdb0241f7b7b430ffcca23c2d3839ec4cb6ed527e795"
    );
    let rows: BTreeMap<_, _> = text
        .lines()
        .skip(1)
        .map(|l| l.split_once('\t').unwrap())
        .collect();
    assert_eq!(rows.len(), 17);
    assert_eq!(rows["ratio_helper_one"], "accept");
    assert_eq!(rows["ratio_helper_above_one"], "reject");
    assert_eq!(rows["ratio_full_policy_baseline"], "accept"); // original 0.5
    assert_eq!(
        rows["ratio_policy_serving_traffic_reserve_ratio_float_0.0"],
        "accept"
    );
    for name in [
        "ratio_policy_serving_traffic_reserve_ratio_float_1.0",
        "ratio_policy_serving_traffic_reserve_ratio_float_1.0000000000000002",
        "ratio_policy_serving_traffic_reserve_ratio_float_nan",
        "ratio_policy_serving_traffic_reserve_ratio_float_inf",
        "ratio_policy_serving_traffic_reserve_ratio_bool_True",
        "ratio_policy_serving_traffic_reserve_ratio_ComparisonInt_1",
    ] {
        assert_eq!(rows[name], "reject", "{name}");
    }
    assert_eq!(
        rows["ratio_policy_minimum_predicted_improvement_ratio_float_1.0"],
        "accept"
    );
    assert_eq!(
        rows["ratio_policy_minimum_predicted_improvement_ratio_float_0.0"],
        "reject"
    );
}
