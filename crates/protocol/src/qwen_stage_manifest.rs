// SPDX-License-Identifier: AGPL-3.0-only
//! Signed v2 stage manifests for a phone that runs real Qwen2 decoder layers, byte-faithful to
//! v0.1.4 `mycelium_mobile/qwen_stage_manifest.py` (verification and signing side: seed
//! `route_assignment.verify_stage_pack` / `load_stage_pack` / `issue_route_assignment`, the phone
//! preparation path, and the mobile artifact-grant twin). Building a manifest from a Safetensors
//! file, exporting tensor objects and the golden input generator read or write files and are not
//! verifiers; they are not ported here.
use crate::canonical;
use crate::identity::{self, Identity};
use serde_json::{Map, Number, Value, json};
use sha2::{Digest, Sha256};

pub const MANIFEST_PROTOCOL: &str = "mycelium.mobile_stage_pack.v2";
pub const GOLDEN_PROTOCOL: &str = "mycelium.mobile_stage_golden.v1";
pub const GOLDEN_GENERATOR: &str = "splitmix64_unit_f32_v1";
pub const SIGNED_MESSAGE_PROTOCOL: &str = "mycelium.membership.signed_message.v1";
const BACKEND: &str = "mobile-rust-qwen2";
const QUANTIZER: &str = "numpy_rowwise_v1";
const ACTIVATION_DTYPE: &str = "float32";
const SOURCE_FILE: &str = "model.safetensors";

/// Source bounds of `qwen_stage_manifest.py:55-62` (sizes and counts, not durations).
const MAX_MANIFEST_BYTES: usize = 512 * 1024;
const MAX_LAYERS_PER_STAGE: i128 = 64;
const MAX_HIDDEN: i128 = 8192;
const MAX_INNER: i128 = 32768;
const MAX_TENSOR_BYTES: i128 = 1 << 30;
const MAX_GOLDEN_SEQ: i128 = 64;
const MAX_SAFE_INTEGER: i128 = (1 << 53) - 1;

const MATRIX_SUFFIXES: [&str; 7] = [
    "self_attn.q_proj.weight",
    "self_attn.k_proj.weight",
    "self_attn.v_proj.weight",
    "self_attn.o_proj.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
];
const VECTOR_SUFFIXES: [&str; 5] = [
    "input_layernorm.weight",
    "self_attn.q_proj.bias",
    "self_attn.k_proj.bias",
    "self_attn.v_proj.bias",
    "post_attention_layernorm.weight",
];
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
const SOURCE_FIELDS: [&str; 3] = ["file", "sha256", "size_bytes"];
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
const TENSOR_FIELDS: [&str; 5] = ["name", "dtype", "shape", "size_bytes", "sha256"];
const GOLDEN_FIELDS: [&str; 5] = ["protocol", "generator", "seed", "seq", "output_digest"];
const ENVELOPE_FIELDS: [&str; 4] = ["protocol", "message", "signature", "verification_key"];

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct StageManifestError(pub &'static str);

fn reject<T>(code: &'static str) -> Result<T, StageManifestError> {
    Err(StageManifestError(code))
}

fn exact_fields(value: &Value, fields: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|f| m.contains_key(*f)))
}

/// `canonical_ascii`: sorted, compact, `ensure_ascii=True`, no NaN.
fn canonical_ascii(value: &Value) -> Result<Vec<u8>, StageManifestError> {
    canonical::to_ascii_bytes(value).map_err(|_| StageManifestError("stage_manifest_noncanonical"))
}

/// `manifest_digest`.
pub fn manifest_digest(unsigned: &Value) -> Result<String, StageManifestError> {
    Ok(format!(
        "sha256:{}",
        hex::encode(Sha256::digest(canonical_ascii(unsigned)?))
    ))
}

/// The (non-bool) Python `int` value of a JSON number, when it fits `i128`.
fn python_int(value: &Value) -> Option<i128> {
    let text = value.as_number()?.to_string();
    if text.bytes().any(|c| matches!(c, b'.' | b'e' | b'E')) {
        return None;
    }
    text.parse().ok()
}

