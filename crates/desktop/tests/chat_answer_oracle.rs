// SPDX-License-Identifier: AGPL-3.0-only
//! Fast tests of the packaged chat gate's oracle-equal answer check (no GUI, no model). The
//! expected values are the executed v0.1.4 output in `fixtures/chat-answer-oracle.json`.
#[path = "support/chat_answer.rs"]
mod chat_answer;
use serde_json::{Value, json};

fn oracle() -> Value {
    serde_json::from_str(chat_answer::ORACLE).unwrap()
}
fn result_of(n: usize) -> Value {
    let o = oracle();
    let ids: Vec<Value> = o["token_ids"].as_array().unwrap()[..n].to_vec();
    let text: String = o["pieces"].as_array().unwrap()[..n]
        .iter()
        .map(|p| p.as_str().unwrap())
        .collect();
    json!({"token_ids": ids, "text": text})
}

#[test]
fn the_oracle_fixture_is_the_executed_v014_answer_for_the_gate_question() {
    let o = oracle();
    assert_eq!(
        o["oracle"].as_str().unwrap().split(' ').nth(1),
        Some("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert_eq!(o["max_new_tokens"], 64);
    assert_eq!(o["token_ids"].as_array().unwrap().len(), 64);
    assert_eq!(o["pieces"][0], "Rain");
    assert!(
        o["text"]
            .as_str()
            .unwrap()
            .starts_with("Rain is formed when water vapor")
    );
}

#[test]
fn round_9_whitespace_answer_is_refused() {
    // 271 is "\n\n" in the Qwen2 vocabulary: exactly what round 9 recorded as the answer.
    let seen = json!({"token_ids": [271], "text": "\n\n"});
    assert!(
        chat_answer::check(&seen)
            .unwrap_err()
            .contains("whitespace-only")
    );
    let whitespace_with_oracle_ids =
        json!({"token_ids": oracle()["token_ids"].as_array().unwrap()[..1], "text": "\n\n"});
    assert!(chat_answer::check(&whitespace_with_oracle_ids).is_err());
}

#[test]
fn an_oracle_prefix_of_any_length_passes() {
    for n in [1, 2, 3, 16, 17, 40, 64] {
        assert!(chat_answer::check(&result_of(n)).is_ok(), "{n}");
    }
}

#[test]
fn a_wrong_token_or_text_inside_the_horizon_is_refused() {
    let mut wrong = result_of(5);
    wrong["token_ids"][3] = json!(1);
    assert!(chat_answer::check(&wrong).is_err());
    let mut text = result_of(5);
    text["text"] = json!("Rain is formed whenX");
    assert!(chat_answer::check(&text).is_err());
    let empty = json!({"token_ids": [], "text": ""});
    assert!(chat_answer::check(&empty).is_err());
    let mut too_long = result_of(64);
    too_long["token_ids"].as_array_mut().unwrap().push(json!(1));
    assert!(chat_answer::check(&too_long).is_err());
}

#[test]
fn only_the_documented_horizon_tolerates_numeric_divergence() {
    let mut late = result_of(30);
    late["token_ids"][20] = json!(1); // beyond index 16: bf16-torch vs MLX noise region
    assert!(chat_answer::check(&late).is_ok());
    let mut early = result_of(30);
    early["token_ids"][15] = json!(1); // inside the horizon
    assert!(chat_answer::check(&early).is_err());
}
