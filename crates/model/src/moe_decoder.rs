// SPDX-License-Identifier: AGPL-3.0-only
//! Closed Qwen3.6-35B-A3B decoder component. Not StageBackend/model admission.
//! Source: qwen35_mlx.py::decoder_layer @ f4e366e9. All experts belong to a layer.
use crate::{
    Tensor, Weight, hybrid,
    moe::{BlockOutput, MoeTensor, qwen36_block},
};
use stage_contract::{Error, ErrorCode, Result};
use std::collections::BTreeMap;

pub fn layer_weights(layer: u32) -> Result<BTreeMap<String, Vec<i32>>> {
    if layer >= 40 {
        return Err(Error::new(ErrorCode::InvalidDescriptor, "moe-layer"));
    }
    let mut items = vec![
        ("input_layernorm.weight", vec![2048]),
        ("post_attention_layernorm.weight", vec![2048]),
        ("mlp.gate.weight", vec![256, 2048]),
        ("mlp.experts.gate_up_proj", vec![256, 1024, 2048]),
        ("mlp.experts.down_proj", vec![256, 2048, 512]),
        ("mlp.shared_expert.gate_proj.weight", vec![512, 2048]),
        ("mlp.shared_expert.up_proj.weight", vec![512, 2048]),
        ("mlp.shared_expert.down_proj.weight", vec![2048, 512]),
        ("mlp.shared_expert_gate.weight", vec![1, 2048]),
    ];
    if hybrid::full_attention(layer) {
        items.extend([
            ("self_attn.q_proj.weight", vec![8192, 2048]),
            ("self_attn.k_proj.weight", vec![512, 2048]),
            ("self_attn.v_proj.weight", vec![512, 2048]),
            ("self_attn.o_proj.weight", vec![2048, 4096]),
            ("self_attn.q_norm.weight", vec![256]),
            ("self_attn.k_norm.weight", vec![256]),
        ]);
    } else {
        items.extend([
            ("linear_attn.in_proj_qkv.weight", vec![8192, 2048]),
            ("linear_attn.in_proj_z.weight", vec![4096, 2048]),
            ("linear_attn.in_proj_a.weight", vec![32, 2048]),
            ("linear_attn.in_proj_b.weight", vec![32, 2048]),
            ("linear_attn.out_proj.weight", vec![2048, 4096]),
            ("linear_attn.conv1d.weight", vec![8192, 1, 4]),
            ("linear_attn.dt_bias", vec![32]),
            ("linear_attn.A_log", vec![32]),
            ("linear_attn.norm.weight", vec![128]),
        ]);
    }
    Ok(items
        .into_iter()
        .map(|(name, shape)| (format!("{}layers.{layer}.{name}", hybrid::PREFIX), shape))
        .collect())
}

pub const LAYERS: u32 = 40;
pub const HIDDEN: i32 = 2048;
pub const VOCAB: i32 = 248_320;
/// Largest row count one hidden/ids batch may carry (matches the layer cursor bound).
pub const MAX_ROWS: i32 = 128;
pub const EMBED_KEY: &str = "model.language_model.embed_tokens.weight";
pub const NORM_KEY: &str = "model.language_model.norm.weight";
/// Qwen3.6-35B-A3B is UNTIED (`tie_word_embeddings=false`): the head is its own tensor.
pub const HEAD_KEY: &str = "lm_head.weight";
const NORM_EPSILON: f32 = 1e-6;

