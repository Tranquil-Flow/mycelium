// SPDX-License-Identifier: AGPL-3.0-only
//! Tokenizer + chat-template identity against the cached Qwen3.6 snapshot (removable drive, read only,
//! 13 MB). Ignored by default because the snapshot is not always mounted; run explicitly with
//! `MYCELIUM_MOE_CHECKPOINT=<snapshot dir> cargo test -p model --features tokenizer -- --ignored`.
#![cfg(feature = "tokenizer")]
use model::{
    chat::{Message, Role},
    tokenizer::Qwen36Tokenizer,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::PathBuf;

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/qwen36-tokenizer-identity.json")).unwrap()
}
#[test]
#[ignore = "needs the cached snapshot on the removable drive"]
fn snapshot_files_and_every_recorded_id_sequence_are_identical() {
    let root = PathBuf::from(
        std::env::var("MYCELIUM_MOE_CHECKPOINT").expect("cached snapshot; never download"),
    );
    let fx = fixture();
    assert_eq!(root.file_name().unwrap().to_str().unwrap(), fx["snapshot"]);
    for (name, info) in fx["files"].as_object().unwrap() {
        let bytes = std::fs::read(root.join(name)).unwrap();
        assert_eq!(info["bytes"], bytes.len(), "{name}");
        assert_eq!(
            info["sha256"],
            format!("{:x}", Sha256::digest(&bytes)),
            "{name}"
        );
    }
    let tok = Qwen36Tokenizer::from_file(&root.join("tokenizer.json")).unwrap();
    assert_eq!(fx["vocab_size_with_added"], tok.vocab_size_with_added());
    for (token, id) in fx["specials"].as_object().unwrap() {
        assert_eq!(
            Some(id.as_u64().unwrap() as u32),
            tok.token_to_id(token),
            "{token}"
        );
    }
    let mut checks = 0;
    for (name, case) in fx["plain"].as_object().unwrap() {
        let want: Vec<u32> = serde_json::from_value(case["ids"].clone()).unwrap();
        assert_eq!(
            tok.encode(case["text"].as_str().unwrap()).unwrap(),
            want,
            "plain {name}"
        );
        checks += 1;
    }
    for (name, case) in fx["cases"].as_object().unwrap() {
        let messages: Vec<Message> = case["messages"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| Message {
                role: match m["role"].as_str().unwrap() {
                    "system" => Role::System,
                    "user" => Role::User,
                    _ => Role::Assistant,
                },
                content: m["content"].as_str().unwrap(),
            })
            .collect();
        for (thinking, key) in [(true, "thinking"), (false, "no_thinking")] {
            let prompt = model::chat::render(&messages, thinking).unwrap();
            let want: Vec<u32> = serde_json::from_value(case[key]["ids"].clone()).unwrap();
            assert_eq!(tok.encode(&prompt).unwrap(), want, "{name} {key}");
            checks += 1;
        }
        let v014: Vec<u32> = serde_json::from_value(case["v014_ids"].clone()).unwrap();
        assert_eq!(
            tok.encode(case["v014_build_qwen_chat_prompt"].as_str().unwrap())
                .unwrap(),
            v014,
            "{name} v0.1.4 prompt"
        );
        checks += 1;
    }
    assert!(checks >= 11 + 9 * 3);
}
