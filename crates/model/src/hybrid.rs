// SPDX-License-Identifier: AGPL-3.0-only
//! Closed Qwen3.5-0.8B text-only hybrid port. Ordered recurrence; no rewind.
use crate::{DenseConfig, Kv, Tensor, Weight, linear, rms_epsilon, rope};
use stage_contract::{Error, ErrorCode, Result, StageDescriptor};
use std::collections::BTreeMap;
pub const PREFIX: &str = "model.language_model.";
pub fn full_attention(layer: u32) -> bool {
    layer % 4 == 3
}
pub fn weights(c: DenseConfig, d: &StageDescriptor) -> Result<BTreeMap<String, Vec<i32>>> {
    d.validate(c.layers)?;
    let mut out = BTreeMap::new();
    if d.embedding || d.head {
        out.insert(
            format!("{PREFIX}embed_tokens.weight"),
            vec![c.vocab, c.hidden],
        );
    }
    if d.head {
        out.insert(format!("{PREFIX}norm.weight"), vec![c.hidden]);
    }
    for i in d.layers.clone() {
        let mut w = vec![
            ("input_layernorm.weight", vec![1024]),
            ("post_attention_layernorm.weight", vec![1024]),
            ("mlp.gate_proj.weight", vec![3584, 1024]),
            ("mlp.up_proj.weight", vec![3584, 1024]),
            ("mlp.down_proj.weight", vec![1024, 3584]),
        ];
        if full_attention(i) {
            w.extend([
                ("self_attn.q_proj.weight", vec![4096, 1024]),
                ("self_attn.k_proj.weight", vec![512, 1024]),
                ("self_attn.v_proj.weight", vec![512, 1024]),
                ("self_attn.o_proj.weight", vec![1024, 2048]),
                ("self_attn.q_norm.weight", vec![256]),
                ("self_attn.k_norm.weight", vec![256]),
            ]);
        } else {
            w.extend([
                ("linear_attn.in_proj_qkv.weight", vec![6144, 1024]),
                ("linear_attn.in_proj_z.weight", vec![2048, 1024]),
                ("linear_attn.in_proj_a.weight", vec![16, 1024]),
                ("linear_attn.in_proj_b.weight", vec![16, 1024]),
                ("linear_attn.out_proj.weight", vec![1024, 2048]),
                ("linear_attn.conv1d.weight", vec![6144, 1, 4]),
                ("linear_attn.dt_bias", vec![16]),
                ("linear_attn.A_log", vec![16]),
                ("linear_attn.norm.weight", vec![128]),
            ]);
        }
        for (name, shape) in w {
            out.insert(format!("{PREFIX}layers.{i}.{name}"), shape);
        }
    }
    Ok(out)
}
pub fn norm_offset<T: Tensor>(x: &T, w: &T, epsilon: f32) -> Result<T> {
    rms_epsilon(x, &w.add(&w.scalar(1.)?)?, epsilon)
}
fn partial_rope<T: Tensor>(x: &T, pos: i32) -> Result<T> {
    let s = x.shape();
    let r = x.slice(&[0, 0, 0, 0], &[s[0], s[1], s[2], 64])?;
    rope(&r, pos, 10_000_000.)?.cat(&x.slice(&[0, 0, 0, 64], &s)?, 3)
}
type Weights<T> = BTreeMap<String, Weight<T>>;
fn weight<'a, T: Tensor>(w: &'a Weights<T>, p: &str, n: &str) -> Result<&'a Weight<T>> {
    w.get(&format!("{p}{n}"))
        .ok_or_else(|| Error::new(ErrorCode::Artifact, "hybrid-weight"))
}
pub(crate) fn attention<T: Tensor>(
    x: &T,
    w: &Weights<T>,
    p: &str,
    pos: i32,
    past: &Option<(T, T)>,
    heads: i32,
) -> Result<(T, (T, T))> {
    let seq = x.shape()[1];
    let get = |n| weight(w, p, n);
    let projected = linear(x, get("self_attn.q_proj.weight")?)?.reshape(&[1, seq, heads, 512])?;
    let query = projected.slice(&[0, 0, 0, 0], &[1, seq, heads, 256])?;
    let gate = projected
        .slice(&[0, 0, 0, 256], &[1, seq, heads, 512])?
        .reshape(&[1, seq, heads * 256])?;
    let query = norm_offset(&query, get("self_attn.q_norm.weight")?.float()?, 1e-6)?
        .transpose(&[0, 2, 1, 3])?;
    let key = linear(x, get("self_attn.k_proj.weight")?)?.reshape(&[1, seq, 2, 256])?;
    let key = norm_offset(&key, get("self_attn.k_norm.weight")?.float()?, 1e-6)?
        .transpose(&[0, 2, 1, 3])?;
    let value = linear(x, get("self_attn.v_proj.weight")?)?
        .reshape(&[1, seq, 2, 256])?
        .transpose(&[0, 2, 1, 3])?;
    let query = partial_rope(&query, pos)?;
    let key = partial_rope(&key, pos)?;
    let (key, value) = match past {
        Some((k, v)) => (k.cat(&key, 2)?, v.cat(&value, 2)?),
        None => (key, value),
    };
    let scores = query
        .matmul(&key.repeat(heads / 2, 1)?.transpose(&[0, 1, 3, 2])?)?
        .div(&x.scalar(16.)?)?;
    let mask = x
        .range(pos, pos + seq, 1)?
        .reshape(&[seq, 1])?
        .ge(&x.range(0, pos + seq, 1)?.reshape(&[1, pos + seq])?)?
        .reshape(&[1, 1, seq, pos + seq])?;
    let prob = mask
        .select(&scores, &x.scalar(f32::NEG_INFINITY)?)?
        .softmax()?;
    let attended = prob
        .matmul(&value.repeat(heads / 2, 1)?)?
        .transpose(&[0, 2, 1, 3])?
        .reshape(&[1, seq, heads * 256])?;
    Ok((
        linear(
            &attended.mul(&gate.sigmoid()?)?,
            get("self_attn.o_proj.weight")?,
        )?,
        (key, value),
    ))
}
pub(crate) fn delta<T: Tensor>(
    x: &T,
    w: &Weights<T>,
    p: &str,
    past: &Option<(T, T)>,
    hv: i32,
) -> Result<(T, (T, T))> {
    let seq = x.shape()[1];
    let value_dim = hv * 128;
    let conv_dim = 4096 + value_dim;
    let get = |n| weight(w, p, n);
    let mixed = linear(x, get("linear_attn.in_proj_qkv.weight")?)?;
    let z = linear(x, get("linear_attn.in_proj_z.weight")?)?;
    let a = linear(x, get("linear_attn.in_proj_a.weight")?)?;
    let b = linear(x, get("linear_attn.in_proj_b.weight")?)?;
    let (conv, state) = match past {
        Some((c, s)) => (c.share()?, s.share()?),
        None => (
            mixed
                .slice(&[0, 0, 0], &[1, 1, conv_dim])?
                .zeros()?
                .repeat(3, 1)?,
            x.scalar(0.)?
                .reshape(&[1, 1, 1, 1])?
                .repeat(hv, 1)?
                .repeat(128, 2)?
                .repeat(128, 3)?,
        ),
    };
    let input = conv.cat(&mixed, 1)?;
    // Copy the three-row tail: a slice view would retain the full prefill
    // backing allocation despite the fixed logical convolution state size.
    let next_conv = input.slice(&[0, seq, 0], &[1, seq + 3, conv_dim])?.copy()?;
    let kernel = get("linear_attn.conv1d.weight")?
        .float()?
        .reshape(&[conv_dim, 4])?
        .transpose(&[1, 0])?;
    let tap = |t| -> Result<T> {
        input.slice(&[0, t, 0], &[1, t + seq, conv_dim])?.mul(
            &kernel
                .slice(&[t, 0], &[t + 1, conv_dim])?
                .reshape(&[conv_dim])?,
        )
    };
    let mut conv = tap(0)?;
    for t in 1..4 {
        conv = conv.add(&tap(t)?)?;
    }
    let conv = conv.mul(&conv.sigmoid()?)?;
    let q = conv
        .slice(&[0, 0, 0], &[1, seq, 2048])?
        .reshape(&[1, seq, 16, 128])?;
    let k = conv
        .slice(&[0, 0, 2048], &[1, seq, 4096])?
        .reshape(&[1, seq, 16, 128])?;
    let v = conv
        .slice(&[0, 0, 4096], &[1, seq, conv_dim])?
        .reshape(&[1, seq, hv, 128])?;
    let (q, k) = if hv == 16 {
        (q, k)
    } else {
        (q.repeat(hv / 16, 2)?, k.repeat(hv / 16, 2)?)
    };
    let l2 = |v: &T| v.mul(&v.square()?.sum()?.add(&v.scalar(1e-6)?)?.rsqrt()?);
    let q = l2(&q)?.mul(&q.scalar(128f64.powf(-0.5) as f32)?)?;
    let k = l2(&k)?;
    let beta = b.sigmoid()?;
    let dt = a.add(get("linear_attn.dt_bias")?.float()?)?;
    let softplus = dt.logaddexp(&dt.zeros()?)?;
    let decay = get("linear_attn.A_log")?
        .float()?
        .exp()?
        .neg()?
        .mul(&softplus)?
        .exp()?;
    let mut state = state;
    let mut output: Option<T> = None;
    for t in 0..seq {
        let row = |v: &T| {
            v.slice(&[0, t, 0, 0], &[1, t + 1, hv, 128])?
                .reshape(&[1, hv, 128])
        };
        let qt = row(&q)?;
        let kt = row(&k)?;
        let decay = decay
            .slice(&[0, t, 0], &[1, t + 1, hv])?
            .reshape(&[1, hv, 1, 1])?;
        state = state.mul(&decay)?;
        let remembered = state
            .mul(&kt.reshape(&[1, hv, 1, 128])?)?
            .sum()?
            .reshape(&[1, hv, 128])?;
        let beta = beta
            .slice(&[0, t, 0], &[1, t + 1, hv])?
            .reshape(&[1, hv, 1])?;
        let delta = row(&v)?.add(&remembered.neg()?)?.mul(&beta)?;
        state = state.add(
            &delta
                .reshape(&[1, hv, 128, 1])?
                .mul(&kt.reshape(&[1, hv, 1, 128])?)?,
        )?;
        let step = state
            .mul(&qt.reshape(&[1, hv, 1, 128])?)?
            .sum()?
            .reshape(&[1, 1, hv, 128])?;
        output = Some(match output {
            None => step,
            Some(o) => o.cat(&step, 1)?,
        });
    }
    let core = output.ok_or_else(|| Error::new(ErrorCode::MalformedTensor, "empty-hybrid"))?;
    let core = rms_epsilon(&core, get("linear_attn.norm.weight")?.float()?, 1e-6)?;
    let z = z.reshape(&[1, seq, hv, 128])?;
    let core = core
        .mul(&z.mul(&z.sigmoid()?)?)?
        .reshape(&[1, seq, value_dim])?;
    Ok((
        linear(&core, get("linear_attn.out_proj.weight")?)?,
        (next_conv, state),
    ))
}
pub fn forward<T: Tensor>(
    d: &StageDescriptor,
    mut x: T,
    w: &Weights<T>,
    past: &Kv<T>,
    pos: i32,
) -> Result<(T, Kv<T>)> {
    d.validate(24)?;
    let shape = x.shape();
    if shape.len() != 3
        || shape[0] != 1
        || shape[1] <= 0
        || shape[2] != 1024
        || pos < 0
        || pos.checked_add(shape[1]).is_none()
        || past.len() != (d.layers.end - d.layers.start) as usize
    {
        return Err(Error::new(ErrorCode::MalformedTensor, "hybrid-shape"));
    }
    for (i, p) in d.layers.clone().zip(past) {
        let valid = match p {
            None => pos == 0,
            Some((a, b)) => {
                pos > 0
                    && if full_attention(i) {
                        a.shape() == [1, 2, pos, 256] && b.shape() == [1, 2, pos, 256]
                    } else {
                        a.shape() == [1, 3, 6144] && b.shape() == [1, 16, 128, 128]
                    }
            }
        };
        if !valid {
            return Err(Error::new(ErrorCode::MalformedTensor, "hybrid-state"));
        }
    }
    for (name, shape) in weights(DenseConfig::for_descriptor(d)?, d)? {
        if w.get(&name).is_none_or(|v| v.shape() != shape) {
            return Err(Error::new(
                ErrorCode::MalformedTensor,
                "hybrid-weight-shape",
            ));
        }
    }
    let mut next = Vec::new();
    for (i, past) in d.layers.clone().zip(past) {
        let p = format!("{PREFIX}layers.{i}.");
        let get = |n| weight(w, &p, n);
        let norm = norm_offset(&x, get("input_layernorm.weight")?.float()?, 1e-6)?;
        let (mixed, state) = if full_attention(i) {
            attention(&norm, w, &p, pos, past, 8)?
        } else {
            delta(&norm, w, &p, past, 16)?
        };
        x = x.add(&mixed)?;
        let norm = norm_offset(&x, get("post_attention_layernorm.weight")?.float()?, 1e-6)?;
        let gate = linear(&norm, get("mlp.gate_proj.weight")?)?;
        let up = linear(&norm, get("mlp.up_proj.weight")?)?;
        x = x.add(&linear(
            &gate.mul(&gate.sigmoid()?)?.mul(&up)?,
            get("mlp.down_proj.weight")?,
        )?)?;
        // Materialize ordered recurrent output before retaining it; no replay by slicing.
        state.0.eval()?;
        state.1.eval()?;
        x.eval()?;
        next.push(Some(state));
    }
    Ok((x, next))
}
