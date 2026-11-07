// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/route.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_live/route.py";
// Same unit, different declaration: the cleanup-fanout fallback reserve.
const LOOK_ALIKE: &str = "PyMyceliumDeadlinesCleanupFallbackReserveSecondsL193";

// Pinned (key) lines of the cancellation-deadline dict key and its same-function readers.
const FAMILY: [usize; 11] = [
    4103, 4119, 4175, 4177, 4196, 4379, 4382, 4404, 4434, 4446, 5238,
];

fn at(rows: &[Bound], line: usize) -> &Bound {
    rows.iter()
        .find(|r| r.path == P && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {P}:{line}"))
}

#[test]
fn b_route_cancellation_deadline_key_inherits_the_validated_cleanup_ceiling() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let ceiling = rows
        .iter()
        .find(|r| r.path == P && r.line == 4095)
        .expect("reviewed cancel_request ceiling writer");
    for line in FAMILY {
        let r = at(&rows, line);
        assert_eq!(r.disposition, "derived", "{line}");
        assert_eq!(r.unit, "milliseconds", "{line}");
        assert_eq!(r.relation, ceiling.relation, "{line}");
        assert!(r.value.is_none() && r.budget.is_empty(), "{line}");
        assert!(proof::validate(r, &rows, &all), "{line}");
        let mut bad = r.clone();
        bad.relation = LOOK_ALIKE.into();
        assert_ne!(bad.relation, r.relation);
        assert!(
            !proof::validate(&bad, &rows, &all),
            "{line}: wrong same-unit ID"
        );
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_route_cancellation_deadline_needs_its_writer_and_leaves_authorizer_results_pending() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    // The dict-key readers are only as good as the validated store (:4103).
    let without_store: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 4103))
        .cloned()
        .collect();
    for line in [4119, 4379, 4404, 4434, 5238] {
        assert!(
            !proof::validate(at(&rows, line), &without_store, &all),
            "{line}"
        );
    }
    // The fanout keyword has two writers; both must carry the relation.
    let without_tightened: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 4177))
        .cloned()
        .collect();
    assert!(!proof::validate(at(&rows, 4196), &without_tightened, &all));
    // The :4446 else-branch offset needs the module constant's own reviewed row.
    let without_constant: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 174))
        .cloned()
        .collect();
    assert!(!proof::validate(at(&rows, 4446), &without_constant, &all));
    // Results of the injected `authorize_cleanup` callback are not claimed by this family:
    // round 27 reviews exactly these sites under its own rule
    // (`partition-b-family:authorized-cleanup-deadline`, derived from the controller
    // allowance formula link; see inventory_partition_b_round27 for the exact rows/IDs and
    // its formula-link / local-writer / sibling-ID controls).
    for line in [4395, 4451, 5217, 5244, 5264, 5405] {
        let r = rows
            .iter()
            .find(|x| x.path == P && x.line == line)
            .unwrap_or_else(|| panic!("{P}:{line} must be reviewed by round 27"));
        assert!(
            r.review
                .starts_with("partition-b-family:authorized-cleanup-deadline;"),
            "{P}:{line}"
        );
        assert!(
            !r.review
                .starts_with("partition-b-family:cancel-deadline-key"),
            "{P}:{line} is not a cancel-deadline-key row"
        );
        assert_eq!(r.disposition, "derived", "{P}:{line}");
        assert_eq!(r.unit, "milliseconds", "{P}:{line}");
        assert_eq!(r.relation, "CancelAndCleanup", "{P}:{line}");
        assert!(r.value.is_none() && r.budget.is_empty(), "{P}:{line}");
        assert!(proof::validate(r, &rows, &all), "{P}:{line}");
    }
    // A row moved onto another expression of its key is rejected.
    let mut moved = at(&rows, 4119).clone();
    moved.expression = "float(active['cancellation_started_at'])".into();
    assert!(!proof::validate(&moved, &rows, &all));
}