fn int_in(
    value: &Value,
    code: &'static str,
    minimum: i128,
    maximum: i128,
) -> Result<i128, StageManifestError> {
    python_int(value)
        .filter(|n| (minimum..=maximum).contains(n))
        .map_or_else(|| reject(code), Ok)
}

/// `_positive_float`: a JSON float (not an int, not a bool), finite and > 0.
fn positive_float(value: &Value, code: &'static str) -> Result<f64, StageManifestError> {
    let float_typed = value.as_number().is_some_and(|n| {
        n.to_string()
            .bytes()
            .any(|c| matches!(c, b'.' | b'e' | b'E'))
    });
    value
        .as_number()
        .and_then(Number::as_f64)
        .filter(|f| float_typed && f.is_finite() && *f > 0.0)
        .map_or_else(|| reject(code), Ok)
}

fn lower_hex(s: &str) -> bool {
    s.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
}

fn digest_text(value: &Value) -> bool {
    value
        .as_str()
        .and_then(|s| s.strip_prefix("sha256:"))
        .is_some_and(|h| h.len() == 64 && lower_hex(h))
}

fn commit_text(value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|s| s.len() == 40 && lower_hex(s))
}

/// `[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z`.
fn id_text(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        let b = s.as_bytes();
        (1..=256).contains(&b.len())
            && b[0].is_ascii_alphanumeric()
            && b.iter().all(|c| {
                c.is_ascii_alphanumeric()
                    || matches!(c, b'.' | b'_' | b':' | b'/' | b'@' | b'+' | b'-')
            })
    })
}

/// `tensor_names`: every upstream tensor a layer range needs, sorted as strings.
pub fn tensor_names(start_layer: i128, end_layer_exclusive: i128) -> Vec<String> {
    let mut names: Vec<String> = (start_layer..end_layer_exclusive)
        .flat_map(|layer| {
            MATRIX_SUFFIXES
                .iter()
                .chain(VECTOR_SUFFIXES.iter())
                .map(move |suffix| format!("model.layers.{layer}.{suffix}"))
        })
        .collect();
    names.sort();
    names
}

/// `expected_shapes` for one layer, keyed by tensor name.
fn expected_shapes(architecture: &Architecture, layer: i128) -> Vec<(String, Vec<i128>)> {
    let (h, i) = (architecture.hidden, architecture.inner);
    let kv = architecture.kv_heads * architecture.head_dim;
    let n = format!("model.layers.{layer}.");
    vec![
        (format!("{n}input_layernorm.weight"), vec![h]),
        (format!("{n}self_attn.q_proj.weight"), vec![h, h]),
        (format!("{n}self_attn.q_proj.bias"), vec![h]),
        (format!("{n}self_attn.k_proj.weight"), vec![kv, h]),
        (format!("{n}self_attn.k_proj.bias"), vec![kv]),
        (format!("{n}self_attn.v_proj.weight"), vec![kv, h]),
        (format!("{n}self_attn.v_proj.bias"), vec![kv]),
        (format!("{n}self_attn.o_proj.weight"), vec![h, h]),
        (format!("{n}post_attention_layernorm.weight"), vec![h]),
        (format!("{n}mlp.gate_proj.weight"), vec![i, h]),
        (format!("{n}mlp.up_proj.weight"), vec![i, h]),
        (format!("{n}mlp.down_proj.weight"), vec![h, i]),
    ]
}

struct Architecture {
    hidden: i128,
    inner: i128,
    kv_heads: i128,
    head_dim: i128,
    layers: i128,
    normalized: Value,
}

