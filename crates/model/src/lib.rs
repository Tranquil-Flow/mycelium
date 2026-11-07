// SPDX-License-Identifier: AGPL-3.0-only
//! Qwen2 explicit FP32 arithmetic, independent of native handles.
//! Port source: mycelium_router/mlx_runtime.py @ 27fdd49 (same bytes as spike oracle).
pub mod chat;
mod dense_config;
pub mod greedy;
pub mod hybrid;
pub mod moe;
pub mod moe_decoder;
pub mod moe_stage;
pub mod quant;
#[cfg(feature = "tokenizer")]
pub mod tokenizer;
pub use dense_config::DenseConfig;
mod weight;
use stage_contract::{Error, ErrorCode, Result, StageDescriptor};
use std::collections::BTreeMap;
pub use weight::Weight;

/// Deliberately fixed first qualified config; other supported product cells remain blockers.
#[derive(Debug, Clone, Copy)]
pub struct Qwen2;
impl Qwen2 {
    pub const HIDDEN: u32 = 896;
    pub const VOCAB: u32 = 151936;
    pub const LAYERS: u32 = 24;
    pub fn weights(d: &StageDescriptor) -> Result<BTreeMap<String, Vec<i32>>> {
        DenseConfig::for_descriptor(d)?.weights(d)
    }
    pub fn state_bytes(layers: u32, tokens: u32) -> Result<u64> {
        u64::from(layers)
            .checked_mul(u64::from(tokens))
            .and_then(|n| n.checked_mul(2 * 2 * 64 * 4))
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "kv-size"))
    }
}