/// Entry-stage tensor owned by the embedding owner only.
pub fn embedding_weights() -> BTreeMap<String, Vec<i32>> {
    BTreeMap::from([(EMBED_KEY.to_owned(), vec![VOCAB, HIDDEN])])
}
/// Final-stage tensors owned by the head owner only (never the embedding).
pub fn head_weights() -> BTreeMap<String, Vec<i32>> {
    BTreeMap::from([
        (NORM_KEY.to_owned(), vec![HIDDEN]),
        (HEAD_KEY.to_owned(), vec![VOCAB, HIDDEN]),
    ])
}
/// Exact assigned inventory of a resident stage; unassigned tensors never appear.
pub fn stage_weights(
    layers: std::ops::Range<u32>,
    embedding: bool,
    head: bool,
) -> Result<BTreeMap<String, Vec<i32>>> {
    if layers.start >= layers.end
        || layers.end > LAYERS
        || (embedding && layers.start != 0)
        || (head && layers.end != LAYERS)
    {
        return Err(Error::new(ErrorCode::InvalidDescriptor, "moe-stage-range"));
    }
    let mut out = BTreeMap::new();
    if embedding {
        out.extend(embedding_weights());
    }
    for layer in layers {
        out.extend(layer_weights(layer)?);
    }
    if head {
        out.extend(head_weights());
    }
    Ok(out)
}
/// Resident bytes of one tensor in the explicit-F32 or persistent packed-int8 profile.
/// Packed matrices (2-D except routers) and expert stacks hold one byte per code, an f32
/// row scale and a (scale, bias) f32 pair per 128-wide group; everything else is f32.
pub fn resident_bytes(name: &str, shape: &[i32], packed: bool) -> Result<u64> {
    let bad = || Error::new(ErrorCode::MalformedTensor, "moe-resident-shape");
    if shape.is_empty() || shape.iter().any(|d| *d <= 0) {
        return Err(bad());
    }
    let elements = shape
        .iter()
        .try_fold(1u64, |n, d| n.checked_mul(*d as u64))
        .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "moe-resident-bytes"))?;
    let router =
        name.ends_with(".mlp.gate.weight") || name.ends_with(".mlp.shared_expert_gate.weight");
    let expert =
        name.ends_with(".mlp.experts.gate_up_proj") || name.ends_with(".mlp.experts.down_proj");
    let overflow = || Error::new(ErrorCode::ResourceCap, "moe-resident-bytes");
    if packed && ((shape.len() == 2 && !router) || expert) {
        let width = *shape.last().ok_or_else(bad)? as u64;
        if !width.is_multiple_of(128) {
            return Err(bad());
        }
        let rows = elements / width;
        elements
            .checked_add(rows.checked_mul(4).ok_or_else(overflow)?)
            .and_then(|n| n.checked_add(elements / 128 * 8))
            .ok_or_else(overflow)
    } else {
        elements.checked_mul(4).ok_or_else(overflow)
    }
}
pub fn stage_resident_bytes(
    layers: std::ops::Range<u32>,
    embedding: bool,
    head: bool,
    packed: bool,
) -> Result<u64> {
    stage_weights(layers, embedding, head)?
        .iter()
        .try_fold(0u64, |total, (name, shape)| {
            total
                .checked_add(resident_bytes(name, shape, packed)?)
                .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "moe-stage-bytes"))
        })
}
/// One contiguous resident stage of a split plan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StagePlan {
    pub layers: std::ops::Range<u32>,
    pub embedding: bool,
    pub head: bool,
    pub resident_bytes: u64,
}
/// Greedy minimum-stage contiguous split whose every stage fits `cap` resident bytes.
pub fn plan_stage_split(cap: u64, packed: bool) -> Result<Vec<StagePlan>> {
    let mut plan = Vec::new();
    let mut start = 0;
    while start < LAYERS {
        let embedding = start == 0;
        let mut best = None;
        for end in start + 1..=LAYERS {
            let head = end == LAYERS;
            let bytes = stage_resident_bytes(start..end, embedding, head, packed)?;
            if bytes > cap {
                break;
            }
            best = Some(StagePlan {
                layers: start..end,
                embedding,
                head,
                resident_bytes: bytes,
            });
        }
        let stage = best.ok_or_else(|| Error::new(ErrorCode::ResourceCap, "moe-stage-split"))?;
        start = stage.layers.end;
        plan.push(stage);
    }
    Ok(plan)
}
fn rows<T: Tensor>(x: &T) -> Result<i32> {
    let shape = x.shape();
    if shape.len() != 3
        || shape[0] != 1
        || !(1..=MAX_ROWS).contains(&shape[1])
        || shape[2] != HIDDEN
    {
        return Err(Error::new(ErrorCode::MalformedTensor, "moe-head-shape"));
    }
    Ok(shape[1])
}
/// Entry-stage lookup in the UNTIED embedding (`ids` is `[1, n]` uint32).
pub fn embed<T: MoeTensor>(ids: &T, weights: &BTreeMap<String, Weight<T>>) -> Result<T> {
    let shape = ids.shape();
    if shape.len() != 2 || shape[0] != 1 || !(1..=MAX_ROWS).contains(&shape[1]) {
        return Err(Error::new(ErrorCode::MalformedTensor, "moe-embed-ids"));
    }
    weights
        .get(EMBED_KEY)
        .filter(|w| w.shape() == [VOCAB, HIDDEN])
        .ok_or_else(|| Error::new(ErrorCode::Artifact, "moe-embed-weight"))?
        .embedding(ids)
}
/// Zero-centred final RMS norm (`model.language_model.norm.weight`, epsilon 1e-6).
pub fn final_norm<T: MoeTensor>(x: &T, weights: &BTreeMap<String, Weight<T>>) -> Result<T> {
    rows(x)?;
    let norm = weights
        .get(NORM_KEY)
        .filter(|w| w.shape() == [HIDDEN])
        .ok_or_else(|| Error::new(ErrorCode::Artifact, "moe-norm-weight"))?;
    hybrid::norm_offset(x, norm.float()?, NORM_EPSILON)
}
/// Final zero-centred norm then the UNTIED `lm_head`. Oracle: `hidden[:, -1:, :]`
/// unless `all_positions`. Never falls back to the embedding image.
pub fn head<T: MoeTensor>(
    x: &T,
    weights: &BTreeMap<String, Weight<T>>,
    all_positions: bool,
) -> Result<T> {
    let count = rows(x)?;
    let lm_head = weights
        .get(HEAD_KEY)
        .filter(|w| w.shape() == [VOCAB, HIDDEN])
        .ok_or_else(|| Error::new(ErrorCode::Artifact, "moe-untied-head-weight"))?;
    let hidden = if !all_positions && count > 1 {
        x.slice(&[0, count - 1, 0], &[1, count, HIDDEN])?
    } else {
        x.share()?
    };
    lm_head.linear(&final_norm(&hidden, weights)?)
}

