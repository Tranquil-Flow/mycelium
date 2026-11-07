// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_iroh_sidecar/client.py; O:path:mycelium_live/router_port.py.
use budget_model::inventory::{Bound, Candidate, candidates, checked_in};
#[path = "common/partition_b.rs"]
mod proof;
use proof::validate_rows as validate;
use std::collections::{BTreeMap, BTreeSet};

fn proposed(c: &Candidate, target: &Bound, rule: &str) -> Bound {
    let mut row = target.clone();
    row.key.clone_from(&c.key);
    row.path.clone_from(&c.path);
    row.line = c.line;
    row.column = c.column;
    row.scope.clone_from(&c.scope);
    row.symbol.clone_from(&c.symbol);
    row.expression.clone_from(&c.expression);
    row.relation = if target.budget.is_empty() {
        target.relation.clone()
    } else {
        target.budget.clone()
    };
    row.budget.clear();
    row.disposition = "derived".into();
    row.value = None;
    row.review = format!("partition-b-family:{rule}; exact pinned witness");
    row.source_sha = budget_model::inventory::SOURCE_SHA.into();
    row
}

#[test]
fn b_families_require_the_exact_source_target_and_same_unit_provenance() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let baseline = checked_in().unwrap();
    let mut seen = BTreeSet::new();
    for line in include_str!("../data/python-partition-b-families.tsv")
        .lines()
        .skip(1)
    {
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f.len(), 23);
        assert!(seen.insert(f[0]));
        let c = &all[f[0]];
        let target = baseline
            .iter()
            .find(|b| b.key == f[9])
            .expect("independent target");
        let row = proposed(c, target, f[18]);
        let mut rows: Vec<_> = baseline
            .iter()
            .filter(|r| r.key != row.key)
            .cloned()
            .collect();
        rows.push(row.clone());
        assert_eq!(validate(&rows), Ok(()), "positive {}", row.key);
        for mutation in 0..8 {
            let mut bad = row.clone();
            match mutation {
                0 => {
                    bad.relation = if row.relation == "CancelAndCleanup" {
                        "RouterClose"
                    } else {
                        "CancelAndCleanup"
                    }
                    .into()
                }
                1 => bad.expression.push_str(" + 1"),
                2 => bad.scope.push('!'),
                3 => bad.symbol.push('!'),
                4 => bad.path.push('!'),
                5 => bad.line += 1,
                6 => bad.column += 1,
                7 => bad.review = "partition-b-family:unreviewed-rule; reject".into(),
                _ => unreachable!(),
            }
            rows.pop();
            rows.push(bad);
            assert!(validate(&rows).is_err(), "{} mutation {mutation}", row.key);
        }
    }
    assert!(!seen.is_empty());
}

#[test]
fn b_deadline_helpers_do_not_launder_required_arguments_or_mixed_writers() {
    let all = candidates().unwrap();
    let baseline = checked_in().unwrap();
    let target = baseline
        .iter()
        .find(|b| b.key == "mycelium_router/transports/iroh.py:2964:12:assignment:deadline")
        .unwrap();
    for (path, line) in [
        ("mycelium_router/transports/iroh.py", 1909),
        ("mycelium_router/transports/iroh.py", 2976),
        ("mycelium_router/transports/iroh.py", 3031),
        ("mycelium_router/transports/iroh.py", 3084),
        ("mycelium_iroh_sidecar/client.py", 988),
        ("mycelium_iroh_sidecar/client.py", 1033),
        ("mycelium_live/route.py", 5217),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        let row = proposed(c, target, "remaining-from-deadline");
        let mut rows = baseline.clone();
        rows.push(row);
        assert!(
            validate(&rows).is_err(),
            "unwitnessed required/mutable site {}",
            c.key
        );
    }
}
