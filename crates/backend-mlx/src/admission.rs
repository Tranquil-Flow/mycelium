// SPDX-License-Identifier: AGPL-3.0-only
use model::{DenseConfig, Qwen2};
use stage_contract::*;
pub const CHECKPOINT: &str = "fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe";
pub const RAW_HEADER: &str = "bd808652be103f0e4e29786b9aa8829e0f38607880486926c7246ee2de577a76";
// MANIFEST is the existing sorted-JSON manifest identity, not raw header bytes.
pub const MANIFEST: &str = "65d3f3bf8256224d5ecc7dc63e12c2211717a116cb98428648e5f1fe952a14d5";
pub const REVISION: &str = "7ae557604adf67be50417f59c2c2f167def9a775";
pub fn digest_hex(s: &str) -> Result<[u8; 32]> {
    if s.len() != 64 || !s.is_ascii() {
        return Err(Error::new(ErrorCode::Artifact, "digest-format"));
    }
    let mut out = [0; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16)
            .map_err(|_| Error::new(ErrorCode::Artifact, "digest-format"))?;
    }
    Ok(out)
}
pub fn qwen2_descriptor(
    layers: std::ops::Range<u32>,
    embedding: bool,
    head: bool,
) -> Result<StageDescriptor> {
    Ok(StageDescriptor {
        checkpoint: digest_hex(CHECKPOINT)?,
        revision: REVISION.into(),
        tensor_manifest: digest_hex(MANIFEST)?,
        semantics: "qwen2-v014-explicit-v1".into(),
        architecture: Architecture::Qwen2,
        layers,
        embedding,
        head,
        representation: "bf16-expanded-f32".into(),
        quantizer: "none".into(),
        profile: NumericsProfile::MlxExplicitF32,
        state_schema: "attention-kv-f32-v1".into(),
    })
}
pub fn qwen2_packed_descriptor(
    layers: std::ops::Range<u32>,
    embedding: bool,
    head: bool,
) -> Result<StageDescriptor> {
    let mut d = qwen2_descriptor(layers, embedding, head)?;
    d.profile = NumericsProfile::MlxPackedInt8;
    d.representation = "persistent_packed_int8".into();
    d.quantizer = "symmetric-rowwise-int8-half-away-v1".into();
    Ok(d)
}
pub const QWEN3_CHECKPOINT: &str =
    "f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b";
pub const QWEN3_RAW_HEADER: &str =
    "399d16f500e925c7e923fe05966c6df6862ab64da60916843119e802f1801bca";