/// `_verify_architecture`.
fn verify_architecture(value: &Value) -> Result<Architecture, StageManifestError> {
    const CODE: &str = "stage_manifest_architecture_invalid";
    if !exact_fields(value, &ARCH_FIELDS) {
        return reject(CODE);
    }
    if value["model_type"] != "qwen2" || value["hidden_act"] != "silu" {
        return reject(CODE);
    }
    let hidden = int_in(&value["hidden_size"], CODE, 1, MAX_HIDDEN)?;
    let inner = int_in(&value["intermediate_size"], CODE, 1, MAX_INNER)?;
    let heads = int_in(&value["num_attention_heads"], CODE, 1, hidden)?;
    let kv_heads = int_in(&value["num_key_value_heads"], CODE, 1, heads)?;
    let head_dim = int_in(&value["head_dim"], CODE, 2, hidden)?;
    let layers = int_in(&value["num_hidden_layers"], CODE, 1, 4096)?;
    let eps = positive_float(&value["rms_norm_eps"], CODE)?;
    let theta = positive_float(&value["rope_theta"], CODE)?;
    if heads % kv_heads != 0 || head_dim % 2 != 0 || head_dim * heads != hidden || theta <= 1.0 {
        return reject(CODE);
    }
    let normalized = json!({
        "model_type": "qwen2",
        "hidden_act": "silu",
        "hidden_size": hidden as i64,
        "intermediate_size": inner as i64,
        "num_attention_heads": heads as i64,
        "num_key_value_heads": kv_heads as i64,
        "head_dim": head_dim as i64,
        "num_hidden_layers": layers as i64,
        "rms_norm_eps": value["rms_norm_eps"],
        "rope_theta": value["rope_theta"],
    });
    let _ = (eps, theta);
    Ok(Architecture {
        hidden,
        inner,
        kv_heads,
        head_dim,
        layers,
        normalized,
    })
}

/// `_verify_golden`: `None` stays `None`.
fn verify_golden(value: &Value) -> Result<(), StageManifestError> {
    const CODE: &str = "stage_manifest_golden_invalid";
    if value.is_null() {
        return Ok(());
    }
    if !exact_fields(value, &GOLDEN_FIELDS)
        || value["protocol"] != GOLDEN_PROTOCOL
        || value["generator"] != GOLDEN_GENERATOR
    {
        return reject(CODE);
    }
    int_in(&value["seed"], CODE, 0, MAX_SAFE_INTEGER)?;
    int_in(&value["seq"], CODE, 1, MAX_GOLDEN_SEQ)?;
    if !digest_text(&value["output_digest"]) {
        return reject(CODE);
    }
    Ok(())
}

/// Python list equality of a JSON array against integers (`[8.0] == [8]`, `[True] == [1]`).
fn shape_equals(value: &Value, expected: &[i128]) -> bool {
    let Some(items) = value.as_array().filter(|a| a.len() == expected.len()) else {
        return false;
    };
    items.iter().zip(expected).all(|(item, want)| match item {
        Value::Bool(b) => i128::from(*b) == *want,
        Value::Number(n) => match python_int(item) {
            Some(i) => i == *want,
            None => n
                .as_f64()
                .is_some_and(|f| f.fract() == 0.0 && f.abs() < 1.0e30 && f as i128 == *want),
        },
        _ => false,
    })
}

