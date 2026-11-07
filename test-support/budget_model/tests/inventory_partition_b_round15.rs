// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_speculative_decode.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_speculative_decode.py";
const MAXIMUM: &str = "PyMyceliumSpeculativeDecodeDeadlineMaximumMsL76";
const MINIMUM: &str = "PyMyceliumSpeculativeDecodeDeadlineMinimumMsL75";

fn at(rows: &[Bound], line: usize) -> &Bound {
    rows.iter()
        .find(|r| r.path == P && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {P}:{line}"))
}

#[test]
fn b_admitted_proposal_deadline_is_bounded_by_the_ceiling_in_every_returning_branch() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in [345, 346, 1815, 1832] {
        let r = at(&rows, line);
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(r.relation, MAXIMUM, "line {line}");
        assert!(r.semantic.contains("ceiling"));
        assert!(proof::validate(r, &rows, &all), "line {line}");
        // The floor has the same unit; same-unit existence alone must not satisfy provenance.
        let mut bad = r.clone();
        bad.relation = MINIMUM.into();
        assert!(
            !proof::validate(&bad, &rows, &all),
            "floor ID accepted at {line}"
        );
        bad = r.clone();
        bad.expression = "raw".into();
        assert!(!proof::validate(&bad, &rows, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_unclamped_remaining_request_deadline_inputs_stay_pending() {
    let rows = checked_in().unwrap();
    // The remaining request budget enters the clamp as an input and is not itself capped
    // where it is merely forwarded; the draft-side copy depends on mutable state.deadline.
    assert!(!rows.iter().any(|r| r.path == P && r.line == 1732));
    assert!(
        !rows
            .iter()
            .any(|r| r.path == "mycelium_a11_route_speculation.py"
                && (r.line == 377 || r.line == 464))
    );
}
