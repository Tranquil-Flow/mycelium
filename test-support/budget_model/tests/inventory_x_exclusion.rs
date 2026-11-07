// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_linux/onboarding.py.
//! L1b round 7, RU-013: an absolute instant is `not-a-bound` only with a pinned same-file producer row (a census
//! row doing clock-plus-operand arithmetic, symbol-related to the instant). 83 rows (41 deadline carriers, 38
//! absolute carriers, 4 expiry compares) are reviewed under the rule; the 41 structural X rows are not touched.
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
    Bound, Candidate, candidate_keys, candidates, checked_in, observation_flows, validate,
    x_exclusion,
};
use std::collections::{BTreeMap, BTreeSet};

fn world() -> (Vec<Bound>, BTreeMap<String, Candidate>) {
    let map = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    (checked_in().unwrap(), map)
}

fn forecast_rows() -> Vec<Vec<&'static str>> {
    x_exclusion::FORECAST
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect()
}

fn exclusion_rows(rows: &[Bound]) -> Vec<&Bound> {
    rows.iter()
        .filter(|r| r.review.starts_with(x_exclusion::PREFIX))
        .collect()
}

#[test]
fn eighty_three_instants_are_excluded_each_with_its_pinned_producer() {
    let (rows, map) = world();
    let reviewed = exclusion_rows(&rows);
    assert_eq!(reviewed.len(), 83);
    let by_rule: BTreeMap<_, _> = x_exclusion::RULES
        .iter()
        .map(|rule| {
            let n = reviewed
                .iter()
                .filter(|r| {
                    r.review
                        .starts_with(&format!("{}{rule};", x_exclusion::PREFIX))
                })
                .count();
            (*rule, n)
        })
        .collect();
    assert_eq!(
        by_rule,
        BTreeMap::from([
            ("absolute-instant-carrier", 38),
            ("absolute-instant-compare", 4),
            ("deadline-instant-carrier", 41)
        ])
    );
    let forecast = forecast_rows();
    for r in &reviewed {
        let line = forecast
            .iter()
            .find(|f| f[0] == r.key)
            .unwrap_or_else(|| panic!("{} not in the forecast", r.key));
        assert_eq!(line[2], "X", "{}", r.key);
        let producer = line[7];
        assert!(!producer.is_empty(), "{}", r.key);
        // Independent re-derivation of the producer invariants (not the validator's code path).
        let (c, p) = (&map[&r.key], &map[producer]);
        assert_eq!(p.path, c.path, "{}: producer in the same file", r.key);
        assert_ne!(p.key, c.key);
        assert!(
            ["monotonic(", "time.time(", "_clock(", " + "]
                .iter()
                .any(|t| p.expression.contains(t)),
            "{}: producer {} does clock + operand arithmetic ({})",
            r.key,
            p.key,
            p.expression
        );
        assert_eq!(r.disposition, "not-a-bound");
        assert!(
            r.budget.is_empty() && r.value.is_none() && r.unit == "not-applicable",
            "{}",
            r.key
        );
        assert_eq!(
            r.relation,
            format!("absolute-instant-not-a-magnitude; producer={producer}")
        );
        assert!(x_exclusion::validate(r, &map), "{}", r.key);
    }
    assert!(validate(&rows).is_ok());
}

#[test]
fn only_forecast_x_rows_with_a_producer_are_reviewed_and_structural_rows_stay_pending() {
    let (rows, _) = world();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.as_str()).collect();
    for f in forecast_rows() {
        let instant = matches!(
            f[3],
            "x-absolute-instant-carrier"
                | "x-deadline-instant-carrier"
                | "x-absolute-instant-compare"
        );
        if f[2] == "X" && !instant {
            // Round 9 (RU-023) narrowing: the only structural X rows that may be reviewed carry the
            // `observation-flow:` review (inventory_observation_flows pins which two).
            let observation = rows
                .iter()
                .any(|r| r.key == f[0] && r.review.starts_with(observation_flows::PREFIX));
            assert!(
                !reviewed.contains(f[0]) || observation,
                "{} (structural X) must stay pending",
                f[0]
            );
        }
        if f[2] == "R2" && f[3] == "r2-instant-carrier-edge" {
            // The 247 producer-less instants are NOT excluded: they need the carrier edge.
            assert!(f[7].is_empty(), "{}", f[0]);
            assert!(
                !rows
                    .iter()
                    .any(|r| r.key == f[0] && r.review.starts_with(x_exclusion::PREFIX)),
                "{} is R2, not X",
                f[0]
            );
        }
    }
}

fn pick<'a>(rows: &'a [Bound], rule: &str) -> &'a Bound {
    rows.iter()
        .find(|r| {
            r.review
                .starts_with(&format!("{}{rule};", x_exclusion::PREFIX))
        })
        .unwrap()
}

