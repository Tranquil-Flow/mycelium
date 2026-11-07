// SPDX-License-Identifier: AGPL-3.0-only
//! Model-free production admission; no MLX context or checkpoint access.
use backend_mlx::admission::{admit, digest_hex};
use stage_contract::*;
fn descriptor() -> StageDescriptor {
    StageDescriptor {
        checkpoint: digest_hex("f1483d385d7ec987aac0afd9be96bbab3733bd34b80a7b56e784f8795ec541ee")
            .unwrap(),
        revision: "995ad96eacd98c81ed38be0c5b274b04031597b0".into(),
        tensor_manifest: digest_hex(
            "41b9356101ebf8e7519e150dc811f80c4226e727301fbb032b890f006ed0be83",
        )
        .unwrap(),
        semantics: "qwen36-35b-a3b-v014-explicit-v1".into(),
        architecture: Architecture::Qwen35MoeHybrid,
        layers: 0..1,
        embedding: false,
        head: false,
        representation: "persistent_packed_int8".into(),
        quantizer: "symmetric-rowwise-int8-half-away-v1".into(),
        profile: NumericsProfile::MlxPackedInt8,
        state_schema: "hybrid-conv-deltanet-kv-f32-v1".into(),
    }
}
fn limits() -> Limits {
    Limits {
        weight_bytes: 6 * 1024 * 1024 * 1024,
        state_bytes: 1024 * 1024 * 1024,
        scratch_bytes: 6 * 1024 * 1024 * 1024,
        max_tokens: 8,
    }
}
/// Proves: B-NUM-10 (partial: local descriptor/residency only, not signed load proof).
#[test]
fn bounded_moe_descriptor_reaches_production_admission() {
    let d = descriptor();
    d.validate(40).unwrap();
    let admitted = admit(&d, limits());
    assert!(
        admitted.is_ok(),
        "bounded MoE production descriptor refused: {admitted:?}"
    );
    // Recorded assigned image, independently summed in the existing fixture.
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/moe-decoder-packed.json")).unwrap();
    let expected: u64 = fixture["weights"]
        .as_object()
        .unwrap()
        .iter()
        .filter(|(n, _)| n.starts_with("model.language_model.layers.0."))
        .map(|(_, v)| {
            v["resident"]
                .as_object()
                .unwrap()
                .values()
                .map(|p| p["payload_bytes"].as_u64().unwrap())
                .sum::<u64>()
        })
        .sum();
    assert_eq!(admitted.unwrap(), expected);
}
#[test]
fn foreign_model_pack_profile_and_assignment_geometry_refuse() {
    for field in 0..12 {
        let mut d = descriptor();
        match field {
            0 => d.checkpoint[0] ^= 1,
            1 => d.tensor_manifest[0] ^= 1,
            2 => d.revision.push('x'),
            3 => d.semantics.push('x'),
            4 => d.representation.push('x'),
            5 => d.quantizer.push('x'),
            6 => d.state_schema.push('x'),
            7 => d.profile = NumericsProfile::CpuInt8F32,
            8 => d.layers = 0..41,
            9 => d.layers = 1..1,
            10 => {
                d.layers = 1..2;
                d.embedding = true;
            }
            _ => d.head = true,
        }
        assert!(admit(&d, limits()).is_err(), "field {field}");
    }
}

