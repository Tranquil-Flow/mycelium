// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/scheduler.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_router/scheduler.py";
const FIELD: &str = "PyMyceliumRouterContractsRouterconfigMaximumPendingBytesL160";

#[test]
fn b_scheduler_pending_byte_comparison_enforces_the_router_config_field_only() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let r = rows
        .iter()
        .find(|r| r.path == P && r.line == 57 && r.review.starts_with("partition-b-family:"))
        .expect("pending-byte comparison row");
    assert_eq!(r.disposition, "derived");
    assert_eq!(r.unit, "bytes");
    assert_eq!(r.relation, FIELD);
    assert!(proof::validate(r, &rows, &all));
    // Same unit, different declaration (the per-batch ceiling): must be rejected.
    let mut bad = r.clone();
    bad.relation = "PyMyceliumRouterTokenPathAuthorityMaxReceiptBytesL27".into();
    assert!(!proof::validate(&bad, &rows, &all));
    // A millisecond relation is a unit mismatch.
    let mut wrong_unit = r.clone();
    wrong_unit.unit = "milliseconds".into();
    assert!(!proof::validate(&wrong_unit, &rows, &all));
    // The sibling retry hint (:52) keeps its own, different ID.
    let hint = rows
        .iter()
        .find(|x| x.path == P && x.line == 52)
        .expect("reviewed retry floor");
    assert_ne!(hint.relation, r.relation);
    // Without the declaration row there is nothing to inherit.
    let without: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == "mycelium_router/contracts.py" && x.line == 160))
        .cloned()
        .collect();
    assert!(!proof::validate(r, &without, &all));
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
