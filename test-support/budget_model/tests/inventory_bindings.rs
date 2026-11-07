// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/managed_control.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};
use std::collections::{BTreeMap, BTreeSet};

#[test]
fn binding_categories_reference_real_rows_without_turning_defaults_into_values() {
    let candidates: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|row| (row.key.clone(), row))
        .collect();
    let rows = checked_in().unwrap();
    let mut rules = BTreeSet::new();
    let mut keys = BTreeSet::new();
    let input = include_str!("../data/python-binding-edges.tsv");
    assert_eq!(
        input.lines().next(),
        Some("key\ttarget_key\trule\treference")
    );
    let mut graph = BTreeMap::new();
    for line in input.lines().skip(1) {
        let fields: Vec<_> = line.split('\t').collect();
        assert_eq!(fields.len(), 4);
        assert!(keys.insert(fields[0]));
        rules.insert(fields[2]);
        graph.insert(fields[0], fields[1]);
        let source = &candidates[fields[0]];
        let target = &candidates[fields[1]];
        assert_eq!(source.expression, fields[3]);
        assert!(bindings::accepts(fields[2], source, target));
        let bound = rows.iter().find(|r| r.key == source.key).unwrap();
        assert!(
            bindings::validate(bound, &rows, &candidates),
            "{}",
            bound.key
        );
        assert!(bound.value.is_none());
        let mut changed = source.clone();
        changed.expression = format!("int({})", changed.expression);
        assert!(
            !bindings::accepts(fields[2], &changed, target),
            "casts are not aliases"
        );
        changed = source.clone();
        changed.path = "other.py".into();
        assert!(!bindings::accepts(fields[2], &changed, target));
    }
    assert_eq!(
        rules,
        BTreeSet::from([
            "same-scope-parameter",
            "same-scope-binding",
            "module-constant",
            "constructor-field",
            "parameter-float-normalization",
            "parameter-integral-normalization",
            "monotonic-offset-module-constant",
            "monotonic-offset-same-scope-parameter",
            "monotonic-offset-same-scope-binding",
            "monotonic-offset-constructor-field",
            "comparison-reference-module-constant",
            "comparison-reference-same-scope-parameter",
            "comparison-reference-constructor-field",
            "zero-validation-same-scope-parameter",
            "class-field",
            "parameter-bound-float-normalization",
            "imported-constant",
            "imported-float-constant",
            "remaining-monotonic-local",
            "typed-config-field",
            "argparse-option",
            "validated-parameter-default",
            "optional-parameter-default",
            "resolved-optional-value",
            "resolved-poll-default",
            "schedule-maximum",
            "upper-cap-reference",
            "imported-upper-cap-reference",
            "monotonic-upper-cap-reference",
            "lower-floor-reference",
            "comparison-imported-constant",
            "validated-required-ceiling",
            "terminal-composer-policy",
            "terminal-retention-window",
            "retained-kv-expiry",
            "terminal-capacity-enforcement",
            "terminal-positive-policy",
            "reviewed-config-clamp",
            "reviewed-local-comparison",
            "directory-offer-age",
            "reviewed-clock-offset",
            "reviewed-helper-field",
            "gossip-successful-validation",
            "reviewed-helper-return",
            "reviewed-composed-ceiling",
            "reviewed-retry-state"
        ])
    );
    for mut key in graph.keys().copied() {
        let mut visited = BTreeSet::new();
        while let Some(target) = graph.get(key) {
            assert!(visited.insert(key), "cyclic binding: {key}");
            key = target;
        }
    }
}

#[test]
fn explicit_cast_category_preserves_default_but_never_claims_a_constant_runtime_value() {
    let rows = candidates().unwrap();
    let source = rows
        .iter()
        .find(|r| {
            r.path == "mycelium_a13_macos/managed_control.py"
                && r.symbol == "self.timeout"
                && r.expression == "float(timeout)"
        })
        .unwrap();
    let target = rows
        .iter()
        .find(|r| {
            r.path == source.path
                && r.scope == source.scope
                && r.symbol == "timeout"
                && r.kind == "parameter"
        })
        .unwrap();
    assert!(bindings::accepts(
        "parameter-float-normalization",
        source,
        target
    ));
    assert!(!bindings::accepts("same-scope-parameter", source, target));
    let mut integer = source.clone();
    integer.expression = "int(timeout)".into();
    let mut fractional = target.clone();
    fractional.expression = "0.1".into();
    assert!(!bindings::accepts(
        "parameter-integral-normalization",
        &integer,
        &fractional
    ));
    fractional.expression = "10.0".into();
    assert!(bindings::accepts(
        "parameter-integral-normalization",
        &integer,
        &fractional
    ));
    fractional.expression = "9007199254740993".into();
    assert!(!bindings::accepts(
        "parameter-float-normalization",
        source,
        &fractional
    ));
}

