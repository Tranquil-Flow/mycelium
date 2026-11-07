// SPDX-License-Identifier: AGPL-3.0-only
//! Closed Qwen3.6 descriptor and assigned-image verification. This is a local
//! StageDescriptor, not an L5 assignment, load proof, or new wire format.
//! Identity pins are a lossless projection of the already recorded r6/r7 source
//! images; no checkpoint is opened by descriptor/admission operations.
use crate::admission::digest_hex;
use model::{hybrid, moe_decoder as moe};
use sha2::{Digest, Sha256};
use stage_contract::*;
use std::{
    collections::BTreeMap,
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
};

pub const IMAGE: &str = "f1483d385d7ec987aac0afd9be96bbab3733bd34b80a7b56e784f8795ec541ee";
pub const INDEX: &str = "41b9356101ebf8e7519e150dc811f80c4226e727301fbb032b890f006ed0be83";
const PINS: &str = include_str!("qwen36-pins.json");
pub fn descriptor(
    layers: std::ops::Range<u32>,
    embedding: bool,
    head: bool,
    profile: NumericsProfile,
) -> Result<StageDescriptor> {
    let (representation, quantizer) = match profile {
        NumericsProfile::MlxExplicitF32 => ("bf16-expanded-f32", "none"),
        NumericsProfile::MlxPackedInt8 => (
            "persistent_packed_int8",
            "symmetric-rowwise-int8-half-away-v1",
        ),
        _ => return Err(Error::new(ErrorCode::Unsupported, "moe-profile")),
    };
    let d = StageDescriptor {
        checkpoint: digest_hex(IMAGE)?,
        revision: "995ad96eacd98c81ed38be0c5b274b04031597b0".into(),
        tensor_manifest: digest_hex(INDEX)?,
        semantics: "qwen36-35b-a3b-v014-explicit-v1".into(),
        architecture: Architecture::Qwen35MoeHybrid,
        layers,
        embedding,
        head,
        representation: representation.into(),
        quantizer: quantizer.into(),
        profile,
        state_schema: "hybrid-conv-deltanet-kv-f32-v1".into(),
    };
    d.validate(moe::LAYERS)?;
    Ok(d)
}
pub fn validate(d: &StageDescriptor) -> Result<()> {
    d.validate(moe::LAYERS)?;
    if *d != descriptor(d.layers.clone(), d.embedding, d.head, d.profile)? {
        return Err(Error::new(ErrorCode::Unsupported, "model-profile-identity"));
    }
    Ok(())
}
pub fn state_bytes(mut layers: std::ops::Range<u32>, tokens: u32) -> Result<u64> {
    moe::stage_weights(layers.clone(), false, false)?;
    layers.try_fold(0u64, |n, layer| {
        // qwen35_mlx.py: full K/V [1,2,t,256]; linear conv [1,3,8192]
        // and recurrent [1,32,128,128]. Fixed state is not sliceable KV.
        let bytes = if hybrid::full_attention(layer) {
            u64::from(tokens) * 2 * 2 * 256 * 4
        } else {
            (3 * 8192 + 32 * 128 * 128) * 4
        };
        n.checked_add(bytes)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "moe-state-bytes"))
    })
}
pub fn requirements(d: &StageDescriptor, limits: Limits) -> Result<budgets::Reservation> {
    validate(d)?;
    let cap = || Error::new(ErrorCode::ResourceCap, "prepare-cap");
    if limits.max_tokens == 0 || limits.max_tokens > i32::MAX as u32 {
        return Err(cap());
    }
    let packed = d.profile == NumericsProfile::MlxPackedInt8;
    let weights = moe::stage_weights(d.layers.clone(), d.embedding, d.head)?;
    let resident = moe::stage_resident_bytes(d.layers.clone(), d.embedding, d.head, packed)?;
    let state = state_bytes(d.layers.clone(), limits.max_tokens)?;
    // Logical graph and conversion reservations, NOT measured physical allocator peaks.
    // Preserve the existing loader's 32-MiB chunk / ten-copy reserve. The FP32
    // CPU-stream load retains assigned source arrays alongside expanded arrays.
    let loading = if packed {
        weights
            .iter()
            .map(|(name, shape)| {
                let elements: u64 = shape.iter().map(|v| *v as u64).product();
                Ok(moe::resident_bytes(name, shape, true)?
                    + (elements * 4).min(32 * 1024 * 1024) * 10)
            })
            .collect::<Result<Vec<u64>>>()?
            .into_iter()
            .max()
            .unwrap_or(0)
    } else {
        resident
    };
    let rows = u64::from(limits.max_tokens).min(moe::MAX_ROWS as u64);
    let scratch = rows
        .checked_mul(9 * 32 * 128 * 128 * 4 + 16 * 8192 * 4 + 8 * 512 * 4 * 8)
        .and_then(|n| n.checked_add(rows * 16 * u64::from(limits.max_tokens) * 4))
        .and_then(|n| n.checked_add(state))
        .and_then(|n| {
            n.checked_add(if d.head {
                moe::VOCAB as u64 * 4 * if packed { 64 } else { 2 }
            } else {
                0
            })
        })
        .ok_or_else(cap)?
        .max(loading);
    if resident > limits.weight_bytes
        || state > limits.state_bytes
        || scratch > limits.scratch_bytes
    {
        return Err(cap());
    }
    Ok(budgets::Reservation {
        staged: budgets::Bytes::ZERO,
        staging_overhead: budgets::Bytes::ZERO,
        resident: budgets::Bytes::new(resident),
        scratch: budgets::Bytes::new(scratch),
        kv: budgets::Bytes::new(state),
    })
}
/// Exactly the assigned tensors, grouped by their sealed original shard name.
/// The directory contains original immutable shards (L7 owns staging). No copy,
/// downloading, complete-model hash pass, or unassigned tensor materialization.
pub fn shard_plan(d: &StageDescriptor) -> Result<BTreeMap<String, BTreeMap<String, Vec<i32>>>> {
    validate(d)?;
    let pins = pins()?;
    let mut out: BTreeMap<String, BTreeMap<String, Vec<i32>>> = BTreeMap::new();
    for (name, shape) in moe::stage_weights(d.layers.clone(), d.embedding, d.head)? {
        let pin = &pins["weights"][&name];
        if pin["shape"] != serde_json::json!(shape) {
            return Err(bad());
        }
        let shard = pin["shard"].as_str().ok_or_else(bad)?;
        if Path::new(shard).components().count() != 1
            || !shard.starts_with("model-")
            || !shard.ends_with(".safetensors")
        {
            return Err(bad());
        }
        out.entry(shard.into()).or_default().insert(name, shape);
    }
    Ok(out)
}
/// Closed source metadata for exactly this assignment. This is a projection of
/// the recorded image, not authority, an artifact header, or a load receipt.
pub fn assigned_image(d: &StageDescriptor) -> Result<serde_json::Value> {
    let pins = pins()?;
    let mut shards = serde_json::Map::new();
    let mut weights = serde_json::Map::new();
    for (shard, wanted) in shard_plan(d)? {
        shards.insert(shard.clone(), pins["checkpoint"]["shards"][&shard].clone());
        for name in wanted.keys() {
            weights.insert(name.clone(), pins["weights"][name].clone());
        }
    }
    Ok(serde_json::json!({"shards": shards, "weights": weights}))
}
fn bad() -> Error {
    Error::new(ErrorCode::Artifact, "moe-source-identity")
}
fn pins() -> Result<serde_json::Value> {
    let digest: [u8; 32] = Sha256::digest(PINS.as_bytes()).into();
    if digest != digest_hex(IMAGE)? {
        return Err(bad());
    }
    serde_json::from_str(PINS).map_err(|_| bad())
}
/// Verify only a selected shard's header and assigned source payloads. Called
/// before AND after native loading; the immutable-artifact precondition is the
/// same as the dense worker, not an adversarial-filesystem/held-fd proof.
pub fn verify_shard(root: &Path, shard: &str, wanted: &BTreeMap<String, Vec<i32>>) -> Result<()> {
    let pins = pins()?;
    let identity = &pins["checkpoint"]["shards"][shard];
    if identity.is_null() || Path::new(shard).components().count() != 1 {
        return Err(bad());
    }
    let mut file = File::open(root.join(shard)).map_err(|_| bad())?;
    if Some(file.metadata().map_err(|_| bad())?.len()) != identity["file_bytes"].as_u64() {
        return Err(bad());
    }
    let mut size = [0; 8];
    file.read_exact(&mut size).map_err(|_| bad())?;
    let size = u64::from_le_bytes(size);
    if size > 1024 * 1024 {
        return Err(bad());
    }
    let mut header = vec![0; size as usize];
    file.read_exact(&mut header).map_err(|_| bad())?;
    if format!("{:x}", Sha256::digest(&header))
        != identity["header_sha256"].as_str().ok_or_else(bad)?
    {
        return Err(bad());
    }
    let header: serde_json::Value = serde_json::from_slice(&header).map_err(|_| bad())?;
    for (name, shape) in wanted {
        let pin = &pins["weights"][name];
        let meta = &header[name];
        if pin["shard"] != shard
            || pin["shape"] != serde_json::json!(shape)
            || meta["shape"] != pin["shape"]
            || meta["dtype"] != pin["source_dtype"]
        {
            return Err(bad());
        }
        let start = meta["data_offsets"][0].as_u64().ok_or_else(bad)?;
        let end = meta["data_offsets"][1].as_u64().ok_or_else(bad)?;
        if end.checked_sub(start) != pin["source_bytes"].as_u64()
            || size
                .checked_add(8)
                .and_then(|n| n.checked_add(end))
                .is_none_or(|n| Some(n) > identity["file_bytes"].as_u64())
        {
            return Err(bad());
        }
        file.seek(SeekFrom::Start(8 + size + start))
            .map_err(|_| bad())?;
        let mut remaining = end - start;
        let mut hash = Sha256::new();
        let mut buffer = vec![0; 1024 * 1024];
        while remaining > 0 {
            let n = remaining.min(buffer.len() as u64) as usize;
            file.read_exact(&mut buffer[..n]).map_err(|_| bad())?;
            hash.update(&buffer[..n]);
            remaining -= n as u64;
        }
        if format!("{:x}", hash.finalize()) != pin["source_sha256"].as_str().ok_or_else(bad)? {
            return Err(bad());
        }
    }
    Ok(())
}
