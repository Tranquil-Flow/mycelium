// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:two_process_runtime_qualification.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "two_process_runtime_qualification.py";
const QUALIFICATION: &str = "PyTwoProcessRuntimeQualificationRunQualificationTimeoutSecondsL724";

#[test]
fn b_assignment_load_deadline_forwards_the_sole_caller_timeout_not_a_default() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in [458, 508] {
        let r = rows
            .iter()
            .find(|r| r.path == P && r.line == line && r.review.starts_with("partition-b-family:"))
            .unwrap_or_else(|| panic!("missing B family row {P}:{line}"));
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(r.relation, QUALIFICATION, "line {line}");
        assert!(proof::validate(r, &rows, &all), "line {line}");
        for wrong in [
            "CancelAndCleanup",
            "PyMyceliumSpeculativeDecodeDeadlineMaximumMsL76",
        ] {
            let mut bad = r.clone();
            bad.relation = wrong.into();
            assert!(
                !proof::validate(&bad, &rows, &all),
                "wrong ID {wrong} at {line}"
            );
        }
        let mut bad = r.clone();
        bad.expression = bad
            .expression
            .replace("float(timeout_seconds)", "timeout_seconds");
        if bad.expression != r.expression {
            assert!(!proof::validate(&bad, &rows, &all));
        }
    }
    // Without the reviewed caller keyword the deadline has no source.
    let r = rows.iter().find(|r| r.path == P && r.line == 458).unwrap();
    let without: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 757))
        .cloned()
        .collect();
    assert!(!proof::validate(r, &without, &all));
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
