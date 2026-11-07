// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_deadlines.py; F1/F2/F16.
use budget_model::inventory::{Bound, SOURCE_SHA, require_complete, validate};
use std::collections::BTreeSet;

#[test]
fn derived_dispositions_require_real_typed_target_and_no_fabricated_value() {
    let mut row = sample();
    row.budget.clear();
    row.value = None;
    row.disposition = "derived".into();
    row.relation = "WorkerCleanupReserve".into();
    assert_eq!(validate(&[row.clone()]), Ok(()));
    row.relation = "NotDeclared".into();
    assert!(validate(&[row.clone()]).is_err());
    row.relation = "WorkerCleanupReserve".into();
    row.unit = "bytes".into();
    assert!(validate(&[row.clone()]).is_err());
    row.unit = "milliseconds".into();
    row.value = Some(10_000);
    assert!(validate(&[row]).is_err());
}

#[test]
fn parser_refuses_unknown_header_truncated_rows_and_invalid_numbers() {
    use budget_model::inventory::{HEADER, parse};
    assert!(parse("wrong header\n").is_err());
    assert!(parse(&format!("{HEADER}\nshort\trow\n")).is_err());
    let fields = [
        "key",
        "file.py",
        "nan",
        "0",
        "scope",
        "symbol",
        "1",
        "milliseconds",
        "1",
        "meaning",
        "local",
        "id",
        "",
        "review",
        "relation",
        SOURCE_SHA,
    ];
    assert!(parse(&format!("{HEADER}\n{}\n", fields.join("\t"))).is_err());
}

fn sample() -> Bound {
    Bound {
        key: "mycelium_a13_macos/worker.py:35:0:assignment:_CLEANUP_RESERVE_SECONDS".into(),
        path: "mycelium_a13_macos/worker.py".into(),
        line: 35,
        column: 0,
        scope: "<module>".into(),
        symbol: "_CLEANUP_RESERVE_SECONDS".into(),
        expression: "10".into(),
        unit: "milliseconds".into(),
        value: Some(10_000),
        semantic: "Worker cleanup reserve within the authorized run wall".into(),
        authority: "member-consent-or-owner-local".into(),
        budget: "WorkerCleanupReserve".into(),
        disposition: String::new(),
        review: "L1b: declaration and worker admission reviewed at pinned source".into(),
        relation: "reserve is subtracted from effective worker wall".into(),
        source_sha: SOURCE_SHA.into(),
    }
}

#[test]
fn accepts_mapped_bound_and_complete_candidate_set() {
    let row = sample();
    let keys = BTreeSet::from([row.key.clone()]);
    assert_eq!(validate(std::slice::from_ref(&row)), Ok(()));
    assert_eq!(require_complete(&[row], &keys), Ok(()));
}

#[test]
fn rejects_missing_budget_and_disposition() {
    let mut row = sample();
    row.budget.clear();
    assert!(validate(&[row]).is_err());
}
#[test]
fn rejects_duplicate_inventory_keys() {
    let row = sample();
    assert!(validate(&[row.clone(), row]).is_err());
}
#[test]
fn rejects_dangling_budget() {
    let mut row = sample();
    row.budget = "NoSuchBudget".into();
    assert!(validate(&[row]).is_err());
}
#[test]
fn rejects_unit_and_value_drift() {
    let mut row = sample();
    row.unit = "bytes".into();
    assert!(validate(&[row.clone()]).is_err());
    row.unit = "milliseconds".into();
    row.value = Some(30_000);
    assert!(validate(&[row]).is_err());
}
#[test]
fn rejects_unreviewed_and_unknown_dispositions() {
    let mut row = sample();
    row.budget.clear();
    row.value = None;
    row.disposition = "pending".into();
    assert!(validate(&[row.clone()]).is_err());
    row.disposition = "test-only".into();
    row.review.clear();
    assert!(validate(&[row]).is_err());
}
#[test]
fn rejects_ambiguous_mapping_and_disposition() {
    let mut row = sample();
    row.disposition = "test-only".into();
    assert!(validate(&[row]).is_err());
}
#[test]
fn rejects_wrong_pin_and_missing_provenance() {
    let mut row = sample();
    row.source_sha = "f5bfb709".into();
    assert!(validate(&[row.clone()]).is_err());
    row.source_sha = SOURCE_SHA.into();
    row.line = 0;
    assert!(validate(&[row]).is_err());
}
#[test]
fn refuses_claim_of_completeness_when_candidate_is_missing() {
    let row = sample();
    let keys = BTreeSet::from([row.key.clone(), "unreviewed-call-site".into()]);
    assert!(require_complete(&[row], &keys).is_err());
}
#[test]
fn rejects_untracked_inventory_row() {
    assert!(require_complete(&[sample()], &BTreeSet::new()).is_err());
}
#[test]
fn rejects_category_claim_not_satisfied_by_real_candidate() {
    let mut row = sample();
    row.budget.clear();
    row.value = None;
    row.unit = "not-applicable".into();
    row.disposition = "not-a-bound".into();
    row.review = "category:string-join; string concatenation".into();
    assert!(validate(&[row]).is_err());
}

#[test]
fn accepts_reviewed_non_enforcing_disposition() {
    let mut row = sample();
    row.budget.clear();
    row.value = None;
    row.unit = "not-applicable".into();
    row.disposition = "test-only".into();
    row.review = "Explicit fixture construction, not an executing service policy".into();
    assert_eq!(validate(&[row]), Ok(()));
}
