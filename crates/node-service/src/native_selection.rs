// SPDX-License-Identifier: AGPL-3.0-only
//! CPU admission for the native adapter. Metadata is supplied by the authenticated
//! caller; matching pinned metadata does not issue authority or a load proof.
use backend_mlx::{admission, moe_admission};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use stage_contract::{
    Architecture, Error, ErrorCode, Limits, NumericsProfile, Result, StageDescriptor,
};
use std::{ops::Range, path::Path};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Profile {
    PackedInt8,
    ExplicitF32,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssignedImage {
    pub model_id: String,
    pub revision: String,
    pub image_digest: String,
    pub tensor_manifest: String,
    pub profile: Profile,
    pub layers: Range<u32>,
    pub total_layers: u32,
    /// Exact assigned source tensor/shard metadata, not an untrusted data header.
    pub source_metadata: Value,
}

#[derive(Debug)]
pub struct Selection {
    pub descriptor: StageDescriptor,
    pub limits: Limits,
    pub reservation: budgets::Reservation,
}

/// Missing metadata is the legacy dense-only contract. Explicit metadata is
/// never downgraded to that path, even when malformed or shape-compatible.
pub fn select(
    image: Option<&AssignedImage>,
    range: Range<u32>,
    total: u32,
    memory: u64,
    tokens: u32,
) -> Result<Selection> {
    let embedding = range.start == 0;
    let head = range.end == total;
    let descriptor = match image {
        None => admission::qwen2_packed_descriptor(range.clone(), embedding, head)?,
        Some(image) => {
            if image.layers != range || image.total_layers != total {
                return Err(Error::new(ErrorCode::Binding, "native-assigned-range"));
            }
            let profile = match image.profile {
                Profile::PackedInt8 => NumericsProfile::MlxPackedInt8,
                Profile::ExplicitF32 => NumericsProfile::MlxExplicitF32,
            };
            let d = match image.model_id.as_str() {
                "Qwen/Qwen3-0.6B" => {
                    admission::qwen3_descriptor(range.clone(), embedding, head, profile)?
                }
                "Qwen/Qwen3.6-35B-A3B" => {
                    moe_admission::descriptor(range.clone(), embedding, head, profile)?
                }
                "Qwen/Qwen2.5-0.5B-Instruct" => match profile {
                    NumericsProfile::MlxPackedInt8 => {
                        admission::qwen2_packed_descriptor(range.clone(), embedding, head)?
                    }
                    _ => admission::qwen2_descriptor(range.clone(), embedding, head)?,
                },
                _ => return Err(Error::new(ErrorCode::Unsupported, "native-model")),
            };
            if image.revision != d.revision
                || admission::digest_hex(&image.image_digest)? != d.checkpoint
                || admission::digest_hex(&image.tensor_manifest)? != d.tensor_manifest
            {
                return Err(Error::new(ErrorCode::Binding, "native-image-identity"));
            }
            let expected = if d.architecture == Architecture::Qwen35MoeHybrid {
                moe_admission::assigned_image(&d)?
            } else {
                serde_json::json!({"header_sha256": admission::artifact_layout(&d)?.1})
            };
            if image.source_metadata != expected {
                return Err(Error::new(ErrorCode::Artifact, "native-source-binding"));
            }
            d
        }
    };
    if total != admission::total_layers(&descriptor)? {
        return Err(Error::new(ErrorCode::Binding, "native-model-total"));
    }
    let limits = Limits {
        weight_bytes: memory,
        scratch_bytes: memory,
        state_bytes: memory,
        max_tokens: tokens,
    };
    let reservation = admission::requirements(&descriptor, limits)?;
    let reserved = reservation
        .total()
        .map_err(|_| Error::new(ErrorCode::ResourceCap, "native-resource-overflow"))?
        .memory
        .get();
    if reserved > memory {
        return Err(Error::new(
            ErrorCode::ResourceCap,
            "native_exceeds_assigned_memory",
        ));
    }
    Ok(Selection {
        descriptor,
        limits,
        reservation,
    })
}

/// Decode the OPTIONAL field on the PRIVATE agent-to-worker command, never an
/// external signed plan. Explicit null/invalid data is an error, not a default.
/// Repeat assignment/range/resource checks at both sides of the private link.
pub fn assigned_image(
    head: &Value,
    range: Range<u32>,
    total: u32,
    memory: u64,
    tokens: u32,
) -> Result<Option<AssignedImage>> {
    let allowed = [
        "command",
        "checkpoint",
        "start",
        "end",
        "total",
        "memory",
        "tokens",
        "id",
        "generation",
        "stage_image",
    ];
    if head
        .as_object()
        .is_none_or(|object| object.keys().any(|key| !allowed.contains(&key.as_str())))
    {
        return Err(Error::new(ErrorCode::Binding, "native-private-profile"));
    }
    let image = head
        .get("stage_image")
        .map(|value| {
            serde_json::from_value::<AssignedImage>(value.clone())
                .map_err(|_| Error::new(ErrorCode::Artifact, "native-image-metadata"))
        })
        .transpose()?;
    select(image.as_ref(), range, total, memory, tokens)?;
    Ok(image)
}

/// Adapt the existing dense-only signed member/owner-leaf plan. It has no
/// model/profile fields and cannot describe MoE; do not extend its wire schema.
pub fn legacy_plan_image(plan: &Value, owner_leaf: bool) -> Result<AssignedImage> {
    let role = if owner_leaf {
        "checkpoint"
    } else {
        "acceptance"
    };
    let allowed = [role, "start", "end", "total", "tokens", "memory"];
    if plan.as_object().is_none_or(|object| {
        object.keys().any(|key| !allowed.contains(&key.as_str()))
            || allowed.iter().any(|key| !object.contains_key(*key))
    }) {
        return Err(Error::new(
            ErrorCode::Unsupported,
            "native-legacy-plan-profile",
        ));
    }
    let number = |name: &str| -> Result<u32> {
        plan[name]
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .ok_or_else(|| Error::new(ErrorCode::Binding, "native-plan-range"))
    };
    let planning: Value = serde_json::from_str(include_str!("../data/qwen2-planning.json"))
        .map_err(|_| Error::new(ErrorCode::Artifact, "native-legacy-profile"))?;
    Ok(AssignedImage {
        model_id: planning["model"]["model_id"]
            .as_str()
            .ok_or_else(|| Error::new(ErrorCode::Artifact, "native-legacy-profile"))?
            .into(),
        revision: planning["model"]["resolved_commit"]
            .as_str()
            .ok_or_else(|| Error::new(ErrorCode::Artifact, "native-legacy-profile"))?
            .into(),
        image_digest: admission::CHECKPOINT.into(),
        tensor_manifest: admission::MANIFEST.into(),
        profile: Profile::PackedInt8,
        layers: number("start")?..number("end")?,
        total_layers: number("total")?,
        source_metadata: serde_json::json!({"header_sha256":admission::RAW_HEADER}),
    })
}

impl Selection {
    /// Source IO only, before Context creation. The existing immutable L7 source
    /// precondition remains; this is not a held-FD/adversarial-filesystem proof.
    pub fn verify_source(&self, path: &Path) -> Result<()> {
        if self.descriptor.architecture == Architecture::Qwen35MoeHybrid {
            for (shard, wanted) in moe_admission::shard_plan(&self.descriptor)? {
                moe_admission::verify_shard(path, &shard, &wanted)?;
            }
            Ok(())
        } else {
            backend_mlx::source_image::verify_artifact(path, &self.descriptor)
        }
    }
}
