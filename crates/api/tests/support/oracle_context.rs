// SPDX-License-Identifier: AGPL-3.0-only
//! The oracle's serving context and the product's (owner ruling 16 Nov 2026,
//! `contracts/divergences.json`). Every v0.1.4 fixture that carries a consent document was recorded
//! at the oracle's `DEMO_CONTEXT_TOKENS` (model_resources.py:76 @ f4e366e9, 4,096); the product
//! computes the whole-model member figures at its model contract's serving context
//! (`model_catalog::Model::serving_context_tokens`, 2,356). Parity is checked at 4,096, passed
//! explicitly; the product's own document is what the product is compared with. The contract's
//! value never reaches a fixture comparison.
#![allow(dead_code)]
use api::model_catalog;
use serde_json::Value;

/// `model_resources.DEMO_CONTEXT_TOKENS` at f4e366e9: the context the fixtures were recorded at.
pub const ORACLE_CONTEXT_TOKENS: u64 = 4096;

/// Every consent document inside an oracle result (`resources`): checked equal to the product's
/// document computed at [`ORACLE_CONTEXT_TOKENS`], then replaced by the product's document at the
/// contract's serving context, which is what the product returns. Everything else in the result
/// is compared as recorded.
pub fn at_product_context(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, inner) in map.iter_mut() {
                if key == "resources" && inner.get("storage_consent_ceiling_bytes").is_some() {
                    let selected = inner["selected"]["model_id"]
                        .as_str()
                        .zip(inner["selected"]["resolved_commit"].as_str())
                        .map(|(m, c)| (m.to_owned(), c.to_owned()));
                    let selected = selected.as_ref().map(|(m, c)| (m.as_str(), c.as_str()));
                    let memory = inner["device_memory_bytes"].as_u64();
                    assert_eq!(
                        *inner,
                        model_catalog::consent_document_at(selected, memory, ORACLE_CONTEXT_TOKENS),
                        "the consent document at the oracle's context equals the oracle's"
                    );
                    *inner = model_catalog::consent_document(selected, memory);
                } else {
                    at_product_context(inner);
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(at_product_context),
        _ => {}
    }
}

/// The released app's response bytes (compact `json.dumps`) with the one consent-document figure
/// that depends on the serving context, each model's `whole_model_member_rss_bytes`, moved from
/// its value at [`ORACLE_CONTEXT_TOKENS`] to the product's at the contract's context. Every such
/// field in the bytes must carry a registered model's figure at 4,096 (this pins the oracle side);
/// every other byte stays as recorded.
pub fn at_product_context_bytes(oracle_bytes: &str) -> String {
    const FIELD: &str = "\"whole_model_member_rss_bytes\":";
    let rss = |member: Option<Value>| member.unwrap()["max_rss_bytes"].as_u64().unwrap();
    let pairs: Vec<(u64, u64)> = model_catalog::registered()
        .iter()
        .map(|m| {
            (
                rss(model_catalog::whole_model_member_at(m.model_id, ORACLE_CONTEXT_TOKENS)),
                rss(model_catalog::whole_model_member(m.model_id)),
            )
        })
        .collect();
    let mut out = String::with_capacity(oracle_bytes.len());
    let mut rest = oracle_bytes;
    while let Some(at) = rest.find(FIELD) {
        let (head, tail) = rest.split_at(at + FIELD.len());
        out.push_str(head);
        let digits = tail.bytes().take_while(u8::is_ascii_digit).count();
        let recorded: u64 = tail[..digits].parse().expect("an integer figure");
        let (_, product) = pairs
            .iter()
            .find(|(oracle, _)| *oracle == recorded)
            .unwrap_or_else(|| panic!("{recorded} is no registered model's figure at 4,096"));
        out.push_str(&product.to_string());
        rest = &tail[digits..];
    }
    out.push_str(rest);
    out
}
