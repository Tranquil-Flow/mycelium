// SPDX-License-Identifier: AGPL-3.0-only
//! Review regressions against the interrupted L2b candidate, not product tests.
use sim::{model::*, trace::Trace};

#[test]
fn f14_stages_both_recorded_shares_within_each_consent() {
    // L10-SPLIT.md:24-33: the member share changes with the owner partition.
    let c = Config::standard(Incident::F14, Mode::Corrected, 1).unwrap();
    let o = run(
        &c,
        &[Input {
            at_ms: 0,
            task: 1,
            command: Command::Begin,
        }],
    )
    .unwrap();
    assert_eq!(o.metrics.get("member_selected_bytes"), Some(&1_735_338_491));
    assert_eq!(o.metrics.get("member_reserved_bytes"), Some(&1_735_338_491));
    assert_eq!(o.metrics.get("member_staged_bytes"), Some(&1_735_338_491));
    assert_eq!(o.metrics["staged_bytes"], 1_997_798_421);
    assert!(o.metrics["member_staged_bytes"] <= o.metrics["member_storage_cap"]);
    assert!(o.metrics["staged_bytes"] <= o.metrics["owner_storage_cap"]);
    assert_eq!(o.successful_chats, 1);
}

#[test]
fn f14_candidate_selection_checks_asymmetric_caps_and_preserves_fresh_default() {
    use budgets::BudgetId as B;
    use sim::incidents::select_route_shares;
    let saved = B::SavedStorageConsent.bytes().unwrap().get();
    let fresh = B::FreshStorageConsent.bytes().unwrap().get();
    let old = select_route_shares(Mode::Corrected, fresh, saved).unwrap();
    assert_eq!((old.owner, old.member), (2_227_449_134, 1_505_687_777));
    assert_eq!(
        select_route_shares(Mode::Corrected, saved, saved / 2).unwrap_err(),
        "member_storage_cap_below_share:RouteStorage"
    );
    assert!(select_route_shares(Mode::Corrected, saved / 2, fresh).is_err());
}

#[test]
fn cli_rejects_recorded_outcome_drift_before_incident_assertion() {
    let mut t: Trace = serde_json::from_str(include_str!("../traces/F1-v2.json")).unwrap();
    t.expected_outcome.log.push("tampered-event".into());
    let path = std::env::temp_dir().join(format!("l2b-review-trace-{}.json", std::process::id()));
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .unwrap();
    serde_json::to_writer(file, &t).unwrap();
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_sim"))
        .args([
            "replay",
            path.to_str().unwrap(),
            &t.build_id,
            &t.cargo_lock_sha256,
            concat!(env!("CARGO_MANIFEST_DIR"), "/../profiles/data/v014-v1.json"),
            "historical",
        ])
        .output()
        .unwrap();
    std::fs::remove_file(path).unwrap();
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("recorded_outcome_mismatch"),
        "replay must reject golden drift before claiming incident RED: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn recorded_event_order_config_and_failure_set_are_bound() {
    let t: Trace = serde_json::from_str(include_str!("../traces/F1-v2.json")).unwrap();
    t.verify_recorded().unwrap();
    for mutant in 0..3 {
        let mut changed = t.clone();
        match mutant {
            0 => changed.expected_outcome.log.swap(0, 1),
            1 => changed.expected_failure.clear(),
            _ => {
                changed.config.authority_until_ms = sim::span(budgets::BudgetId::MembershipTtl)
                    .unwrap()
                    .millis()
            }
        }
        assert!(changed.verify_recorded().is_err());
    }
}

#[test]
fn every_extended_trace_rejects_build_lock_version_and_budget_drift() {
    for raw in [
        include_str!("../traces/F1-v2.json"),
        include_str!("../traces/F2-v2.json"),
        include_str!("../traces/F3-v2.json"),
        include_str!("../traces/F5-v2.json"),
        include_str!("../traces/F13-v2.json"),
        include_str!("../traces/F14-v2.json"),
        include_str!("../traces/F15b-v2.json"),
    ] {
        let t: Trace = serde_json::from_str(raw).unwrap();
        t.require_identity(&t.build_id, &t.cargo_lock_sha256)
            .unwrap();
        assert!(
            t.require_identity("wrong-build", &t.cargo_lock_sha256)
                .is_err()
        );
        assert!(t.require_identity(&t.build_id, &"0".repeat(64)).is_err());
        for mutant in 0..3 {
            let mut changed = t.clone();
            match mutant {
                0 => changed.simulator_version.push_str("-changed"),
                1 => changed.prng_version.push_str("-changed"),
                _ => changed.budget_snapshot.clear(),
            }
            assert!(changed.replay().is_err());
        }
    }
}
