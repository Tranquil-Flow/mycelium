// SPDX-License-Identifier: AGPL-3.0-only
use sim::{
    model::{self, Command, Mode},
    trace::Trace,
};

#[test]
fn seven_persisted_incidents_replay_exactly_and_corrected_controls_progress() {
    let bundle: latency_profiles::Bundle =
        serde_json::from_str(include_str!("../../profiles/data/v014-v1.json")).unwrap();
    for raw in [
        include_str!("../traces/F1-v2.json"),
        include_str!("../traces/F2-v2.json"),
        include_str!("../traces/F3-v2.json"),
        include_str!("../traces/F5-v2.json"),
        include_str!("../traces/F13-v2.json"),
        include_str!("../traces/F14-v2.json"),
        include_str!("../traces/F15b-v2.json"),
    ] {
        let mut t: Trace = serde_json::from_str(raw).unwrap();
        assert_eq!(t.config.mode, Mode::Historical);
        assert_eq!(t.commands.len(), 1);
        assert_eq!(t.commands[0].command, Command::Begin);
        assert_eq!(t.profile_id, "section7-policy-model-v1-NOT-MEASURED");
        t.validate_profile(&bundle).unwrap();
        t.seed = u64::MAX; // concrete inputs, not the current generator
        let outcome = t.replay().unwrap();
        assert_eq!(outcome, t.expected_outcome);
        assert_eq!(model::violations(&t.config, &outcome), t.expected_failure);
        let corrected = t.corrected();
        let positive = corrected.replay().unwrap();
        assert_eq!(
            positive,
            corrected.replay().unwrap(),
            "corrected event order drift"
        );
        assert!(model::violations(&corrected.config, &positive).is_empty());
        assert!(positive.successful_chats > 0);
        for action in t.config.incident.required_transitions() {
            assert!(positive.transitions.contains_key(*action));
        }
        t.profile_id = "invented-measured-p99".into();
        assert_eq!(
            t.validate_profile(&bundle).unwrap_err(),
            "model_profile_mismatch"
        );
    }
}
