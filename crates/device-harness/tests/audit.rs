// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::{
    audit::*,
    package::digest,
    release::{Identity, MANDATORY},
};
use std::collections::{BTreeMap, BTreeSet};
fn set(xs: &[&str]) -> BTreeSet<String> {
    xs.iter().map(|s| (*s).into()).collect()
}
fn inventory() -> Inventory {
    Inventory {
        source: "a".repeat(40),
        source_files: BTreeMap::from([("src/main.rs".into(), "f".repeat(64))]),
        required_obligations: set(&["R6", "R7"]),
        previous_tests: BTreeMap::from([
            ("old::removed".into(), set(&["R7"])),
            ("old::kept".into(), set(&["R6"])),
        ]),
        tests: BTreeMap::from([
            (
                "new::replacement".into(),
                Test {
                    job: "mandatory".into(),
                    obligations: set(&["R7"]),
                    incidents: set(&MANDATORY),
                    passed: true,
                },
            ),
            (
                "old::kept".into(),
                Test {
                    job: "mandatory".into(),
                    obligations: set(&["R6"]),
                    incidents: BTreeSet::new(),
                    passed: true,
                },
            ),
        ]),
        selected_jobs: set(&["mandatory"]),
        removals: BTreeMap::from([(
            "old::removed".into(),
            Removal {
                reviewer: "fixture-reviewer-not-real-approval".into(),
                evidence: "fixture-proof-not-retirement-authority".into(),
                replacements: set(&["new::replacement"]),
            },
        )]),
    }
}
fn identity(i: &Inventory) -> Identity {
    Identity {
        source: i.source.clone(),
        lock: "b".repeat(64),
        executable: "c".repeat(64),
        archive: "d".repeat(64),
        selection: digest(&serde_json::to_vec(i).unwrap()),
    }
}
#[test]
fn equal_counts_do_not_hide_removed_ids_or_uncovered_obligations() {
    let good = inventory();
    validate(&good, &identity(&good)).unwrap();
    assert_eq!(good.previous_tests.len(), good.tests.len());
    for case in 0..6 {
        let mut bad = good.clone();
        match case {
            0 => bad.removals.clear(),
            1 => bad
                .removals
                .get_mut("old::removed")
                .unwrap()
                .reviewer
                .clear(),
            2 => bad
                .removals
                .get_mut("old::removed")
                .unwrap()
                .evidence
                .clear(),
            3 => {
                bad.removals.get_mut("old::removed").unwrap().replacements =
                    set(&["not::collected"])
            }
            4 => bad
                .tests
                .get_mut("new::replacement")
                .unwrap()
                .obligations
                .clear(),
            _ => {
                bad.required_obligations.insert("R8".into());
            }
        }
        assert!(validate(&bad, &identity(&bad)).is_err(), "case {case}");
    }
}
#[test]
fn every_incident_needs_a_passing_test_in_a_selected_job() {
    for missing in MANDATORY {
        let mut bad = inventory();
        bad.tests
            .get_mut("new::replacement")
            .unwrap()
            .incidents
            .remove(missing);
        assert!(validate(&bad, &identity(&bad)).is_err(), "{missing}");
    }
    for case in 0..3 {
        let mut bad = inventory();
        match case {
            0 => bad.tests.get_mut("new::replacement").unwrap().job = "unselected".into(),
            1 => bad.tests.get_mut("new::replacement").unwrap().passed = false,
            _ => {
                bad.selected_jobs.insert("missing-job".into());
            }
        }
        assert!(validate(&bad, &identity(&bad)).is_err(), "case {case}");
    }
}
#[test]
fn candidate_and_source_inventory_are_exact_not_cross_language_digest_equivalence() {
    let i = inventory();
    let id = identity(&i);
    let mut changed = i.clone();
    changed.source = "e".repeat(40);
    assert!(validate(&changed, &id).is_err());
    let mut changed = i.clone();
    changed
        .source_files
        .insert("src/other.rs".into(), "e".repeat(64));
    assert!(validate(&changed, &id).is_err());
    for path in [
        "../escape.rs",
        "/absolute.rs",
        "src/tool.py",
        "pkg/__pycache__/cached.pyc",
        "pkg/PyO3/Cargo.toml",
        "venv/bin/python3",
    ] {
        let mut bad = i.clone();
        bad.source_files.insert(path.into(), "f".repeat(64));
        assert!(validate(&bad, &identity(&bad)).is_err(), "{path}");
    }
    let mut bad = i;
    bad.source_files.clear();
    assert!(validate(&bad, &identity(&bad)).is_err());
}
