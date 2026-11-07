// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/public_https.py.
//! L1b round 9, RU-024 / SE-L1B-RATIO-UNIT: `Value::Ratio(Permille)` is the unit of dimensionless fractions. Eleven
//! declaration-site ratio literals (`_DNS_RETRY_FRACTION = 0.5`, `memory_reserve_fraction`, ...) are reviewed under
//! `ratio-category:literal`: an exact decimal with at most three fraction digits and a value of at most 1, scaled to
//! permille without rounding, registered as `PyRatio*` ids with exact `path:line @ ef9ae61d` provenance. The other seven
//! rows of the `r2-ratio-unit` family (tuple-unpacked forwards, a derived duration, comparisons) stay pending.
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact four-discovery addition to the bounds-only gap"
)]
mod r32_verifier_admission;

#[path = "common/r29_retry_admission.rs"]
#[allow(
    dead_code,
    reason = "exact additive manual review outside bounds-only count"
)]
mod r29_retry_admission;
use budget_model::inventory::{
    Bound, Candidate, candidate_keys, candidates, checked_in, ratio, validate,
};
use budgets::table::{BUDGET_TABLE, Provenance, Value};
use budgets::{BudgetId, Permille};
use std::collections::{BTreeMap, BTreeSet};

/// (census key, permille, expected registry id): independent literals from the oracle source text.
const CASES: &[(&str, u64, &str)] = &[
    (
        "mycelium_a13_macos/public_https.py:46:0:assignment:_DNS_RETRY_FRACTION",
        500,
        "PyRatioMyceliumA13MacosPublicHttpsDnsRetryFractionL46C0",
    ),
    (
        "mycelium_a13_macos/worker.py:309:0:assignment:_NATIVE_RESPONSE_TRANSIT_MARGIN",
        700,
        "PyRatioMyceliumA13MacosWorkerNativeResponseTransitMarginL309C0",
    ),
    (
        "mycelium_layer_planner/contracts.py:219:4:assignment:memory_reserve_fraction",
        100,
        "PyRatioMyceliumLayerPlannerContractsMemoryReserveFractionL219C4",
    ),
    (
        "mycelium_live/local_preparer.py:1101:12:dict-field:serving_traffic_reserve_ratio",
        400,
        "PyRatioMyceliumLiveLocalPreparerServingTrafficReserveRatioL1101C12",
    ),
    (
        "mycelium_live/model_capacity.py:268:28:keyword:memory_reserve_fraction",
        100,
        "PyRatioMyceliumLiveModelCapacityMemoryReserveFractionL268C28",
    ),
    (
        "mycelium_speculative_decode.py:77:0:assignment:DEADLINE_WARM_P95_MULTIPLIER",
        750,
        "PyRatioMyceliumSpeculativeDecodeDeadlineWarmP95MultiplierL77C0",
    ),
    (
        "scripts/build_a5_replica_pair_graph.py:98:12:dict-field:memory_reserve_fraction",
        0,
        "PyRatioScriptsBuildA5ReplicaPairGraphMemoryReserveFractionL98C12",
    ),
    (
        "scripts/build_m13_physical_control_plane.py:154:8:dict-field:memory_reserve_fraction",
        0,
        "PyRatioScriptsBuildM13PhysicalControlPlaneMemoryReserveFractionL154C8",
    ),
    (
        "scripts/build_m17_model_feasibility.py:123:30:keyword:memory_reserve_fraction",
        100,
        "PyRatioScriptsBuildM17ModelFeasibilityMemoryReserveFractionL123C30",
    ),
    (
        "scripts/build_m18_physical_control_plane.py:33:4:dict-field:memory_reserve_fraction",
        0,
        "PyRatioScriptsBuildM18PhysicalControlPlaneMemoryReserveFractionL33C4",
    ),
    (
        "scripts/probe_a5_replica_gain_threshold.py:77:12:dict-field:memory_reserve_fraction",
        0,
        "PyRatioScriptsProbeA5ReplicaGainThresholdMemoryReserveFractionL77C12",
    ),
];

