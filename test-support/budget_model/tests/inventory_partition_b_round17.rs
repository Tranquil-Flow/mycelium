// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_deadlines.py; O:path:mycelium_router/hop_join.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

fn at<'a>(rows: &'a [Bound], path: &str, line: usize) -> &'a Bound {
    rows.iter()
        .find(|r| r.path == path && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {path}:{line}"))
}

#[test]
fn b_sole_caller_inputs_carry_the_callers_relation_and_no_extra_default() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, line, relation, wrong) in [
        (
            "mycelium_deadlines.py",
            1181,
            "PyMyceliumDeadlinesQualificationMaxAgeSecondsL313",
            // Same unit and the paired interval operand: swapping the pair must fail.
            "PyMyceliumDeadlinesRequalificationIntervalSecondsL319",
        ),
        (
            "mycelium_deadlines.py",
            1182,
            "PyMyceliumDeadlinesRequalificationIntervalSecondsL319",
            "PyMyceliumDeadlinesQualificationMaxAgeSecondsL313",
        ),
        (
            "mycelium_router/hop_join.py",
            137,
            "PyMyceliumRouterContractsRouterconfigLiveHopCollectionWindowSecondsL171",
            "PyMyceliumRouterContractsRouterconfigBackpressureRetryAfterSecondsL161",
        ),
    ] {
        let r = at(&rows, path, line);
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(r.relation, relation, "{path}:{line}");
        assert!(proof::validate(r, &rows, &all), "{path}:{line}");
        let mut bad = r.clone();
        bad.relation = wrong.into();
        assert!(
            !proof::validate(&bad, &rows, &all),
            "wrong ID {path}:{line}"
        );
        bad = r.clone();
        bad.symbol = "other".into();
        assert!(!proof::validate(&bad, &rows, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_checker_inputs_without_a_reviewed_sole_caller_stay_pending() {
    let rows = checked_in().unwrap();
    // fanout_window_ms is forwarded from an optional/None-default parameter whose own review
    // is still pending, so the peer-bound checker input cannot borrow a relation yet.
    assert!(
        !rows
            .iter()
            .any(|r| r.path == "mycelium_deadlines.py" && r.line == 1154)
    );
}