pub fn qwen3_descriptor(
    layers: std::ops::Range<u32>,
    embedding: bool,
    head: bool,
    profile: NumericsProfile,
) -> Result<StageDescriptor> {
    let mut d = match profile {
        NumericsProfile::MlxExplicitF32 => qwen2_descriptor(layers, embedding, head)?,
        NumericsProfile::MlxPackedInt8 => qwen2_packed_descriptor(layers, embedding, head)?,
        _ => return Err(Error::new(ErrorCode::Unsupported, "qwen3-profile")),
    };
    d.checkpoint = digest_hex(QWEN3_CHECKPOINT)?;
    d.revision = "c1899de289a04d12100db370d81485cdf75e47ca".into();
    d.tensor_manifest =
        digest_hex("eea99c0eb1d5b35558409bd80c96624279199e1b4992f56e78c5e98c46e98daa")?;
    d.architecture = Architecture::Qwen3;
    d.semantics = "qwen3-v014-explicit-v1".into();
    Ok(d)
}
pub fn qwen35_descriptor(
    layers: std::ops::Range<u32>,
    embedding: bool,
    head: bool,
    profile: NumericsProfile,
) -> Result<StageDescriptor> {
    let mut d = qwen3_descriptor(layers, embedding, head, profile)?;
    d.checkpoint = digest_hex("04b1c301231dd422b8860db31311ab2721511346a32cb1e079c4c4e5f1fe4696")?;
    d.tensor_manifest =
        digest_hex("44db78c384ece32b3a1fcedd12334a0213489b26edd0b696c94ff5b0a0be0c97")?;
    d.revision = "2fc06364715b967f1860aea9cf38778875588b17".into();
    d.architecture = Architecture::Qwen35DenseHybrid;
    d.semantics = "qwen35-08b-v014-explicit-v1".into();
    d.state_schema = "hybrid-conv-deltanet-kv-f32-v1".into();
    Ok(d)
}
pub fn artifact_layout(d: &StageDescriptor) -> Result<(u64, &'static str)> {
    match d.architecture {
        Architecture::Qwen2 => Ok((988_097_824, RAW_HEADER)),
        Architecture::Qwen3 => Ok((1_503_300_328, QWEN3_RAW_HEADER)),
        Architecture::Qwen35DenseHybrid => Ok((
            1746942600,
            "c894b6055113f9d5a9c5c68e703662de8ba27df5a9631b47f10b75ac3fe0b083",
        )),
        _ => Err(Error::new(ErrorCode::Unsupported, "artifact-architecture")),
    }
}
/// Total layers of the pinned backend geometry; this does not infer a model.
pub fn total_layers(d: &StageDescriptor) -> Result<u32> {
    if d.architecture == Architecture::Qwen35MoeHybrid {
        crate::moe_admission::validate(d)?;
        Ok(model::moe_decoder::LAYERS)
    } else {
        Ok(DenseConfig::for_descriptor(d)?.layers)
    }
}
/// Returns assigned resident bytes before opening any artifact/native handle.
pub fn admit(d: &StageDescriptor, limits: Limits) -> Result<u64> {
    Ok(requirements(d, limits)?.resident.get())
}
/// Minimum logical reservation for this exact descriptor and context bound.
/// Signed authority/storage and physical allocator overhead belong to the caller.
pub fn requirements(d: &StageDescriptor, limits: Limits) -> Result<budgets::Reservation> {
    if d.architecture == Architecture::Qwen35MoeHybrid {
        return crate::moe_admission::requirements(d, limits);
    }
    let cfg = DenseConfig::for_descriptor(d)?;
    d.validate(cfg.layers)?;
    let packed = d.profile == NumericsProfile::MlxPackedInt8;
    let expected = if cfg.hybrid {
        qwen35_descriptor(d.layers.clone(), d.embedding, d.head, d.profile)?
    } else if d.architecture == Architecture::Qwen3 {
        qwen3_descriptor(d.layers.clone(), d.embedding, d.head, d.profile)?
    } else if packed {
        qwen2_packed_descriptor(d.layers.clone(), d.embedding, d.head)?
    } else {
        qwen2_descriptor(d.layers.clone(), d.embedding, d.head)?
    };
    if *d != expected {
        return Err(Error::new(ErrorCode::Unsupported, "model-profile-identity"));
    }
    let cap = || Error::new(ErrorCode::ResourceCap, "prepare-cap");
    if limits.max_tokens == 0 || limits.max_tokens > i32::MAX as u32 {
        return Err(cap());
    }
    let weights = Qwen2::weights(d)?;
    let bytes = weights
        .values()
        .try_fold(0u64, |total, shape| {
            let elements = shape
                .iter()
                .try_fold(1u64, |n, &dim| n.checked_mul(dim as u64))?;
            let tensor = if packed && shape.len() == 2 {
                elements
                    .checked_add(shape[0] as u64 * 4)?
                    .checked_add(elements / 128 * 8)?
            } else {
                elements.checked_mul(4)?
            };
            total.checked_add(tensor)
        })
        .ok_or_else(cap)?;
    let state = cfg.state_bytes_for(d.layers.clone(), limits.max_tokens)?;
    // Conservative explicit-op logical scratch reservation, not a measured
    // per-stage Metal peak. Native allocator cap is enforced by Context.
    let t = u64::from(limits.max_tokens);
    let compute_scratch = t
        .checked_mul(cfg.hidden as u64 * 20 + cfg.inner as u64 * 8 + cfg.heads as u64 * t * 4)
        .and_then(|n| n.checked_mul(4))
        .and_then(|n| n.checked_add(state)) // committed + replacement KV coexist
        .and_then(|n| {
            n.checked_add(if d.head {
                cfg.vocab as u64 * 4 * if packed { 64 } else { 2 }
            } else {
                0
            })
        })
        .ok_or_else(cap)?;
    // Lazy safetensors map retains the assigned BF16 image until all selected
    // FP32 casts finish. Reserve both images, not only steady resident bytes.
    // Streaming packed loader: at most one packed staging tensor and a 32 MiB
    // float chunk; reserve the oracle's conservative ten-copy conversion bound.
    // 129-row full-vocabulary probe is also below this reserve.
    let loading = if packed {
        weights
            .values()
            .map(|s| {
                let elements: u64 = s.iter().map(|v| *v as u64).product();
                if s.len() == 2 {
                    elements + s[0] as u64 * 4 + (elements * 4).min(32 * 1024 * 1024) * 10
                } else {
                    elements * 4 * 3
                }
            })
            .max()
            .unwrap_or(0)
    } else {
        bytes / 2
            + if cfg.hybrid {
                weights
                    .iter()
                    .filter(|(n, _)| n.ends_with(".A_log") || n.ends_with(".dt_bias"))
                    .map(|(_, s)| s[0] as u64 * 2)
                    .sum::<u64>()
            } else {
                0
            }
    };
    // Ordered DeltaNet builds up to max_tokens recurrence operations per layer.
    // Reserve a conservative nine full recurrent intermediates per position,
    // plus convolution/projection temporaries, in addition to dense scratch.
    // This is an explicit graph bound, not a measured allocator peak claim.
    let hybrid_scratch = if cfg.hybrid {
        t.checked_mul(9 * 16 * 128 * 128 * 4 + 16 * 6144 * 4)
            .and_then(|n| n.checked_add(compute_scratch))
            .ok_or_else(cap)?
    } else {
        compute_scratch
    };
    let scratch = hybrid_scratch.max(loading);
    if bytes > limits.weight_bytes || state > limits.state_bytes || scratch > limits.scratch_bytes {
        return Err(cap());
    }
    Ok(budgets::Reservation {
        staged: budgets::Bytes::ZERO,
        staging_overhead: budgets::Bytes::ZERO,
        resident: budgets::Bytes::new(bytes),
        scratch: budgets::Bytes::new(scratch),
        kv: budgets::Bytes::new(state),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    fn limits() -> Limits {
        Limits {
            weight_bytes: 4_000_000_000,
            state_bytes: 10_000_000,
            scratch_bytes: 512_000_000,
            max_tokens: 128,
        }
    }
    #[test]
    fn supported_and_unsupported_profiles_are_explicit() {
        let mut d = qwen2_descriptor(0..8, true, false).unwrap();
        assert!(admit(&d, limits()).unwrap() > 0);
        d.profile = NumericsProfile::MlxPackedInt8;
        assert_eq!(
            admit(&d, limits()).unwrap_err().code,
            ErrorCode::Unsupported
        );
    }
    #[test]
    fn f14_caps_refuse_before_artifact_io() {
        let d = qwen2_descriptor(0..8, true, false).unwrap();
        for which in 0..4 {
            let mut cap = limits();
            match which {
                0 => cap.weight_bytes = 1,
                1 => cap.state_bytes = 1,
                2 => cap.scratch_bytes = 1,
                _ => cap.max_tokens = 0,
            }
            assert_eq!(admit(&d, cap).unwrap_err().code, ErrorCode::ResourceCap);
        }
    }
    #[test]
    fn bf16_loading_scratch_is_reserved_before_io() {
        let d = qwen2_descriptor(8..24, false, true).unwrap();
        let mut cap = limits();
        // Sealed Qwen2.5-0.5B assigned tensor image: 749467392 BF16 bytes,
        // in addition to the expanded 1498934784-byte FP32 resident image.
        cap.scratch_bytes = 749_467_391;
        assert_eq!(admit(&d, cap).unwrap_err().code, ErrorCode::ResourceCap);
        cap.scratch_bytes = 749_467_392;
        assert_eq!(admit(&d, cap).unwrap(), 1_498_934_784);
    }
    #[test]
    fn packed_profile_has_its_own_identity_and_exact_residency() {
        let mut d = qwen2_descriptor(0..8, true, false).unwrap();
        d.profile = NumericsProfile::MlxPackedInt8;
        d.representation = "persistent_packed_int8".into();
        d.quantizer = "symmetric-rowwise-int8-half-away-v1".into();
        // Independent sum of code bytes, per-row scales, group affine values,
        // and unquantized norm/bias vectors for the assigned eight layers.
        let matrix = |rows: u64, cols: u64| rows * cols + rows * 4 + rows * cols / 128 * 8;
        let expected = matrix(151936, 896)
            + 8 * (2 * matrix(896, 896)
                + 2 * matrix(128, 896)
                + 2 * matrix(4864, 896)
                + matrix(896, 4864)
                + (2 * 896 + 896 + 2 * 128) * 4);
        assert_eq!(admit(&d, limits()).unwrap(), expected);
        let mut cap = limits();
        cap.weight_bytes = expected - 1;
        assert_eq!(admit(&d, cap).unwrap_err().code, ErrorCode::ResourceCap);
        cap.weight_bytes = expected;
        assert_eq!(admit(&d, cap).unwrap(), expected);
    }
    #[test]
    fn packed_conversion_scratch_boundary_refuses_before_io() {
        let d = qwen2_packed_descriptor(0..8, true, false).unwrap();
        let mut cap = limits();
        let staging = 151936 * 896 + 151936 * 4 + 32 * 1024 * 1024 * 10;
        cap.scratch_bytes = staging - 1;
        assert_eq!(admit(&d, cap).unwrap_err().code, ErrorCode::ResourceCap);
        cap.scratch_bytes = staging;
        assert!(admit(&d, cap).is_ok());
        for field in 0..3 {
            let mut foreign = d.clone();
            match field {
                0 => foreign.quantizer = "other-rounding".into(),
                1 => foreign.representation = "signed-codes".into(),
                _ => foreign.architecture = Architecture::Qwen35DenseHybrid,
            }
            assert_eq!(
                admit(&foreign, cap).unwrap_err().code,
                ErrorCode::Unsupported
            );
        }
    }
    #[test]
    fn qwen3_profiles_state_weight_caps_and_foreign_identity() {
        for profile in [
            NumericsProfile::MlxExplicitF32,
            NumericsProfile::MlxPackedInt8,
        ] {
            let d = qwen3_descriptor(0..9, true, false, profile).unwrap();
            let mut cap = limits();
            cap.scratch_bytes = 1_024_000_000;
            let matrix = |r: u64, c: u64| {
                if profile == NumericsProfile::MlxPackedInt8 {
                    r * c + r * 4 + r * c / 128 * 8
                } else {
                    r * c * 4
                }
            };
            let expected = matrix(151936, 1024)
                + 9 * (matrix(2048, 1024)
                    + matrix(1024, 2048)
                    + 2 * matrix(1024, 1024)
                    + 2 * matrix(3072, 1024)
                    + matrix(1024, 3072)
                    + (2 * 1024 + 2 * 128) * 4);
            assert_eq!(admit(&d, cap).unwrap(), expected);
            cap.weight_bytes = expected - 1;
            assert_eq!(admit(&d, cap).unwrap_err().code, ErrorCode::ResourceCap);
            cap.weight_bytes = expected;
            let state = 9 * 128 * 2 * 8 * 128 * 4;
            cap.state_bytes = state - 1;
            assert_eq!(admit(&d, cap).unwrap_err().code, ErrorCode::ResourceCap);
            cap.state_bytes = state;
            assert_eq!(admit(&d, cap).unwrap(), expected);
            for field in 0..4 {
                let mut other = d.clone();
                match field {
                    0 => other.revision = "other".into(),
                    1 => other.tensor_manifest = [9; 32],
                    2 => other.architecture = Architecture::Qwen35MoeHybrid,
                    _ => other.profile = NumericsProfile::CpuInt8F32,
                }
                assert_eq!(admit(&other, cap).unwrap_err().code, ErrorCode::Unsupported);
            }
        }
    }
    #[test]
    fn hybrid_state_caps_depend_on_layer_kinds_not_context_only() {
        let d = qwen35_descriptor(0..4, false, false, NumericsProfile::MlxPackedInt8).unwrap();
        let cfg = DenseConfig::for_descriptor(&d).unwrap();
        let fixed = 3 * (3 * 6144 + 16 * 128 * 128) * 4;
        assert_eq!(
            cfg.state_bytes_for(0..4, 1).unwrap(),
            fixed + 2 * 2 * 256 * 4
        );
        assert_eq!(
            cfg.state_bytes_for(0..4, 128).unwrap(),
            fixed + 128 * 2 * 2 * 256 * 4
        );
        assert_eq!(
            cfg.state_bytes_for(1..3, 128).unwrap(),
            2 * (3 * 6144 + 16 * 128 * 128) * 4
        );
        let mut cap = limits();
        cap.scratch_bytes = 2_048_000_000;
        cap.state_bytes = cfg.state_bytes_for(0..4, 128).unwrap() - 1;
        assert_eq!(admit(&d, cap).unwrap_err().code, ErrorCode::ResourceCap);
        cap.state_bytes += 1;
        assert!(admit(&d, cap).is_ok());
        let mut wrong = d.clone();
        wrong.state_schema = "attention-kv-f32-v1".into();
        assert_eq!(admit(&wrong, cap).unwrap_err().code, ErrorCode::Unsupported);
        wrong = d;
        wrong.architecture = Architecture::Qwen35MoeHybrid;
        assert_eq!(admit(&wrong, cap).unwrap_err().code, ErrorCode::Unsupported);
    }
    #[test]
    fn foreign_identity_refuses() {
        let mut d = qwen2_descriptor(8..24, false, true).unwrap();
        d.checkpoint = [9; 32];
        assert_eq!(
            admit(&d, limits()).unwrap_err().code,
            ErrorCode::Unsupported
        );
    }
}