/// Fallible primitive seam, not a universal model compiler. Implementations must
/// preserve operation ordering and may not silently use fused/quantized kernels.
pub trait Tensor: Sized {
    fn shape(&self) -> Vec<i32>;
    fn share(&self) -> Result<Self>;
    fn copy(&self) -> Result<Self>;
    fn scalar(&self, v: f32) -> Result<Self>;
    fn range(&self, start: i32, end: i32, step: i32) -> Result<Self>;
    fn reshape(&self, dims: &[i32]) -> Result<Self>;
    fn transpose(&self, axes: &[i32]) -> Result<Self>;
    fn slice(&self, start: &[i32], stop: &[i32]) -> Result<Self>;
    fn cat(&self, other: &Self, axis: i32) -> Result<Self>;
    fn repeat(&self, times: i32, axis: i32) -> Result<Self>;
    fn mean(&self) -> Result<Self>;
    fn sum(&self) -> Result<Self>;
    fn exp(&self) -> Result<Self>;
    fn logaddexp(&self, b: &Self) -> Result<Self>;
    fn softmax(&self) -> Result<Self>;
    fn select(&self, yes: &Self, no: &Self) -> Result<Self>;
    fn square(&self) -> Result<Self>;
    fn rsqrt(&self) -> Result<Self>;
    fn sigmoid(&self) -> Result<Self>;
    fn cos(&self) -> Result<Self>;
    fn sin(&self) -> Result<Self>;
    fn neg(&self) -> Result<Self>;
    fn zeros(&self) -> Result<Self>;
    fn add(&self, b: &Self) -> Result<Self>;
    fn mul(&self, b: &Self) -> Result<Self>;
    fn div(&self, b: &Self) -> Result<Self>;
    fn pow(&self, b: &Self) -> Result<Self>;
    fn matmul(&self, b: &Self) -> Result<Self>;
    fn ge(&self, b: &Self) -> Result<Self>;
    fn eval(&self) -> Result<()>;
    fn take(&self, ids: &Self) -> Result<Self>;
    fn quantized_matmul(&self, codes: &Self, scales: &Self, biases: &Self) -> Result<Self>;
    fn dequantize(&self, scales: &Self, biases: &Self) -> Result<Self>;
    fn equal(&self, other: &Self) -> Result<bool>;
}
pub fn final_norm<T: Tensor>(cfg: DenseConfig, x: &T, w: &T) -> Result<T> {
    if cfg.hybrid {
        hybrid::norm_offset(x, w, cfg.epsilon)
    } else {
        rms_epsilon(x, w, cfg.epsilon)
    }
}
pub fn rms<T: Tensor>(x: &T, w: &T) -> Result<T> {
    rms_epsilon(x, w, 1e-6)
}
pub fn rms_epsilon<T: Tensor>(x: &T, w: &T, epsilon: f32) -> Result<T> {
    let shape = x.shape();
    if shape.is_empty() || shape.iter().any(|d| *d <= 0) || w.shape() != [shape[shape.len() - 1]] {
        return Err(Error::new(ErrorCode::MalformedTensor, "rms-shape"));
    }
    x.mul(&x.square()?.mean()?.add(&x.scalar(epsilon)?)?.rsqrt()?)?
        .mul(w)
}
pub fn linear<T: Tensor>(x: &T, w: &Weight<T>) -> Result<T> {
    w.linear(x)
}
fn float_linear<T: Tensor>(x: &T, w: &T) -> Result<T> {
    let shape = x.shape();
    let weight = w.shape();
    if shape.len() < 2
        || weight.len() != 2
        || shape.iter().any(|d| *d <= 0)
        || weight.iter().any(|d| *d <= 0)
        || shape[shape.len() - 1] != weight[1]
    {
        return Err(Error::new(ErrorCode::MalformedTensor, "linear-shape"));
    }
    let rows = shape[..shape.len() - 1]
        .iter()
        .try_fold(1i32, |n, d| n.checked_mul(*d))
        .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "linear-rows"))?;
    let wt = w.transpose(&[1, 0])?;
    if rows == 1 {
        let y = x.cat(&x.zeros()?, 0)?.matmul(&wt)?;
        let mut stop = y.shape();
        stop[0] = 1;
        y.slice(&vec![0; stop.len()], &stop)
    } else {
        x.matmul(&wt)
    }
}
fn rope<T: Tensor>(x: &T, pos: i32, theta: f32) -> Result<T> {
    let sh = x.shape();
    let dim = sh[3];
    let seq = sh[2];
    let exponent = x.range(0, dim, 2)?.div(&x.scalar(dim as f32)?)?;
    let inv = x.scalar(1.)?.div(&x.scalar(theta)?.pow(&exponent)?)?;
    let freqs = x
        .range(pos, pos + seq, 1)?
        .reshape(&[-1, 1])?
        .mul(&inv.reshape(&[1, -1])?)?;
    let emb = freqs.cat(&freqs, -1)?.reshape(&[1, 1, seq, dim])?;
    let first = x.slice(&[0, 0, 0, 0], &[sh[0], sh[1], seq, dim / 2])?;
    let second = x.slice(&[0, 0, 0, dim / 2], &sh)?;
    x.mul(&emb.cos()?)?
        .add(&second.neg()?.cat(&first, -1)?.mul(&emb.sin()?)?)
}
pub type Kv<T> = Vec<Option<(T, T)>>;
/// Build replacement state without mutating committed state. Caller commits only
/// after output, head, state materialization and host export have all succeeded.
pub fn forward<T: Tensor>(
    d: &StageDescriptor,
    mut x: T,
    w: &BTreeMap<String, Weight<T>>,
    old: &Kv<T>,
    pos: i32,
) -> Result<(T, Kv<T>)> {
    let cfg = DenseConfig::for_descriptor(d)?;
    d.validate(cfg.layers)?;
    if cfg.hybrid {
        return hybrid::forward(d, x, w, old, pos);
    }
    let shape = x.shape();
    let malformed = || Error::new(ErrorCode::MalformedTensor, "qwen2-state-shape");
    if shape.len() != 3
        || shape[0] != 1
        || shape[1] <= 0
        || shape[2] != cfg.hidden
        || pos < 0
        || pos.checked_add(shape[1]).is_none()
        || old.len() != (d.layers.end - d.layers.start) as usize
    {
        return Err(malformed());
    }
    for pair in old {
        match pair {
            None if pos == 0 => {}
            Some((k, v))
                if pos > 0
                    && k.shape() == [1, cfg.kv_heads, pos, cfg.head_dim]
                    && v.shape() == [1, cfg.kv_heads, pos, cfg.head_dim] => {}
            _ => return Err(malformed()),
        }
    }
    for (name, shape) in Qwen2::weights(d)? {
        if w.get(&name).is_none_or(|a| a.shape() != shape) {
            return Err(Error::new(ErrorCode::MalformedTensor, "qwen2-weight-shape"));
        }
    }
    let mut next = Vec::new();
    for (idx, i) in d.layers.clone().enumerate() {
        let prefix = format!("model.layers.{i}.");
        let weight = |name: &str| {
            w.get(&(prefix.clone() + name))
                .ok_or_else(|| Error::new(ErrorCode::Artifact, "missing-weight"))
        };
        let seq = x.shape()[1];
        let norm = rms_epsilon(&x, weight("input_layernorm.weight")?.float()?, cfg.epsilon)?;
        let proj = |name: &str, heads: i32| -> Result<T> {
            let x = linear(&norm, weight(&format!("self_attn.{name}_proj.weight"))?)?;
            let x = if cfg.qk_norm {
                x
            } else {
                x.add(weight(&format!("self_attn.{name}_proj.bias"))?.float()?)?
            };
            let x = x
                .reshape(&[1, seq, heads, cfg.head_dim])?
                .transpose(&[0, 2, 1, 3])?;
            if cfg.qk_norm && name != "v" {
                rms_epsilon(
                    &x,
                    weight(&format!("self_attn.{name}_norm.weight"))?.float()?,
                    cfg.epsilon,
                )
            } else {
                Ok(x)
            }
        };
        let q = rope(&proj("q", cfg.heads)?, pos, cfg.theta)?;
        let k = rope(&proj("k", cfg.kv_heads)?, pos, cfg.theta)?;
        let v = proj("v", cfg.kv_heads)?;
        let (k, v) = if let Some((pk, pv)) = &old[idx] {
            (pk.cat(&k, 2)?, pv.cat(&v, 2)?)
        } else {
            (k, v)
        };
        let scores = q
            .matmul(
                &k.repeat(cfg.heads / cfg.kv_heads, 1)?
                    .transpose(&[0, 1, 3, 2])?,
            )?
            .div(&x.scalar((cfg.head_dim as f64).sqrt() as f32)?)?;
        let causal = x
            .range(pos, pos + seq, 1)?
            .reshape(&[-1, 1])?
            .ge(&x.range(0, k.shape()[2], 1)?.reshape(&[1, -1])?)?
            .reshape(&[1, 1, seq, -1])?;
        let probs = causal
            .select(&scores, &x.scalar(f32::NEG_INFINITY)?)?
            .softmax()?;
        let att = probs
            .matmul(&v.repeat(cfg.heads / cfg.kv_heads, 1)?)?
            .transpose(&[0, 2, 1, 3])?
            .reshape(&[1, seq, cfg.heads * cfg.head_dim])?;
        x = x.add(&linear(&att, weight("self_attn.o_proj.weight")?)?)?;
        let norm = rms_epsilon(
            &x,
            weight("post_attention_layernorm.weight")?.float()?,
            cfg.epsilon,
        )?;
        let gate = linear(&norm, weight("mlp.gate_proj.weight")?)?;
        let up = linear(&norm, weight("mlp.up_proj.weight")?)?;
        x = x.add(&linear(
            &gate.mul(&gate.sigmoid()?)?.mul(&up)?,
            weight("mlp.down_proj.weight")?,
        )?)?;
        next.push(Some((k, v)));
    }
    x.eval()?;
    for (k, v) in next.iter().flatten() {
        k.eval()?;
        v.eval()?;
    }
    Ok((x, next))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn qwen3_geometry_has_qk_norm_and_no_projection_bias() {
        let d = StageDescriptor {
            checkpoint: [1; 32],
            revision: "fixture".into(),
            tensor_manifest: [2; 32],
            semantics: "qwen3".into(),
            architecture: stage_contract::Architecture::Qwen3,
            layers: 0..9,
            embedding: true,
            head: false,
            representation: "f32".into(),
            quantizer: "none".into(),
            profile: stage_contract::NumericsProfile::MlxExplicitF32,
            state_schema: "kv".into(),
        };
        let w = Qwen2::weights(&d).unwrap();
        assert_eq!(w["model.embed_tokens.weight"], [151936, 1024]);
        assert_eq!(w["model.layers.0.self_attn.q_proj.weight"], [2048, 1024]);
        assert_eq!(w["model.layers.0.self_attn.k_proj.weight"], [1024, 1024]);
        assert_eq!(w["model.layers.0.self_attn.q_norm.weight"], [128]);
        assert!(!w.keys().any(|n| n.ends_with(".bias")));
    }
    #[test]
    fn hybrid_geometry_owns_conv_recurrence_and_gated_attention() {
        let d = StageDescriptor {
            checkpoint: [1; 32],
            revision: "fixture".into(),
            tensor_manifest: [2; 32],
            semantics: "qwen35".into(),
            architecture: stage_contract::Architecture::Qwen35DenseHybrid,
            layers: 0..4,
            embedding: true,
            head: false,
            representation: "f32".into(),
            quantizer: "none".into(),
            profile: stage_contract::NumericsProfile::MlxExplicitF32,
            state_schema: "hybrid".into(),
        };
        let w = Qwen2::weights(&d).unwrap();
        assert_eq!(
            w["model.language_model.layers.0.linear_attn.conv1d.weight"],
            [6144, 1, 4]
        );
        assert_eq!(
            w["model.language_model.layers.0.linear_attn.in_proj_a.weight"],
            [16, 1024]
        );
        assert_eq!(
            w["model.language_model.layers.3.self_attn.q_proj.weight"],
            [4096, 1024]
        );
        assert_eq!(
            w["model.language_model.embed_tokens.weight"],
            [248320, 1024]
        );
        assert!(!w.keys().any(|n| n.starts_with("model.visual")));
    }
    #[test]
    fn kv_accounting_is_exact() {
        // L8/F14: two tensors * 2 KV heads * 64 floats * 4 bytes * layers * tokens.
        assert_eq!(Qwen2::state_bytes(8, 35).unwrap(), 286720);
        assert_eq!(Qwen2::state_bytes(16, 98).unwrap(), 1605632);
    }
}