/// The rest of the `r2-ratio-unit` family: not literal declarations of a pinned ratio.
const STAY_PENDING: &[&str] = &[
    "mycelium_layer_planner/contracts.py:241:15:comparison:0",
    "mycelium_layer_planner/gossip_adapter.py:325:24:comparison:reserve",
    "mycelium_layer_planner/gossip_adapter.py:325:7:comparison:reserve",
    "mycelium_qualification/replica_track_backend.py:420:12:assignment:(cancelled, deadline, pending_generation)",
    "mycelium_qualification/replica_track_backend.py:443:16:assignment:(cancelled, deadline, pending_generation)",
    "mycelium_swarm_artifacts.py:1110:11:comparison:serving_reserve",
    "scripts/run_a5_benchmark_gate.py:856:8:keyword:duration_after_measure",
];

fn world() -> (Vec<Bound>, BTreeMap<String, Candidate>) {
    let map = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    (checked_in().unwrap(), map)
}

fn row<'a>(rows: &'a [Bound], key: &str) -> &'a Bound {
    rows.iter()
        .find(|r| r.key == key)
        .unwrap_or_else(|| panic!("missing row {key}"))
}

#[test]
fn ratio_is_a_distinct_value_class_with_a_permille_unit() {
    assert_eq!(Permille::new(750).get(), 750);
    assert_ne!(
        Value::Ratio(Permille::new(500)),
        Value::Count(budgets::Entries::new(500))
    );
    assert_eq!(
        Permille::new(1000).checked_sub(Permille::new(250)).unwrap(),
        Permille::new(750)
    );
    assert!(Permille::new(1).checked_sub(Permille::new(2)).is_err());
    // A ratio is never a duration, a byte count or an entry count.
    for case in CASES {
        let id = BudgetId::from_name(case.2).unwrap();
        assert!(id.span(budgets::Entries::new(1)).is_err(), "{}", case.2);
    }
}

#[test]
fn eleven_ratio_literals_are_registered_and_reviewed_exactly() {
    let (rows, map) = world();
    assert_eq!(CASES.len(), 11);
    for (key, value, id) in CASES {
        let r = row(&rows, key);
        assert_eq!(r.unit, "permille", "{key}");
        assert_eq!(r.value, Some(*value), "{key}");
        assert_eq!(r.budget, *id, "{key}");
        assert!(r.disposition.is_empty(), "{key}");
        assert!(r.review.starts_with("ratio-category:literal; "), "{key}");
        assert!(ratio::validate(r, &map), "{key}");
        let entry = BudgetId::from_name(id).unwrap().entry();
        assert_eq!(entry.value, Value::Ratio(Permille::new(*value)), "{id}");
        let c = &map[*key];
        assert_eq!(
            entry.provenance,
            Provenance::Source(Box::leak(
                format!("{}:{} @ ef9ae61d", c.path, c.line).into_boxed_str()
            )),
            "{id}"
        );
    }
    assert!(validate(&rows).is_ok());
    // Preserve the eleven literal-family ids exactly; additive manual verdicts must
    // independently be validated, carry permille units and use a numeric registry id.
    let all = budget_model::inventory::all_reviewed().unwrap();
    assert_eq!(validate(&all), Ok(()));
    let ratio_ids: BTreeSet<&str> = BUDGET_TABLE
        .iter()
        .filter(|e| matches!(e.value, Value::Ratio(_)))
        .map(|e| e.id.name())
        .collect();
    let literal_ids: BTreeSet<&str> = CASES.iter().map(|c| c.2).collect();
    let adjudicated_ids: BTreeSet<&str> = all
        .iter()
        .filter(|r| r.review.starts_with("manual-adjudication:") && r.unit == "permille")
        .filter(|r| r.disposition.is_empty())
        .map(|r| r.budget.as_str())
        .collect();
    assert!(literal_ids.is_disjoint(&adjudicated_ids));
    assert_eq!(
        ratio_ids,
        literal_ids.union(&adjudicated_ids).copied().collect()
    );
    for id in literal_ids {
        assert_eq!(rows.iter().filter(|r| r.budget == id).count(), 1, "{id}");
    }
    for id in adjudicated_ids {
        assert!(
            all.iter()
                .any(|r| r.budget == id && r.disposition.is_empty())
        );
    }
}

