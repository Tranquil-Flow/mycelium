// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/route.py; O:path:mycelium_live/router_port.py;
// O:path:mycelium_live/command_controller.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const ROUTE: &str = "mycelium_live/route.py";
const PORT: &str = "mycelium_live/router_port.py";
const CONTROLLER: &str = "mycelium_live/command_controller.py";
const ALLOWANCE: &str = "CancelAndCleanup";
// Same unit and numerically the same 8 s, but the controller's own derived ID.
const CONTROLLER_ID: &str = "PyCommandInterruptionCleanup";

const SITES: [(&str, usize); 18] = [
    (ROUTE, 4395),
    (ROUTE, 4398),
    (ROUTE, 4451),
    (ROUTE, 4454),
    (ROUTE, 5217),
    (ROUTE, 5223),
    (ROUTE, 5230),
    (ROUTE, 5244),
    (ROUTE, 5250),
    (ROUTE, 5264),
    (ROUTE, 5267),
    (ROUTE, 5273),
    (ROUTE, 5405),
    (ROUTE, 5418),
    (PORT, 2160),
    (PORT, 2172),
    (PORT, 2477),
    (PORT, 2492),
];

fn at<'a>(rows: &'a [Bound], path: &str, line: usize) -> &'a Bound {
    rows.iter()
        .find(|r| r.path == path && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {path}:{line}"))
}

fn without(rows: &[Bound], path: &str, line: usize) -> Vec<Bound> {
    rows.iter()
        .filter(|x| !(x.path == path && x.line == line))
        .cloned()
        .collect()
}

fn all_candidates() -> BTreeMap<String, budget_model::inventory::Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}

#[test]
fn b_authorized_cleanup_deadlines_are_one_cancel_and_cleanup_allowance() {
    let all = all_candidates();
    let rows = checked_in().unwrap();
    for (path, line) in SITES {
        let r = at(&rows, path, line);
        assert_eq!(r.disposition, "derived", "{path}:{line}");
        assert_eq!(r.unit, "milliseconds", "{path}:{line}");
        assert_eq!(r.relation, ALLOWANCE, "{path}:{line}");
        assert!(r.value.is_none() && r.budget.is_empty(), "{path}:{line}");
        assert!(proof::validate(r, &rows, &all), "{path}:{line}");
        let mut bad = r.clone();
        bad.relation = CONTROLLER_ID.into();
        assert!(
            !proof::validate(&bad, &rows, &all),
            "{path}:{line}: sibling ID"
        );
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_authorized_cleanup_deadlines_need_the_formula_link_and_every_local_writer() {
    let all = all_candidates();
    let rows = checked_in().unwrap();
    // The controller's allowance is the route's allowance only through deadlines.py:234.
    let no_formula = without(&rows, "mycelium_deadlines.py", 234);
    for (path, line) in [(ROUTE, 4395), (PORT, 2477)] {
        assert!(
            !proof::validate(at(&rows, path, line), &no_formula, &all),
            "{path}:{line} without the formula row"
        );
    }
    // A forwarded local is only as good as each reaching writer.
    for (forward, writer) in [
        (4398, 4395),
        (4454, 4451),
        (5223, 5217),
        (5250, 5238),
        (5250, 5242),
        (5267, 5399),
        (5418, 5262),
    ] {
        assert!(
            !proof::validate(
                at(&rows, ROUTE, forward),
                &without(&rows, ROUTE, writer),
                &all
            ),
            "{forward} without writer {writer}"
        );
    }
    // A forward whose writer list is rewritten to a subset is not the exact census.
    let mut subset = at(&rows, ROUTE, 5250).clone();
    subset.expression = "cancellation_deadline".into();
    assert!(!proof::validate(&subset, &rows, &all));
    // The raw cleanup input of CommandController.cancel (:421) and the router-port values that
    // flow into it are pre-validation inputs (round 4 keeps them unreviewed).
    for (path, line) in [(CONTROLLER, 421), (PORT, 2757), (PORT, 2770)] {
        assert!(
            !rows
                .iter()
                .any(|x| x.path == path && x.line == line && x.review.starts_with("partition-b-")),
            "{path}:{line} must stay pending"
        );
    }
    // Inputs to the authorizer (not results) and public pass-through parameters stay pending.
    for (path, line) in [
        (PORT, 2709),
        (PORT, 2459),
        (PORT, 2479),
        (ROUTE, 4836),
        (ROUTE, 5008),
    ] {
        assert!(
            !rows
                .iter()
                .any(|x| x.path == path && x.line == line && x.review.starts_with("partition-b-")),
            "{path}:{line} must stay pending"
        );
    }
}
