// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/transports/iroh.py; O:path:mycelium_speculative_decode.py.
use budget_model::inventory::{Candidate, candidates, categories, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

#[test]
fn b_count_consumption_tail_retention_and_dispatch_wait_have_distinct_units() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, line, unit, expected, wrong) in [
        (
            "mycelium_router/transports/iroh.py",
            1912,
            "entries",
            "PyPrefillMaximumResends",
            "RenewalDivisor",
        ),
        (
            "mycelium_speculative_decode.py",
            394,
            "entries",
            "PyFinalMyceliumSpeculativeDecodeCircuitBreakerRollingWindowCyclesL81C0",
            "RenewalDivisor",
        ),
        (
            "mycelium_router/transports/iroh.py",
            3287,
            "milliseconds",
            "PyMyceliumRouterTransportsIrohIrohtransportInitPollIntervalSecondsL855",
            "CancelAndCleanup",
        ),
    ] {
        let r = rows
            .iter()
            .find(|r| r.path == path && r.line == line)
            .expect("exact reviewed count/default consumer");
        assert_eq!(r.unit, unit);
        assert_eq!(r.relation, expected);
        assert!(proof::validate(r, &rows, &all));
        assert!(budgets::BudgetId::from_name(wrong).is_some());
        let mut bad = r.clone();
        bad.relation = wrong.into();
        assert!(!proof::validate(&bad, &rows, &all));
        bad = r.clone();
        bad.expression.push_str(" + 1");
        assert!(!proof::validate(&bad, &rows, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_ordering_and_tuple_slots_cannot_exempt_waits_or_nested_scalar_guards() {
    let all = candidates().unwrap();
    for (path, line, category) in [
        (
            "mycelium_router/transports/iroh.py",
            1926,
            "reviewed-structural-slot",
        ),
        (
            "mycelium_router/mlx_runtime.py",
            2756,
            "reviewed-nonscalar-slot",
        ),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(categories::accepts(category, c));
        let mut bad = c.clone();
        bad.source.push('!');
        assert!(!categories::accepts(category, &bad));
    }
    for (path, line) in [
        ("mycelium_router/transports/iroh.py", 1917),
        ("mycelium_router/mlx_runtime.py", 3358),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(!categories::accepts("reviewed-structural-slot", c));
        assert!(!categories::accepts("reviewed-nonscalar-slot", c));
    }
    // This is a pinned source discovery, explicitly NOT an accepted census row.
    for line in include_str!("data/partition-b-uncatalogued-guards.tsv")
        .lines()
        .skip(1)
    {
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f.len(), 11);
        assert_eq!(f[10], budget_model::inventory::CENSUS_SOURCE_SHA);
        assert!(include_str!("../data/python-scope.tsv").lines().any(|l| {
            let s: Vec<_> = l.split('\t').collect();
            s.first() == Some(&f[1]) && s.get(1) == Some(&f[9])
        }));
        let c = Candidate {
            key: f[0].into(),
            path: f[1].into(),
            line: f[2].parse().unwrap(),
            column: f[3].parse().unwrap(),
            scope: f[4].into(),
            symbol: f[5].into(),
            expression: f[6].into(),
            kind: f[7].into(),
            source: f[8].into(),
        };
        // Coordinator adjudication: the discovery is now an exact canonical candidate,
        // still pending (no reviewed row), and no waiver category accepts it.
        let canonical = all
            .iter()
            .find(|old| old.key == c.key)
            .expect("adjudicated discovery must be a canonical candidate");
        assert_eq!(*canonical, c);
        let reviewed = checked_in().unwrap().into_iter().find(|r| r.key == c.key);
        if c.path == "mycelium_live/artifact_transport.py" && c.line == 44 {
            // Source-reviewed by the coordinator after this fixture was written
            // (inventory_canonical_stream_block pins the exact row); still no waiver.
            let row = reviewed.expect("artifact_transport.py:44 is now source-reviewed");
            assert_eq!(
                (row.unit.as_str(), row.value, row.budget.as_str()),
                (
                    "bytes",
                    Some(65_536),
                    "PyMyceliumLiveArtifactTransportStreamBlockBytesL44"
                )
            );
        } else {
            assert!(
                reviewed.is_none(),
                "adjudicated discovery stays pending until source-reviewed"
            );
        }
        assert!(!categories::accepts("reviewed-nonscalar-slot", &c));
        assert!(!categories::accepts("reviewed-structural-slot", &c));
    }
}

#[test]
fn b_closed_retry_callers_bound_only_the_sleep_argument() {
    let all = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let r = rows
        .iter()
        .find(|r| r.path == "mycelium_a7_entry_controller.py" && r.line == 556)
        .expect("closed linear retry series");
    assert_eq!(r.value, Some(100));
    assert_eq!(r.unit, "milliseconds");
    assert!(proof::validate(r, &rows, &all));
    let mut bad = r.clone();
    bad.budget = "CancelAndCleanup".into();
    assert!(!proof::validate(&bad, &rows, &all));
    bad = r.clone();
    bad.value = Some(150);
    assert!(
        !proof::validate(&bad, &rows, &all),
        "last attempt has no following sleep"
    );
}