/// The row with its producer key replaced consistently in the review text and the relation.
fn retarget(r: &Bound, old: &str, new: &str) -> Bound {
    Bound {
        review: r.review.replace(old, new),
        relation: r.relation.replace(old, new),
        ..r.clone()
    }
}

fn producer_of(key: &str) -> &'static str {
    forecast_rows().into_iter().find(|f| f[0] == key).unwrap()[7]
}

#[test]
fn producer_class_family_and_carrier_mutations_are_refused() {
    let (rows, map) = world();
    for rule in x_exclusion::RULES {
        let r = pick(&rows, rule);
        let producer = producer_of(&r.key);
        assert!(
            x_exclusion::validate_with(r, &map, x_exclusion::FORECAST),
            "{rule} baseline"
        );
        // Producer in another file.
        let other = map
            .values()
            .find(|c| c.path != r.path && c.expression.contains(" + "))
            .unwrap();
        let text = x_exclusion::FORECAST.replacen(
            &format!("\t{producer}\t"),
            &format!("\t{}\t", other.key),
            1,
        );
        assert!(
            !x_exclusion::validate_with(r, &map, &text),
            "{rule}: producer in another file"
        );
        // The same-file requirement alone: a symbol-related, clock-arithmetic producer that lives in another file.
        let (far_key, far_path) = {
            let far = map
                .values()
                .find(|c| c.path != r.path && c.kind == "assignment")
                .unwrap();
            (far.key.clone(), far.path.clone())
        };
        let mut m = map.clone();
        let carrier_symbol = map[&r.key].symbol.clone();
        let far = m.get_mut(&far_key).unwrap();
        far.symbol = carrier_symbol;
        far.expression = "clock + allowance".into();
        assert_ne!(far_path, r.path);
        let text = x_exclusion::FORECAST.replacen(
            &format!("\t{producer}\t"),
            &format!("\t{far_key}\t"),
            1,
        );
        let retargeted = retarget(r, producer, &far_key);
        assert!(
            !x_exclusion::validate_with(&retargeted, &m, &text),
            "{rule}: producer file"
        );
        // Control: the same retargeted row with the producer moved into the carrier's own file is valid.
        let mut near = m.clone();
        near.get_mut(&far_key).unwrap().path = r.path.clone();
        assert!(
            x_exclusion::validate_with(&retargeted, &near, &text),
            "{rule}: control (same file)"
        );
        // A duplicated forecast line is ambiguous and refused.
        let dup_line = x_exclusion::FORECAST
            .lines()
            .find(|l| l.starts_with(&r.key))
            .unwrap();
        let text = format!("{}\n{dup_line}\n", x_exclusion::FORECAST.trim_end());
        assert!(
            !x_exclusion::validate_with(r, &map, &text),
            "{rule}: duplicate forecast line"
        );
        // Producer is the carrier itself.
        let text = x_exclusion::FORECAST.replacen(
            &format!("\t{producer}\t"),
            &format!("\t{}\t", r.key),
            1,
        );
        assert!(
            !x_exclusion::validate_with(r, &map, &text),
            "{rule}: producer == carrier"
        );
        if rule == "absolute-instant-compare" {
            // The compare family may do clock arithmetic itself; "producer == carrier" must still be refused.
            let mut m = map.clone();
            let mut same = r.clone();
            m.get_mut(&r.key).unwrap().expression = format!("{} + 0", r.expression);
            same.expression = format!("{} + 0", r.expression);
            let text = x_exclusion::FORECAST.replacen(
                &format!("\t{producer}\t"),
                &format!("\t{}\t", r.key),
                1,
            );
            let same = retarget(&same, producer, &r.key);
            assert!(
                !x_exclusion::validate_with(&same, &m, &text),
                "{rule}: producer is the carrier (consistent)"
            );
        }
        // Producer that is not a census row, or has no producer at all.
        let text = x_exclusion::FORECAST.replacen(
            &format!("\t{producer}\t"),
            "\tnowhere.py:1:1:assignment:x\t",
            1,
        );
        assert!(
            !x_exclusion::validate_with(r, &map, &text),
            "{rule}: producer not in the census"
        );
        let text = x_exclusion::FORECAST.replacen(&format!("\t{producer}\t"), "\t\t", 1);
        assert!(
            !x_exclusion::validate_with(r, &map, &text),
            "{rule}: no producer"
        );
        // Producer without clock arithmetic, or unrelated symbol.
        let mut m = map.clone();
        m.get_mut(producer).unwrap().expression = "42".into();
        assert!(
            !x_exclusion::validate_with(r, &m, x_exclusion::FORECAST),
            "{rule}: producer expression"
        );
        let mut m = map.clone();
        m.get_mut(producer).unwrap().symbol = "zzz_unrelated".into();
        assert!(
            !x_exclusion::validate_with(r, &m, x_exclusion::FORECAST),
            "{rule}: producer symbol"
        );
        // Class and family come from the pinned forecast: relabelled class or family is refused.
        let line = x_exclusion::FORECAST
            .lines()
            .find(|l| l.starts_with(&r.key))
            .unwrap();
        let text = x_exclusion::FORECAST.replacen(line, &line.replacen("\tX\t", "\tR2\t", 1), 1);
        assert!(!x_exclusion::validate_with(r, &map, &text), "{rule}: class");
        let other_rule = x_exclusion::RULES.iter().find(|o| **o != rule).unwrap();
        let mut relabelled = r.clone();
        relabelled.review = relabelled.review.replacen(rule, other_rule, 1);
        assert!(
            !x_exclusion::validate_with(&relabelled, &map, x_exclusion::FORECAST),
            "{rule}: family"
        );
        // Missing from the forecast.
        let text = x_exclusion::FORECAST.replacen(line, "", 1);
        assert!(
            !x_exclusion::validate_with(r, &map, &text),
            "{rule}: not in the forecast"
        );
        // Row-field mutations.
        let mutants: Vec<(&str, Bound)> = vec![
            (
                "value",
                Bound {
                    value: Some(1),
                    ..r.clone()
                },
            ),
            (
                "unit",
                Bound {
                    unit: "milliseconds".into(),
                    ..r.clone()
                },
            ),
            (
                "budget",
                Bound {
                    budget: "PyPrefillMaximumResends".into(),
                    ..r.clone()
                },
            ),
            (
                "disposition",
                Bound {
                    disposition: "derived".into(),
                    ..r.clone()
                },
            ),
            (
                "relation",
                Bound {
                    relation: format!("{}x", r.relation),
                    ..r.clone()
                },
            ),
            (
                "producer text",
                Bound {
                    review: r.review.replace(producer, "elsewhere"),
                    ..r.clone()
                },
            ),
            (
                "expression",
                Bound {
                    expression: format!("{}0", r.expression),
                    ..r.clone()
                },
            ),
        ];
        for (what, m) in mutants {
            assert!(
                !x_exclusion::validate_with(&m, &map, x_exclusion::FORECAST),
                "{rule}: {what}"
            );
            let mut all = rows.clone();
            let i = all.iter().position(|x| x.key == r.key).unwrap();
            all[i] = m;
            assert!(validate(&all).is_err(), "{rule}: {what} (whole validator)");
        }
        // A carrier that does clock arithmetic itself is not an X carrier (the compare family compares to now).
        if rule != "absolute-instant-compare" {
            let mut m = map.clone();
            let mut bad = r.clone();
            m.get_mut(&r.key).unwrap().expression = format!("{} + 1", r.expression);
            bad.expression = format!("{} + 1", r.expression);
            assert!(
                !x_exclusion::validate_with(&bad, &m, x_exclusion::FORECAST),
                "{rule}: carrier arithmetic"
            );
        }
    }
}

