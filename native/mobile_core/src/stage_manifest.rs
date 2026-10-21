// SPDX-License-Identifier: AGPL-3.0-only
//! Signed v2 mobile stage manifests (`mycelium.mobile_stage_pack.v2`), the
//! Rust side of `mycelium_mobile/qwen_stage_manifest.py`.
//!
//! A v2 manifest carries no weights. It pins the upstream model and source
//! file, the Qwen2 architecture, the layer range, the backend
//! (`mobile-rust-qwen2`), the on-device quantizer (`numpy_rowwise_v1`), the
//! activation dtype, and every tensor's raw upstream bytes by SHA-256, plus an
//! optional golden the phone uses to prove on load that it computes
//! bit-identically to the host build (plan R1a). `pack_digest` is the SHA-256
//! of the canonical ASCII JSON of all other fields; the seed signs the whole
//! manifest as a member-channel signed message, so a persisted manifest can be
//! re-verified against the pinned seed key at boot.
//!
//! Validation is fail-closed and mirrors the Python verifier field for field.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::bf16::SourceDType;
use crate::canonical;
use crate::membership;
use crate::qwen2::{MATRIX_SUFFIXES, QWEN2_BACKEND, Qwen2Config, Qwen2Stage, VECTOR_SUFFIXES};

pub const MANIFEST_PROTOCOL: &str = "mycelium.mobile_stage_pack.v2";
pub const GOLDEN_PROTOCOL: &str = "mycelium.mobile_stage_golden.v1";
pub const GOLDEN_GENERATOR: &str = "splitmix64_unit_f32_v1";
pub const QUANTIZER: &str = "numpy_rowwise_v1";
pub const ACTIVATION_DTYPE: &str = "float32";
pub const SOURCE_FILE: &str = "model.safetensors";
pub const MAX_MANIFEST_BYTES: usize = 512 * 1024;
pub const MAX_LAYERS_PER_STAGE: usize = 64;
pub const MAX_TENSOR_BYTES: u64 = 1 << 30;
pub const MAX_GOLDEN_SEQ: usize = 64;
const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ManifestError(pub String);

fn reject<T>(code: &str) -> Result<T, ManifestError> {
    Err(ManifestError(code.to_owned()))
}

/// One upstream tensor the stage needs, pinned by its raw bytes.
#[derive(Debug, Clone, PartialEq)]
pub struct TensorSpec {
    pub name: String,
    pub dtype: SourceDType,
    pub shape: Vec<usize>,
    pub size_bytes: u64,
    /// `sha256:<64 hex>` of the raw upstream little-endian bytes.
    pub sha256: String,
}

impl TensorSpec {
    /// The 64 lowercase hex digits (the content address of the object).
    pub fn hex(&self) -> &str {
        &self.sha256["sha256:".len()..]
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Golden {
    pub seed: u64,
    pub seq: usize,
    pub output_digest: String,
}

/// A verified v2 manifest.
#[derive(Debug, Clone, PartialEq)]
pub struct StageManifest {
    pub stage_id: String,
    pub model_id: String,
    pub resolved_commit: String,
    pub source_sha256: String,
    pub source_size_bytes: u64,
    pub config: Qwen2Config,
    pub num_hidden_layers: usize,
    pub start_layer: usize,
    pub end_layer_exclusive: usize,
    pub tensors: Vec<TensorSpec>,
    pub golden: Option<Golden>,
    pub pack_digest: String,
    document: Value,
}

fn exact_fields(v: &Value, fields: &[&str], code: &str) -> Result<(), ManifestError> {
    let map = v.as_object().ok_or_else(|| ManifestError(code.into()))?;
    if map.len() != fields.len() || !fields.iter().all(|f| map.contains_key(*f)) {
        return reject(code);
    }
    Ok(())
}

fn int(v: &Value, code: &str, min: u64, max: u64) -> Result<u64, ManifestError> {
    v.as_u64().filter(|n| (min..=max).contains(n)).ok_or_else(|| ManifestError(code.into()))
}

/// A JSON float (not an integer literal), finite and positive, as Python's
/// `isinstance(value, float)` check requires.
fn positive_float(v: &Value, code: &str) -> Result<f64, ManifestError> {
    match v {
        Value::Number(n) if !n.is_u64() && !n.is_i64() => {
            n.as_f64().filter(|f| f.is_finite() && *f > 0.0).ok_or_else(|| ManifestError(code.into()))
        }
        _ => reject(code),
    }
}

fn is_digest(s: &str) -> bool {
    s.len() == 71 && s.starts_with("sha256:") && s[7..].bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn digest_str<'v>(v: &'v Value, code: &str) -> Result<&'v str, ManifestError> {
    v.as_str().filter(|s| is_digest(s)).ok_or_else(|| ManifestError(code.into()))
}

/// `[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}`
fn identifier<'v>(v: &'v Value, code: &str) -> Result<&'v str, ManifestError> {
    let s = v.as_str().ok_or_else(|| ManifestError(code.into()))?;
    let b = s.as_bytes();
    let ok = !b.is_empty()
        && b.len() <= 256
        && b[0].is_ascii_alphanumeric()
        && b.iter().all(|c| c.is_ascii_alphanumeric() || b"._:/@+-".contains(c));
    if ok { Ok(s) } else { reject(code) }
}