#[test]
fn the_decimal_parser_is_exact_and_canonical() {
    for (text, want) in [
        ("0", 0),
        ("1", 1000),
        ("0.5", 500),
        ("0.75", 750),
        ("0.1", 100),
        ("0.001", 1),
        ("0.250", 250),
        ("1.0", 1000),
        ("1.000", 1000),
        ("0.999", 999),
    ] {
        assert_eq!(ratio::permille(text), Some(want), "{text}");
    }
    for text in [
        "", ".", ".5", "0.", "00.5", "01", "1.001", "1.5", "2", "10", "0.0005", "0.1234", "-0.1",
        "+0.1", "1e-1", "0,5", "0.5 ", " 0.5", "0x1", "0.5.0", "abc", "1_0", "0.5f", "٠", "0.+5",
        "0.5\n", "+1",
    ] {
        assert_eq!(ratio::permille(text), None, "{text:?}");
    }
}

#[test]
fn each_validator_check_is_refused_when_broken() {
    let (rows, map) = world();
    let (key, value, id) = CASES[0];
    let base = row(&rows, key).clone();
    assert!(ratio::validate(&base, &map));
    type Mutation<'a> = (&'a str, Box<dyn Fn(&mut Bound) + 'a>);
    let bad: Vec<Mutation<'_>> = vec![
        (
            "prefix",
            Box::new(|b| b.review = b.review.replacen("ratio-category:", "numeric-category:", 1)),
        ),
        (
            "rule",
            Box::new(|b| b.review = b.review.replacen("literal;", "range;", 1)),
        ),
        ("unit", Box::new(|b| b.unit = "entries".into())),
        ("value", Box::new(|b| b.value = Some(value + 1))),
        ("value-none", Box::new(|b| b.value = None)),
        (
            "budget-other-ratio",
            Box::new(|b| b.budget = CASES[1].2.into()),
        ),
        (
            "budget-missing",
            Box::new(|b| b.budget = "PyRatioNope".into()),
        ),
        ("budget-empty", Box::new(|b| b.budget = String::new())),
        (
            "disposition",
            Box::new(|b| b.disposition = "derived".into()),
        ),
        ("path", Box::new(|b| b.path = "other.py".into())),
        ("line", Box::new(|b| b.line += 1)),
        ("column", Box::new(|b| b.column += 1)),
        ("scope", Box::new(|b| b.scope = "other".into())),
        ("symbol", Box::new(|b| b.symbol = "other".into())),
        ("expression", Box::new(|b| b.expression = "0.50".into())),
        (
            "source-sha",
            Box::new(|b| b.source_sha = "f4e366e98a1f705dfaf182897044500d290dbd7f".into()),
        ),
        (
            "source-sha-short",
            Box::new(|b| b.source_sha = "ef9".into()),
        ),
    ];
    for (name, f) in &bad {
        let mut b = base.clone();
        f(&mut b);
        assert!(!ratio::validate(&b, &map), "{name}");
    }
    // The registered id must hold the ratio class and this exact value (a Count of the same number is refused).
    let _ = id;
    let census = |f: &dyn Fn(&mut Candidate)| {
        let mut m = map.clone();
        f(m.get_mut(key).unwrap());
        ratio::validate(&base, &m)
    };
    assert!(!census(&|c| c.kind = "comparison".into()));
    assert!(!census(&|c| c.kind = "parameter".into()));
    assert!(!census(&|c| c.symbol = "_dns_retry_budget".into()));
    assert!(!census(&|c| c.expression = "1.5".into()));
    assert!(!census(&|c| c.expression = "0.5 * 2".into()));
    assert!(!census(&|c| c.line += 1));
    let mut missing = map.clone();
    missing.remove(key);
    assert!(!ratio::validate(&base, &missing));
    // The three admitted census kinds each work with a pinned symbol.
    for kind in ["assignment", "keyword", "dict-field"] {
        let mut m = map.clone();
        m.get_mut(key).unwrap().kind = kind.into();
        assert!(ratio::validate(&base, &m), "{kind}");
    }
    // Symbol matching is case-insensitive but exact.
    let mut m = map.clone();
    m.get_mut(key).unwrap().symbol = "_DNS_RETRY_FRACTION_X".into();
    let mut b = base.clone();
    b.symbol = "_DNS_RETRY_FRACTION_X".into();
    assert!(!ratio::validate(&b, &m));
}

