// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/command_controller.py; O:path:mycelium_live/member_transport.py.
use budget_model::inventory::{candidates, categories, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

#[test]
fn b_clock_offset_and_validated_branch_families_preserve_policy_authority() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in include_str!("data/partition-b-round4.tsv").lines().skip(1) {
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f.len(), 3);
        let r = rows
            .iter()
            .find(|r| r.key == f[0])
            .expect("reviewed exact B offset/observation site");
        assert!(r.review.starts_with(f[1]));
        assert_eq!(r.unit, f[2]);
        if let Some(category) = f[1].strip_prefix("category:") {
            assert!(categories::accepts(category, &all[f[0]]));
            let mut bad = all[f[0]].clone();
            bad.source.push('!');
            assert!(!categories::accepts(category, &bad));
        } else {
            assert!(proof::validate(r, &rows, &all), "{}", r.key);
            let mut bad = r.clone();
            if r.budget.is_empty() {
                bad.relation = if r.relation == "CancelAndCleanup" {
                    "RouterClose"
                } else {
                    "CancelAndCleanup"
                }
                .into();
            } else {
                bad.budget = "CancelAndCleanup".into();
            }
            assert!(
                !proof::validate(&bad, &rows, &all),
                "wrong duration ID {}",
                r.key
            );
            bad = r.clone();
            bad.expression.push_str(" + 1");
            assert!(!proof::validate(&bad, &rows, &all));
        }
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_serialized_expiry_aliases_do_not_restart_the_local_allowance() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, line) in [
        ("mycelium_live/artifact_transport.py", 563),
        ("mycelium_live/member_transport.py", 1050),
        ("mycelium_live/supervisor.py", 539),
    ] {
        let row = rows
            .iter()
            .find(|r| r.path == path && r.line == line)
            .expect("one original expiry propagated");
        assert!(budget_model::inventory::bindings::validate(
            row, &rows, &all
        ));
        let mut bad = row.clone();
        bad.relation = "CancelAndCleanup".into();
        assert_ne!(bad.relation, row.relation);
        assert!(!budget_model::inventory::bindings::validate(
            &bad, &rows, &all
        ));
    }
}

#[test]
fn b_raw_cleanup_input_and_stale_fault_writer_do_not_inherit_the_validated_branch() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, accepted, refused) in [
        ("mycelium_live/command_controller.py", 416, 421),
        ("mycelium_live/a6_recovery.py", 436, 477),
    ] {
        let original = rows
            .iter()
            .find(|r| r.path == path && r.line == accepted)
            .expect("validated branch");
        let c = all
            .values()
            .find(|c| c.path == path && c.line == refused)
            .unwrap();
        let mut bad = original.clone();
        bad.key.clone_from(&c.key);
        bad.line = c.line;
        bad.column = c.column;
        bad.scope.clone_from(&c.scope);
        bad.symbol.clone_from(&c.symbol);
        bad.expression.clone_from(&c.expression);
        assert!(
            !proof::validate(&bad, &rows, &all),
            "unvalidated writer {}",
            c.key
        );
    }
}
