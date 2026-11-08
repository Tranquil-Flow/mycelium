// SPDX-License-Identifier: AGPL-3.0-only
//! CPU admission only; native resident proof is separately model-gated.
use backend_mlx::admission;
use node_service::native_selection::{AssignedImage, Profile, select};
use stage_contract::{Architecture, NumericsProfile};

#[test]
fn qwen3_assigned_stage_selects_the_pinned_backend_without_changing_legacy() {
    for range in [0..14, 14..28] {
        let descriptor = admission::qwen3_descriptor(
            range.clone(),
            range.start == 0,
            range.end == 28,
            NumericsProfile::MlxPackedInt8,
        )
        .unwrap();
        let image = AssignedImage {
            model_id: "Qwen/Qwen3-0.6B".into(),
            revision: descriptor.revision.clone(),
            image_digest: admission::QWEN3_CHECKPOINT.into(),
            tensor_manifest: descriptor
                .tensor_manifest
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect(),
            profile: Profile::PackedInt8,
            layers: range.clone(),
            total_layers: 28,
            source_metadata: serde_json::json!({"header_sha256":admission::QWEN3_RAW_HEADER}),
        };
        let got = select(Some(&image), range, 28, 4_294_967_296, 64).unwrap();
        assert_eq!(got.descriptor, descriptor);
        assert_eq!(got.descriptor.architecture, Architecture::Qwen3);
    }
    let legacy = select(None, 0..12, 24, 4_294_967_296, 64).unwrap();
    assert_eq!(legacy.descriptor.architecture, Architecture::Qwen2);
}
