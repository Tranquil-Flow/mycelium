// SPDX-License-Identifier: AGPL-3.0-only
//! Dense Qwen header accounting, never numerical or route qualification.
use crate::{
    Error, Result, add,
    placement::{LayerProfile, valid_layer_count},
};
use serde_json::Value;
use std::{collections::BTreeSet, io::Read};

/// Read only the safetensors header from an already-owned file/reader. Payload
/// integrity remains the artifact verifier's responsibility. No path is reopened.
pub fn read_header(reader: &mut impl Read) -> Result<Value> {
    let bad = || Error::new("model_header_invalid");
    let mut length = [0; 8]; // Safetensors wire length, not an operational bound.
    reader.read_exact(&mut length).map_err(|_| bad())?;
    let length = u64::from_le_bytes(length);
    let maximum = budgets::BudgetId::PyMyceliumA13MacosLayerSplitMaxHeaderBytesL47
        .bytes()
        .map_err(|_| Error::new("model_header_policy_invalid"))?
        .get();
    if length < b"{}".len() as u64 || length > maximum {
        return Err(bad());
    }
    let mut bytes = Vec::new();
    let size = usize::try_from(length).map_err(|_| bad())?;
    bytes.try_reserve_exact(size).map_err(|_| bad())?;
    bytes.resize(size, 0);
    reader.read_exact(&mut bytes).map_err(|_| bad())?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| bad())?;
    if !value.is_object() {
        return Err(bad());
    }
    Ok(value)
}

/// Port of layer_split.model_layer_profile after header acquisition. Qwen3 MoE
/// and Qwen3.5 are refused here exactly as in the released dense A13 compiler,
/// regardless of whether another backend has a corresponding numerical kernel.
pub fn from_headers(config: &Value, headers: &[Value]) -> Result<LayerProfile> {
    let family = match config["model_type"].as_str() {
        Some("qwen2") => "qwen2",
        Some("qwen3") => "qwen3",
        _ => return Err(Error::new("model_architecture_unsupported")),
    };
    let bad_config = || Error::new("model_config_invalid");
    let layers = config["num_hidden_layers"]
        .as_u64()
        .and_then(|v| usize::try_from(v).ok())
        .filter(|v| valid_layer_count(*v))
        .ok_or_else(bad_config)?;
    let tied = config
        .get("tie_word_embeddings")
        .map_or(Some(false), Value::as_bool)
        .ok_or_else(bad_config)?;
    let bad = || Error::new("model_header_invalid");
    let mut profile = LayerProfile {
        model_type: family,
        layer_bytes: vec![0; layers],
        embedding_bytes: 0,
        output_bytes: 0,
        tie_word_embeddings: tied,
    };
    let mut seen = BTreeSet::new();
    for header in headers {
        for (name, record) in header.as_object().ok_or_else(bad)? {
            if name == "__metadata__" {
                continue;
            }
            let offsets = record["data_offsets"]
                .as_array()
                .filter(|v| v.len() == 2)
                .ok_or_else(bad)?;
            let start = offsets[0].as_u64().ok_or_else(bad)?;
            let end = offsets[1].as_u64().ok_or_else(bad)?;
            let size = end.checked_sub(start).ok_or_else(bad)?;
            if !seen.insert(name) {
                return Err(bad());
            }
            if let Some((layer, _)) = name
                .strip_prefix("model.layers.")
                .and_then(|v| v.split_once('.'))
                .filter(|(n, _)| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
            {
                let index = layer
                    .parse::<usize>()
                    .map_err(|_| Error::new("model_layer_out_of_range"))?;
                let count = profile
                    .layer_bytes
                    .get_mut(index)
                    .ok_or_else(|| Error::new("model_layer_out_of_range"))?;
                *count = add(*count, size)?;
            } else if name.starts_with("model.embed_tokens.") {
                profile.embedding_bytes = add(profile.embedding_bytes, size)?;
            } else if name.starts_with("model.norm.") || name.starts_with("lm_head.") {
                profile.output_bytes = add(profile.output_bytes, size)?;
            } else {
                return Err(Error::new("model_tensor_unsupported"));
            }
        }
    }
    if profile.layer_bytes.contains(&0) || profile.embedding_bytes == 0 {
        return Err(bad());
    }
    Ok(profile)
}
