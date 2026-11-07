// SPDX-License-Identifier: AGPL-3.0-only
//! The token stream the chat shows: v0.1.4's `IncrementalTokenDecoder` semantics (UTF-8
//! incremental decode, `errors=replace`, flush on finish, silent stop ids, special tokens
//! spelled literally). Expected values: executed oracle output, FX-L11-detok
//! (`fixtures/qwen2-detok-oracle.json`, probe sha256 inside).
use node_service::detok::{Detokenizer, byte_level_bytes};
use serde_json::Value;

const FIXTURE: &str = include_str!("fixtures/qwen2-detok-oracle.json");

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
fn fixture() -> Value {
    serde_json::from_str(FIXTURE).unwrap()
}

#[test]
fn every_oracle_stream_renders_the_same_pieces() {
    let f = fixture();
    let stop: Vec<u32> = serde_json::from_value(f["stop_token_ids"].clone()).unwrap();
    for case in f["cases"].as_array().unwrap() {
        let mut d = Detokenizer::new(&stop);
        let mut pieces = Vec::new();
        for id in case["ids"].as_array().unwrap() {
            let id = u32::try_from(id.as_u64().unwrap()).unwrap();
            let bytes = f["token_bytes_hex"][id.to_string()]
                .as_str()
                .map(hex)
                .unwrap_or_default();
            pieces.push(d.push(id, &bytes));
        }
        pieces.push(d.finish());
        let want: Vec<String> = serde_json::from_value(case["pieces"].clone()).unwrap();
        assert_eq!(pieces, want, "{}", case["name"]);
        assert_eq!(pieces.concat(), case["text"].as_str().unwrap());
    }
}

#[test]
fn the_cumulative_decode_that_failed_requests_is_not_the_contract() {
    // A token boundary inside one character: the whole-list decode shows U+FFFD and then the
    // character, which does not extend the earlier text (round 9 `nonappend_token_decode`).
    let f = fixture();
    let case = f["cases"][0].clone();
    assert_eq!(case["name"], "日本語 🌧 Grüße");
    let pieces: Vec<String> = serde_json::from_value(case["pieces"].clone()).unwrap();
    assert!(
        pieces.contains(&String::new()),
        "pending bytes emit nothing yet"
    );
    assert!(
        pieces.contains(&"🌧".to_owned()),
        "and the character appears whole"
    );
}

#[test]
fn byte_level_tokens_map_to_their_bytes_and_unknown_symbols_stay_literal() {
    assert_eq!(byte_level_bytes("Ġthe"), b" the");
    assert_eq!(byte_level_bytes("Ċ"), b"\n");
    assert_eq!(byte_level_bytes("ĊĊ"), b"\n\n");
    assert_eq!(byte_level_bytes("Ã©"), "é".as_bytes());
    // Not byte-level symbols: v0.1.4 falls back to the token's own UTF-8 (`KeyError` branch).
    assert_eq!(byte_level_bytes("<|im_start|>"), b"<|im_start|>");
}

/// The mapping agrees with v0.1.4's reader on every id of the fixture (needs the pinned
/// tokenizer.json: `MYCELIUM_MODEL_SNAPSHOT=<snapshot dir> --features metal ... -- --ignored`).
/// `tokenizers` is an optional dependency enabled only by `metal`, so this one test is
/// compiled only there; the fixture-driven tests above run in every feature set.
#[cfg(feature = "metal")]
#[test]
#[ignore = "needs the pinned Qwen2.5-0.5B-Instruct tokenizer.json (MYCELIUM_MODEL_SNAPSHOT)"]
fn byte_mapping_equals_the_v014_reader_on_the_real_vocabulary() {
    let snapshot = std::path::PathBuf::from(std::env::var("MYCELIUM_MODEL_SNAPSHOT").unwrap());
    let codec = tokenizers::Tokenizer::from_file(snapshot.join("tokenizer.json")).unwrap();
    let f = fixture();
    for (id, want) in f["token_bytes_hex"].as_object().unwrap() {
        let token = codec.id_to_token(id.parse().unwrap()).unwrap();
        assert_eq!(
            byte_level_bytes(&token),
            hex(want.as_str().unwrap()),
            "id {id} {token:?}"
        );
    }
}