#[test]
fn a_forged_exclusion_on_a_non_x_or_structural_row_is_refused() {
    let (rows, map) = world();
    let template = pick(&rows, "absolute-instant-carrier");
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.as_str()).collect();
    for f in forecast_rows() {
        let wanted = matches!(
            (f[2], f[3]),
            ("R2", "r2-instant-carrier-edge")
                | ("X", "x-elapsed-or-measured-observation")
                | ("X", "x-sentinel-deadline")
        );
        if !wanted || reviewed.contains(f[0]) {
            continue;
        }
        let c = &map[f[0]];
        let mut forged = template.clone();
        forged.key = f[0].into();
        forged.path = c.path.clone();
        forged.line = c.line;
        forged.column = c.column;
        forged.scope = c.scope.clone();
        forged.symbol = c.symbol.clone();
        forged.expression = c.expression.clone();
        // Even with a producer written into the forecast line, the family/class do not match the rule.
        assert!(
            !x_exclusion::validate_with(&forged, &map, x_exclusion::FORECAST),
            "{} ({}) must be refused",
            f[0],
            f[3]
        );
    }
}

#[test]
fn total_pending_after_the_exclusion_batch() {
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|r| r.key).collect();
    assert_eq!(
        candidate_keys().unwrap().difference(&reviewed).count(),
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len() // R29 exact reviewed discovery outside bounds-only; R17 +12 tuple-floor operands; manual reviews separate.
    ); // +8 R16 nonkeyword operands; +570 RU-051 keyword operands. // 833 - 83 - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}
