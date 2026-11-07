// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/command_controller.py; O:path:mycelium_live/liveness.py; O:path:mycelium_seed/invite_batch.py.
//! L1b round 8, rule `r2-symbolic-upper-constant` (same-file module constants): a chained comparison whose upper
//! operand is a module constant with a reviewed row derives from that constant's budget through the existing
//! `comparison-reference-module-constant` binding category (edge in `python-binding-edges.tsv`). The seven rows
//! below are the pending comparisons whose upper operand is such a constant; the constants' own source text and
//! values are listed independently of the registry.
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
    Bound, Candidate, bindings, candidate_keys, candidates, checked_in, validate,
};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::{BTreeMap, BTreeSet};

/// (comparison key, constant row key, registry id, unit, value in that unit, constant source line)
type Case = (
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    u64,
    &'static str,
);
const CASES: &[Case] = &[
    (
        "mycelium_a13_macos/pairing.py:666:23:comparison:_WINDOW_TTL_MIN",
        "mycelium_a13_macos/pairing.py:453:0:assignment:_WINDOW_TTL_MAX",
        "PyFinalMyceliumA13MacosPairingWindowTtlMaxL453C0",
        "milliseconds",
        900 * 1000,
        "_WINDOW_TTL_MAX = 900",
    ),
    (
        "mycelium_a13_macos/serving.py:77:12:comparison:SLICE_FETCH_MIN_SOURCE_BYTES",
        "mycelium_a13_macos/serving.py:57:0:assignment:SLICE_FETCH_MAX_SOURCE_BYTES",
        "PyMyceliumA13MacosServingSliceFetchMaxSourceBytesL57",
        "bytes",
        32_000_000_000,
        "SLICE_FETCH_MAX_SOURCE_BYTES = 32_000_000_000",
    ),
    (
        "mycelium_live/command_controller.py:95:37:comparison:minimum",
        "mycelium_live/command_controller.py:34:0:assignment:_MAX_MONOTONIC_MS",
        "PyExactMyceliumLiveCommandControllerL34C0",
        "milliseconds",
        i64::MAX as u64,
        "_MAX_MONOTONIC_MS = (1 << 63) - 1",
    ),
    (
        "mycelium_live/liveness.py:81:37:comparison:minimum",
        "mycelium_live/liveness.py:27:0:assignment:_MAX_MONOTONIC_MS",
        "PyExactMyceliumLiveLivenessL27C0",
        "milliseconds",
        i64::MAX as u64,
        "_MAX_MONOTONIC_MS = (1 << 63) - 1",
    ),
    (
        "mycelium_seed/invite_batch.py:122:15:comparison:MIN_INVITE_TTL_SECONDS",
        "mycelium_seed/invite_batch.py:28:0:assignment:MAX_INVITE_TTL_SECONDS",
        "PyMyceliumSeedInviteBatchMaxInviteTtlSecondsL28",
        "milliseconds",
        86_400 * 1000,
        "MAX_INVITE_TTL_SECONDS = 86_400",
    ),
    (
        "scripts/build_qwen_live_route.py:1196:40:comparison:-_MANAGED_OBSERVATIONS_MAX_SKEW_SECONDS",
        "scripts/build_qwen_live_route.py:137:0:assignment:_MANAGED_OBSERVATIONS_MAX_AGE_SECONDS",
        "PyScriptsBuildQwenLiveRouteManagedObservationsMaxAgeSecondsL137",
        "milliseconds",
        10 * 60 * 1000,
        "_MANAGED_OBSERVATIONS_MAX_AGE_SECONDS = 10 * 60",
    ),
    (
        "scripts/build_qwen_live_route.py:1203:11:comparison:-_MANAGED_OBSERVATIONS_MAX_SKEW_SECONDS",
        "scripts/build_qwen_live_route.py:137:0:assignment:_MANAGED_OBSERVATIONS_MAX_AGE_SECONDS",
        "PyScriptsBuildQwenLiveRouteManagedObservationsMaxAgeSecondsL137",
        "milliseconds",
        10 * 60 * 1000,
        "_MANAGED_OBSERVATIONS_MAX_AGE_SECONDS = 10 * 60",
    ),
];

