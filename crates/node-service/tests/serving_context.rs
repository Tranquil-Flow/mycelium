// SPDX-License-Identifier: AGPL-3.0-only
//! Lane CAP: the serving context has one source, the model contract
//! (`api::model_catalog::Model::serving_context_tokens`). The headless `mycelium-node` path
//! (`native.rs`, Qwen2.5-0.5B, not the packaged route) still carries its own planning document
//! with a `context_tokens` figure (`data/qwen2-planning.json`, the v0.1.4 planning input it was
//! ported from); this pins that figure to the contract, so the two cannot drift apart unnoticed.
use serde_json::Value;

#[test]
fn the_headless_planning_document_states_the_contracts_serving_context() {
    let planning: Value =
        serde_json::from_str(include_str!("../data/qwen2-planning.json")).expect("valid JSON");
    let model = api::model_catalog::find(
        planning["model"]["model_id"].as_str().expect("model id"),
        planning["model"]["resolved_commit"]
            .as_str()
            .expect("resolved commit"),
    )
    .expect("the headless model is a registered contract");
    assert_eq!(
        planning["resources"]["context_tokens"].as_u64(),
        Some(model.serving_context_tokens()),
        "qwen2-planning.json and the model contract name different serving contexts"
    );
}

#[test]
fn every_registered_model_serves_the_ruled_context() {
    // Owner ruling 15 Nov: 4,096 tokens for every registered model today.
    for model in api::model_catalog::registered() {
        assert_eq!(model.serving_context_tokens(), 4096, "{}", model.model_id);
    }
}
