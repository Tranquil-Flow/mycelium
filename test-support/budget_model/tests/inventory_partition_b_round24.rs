// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/route.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_live/route.py";
const HORIZON: &str = "PyMyceliumDeadlinesCleanupReconciliationHorizonSecondsL220";
const POLL: &str = "PyMyceliumDeadlinesCleanupReceiptPollSecondsL217";

fn find(rows: &[Bound], line: usize, family: bool) -> Option<&Bound> {
    rows.iter().find(|r| {
        r.path == P && r.line == line && (!family || r.review.starts_with("partition-b-family:"))
    })
}

fn at(rows: &[Bound], line: usize) -> &Bound {
    find(rows, line, true).unwrap_or_else(|| panic!("missing B family row {P}:{line}"))
}

#[test]
fn b_cleanup_reconciliation_chain_keeps_the_horizon_and_poll_relations_apart() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (line, relation, wrong) in [
        (3886, HORIZON, POLL),
        (3988, HORIZON, POLL),
        (3889, POLL, HORIZON),
        (3919, POLL, HORIZON),
    ] {
        let r = at(&rows, line);
        assert_eq!(r.disposition, "derived", "{line}");
        assert_eq!(r.unit, "milliseconds", "{line}");
        assert_eq!(r.relation, relation, "{line}");
        assert!(proof::validate(r, &rows, &all), "{line}");
        let mut bad = r.clone();
        bad.relation = wrong.into();
        assert!(!proof::validate(&bad, &rows, &all), "{line}: swapped ID");
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_cleanup_reconciliation_requires_every_caller_and_default_to_be_reviewed() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let drop = |line: usize| -> Vec<Bound> {
        rows.iter()
            .filter(|x| !(x.path == P && x.line == line))
            .cloned()
            .collect()
    };
    // The submit helper has two callers (:3857 schedule, :3988 resubmit): both must be reviewed.
    assert!(!proof::validate(at(&rows, 3886), &drop(3988), &all));
    assert!(!proof::validate(at(&rows, 3886), &drop(3857), &all));
    // The retry hint's only non-None caller is the :3991 resubmit; its relation is the target.
    assert!(!proof::validate(at(&rows, 3889), &drop(3991), &all));
    // The omitted-constraint defaults must stay reviewed None markers.
    assert!(!proof::validate(at(&rows, 3889), &drop(3870), &all));
    assert!(!proof::validate(at(&rows, 3889), &drop(3912), &all));
    // The wait is bounded by the retry hint (poll interval), not by the horizon operand.
    assert!(!proof::validate(at(&rows, 3919), &drop(3889), &all));
    // The ordinary cleanup sleeps/attempt windows were reviewed before and are untouched.
    assert!(find(&rows, 3935, false).is_some());
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
