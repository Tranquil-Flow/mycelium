// SPDX-License-Identifier: AGPL-3.0-only
use planner::{compile::assignment_id, routes::*};
use serde_json::{Value, json};
use stage_contract::{Architecture, NumericsProfile, StageDescriptor};
fn fixtures() -> Value {
    serde_json::from_str(include_str!("fixtures/routes.json")).unwrap()
}
fn costs(case: &Value) -> LegacyGraphCosts {
    let c = &case["expected"]["stages"][0]["stage_cost"];
    LegacyGraphCosts {
        prefill_work_units: c["prefill_work_units_per_prompt_token"].as_f64().unwrap(),
        decode_work_units: c["decode_work_units_per_token"].as_f64().unwrap(),
        kv_bytes_per_token: c["kv_bytes_per_context_token"].as_u64().unwrap(),
    }
}
#[test]
fn recorded_python_graph_signatures_proof_digests_and_mode_precedence_match() {
    let f = fixtures();
    for case in f["cases"].as_array().unwrap() {
        let graph = compile_graph(
            case["assignments"].as_array().unwrap(),
            case["proofs"].as_array().unwrap(),
            "iroh",
            "iroh",
            costs(case),
        )
        .unwrap();
        assert_eq!(graph.metadata(), &case["expected"]);
    }
    for case in f["modes"].as_array().unwrap() {
        let out = fast_mode(case["operator"].as_str(), case["app_default"].as_str());
        if let Some(error) = case["error"].as_str() {
            assert_eq!(out.unwrap_err().code, error);
        } else {
            assert_eq!(out.unwrap(), case["enabled"].as_bool().unwrap());
        }
    }
    assert!(require_fast_owner(true, false).is_err());
    assert!(require_fast_owner(false, false).is_ok());
    assert!(require_fast_owner(true, true).is_ok());
}
#[test]
fn graph_refuses_foreign_proof_noncontiguous_range_and_runtime_skew() {
    let f = fixtures();
    let case = &f["cases"][0];
    let assignments = case["assignments"].as_array().unwrap();
    let proofs = case["proofs"].as_array().unwrap();
    for field in [
        "assignment_id",
        "node_id",
        "deployment_id",
        "resolved_commit",
        "manifest_digest",
    ] {
        let mut bad = proofs.clone();
        bad[0][field] = json!("foreign");
        assert!(
            compile_graph(assignments, &bad, "iroh", "iroh", costs(case)).is_err(),
            "{field}"
        );
    }
    let mut bad = assignments.clone();
    bad[1]["range"]["start_layer"] = json!(0);
    bad[1]["assignment_id"] = json!(assignment_id(&bad[1]).unwrap());
    assert!(compile_graph(&bad, proofs, "iroh", "iroh", costs(case)).is_err());
    let mut bad = assignments.clone();
    bad[1]["runtime"]["dtype"] = json!("float16");
    bad[1]["assignment_id"] = json!(assignment_id(&bad[1]).unwrap());
    assert!(compile_graph(&bad, proofs, "iroh", "iroh", costs(case)).is_err());
    assert!(compile_graph(assignments, proofs, "iroh://foreign", "iroh", costs(case)).is_err());
}
#[test]
fn expanded_normal_fast_owner_admission_and_graph_identities_match_oracle() {
    let base = fixtures();
    let f: Value = serde_json::from_str(include_str!("fixtures/route-continuation.json")).unwrap();
    for case in f["activation"].as_array().unwrap() {
        let result = fast_mode(case["operator"].as_str(), case["app_default"].as_str()).and_then(
            |enabled| require_fast_owner(enabled, case["owner"] == "true").map(|()| enabled),
        );
        if let Some(error) = case["error"].as_str() {
            assert_eq!(result.unwrap_err().code, error, "{case}");
        } else {
            assert_eq!(
                result.unwrap(),
                case["enabled"].as_bool().unwrap(),
                "{case}"
            );
        }
    }
    for case in f["graphs"].as_array().unwrap() {
        let original = &base["cases"][case["base"].as_u64().unwrap() as usize];
        let graph = compile_graph(
            original["assignments"].as_array().unwrap(),
            case["proofs"].as_array().unwrap(),
            "iroh",
            "iroh",
            costs(case),
        )
        .unwrap();
        assert_eq!(graph.metadata(), &case["expected"]);
        assert!(protocol::execution_graph::parse(graph.metadata()).is_ok());
    }
}
#[test]
fn shared_l5_graph_schema_refuses_zero_hidden_geometry() {
    let f = fixtures();
    let c = &f["cases"][0];
    let mut assignments = c["assignments"].as_array().unwrap().clone();
    let mut proofs = c["proofs"].as_array().unwrap().clone();
    for (a, p) in assignments.iter_mut().zip(&mut proofs) {
        a["runtime"]["model_config"]["n_embd"] = json!(0);
        a["assignment_id"] = json!(assignment_id(a).unwrap());
        p["assignment_id"] = a["assignment_id"].clone();
        p["runtime"] = a["runtime"].clone();
    }
    assert!(compile_graph(&assignments, &proofs, "iroh", "iroh", costs(c)).is_err());
}
fn template() -> StageDescriptor {
    StageDescriptor {
        checkpoint: [1; 32],
        revision: "a".repeat(40),
        tensor_manifest: [2; 32],
        semantics: "qwen2-v014-explicit-v1".into(),
        architecture: Architecture::Qwen2,
        layers: 0..3,
        embedding: true,
        head: true,
        representation: "persistent_packed_int8".into(),
        quantizer: "symmetric-rowwise-int8-half-away-v1".into(),
        profile: NumericsProfile::MlxPackedInt8,
        state_schema: "attention-kv-f32-v1".into(),
    }
}
#[test]
fn l8_descriptor_binds_exact_assignment_without_profile_promotion() {
    let f = fixtures();
    let a = &f["cases"][0]["assignments"][0];
    let t = template();
    let d = bind_stage(&t, a).unwrap();
    assert_eq!(d.layers, 0..1);
    assert!(d.embedding);
    assert!(!d.head);
    assert_eq!(d.checkpoint, t.checkpoint);
    assert_eq!(d, bind_stage(&t, a).unwrap());
    let mut bad = t.clone();
    bad.revision = "b".repeat(40);
    assert!(bind_stage(&bad, a).is_err());
    bad = t.clone();
    bad.profile = NumericsProfile::MlxExplicitF32;
    assert!(bind_stage(&bad, a).is_err());
}
