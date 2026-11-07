// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::boundaries::validate_leave;
use serde_json::{Value, json};

fn leave(mode: &str) -> Value {
    let mut names = vec!["fence"];
    if mode == "smooth" {
        names.extend(["unload", "publish_withdrawal"]);
    }
    names.extend(["withdraw_consent", "disconnect", "cancel_work"]);
    json!({"state":"left","mode":mode,"fallback":null,"handoff":"unavailable",
        "steps": names.into_iter().map(|name| json!({"name":name,"ok":true,"code":null})).collect::<Vec<_>>()})
}

#[test]
fn leave_requires_every_real_step_not_just_left_label() {
    for mode in ["instant", "smooth"] {
        let original = leave(mode);
        assert!(validate_leave(&original, mode).is_ok());
        for index in 0..original["steps"].as_array().unwrap().len() {
            let mut failed = original.clone();
            failed["steps"][index]["ok"] = json!(false);
            assert!(validate_leave(&failed, mode).is_err());
            let mut missing = original.clone();
            missing["steps"].as_array_mut().unwrap().remove(index);
            assert!(validate_leave(&missing, mode).is_err());
        }
        for key in ["state", "mode", "fallback", "handoff"] {
            let mut bad = original.clone();
            bad[key] = json!("unexpected");
            assert!(validate_leave(&bad, mode).is_err());
        }
        let mut extra = original.clone();
        extra["steps"]
            .as_array_mut()
            .unwrap()
            .push(json!({"name":"invented","ok":true,"code":null}));
        assert!(validate_leave(&extra, mode).is_err());
    }
}

#[test]
fn boundary_cli_selects_explicit_scenario_before_target_read() {
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_journey-tests"))
        .args([
            "--target",
            "/nonexistent-l3b-manifest",
            "--receipt",
            "/nonexistent-l3b-receipt",
            "--golden",
            "/nonexistent-l3b-golden",
            "--scenario",
            "loopback-boundaries",
        ])
        .output()
        .unwrap();
    assert!(!result.status.success());
    assert!(
        String::from_utf8(result.stderr)
            .unwrap()
            .contains("json:read:io:NotFound")
    );
}