#[test]
fn assigned_shards_and_roles_match_recorded_images() {
    use backend_mlx::moe_admission as moe;
    use std::collections::BTreeMap;
    let decoder: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/moe-decoder-packed.json")).unwrap();
    let entry: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/moe-entry-packed.json")).unwrap();
    let head: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/moe-head-packed.json")).unwrap();
    let all: BTreeMap<_, _> = [&decoder, &entry, &head]
        .into_iter()
        .flat_map(|f| f["weights"].as_object().unwrap().iter())
        .collect();
    for (range, embedding, head) in [
        (0..2, true, false),
        (2..4, false, false),
        (38..40, false, true),
    ] {
        let d = moe::descriptor(
            range.clone(),
            embedding,
            head,
            NumericsProfile::MlxPackedInt8,
        )
        .unwrap();
        let plan = moe::shard_plan(&d).unwrap();
        let actual: BTreeMap<_, _> = plan
            .iter()
            .flat_map(|(shard, weights)| {
                weights
                    .iter()
                    .map(move |(name, shape)| (name, (shard, shape)))
            })
            .collect();
        let expected: BTreeMap<_, _> = all
            .iter()
            .filter(|(name, _)| {
                if **name == model::moe_decoder::EMBED_KEY {
                    embedding
                } else if **name == model::moe_decoder::HEAD_KEY
                    || **name == model::moe_decoder::NORM_KEY
                {
                    head
                } else {
                    name.split(".layers.")
                        .nth(1)
                        .and_then(|s| s.split('.').next())
                        .and_then(|n| n.parse::<u32>().ok())
                        .is_some_and(|n| range.contains(&n))
                }
            })
            .map(|(k, v)| (*k, *v))
            .collect();
        assert_eq!(actual.len(), expected.len());
        for (name, (shard, shape)) in actual {
            assert_eq!(*shard, expected[name]["shard"].as_str().unwrap());
            assert_eq!(serde_json::json!(shape), expected[name]["shape"]);
        }
    }
}
#[test]
fn resources_and_profile_identity_are_checked_before_io() {
    use backend_mlx::moe_admission as moe;
    for profile in [
        NumericsProfile::MlxExplicitF32,
        NumericsProfile::MlxPackedInt8,
    ] {
        let d = moe::descriptor(0..1, false, false, profile).unwrap();
        let amount = moe::requirements(&d, limits()).unwrap();
        for field in 0..4 {
            let mut cap = limits();
            match field {
                0 => cap.weight_bytes = amount.resident.get() - 1,
                1 => cap.state_bytes = amount.kv.get() - 1,
                2 => cap.scratch_bytes = amount.scratch.get() - 1,
                _ => cap.max_tokens = 0,
            }
            assert_eq!(admit(&d, cap).unwrap_err().code, ErrorCode::ResourceCap);
        }
        let exact = Limits {
            weight_bytes: amount.resident.get(),
            state_bytes: amount.kv.get(),
            scratch_bytes: amount.scratch.get(),
            ..limits()
        };
        assert_eq!(admit(&d, exact).unwrap(), amount.resident.get());
    }
    // Recorded r6 state shapes: linear conv [1,3,8192] + recurrent [1,32,128,128]; full [1,2,t,256] twice.
    assert_eq!(
        moe::state_bytes(0..1, 3).unwrap(),
        (3 * 8192 + 32 * 128 * 128) * 4
    );
    assert_eq!(
        moe::state_bytes(0..1, 5).unwrap(),
        moe::state_bytes(0..1, 3).unwrap()
    );
    assert_eq!(moe::state_bytes(3..4, 5).unwrap(), 2 * 2 * 5 * 256 * 4);
}
#[test]
fn artifact_wrong_origin_missing_and_wrong_shard_refuse() {
    use backend_mlx::moe_admission as moe;
    let d = descriptor();
    let plan = moe::shard_plan(&d).unwrap();
    let (shard, wanted) = plan.first_key_value().unwrap();
    // Public verifier, with an inert nonexistent directory. Never a model path.
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/not-a-model");
    assert_eq!(
        moe::verify_shard(&root, shard, wanted).unwrap_err().code,
        ErrorCode::Artifact
    );
    assert_eq!(
        moe::verify_shard(&root, "../model-00002-of-00026.safetensors", wanted)
            .unwrap_err()
            .code,
        ErrorCode::Artifact
    );
    assert_eq!(
        moe::verify_shard(&root, "foreign.safetensors", wanted)
            .unwrap_err()
            .code,
        ErrorCode::Artifact
    );
}
