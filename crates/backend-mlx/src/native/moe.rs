// SPDX-License-Identifier: AGPL-3.0-only
//! Narrow MLX-C operations for the separately qualified MoE component.
use super::*;
use model::{Weight, moe::MoeTensor};

impl Array {
    fn moe_u32(&self) -> Result<Self> {
        self.out("moe-u32", |p| unsafe {
            mlx_astype(p, self.raw, mlx_dtype__MLX_UINT32, self.ctx.stream)
        })
    }
    fn moe_scalar(&self, value: u32) -> Result<Self> {
        self.ctx.words(&[value], &[1])?.reshape(&[])
    }
}
impl MoeTensor for Array {
    fn route_top_k(&self, k: i32) -> Result<(Self, Self)> {
        let shape = self.shape();
        if shape.len() != 2 || shape.iter().any(|d| *d <= 0) || k <= 0 || k > shape[1] {
            return Err(Error::new(ErrorCode::MalformedTensor, "moe-route-shape"));
        }
        let top = self.out("moe-topk", |p| unsafe {
            mlx_topk_axis(p, self.raw, k, -1, self.ctx.stream)
        })?;
        let threshold = top.out("moe-threshold", |p| unsafe {
            mlx_min_axis(p, top.raw, -1, true, self.ctx.stream)
        })?;
        let greater = self.out("moe-greater", |p| unsafe {
            mlx_greater(p, self.raw, threshold.raw, self.ctx.stream)
        })?;
        let tied = self.out("moe-tied", |p| unsafe {
            mlx_equal(p, self.raw, threshold.raw, self.ctx.stream)
        })?;
        let greater = greater.moe_u32()?;
        let count = greater.sum()?;
        let top_count = self.moe_scalar(k as u32)?;
        let needed = top_count.out("moe-needed", |p| unsafe {
            mlx_subtract(p, top_count.raw, count.raw, self.ctx.stream)
        })?;
        let tied_u32 = tied.moe_u32()?;
        let ordinal = tied_u32.out("moe-tie-order", |p| unsafe {
            mlx_cumsum_axis(
                p,
                tied_u32.raw,
                -1,
                false,
                true,
                mlx_optional_dtype {
                    value: mlx_dtype__MLX_UINT32,
                    has_value: true,
                },
                self.ctx.stream,
            )
        })?;
        let within = needed.ge(&ordinal)?;
        let selected_ties = tied.out("moe-selected-ties", |p| unsafe {
            mlx_logical_and(p, tied.raw, within.raw, self.ctx.stream)
        })?;
        let selected = greater.out("moe-selected", |p| unsafe {
            mlx_logical_or(p, greater.raw, selected_ties.raw, self.ctx.stream)
        })?;
        let expert_ids: Vec<u32> = (0..shape[1] as u32).collect();
        let expert_ids = self.ctx.words(&expert_ids, &[shape[1]])?;
        let order = selected.select(
            &expert_ids,
            &expert_ids.add(&self.moe_scalar(shape[1] as u32)?)?,
        )?;
        let order = order.out("moe-argsort", |p| unsafe {
            mlx_argsort_axis(p, order.raw, -1, self.ctx.stream)
        })?;
        let indices = order.slice(&[0, 0], &[shape[0], k])?;
        let scores = self.out("moe-selected-scores", |p| unsafe {
            mlx_take_along_axis(p, self.raw, indices.raw, -1, self.ctx.stream)
        })?;
        // The ascending ids are only an intermediate. The oracle then computes
        // pairwise rank: descending probability, ties to the low expert id.
        let mine = scores.reshape(&[shape[0], k, 1])?;
        let other = scores.reshape(&[shape[0], 1, k])?;
        let beaten = other.out("moe-rank-greater", |p| unsafe {
            mlx_greater(p, other.raw, mine.raw, self.ctx.stream)
        })?;
        let equal = other.out("moe-rank-equal", |p| unsafe {
            mlx_equal(p, other.raw, mine.raw, self.ctx.stream)
        })?;
        let slots: Vec<u32> = (0..k as u32).collect();
        let slots = self.ctx.words(&slots, &[k])?;
        let column = slots.reshape(&[1, k])?;
        let row = slots.reshape(&[k, 1])?;
        let earlier = column.out("moe-rank-earlier", |p| unsafe {
            mlx_less(p, column.raw, row.raw, self.ctx.stream)
        })?;
        let earlier_equal = equal.out("moe-rank-tie", |p| unsafe {
            mlx_logical_and(p, equal.raw, earlier.raw, self.ctx.stream)
        })?;
        let beaten = beaten.out("moe-rank-beaten", |p| unsafe {
            mlx_logical_or(p, beaten.raw, earlier_equal.raw, self.ctx.stream)
        })?;
        let rank = beaten.moe_u32()?.sum()?.reshape(&[shape[0], k])?;
        let by_rank = rank.out("moe-rank-order", |p| unsafe {
            mlx_argsort_axis(p, rank.raw, -1, self.ctx.stream)
        })?;
        let indices = indices.out("moe-ranked-indices", |p| unsafe {
            mlx_take_along_axis(p, indices.raw, by_rank.raw, -1, self.ctx.stream)
        })?;
        let scores = scores.out("moe-ranked-scores", |p| unsafe {
            mlx_take_along_axis(p, scores.raw, by_rank.raw, -1, self.ctx.stream)
        })?;
        let normalized = scores.div(&scores.sum()?)?;
        Ok((normalized, indices))
    }
    fn expert_product(&self, weight: &Weight<Self>, indices: &Self) -> Result<Self> {
        let xshape = self.shape();
        let wshape = weight.shape();
        let ishape = indices.shape();
        if xshape.len() != 3
            || wshape.len() != 3
            || ishape.len() != 2
            || xshape.iter().chain(&wshape).chain(&ishape).any(|d| *d <= 0)
            || xshape[0] != ishape[0]
            || (xshape[1] != 1 && xshape[1] != ishape[1])
            || xshape[2] != wshape[2]
        {
            return Err(Error::new(ErrorCode::MalformedTensor, "moe-expert-shape"));
        }
        // A bounded host check prevents arbitrary public indices reaching native
        // gather kernels. Routing arithmetic itself remains entirely on device.
        if indices.words()?.iter().any(|v| *v >= wshape[0] as u32) {
            return Err(Error::new(ErrorCode::MalformedTensor, "moe-expert-index"));
        }
        let expanded = self.reshape(&[xshape[0], xshape[1], 1, xshape[2]])?;
        let null = mlx_array {
            ctx: std::ptr::null_mut(),
        };
        let result = match weight {
            Weight::Float(w) => {
                let transposed = w.transpose(&[0, 2, 1])?;
                expanded.out("moe-gather-mm", |p| unsafe {
                    mlx_gather_mm(
                        p,
                        expanded.raw,
                        transposed.raw,
                        null,
                        indices.raw,
                        false,
                        self.ctx.stream,
                    )
                })?
            }
            Weight::Packed {
                codes,
                group_scales,
                group_biases,
                scales,
            } => {
                let groups = [wshape[0], wshape[1], wshape[2] / 128];
                if wshape[2] % 128 != 0
                    || group_scales.shape() != groups
                    || group_biases.shape() != groups
                    || scales.shape() != wshape[..2]
                {
                    return Err(Error::new(ErrorCode::MalformedTensor, "moe-packed-shape"));
                }
                let product = expanded.out("moe-gather-qmm", |p| unsafe {
                    mlx_gather_qmm(
                        p,
                        expanded.raw,
                        codes.raw,
                        group_scales.raw,
                        group_biases.raw,
                        null,
                        indices.raw,
                        true,
                        mlx_optional_int {
                            value: 128,
                            has_value: true,
                        },
                        mlx_optional_int {
                            value: 8,
                            has_value: true,
                        },
                        c"affine".as_ptr(),
                        null,
                        false,
                        self.ctx.stream,
                    )
                })?;
                return product.reshape(&[xshape[0], ishape[1], wshape[1]]);
            }
        };
        result.reshape(&[xshape[0], ishape[1], wshape[1]])
    }
    fn sum_experts(&self) -> Result<Self> {
        if self.shape().len() != 3 {
            return Err(Error::new(ErrorCode::MalformedTensor, "moe-sum-shape"));
        }
        self.out("moe-sum-experts", |p| unsafe {
            mlx_sum_axis(p, self.raw, 1, false, self.ctx.stream)
        })
    }
}