/// `verify_stage_manifest`: strictly validate one v2 manifest and recompute its `pack_digest`.
pub fn verify_stage_manifest(document: &Value) -> Result<Value, StageManifestError> {
    if !exact_fields(document, &MANIFEST_FIELDS) {
        return reject("stage_manifest_fields_invalid");
    }
    if document["protocol"] != MANIFEST_PROTOCOL {
        return reject("stage_manifest_protocol_invalid");
    }
    let raw = canonical_ascii(document)?;
    if raw.len() > MAX_MANIFEST_BYTES {
        return reject("stage_manifest_too_large");
    }
    let mut unsigned = document.clone();
    if let Some(map) = unsigned.as_object_mut() {
        map.remove("pack_digest");
    }
    if document["pack_digest"] != manifest_digest(&unsigned)?.as_str() {
        return reject("stage_manifest_digest_mismatch");
    }
    const IDENTITY: &str = "stage_manifest_identity_invalid";
    if !id_text(&document["stage_id"]) || !id_text(&document["model_id"]) {
        return reject(IDENTITY);
    }
    if !commit_text(&document["resolved_commit"]) {
        return reject(IDENTITY);
    }
    if document["backend"] != BACKEND {
        return reject("stage_manifest_backend_unsupported");
    }
    if document["quantizer"] != QUANTIZER {
        return reject("stage_manifest_quantizer_unsupported");
    }
    if document["activation_dtype"] != ACTIVATION_DTYPE {
        return reject("stage_manifest_activation_dtype_unsupported");
    }
    if document["route_ready"] != Value::Bool(false) {
        return reject("stage_manifest_route_ready_invalid");
    }
    const SOURCE: &str = "stage_manifest_source_invalid";
    let source = &document["source"];
    if !exact_fields(source, &SOURCE_FIELDS) || source["file"] != SOURCE_FILE {
        return reject(SOURCE);
    }
    if !digest_text(&source["sha256"]) {
        return reject(SOURCE);
    }
    int_in(&source["size_bytes"], SOURCE, 9, MAX_SAFE_INTEGER)?;
    let architecture = verify_architecture(&document["architecture"])?;
    if architecture.normalized != document["architecture"] {
        return reject("stage_manifest_architecture_invalid");
    }
    const RANGE: &str = "stage_manifest_layer_range_invalid";
    let start = int_in(&document["start_layer"], RANGE, 0, MAX_SAFE_INTEGER)?;
    let end = int_in(&document["end_layer_exclusive"], RANGE, 0, MAX_SAFE_INTEGER)?;
    if !(start < end && end <= architecture.layers) || end - start > MAX_LAYERS_PER_STAGE {
        return reject(RANGE);
    }
    const TENSORS: &str = "stage_manifest_tensors_invalid";
    let names = tensor_names(start, end);
    let Some(tensors) = document["tensors"]
        .as_array()
        .filter(|t| t.len() == names.len())
    else {
        return reject(TENSORS);
    };
    let shapes: Vec<(String, Vec<i128>)> = (start..end)
        .flat_map(|layer| expected_shapes(&architecture, layer))
        .collect();
    for (expected_name, tensor) in names.iter().zip(tensors) {
        if !exact_fields(tensor, &TENSOR_FIELDS) || tensor["name"] != expected_name.as_str() {
            return reject(TENSORS);
        }
        let width: i128 = match tensor["dtype"].as_str() {
            Some("BF16") => 2,
            Some("F32") => 4,
            _ => return reject("stage_manifest_tensor_dtype_unsupported"),
        };
        let shape = shapes
            .iter()
            .find(|(n, _)| n == expected_name)
            .map(|(_, s)| s.as_slice())
            .unwrap_or_default();
        if !shape_equals(&tensor["shape"], shape) {
            return reject("stage_manifest_tensor_shape_invalid");
        }
        let size = int_in(&tensor["size_bytes"], TENSORS, 1, MAX_TENSOR_BYTES)?;
        if size != shape.iter().product::<i128>() * width {
            return reject("stage_manifest_tensor_size_invalid");
        }
        if !digest_text(&tensor["sha256"]) {
            return reject(TENSORS);
        }
    }
    verify_golden(&document["golden"])?;
    serde_json::from_slice(&raw).map_err(|_| StageManifestError("stage_manifest_noncanonical"))
}

/// `is_signed_stage_manifest`.
pub fn is_signed_stage_manifest(document: &Value) -> bool {
    document["protocol"] == SIGNED_MESSAGE_PROTOCOL
        && document["message"].is_object()
        && document["message"]["protocol"] == MANIFEST_PROTOCOL
}

