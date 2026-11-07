// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::{cli::*, stranger};
use serde_json::json;
fn args(s: &[&str]) -> Vec<String> {
    s.iter().map(|s| s.to_string()).collect()
}
#[test]
fn parser_has_explicit_owned_stop_and_no_live_launch_or_secret_argv() {
    assert_eq!(parse(&args(&["--help"])).unwrap(), Action::Help);
    assert_eq!(
        parse(&args(&["evaluate", "a.json"])).unwrap(),
        Action::Evaluate("a.json".into())
    );
    assert_eq!(
        parse(&args(&["stop", "--attempt", "a", "--budgets", "b.json"])).unwrap(),
        Action::Stop {
            attempt: "a".into(),
            budgets: "b.json".into()
        }
    );
    for v in [
        vec!["stranger"],
        vec!["soak"],
        vec!["stop", "--pid", "123"],
        vec!["stop", "--attempt", "a"],
        vec!["join", "--token", "PRIVATE"],
        vec!["evaluate", "a", "extra"],
    ] {
        assert!(parse(&args(&v)).is_err());
    }
}
#[test]
fn strict_evaluator_refuses_unknown_schema_and_never_promotes_fixture_evidence() {
    let candidate = stranger::Candidate {
        version: "0.2.0".into(),
        source_sha: "a".repeat(40),
        zip_sha256: "b".repeat(64),
        exe_sha256: "c".repeat(64),
    };
    let mut input = json!({"scenario":"stranger","schema_version":1,"candidate":candidate,"evidence":stranger::Evidence::default()});
    let v = evaluate(&serde_json::to_vec(&input).unwrap()).unwrap();
    assert_eq!(v["scenario_passed"], false);
    assert_eq!(v["release_qualified"], false);
    assert_eq!(v["evidence_boundary"], "offline_replay_not_physical");
    input["schema_version"] = json!(2);
    assert!(evaluate(&serde_json::to_vec(&input).unwrap()).is_err());
    input["schema_version"] = json!(1);
    input["extra"] = json!(true);
    assert!(evaluate(&serde_json::to_vec(&input).unwrap()).is_err());
    assert!(evaluate(b"not json PRIVATE").is_err());
}
#[test]
fn candidate_identity_rejects_dot_only_or_empty_components_not_just_slashes() {
    let c = stranger::Candidate {
        version: "0.2.0-dev".into(),
        source_sha: "a".repeat(40),
        zip_sha256: "b".repeat(64),
        exe_sha256: "c".repeat(64),
    };
    assert!(c.validate().is_ok());
    for version in ["..", "...", "0..2", "0.2.", "../0.2.0", "0.2.0-", "a.b.c"] {
        assert!(
            stranger::Candidate {
                version: version.into(),
                ..c.clone()
            }
            .validate()
            .is_err(),
            "{version}"
        );
    }
}
