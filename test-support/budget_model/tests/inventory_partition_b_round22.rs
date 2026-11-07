// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/router_port.py; O:path:mycelium_live/route.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_live/router_port.py";
const CONTROLLER: &str = "mycelium_live/command_controller.py";
const OFFSET: &str = "PyQuantityMyceliumLiveRouterPortL847C16";
// Same unit and the same 3_600_000 value, but a different declaration (:222).
const LOOK_ALIKE: &str = "PyMyceliumLiveRouterPortMaxRecoveryDeadlineMsL222";

fn at<'a>(rows: &'a [Bound], path: &str, line: usize) -> &'a Bound {
    rows.iter()
        .find(|r| r.path == path && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {path}:{line}"))
}

#[test]
fn b_command_identity_deadline_copies_inherit_the_sole_one_hour_offset() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in [893, 1061, 1449, 1745, 2594, 2637] {
        let r = at(&rows, P, line);
        assert_eq!(r.disposition, "derived", "{line}");
        assert_eq!(r.unit, "milliseconds", "{line}");
        assert_eq!(r.relation, OFFSET, "{line}");
        assert!(r.value.is_none() && r.budget.is_empty(), "{line}");
        assert!(proof::validate(r, &rows, &all), "{line}");
        let mut bad = r.clone();
        bad.relation = LOOK_ALIKE.into();
        assert!(!proof::validate(&bad, &rows, &all), "{line}: look-alike ID");
    }
    // The router-port test hook compares directly with a command deadline (round 11 keeps that
    // comparison unreviewed): it must stay pending.
    assert!(
        !rows
            .iter()
            .any(|r| r.path == P && r.line == 1440 && r.review.starts_with("partition-b-"))
    );
    // The controller only receives CommandIdentity values, so its readers inherit the offset.
    for line in [842, 844, 891] {
        let r = at(&rows, CONTROLLER, line);
        assert_eq!(r.disposition, "derived", "{line}");
        assert_eq!(r.unit, "milliseconds", "{line}");
        assert_eq!(r.relation, OFFSET, "{line}");
        assert!(proof::validate(r, &rows, &all), "controller {line}");
        let mut bad = r.clone();
        bad.relation = LOOK_ALIKE.into();
        assert!(
            !proof::validate(&bad, &rows, &all),
            "controller {line}: look-alike ID"
        );
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_command_identity_deadline_needs_both_constructors_and_stays_off_the_route_side() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    // Without the reviewed :847 offset there is nothing to inherit.
    let without_offset: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 847))
        .cloned()
        .collect();
    assert!(!proof::validate(at(&rows, P, 2594), &without_offset, &all));
    // The recovery constructor (:2637) takes its parameter from one caller (:1745);
    // that caller's row is part of the proof.
    let without_caller: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 1745))
        .cloned()
        .collect();
    assert!(!proof::validate(at(&rows, P, 2637), &without_caller, &all));
    // The controller's min() only tightens the reviewed identity deadline: without the :845
    // writer row the :845 tightening has nothing to preserve, and the independent cleanup
    // deadline (:861, a different declaration) is not a source for either row.
    let without_writer: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == CONTROLLER && x.line == 842))
        .cloned()
        .collect();
    assert!(!proof::validate(
        at(&rows, CONTROLLER, 844),
        &without_writer,
        &all
    ));
    let cleanup = rows
        .iter()
        .find(|x| x.path == CONTROLLER && x.line == 861)
        .expect("reviewed cleanup deadline writer");
    assert_ne!(at(&rows, CONTROLLER, 844).relation, cleanup.relation);
    // route.py accepts an unchecked public `command_identity` mapping whose deadline is only
    // required to be in the future, so none of its copies may borrow the router-port offset.
    for line in [4151, 4858, 4913, 5030] {
        assert!(
            !rows.iter().any(|x| x.path == "mycelium_live/route.py"
                && x.line == line
                && x.review.starts_with("partition-b-")),
            "route.py:{line} must stay pending"
        );
    }
    // A row moved onto a different expression of the same key is rejected.
    let mut moved = at(&rows, P, 893).clone();
    moved.expression = "identity.cancellation_generation".into();
    assert!(!proof::validate(&moved, &rows, &all));
}
