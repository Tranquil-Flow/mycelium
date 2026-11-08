// SPDX-License-Identifier: AGPL-3.0-only
//! CPU-only admission of a single emitted dense shard. Never opens source paths
//! from the recipe. The admitting compiler verifies source-to-shard equality;
//! this consumer verifies the pinned recipe, transfer, coverage and tensor set.
use serde_json::Value;
use sha2::{Digest, Sha256};
use stage_contract::{Error, ErrorCode, Result, StageDescriptor, native_shard as contract};
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
};

fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let word = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        out.push(ALPHABET[(word >> 18) as usize] as char);
        out.push(ALPHABET[((word >> 12) & 63) as usize] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[((word >> 6) & 63) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[(word & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}
fn bad(code: &'static str) -> Error {
    Error::new(ErrorCode::Artifact, code)
}
fn number(v: &Value, key: &str) -> Result<u64> {
    v[key].as_u64().ok_or_else(|| bad(contract::RECIPE))
}
fn hash_range(file: &mut File, offset: u64, length: u64) -> Result<String> {
    file.seek(SeekFrom::Start(offset))
        .map_err(|_| bad(contract::READ))?;
    let mut hash = Sha256::new();
    let mut left = length;
    let mut buffer = vec![0; 1024 * 1024];
    while left != 0 {
        let n = left.min(buffer.len() as u64) as usize;
        file.read_exact(&mut buffer[..n])
            .map_err(|_| bad(contract::LENGTH))?;
        hash.update(&buffer[..n]);
        left -= n as u64;
    }
    Ok(format!("{:x}", hash.finalize()))
}

/// No GPU allocation, and no source checkpoint access. `recipe_digest` is the
/// authenticated compiler's pin, not a value inferred from the file being read.
pub fn verify(path: &Path, descriptor: &StageDescriptor, shard: &Value) -> Result<()> {
    if shard["protocol"] != contract::PROTOCOL
        || shard["range"] != serde_json::json!([descriptor.layers.start, descriptor.layers.end])
    {
        return Err(bad(contract::BINDING));
    }
    let recipe = &shard["recipe"];
    let mut body = recipe.clone();
    body.as_object_mut()
        .ok_or_else(|| bad(contract::RECIPE))?
        .remove("node_plan_digest");
    let digest = protocol::canonical::digest(&body).map_err(|_| bad(contract::RECIPE))?;
    if recipe["node_plan_digest"] != digest
        || shard["recipe_digest"] != digest
        || recipe["protocol"] != "mycelium.a13_node_slice_plan.v1"
        || recipe["node_id"] != shard["node_id"]
        || recipe["resolved_commit"] != descriptor.revision
        || recipe["model_id"] != "Qwen/Qwen3-0.6B"
    {
        return Err(bad(contract::RECIPE));
    }
    let files = recipe["files"]
        .as_array()
        .ok_or_else(|| bad(contract::RECIPE))?;
    if files.len() != 1 {
        return Err(bad(contract::RECIPE));
    }
    let row = &files[0];
    let name = row["path"]
        .as_str()
        .and_then(|s| s.strip_prefix("deployment/"))
        .filter(|s| !s.is_empty() && !s.contains('/') && !s.contains('\\') && !s.starts_with('.'))
        .ok_or_else(|| bad(contract::PATH))?;
    if path.file_name().and_then(|s| s.to_str()) != Some(name)
        || path.canonicalize().map_err(|_| bad(contract::PATH))? != path
        || !std::fs::symlink_metadata(path)
            .map_err(|_| bad(contract::PATH))?
            .is_file()
    {
        return Err(bad(contract::PATH));
    }
    let size = number(row, "size_bytes")?;
    if shard["transfer"]["path"] != row["path"]
        || shard["transfer"]["size_bytes"] != row["size_bytes"]
        || shard["transfer"]["content_digest"] != row["content_digest"]
    {
        return Err(bad(contract::BINDING));
    }
    let mut file = File::open(path).map_err(|_| bad(contract::READ))?;
    if file.metadata().map_err(|_| bad(contract::READ))?.len() != size {
        return Err(bad(contract::LENGTH));
    }
    if row["content_digest"] != format!("sha256:{}", hash_range(&mut file, 0, size)?) {
        return Err(bad(contract::HASH));
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|_| bad(contract::READ))?;
    let mut prefix = [0; 8];
    file.read_exact(&mut prefix)
        .map_err(|_| bad(contract::LENGTH))?;
    let header_size = u64::from_le_bytes(prefix);
    let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxHeaderBytesL67
        .bytes()
        .map_err(|_| bad(contract::RECIPE))?
        .get();
    if header_size > cap || header_size.checked_add(8).is_none_or(|n| n > size) {
        return Err(bad(contract::RECIPE));
    }
    let mut raw = vec![0; header_size as usize];
    file.read_exact(&mut raw)
        .map_err(|_| bad(contract::LENGTH))?;
    let header: Value = serde_json::from_slice(&raw).map_err(|_| bad(contract::TENSORS))?;
    let wanted = model::Qwen2::weights(descriptor)?;
    let tensors = header.as_object().ok_or_else(|| bad(contract::TENSORS))?;
    if tensors.len() != wanted.len() {
        return Err(bad(contract::TENSORS));
    }
    let mut ends = Vec::new();
    for (name, shape) in wanted {
        let tensor = &header[&name];
        let start = tensor["data_offsets"][0]
            .as_u64()
            .ok_or_else(|| bad(contract::TENSORS))?;
        let end = tensor["data_offsets"][1]
            .as_u64()
            .ok_or_else(|| bad(contract::TENSORS))?;
        let bytes = shape
            .iter()
            .try_fold(2u64, |n, d| n.checked_mul(*d as u64))
            .ok_or_else(|| bad(contract::TENSORS))?;
        if tensor["shape"] != serde_json::json!(shape)
            || tensor["dtype"] != "BF16"
            || end.checked_sub(start) != Some(bytes)
        {
            return Err(bad(contract::TENSORS));
        }
        ends.push((start, end));
    }
    ends.sort_unstable();
    let mut cursor = 0;
    for (start, end) in ends {
        if start != cursor {
            return Err(bad(contract::TENSORS));
        }
        cursor = end;
    }
    if cursor.checked_add(header_size + 8) != Some(size) {
        return Err(bad(contract::TENSORS));
    }
    let source_size = crate::admission::artifact_layout(descriptor)?.0;
    let source_digest = format!(
        "sha256:{}",
        descriptor
            .checkpoint
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    );
    let sources = recipe["source_files"]
        .as_array()
        .ok_or_else(|| bad(contract::RECIPE))?;
    if sources.len() != 1
        || sources[0]["content_digest"] != source_digest
        || sources[0]["size_bytes"] != source_size
    {
        return Err(bad(contract::RECIPE));
    }
    let inline = base64(&[prefix.as_slice(), raw.as_slice()].concat());
    let segments = row["segments"]
        .as_array()
        .ok_or_else(|| bad(contract::RECIPE))?;
    if segments.is_empty() || segments.len() > 16384 {
        return Err(bad(contract::RECIPE));
    }
    let max = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
        .bytes()
        .map_err(|_| bad(contract::RECIPE))?
        .get();
    let mut cursor = 0;
    for (i, segment) in segments.iter().enumerate() {
        let offset = number(segment, "offset")?;
        let length = number(segment, "length")?;
        let end = offset
            .checked_add(length)
            .ok_or_else(|| bad(contract::RECIPE))?;
        if offset != cursor
            || length == 0
            || end > size
            || (i == 0 && (length != header_size + 8 || segment["inline_base64"] != inline))
            || (i != 0
                && (length > max
                    || segment["source_path"] != sources[0]["path"]
                    || segment["source_offset"]
                        .as_u64()
                        .and_then(|n| n.checked_add(length))
                        .is_none_or(|n| n > source_size)))
            || segment["sha256"] != format!("sha256:{}", hash_range(&mut file, offset, length)?)
        {
            return Err(bad(contract::RECIPE));
        }
        cursor = end;
    }
    if cursor != size {
        return Err(bad(contract::RECIPE));
    }
    Ok(())
}
