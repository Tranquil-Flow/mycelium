// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/supervisor.py; O:path:mycelium_live/registry.py;
// O:path:mycelium_live/route.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const SUP: &str = "mycelium_live/supervisor.py";
const REG: &str = "mycelium_live/registry.py";
const ROUTE: &str = "mycelium_live/route.py";
const GUARD: &str = "PyUpperGuardMyceliumLiveSupervisorL514C47";
// Same unit; the lower floor registered from the same chained comparison.
const FLOOR: &str = "PyRangeFloorMyceliumLiveSupervisorL514C47";

fn at<'a>(rows: &'a [Bound], path: &str, line: usize) -> &'a Bound {
    rows.iter()
        .find(|r| r.path == path && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {path}:{line}"))
}

#[test]
fn b_invite_ttl_chain_carries_only_the_upper_acceptance_guard() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, line) in [(SUP, 513), (REG, 921), (ROUTE, 6829)] {
        let r = at(&rows, path, line);
        assert_eq!(r.disposition, "derived", "{path}:{line}");
        assert_eq!(r.relation, GUARD, "{path}:{line}");
        assert!(proof::validate(r, &rows, &all), "{path}:{line}");
        let mut bad = r.clone();
        bad.relation = FLOOR.into();
        assert!(
            !proof::validate(&bad, &rows, &all),
            "{path}:{line}: floor ID"
        );
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_invite_ttl_chain_needs_the_guard_and_each_forwarding_hop() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let drop = |path: &str, line: usize| -> Vec<Bound> {
        rows.iter()
            .filter(|x| !(x.path == path && x.line == line))
            .cloned()
            .collect()
    };
    // The pre-use assignment needs the reviewed upper guard (:514 upper comparison row).
    let guard_key = rows
        .iter()
        .find(|x| x.path == SUP && x.line == 514 && x.budget == GUARD)
        .map(|x| x.key.clone())
        .expect("reviewed upper guard row");
    let without_guard: Vec<_> = rows
        .iter()
        .filter(|x| x.key != guard_key)
        .cloned()
        .collect();
    assert!(!proof::validate(at(&rows, SUP, 513), &without_guard, &all));
    // The registry hop forwards the supervisor keyword; the route hop forwards the registry's.
    assert!(!proof::validate(at(&rows, REG, 921), &drop(SUP, 520), &all));
    assert!(!proof::validate(
        at(&rows, ROUTE, 6829),
        &drop(REG, 921),
        &all
    ));
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
