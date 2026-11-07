// SPDX-License-Identifier: AGPL-3.0-only
//! The steps that are not ported yet end a run with ONE tagged refusal that v0.1.4 never produces
//! (`owner_run_step_not_ported`), naming the step and the seam that must close; they never succeed.
mod support;
use owner_run::machine::closed_code;
use owner_run::steps::{NOT_PORTED, NOT_PORTED_CODE, not_ported};
use owner_run::{Clock, Cmd, StepOutcome};
use serde_json::Value;
use support::Sim;

const FIXTURES: [&str; 4] = [
    include_str!("fixtures/oracle_owner_run.json"),
    include_str!("fixtures/oracle_demo_action.json"),
    include_str!("fixtures/oracle_ceiling.json"),
    include_str!("fixtures/oracle_model_step.json"),
];

/// Proves: B-RUN-03 (a step that cannot run ends the run with a closed code and a message, never a success).
#[test]
fn every_unported_step_is_an_explicit_tagged_refusal() {
    let names: Vec<&str> = NOT_PORTED.iter().map(|(n, _, _)| *n).collect();
    assert_eq!(names, ["serving"]);
    assert!(closed_code(NOT_PORTED_CODE));
    for (step, tag, seam) in NOT_PORTED {
        let owner_run::chain::StepError::Refused(e) = not_ported(step) else {
            panic!("{step}: not a refusal");
        };
        assert_eq!(e.code, NOT_PORTED_CODE);
        let message = e.message.expect("message");
        assert!(
            message.contains(step) && message.contains(tag) && message.contains(seam),
            "{message}"
        );
        assert!(
            tag.starts_with("NP-SLICE1-") && seam.starts_with("SE-"),
            "{tag} {seam}"
        );
    }
}

/// Proves: B-RUN-03 (v0.1.4 never produces the code, so it cannot hide a ported outcome: it is absent from
/// every executed fixture).
#[test]
fn v014_never_produces_the_not_ported_code() {
    for (i, text) in FIXTURES.iter().enumerate() {
        assert!(
            !text.contains(NOT_PORTED_CODE),
            "fixture {i} contains the code"
        );
        assert!(
            serde_json::from_str::<Value>(text).is_ok(),
            "fixture {i} parses"
        );
    }
}

/// Proves: B-RUN-03 and B-RUN-04 (a run that reaches an unported step fails at that step with its message,
/// tears down with failed=true and a later Start is allowed).
#[test]
fn a_run_stops_at_the_first_unported_step_and_tears_down() {
    let mut sim = Sim::new(None);
    let cmds = sim.run.start("start", None, sim.clock()).expect("start");
    sim.exec(cmds);
    for step in [
        "model",
        "contributor",
        "descriptors",
        "observe",
        "compile",
        "slices",
    ] {
        sim.give(&format!("step:{step}"), &serde_json::json!({"ok": true}));
    }
    // Legacy hosts without work ports still refuse at publish.
    let owner_run::chain::StepError::Refused(e) = not_ported("publish") else {
        panic!("refusal expected")
    };
    let next = sim.run.step_finished(StepOutcome::Refused(e));
    assert_eq!(next, vec![Cmd::Teardown { failed: true }]);
    sim.exec(next);
    let snap = sim.run.snapshot();
    assert_eq!(snap.state, "failing");
    assert_eq!(snap.failed_step, Some("publish"));
    assert_eq!(snap.error_code.as_deref(), Some(NOT_PORTED_CODE));
    assert!(
        snap.error_message
            .as_deref()
            .is_some_and(|m| m.contains("NP-SLICE1-publish"))
    );
    let cmds = sim.run.teardown_finished(
        Ok(()),
        Clock {
            mono: 0.0,
            wall: 0.0,
        },
    );
    assert!(cmds.is_empty());
    assert_eq!(sim.run.snapshot().state, "failed");
    assert!(
        sim.run.start("start", None, sim.clock()).is_ok(),
        "a failed run does not block the next Start"
    );
}
