// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/liveness.py; O:path:mycelium_live/command_controller.py.
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
fn b_state_snapshot_reads_need_every_writer_reviewed_with_the_same_relation() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, line, relation, writers) in [
        (
            "mycelium_live/liveness.py",
            808,
            "PyMyceliumLiveLivenessIdleKeepaliveMsL17",
            vec![362, 694, 263],
        ),
        (
            "mycelium_live/command_controller.py",
            929,
            "PyCommandInterruptionCleanup",
            vec![471, 861, 272],
        ),
    ] {
        let r = at(&rows, path, line);
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(r.relation, relation);
        assert!(proof::validate(r, &rows, &all), "{path}:{line}");
        let mut bad = r.clone();
        bad.relation = "CancelAndCleanup".into();
        assert!(!proof::validate(&bad, &rows, &all), "wrong ID {path}");
        // Dropping any one writer's review, or changing its relation, rejects the snapshot read.
        for w in &writers {
            let without: Vec<Bound> = rows
                .iter()
                .filter(|x| !(x.path == path && x.line == *w))
                .cloned()
                .collect();
            if !without.iter().any(|x| x.path == path && x.line == line) {
                continue;
            }
            let ok = proof::validate(r, &without, &all);
            // The constructor/target row itself may be the removed one; either way it must fail.
            assert!(
                !ok,
                "{path}: writer {w} removed but snapshot still validates"
            );
        }
        let mut changed = rows.clone();
        for x in &mut changed {
            if x.path == path && writers.contains(&x.line) && x.disposition == "derived" {
                x.relation = "CancelAndCleanup".into();
            }
        }
        assert!(!proof::validate(r, &changed, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_state_snapshot_family_does_not_cover_other_readers() {
    let rows = checked_in().unwrap();
    // The effective-deadline min() and identity echoes read different fields/authorities.
    for (path, line) in [
        ("mycelium_live/command_controller.py", 842),
        ("mycelium_live/command_controller.py", 844),
        ("mycelium_live/command_controller.py", 891),
    ] {
        assert!(!rows.iter().any(|r| {
            r.path == path
                && r.line == line
                && r.review
                    .starts_with("partition-b-family:state-field-snapshot")
        }));
    }
}
