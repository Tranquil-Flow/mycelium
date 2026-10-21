// SPDX-License-Identifier: AGPL-3.0-only
//! Symmetric row-wise int8 weights, bit-identical to the NumPy serving
//! quantizer `numpy_runtime.quantize_qwen2_numpy_tensor`:
//!
//! ```text
//! scale = max(|row|) / 127            (f32)
//! scale = 1.0 if scale == 0
//! q     = clip(sign(x/scale) * floor(|x/scale| + 0.5), -127, 127)
//! ```
//!
//! Every step is one IEEE f32 operation in the same order NumPy evaluates it,
//! so values and scales match byte for byte (`tests/qwen2_parity.rs` checks
//! all seven matrices of a real Qwen2.5-0.5B layer and crafted edge rows).

use crate::bf16::{SourceDType, decode_into};

#[derive(Debug, Clone, PartialEq)]
pub struct Int8Rowwise {
    pub rows: usize,
    pub cols: usize,
    /// Row-major `rows x cols`.
    pub values: Vec<i8>,
    pub scales: Vec<f32>,
}

/// Quantize one row into `out`; returns the row scale. `row` must be finite.
pub fn quantize_row(row: &[f32], out: &mut [i8]) -> f32 {
    debug_assert_eq!(row.len(), out.len());
    // max is exact and order-independent for finite inputs.
    let max_abs = row.iter().fold(0.0f32, |m, v| m.max(v.abs()));
    let mut scale = max_abs / 127.0f32;
    if scale == 0.0 {
        scale = 1.0;
    }
    for (o, &x) in out.iter_mut().zip(row) {
        let scaled = x / scale;
        let magnitude = (scaled.abs() + 0.5f32).floor();
        // sign(0) = 0 in NumPy; a zero magnitude is 0 either way.
        let rounded = if scaled < 0.0 { -magnitude } else { magnitude };
        *o = rounded.clamp(-127.0, 127.0) as i8;
    }
    scale
}

impl Int8Rowwise {
    /// Quantize row-major f32 values.
    pub fn from_f32(rows: usize, cols: usize, values: &[f32]) -> Option<Self> {
        if rows.checked_mul(cols)? != values.len() || values.iter().any(|v| !v.is_finite()) {
            return None;
        }
        let mut q = vec![0i8; values.len()];
        let mut scales = Vec::with_capacity(rows);
        for (src, dst) in values.chunks_exact(cols.max(1)).zip(q.chunks_exact_mut(cols.max(1))) {
            scales.push(quantize_row(src, dst));
        }
        Some(Self { rows, cols, values: q, scales })
    }

    /// Stream upstream source bytes row by row: only one f32 row is ever
    /// materialised, as in `runtime_loader._load_rowwise_int8_weight`.
    /// `None` on a length mismatch or a non-finite source value.
    pub fn from_source(rows: usize, cols: usize, dtype: SourceDType, bytes: &[u8]) -> Option<Self> {
        let row_bytes = cols.checked_mul(dtype.bytes())?;
        if rows.checked_mul(row_bytes)? != bytes.len() || cols == 0 {
            return None;
        }
        let mut q = vec![0i8; rows * cols];
        let mut scales = Vec::with_capacity(rows);
        let mut row = vec![0.0f32; cols];
        for (src, dst) in bytes.chunks_exact(row_bytes).zip(q.chunks_exact_mut(cols)) {
            decode_into(dtype, src, &mut row);
            if row.iter().any(|v| !v.is_finite()) {
                return None;
            }
            scales.push(quantize_row(&row, dst));
        }
        Some(Self { rows, cols, values: q, scales })
    }

    pub fn row(&self, r: usize) -> &[i8] {
        &self.values[r * self.cols..(r + 1) * self.cols]
    }

    /// Resident bytes (values + scales).
    pub fn bytes(&self) -> usize {
        self.values.len() + 4 * self.scales.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounds_half_away_from_zero_and_handles_zero_rows() {
        let q = Int8Rowwise::from_f32(2, 4, &[127.0, 0.5, -0.5, -1.5, 0.0, 0.0, -0.0, 0.0]).unwrap();
        assert_eq!(q.values, vec![127, 1, -1, -2, 0, 0, 0, 0]);
        assert_eq!(q.scales, vec![1.0, 1.0]);
        assert!(Int8Rowwise::from_f32(1, 2, &[1.0, f32::NAN]).is_none());
        assert!(Int8Rowwise::from_f32(1, 3, &[1.0, 2.0]).is_none());
    }

    #[test]
    fn source_streaming_matches_f32_path() {
        let values: Vec<f32> = (0..24).map(|i| ((i * 37 % 11) as f32 - 5.0) * 0.125).collect();
        let bytes: Vec<u8> = values.iter().flat_map(|v| v.to_le_bytes()).collect();
        let a = Int8Rowwise::from_f32(4, 6, &values).unwrap();
        let b = Int8Rowwise::from_source(4, 6, SourceDType::F32, &bytes).unwrap();
        assert_eq!(a, b);
        assert!(Int8Rowwise::from_source(4, 6, SourceDType::F32, &bytes[1..]).is_none());
    }
}
