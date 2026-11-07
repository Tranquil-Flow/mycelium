// SPDX-License-Identifier: AGPL-3.0-only
//! Qwen3.6-35B-A3B's MoE block, not stage/model admission. The descriptor still
//! refuses MoE until hybrid state, head, admission and full replay are qualified.
//! Arithmetic order: qwen35_mlx.py @ f4e366e9, sparse_moe_block/route_top_k.
use crate::{Tensor, Weight, linear};
use stage_contract::{Error, ErrorCode, Result};
use std::collections::BTreeMap;

/// Separate native seam: dense implementations do not inherit MoE semantics.
pub trait MoeTensor: Tensor {
    /// Threshold selection, ties to low expert id, descending probability rank.
    fn route_top_k(&self, k: i32) -> Result<(Self, Self)>;
    /// One row per (token, expert); no dense dequantized expert image.
    fn expert_product(&self, weight: &Weight<Self>, indices: &Self) -> Result<Self>;
    fn expert_linear(&self, weight: &Weight<Self>, indices: &Self) -> Result<Self> {
        let product = self.expert_product(weight, indices)?;
        match weight {
            Weight::Packed { scales, .. } => product.mul(&scales.take(indices)?),
            Weight::Float(_) => Ok(product),
        }
    }
    fn sum_experts(&self) -> Result<Self>;
}

pub struct BlockOutput<T> {
    pub output: T,
    pub probabilities: T,
    pub scores: T,
    pub indices: T,
}

/// Exactly the cached Qwen3.6-35B-A3B first-layer MoE geometry. This intentionally
/// does not broaden DenseConfig or turn a component oracle into model acceptance.
pub fn qwen36_first_block<T: MoeTensor>(
    x: &T,
    weights: &BTreeMap<String, Weight<T>>,
) -> Result<BlockOutput<T>> {
    qwen36_block(x, weights, 0)
}

/// Closed layer-indexed component; does not admit a model or stage.
pub fn qwen36_block<T: MoeTensor>(
    x: &T,
    weights: &BTreeMap<String, Weight<T>>,
    layer: u32,
) -> Result<BlockOutput<T>> {
    if layer >= 40 {
        return Err(Error::new(ErrorCode::InvalidDescriptor, "moe-layer"));
    }
    let prefix = format!("model.language_model.layers.{layer}.mlp");
    let shape = x.shape();
    if shape.len() != 3 || shape[0] != 1 || !(1..=128).contains(&shape[1]) || shape[2] != 2048 {
        return Err(Error::new(ErrorCode::MalformedTensor, "moe-input-shape"));
    }
    let get = |suffix: &str, expected: &[i32]| -> Result<&Weight<T>> {
        let weight = weights
            .get(&format!("{prefix}.{suffix}"))
            .ok_or_else(|| Error::new(ErrorCode::Artifact, "moe-missing-weight"))?;
        if weight.shape() != expected {
            return Err(Error::new(ErrorCode::MalformedTensor, "moe-weight-shape"));
        }
        Ok(weight)
    };
    let router = get("gate.weight", &[256, 2048])?;
    let gate_up = get("experts.gate_up_proj", &[256, 1024, 2048])?;
    let down = get("experts.down_proj", &[256, 2048, 512])?;
    let shared_gate = get("shared_expert.gate_proj.weight", &[512, 2048])?;
    let shared_up = get("shared_expert.up_proj.weight", &[512, 2048])?;
    let shared_down = get("shared_expert.down_proj.weight", &[2048, 512])?;
    let shared_router = get("shared_expert_gate.weight", &[1, 2048])?;
    // Both routing projections remain FP32 in the packed profile.
    router.float()?;
    shared_router.float()?;
    let rows = shape[1];
    let flat = x.reshape(&[rows, 2048])?;
    let probabilities = linear(&flat, router)?.softmax()?;
    let (scores, indices) = probabilities.route_top_k(8)?;
    let projected = flat
        .reshape(&[rows, 1, 2048])?
        .expert_linear(gate_up, &indices)?;
    let gate = projected.slice(&[0, 0, 0], &[rows, 8, 512])?;
    let up = projected.slice(&[0, 0, 512], &[rows, 8, 1024])?;
    let expert = gate
        .mul(&gate.sigmoid()?)?
        .mul(&up)?
        .expert_linear(down, &indices)?;
    let mixed = expert.mul(&scores.reshape(&[rows, 8, 1])?)?.sum_experts()?;
    let gate = linear(&flat, shared_gate)?;
    let shared = linear(
        &gate
            .mul(&gate.sigmoid()?)?
            .mul(&linear(&flat, shared_up)?)?,
        shared_down,
    )?;
    let shared = shared.mul(&linear(&flat, shared_router)?.sigmoid()?)?;
    Ok(BlockOutput {
        output: mixed.add(&shared)?.reshape(&shape)?,
        probabilities,
        scores,
        indices,
    })
}