/// Every upstream tensor name of `[start, end)`, sorted (the manifest order).
pub fn tensor_names(start: usize, end: usize) -> Vec<String> {
    let mut names: Vec<String> = (start..end)
        .flat_map(|l| MATRIX_SUFFIXES.iter().chain(VECTOR_SUFFIXES.iter()).map(move |s| format!("model.layers.{l}.{s}")))
        .collect();
    names.sort();
    names
}

fn expected_shape(config: &Qwen2Config, name: &str) -> Option<Vec<usize>> {
    let (h, i) = (config.hidden_size, config.intermediate_size);
    let kv = config.num_key_value_heads * config.head_dim;
    let suffix = name.splitn(4, '.').nth(3)?;
    Some(match suffix {
        "input_layernorm.weight" | "post_attention_layernorm.weight" | "self_attn.q_proj.bias" => vec![h],
        "self_attn.q_proj.weight" | "self_attn.o_proj.weight" => vec![h, h],
        "self_attn.k_proj.weight" | "self_attn.v_proj.weight" => vec![kv, h],
        "self_attn.k_proj.bias" | "self_attn.v_proj.bias" => vec![kv],
        "mlp.gate_proj.weight" | "mlp.up_proj.weight" => vec![i, h],
        "mlp.down_proj.weight" => vec![h, i],
        _ => return None,
    })
}

const MANIFEST_FIELDS: [&str; 15] = [
    "protocol",
    "stage_id",
    "model_id",
    "resolved_commit",
    "source",
    "architecture",
    "start_layer",
    "end_layer_exclusive",
    "backend",
    "quantizer",
    "activation_dtype",
    "tensors",
    "golden",
    "route_ready",
    "pack_digest",
];
const ARCH_FIELDS: [&str; 10] = [
    "model_type",
    "hidden_act",
    "hidden_size",
    "intermediate_size",
    "num_attention_heads",
    "num_key_value_heads",
    "head_dim",
    "num_hidden_layers",
    "rms_norm_eps",
    "rope_theta",
];

fn architecture(v: &Value) -> Result<(Qwen2Config, usize), ManifestError> {
    let code = "stage_manifest_architecture_invalid";
    exact_fields(v, &ARCH_FIELDS, code)?;
    if v["model_type"] != "qwen2" || v["hidden_act"] != "silu" {
        return reject(code);
    }
    let hidden = int(&v["hidden_size"], code, 1, 8192)? as usize;
    let heads = int(&v["num_attention_heads"], code, 1, hidden as u64)? as usize;
    let config = Qwen2Config {
        hidden_size: hidden,
        intermediate_size: int(&v["intermediate_size"], code, 1, 32768)? as usize,
        num_attention_heads: heads,
        num_key_value_heads: int(&v["num_key_value_heads"], code, 1, heads as u64)? as usize,
        head_dim: int(&v["head_dim"], code, 2, hidden as u64)? as usize,
        rms_norm_eps: positive_float(&v["rms_norm_eps"], code)?,
        rope_theta: positive_float(&v["rope_theta"], code)?,
    };
    let layers = int(&v["num_hidden_layers"], code, 1, 4096)? as usize;
    config.validate().map_err(|_| ManifestError(code.into()))?;
    Ok((config, layers))
}

