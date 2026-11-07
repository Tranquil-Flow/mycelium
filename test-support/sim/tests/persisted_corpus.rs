// SPDX-License-Identifier: AGPL-3.0-only
use sim::{
    model::{self, Command, Mode},
    trace::Trace,
};
#[test]
fn persisted_minimal_incident_corpus_replays_after_generator_changes() {
    let bundle: latency_profiles::Bundle =
        serde_json::from_str(include_str!("../../profiles/data/v014-v1.json")).unwrap();
    let (registry, _) = obligations::checked_in().unwrap();
    for data in [
        include_str!("../traces/F4-v2.json"),
        include_str!("../traces/F15-v2.json"),
        include_str!("../traces/F16-v2.json"),
    ] {
        let mut t: Trace = serde_json::from_str(data).unwrap();
        assert_eq!(t.config.mode, Mode::Historical);
        assert_eq!(t.commands.len(), 1);
        assert_eq!(t.commands[0].command, Command::Begin);
        assert!(
            registry
                .escapes
                .iter()
                .any(|e| e.id == format!("{:?}", t.config.incident))
        );
        t.validate_profile(&bundle).unwrap();
        let o = t.replay().unwrap();
        assert_eq!(o, t.expected_outcome);
        assert_eq!(model::violations(&t.config, &o), t.expected_failure);
        t.seed = u64::MAX;
        assert_eq!(t.replay().unwrap(), o);
        let green = t.corrected();
        assert!(model::violations(&green.config, &green.replay().unwrap()).is_empty());
        let mut changed = bundle.clone();
        changed
            .profiles
            .iter_mut()
            .find(|p| p.id == t.profile_id)
            .unwrap()
            .samples[0]
            .value = 1.0;
        assert_eq!(
            t.validate_profile(&changed).unwrap_err(),
            "profile_budget_mismatch"
        );
    }
}
