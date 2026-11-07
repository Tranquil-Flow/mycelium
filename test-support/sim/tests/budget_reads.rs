// SPDX-License-Identifier: AGPL-3.0-only
use sim::trace::Trace;
#[test]
fn an_unused_registry_declaration_is_not_a_replay_dependency() {
    let mut t: Trace = serde_json::from_str(include_str!("../traces/F16-v2.json")).unwrap();
    // Compile is never read by this lifecycle scenario. The v1 whole-registry
    // snapshot incorrectly treats it as a replay dependency.
    t.budget_snapshot.remove("Compile");
    assert_eq!(t.replay().unwrap(), t.expected_outcome);
}

#[test]
fn registry_growth_is_allowed_but_every_used_declaration_field_is_pinned() {
    use sim::trace_budgets::{registry, validate};
    let t: Trace = serde_json::from_str(include_str!("../traces/F16-v2.json")).unwrap();
    let mut grown = registry();
    let declaration = grown["OwnerPoll"].clone();
    grown.insert("UnrelatedNewBudget".into(), declaration);
    grown.remove("Compile");
    validate(&t.budget_snapshot, &grown).unwrap();
    assert_eq!(t.verify_recorded().unwrap(), t.expected_outcome);
    for field in 0..4 {
        let mut changed = t.clone();
        let used = changed.budget_snapshot.get_mut("OwnerPoll").unwrap();
        match field {
            0 => used.value.push_str("-changed"),
            1 => used.unit = "bytes".into(),
            2 => used.provenance.push_str("-changed"),
            _ => used.purpose.push_str("-changed"),
        }
        assert_eq!(changed.replay().unwrap_err(), "budget_snapshot_mismatch");
    }
}

#[test]
fn missing_direct_and_transitive_reads_fail_closed_and_unused_pins_are_rejected() {
    let t: Trace = serde_json::from_str(include_str!("../traces/F16-v2.json")).unwrap();
    for id in ["OwnerPoll", "HalfWallDivisor", "RolloverMargin"] {
        let mut changed = t.clone();
        assert!(changed.budget_snapshot.remove(id).is_some());
        assert_eq!(changed.replay().unwrap_err(), "unpinned_budget_read");
    }
    let mut changed = t.clone();
    changed.budget_snapshot.insert(
        "Compile".into(),
        sim::trace_budgets::registry()["Compile"].clone(),
    );
    assert_eq!(changed.replay().unwrap_err(), "budget_read_set_mismatch");
    // A changed driver introduces a real extra accessor read, not just a changed
    // serialized declaration. The same enforcement used by replay rejects it.
    let (_, reads) = sim::trace_budgets::capture(|| {
        let outcome = sim::model::run(&t.config, &t.commands)?;
        sim::span(budgets::BudgetId::Compile)?;
        Ok(outcome)
    })
    .unwrap();
    assert_eq!(
        sim::trace_budgets::enforce_reads(&t.budget_snapshot, &reads).unwrap_err(),
        "unpinned_budget_read"
    );
}

#[test]
fn even_outcome_neutral_input_drift_is_rejected() {
    let t: Trace = serde_json::from_str(include_str!("../traces/F16-v2.json")).unwrap();
    for field in 0..4 {
        let mut changed = t.clone();
        match field {
            0 => changed.config.authority_until_ms += 1,
            1 => changed.commands[0].task += 1,
            2 => changed.profile_id.push_str("-changed"),
            _ => changed.commands.push(sim::model::Input {
                at_ms: 0,
                task: 0,
                command: sim::model::Command::Observe,
            }),
        }
        assert_eq!(changed.replay().unwrap_err(), "execution_input_mismatch");
        assert_eq!(
            changed.corrected().replay().unwrap_err(),
            "execution_input_mismatch"
        );
    }
}

#[test]
fn minimized_trace_has_exact_reads_and_pinned_corrected_outcome() {
    use sim::model::{Config, Incident, Mode};
    let t = Trace::generate(
        "build".into(),
        "a".repeat(64),
        17,
        Config::standard(Incident::F16, Mode::Historical, 1).unwrap(),
    )
    .unwrap()
    .minimize()
    .unwrap();
    assert!(!t.budget_snapshot.contains_key("ControlClockSkew"));
    assert!(t.budget_snapshot.contains_key("HistoricalOuterStop"));
    assert!(
        !t.corrected_budget_snapshot
            .contains_key("HistoricalOuterStop")
    );
    t.verify_recorded().unwrap();
    t.corrected().verify_recorded().unwrap();
    let mut bad = t.corrected();
    bad.expected_outcome.log.reverse();
    assert_eq!(
        bad.verify_recorded().unwrap_err(),
        "recorded_outcome_mismatch"
    );
}

#[test]
fn profile_metadata_and_replay_event_order_are_not_silently_refreshed() {
    use sim::model::{Config, Incident, Mode};
    let t = Trace::generate(
        "build".into(),
        "a".repeat(64),
        17,
        Config::standard(Incident::F16, Mode::Historical, 1).unwrap(),
    )
    .unwrap();
    let mut bundle: latency_profiles::Bundle =
        serde_json::from_str(include_str!("../../profiles/data/v014-v1.json")).unwrap();
    t.validate_profile(&bundle).unwrap();
    bundle
        .profiles
        .iter_mut()
        .find(|p| p.id == t.profile_id)
        .unwrap()
        .samples[0]
        .method
        .push_str("changed");
    assert_eq!(
        t.validate_profile(&bundle).unwrap_err(),
        "profile_content_mismatch"
    );
    let mut changed = t;
    changed.expected_outcome.log.swap(0, 1);
    assert_eq!(changed.replay().unwrap_err(), "recorded_outcome_mismatch");
}