fn golden(v: &Value) -> Result<Option<Golden>, ManifestError> {
    if v.is_null() {
        return Ok(None);
    }
    let code = "stage_manifest_golden_invalid";
    exact_fields(v, &["protocol", "generator", "seed", "seq", "output_digest"], code)?;
    if v["protocol"] != GOLDEN_PROTOCOL || v["generator"] != GOLDEN_GENERATOR {
        return reject(code);
    }
    Ok(Some(Golden {
        seed: int(&v["seed"], code, 0, MAX_SAFE_INTEGER)?,
        seq: int(&v["seq"], code, 1, MAX_GOLDEN_SEQ as u64)? as usize,
        output_digest: digest_str(&v["output_digest"], code)?.to_owned(),
    }))
}

impl StageManifest {
    /// Strictly validate an unsigned manifest document and recompute its digest.
    pub fn from_document(v: &Value) -> Result<Self, ManifestError> {
        exact_fields(v, &MANIFEST_FIELDS, "stage_manifest_fields_invalid")?;
        if v["protocol"] != MANIFEST_PROTOCOL {
            return reject("stage_manifest_protocol_invalid");
        }
        let raw = canonical::to_ascii_bytes(v).map_err(|_| ManifestError("stage_manifest_noncanonical".into()))?;
        if raw.len() > MAX_MANIFEST_BYTES {
            return reject("stage_manifest_too_large");
        }
        let mut unsigned: Map<String, Value> = v.as_object().unwrap().clone();
        unsigned.remove("pack_digest");
        let digest = canonical::sha256_prefixed(
            &canonical::to_ascii_bytes(&Value::Object(unsigned)).map_err(|_| ManifestError("stage_manifest_noncanonical".into()))?,
        );
        if v["pack_digest"] != digest.as_str() {
            return reject("stage_manifest_digest_mismatch");
        }
        let id = "stage_manifest_identity_invalid";
        let stage_id = identifier(&v["stage_id"], id)?.to_owned();
        let model_id = identifier(&v["model_id"], id)?.to_owned();
        let commit = v["resolved_commit"].as_str().unwrap_or("");
        if commit.len() != 40 || !commit.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return reject(id);
        }
        if v["backend"] != QWEN2_BACKEND {
            return reject("stage_manifest_backend_unsupported");
        }
        if v["quantizer"] != QUANTIZER {
            return reject("stage_manifest_quantizer_unsupported");
        }
        if v["activation_dtype"] != ACTIVATION_DTYPE {
            return reject("stage_manifest_activation_dtype_unsupported");
        }
        if v["route_ready"] != Value::Bool(false) {
            return reject("stage_manifest_route_ready_invalid");
        }
        let source = &v["source"];
        let sc = "stage_manifest_source_invalid";
        exact_fields(source, &["file", "sha256", "size_bytes"], sc)?;
        if source["file"] != SOURCE_FILE {
            return reject(sc);
        }
        let source_sha256 = digest_str(&source["sha256"], sc)?.to_owned();
        let source_size_bytes = int(&source["size_bytes"], sc, 9, MAX_SAFE_INTEGER)?;
        let (config, num_hidden_layers) = architecture(&v["architecture"])?;
        let lr = "stage_manifest_layer_range_invalid";
        let start = int(&v["start_layer"], lr, 0, MAX_SAFE_INTEGER)? as usize;
        let end = int(&v["end_layer_exclusive"], lr, 0, MAX_SAFE_INTEGER)? as usize;
        if !(start < end && end <= num_hidden_layers) || end - start > MAX_LAYERS_PER_STAGE {
            return reject(lr);
        }
        let names = tensor_names(start, end);
        let tc = "stage_manifest_tensors_invalid";
        let list = v["tensors"].as_array().filter(|a| a.len() == names.len()).ok_or_else(|| ManifestError(tc.into()))?;
        let mut tensors = Vec::with_capacity(list.len());
        for (name, t) in names.iter().zip(list) {
            exact_fields(t, &["name", "dtype", "shape", "size_bytes", "sha256"], tc)?;
            if t["name"] != name.as_str() {
                return reject(tc);
            }
            let dtype = t["dtype"]
                .as_str()
                .and_then(SourceDType::parse)
                .ok_or_else(|| ManifestError("stage_manifest_tensor_dtype_unsupported".into()))?;
            let shape: Vec<usize> = t["shape"]
                .as_array()
                .and_then(|a| a.iter().map(|d| d.as_u64().map(|d| d as usize)).collect::<Option<Vec<_>>>())
                .ok_or_else(|| ManifestError("stage_manifest_tensor_shape_invalid".into()))?;
            if Some(&shape) != expected_shape(&config, name).as_ref() {
                return reject("stage_manifest_tensor_shape_invalid");
            }
            let size_bytes = int(&t["size_bytes"], tc, 1, MAX_TENSOR_BYTES)?;
            if size_bytes != shape.iter().product::<usize>() as u64 * dtype.bytes() as u64 {
                return reject("stage_manifest_tensor_size_invalid");
            }
            tensors.push(TensorSpec { name: name.clone(), dtype, shape, size_bytes, sha256: digest_str(&t["sha256"], tc)?.to_owned() });
        }
        let golden = golden(&v["golden"])?;
        Ok(Self {
            stage_id,
            model_id,
            resolved_commit: commit.to_owned(),
            source_sha256,
            source_size_bytes,
            config,
            num_hidden_layers,
            start_layer: start,
            end_layer_exclusive: end,
            tensors,
            golden,
            pack_digest: digest,
            document: v.clone(),
        })
    }

    /// Verify a seed-signed manifest envelope against the pinned seed key.
    pub fn verify_signed(envelope: &Value, pinned_seed_key: &Value) -> Result<Self, ManifestError> {
        let message = membership::verify_seed_envelope(envelope, pinned_seed_key, MANIFEST_PROTOCOL)
            .map_err(|e| ManifestError(format!("stage_manifest_{}", e.0)))?;
        Self::from_document(&message)
    }

    pub fn document(&self) -> &Value {
        &self.document
    }

    pub fn tensor(&self, name: &str) -> Option<&TensorSpec> {
        self.tensors.binary_search_by(|t| t.name.as_str().cmp(name)).ok().map(|i| &self.tensors[i])
    }

    /// Raw upstream bytes the phone must hold for this stage.
    pub fn total_bytes(&self) -> u64 {
        self.tensors.iter().map(|t| t.size_bytes).sum()
    }

    /// Resident bytes once loaded: int8 values + one f32 scale per row for the
    /// matrices, f32 for the vectors (what `Qwen2Stage::resident_bytes` reports).
    pub fn resident_bytes(&self) -> u64 {
        self.tensors
            .iter()
            .map(|t| match t.shape.as_slice() {
                [rows, cols] => (rows * cols + rows * 4) as u64,
                [n] => (*n * 4) as u64,
                _ => 0,
            })
            .sum()
    }

    /// Memory admission (plan M2): refuse unless `available` covers 4x the
    /// resident footprint (weights + transient tensor + activation buffers).
    pub fn admit_memory(&self, available_bytes: u64) -> Result<(), ManifestError> {
        if available_bytes / 4 < self.resident_bytes() {
            return reject("stage_memory_insufficient");
        }
        Ok(())
    }
}

