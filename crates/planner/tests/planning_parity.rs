// SPDX-License-Identifier: AGPL-3.0-only
use planner::placement::{LayerProfile, SplitPolicy};
use serde_json::Value;

fn verdict(case: &Value, actual: planner::Result<Value>) {
    if let Some(expected) = case.get("expected") {
        assert_eq!(
            &actual.unwrap_or_else(|e| panic!("{}: {e}", case["source_test"])),
            expected,
            "{}",
            case["source_test"]
        );
    } else {
        let e = actual.unwrap_err();
        assert_eq!(e.code, case["error"]["code"], "{}", case["source_test"]);
        assert_eq!(
            e.node_id.as_deref(),
            case["error"]["node_id"].as_str(),
            "{}",
            case["source_test"]
        );
    }
}
#[test]
fn original_a13_planner_tests_recorded_calls_match() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning-round4.json")).unwrap();
    for c in f["split_cases"].as_array().unwrap() {
        let members = match planner::placement::members_from_document(&c["members"]) {
            Ok(m) => m,
            Err(e) => {
                verdict(c, Err(e));
                continue;
            }
        };
        let p = &c["profile"];
        let profile = LayerProfile {
            model_type: match p["model_type"].as_str().unwrap() {
                "qwen2" => "qwen2",
                "qwen3" => "qwen3",
                _ => panic!("unexpected oracle family"),
            },
            layer_bytes: serde_json::from_value(p["layer_bytes"].clone()).unwrap(),
            embedding_bytes: p["embedding_bytes"].as_u64().unwrap(),
            output_bytes: p["output_bytes"].as_u64().unwrap(),
            tie_word_embeddings: p["tie_word_embeddings"].as_bool().unwrap(),
        };
        let policy: SplitPolicy = serde_json::from_value(c["policy"].clone()).unwrap();
        verdict(
            c,
            planner::placement::split(&members, &profile, &policy, |stage, member| {
                if let Some(caps) = c["stage_caps"].as_array() {
                    let index = members
                        .iter()
                        .position(|m| m.node_id == member.node_id)
                        .unwrap();
                    if stage.stage_bytes > caps[index].as_u64().unwrap() {
                        return Err(planner::Error::node(
                            "member_storage_cap_below_share",
                            &member.node_id,
                        ));
                    }
                }
                Ok(())
            }),
        );
    }
}
#[test]
fn source_recorded_compiled_resource_rows_match_whole_and_partitioned_models() {
    use planner::placement::{ResourcePolicy, member_resources, registered_model};
    let f: Value = serde_json::from_str(include_str!("fixtures/planning-round4.json")).unwrap();
    let base: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let policy: ResourcePolicy = serde_json::from_value(base["resources"].clone()).unwrap();
    let cases = f["resource_row_cases"].as_array().unwrap();
    assert!(!cases.is_empty());
    for c in cases {
        let actual = registered_model(
            c["model_id"].as_str().unwrap(),
            c["resolved_commit"].as_str().unwrap(),
        )
        .and_then(|m| {
            member_resources(
                &m,
                &policy,
                &c["split"],
                c["owner_node_id"].as_str(),
                c["slice_mode"].as_bool().unwrap(),
                c["quantization"].as_str().unwrap(),
            )
        });
        verdict(c, actual);
    }
}
#[test]
fn pinned_shape_and_measured_floors_cannot_be_replaced_by_input_descriptors() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    for (field, value) in [("group_peak_bytes", 1), ("member_disk_bytes", 1)] {
        let mut model = f["models"][0].clone();
        model["anchor"][field] = serde_json::json!(value);
        assert_eq!(
            planner::plan(
                &model,
                &f["policies"]["int8-weight-only"],
                &f["resources"],
                &f["cases"][0]
            )
            .unwrap_err()
            .code,
            "model_registry_mismatch"
        );
    }
    let mut model = f["models"][0].clone();
    model["shape"]["hidden_size"] = serde_json::json!(1);
    assert_eq!(
        planner::plan(
            &model,
            &f["policies"]["int8-weight-only"],
            &f["resources"],
            &f["cases"][0]
        )
        .unwrap_err()
        .code,
        "model_registry_mismatch"
    );
}
#[test]
fn unsupported_models_and_revisions_cannot_borrow_dense_resource_costs() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    for name in [
        "Qwen/Qwen3-30B-A3B",
        "Qwen/Qwen3.5-35B-A3B",
        "unknown/model",
    ] {
        let mut model = f["models"][0].clone();
        let mut request = f["cases"][0].clone();
        model["model_id"] = serde_json::json!(name);
        request["model_id"] = serde_json::json!(name);
        assert_eq!(
            planner::plan(
                &model,
                &f["policies"]["int8-weight-only"],
                &f["resources"],
                &request
            )
            .unwrap_err()
            .code,
            "model_resources_unregistered"
        );
    }
    let mut model = f["models"][0].clone();
    model["resolved_commit"] = serde_json::json!("0000000000000000000000000000000000000000");
    assert_eq!(
        planner::plan(
            &model,
            &f["policies"]["int8-weight-only"],
            &f["resources"],
            &f["cases"][0]
        )
        .unwrap_err()
        .code,
        "model_resources_unregistered"
    );
}

#[test]
fn original_a13_resource_planning_calls_match() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning-round4.json")).unwrap();
    let base: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    for c in f["planning_cases"].as_array().unwrap() {
        let m = base["models"]
            .as_array()
            .unwrap()
            .iter()
            .find(|m| {
                m["model_id"] == c["model_id"] && m["resolved_commit"] == c["resolved_commit"]
            })
            .unwrap();
        verdict(
            c,
            planner::plan(
                m,
                &base["policies"][c["quantization"].as_str().unwrap()],
                &base["resources"],
                c,
            ),
        );
    }
}
