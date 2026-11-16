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

/// Owner ruling 16 Nov 2026 (REPORT-laneCAP option 2): v0.2.0 serves 2,356 tokens, v0.1.4's own
/// derivation (model_resources.py:72-76 @ f4e366e9) before it rounded up to 4,096. At 4,096 the
/// stage worker's own reservation exceeds a 4 GB memory consent for every registered model, so
/// every one of them gets it (the 15 Nov figure was 4,096 for all).
#[test]
fn every_registered_model_serves_the_ruled_context() {
    for model in api::model_catalog::registered() {
        assert_eq!(model.serving_context_tokens(), 2356, "{}", model.model_id);
    }
}

/// The ruled figure's own derivation, on this product's page limits: a prompt of at most
/// `ChatMaxPromptChars` (2,000) characters, v0.1.4's template allowance (under 100 tokens) and at
/// most `ChatMaxNewTokensCap` (256) new tokens. The pinned tokenizer is byte-level BPE (at most one
/// token per UTF-8 byte) and v0.2 encodes the page text without a template, so every ASCII prompt
/// the page accepts fits with the largest answer; only multi-byte text can need more than the
/// remaining 2,100 tokens, and the page refuses that by name (`chat_context_too_long`).
#[test]
fn a_full_ascii_page_prompt_and_the_largest_answer_fit_the_demo_models_context() {
    let count = |id: budgets::BudgetId| id.count().map(|n| n.get()).unwrap();
    let prompt_bytes = count(budgets::BudgetId::ChatMaxPromptChars);
    let new_tokens = count(budgets::BudgetId::ChatMaxNewTokensCap);
    let model = api::model_catalog::by_run_model_name("qwen3-0.6b").expect("the demo model");
    assert!(
        prompt_bytes + 100 + new_tokens <= model.serving_context_tokens(),
        "{prompt_bytes} + 100 + {new_tokens} > {}",
        model.serving_context_tokens()
    );
}
