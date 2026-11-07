// SPDX-License-Identifier: AGPL-3.0-only
use obligations::{Registry, Snapshot, validate};
use serde::Deserialize;

#[derive(Deserialize)]
struct Fixture {
    registry: Registry,
    snapshot: Snapshot,
}
fn fixture(raw: &str) -> Fixture {
    serde_json::from_str(raw).unwrap()
}
fn valid() -> Fixture {
    fixture(include_str!("fixtures/valid.json"))
}
fn rejected(f: Fixture, code: &str, subject: &str) {
    let errors = validate(&f.registry, &f.snapshot).expect_err("invalid inventory was accepted");
    assert!(
        errors
            .iter()
            .any(|e| e.code == code && e.subject == subject),
        "{errors:?}"
    );
}
#[test]
fn positive_control() {
    let f = valid();
    assert_eq!(validate(&f.registry, &f.snapshot), Ok(()));
}
#[test]
fn rejects_unmapped_supplied_path() {
    rejected(
        fixture(include_str!("fixtures/unmapped.json")),
        "unmapped_path",
        "new.py",
    );
}
#[test]
fn rejects_duplicate_id() {
    rejected(
        fixture(include_str!("fixtures/duplicate-id.json")),
        "duplicate_id",
        "O:a",
    );
}
#[test]
fn rejects_dangling_module() {
    rejected(
        fixture(include_str!("fixtures/dangling-destination.json")),
        "unknown_destination",
        "control::typo",
    );
}
#[test]
fn rejects_retirement_without_reason() {
    rejected(
        fixture(include_str!("fixtures/retirement-no-reason.json")),
        "retirement_reason_missing",
        "P:a.py",
    );
}
#[test]
fn rejects_retirement_without_replacement() {
    rejected(
        fixture(include_str!("fixtures/retirement-no-replacement.json")),
        "retirement_replacement_missing",
        "P:a.py",
    );
}
#[test]
fn rejects_missing_f15b_without_confusing_f15() {
    rejected(
        fixture(include_str!("fixtures/missing-f-id.json")),
        "missing_escape",
        "F15b",
    );
}
#[test]
fn rejects_every_missing_escape() {
    for id in [
        "F1", "F2", "F3", "F4", "F5", "F13", "F14", "F15", "F15b", "F16",
    ] {
        let mut f = valid();
        f.registry.escapes.retain(|e| e.id != id);
        rejected(f, "missing_escape", id);
    }
}
#[test]
fn rejects_missing_contract_crosswalk() {
    let mut f = valid();
    f.registry.contracts.clear();
    rejected(f, "unmapped_contract", "mycelium.fixture.v1");
}
#[test]
fn rejects_missing_test_group_crosswalk() {
    let mut f = valid();
    f.registry.test_groups.clear();
    rejected(f, "unmapped_test", "a.py");
}
#[test]
fn rejects_dangling_obligation() {
    let mut f = valid();
    f.registry.obligations.clear();
    rejected(f, "unknown_obligation", "O:a");
}
#[test]
fn rejects_source_revision_mismatch() {
    let mut f = valid();
    f.snapshot.source_sha = "3".repeat(40);
    rejected(f, "source_mismatch", &"3".repeat(40));
}
#[test]
fn rejects_duplicate_path_with_distinct_id() {
    let mut f = valid();
    let mut row = f.registry.paths[0].clone();
    row.id = "P:other".into();
    f.registry.paths.push(row);
    rejected(f, "duplicate_path", "a.py");
}
#[test]
fn rejects_empty_distinguishing_observation() {
    let mut f = valid();
    f.registry.escapes[0].observation.clear();
    rejected(f, "escape_evidence_missing", "F1");
}
#[test]
fn rejects_unreviewed_retirement() {
    let mut f = valid();
    f.registry.paths[0].disposition = obligations::Disposition::Retire {
        reason: "Equivalent property coverage".into(),
        replacement: "O:a".into(),
        reviewer: "".into(),
    };
    rejected(f, "retirement_reviewer_missing", "P:a.py");
}
#[test]
fn accepts_reviewed_retirement_with_replacement() {
    let mut f = valid();
    f.registry.paths[0].disposition = obligations::Disposition::Retire {
        reason: "Equivalent property coverage".into(),
        replacement: "O:a".into(),
        reviewer: "L12 fixture reviewer".into(),
    };
    assert_eq!(validate(&f.registry, &f.snapshot), Ok(()));
}
