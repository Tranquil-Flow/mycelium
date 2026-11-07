// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Tensor, float_linear};
use stage_contract::{Error, ErrorCode, Result};

/// Exactly one resident representation. Packed weights have no signed/FP32 image.
pub enum Weight<T> {
    Float(T),
    Packed {
        codes: T,
        scales: T,
        group_scales: T,
        group_biases: T,
    },
}
impl<T: Tensor> Weight<T> {
    pub fn shape(&self) -> Vec<i32> {
        match self {
            Self::Float(a) => a.shape(),
            Self::Packed { codes, .. } => {
                let mut shape = codes.shape();
                if let Some(width) = shape.last_mut() {
                    *width *= 4;
                }
                shape
            }
        }
    }
    pub fn float(&self) -> Result<&T> {
        match self {
            Self::Float(a) => Ok(a),
            _ => Err(Error::new(
                ErrorCode::MalformedTensor,
                "expected-float-weight",
            )),
        }
    }
    pub fn embedding(&self, ids: &T) -> Result<T> {
        match self {
            Self::Float(a) => a.take(ids),
            Self::Packed {
                codes,
                scales,
                group_scales,
                group_biases,
            } => {
                let selected = codes.take(ids)?;
                let selected_scales = scales.take(ids)?;
                let mut shape = selected_scales.shape();
                shape.push(1);
                selected
                    .dequantize(&group_scales.take(ids)?, &group_biases.take(ids)?)?
                    .mul(&selected_scales.reshape(&shape)?)
            }
        }
    }
    /// The product before row scales, with v0.1.4's mandatory 32-row floor.
    pub fn product(&self, x: &T) -> Result<T> {
        let Self::Packed {
            codes,
            group_scales,
            group_biases,
            ..
        } = self
        else {
            return float_linear(x, self.float()?);
        };
        let shape = x.shape();
        let wshape = self.shape();
        if shape.len() < 2
            || wshape.len() != 2
            || shape.iter().any(|d| *d <= 0)
            || shape.last() != wshape.last()
        {
            return Err(Error::new(
                ErrorCode::MalformedTensor,
                "packed-linear-shape",
            ));
        }
        let rows = shape[..shape.len() - 1]
            .iter()
            .try_fold(1i32, |n, d| n.checked_mul(*d))
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "packed-linear-rows"))?;
        let width = wshape[1];
        let flat = x.reshape(&[rows, width])?;
        let flat = if rows < 32 {
            let zero = flat
                .slice(&[0, 0], &[1, width])?
                .zeros()?
                .repeat(32 - rows, 0)?;
            flat.cat(&zero, 0)?
        } else {
            flat
        };
        let product = flat.quantized_matmul(codes, group_scales, group_biases)?;
        let product = product.slice(&[0, 0], &[rows, wshape[0]])?;
        let mut out_shape = shape;
        *out_shape
            .last_mut()
            .ok_or_else(|| Error::new(ErrorCode::MalformedTensor, "packed-output"))? = wshape[0];
        product.reshape(&out_shape)
    }
    pub fn linear(&self, x: &T) -> Result<T> {
        match self {
            Self::Float(a) => float_linear(x, a),
            Self::Packed { scales, .. } => self.product(x)?.mul(scales),
        }
    }
    /// Per-shape admission uses the exact product-call sizes of v0.1.4.
    /// Caller supplies the seeded 129-row native probe; false MUST refuse.
    pub fn rows_are_independent(&self, probe: &T) -> Result<bool> {
        let shape = self.shape();
        if shape.len() != 2 || shape.iter().any(|d| *d <= 0) {
            return Err(Error::new(ErrorCode::MalformedTensor, "row-probe-weight"));
        }
        let width = shape[1];
        if probe.shape() != [129, width] {
            return Err(Error::new(ErrorCode::MalformedTensor, "row-probe-shape"));
        }
        let reference = self.product(probe)?;
        reference.eval()?;
        for rows in [1, 2, 8, 31, 32, 33, 65] {
            let actual = self.product(&probe.slice(&[0, 0], &[rows, width])?)?;
            if !actual.equal(&reference.slice(&[0, 0], &[rows, self.shape()[0]])?)? {
                return Ok(false);
            }
        }
        Ok(true)
    }
}