/// `sign_stage_manifest`: seed-sign a verified manifest as a member-channel signed message.
pub fn sign_stage_manifest(
    manifest: &Value,
    signer: &Identity,
) -> Result<Value, StageManifestError> {
    let verified = verify_stage_manifest(manifest)?;
    let signature = signer
        .sign(&verified)
        .map_err(|_| StageManifestError("stage_manifest_signature_invalid"))?;
    Ok(json!({
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "message": verified,
        "signature": signature,
        "verification_key": signer.public_key_record(),
    }))
}

/// Python `dict(value)` over JSON data: a mapping, a sequence of key/value pairs, or empty.
/// `None` is the `TypeError`/`ValueError` the oracle raises.
fn python_dict(value: &Value) -> Option<Map<String, Value>> {
    match value {
        Value::Object(map) => Some(map.clone()),
        Value::String(text) if text.is_empty() => Some(Map::new()),
        Value::Array(items) => {
            let mut map = Map::new();
            for item in items {
                let (key, val) = match item {
                    Value::Array(pair) if pair.len() == 2 => (pair[0].clone(), pair[1].clone()),
                    Value::String(pair) if pair.chars().count() == 2 => {
                        let mut chars = pair.chars();
                        (
                            Value::String(chars.next()?.to_string()),
                            Value::String(chars.next()?.to_string()),
                        )
                    }
                    _ => return None,
                };
                // A non-text key can never equal a JSON object's key; keep it distinct.
                let key = key
                    .as_str()
                    .map_or_else(|| format!("\u{0}non-text:{key}"), str::to_owned);
                map.insert(key, val);
            }
            Some(map)
        }
        _ => None,
    }
}

/// `verify_signed_stage_manifest`: verify a seed-signed manifest against the pinned seed key.
pub fn verify_signed_stage_manifest(
    envelope: &Value,
    pinned_key_record: &Value,
) -> Result<Value, StageManifestError> {
    const ENVELOPE: &str = "stage_manifest_envelope_invalid";
    if !exact_fields(envelope, &ENVELOPE_FIELDS) || envelope["protocol"] != SIGNED_MESSAGE_PROTOCOL
    {
        return reject(ENVELOPE);
    }
    let carried = python_dict(&envelope["verification_key"])
        .ok_or(StageManifestError("stage_manifest_envelope_invalid"))?;
    let pinned = python_dict(pinned_key_record)
        .ok_or(StageManifestError("stage_manifest_signer_mismatch"))?;
    if carried != pinned {
        return reject("stage_manifest_signer_mismatch");
    }
    let manifest = verify_stage_manifest(&envelope["message"])?;
    let valid = python_dict(&envelope["signature"]).is_some_and(|signature| {
        identity::verify(
            &manifest,
            &Value::Object(signature),
            &Value::Object(pinned.clone()),
        )
        .is_ok()
    });
    if !valid {
        return reject("stage_manifest_signature_invalid");
    }
    Ok(manifest)
}

/// Python `float(value)` for the scalars a Hugging Face config carries.
fn python_float(value: Option<&Value>, default: f64) -> Result<f64, StageManifestError> {
    const CODE: &str = "stage_manifest_architecture_invalid";
    match value {
        None => Ok(default),
        Some(Value::Bool(b)) => Ok(f64::from(u8::from(*b))),
        Some(Value::Number(n)) => n.as_f64().map_or_else(|| reject(CODE), Ok),
        Some(Value::String(text)) => {
            let trimmed = text.trim_matches(crate::invite::py_space);
            // Python `float()`: an underscore is allowed only between two digits.
            let bytes = trimmed.as_bytes();
            let grouped = bytes.iter().enumerate().all(|(at, c)| {
                *c != b'_'
                    || (at > 0
                        && bytes.get(at + 1).is_some_and(u8::is_ascii_digit)
                        && bytes[at - 1].is_ascii_digit())
            });
            if !grouped {
                return reject(CODE);
            }
            let trimmed = trimmed.replace('_', "");
            trimmed.parse::<f64>().map_or_else(|_| reject(CODE), Ok)
        }
        Some(_) => reject(CODE),
    }
}

