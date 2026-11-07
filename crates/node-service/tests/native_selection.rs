// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NUM-05/B-NUM-10 metadata/admission only, not native load/compute.
//! Source metadata is the immutable recorded r6/r7 image, not model file reads.
use backend_mlx::{admission, moe_admission};
use node_service::native_selection::{AssignedImage, Profile, select};
use serde_json::{Value, json};
use stage_contract::{Architecture, ErrorCode, NumericsProfile};
use std::ops::Range;

fn moe(range: Range<u32>, profile: Profile) -> AssignedImage {
    let pins: Value =
        serde_json::from_str(include_str!("../../backend-mlx/src/qwen36-pins.json")).unwrap();
    // Independently select recorded tensor rows by their source names/roles.
    let weights: serde_json::Map<String, Value> = pins["weights"]
        .as_object()
        .unwrap()
        .iter()
        .filter(|(name, _)| {
            if *name == "model.language_model.embed_tokens.weight" {
                range.start == 0
            } else if *name == "lm_head.weight" || *name == "model.language_model.norm.weight" {
                range.end == 40
            } else {
                name.split(".layers.")
                    .nth(1)
                    .and_then(|s| s.split('.').next())
                    .and_then(|s| s.parse::<u32>().ok())
                    .is_some_and(|n| range.contains(&n))
            }
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let shards: serde_json::Map<String, Value> = weights
        .values()
        .map(|v| {
            let name = v["shard"].as_str().unwrap();
            (name.to_owned(), pins["checkpoint"]["shards"][name].clone())
        })
        .collect();
    AssignedImage {
        model_id: pins["checkpoint"]["repo"].as_str().unwrap().into(),
        revision: pins["checkpoint"]["revision"].as_str().unwrap().into(),
        image_digest: moe_admission::IMAGE.into(),
        tensor_manifest: moe_admission::INDEX.into(),
        profile,
        layers: range,
        total_layers: 40,
        source_metadata: json!({"weights":weights,"shards":shards}),
    }
}
fn memory() -> u64 {
    u64::MAX / 2
}

#[test]
fn explicit_moe_selects_admitted_backend_for_entry_middle_and_head() {
    for range in [0..2, 2..4, 38..40] {
        for profile in [Profile::PackedInt8, Profile::ExplicitF32] {
            let image = moe(range.clone(), profile);
            let got = select(Some(&image), range.clone(), 40, memory(), 5).unwrap();
            assert_eq!(
                got.descriptor.architecture,
                Architecture::Qwen35MoeHybrid,
                "explicit source-bound MoE must not become dense"
            );
            let expected = moe_admission::descriptor(
                range.clone(),
                range.start == 0,
                range.end == 40,
                match profile {
                    Profile::PackedInt8 => NumericsProfile::MlxPackedInt8,
                    Profile::ExplicitF32 => NumericsProfile::MlxExplicitF32,
                },
            )
            .unwrap();
            assert_eq!(got.descriptor, expected);
            assert_eq!(
                got.reservation,
                admission::requirements(&expected, got.limits).unwrap()
            );
            let config = backend_mlx::lifecycle::Config::for_descriptor(&got.descriptor).unwrap();
            assert!(matches!(config, backend_mlx::lifecycle::Config::Moe));
        }
    }
}
#[test]
fn no_metadata_preserves_dense_descriptor_and_resources() {
    for range in [0..8, 8..24] {
        let got = select(None, range.clone(), 24, memory(), 5).unwrap();
        let expected =
            admission::qwen2_packed_descriptor(range.clone(), range.start == 0, range.end == 24)
                .unwrap();
        assert_eq!(got.descriptor, expected);
        assert_eq!(
            got.reservation,
            admission::requirements(&expected, got.limits).unwrap()
        );
    }
}
#[test]
fn foreign_model_revision_image_and_manifest_refuse_instead_of_falling_back() {
    let good = moe(2..4, Profile::PackedInt8);
    for field in 0..4 {
        let mut bad = good.clone();
        match field {
            0 => bad.model_id = "Qwen/same-shape-imposter".into(),
            1 => bad.revision = "foreign".into(),
            2 => bad.image_digest = "00".repeat(32),
            _ => bad.tensor_manifest = "00".repeat(32),
        }
        assert!(
            select(Some(&bad), 2..4, 40, memory(), 5).is_err(),
            "identity field {field} must refuse"
        );
    }
}
#[test]
fn range_and_total_are_bound_to_the_assigned_image() {
    let good = moe(2..4, Profile::PackedInt8);
    for (range, total) in [(1..3, 40), (2..4, 24), (2..4, 41), (4..4, 40), (39..41, 40)] {
        assert!(select(Some(&good), range, total, memory(), 5).is_err());
    }
    assert!(
        select(None, 0..8, 40, memory(), 5).is_err(),
        "legacy caller cannot imply MoE by total alone"
    );
}
#[test]
fn missing_or_foreign_source_binding_and_dtype_refuse() {
    let good = moe(2..4, Profile::PackedInt8);
    for field in 0..7 {
        let mut bad = good.clone();
        let name = bad.source_metadata["weights"]
            .as_object()
            .unwrap()
            .keys()
            .next()
            .unwrap()
            .clone();
        match field {
            0 => bad.source_metadata = Value::Null,
            1 => bad.source_metadata["weights"][&name]["source_dtype"] = json!("F64"),
            2 => bad.source_metadata["weights"][&name]["source_sha256"] = json!("00".repeat(32)),
            3 => bad.source_metadata["weights"][&name]["shard"] = json!("foreign.safetensors"),
            4 => {
                bad.source_metadata["weights"]
                    .as_object_mut()
                    .unwrap()
                    .remove(&name);
            }
            5 => bad.source_metadata["weights"]["unassigned"] = json!({}),
            _ => bad.source_metadata["shards"] = json!({}),
        }
        assert!(
            select(Some(&bad), 2..4, 40, memory(), 5).is_err(),
            "source binding field {field} must refuse"
        );
    }
}
#[test]
fn combined_reservation_and_token_caps_refuse_before_source_io() {
    for image in [None, Some(moe(2..4, Profile::PackedInt8))] {
        let (range, total) = if image.is_some() {
            (2..4, 40)
        } else {
            (0..8, 24)
        };
        let got = select(image.as_ref(), range.clone(), total, memory(), 5).unwrap();
        let total_bytes = got.reservation.total().unwrap().memory.get();
        assert_eq!(
            select(image.as_ref(), range.clone(), total, total_bytes - 1, 5)
                .unwrap_err()
                .code,
            ErrorCode::ResourceCap
        );
        assert!(select(image.as_ref(), range.clone(), total, total_bytes, 5).is_ok());
        assert_eq!(
            select(image.as_ref(), range, total, memory(), 0)
                .unwrap_err()
                .code,
            ErrorCode::ResourceCap
        );
    }
}
#[test]
fn private_payload_roundtrip_preserves_binding_and_refuses_missing_fields() {
    let good = moe(2..4, Profile::PackedInt8);
    let encoded = serde_json::to_value(&good).unwrap();
    let decoded: AssignedImage = serde_json::from_value(encoded.clone()).unwrap();
    assert_eq!(
        select(Some(&decoded), 2..4, 40, memory(), 5)
            .unwrap()
            .descriptor
            .architecture,
        Architecture::Qwen35MoeHybrid
    );
    for key in ["source_metadata", "image_digest", "model_id", "profile"] {
        let mut bad = encoded.clone();
        bad.as_object_mut().unwrap().remove(key);
        assert!(
            serde_json::from_value::<AssignedImage>(bad).is_err(),
            "missing {key}"
        );
    }
    let mut bad = encoded;
    bad["profile"] = json!("guess_from_shape");
    assert!(serde_json::from_value::<AssignedImage>(bad).is_err());
}

#[test]
fn external_legacy_plan_cannot_claim_an_unrepresented_model_or_private_metadata() {
    use node_service::native_selection::legacy_plan_image;
    let member =
        json!({"acceptance":{},"start":8,"end":24,"total":24,"tokens":5,"memory":memory()});
    let image = legacy_plan_image(&member, false).unwrap();
    assert_eq!(
        select(Some(&image), 8..24, 24, memory(), 5)
            .unwrap()
            .descriptor,
        select(None, 8..24, 24, memory(), 5).unwrap().descriptor
    );
    for (key, value) in [
        ("model_id", json!("Qwen/Qwen3.6-35B-A3B")),
        ("profile", json!("unknown")),
        (
            "stage_image",
            serde_json::to_value(moe(2..4, Profile::PackedInt8)).unwrap(),
        ),
    ] {
        let mut foreign = member.clone();
        foreign[key] = value;
        assert!(
            legacy_plan_image(&foreign, false).is_err(),
            "external {key} cannot silently select dense"
        );
    }
    assert!(legacy_plan_image(&member, true).is_err());
    let owner = json!({"checkpoint":"synthetic-not-a-model","start":0,"end":8,"total":24,"tokens":5,"memory":memory()});
    assert!(legacy_plan_image(&owner, true).is_ok());
    assert!(legacy_plan_image(&owner, false).is_err());
}
#[test]
fn private_missing_image_cannot_downgrade_declared_moe_or_unknown_profile() {
    use node_service::native_selection::assigned_image;
    let head = json!({"command":"prepare","checkpoint":"synthetic-not-a-model","start":0,"end":8,"total":24,"memory":memory(),"tokens":5,"id":"11".repeat(32),"generation":1});
    assert!(
        assigned_image(&head, 0..8, 24, memory(), 5)
            .unwrap()
            .is_none()
    );
    for (key, value) in [
        ("model_id", json!("Qwen/Qwen3.6-35B-A3B")),
        ("profile", json!("unknown")),
        ("stage_image", Value::Null),
    ] {
        let mut foreign = head.clone();
        foreign[key] = value;
        assert!(
            assigned_image(&foreign, 0..8, 24, memory(), 5).is_err(),
            "private {key} cannot silently select dense"
        );
    }
}
#[test]
fn private_link_roundtrip_reaches_the_production_metadata_consumer() {
    use node_service::{native_selection::assigned_image, worker_link::Link};
    use std::os::unix::net::UnixStream;
    let image = moe(2..4, Profile::PackedInt8);
    let command = json!({"command":"prepare","checkpoint":"synthetic-not-a-model","start":2,"end":4,"total":40,"memory":memory(),"tokens":5,"id":"11".repeat(32),"generation":1,"stage_image":image});
    let (a, b) = UnixStream::pair().unwrap();
    let wait = node_service::service::duration(budgets::BudgetId::WorkerStartup);
    let mut sender = Link::new(a, wait).unwrap();
    let mut receiver = Link::new(b, wait).unwrap();
    let sent = command.clone();
    let thread = std::thread::spawn(move || sender.send(&sent, &[]));
    let (received, body) = receiver.receive().unwrap();
    thread.join().unwrap().unwrap();
    assert!(body.is_empty());
    assert_eq!(received, command);
    let parsed = assigned_image(&received, 2..4, 40, memory(), 5)
        .unwrap()
        .unwrap();
    assert_eq!(
        select(Some(&parsed), 2..4, 40, memory(), 5)
            .unwrap()
            .descriptor
            .architecture,
        Architecture::Qwen35MoeHybrid
    );
    assert!(assigned_image(&received, 1..3, 40, memory(), 5).is_err());
    assert!(assigned_image(&received, 2..4, 40, 1, 5).is_err());
}
#[test]
fn absent_and_tiny_synthetic_source_refuse_without_native_runtime() {
    let root = std::env::temp_dir().join(format!("l8-r10-source-{}", std::process::id()));
    std::fs::create_dir(&root).unwrap();
    let path = root.join("qwen-moe-shaped-name.safetensors");
    std::fs::write(&path, b"tiny synthetic invalid source; not model weights").unwrap();
    let result = || {
        for image in [None, Some(moe(2..4, Profile::PackedInt8))] {
            let (range, total) = if image.is_some() {
                (2..4, 40)
            } else {
                (0..8, 24)
            };
            let selected = select(image.as_ref(), range, total, memory(), 5).unwrap();
            assert_eq!(
                selected.verify_source(&root).unwrap_err().code,
                ErrorCode::Artifact
            );
            assert_eq!(
                selected.verify_source(&path).unwrap_err().code,
                ErrorCode::Artifact
            );
            assert_eq!(
                selected
                    .verify_source(&root.join("absent"))
                    .unwrap_err()
                    .code,
                ErrorCode::Artifact
            );
        }
    };
    let result = std::panic::catch_unwind(result);
    std::fs::remove_dir_all(&root).unwrap();
    result.unwrap();
}
