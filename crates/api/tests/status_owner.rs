// SPDX-License-Identifier: AGPL-3.0-only
use api::{schema::Snapshot, status::StatusOwner};
use serde_json::{Value, json};
fn snapshot(label: &str) -> Snapshot {
    let mut v: Value =
        serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap();
    v["label"] = json!(label);
    Snapshot::parse(v).unwrap()
}
#[test]
fn collection_revision_is_not_source_generation_and_stale_failure_cannot_erase_success() {
    let owner = StatusOwner::new(7).unwrap();
    let old = owner.begin().unwrap();
    let new = owner.begin().unwrap();
    owner.complete(new, 7, snapshot("new")).unwrap();
    assert_eq!(
        owner.fail(old).unwrap_err().code,
        "stale_status_publication"
    );
    assert_eq!(
        owner.status().read().unwrap()["status_snapshot"]["refresh_failed"],
        false
    );
    let failed = owner.begin().unwrap();
    owner.fail(failed).unwrap();
    let v = owner.status().read().unwrap();
    assert_eq!(v["label"], "new");
    assert_eq!(v["status_snapshot"]["refresh_failed"], true);
    let next = owner.begin().unwrap();
    owner
        .complete(next, 7, snapshot("same-generation"))
        .unwrap();
    assert_eq!(owner.status().read().unwrap()["label"], "same-generation");
}
#[test]
fn successor_fences_collectors_before_first_success_and_failure_orders_completions() {
    let owner = StatusOwner::new(1).unwrap();
    let first = owner.begin().unwrap();
    owner.complete(first, 1, snapshot("old-good")).unwrap();
    let stale = owner.begin().unwrap();
    owner.advance(2).unwrap();
    assert_eq!(
        owner
            .complete(stale, 1, snapshot("retired"))
            .unwrap_err()
            .code,
        "stale_status_publication"
    );
    let wrong_source = owner.begin().unwrap();
    assert_eq!(
        owner
            .complete(wrong_source, 1, snapshot("wrong-source"))
            .unwrap_err()
            .code,
        "stale_status_publication"
    );
    let slow = owner.begin().unwrap();
    let failed = owner.begin().unwrap();
    owner.fail(failed).unwrap();
    assert!(owner.complete(slow, 2, snapshot("slow")).is_err());
    assert_eq!(owner.status().read().unwrap()["label"], "old-good");
    assert!(owner.advance(2).is_err());
    assert!(owner.advance(1).is_err());
    let current = owner.begin().unwrap();
    owner.complete(current, 2, snapshot("successor")).unwrap();
}
#[test]
fn refresh_runs_collection_without_status_lock_and_preserves_source_and_fencing_errors() {
    let owner = StatusOwner::new(1).unwrap();
    owner.refresh(|| Ok((1, snapshot("initial")))).unwrap();
    owner
        .refresh(|| {
            assert_eq!(owner.status().read().unwrap()["label"], "initial");
            Ok((1, snapshot("next")))
        })
        .unwrap();
    let error = owner
        .refresh(|| {
            // A nested refresh settles later while the older source is blocked.
            owner.refresh(|| Ok((1, snapshot("newer")))).unwrap();
            Err(api::schema::PublicError::new(503, "source_failed"))
        })
        .unwrap_err();
    assert_eq!(error.primary.code, "source_failed");
    assert_eq!(error.settlement.unwrap().code, "stale_status_publication");
    let state = owner.status().read().unwrap();
    assert_eq!(state["label"], "newer");
    assert_eq!(state["status_snapshot"]["refresh_failed"], false);
}
#[test]
fn tickets_are_bound_to_owner_and_legacy_publish_cannot_bypass_owner() {
    let a = StatusOwner::new(1).unwrap();
    let b = StatusOwner::new(1).unwrap();
    assert!(b.fail(a.begin().unwrap()).is_err());
    let target = a.status();
    assert_eq!(
        target.publish(99, snapshot("bypass")).unwrap_err().code,
        "status_owner_required"
    );
    let current = a.begin().unwrap();
    a.complete(current, 1, snapshot("owned")).unwrap();
    target.record_refresh_failure();
    // Legacy method has no Result; record explicit misuse without mutating the
    // owner's coherent refresh outcome. New callers must use ticketed fail().
    assert_eq!(
        target.read().unwrap()["status_snapshot"]["refresh_failed"],
        false
    );
    assert!(target.legacy_write_refused());
}
