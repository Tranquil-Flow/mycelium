// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/local_preparer.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_live/local_preparer.py";
const MAX: &str = "PyMyceliumDeadlinesStagePackMaxAuthorityTtlMsL306";
const MIN: &str = "PyMyceliumDeadlinesStagePackMinAuthorityTtlMsL302";

fn at(rows: &[Bound], line: usize) -> &Bound {
    rows.iter()
        .find(|r| r.path == P && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {P}:{line}"))
}

fn millis(name: &str) -> u64 {
    match budgets::BudgetId::from_name(name)
        .expect("registered id")
        .entry()
        .value
    {
        budgets::table::Value::Duration(span) => span.millis(),
        other => panic!("{name} is not a duration: {other:?}"),
    }
}

#[test]
fn b_stage_pack_lease_chain_is_bounded_by_the_validated_maximum_not_the_minimum() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    assert!(
        millis(MIN) <= millis(MAX),
        "the warm-only floor must fit the ceiling"
    );
    for line in [1381, 1403, 1449, 1469, 1472, 1554] {
        let r = at(&rows, line);
        assert_eq!(r.disposition, "derived", "{line}");
        assert_eq!(r.unit, "milliseconds", "{line}");
        assert_eq!(r.relation, MAX, "{line}");
        assert!(proof::validate(r, &rows, &all), "{line}");
        let mut bad = r.clone();
        bad.relation = MIN.into();
        assert!(
            !proof::validate(&bad, &rows, &all),
            "{line}: floor ID is not the ceiling"
        );
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_stage_pack_lease_expiry_needs_both_lease_writers_and_leaves_authority_expiry_pending() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    // The expiry offset covers two lease_ttl_ms writers (:1443 floor, :1449 validated).
    let without_warm: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 1443))
        .cloned()
        .collect();
    assert!(!proof::validate(at(&rows, 1469), &without_warm, &all));
    // The preflight copy is only as good as the helper's validated return.
    let without_helper: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 1381))
        .cloned()
        .collect();
    assert!(!proof::validate(at(&rows, 1449), &without_helper, &all));
    // Absolute authority/evidence expiries come from the authorization document: still pending.
    for line in [1027, 1035, 1423] {
        assert!(
            !rows
                .iter()
                .any(|x| x.path == P && x.line == line && x.review.starts_with("partition-b-")),
            "{P}:{line} must stay pending"
        );
    }
    // A row moved to a different expression is rejected.
    let mut moved = at(&rows, 1469).clone();
    moved.expression = "issued_at_unix_ms".into();
    assert!(!proof::validate(&moved, &rows, &all));
}
