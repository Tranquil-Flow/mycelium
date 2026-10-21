// SPDX-License-Identifier: AGPL-3.0-only
//! Exact source-dtype decoding for upstream weight bytes.
//!
//! bf16 is the top half of an IEEE f32, so widening is a 16-bit shift and is
//! exact for every pattern (NaN payloads included). This is the same mapping
//! as `runtime_loader._decode_float_payload` (`(u16 as u32) << 16`).

/// Widen one bf16 bit pattern to f32, exactly.
#[inline]
pub fn bf16_to_f32(bits: u16) -> f32 {
    f32::from_bits(u32::from(bits) << 16)
}

/// Source dtypes a stage accepts from Safetensors. Anything else (F16, int,
/// pre-quantized) is refused rather than guessed at.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceDType {
    BF16,
    F32,
}

impl SourceDType {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "BF16" => Some(Self::BF16),
            "F32" => Some(Self::F32),
            _ => None,
        }
    }

    pub fn bytes(self) -> usize {
        match self {
            Self::BF16 => 2,
            Self::F32 => 4,
        }
    }
}

/// Decode little-endian source bytes into `out` (exactly `out.len()` values).
/// Returns `false` when the byte length does not match.
pub fn decode_into(dtype: SourceDType, bytes: &[u8], out: &mut [f32]) -> bool {
    if bytes.len() != out.len() * dtype.bytes() {
        return false;
    }
    match dtype {
        SourceDType::BF16 => {
            for (o, c) in out.iter_mut().zip(bytes.chunks_exact(2)) {
                *o = bf16_to_f32(u16::from_le_bytes([c[0], c[1]]));
            }
        }
        SourceDType::F32 => {
            for (o, c) in out.iter_mut().zip(bytes.chunks_exact(4)) {
                *o = f32::from_le_bytes([c[0], c[1], c[2], c[3]]);
            }
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_bf16_pattern_widens_exactly() {
        for bits in 0..=u16::MAX {
            let f = bf16_to_f32(bits);
            assert_eq!(f.to_bits(), u32::from(bits) << 16);
            // The f32 value round-trips back to the same bf16 by truncation.
            assert_eq!((f.to_bits() >> 16) as u16, bits);
        }
        assert_eq!(bf16_to_f32(0x3f80), 1.0);
        assert_eq!(bf16_to_f32(0xc000), -2.0);
        assert!(bf16_to_f32(0x7fc1).is_nan());
        assert_eq!(bf16_to_f32(0x7f80), f32::INFINITY);
    }

    #[test]
    fn decode_checks_length_and_endianness() {
        let mut out = [0.0f32; 2];
        assert!(decode_into(SourceDType::BF16, &[0x80, 0x3f, 0x00, 0xc0], &mut out));
        assert_eq!(out, [1.0, -2.0]);
        assert!(!decode_into(SourceDType::BF16, &[0x80, 0x3f], &mut out));
        let mut one = [0.0f32; 1];
        assert!(decode_into(SourceDType::F32, &1.5f32.to_le_bytes(), &mut one));
        assert_eq!(one, [1.5]);
        assert_eq!(SourceDType::parse("F16"), None);
    }
}
