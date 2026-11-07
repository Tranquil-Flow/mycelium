// SPDX-License-Identifier: AGPL-3.0-only
//! Assigned-only streaming BF16 -> packed int8 loader. No full floating-weight
//! or signed-code image is constructed, including during loading.
use crate::native::{Array, Context};
use model::{Tensor, Weight, moe::MoeTensor, quant::pack_rows};
use stage_contract::{Error, ErrorCode, Result};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{Read, Seek, SeekFrom},
    rc::Rc,
};

pub fn load_packed(
    ctx: &Rc<Context>,
    path: &str,
    wanted: &BTreeMap<String, Vec<i32>>,
) -> Result<BTreeMap<String, Weight<Array>>> {
    let bad = || Error::new(ErrorCode::Artifact, "packed-source");
    let mut f = File::open(path).map_err(|_| bad())?;
    let file_bytes = f.metadata().map_err(|_| bad())?.len();
    let mut bytes = [0; 8];
    f.read_exact(&mut bytes).map_err(|_| bad())?;
    let header_bytes = u64::from_le_bytes(bytes);
    if header_bytes > 1024 * 1024 {
        return Err(bad());
    }
    let mut header = vec![0; header_bytes as usize];
    f.read_exact(&mut header).map_err(|_| bad())?;
    let header: serde_json::Value = serde_json::from_slice(&header).map_err(|_| bad())?;
    let data_offset = 8 + header_bytes;
    let mut result = BTreeMap::new();
    let mut verified = BTreeSet::new();
    for (name, shape) in wanted {
        let meta = &header[name];
        let unit = match meta["dtype"].as_str() {
            Some("BF16") => 2,
            Some("F32") => 4,
            _ => return Err(bad()),
        };
        if meta["shape"] != serde_json::json!(shape) {
            return Err(bad());
        }
        let start = meta["data_offsets"][0].as_u64().ok_or_else(bad)?;
        let end = meta["data_offsets"][1].as_u64().ok_or_else(bad)?;
        let elements = shape
            .iter()
            .try_fold(1usize, |n, d| {
                usize::try_from(*d)
                    .ok()
                    .filter(|v| *v > 0)
                    .and_then(|v| n.checked_mul(v))
            })
            .ok_or_else(bad)?;
        if end.checked_sub(start) != (elements as u64).checked_mul(unit as u64)
            || data_offset.checked_add(end).is_none_or(|v| v > file_bytes)
        {
            return Err(bad());
        }
        f.seek(SeekFrom::Start(data_offset + start))
            .map_err(|_| bad())?;
        let router =
            name.ends_with(".mlp.gate.weight") || name.ends_with(".mlp.shared_expert_gate.weight");
        let expert = shape.len() == 3
            && (name.ends_with(".mlp.experts.gate_up_proj")
                || name.ends_with(".mlp.experts.down_proj"));
        let value = if (shape.len() == 2 && !router) || expert {
            let width = shape[shape.len() - 1] as usize;
            let rows = elements / width;
            if !width.is_multiple_of(128) {
                return Err(Error::new(ErrorCode::Unsupported, "packed-width"));
            }
            let chunk_rows = (32 * 1024 * 1024 / (width * 4)).max(1);
            let mut codes = Vec::with_capacity(elements / 4);
            let mut scales = Vec::with_capacity(rows);
            for first in (0..rows).step_by(chunk_rows) {
                let count = (rows - first).min(chunk_rows) * width;
                let input = read_values(&mut f, count, unit)?;
                let (packed, row_scales) = pack_rows(&input, width)?;
                codes.extend(packed);
                scales.extend(row_scales);
            }
            let mut packed_shape = shape.clone();
            packed_shape[shape.len() - 1] /= 4;
            let packed = ctx.words(&codes, &packed_shape)?;
            let scales = ctx.floats(&scales, &shape[..shape.len() - 1])?;
            drop(codes);
            let groups = elements / 128;
            let mut group_shape = shape.clone();
            group_shape[shape.len() - 1] /= 128;
            let group_scales = ctx.floats(&vec![1.; groups], &group_shape)?;
            let group_biases = ctx.floats(&vec![-128.; groups], &group_shape)?;
            for a in [&packed, &scales, &group_scales, &group_biases] {
                a.eval()?;
            }
            let weight = Weight::Packed {
                codes: packed,
                scales,
                group_scales,
                group_biases,
            };
            if verified.insert(shape.clone()) {
                let independent = if expert {
                    expert_rows_are_independent(ctx, &weight, shape)?
                } else {
                    weight.rows_are_independent(&ctx.normal_probe(shape[1])?)?
                };
                if !independent {
                    return Err(Error::new(
                        ErrorCode::Unsupported,
                        "packed-rows-not-independent",
                    ));
                }
            }
            weight
        } else {
            let a = ctx.floats(&read_values(&mut f, elements, unit)?, shape)?;
            a.eval()?;
            Weight::Float(a)
        };
        result.insert(name.clone(), value);
    }
    ctx.synchronize()?;
    ctx.healthy()?;
    Ok(result)
}
fn expert_rows_are_independent(
    ctx: &Rc<Context>,
    weight: &Weight<Array>,
    shape: &[i32],
) -> Result<bool> {
    let probe = ctx.normal_expert_probe(shape[2])?;
    let indices: Vec<u32> = (0..130).map(|n| n % shape[0] as u32).collect();
    let indices = ctx.words(&indices, &[65, 2])?;
    let reference = probe.expert_product(weight, &indices)?;
    reference.eval()?;
    for rows in [1, 2, 8, 32, 65] {
        let partial = probe
            .slice(&[0, 0, 0], &[rows, 1, shape[2]])?
            .expert_product(weight, &indices.slice(&[0, 0], &[rows, 2])?)?;
        if !partial.equal(&reference.slice(&[0, 0, 0], &[rows, 2, shape[1]])?)? {
            return Ok(false);
        }
    }
    Ok(true)
}
fn read_values(f: &mut File, count: usize, unit: usize) -> Result<Vec<f32>> {
    let mut bytes = vec![0; count * unit];
    f.read_exact(&mut bytes)
        .map_err(|_| Error::new(ErrorCode::Artifact, "packed-read"))?;
    let values: Vec<f32> = bytes
        .chunks_exact(unit)
        .map(|b| {
            if unit == 2 {
                f32::from_bits(u32::from(u16::from_le_bytes([b[0], b[1]])) << 16)
            } else {
                f32::from_le_bytes([b[0], b[1], b[2], b[3]])
            }
        })
        .collect();
    if values.iter().any(|v| !v.is_finite()) {
        return Err(Error::new(ErrorCode::MalformedTensor, "packed-nonfinite"));
    }
    Ok(values)
}
