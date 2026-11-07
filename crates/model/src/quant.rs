// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned v0.1.4 numpy_runtime.py:141-149 symmetric row quantizer.
use stage_contract::{Error, ErrorCode, Result};

/// Packs one or more complete rows, never retaining a signed-code image.
pub fn pack_rows(input: &[f32], width: usize) -> Result<(Vec<u32>, Vec<f32>)> {
    if width == 0
        || !width.is_multiple_of(128)
        || input.is_empty()
        || !input.len().is_multiple_of(width)
        || input.iter().any(|v| !v.is_finite())
    {
        return Err(Error::new(ErrorCode::MalformedTensor, "quantize-rows"));
    }
    let mut packed = Vec::with_capacity(input.len() / 4);
    let mut scales = Vec::with_capacity(input.len() / width);
    for row in input.chunks_exact(width) {
        let max = row.iter().fold(0f32, |a, v| a.max(v.abs()));
        let scale = max / 127.;
        let scale = if scale == 0. { 1. } else { scale };
        scales.push(scale);
        for four in row.chunks_exact(4) {
            let mut bytes = [0; 4];
            for (dst, value) in bytes.iter_mut().zip(four) {
                let scaled = value / scale;
                let rounded = scaled.signum() * (scaled.abs() + 0.5).floor();
                let code = rounded.clamp(-127., 127.) as i8;
                *dst = (code as u8) ^ 0x80;
            }
            packed.push(u32::from_le_bytes(bytes));
        }
    }
    Ok((packed, scales))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signed_half_away_rounding_offset_binary_and_zero_scale() {
        let mut input = vec![0.; 256];
        input[..8].copy_from_slice(&[127., -127., 0.5, -0.5, 1.5, -1.5, 2.5, -2.5]);
        let (words, scales) = pack_rows(&input, 128).unwrap();
        assert_eq!(scales, [1., 1.]);
        assert_eq!(words[0].to_le_bytes(), [255, 1, 129, 127]);
        assert_eq!(words[1].to_le_bytes(), [130, 126, 131, 125]);
        assert!(words[2..].iter().all(|w| *w == 0x80808080));
    }
    #[test]
    fn malformed_and_nonfinite_rows_refuse() {
        for (input, width) in [
            (vec![], 128),
            (vec![0.; 128], 0),
            (vec![0.; 127], 127),
            (vec![0.; 129], 128),
            (vec![f32::NAN; 128], 128),
            (vec![f32::INFINITY; 128], 128),
        ] {
            assert_eq!(
                pack_rows(&input, width).unwrap_err().code,
                ErrorCode::MalformedTensor
            );
        }
    }
}
