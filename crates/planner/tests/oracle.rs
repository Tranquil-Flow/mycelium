// SPDX-License-Identifier: AGPL-3.0-only
use serde_json::Value;
// O:incident:F14; O:incident:F15; O:incident:F15b. Component, not HTTP proof.
#[test]
fn registered_staging_allowances_cannot_be_lowered_by_policy_inputs() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let c = &f["cases"][0];
    let model = f["models"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["model_id"] == c["model_id"])
        .unwrap();
    for key in [
        "control_archive_bytes",
        "staged_copy_factor",
        "slice_header_bytes",
    ] {
        let mut r = f["resources"].clone();
        r[key] = serde_json::json!(0);
        assert_eq!(
            planner::plan(
                model,
                &f["policies"][c["quantization"].as_str().unwrap()],
                &r,
                c
            )
            .unwrap_err()
            .code,
            "planner_registered_policy_mismatch"
        );
    }
}
#[test]
fn pinned_python_placement_and_resource_corpus() {
    let mut f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let ui: Value = serde_json::from_str(include_str!("fixtures/ui-planning.json")).unwrap();
    f["cases"]
        .as_array_mut()
        .unwrap()
        .extend(ui["cases"].as_array().unwrap().iter().cloned());
    for (i, case) in f["cases"].as_array().unwrap().iter().enumerate() {
        let model = f["models"]
            .as_array()
            .unwrap()
            .iter()
            .find(|m| m["model_id"] == case["model_id"])
            .unwrap();
        let result = planner::plan(
            model,
            &f["policies"][case["quantization"].as_str().unwrap()],
            &f["resources"],
            case,
        );
        if let Some(expected) = case.get("expected") {
            assert_eq!(
                &result.unwrap_or_else(|e| panic!("case {i}: {e:?}")),
                expected,
                "case {i}"
            );
        } else {
            let error = result.unwrap_err();
            assert_eq!(
                error.code,
                case["error"]["code"].as_str().unwrap(),
                "case {i}"
            );
            assert_eq!(
                error.node_id.as_deref(),
                case["error"]["node_id"].as_str(),
                "case {i}"
            );
        }
    }
}
