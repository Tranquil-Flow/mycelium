// SPDX-License-Identifier: AGPL-3.0-only
//! Closed configurations for the two pinned dense checkpoint cells, not an HF DSL.
use stage_contract::{Architecture, Error, ErrorCode, Result, StageDescriptor};
use std::collections::BTreeMap;
#[derive(Debug, Clone, Copy)]
pub struct DenseConfig {
    pub hidden: i32,
    pub inner: i32,
    pub vocab: i32,
    pub layers: u32,
    pub heads: i32,
    pub kv_heads: i32,
    pub head_dim: i32,
    pub qk_norm: bool,
    pub hybrid: bool,
    pub epsilon: f32,
    pub theta: f32,
}
impl DenseConfig {
    pub fn for_descriptor(d: &StageDescriptor) -> Result<Self> {
        match d.architecture {
            Architecture::Qwen2 => Ok(Self {
                hidden: 896,
                inner: 4864,
                vocab: 151936,
                layers: 24,
                heads: 14,
                kv_heads: 2,
                head_dim: 64,
                qk_norm: false,
                hybrid: false,
                epsilon: 1e-6,
                theta: 1_000_000.,
            }),
            Architecture::Qwen3 => Ok(Self {
                hidden: 1024,
                inner: 3072,
                vocab: 151936,
                layers: 28,
                heads: 16,
                kv_heads: 8,
                head_dim: 128,
                qk_norm: true,
                hybrid: false,
                epsilon: 1e-6,
                theta: 1_000_000.,
            }),
            Architecture::Qwen35DenseHybrid => Ok(Self {
                hidden: 1024,
                inner: 3584,
                vocab: 248320,
                layers: 24,
                heads: 8,
                kv_heads: 2,
                head_dim: 256,
                qk_norm: true,
                hybrid: true,
                epsilon: 1e-6,
                theta: 10_000_000.,
            }),
            _ => Err(Error::new(ErrorCode::Unsupported, "dense-architecture")),
        }
    }
    pub fn weights(self, d: &StageDescriptor) -> Result<BTreeMap<String, Vec<i32>>> {
        d.validate(self.layers)?;
        if self.hybrid {
            return crate::hybrid::weights(self, d);
        }
        let mut w = BTreeMap::new();
        let (h, q, k) = (
            self.hidden,
            self.heads * self.head_dim,
            self.kv_heads * self.head_dim,
        );
        if d.embedding || d.head {
            w.insert("model.embed_tokens.weight".into(), vec![self.vocab, h]);
        }
        if d.head {
            w.insert("model.norm.weight".into(), vec![h]);
        }
        for i in d.layers.clone() {
            let mut layer = vec![
                ("input_layernorm.weight", vec![h]),
                ("post_attention_layernorm.weight", vec![h]),
                ("self_attn.q_proj.weight", vec![q, h]),
                ("self_attn.k_proj.weight", vec![k, h]),
                ("self_attn.v_proj.weight", vec![k, h]),
                ("self_attn.o_proj.weight", vec![h, q]),
                ("mlp.gate_proj.weight", vec![self.inner, h]),
                ("mlp.up_proj.weight", vec![self.inner, h]),
                ("mlp.down_proj.weight", vec![h, self.inner]),
            ];
            if self.qk_norm {
                layer.extend([
                    ("self_attn.q_norm.weight", vec![self.head_dim]),
                    ("self_attn.k_norm.weight", vec![self.head_dim]),
                ]);
            } else {
                layer.extend([
                    ("self_attn.q_proj.bias", vec![q]),
                    ("self_attn.k_proj.bias", vec![k]),
                    ("self_attn.v_proj.bias", vec![k]),
                ]);
            }
            for (name, shape) in layer {
                w.insert(format!("model.layers.{i}.{name}"), shape);
            }
        }
        Ok(w)
    }
    pub fn embedding_key(self) -> &'static str {
        if self.hybrid {
            "model.language_model.embed_tokens.weight"
        } else {
            "model.embed_tokens.weight"
        }
    }
    pub fn norm_key(self) -> &'static str {
        if self.hybrid {
            "model.language_model.norm.weight"
        } else {
            "model.norm.weight"
        }
    }
    pub fn state_bytes_for(self, layers: std::ops::Range<u32>, tokens: u32) -> Result<u64> {
        if layers.start >= layers.end || layers.end > self.layers {
            return Err(Error::new(ErrorCode::InvalidDescriptor, "state-layers"));
        }
        if !self.hybrid {
            return self.state_bytes(layers.end - layers.start, tokens);
        }
        let full = layers
            .clone()
            .filter(|i| crate::hybrid::full_attention(*i))
            .count() as u64;
        let recurrent = u64::from(layers.end - layers.start) - full;
        Ok(
            full * u64::from(tokens) * 2 * 2 * 256 * 4
                + recurrent * (3 * 6144 + 16 * 128 * 128) * 4,
        )
    }
    fn state_bytes(self, layers: u32, tokens: u32) -> Result<u64> {
        u64::from(layers)
            .checked_mul(u64::from(tokens))
            .and_then(|n| n.checked_mul(2 * self.kv_heads as u64 * self.head_dim as u64 * 4))
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "kv-size"))
    }
}
