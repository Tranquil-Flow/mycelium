// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-ROLL-04 (partial: a lost descriptor during rollover fails the run without retry; member or identity drift refuses; the v0.1.4 no-retry contract, V03 lane 3).
//!
//! Characterizes a known v0.1.4 failure, not the proposed v0.3 repair.
//! V03-PLAN lane 3: replace this no-retry contract ONLY in that release.
//! demo_chain.py:698-718,1113-1123 and owner_run.py:366 @ f4e366e9.
use runtime::rollover::{DescriptorGate, State};
use std::collections::BTreeMap;
fn members() -> BTreeMap<String, [String; 4]> {
    ["owner", "joined"]
        .into_iter()
        .map(|id| {
            (
                id.into(),
                [
                    "generation".into(),
                    "incarnation".into(),
                    format!("key-{id}"),
                    format!("endpoint-{id}"),
                ],
            )
        })
        .collect()
}
#[test]
fn v03_lane3_lost_descriptor_during_rollover_fails_run_without_retry() {
    let pins = members();
    let mut rollover = DescriptorGate::new(pins.clone());
    assert_eq!(rollover.state(), State::Collecting);
    // Both Macs remain joined with unchanged identity. ONLY the descriptor
    // collection snapshot lost a report (the 2026-10-27 live incident).
    assert_eq!(
        rollover.finish_collection(&pins, &["owner".into()]),
        Err("rollover_members_changed")
    );
    assert_eq!(rollover.state(), State::Failed("rollover_members_changed"));
    // A late report cannot transparently restart this failed run in v0.2.
    assert_eq!(
        rollover.finish_collection(&pins, &["owner".into(), "joined".into()]),
        Err("rollover_members_changed")
    );
    assert!(!rollover.may_compile());
}
#[test]
fn unchanged_full_collection_continues_but_member_or_identity_drift_refuses() {
    let pins = members();
    let reports = vec!["owner".into(), "joined".into()];
    let mut good = DescriptorGate::new(pins.clone());
    good.finish_collection(&pins, &reports).unwrap();
    assert_eq!(good.state(), State::Ready);
    assert!(good.may_compile());
    for field in 0..4 {
        let mut current = pins.clone();
        current.get_mut("joined").unwrap()[field] = "changed".into();
        let mut gate = DescriptorGate::new(pins.clone());
        assert_eq!(
            gate.finish_collection(&current, &reports),
            Err("rollover_members_changed")
        );
    }
    let mut current = pins.clone();
    current.remove("joined");
    let mut gate = DescriptorGate::new(pins.clone());
    assert_eq!(
        gate.finish_collection(&current, &reports),
        Err("rollover_members_changed")
    );
    let mut gate = DescriptorGate::new(pins);
    assert_eq!(
        gate.finish_collection(&current, &["owner".into(), "owner".into()]),
        Err("rollover_members_changed")
    );
}