/// `splitmix64_unit_f32_v1`: `seq*hidden` values in [-1, 1), each
/// `((z >> 40) - 2^23) / 2^23` of one splitmix64 output (an exact f32), the
/// same sequence `qwen_stage_manifest.golden_input_f32` produces.
pub fn golden_input(seed: u64, seq: usize, hidden: usize) -> Vec<f32> {
    let mut state = seed;
    (0..seq * hidden)
        .map(|_| {
            state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = state;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^= z >> 31;
            ((z >> 40) as i64 - (1 << 23)) as f32 / 8_388_608.0
        })
        .collect()
}

/// `sha256:` of the little-endian f32 bytes (the host golden digest format).
pub fn output_digest(values: &[f32]) -> String {
    let mut h = Sha256::new();
    for v in values {
        h.update(v.to_le_bytes());
    }
    format!("sha256:{}", hex::encode(h.finalize()))
}

/// Run the golden input through the loaded stage; the output digest must be
/// exactly the host's (R1a: phone == host build of the same code, bit for bit).
pub fn golden_digest(stage: &Qwen2Stage, seed: u64, seq: usize) -> Result<String, ManifestError> {
    let input = golden_input(seed, seq, stage.config().hidden_size);
    let out = stage.forward(seq, &input).map_err(|e| ManifestError(e.0))?;
    Ok(output_digest(&out))
}

pub fn self_check(stage: &Qwen2Stage, golden: &Golden) -> Result<(), ManifestError> {
    if golden_digest(stage, golden.seed, golden.seq)? != golden.output_digest {
        return reject("stage_golden_mismatch");
    }
    Ok(())
}
