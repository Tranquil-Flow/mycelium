// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/router_port.py; O:path:mycelium_router/transports/iroh.py.
use budget_model::inventory::{bindings, candidates, categories, checked_in};
use std::collections::{BTreeMap, BTreeSet};

#[test]
fn partition_b_exact_records_callbacks_and_readouts_are_not_scalar_waivers() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let reviewed = checked_in().unwrap();
    let mut seen = BTreeSet::new();
    for line in include_str!("data/partition-b-categories.tsv")
        .lines()
        .skip(1)
    {
        let (key, category) = line.split_once('\t').unwrap();
        assert!(seen.insert(key), "duplicate review fixture: {key}");
        let c = &all[key];
        assert!(categories::accepts(category, c), "{key}: {category}");
        let b = reviewed
            .iter()
            .find(|b| b.key == key)
            .expect("reviewed row");
        assert_eq!(b.disposition, "not-a-bound");
        assert!(b.value.is_none());
        assert!(b.budget.is_empty());
        assert!(b.review.starts_with(&format!("category:{category};")));
        for field in 0..9 {
            let mut bad = c.clone();
            match field {
                0 => bad.key.push('!'),
                1 => bad.path.push('!'),
                2 => bad.line += 1,
                3 => bad.column += 1,
                4 => bad.scope.push('!'),
                5 => bad.symbol.push('!'),
                6 => bad.expression.push('!'),
                7 => bad.kind.push('!'),
                8 => bad.source.push('!'),
                _ => unreachable!(),
            }
            assert!(!categories::accepts(category, &bad), "{key}: field {field}");
        }
    }
    assert!(!seen.is_empty());
}

#[test]
fn partition_b_edges_preserve_source_units_and_reject_same_unit_wrong_ids() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in include_str!("data/partition-b-bindings.tsv")
        .lines()
        .skip(1)
    {
        let f: Vec<_> = line.split('\t').collect();
        let source = &all[f[0]];
        let target = &all[f[1]];
        let b = rows
            .iter()
            .find(|b| b.key == source.key)
            .expect("reviewed bound edge");
        assert!(bindings::accepts(f[2], source, target), "{}", source.key);
        assert!(bindings::validate(b, &rows, &all), "{}", source.key);
        assert_eq!(b.unit, "milliseconds");
        assert_eq!(b.disposition, "derived");
        assert!(b.value.is_none());
        let mut bad = b.clone();
        bad.relation = if b.relation == "CancelAndCleanup" {
            "PyMyceliumLivePhoneStageSessionMaxSecondsL29".into()
        } else {
            "CancelAndCleanup".into()
        };
        assert_ne!(bad.relation, b.relation);
        assert!(matches!(
            budgets::BudgetId::from_name(&bad.relation)
                .unwrap()
                .entry()
                .value,
            budgets::table::Value::Duration(_)
        ));
        assert!(
            !bindings::validate(&bad, &rows, &all),
            "wrong-ID mutation survived: {}",
            source.key
        );
        bad = b.clone();
        bad.expression.push_str(" + 1");
        assert!(!bindings::validate(&bad, &rows, &all));
    }
}

#[test]
fn partition_b_default_branch_does_not_cover_owner_deadline_consumers() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let base = rows
        .iter()
        .find(|r| r.key == "mycelium_router/transports/iroh.py:2964:12:assignment:deadline")
        .expect("reviewed default branch");
    for line in [2975, 2976, 3084] {
        let c = all
            .values()
            .find(|c| c.path == base.path && c.line == line)
            .unwrap();
        let mut forged = base.clone();
        forged.key.clone_from(&c.key);
        forged.line = c.line;
        forged.column = c.column;
        forged.scope.clone_from(&c.scope);
        forged.symbol.clone_from(&c.symbol);
        forged.expression.clone_from(&c.expression);
        assert!(
            !bindings::validate(&forged, &rows, &all),
            "mixed reaching writer at {line}"
        );
    }
}

#[test]
fn partition_b_dynamic_policy_and_nested_enforcement_are_not_records() {
    let all = candidates().unwrap();
    for (path, line, symbol) in [
        ("mycelium_gossip/registry.py", 234, "expires_at_monotonic"),
        ("mycelium_gossip/registry.py", 258, "now"),
        ("mycelium_live/router_port.py", 335, "remaining_ms"),
        (
            "mycelium_live/router_port.py",
            1925,
            "retryable_successor_failure",
        ),
        ("mycelium_live/router_port.py", 3334, "len(retained)"),
        ("mycelium_live/supervisor.py", 1061, "len(settled)"),
        // The line-292 consuming comparison is an additional discovery request;
        // the existing independent declaration is already indexed.
        (
            "mycelium_router/a10_live_publication.py",
            44,
            "MAXIMUM_INTERVALS",
        ),
        ("mycelium_router/transports/iroh.py", 1917, "timeout"),
        (
            "mycelium_router/transports/iroh.py",
            2976,
            "acknowledgement_timeout",
        ),
        ("mycelium_router/transports/iroh.py", 2964, "deadline"),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        for category in [
            "reviewed-nonscalar-slot",
            "reviewed-observation-slot",
            "reviewed-policy-readout",
            "reviewed-structural-slot",
        ] {
            assert!(!categories::accepts(category, c), "{}: {category}", c.key);
        }
    }
}