#[test]
fn the_other_seven_ratio_rows_stay_pending_and_a_forged_review_is_refused() {
    let (rows, map) = world();
    let reviewed: BTreeSet<&str> = rows.iter().map(|r| r.key.as_str()).collect();
    for key in STAY_PENDING {
        assert!(!reviewed.contains(key), "{key} must stay pending");
        let c = &map[*key];
        assert_eq!(ratio::literal(c), None, "{key}");
        let forged = Bound {
            key: (*key).into(),
            path: c.path.clone(),
            line: c.line,
            column: c.column,
            scope: c.scope.clone(),
            symbol: c.symbol.clone(),
            expression: c.expression.clone(),
            unit: "permille".into(),
            value: Some(1000),
            semantic: "forged".into(),
            authority: "forged".into(),
            budget: CASES[0].2.into(),
            disposition: String::new(),
            review: "ratio-category:literal; forged".into(),
            relation: "forged".into(),
            source_sha: "ef9ae61de0222c78d83c8133f8f709619394bf51".into(),
        };
        assert!(!ratio::validate(&forged, &map), "{key}");
    }
    // Same-looking literals outside the pinned symbols are not admitted either.
    for c in map.values() {
        if c.kind == "assignment" && c.symbol.to_ascii_lowercase().contains("fraction") {
            let admitted = ratio::literal(c).is_some();
            assert_eq!(
                admitted,
                CASES.iter().any(|case| case.0 == c.key),
                "{} ({})",
                c.key,
                c.expression
            );
        }
    }
}

#[test]
fn the_pending_count_is_pinned() {
    let rows = checked_in().unwrap();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 737 - 11 ratio literals - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}

#[test]
fn the_registry_entry_must_be_the_exact_ratio_with_exact_site_provenance() {
    use budgets::table::{BudgetEntry, Provenance, Purpose};
    let (rows, map) = world();
    let (key, value, id) = CASES[2];
    let base = row(&rows, key).clone();
    let c = &map[key];
    let site: &'static str =
        Box::leak(format!("{}:{} @ ef9ae61d", c.path, c.line).into_boxed_str());
    let real = BudgetId::from_name(id).unwrap();
    let entry = |v: Value, p: Provenance| BudgetEntry {
        id: real,
        value: v,
        purpose: Purpose::Policy,
        provenance: p,
    };
    let ok = |e: Option<BudgetEntry>| ratio::validate_with(&base, &map, &|_| e);
    let exact = entry(Value::Ratio(Permille::new(value)), Provenance::Source(site));
    assert!(ok(Some(exact)));
    assert!(!ok(None));
    // Wrong class with the same number, wrong value, wrong provenance kind, line and sha.
    assert!(!ok(Some(entry(
        Value::Count(budgets::Entries::new(value)),
        Provenance::Source(site)
    ))));
    assert!(!ok(Some(entry(
        Value::Ratio(Permille::new(value + 1)),
        Provenance::Source(site)
    ))));
    assert!(!ok(Some(entry(
        Value::Ratio(Permille::new(value)),
        Provenance::Measured(site)
    ))));
    let other_line: &'static str =
        Box::leak(format!("{}:{} @ ef9ae61d", c.path, c.line + 1).into_boxed_str());
    assert!(!ok(Some(entry(
        Value::Ratio(Permille::new(value)),
        Provenance::Source(other_line)
    ))));
    let other_sha: &'static str =
        Box::leak(format!("{}:{} @ f4e366e9", c.path, c.line).into_boxed_str());
    assert!(!ok(Some(entry(
        Value::Ratio(Permille::new(value)),
        Provenance::Source(other_sha)
    ))));
    let other_path: &'static str =
        Box::leak(format!("other.py:{} @ ef9ae61d", c.line).into_boxed_str());
    assert!(!ok(Some(entry(
        Value::Ratio(Permille::new(value)),
        Provenance::Source(other_path)
    ))));
    // The census lookup is by the row's own key: another candidate with the same content does not stand in.
    let mut twin = c.clone();
    twin.key = "twin".into();
    let only_twin = BTreeMap::from([("twin".to_string(), twin)]);
    assert!(!ratio::validate_with(&base, &only_twin, &|_| Some(exact)));
    assert!(ratio::validate_with(&base, &map, &|_| Some(exact)));
}