fn float_value(f: f64) -> Value {
    Number::from_f64(f).map_or(Value::Null, Value::Number)
}

/// `architecture_from_hf_config`: the decoder-layer fields of a Hugging Face Qwen2 `config.json`.
pub fn architecture_from_hf_config(config: &Value) -> Result<Value, StageManifestError> {
    const CODE: &str = "stage_manifest_architecture_invalid";
    let Some(map) = config.as_object() else {
        return reject(CODE);
    };
    if map.get("model_type") != Some(&Value::String("qwen2".to_owned())) {
        return reject("stage_manifest_model_type_unsupported");
    }
    if map
        .get("hidden_act")
        .unwrap_or(&Value::String("silu".to_owned()))
        != "silu"
    {
        return reject("stage_manifest_activation_unsupported");
    }
    let get = |key: &str| map.get(key).cloned().unwrap_or(Value::Null);
    let hidden = int_in(&get("hidden_size"), CODE, 1, MAX_SAFE_INTEGER)?;
    let heads = int_in(&get("num_attention_heads"), CODE, 1, MAX_SAFE_INTEGER)?;
    let architecture = json!({
        "model_type": "qwen2",
        "hidden_act": "silu",
        "hidden_size": hidden as i64,
        "intermediate_size": get("intermediate_size"),
        "num_attention_heads": heads as i64,
        "num_key_value_heads": get("num_key_value_heads"),
        "head_dim": if hidden % heads == 0 { hidden / heads } else { 0 } as i64,
        "num_hidden_layers": get("num_hidden_layers"),
        "rms_norm_eps": float_value(python_float(map.get("rms_norm_eps"), 0.0)?),
        "rope_theta": float_value(python_float(map.get("rope_theta"), 0.0)?),
    });
    verify_architecture(&architecture).map(|a| a.normalized)
}

/// `build_stage_manifest`: assemble, digest and verify one v2 manifest from already-known parts
/// (`parts` carries the oracle's keyword arguments: `stage_id`, `model_id`, `resolved_commit`,
/// `source_sha256`, `source_size_bytes`, `architecture`, `start_layer`, `end_layer_exclusive`,
/// `tensors`, `golden`).
pub fn build_stage_manifest(parts: &Value) -> Result<Value, StageManifestError> {
    let invalid = StageManifestError("stage_manifest_noncanonical");
    let get = |key: &str| parts.get(key).cloned().ok_or(invalid.clone());
    let mut tensors: Vec<Value> = get("tensors")?.as_array().cloned().ok_or(invalid.clone())?;
    if tensors
        .iter()
        .any(|t| !t.get("name").is_some_and(Value::is_string))
    {
        return Err(invalid);
    }
    tensors.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
    let mut unsigned = Map::new();
    for (key, value) in [
        ("protocol", json!(MANIFEST_PROTOCOL)),
        ("stage_id", get("stage_id")?),
        ("model_id", get("model_id")?),
        ("resolved_commit", get("resolved_commit")?),
        (
            "source",
            json!({"file": SOURCE_FILE, "sha256": get("source_sha256")?, "size_bytes": get("source_size_bytes")?}),
        ),
        ("architecture", get("architecture")?),
        ("start_layer", get("start_layer")?),
        ("end_layer_exclusive", get("end_layer_exclusive")?),
        ("backend", json!(BACKEND)),
        ("quantizer", json!(QUANTIZER)),
        ("activation_dtype", json!(ACTIVATION_DTYPE)),
        ("tensors", Value::Array(tensors)),
        ("golden", get("golden")?),
        ("route_ready", json!(false)),
    ] {
        unsigned.insert(key.to_owned(), value);
    }
    let mut document = Value::Object(unsigned);
    let digest = manifest_digest(&document)?;
    if let Some(map) = document.as_object_mut() {
        map.insert("pack_digest".to_owned(), Value::String(digest));
    }
    verify_stage_manifest(&document)
}