/// Same-looking rows that must stay pending (reason).
const STAY_PENDING: &[&str] = &[
    // upper operand is an imported name (needs the import-binding comparison rule)
    "mycelium_a13_macos/app.py:811:47:comparison:_WINDOW_TTL_MIN",
    // upper operand is an expression, not a bare constant
    "mycelium_a13_macos/capability.py:166:38:comparison:1024",
    // upper operand constants are timedelta values (no numeric row)
    "mycelium_a15_release_decision.py:1683:15:comparison:expires_at - issued_at",
    "mycelium_a15_release_decision.py:832:33:comparison:trust.expires_at - issued_at",
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
fn seven_symbolic_upper_comparisons_derive_from_their_module_constant() {
    let (rows, map) = world();
    let edges = include_str!("../data/python-binding-edges.tsv");
    for (key, target, id, unit, value, text) in CASES {
        let r = row(&rows, key);
        assert_eq!(r.disposition, "derived", "{key}");
        assert!(r.budget.is_empty() && r.value.is_none(), "{key}");
        assert_eq!(r.unit, *unit, "{key}");
        assert_eq!(r.relation, *id, "{key}");
        assert!(
            r.review
                .starts_with("binding-category:comparison-reference-module-constant;"),
            "{key}"
        );
        let edge = edges
            .lines()
            .find(|l| l.starts_with(&format!("{key}\t")))
            .unwrap_or_else(|| panic!("no edge for {key}"));
        assert_eq!(
            edge,
            format!(
                "{key}\t{target}\tcomparison-reference-module-constant\t{}",
                map[*key].expression
            )
        );
        assert!(bindings::validate(r, &rows, &map), "{key}");
        // The constant's census row carries the source text, and its budget is the id (an exact value).
        let t = row(&rows, target);
        assert_eq!(t.budget, *id, "{target}");
        assert!(
            map[*target]
                .source
                .replace(' ', "")
                .contains(&text.replace(' ', "")),
            "{target}"
        );
        assert!(
            map[*key]
                .expression
                .ends_with(&format!(" <= {}", map[*target].symbol)),
            "{key}"
        );
        let entry = BudgetId::from_name(id).unwrap().entry();
        match (entry.value, *unit) {
            (Value::Duration(span), "milliseconds") => assert_eq!(span.millis(), *value, "{id}"),
            (Value::Storage(b), "bytes") => assert_eq!(b.get(), *value, "{id}"),
            other => panic!("{id}: {other:?}"),
        }
        assert!(matches!(entry.provenance, Provenance::Source(_)), "{id}");
    }
    assert!(validate(&rows).is_ok());
}

#[test]
fn a_consistent_mutation_of_edge_target_expression_relation_or_unit_is_refused() {
    let (rows, map) = world();
    let (key, target, id, ..) = CASES[2]; // command_controller _MAX_MONOTONIC_MS
    let r = row(&rows, key).clone();
    assert!(bindings::validate(&r, &rows, &map));
    let check = |what: &str, r: &Bound, rows: &[Bound], m: &BTreeMap<String, Candidate>| {
        assert!(!bindings::validate(r, rows, m), "{what}");
    };
    // The sibling file's constant has the same name and a different id: not interchangeable.
    let mut m = r.clone();
    m.relation = CASES[3].2.to_string();
    check("other file's id", &m, &rows, &map);
    // The comparison no longer ends in the constant's symbol (census and edge changed together).
    let mut cands = map.clone();
    cands.get_mut(key).unwrap().expression = "minimum <= value <= _MAX_MONOTONIC_MS + 1".into();
    let mut m = r.clone();
    m.expression = "minimum <= value <= _MAX_MONOTONIC_MS + 1".into();
    check(
        "expression no longer ends in the constant",
        &m,
        &rows,
        &cands,
    );
    // The lower-only form does not mention the constant at all.
    let mut cands = map.clone();
    cands.get_mut(key).unwrap().expression = "minimum <= value".into();
    let mut m = r.clone();
    m.expression = "minimum <= value".into();
    check("no upper operand", &m, &rows, &cands);
    // The binding rule itself (the edge table is static, so mutate the source row directly): the comparison's
    // last operand must be exactly the constant's symbol.
    let rule = "comparison-reference-module-constant";
    assert!(bindings::accepts(rule, &map[key], &map[target]));
    for expression in [
        "minimum <= value <= _MAX_MONOTONIC_MS + 1",
        "minimum <= value <= OTHER_CONSTANT",
        "minimum <= value",
        "<= _MAX_MONOTONIC_MS",
        "_MAX_MONOTONIC_MS",
        "minimum == value <= _MAX_MONOTONIC_MS",
    ] {
        let mut source = map[key].clone();
        source.expression = expression.into();
        assert!(
            !bindings::accepts(rule, &source, &map[target]),
            "{expression}"
        );
    }
    // Unit mismatch, a budget instead of derived, and a value on a derived row.
    let mut m = r.clone();
    m.unit = "bytes".into();
    check("unit", &m, &rows, &map);
    let mut m = r.clone();
    m.value = Some(1);
    check("value", &m, &rows, &map);
    let mut m = r.clone();
    m.disposition = String::new();
    m.budget = id.to_string();
    check("budget instead of derived", &m, &rows, &map);
    // The constant row must be reviewed: with it removed the comparison cannot derive.
    let reduced: Vec<Bound> = rows.iter().filter(|b| b.key != target).cloned().collect();
    check("pending constant", &r, &reduced, &map);
    // A constant of another kind or scope is not a module constant.
    let mut cands = map.clone();
    cands.get_mut(target).unwrap().scope = "f".into();
    check("not a module-level constant", &r, &rows, &cands);
    let mut cands = map.clone();
    cands.get_mut(target).unwrap().kind = "keyword".into();
    check("not an assignment", &r, &rows, &cands);
    // Edge from another file's comparison to this constant is refused by the path rule.
    let mut cands = map.clone();
    cands.get_mut(key).unwrap().path = "mycelium_live/other.py".into();
    check("other file", &r, &rows, &cands);
}

#[test]
fn neighbours_stay_pending_and_the_pending_count_is_pinned() {
    let (rows, map) = world();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.as_str()).collect();
    for key in STAY_PENDING {
        assert!(map.contains_key(*key), "{key} census");
        assert!(!reviewed.contains(key), "{key} must stay pending");
    }
    let keys: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    let pending = candidate_keys().unwrap().difference(&keys).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 747 - 7 symbolic-upper-constant comparisons - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}