/// Greedy selection over one logits row: lowest index among equal maxima (numpy/MLX
/// argmax), refusing a wrong vocabulary width or any non-finite value.
pub fn select_greedy(row: &[f32]) -> Result<u32> {
    if row.len() != VOCAB as usize || row.iter().any(|v| !v.is_finite()) {
        return Err(Error::new(ErrorCode::MalformedTensor, "moe-greedy-row"));
    }
    let mut best = 0;
    for (i, v) in row.iter().enumerate() {
        if *v > row[best] {
            best = i;
        }
    }
    u32::try_from(best).map_err(|_| Error::new(ErrorCode::MalformedTensor, "moe-greedy-row"))
}

/// Layer-local cursor, with private committed state. No rewind/slice import API.
/// The embedding/head, signed resource grants and production stage lifecycle are
/// deliberately not provided by this numerical component.
pub struct DecoderLayer<T> {
    layer: u32,
    position: i32,
    max_tokens: i32,
    state: Option<(T, T)>,
}
impl<T: MoeTensor> DecoderLayer<T> {
    pub fn new(layer: u32, max_tokens: u32) -> Result<Self> {
        layer_weights(layer)?;
        let max_tokens = i32::try_from(max_tokens)
            .ok()
            .filter(|n| *n > 0)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "moe-token-cap"))?;
        Ok(Self {
            layer,
            position: 0,
            max_tokens,
            state: None,
        })
    }
    pub fn position(&self) -> i32 {
        self.position
    }
    pub fn state(&self) -> Option<(&T, &T)> {
        self.state.as_ref().map(|(a, b)| (a, b))
    }
    pub fn reset(&mut self) {
        self.state = None;
        self.position = 0;
    }
    pub fn run(
        &mut self,
        x: &T,
        weights: &BTreeMap<String, Weight<T>>,
        position: i32,
    ) -> Result<BlockOutput<T>> {
        let step = self.compute(x, weights, position, true)?;
        Ok(self.commit(step))
    }
    /// Evaluates one layer without touching committed state. `exact` demands that `weights`
    /// is precisely this layer's inventory; a stage passes its own (already exact) larger map.
    pub(crate) fn compute(
        &self,
        x: &T,
        weights: &BTreeMap<String, Weight<T>>,
        position: i32,
        exact: bool,
    ) -> Result<LayerStep<T>> {
        let shape = x.shape();
        if shape.len() != 3 || shape[0] != 1 || !(1..=128).contains(&shape[1]) || shape[2] != 2048 {
            return Err(Error::new(ErrorCode::MalformedTensor, "moe-decoder-shape"));
        }
        if position != self.position {
            return Err(Error::new(ErrorCode::Position, "moe-no-rewind"));
        }
        let next = position
            .checked_add(shape[1])
            .filter(|n| *n <= self.max_tokens)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "moe-token-cap"))?;
        let wanted = layer_weights(self.layer)?;
        if (exact && wanted.len() != weights.len())
            || wanted
                .iter()
                .any(|(name, shape)| weights.get(name).is_none_or(|w| w.shape() != *shape))
        {
            return Err(Error::new(
                ErrorCode::MalformedTensor,
                "moe-decoder-weights",
            ));
        }
        let prefix = format!("{}layers.{}.", hybrid::PREFIX, self.layer);
        let get = |name: &str| {
            weights
                .get(&format!("{prefix}{name}"))
                .ok_or_else(|| Error::new(ErrorCode::Artifact, "moe-decoder-weight"))
        };
        let normalized = hybrid::norm_offset(x, get("input_layernorm.weight")?.float()?, 1e-6)?;
        let (mixed, state) = if hybrid::full_attention(self.layer) {
            hybrid::attention(&normalized, weights, &prefix, position, &self.state, 16)?
        } else {
            hybrid::delta(&normalized, weights, &prefix, &self.state, 32)?
        };
        let hidden = x.add(&mixed)?;
        let normalized = hybrid::norm_offset(
            &hidden,
            get("post_attention_layernorm.weight")?.float()?,
            1e-6,
        )?;
        let mut result = qwen36_block(&normalized, weights, self.layer)?;
        result.output = hidden.add(&result.output)?;
        // Never commit a lazy failed operation. Old state remains intact on error.
        result.output.eval()?;
        state.0.eval()?;
        state.1.eval()?;
        Ok(LayerStep {
            block: result,
            state,
            next,
        })
    }
    /// Infallible publication of an evaluated step.
    pub(crate) fn commit(&mut self, step: LayerStep<T>) -> BlockOutput<T> {
        self.state = Some(step.state);
        self.position = step.next;
        step.block
    }
}
/// An evaluated, uncommitted layer step.
pub(crate) struct LayerStep<T> {
    pub(crate) block: BlockOutput<T>,
    state: (T, T),
    next: i32,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn untied_embedding_and_head_are_distinct_owned_tensors() {
        let e = embedding_weights();
        let h = head_weights();
        assert_eq!(e.keys().collect::<Vec<_>>(), [EMBED_KEY]);
        assert_eq!(e[EMBED_KEY], [VOCAB, HIDDEN]);
        assert_eq!(h.keys().collect::<Vec<_>>(), [HEAD_KEY, NORM_KEY]);
        assert_eq!(h[HEAD_KEY], [VOCAB, HIDDEN]);
        assert_eq!(h[NORM_KEY], [HIDDEN]);
        assert!(e.keys().all(|k| !h.contains_key(k)), "no tied alias");
    }
    #[test]
    fn stage_inventory_follows_ownership_and_refuses_bad_ranges() {
        let first = stage_weights(0..20, true, false).unwrap();
        let last = stage_weights(20..40, false, true).unwrap();
        assert!(first.contains_key(EMBED_KEY));
        assert!(!first.contains_key(NORM_KEY) && !first.contains_key(HEAD_KEY));
        assert!(last.contains_key(NORM_KEY) && last.contains_key(HEAD_KEY));
        assert!(!last.contains_key(EMBED_KEY));
        assert!(
            first
                .keys()
                .all(|k| k == EMBED_KEY || k.contains(".layers."))
        );
        let layer_count = |m: &BTreeMap<String, Vec<i32>>| {
            m.keys()
                .filter_map(|k| {
                    k.split(".layers.")
                        .nth(1)?
                        .split('.')
                        .next()?
                        .parse::<u32>()
                        .ok()
                })
                .collect::<std::collections::BTreeSet<_>>()
                .len()
        };
        assert_eq!((layer_count(&first), layer_count(&last)), (20, 20));
        let all = stage_weights(0..40, true, true).unwrap();
        assert_eq!(all.len(), 30 * 18 + 10 * 15 + 3);
        for bad in [
            (0..0, false, false),
            (0..41, false, false),
            (1..5, true, false),
            (0..39, false, true),
        ] {
            assert_eq!(
                stage_weights(bad.0.clone(), bad.1, bad.2).unwrap_err().code,
                ErrorCode::InvalidDescriptor,
                "{bad:?}"
            );
        }
    }
    #[test]
    fn resident_bytes_follow_the_two_profiles() {
        let n = u64::try_from(VOCAB).unwrap() * 2048;
        assert_eq!(
            resident_bytes(EMBED_KEY, &[VOCAB, HIDDEN], false).unwrap(),
            n * 4
        );
        // codes + one f32 row scale + 16 groups of (scale, bias) per row.
        assert_eq!(
            resident_bytes(HEAD_KEY, &[VOCAB, HIDDEN], true).unwrap(),
            n + 248_320 * 4 + 248_320 * 16 * 8
        );
        // 1-D norm weights stay float in the packed profile.
        assert_eq!(resident_bytes(NORM_KEY, &[HIDDEN], true).unwrap(), 2048 * 4);
        let experts = "model.language_model.layers.0.mlp.experts.gate_up_proj";
        assert_eq!(
            resident_bytes(experts, &[256, 1024, 2048], true).unwrap(),
            256 * 1024 * 2048 + 256 * 1024 * 4 + 256 * 1024 * 16 * 8
        );
        // Routers stay float even though they are 2-D.
        let gate = "model.language_model.layers.0.mlp.gate.weight";
        assert_eq!(
            resident_bytes(gate, &[256, 2048], true).unwrap(),
            256 * 2048 * 4
        );
        assert_eq!(
            resident_bytes(EMBED_KEY, &[VOCAB, 100], true)
                .unwrap_err()
                .code,
            ErrorCode::MalformedTensor
        );
    }
    #[test]
    fn greedy_selection_is_first_max_and_fails_closed() {
        let mut row = vec![0f32; VOCAB as usize];
        row[7] = 3.;
        row[9] = 3.;
        assert_eq!(select_greedy(&row).unwrap(), 7);
        row[VOCAB as usize - 1] = 4.;
        assert_eq!(select_greedy(&row).unwrap(), VOCAB as u32 - 1);
        row[3] = f32::NAN;
        assert_eq!(
            select_greedy(&row).unwrap_err().code,
            ErrorCode::MalformedTensor
        );
        row[3] = f32::INFINITY;
        assert_eq!(
            select_greedy(&row).unwrap_err().code,
            ErrorCode::MalformedTensor
        );
        assert_eq!(
            select_greedy(&row[1..]).unwrap_err().code,
            ErrorCode::MalformedTensor
        );
    }
    #[test]
    fn closed_layer_inventory_keeps_all_experts_and_layer_kind() {
        for layer in 0..40 {
            let w = layer_weights(layer).unwrap();
            let p = format!("{}layers.{layer}.", hybrid::PREFIX);
            assert!(w.keys().all(|k| k.starts_with(&p)));
            assert_eq!(
                w[&(p.clone() + "mlp.experts.gate_up_proj")],
                [256, 1024, 2048]
            );
            if layer % 4 == 3 {
                assert_eq!(w.len(), 15);
                assert_eq!(w[&(p + "self_attn.q_proj.weight")], [8192, 2048]);
            } else {
                assert_eq!(w.len(), 18);
                assert_eq!(w[&(p + "linear_attn.in_proj_qkv.weight")], [8192, 2048]);
            }
        }
        assert_eq!(
            layer_weights(40).unwrap_err().code,
            ErrorCode::InvalidDescriptor
        );
    }
}