#[test]
fn monotonic_offset_edges_preserve_the_clock_and_source_binding() {
    let rows = candidates().unwrap();
    for (rule, path, source_line, target_line) in [
        (
            "monotonic-offset-module-constant",
            "mycelium_a13_macos/managed_control.py",
            2205,
            316,
        ),
        (
            "monotonic-offset-same-scope-parameter",
            "mycelium_a13_macos/token_receipt_mailbox.py",
            80,
            77,
        ),
        (
            "monotonic-offset-constructor-field",
            "mycelium_a6_http_fault_control.py",
            144,
            119,
        ),
    ] {
        let source = rows
            .iter()
            .find(|r| r.path == path && r.line == source_line)
            .unwrap();
        let target = rows
            .iter()
            .find(|r| r.path == path && r.line == target_line)
            .unwrap();
        assert!(bindings::accepts(rule, source, target), "{rule}");
        let mut changed = source.clone();
        for clock in ["time.time()", "other.monotonic()", "time.monotonic() - 1"] {
            changed.expression = format!("{clock} + {}", target.symbol);
            assert!(!bindings::accepts(rule, &changed, target));
        }
        changed.expression = format!("time.monotonic() + {} + 1", target.symbol);
        assert!(!bindings::accepts(rule, &changed, target));
        let mut wrong = target.clone();
        wrong.symbol = "unrelated_seconds".into();
        assert!(!bindings::accepts(rule, source, &wrong));
    }
}

#[test]
fn comparison_edges_preserve_the_referenced_bound_and_zero_validation() {
    let rows = candidates().unwrap();
    for (rule, path, source_line, target_line, symbol) in [
        (
            "comparison-reference-module-constant",
            "mycelium_a13_macos/managed_control.py",
            364,
            95,
            "MAX_FRAME_BYTES",
        ),
        (
            "comparison-reference-same-scope-parameter",
            "mycelium_a13_macos/workflow.py",
            112,
            87,
            "max_archive_bytes",
        ),
        (
            "comparison-reference-constructor-field",
            "mycelium_node/process.py",
            3262,
            2001,
            "self.max_frame_bytes",
        ),
        (
            "zero-validation-same-scope-parameter",
            "mycelium_a13_macos/peer.py",
            126,
            117,
            "timeout",
        ),
    ] {
        let source = rows
            .iter()
            .find(|r| r.path == path && r.line == source_line && r.kind == "comparison")
            .unwrap();
        let target = rows
            .iter()
            .find(|r| r.path == path && r.line == target_line && r.symbol == symbol)
            .unwrap();
        assert!(bindings::accepts(rule, source, target), "{rule}");
        let mut changed = source.clone();
        changed.expression = source.expression.replace(symbol, "unrelated");
        assert!(!bindings::accepts(rule, &changed, target));
        changed = source.clone();
        changed.kind = "keyword".into();
        assert!(!bindings::accepts(rule, &changed, target));
        changed = source.clone();
        changed.expression = format!("{} == 0", target.symbol);
        assert!(!bindings::accepts(rule, &changed, target));
    }
}

#[test]
fn full_validator_rejects_a_valid_budget_with_the_wrong_binding() {
    let mut rows = checked_in().unwrap();
    let row = rows
        .iter_mut()
        .find(|r| r.review.starts_with("binding-category:") && r.unit == "milliseconds")
        .unwrap();
    // A real same-unit ID must not launder a bad provenance edge.
    assert_ne!(row.relation, "RelayWindow");
    row.relation = "RelayWindow".into();
    assert!(validate(&rows).is_err());
}
