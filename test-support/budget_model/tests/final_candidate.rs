// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/managed_runtime.py; F16.
use budget_model::inventory::{REPIN_PATH, candidates, checked_in};
use budgets::BudgetId;

use budget_model::inventory::{PREVIOUS_SOURCE_SHA, SOURCE_SHA, validate};

#[test]
fn final_receipt_cannot_be_backdated_to_the_previous_oracle() {
    let mut rows = checked_in().unwrap();
    let row = rows
        .iter_mut()
        .find(|r| r.path == REPIN_PATH && r.line == 333)
        .unwrap();
    row.source_sha = PREVIOUS_SOURCE_SHA.into();
    assert!(validate(&rows).is_err());
}

#[test]
fn retained_review_identity_cannot_be_silently_relabelled() {
    let mut rows = checked_in().unwrap();
    let row = rows
        .iter_mut()
        .find(|r| r.path == REPIN_PATH && r.line == 525)
        .unwrap();
    row.source_sha = SOURCE_SHA.into();
    assert!(validate(&rows).is_err());
}

#[test]
fn owner_stop_receipt_uses_attempt_count_not_a_duration_or_new_attempt() {
    let id = BudgetId::from_name("PyManagedWorkerReplacementAttempts");
    assert!(
        id.is_some(),
        "the final-candidate replacement ceiling needs a typed ID"
    );
    assert_eq!(id.unwrap().count().unwrap().get(), 3);
    let rows = checked_in().unwrap();
    let declaration = rows
        .iter()
        .find(|r| r.path == REPIN_PATH && r.line == 33)
        .unwrap();
    assert_eq!(declaration.expression, "3");
    assert_eq!(declaration.unit, "entries");
    let added = rows
        .iter()
        .find(|r| r.path == REPIN_PATH && r.line == 333)
        .unwrap();
    assert_eq!(added.disposition, "derived");
    assert_eq!(added.relation, "PyManagedWorkerReplacementAttempts");
    assert_eq!(added.unit, "entries");
    assert!(added.value.is_none());
    let recorded = rows
        .iter()
        .find(|r| r.path == REPIN_PATH && r.line == 334)
        .unwrap();
    assert_eq!(recorded.disposition, "not-a-bound");
    assert!(recorded.review.starts_with("category:clock-observation;"));
    assert!(
        candidates()
            .unwrap()
            .iter()
            .any(|r| r.path == REPIN_PATH && r.line == 33)
    );
}
