// SPDX-License-Identifier: AGPL-3.0-only
//! `physical_deployment.compile_local_model_manifest` (mycelium_qualification/physical_deployment.py:47-1115 @
//! f4e366e9) with `model_manifest.compile_model_manifest` and the architecture adapter table
//! (`model_adapters.py`): the native manifest compile that `serving.prepare_local_source` runs on a downloaded
//! Hugging Face snapshot (SE-L10-MANIFEST-COMPILE). The local snapshot is read without trusting it: every path is a
//! safe relative one, links may only be the final component and only the Hugging Face `../../blobs/<sha>` kind, JSON
//! has no duplicate keys, a safetensors header is fully bounds- and overlap-checked, and every referenced file is
//! hashed. The result is the immutable manifest with its `manifest_digest`.
//!
//! Checked against the executed oracle by `tests/local_manifest.rs` (101 cases, `oracle_manifest.json`, plus the
//! cached real Qwen2.5-0.5B snapshot as an ignored test). Differences, named: a JSON object's key ORDER is not
//! observable here (serde_json sorts), so an index with several defects may report a different one first than
//! Python; the `source_etag` metadata branch is not reachable from a local source and is not ported; a safetensors
//! header in UTF-16/32 (which `json.loads(bytes)` auto-detects) is read as UTF-8 only.
use planner::runtime as rt;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

#[cfg(test)]
#[path = "local_manifest_bounds_tests.rs"]
mod bounds_tests;

const MODEL_INDEX_FILE: &str = "model.safetensors.index.json";
const MONOLITHIC_WEIGHT_FILE: &str = "model.safetensors";
/// `MAX_SAFETENSORS_HEADER_BYTES` (`physical_deployment.py:53`): a registry id (SE-L10-ACQ-BOUNDS, L1b).
fn max_safetensors_header_bytes() -> u64 {
    budgets::BudgetId::PyMyceliumQualificationPhysicalDeploymentMaxSafetensorsHeaderBytesL53
        .bytes()
        .map_or(0, |b| b.get())
}

/// `LocalModelSource`.
#[derive(Clone, Debug)]
pub struct LocalModelSource {
    pub root: PathBuf,
    pub model_id: String,
    pub requested_revision: String,
    pub resolved_commit: String,
    /// `Err(())`: a value that is not a tuple of paths.
    pub tokenizer_assets: Result<Vec<String>, ()>,
}
/// `PhysicalDeploymentError("invalid_local_model_source:<reason>")`; the message is the oracle's.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompileError(pub String);
type R<T> = Result<T, CompileError>;
fn invalid<T>(reason: &str) -> R<T> {
    Err(CompileError(format!("invalid_local_model_source:{reason}")))
}

fn is_commit(s: &str) -> bool {
    s.len() == 40 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}
fn is_sha256_hex(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}
fn is_blob_name(s: &str) -> bool {
    is_commit(s) || is_sha256_hex(s)
}
/// `PurePosixPath(value).parts` for a relative path (empty and `.` parts vanish; a leading `/` is a root).
fn posix_parts(value: &str) -> (bool, Vec<&str>) {
    (
        value.starts_with('/'),
        value
            .split('/')
            .filter(|p| !p.is_empty() && *p != ".")
            .collect(),
    )
}
/// `_safe_local_relative_path`.
fn safe_relative(value: Option<&str>, field: &str) -> R<Vec<String>> {
    let unsafe_path = || invalid(&format!("unsafe_{field}_path"));
    let Some(value) = value else {
        return unsafe_path();
    };
    if value.is_empty()
        || value.chars().count() as u64
            > budgets::BudgetId::PhysicalDeploymentRelativePathMaxChars
                .count()
                .map_or(0, |n| n.get())
        || !value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._/-".contains(&b))
    {
        return unsafe_path();
    }
    let (absolute, parts) = posix_parts(value);
    if absolute
        || parts.len() as u64
            > budgets::BudgetId::PhysicalDeploymentRelativePathMaxParts
                .count()
                .map_or(0, |n| n.get())
        || parts.contains(&"..")
    {
        return unsafe_path();
    }
    Ok(parts.into_iter().map(str::to_owned).collect())
}
fn join(root: &Path, relative: &[String]) -> PathBuf {
    relative
        .iter()
        .fold(root.to_path_buf(), |p, part| p.join(part))
}
/// `_resolve_huggingface_snapshot_link`.
fn resolve_hf_link(root: &Path, candidate: &Path) -> R<PathBuf> {
    let Ok(target) = fs::read_link(candidate) else {
        return invalid("source_path_symlink");
    };
    let target_text = target.to_string_lossy().into_owned();
    let (absolute, parts) = posix_parts(&target_text);
    let name = |p: &Path| p.file_name().map(|n| n.to_string_lossy().into_owned());
    let snapshots_parent = root.parent();
    if snapshots_parent.and_then(name).as_deref() != Some("snapshots")
        || !name(root).is_some_and(|n| is_commit(&n))
        || absolute
        || parts.len() != 4
        || parts[..3] != ["..", "..", "blobs"]
        || !is_blob_name(parts[3])
    {
        return invalid("source_path_symlink");
    }
    let blob_root = root
        .parent()
        .and_then(Path::parent)
        .map(|p| p.join("blobs"));
    let Some(blob_root) = blob_root else {
        return invalid("source_path_symlink");
    };
    if fs::symlink_metadata(&blob_root).is_ok_and(|m| m.file_type().is_symlink()) {
        return invalid("source_path_symlink");
    }
    let resolved_blob_root = fs::canonicalize(&blob_root);
    let resolved = fs::canonicalize(root.join(&target));
    let (Ok(resolved_blob_root), Ok(resolved)) = (resolved_blob_root, resolved) else {
        return invalid("source_path_escape");
    };
    if resolved.strip_prefix(&resolved_blob_root).is_err() {
        return invalid("source_path_escape");
    }
    let Ok(meta) = fs::symlink_metadata(&resolved) else {
        return invalid("source_path_escape");
    };
    if resolved.parent() != Some(resolved_blob_root.as_path())
        || meta.file_type().is_symlink()
        || !meta.is_file()
    {
        return invalid("source_path_symlink");
    }
    Ok(resolved)
}
/// `_resolve_source_path`.
fn resolve_source_path(root: &Path, relative: &[String], required: bool) -> R<Option<PathBuf>> {
    let mut current = root.to_path_buf();
    for (index, part) in relative.iter().enumerate() {
        current.push(part);
        if fs::symlink_metadata(&current).is_ok_and(|m| m.file_type().is_symlink()) {
            if index != relative.len() - 1 {
                return invalid("source_path_symlink");
            }
            return resolve_hf_link(root, &current).map(Some);
        }
        if !current.exists() {
            return if required {
                invalid("source_path_missing")
            } else {
                Ok(None)
            };
        }
    }
    match fs::canonicalize(join(root, relative)) {
        Ok(resolved) if resolved.strip_prefix(root).is_ok() => Ok(Some(resolved)),
        _ => invalid("source_path_escape"),
    }
}
/// `_stat_source_regular`.
fn stat_regular(path: &Path) -> R<fs::Metadata> {
    match fs::symlink_metadata(path) {
        Err(_) => invalid("source_file_unavailable"),
        Ok(m) if !m.is_file() => invalid("source_file_not_regular"),
        Ok(m) => Ok(m),
    }
}
fn open_no_follow(path: &Path) -> R<fs::File> {
    fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .or_else(|_| invalid("source_file_open_failed"))
}

/// JSON with no duplicate keys (`_reject_duplicate_json_keys`); NaN and the infinities are not JSON here either.
/// The value is parsed by serde_json itself (so numbers keep their exact text); a second, value-ignoring pass
/// over the same text looks for a repeated key in any object.
fn strict_json(bytes: &[u8]) -> Option<Value> {
    use serde::de::{DeserializeSeed, MapAccess, SeqAccess, Visitor};
    use std::{cell::Cell, collections::HashSet};
    struct Walk<'a>(&'a Cell<bool>);
    impl<'de> DeserializeSeed<'de> for Walk<'_> {
        type Value = ();
        fn deserialize<D: serde::Deserializer<'de>>(self, d: D) -> Result<(), D::Error> {
            d.deserialize_any(self)
        }
    }
    impl<'de> Visitor<'de> for Walk<'_> {
        type Value = ();
        fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
            f.write_str("JSON")
        }
        fn visit_bool<E>(self, _: bool) -> Result<(), E> {
            Ok(())
        }
        fn visit_i64<E>(self, _: i64) -> Result<(), E> {
            Ok(())
        }
        fn visit_u64<E>(self, _: u64) -> Result<(), E> {
            Ok(())
        }
        fn visit_f64<E>(self, _: f64) -> Result<(), E> {
            Ok(())
        }
        fn visit_str<E>(self, _: &str) -> Result<(), E> {
            Ok(())
        }
        fn visit_unit<E>(self) -> Result<(), E> {
            Ok(())
        }
        fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
            while seq.next_element_seed(Walk(self.0))?.is_some() {}
            Ok(())
        }
        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
            let mut seen: HashSet<String> = HashSet::new();
            while let Some(key) = map.next_key::<String>()? {
                if !seen.insert(key) {
                    self.0.set(true);
                }
                map.next_value_seed(Walk(self.0))?;
            }
            Ok(())
        }
    }
    let text = std::str::from_utf8(bytes).ok()?;
    let value: Value = serde_json::from_str(text).ok()?;
    let duplicate = Cell::new(false);
    let mut de = serde_json::Deserializer::from_str(text);
    Walk(&duplicate).deserialize(&mut de).ok()?;
    (!duplicate.get()).then_some(value)
}
/// `_read_source_json`.
fn read_source_json(root: &Path, relative: &[String]) -> R<Value> {
    let Some(path) = resolve_source_path(root, relative, true)? else {
        return invalid("source_path_missing");
    };
    if stat_regular(&path)?.len() == 0 {
        return invalid("json_file_empty");
    }
    let mut payload = Vec::new();
    if open_no_follow(&path)?.read_to_end(&mut payload).is_err() {
        return invalid("json_file_read_failed");
    }
    strict_json(&payload).map_or_else(|| invalid("invalid_json"), Ok)
}

fn dtype_bytes(dtype: &str) -> Option<u128> {
    Some(match dtype {
        "BOOL" | "U8" | "I8" | "F8_E4M3" | "F8_E5M2" => 1,
        "I16" | "U16" | "F16" | "BF16" => 2,
        "I32" | "U32" | "F32" => 4,
        "I64" | "U64" | "F64" => 8,
        _ => return None,
    })
}
fn integer(v: &Value) -> Option<i128> {
    v.as_i64()
        .map(i128::from)
        .or_else(|| v.as_u64().map(i128::from))
}
/// `_read_safetensors_header`: the sorted tensor names.
fn read_safetensors_header(path: &Path) -> R<Vec<String>> {
    let mut file = open_no_follow(path)?;
    let Ok(meta) = file.metadata() else {
        return invalid("safetensors_header_read_failed");
    };
    if !meta.is_file() {
        return invalid("safetensors_not_regular");
    }
    let file_size = meta.len();
    let mut prefix = [0u8; 8];
    let mut got = 0;
    while got < 8 {
        match file.read(&mut prefix[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return invalid("safetensors_header_read_failed"),
        }
    }
    if got != 8 {
        return invalid("safetensors_header_truncated");
    }
    let header_length = u64::from_le_bytes(prefix);
    if header_length == 0
        || header_length > max_safetensors_header_bytes()
        || header_length > file_size.saturating_sub(8)
    {
        return invalid("safetensors_header_size");
    }
    let mut header_bytes = vec![0u8; header_length as usize];
    if file.read_exact(&mut header_bytes).is_err() {
        return invalid("safetensors_header_truncated");
    }
    let Some(header) = strict_json(&header_bytes) else {
        return invalid("safetensors_header_json");
    };
    let Value::Object(mut header) = header else {
        return invalid("safetensors_header_object");
    };
    if header.is_empty() {
        return invalid("safetensors_header_object");
    }
    if let Some(metadata) = header.remove("__metadata__")
        && !metadata.is_null()
        && !metadata
            .as_object()
            .is_some_and(|m| m.values().all(Value::is_string))
    {
        return invalid("safetensors_metadata_invalid");
    }
    if header.is_empty() {
        return invalid("safetensors_no_tensors");
    }
    let data_size = i128::from(file_size - 8 - header_length);
    let mut intervals: Vec<(i128, i128, String)> = Vec::new();
    let mut names: Vec<String> = Vec::new();
    for (name, entry) in &header {
        let Some(entry) = entry.as_object().filter(|_| !name.is_empty()) else {
            return invalid("safetensors_tensor_invalid");
        };
        if entry.len() != 3
            || !["dtype", "shape", "data_offsets"]
                .iter()
                .all(|k| entry.contains_key(*k))
        {
            return invalid("safetensors_tensor_metadata_invalid");
        }
        let Some(per) = entry["dtype"].as_str().and_then(dtype_bytes) else {
            return invalid("safetensors_dtype_unsupported");
        };
        let dims: Option<Vec<i128>> = entry["shape"]
            .as_array()
            .and_then(|a| a.iter().map(|d| integer(d).filter(|d| *d >= 0)).collect());
        let Some(dims) = dims else {
            return invalid("safetensors_shape_invalid");
        };
        let offsets: Option<Vec<i128>> = entry["data_offsets"]
            .as_array()
            .filter(|a| a.len() == 2)
            .and_then(|a| a.iter().map(integer).collect());
        let Some(offsets) = offsets else {
            return invalid("safetensors_offsets_invalid");
        };
        let (start, end) = (offsets[0], offsets[1]);
        if start < 0 || end < start || end > data_size {
            return invalid("safetensors_offsets_out_of_bounds");
        }
        let mut count: u128 = 1;
        let mut overflow = false;
        for d in &dims {
            match count.checked_mul(*d as u128) {
                Some(c) => count = c,
                None => overflow = true,
            }
        }
        if overflow || (end - start) as u128 != count.saturating_mul(per) {
            return invalid("safetensors_tensor_size_mismatch");
        }
        names.push(name.clone());
        intervals.push((start, end, name.clone()));
    }
    intervals.sort();
    let (mut previous_end, mut cursor) = (0i128, 0i128);
    for (start, end, _) in &intervals {
        if *start < previous_end {
            return invalid("safetensors_data_overlap");
        }
        previous_end = *end;
        if *start > cursor {
            return invalid("safetensors_data_gap");
        }
        cursor = *end;
    }
    if cursor != data_size {
        return invalid("safetensors_trailing_data");
    }
    names.sort();
    Ok(names)
}
fn sha256_file(path: &Path) -> R<String> {
    let mut file = open_no_follow(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        match file.read(&mut buffer) {
            Ok(0) => break,
            Ok(n) => hasher.update(&buffer[..n]),
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return invalid("source_file_open_failed"),
        }
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// `_validate_local_model_source`: the resolved root and the tokenizer assets.
fn validate_source(source: &LocalModelSource) -> R<PathBuf> {
    if fs::symlink_metadata(&source.root).is_ok_and(|m| m.file_type().is_symlink()) {
        return invalid("root_symlink");
    }
    let Ok(root) = fs::canonicalize(&source.root) else {
        return invalid("root_missing");
    };
    if !root.is_dir() {
        return invalid("root_not_directory");
    }
    let id = &source.model_id;
    if !id.contains('/') || id.trim().is_empty() || id != id.trim() {
        return invalid("invalid_model_id");
    }
    let revision = &source.requested_revision;
    if revision.trim().is_empty() || revision != revision.trim() || revision.contains('\0') {
        return invalid("invalid_requested_revision");
    }
    if !is_commit(&source.resolved_commit) {
        return invalid("invalid_resolved_commit");
    }
    let Ok(assets) = &source.tokenizer_assets else {
        return invalid("invalid_tokenizer_assets");
    };
    let mut seen: Vec<String> = Vec::new();
    for asset in assets {
        let path = safe_relative(Some(asset), "tokenizer_asset")?.join("/");
        if seen.contains(&path) {
            return invalid("duplicate_tokenizer_asset");
        }
        seen.push(path);
    }
    Ok(root)
}

type WeightMap = BTreeMap<String, String>;
/// `_normalize_checkpoint_index`: the validated weight map.
fn normalize_checkpoint_index(index: &Value) -> R<WeightMap> {
    let Some(index) = index.as_object() else {
        return invalid("checkpoint_index_not_object");
    };
    let Some(weight_map) = index
        .get("weight_map")
        .and_then(Value::as_object)
        .filter(|m| !m.is_empty())
    else {
        return invalid("checkpoint_index_missing_weight_map");
    };
    let mut out = WeightMap::new();
    for (name, file) in weight_map {
        if name.is_empty() {
            return invalid("checkpoint_index_invalid_tensor_name");
        }
        let relative = safe_relative(file.as_str(), "weight")?;
        out.insert(name.clone(), relative.join("/"));
    }
    Ok(out)
}
/// `_load_checkpoint_index`: the weight map and whether the index was synthesized from a monolithic file.
fn load_checkpoint_index(root: &Path) -> R<(WeightMap, bool)> {
    let index_relative = safe_relative(Some(MODEL_INDEX_FILE), "index")?;
    if resolve_source_path(root, &index_relative, false)?.is_some() {
        return Ok((
            normalize_checkpoint_index(&read_source_json(root, &index_relative)?)?,
            false,
        ));
    }
    let model_relative = safe_relative(Some(MONOLITHIC_WEIGHT_FILE), "weight")?;
    let Some(model_path) = resolve_source_path(root, &model_relative, false)? else {
        return invalid("missing_safetensors_checkpoint");
    };
    stat_regular(&model_path)?;
    let names = read_safetensors_header(&model_path)?;
    Ok((
        names
            .into_iter()
            .map(|n| (n, MONOLITHIC_WEIGHT_FILE.to_owned()))
            .collect(),
        true,
    ))
}

// ----- model_adapters.py -----
struct Adapter {
    architecture: &'static str,
    layer_count_fields: &'static [&'static str],
    block_prefix_template: &'static str,
    components: &'static [(&'static str, &'static [&'static str])],
    tied_lm_head_source: &'static [&'static str],
    alternate_block_prefix_templates: &'static [&'static str],
    supported_architectures: &'static [&'static str],
    decoder_tensor_suffixes: &'static [&'static str],
    layer_type_tensor_suffixes: &'static [(&'static str, &'static [&'static str])],
    excluded_tensor_prefixes: &'static [&'static str],
    causal_lm_architectures: &'static [&'static str],
}
const GPT2_SUFFIXES: &[&str] = &[
    "ln_1.weight",
    "ln_1.bias",
    "attn.c_attn.weight",
    "attn.c_attn.bias",
    "attn.c_proj.weight",
    "attn.c_proj.bias",
    "ln_2.weight",
    "ln_2.bias",
    "mlp.c_fc.weight",
    "mlp.c_fc.bias",
    "mlp.c_proj.weight",
    "mlp.c_proj.bias",
];
const QWEN2_SUFFIXES: &[&str] = &[
    "input_layernorm.weight",
    "self_attn.q_proj.weight",
    "self_attn.q_proj.bias",
    "self_attn.k_proj.weight",
    "self_attn.k_proj.bias",
    "self_attn.v_proj.weight",
    "self_attn.v_proj.bias",
    "self_attn.o_proj.weight",
    "post_attention_layernorm.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
];
const QWEN3_SUFFIXES: &[&str] = &[
    "input_layernorm.weight",
    "self_attn.q_proj.weight",
    "self_attn.q_norm.weight",
    "self_attn.k_proj.weight",
    "self_attn.k_norm.weight",
    "self_attn.v_proj.weight",
    "self_attn.o_proj.weight",
    "post_attention_layernorm.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
];
const QWEN35_SHARED: &[&str] = &[
    "input_layernorm.weight",
    "post_attention_layernorm.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
];
const QWEN35_LINEAR: &[&str] = &[
    "linear_attn.in_proj_qkv.weight",
    "linear_attn.in_proj_z.weight",
    "linear_attn.in_proj_b.weight",
    "linear_attn.in_proj_a.weight",
    "linear_attn.conv1d.weight",
    "linear_attn.dt_bias",
    "linear_attn.A_log",
    "linear_attn.norm.weight",
    "linear_attn.out_proj.weight",
];
const QWEN35_FULL: &[&str] = &[
    "self_attn.q_proj.weight",
    "self_attn.q_norm.weight",
    "self_attn.k_proj.weight",
    "self_attn.k_norm.weight",
    "self_attn.v_proj.weight",
    "self_attn.o_proj.weight",
];
const QWEN35_MOE_SHARED: &[&str] = &[
    "input_layernorm.weight",
    "post_attention_layernorm.weight",
    "mlp.gate.weight",
    "mlp.experts.gate_up_proj",
    "mlp.experts.down_proj",
    "mlp.shared_expert.gate_proj.weight",
    "mlp.shared_expert.up_proj.weight",
    "mlp.shared_expert.down_proj.weight",
    "mlp.shared_expert_gate.weight",
];
const MODEL_LAYERS_COMPONENTS: &[(&str, &[&str])] = &[
    ("input_embedding", &["model.embed_tokens."]),
    ("decoder", &["model.layers.{layer}."]),
    ("final_norm", &["model.norm."]),
    ("lm_head", &["lm_head."]),
];
const QWEN35_COMPONENTS: &[(&str, &[&str])] = &[
    ("input_embedding", &["model.language_model.embed_tokens."]),
    ("decoder", &["model.language_model.layers.{layer}."]),
    ("final_norm", &["model.language_model.norm."]),
    ("lm_head", &["lm_head."]),
];
const QWEN35_LAYER_TYPES: &[(&str, &[&str])] = &[
    ("linear_attention", QWEN35_LINEAR),
    ("full_attention", QWEN35_FULL),
];
const NO_FIELDS: &[&str] = &[];
const fn plain(architecture: &'static str) -> Adapter {
    Adapter {
        architecture,
        layer_count_fields: &["num_hidden_layers", "n_layer"],
        block_prefix_template: "model.layers.{layer}.",
        components: MODEL_LAYERS_COMPONENTS,
        tied_lm_head_source: &["model.embed_tokens."],
        alternate_block_prefix_templates: NO_FIELDS,
        supported_architectures: NO_FIELDS,
        decoder_tensor_suffixes: NO_FIELDS,
        layer_type_tensor_suffixes: &[],
        excluded_tensor_prefixes: NO_FIELDS,
        causal_lm_architectures: NO_FIELDS,
    }
}
const ADAPTERS: &[Adapter] = &[
    Adapter {
        architecture: "gpt2",
        layer_count_fields: &["n_layer", "num_hidden_layers"],
        block_prefix_template: "transformer.h.{layer}.",
        components: &[
            (
                "input_embedding",
                &["transformer.wte.", "transformer.wpe.", "wte.", "wpe."],
            ),
            ("decoder", &["transformer.h.{layer}.", "h.{layer}."]),
            ("final_norm", &["transformer.ln_f.", "ln_f."]),
            ("lm_head", &["lm_head."]),
        ],
        tied_lm_head_source: &["transformer.wte.", "wte."],
        alternate_block_prefix_templates: &["h.{layer}."],
        decoder_tensor_suffixes: GPT2_SUFFIXES,
        ..plain("gpt2")
    },
    Adapter {
        architecture: "bloom",
        layer_count_fields: &["n_layer", "num_hidden_layers"],
        block_prefix_template: "transformer.h.{layer}.",
        components: &[
            (
                "input_embedding",
                &[
                    "transformer.word_embeddings.",
                    "transformer.word_embeddings_layernorm.",
                    "word_embeddings.",
                    "word_embeddings_layernorm.",
                ],
            ),
            ("decoder", &["transformer.h.{layer}.", "h.{layer}."]),
            ("final_norm", &["transformer.ln_f.", "ln_f."]),
            ("lm_head", &["lm_head."]),
        ],
        tied_lm_head_source: &["transformer.word_embeddings.", "word_embeddings."],
        alternate_block_prefix_templates: &["h.{layer}."],
        ..plain("bloom")
    },
    Adapter {
        architecture: "falcon",
        layer_count_fields: &["num_hidden_layers", "n_layer"],
        block_prefix_template: "transformer.h.{layer}.",
        components: &[
            ("input_embedding", &["transformer.word_embeddings."]),
            ("decoder", &["transformer.h.{layer}."]),
            ("final_norm", &["transformer.ln_f."]),
            ("lm_head", &["lm_head."]),
        ],
        tied_lm_head_source: &["transformer.word_embeddings."],
        ..plain("falcon")
    },
    Adapter {
        architecture: "gemma3",
        layer_count_fields: &["text_config.num_hidden_layers", "num_hidden_layers"],
        block_prefix_template: "model.language_model.layers.{layer}.",
        components: QWEN35_COMPONENTS,
        tied_lm_head_source: &["model.language_model.embed_tokens."],
        ..plain("gemma3")
    },
    Adapter {
        architecture: "bert",
        layer_count_fields: &["num_hidden_layers"],
        block_prefix_template: "bert.encoder.layer.{layer}.",
        components: &[
            ("input_embedding", &["bert.embeddings.", "embeddings."]),
            (
                "decoder",
                &["bert.encoder.layer.{layer}.", "encoder.layer.{layer}."],
            ),
            ("final_norm", &[]),
            ("pooler", &["bert.pooler.", "pooler."]),
            ("classifier", &["classifier."]),
            ("qa_head", &["qa_outputs."]),
        ],
        tied_lm_head_source: NO_FIELDS,
        alternate_block_prefix_templates: &["encoder.layer.{layer}."],
        supported_architectures: &[
            "BertModel",
            "BertForSequenceClassification",
            "BertForTokenClassification",
            "BertForQuestionAnswering",
            "BertForMultipleChoice",
        ],
        ..plain("bert")
    },
    Adapter {
        supported_architectures: &["Qwen2ForCausalLM"],
        decoder_tensor_suffixes: QWEN2_SUFFIXES,
        ..plain("qwen2")
    },
    Adapter {
        supported_architectures: &["Qwen3ForCausalLM"],
        decoder_tensor_suffixes: QWEN3_SUFFIXES,
        ..plain("qwen3")
    },
    Adapter {
        architecture: "qwen3_5",
        layer_count_fields: &["text_config.num_hidden_layers", "num_hidden_layers"],
        block_prefix_template: "model.language_model.layers.{layer}.",
        components: QWEN35_COMPONENTS,
        tied_lm_head_source: &["model.language_model.embed_tokens."],
        supported_architectures: &["Qwen3_5ForConditionalGeneration"],
        decoder_tensor_suffixes: QWEN35_SHARED,
        layer_type_tensor_suffixes: QWEN35_LAYER_TYPES,
        excluded_tensor_prefixes: &["model.visual.", "mtp."],
        causal_lm_architectures: &["Qwen3_5ForConditionalGeneration"],
        ..plain("qwen3_5")
    },
    Adapter {
        architecture: "qwen3_5_moe",
        layer_count_fields: &["text_config.num_hidden_layers", "num_hidden_layers"],
        block_prefix_template: "model.language_model.layers.{layer}.",
        components: QWEN35_COMPONENTS,
        tied_lm_head_source: &["model.language_model.embed_tokens."],
        supported_architectures: &["Qwen3_5MoeForConditionalGeneration"],
        decoder_tensor_suffixes: QWEN35_MOE_SHARED,
        layer_type_tensor_suffixes: QWEN35_LAYER_TYPES,
        excluded_tensor_prefixes: &["model.visual.", "mtp."],
        causal_lm_architectures: &["Qwen3_5MoeForConditionalGeneration"],
        ..plain("qwen3_5_moe")
    },
    plain("llama"),
    plain("mistral"),
    plain("mixtral"),
    plain("gemma"),
    plain("gemma2"),
    plain("gemma3_text"),
];
fn format_layer(template: &str, layer: usize) -> String {
    template.replace("{layer}", &layer.to_string())
}
fn compile_failed<T>() -> R<T> {
    invalid("manifest_compile_failed")
}
fn layer_count(adapter: &Adapter, config: &Map<String, Value>) -> Option<usize> {
    for field in adapter.layer_count_fields {
        let mut value: Option<&Value> = None;
        let mut map = Some(config);
        for part in field.split('.') {
            let Some(m) = map else {
                value = None;
                break;
            };
            value = m.get(part);
            map = value.and_then(Value::as_object);
        }
        if let Some(n) = value.and_then(integer).filter(|n| *n > 0)
            && let Ok(n) = usize::try_from(n)
        {
            return Some(n);
        }
    }
    None
}
fn is_causal_lm(config: &Map<String, Value>, extra: &[&str]) -> bool {
    config
        .get("architectures")
        .and_then(Value::as_array)
        .is_some_and(|names| {
            names.iter().any(|n| {
                n.as_str().is_some_and(|n| {
                    n.ends_with("ForCausalLM") || n == "GPT2LMHeadModel" || extra.contains(&n)
                })
            })
        })
}
fn sorted_strings(set: impl IntoIterator<Item = String>) -> Value {
    let sorted: BTreeSet<String> = set.into_iter().collect();
    Value::Array(sorted.into_iter().map(Value::String).collect())
}
fn canonical_digest(document: &Map<String, Value>) -> R<Value> {
    let bytes = protocol::canonical::to_bytes(&Value::Object(document.clone()))
        .or_else(|_| compile_failed())?;
    let hex: String = Sha256::digest(&bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Ok(json!({"algorithm": "sha256", "value": hex}))
}
struct FileMeta {
    size_bytes: u64,
    sha256: String,
}
/// `model_manifest.compile_model_manifest` (the arguments a local source can supply).
fn compile_model_manifest(
    model_id: &str,
    requested_revision: &str,
    resolved_commit: &str,
    config: &Map<String, Value>,
    weight_map: &WeightMap,
    file_metadata: &BTreeMap<String, FileMeta>,
    index_file: &str,
) -> R<Value> {
    if !model_id.contains('/') || requested_revision.is_empty() || !is_commit(resolved_commit) {
        return compile_failed();
    }
    if weight_map.is_empty() {
        return compile_failed();
    }
    let adapter = config
        .get("model_type")
        .and_then(Value::as_str)
        .and_then(|t| ADAPTERS.iter().find(|a| a.architecture == t));
    let Some(adapter) = adapter else {
        return compile_failed();
    };
    // adapter.validate_architectures
    if !adapter.supported_architectures.is_empty()
        && let Some(architectures) = config.get("architectures")
    {
        let Some(names) = architectures.as_array().and_then(|a| {
            a.iter()
                .map(|n| n.as_str().filter(|s| !s.is_empty()))
                .collect::<Option<Vec<&str>>>()
        }) else {
            return compile_failed();
        };
        if names
            .iter()
            .any(|n| !adapter.supported_architectures.contains(n))
        {
            return compile_failed();
        }
    }
    let Some(num_layers) = layer_count(adapter, config) else {
        return compile_failed();
    };
    let templates: Vec<&str> = std::iter::once(adapter.block_prefix_template)
        .chain(adapter.alternate_block_prefix_templates.iter().copied())
        .collect();
    let covers = |template: &str, layer: usize| {
        let prefix = format_layer(template, layer);
        weight_map.keys().any(|k| k.starts_with(&prefix))
    };
    let Some(block_prefix_template) = templates
        .iter()
        .find(|t| (0..num_layers).all(|layer| covers(t, layer)))
        .copied()
    else {
        return compile_failed();
    };
    let mut tensor_keys_by_layer = Map::new();
    let mut layer_files = Map::new();
    let mut recognized: BTreeSet<&String> = BTreeSet::new();
    let mut keys_of_layer: Vec<Vec<String>> = Vec::new();
    for layer in 0..num_layers {
        let prefix = format_layer(block_prefix_template, layer);
        let keys: Vec<&String> = weight_map
            .keys()
            .filter(|k| k.starts_with(&prefix))
            .collect();
        recognized.extend(keys.iter().copied());
        keys_of_layer.push(keys.iter().map(|k| (*k).clone()).collect());
        tensor_keys_by_layer.insert(
            layer.to_string(),
            Value::Array(keys.iter().map(|k| Value::String((*k).clone())).collect()),
        );
        layer_files.insert(
            layer.to_string(),
            sorted_strings(keys.iter().map(|k| weight_map[*k].clone())),
        );
    }
    let referenced: BTreeSet<&String> = weight_map.values().collect();
    let mut files = Vec::new();
    for path in &referenced {
        let Some(meta) = file_metadata.get(*path) else {
            return compile_failed();
        };
        if meta.size_bytes == 0 || !is_sha256_hex(&meta.sha256) {
            return compile_failed();
        }
        files.push(json!({"path": path, "size_bytes": meta.size_bytes,
            "content_digest": {"algorithm": "sha256", "value": meta.sha256}}));
    }
    let mut components = Map::new();
    let mut component_tensor_keys = Map::new();
    let mut static_keys: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (name, prefixes) in adapter.components {
        components.insert(
            (*name).to_owned(),
            Value::Array(prefixes.iter().map(|p| json!(p)).collect()),
        );
        if *name == "decoder" {
            continue;
        }
        let keys: Vec<String> = weight_map
            .keys()
            .filter(|k| {
                prefixes
                    .iter()
                    .any(|p| !p.contains("{layer}") && k.starts_with(p))
            })
            .cloned()
            .collect();
        static_keys.insert((*name).to_owned(), keys);
    }
    for keys in static_keys.values() {
        recognized.extend(weight_map.keys().filter(|k| keys.contains(k)));
    }
    let excluded: Vec<String> = weight_map
        .keys()
        .filter(|k| {
            !recognized.contains(*k)
                && adapter
                    .excluded_tensor_prefixes
                    .iter()
                    .any(|p| k.starts_with(p))
        })
        .cloned()
        .collect();
    let unowned = weight_map
        .keys()
        .any(|k| !recognized.contains(k) && !excluded.contains(k));
    if unowned {
        return compile_failed();
    }
    let mut component_aliases = Map::new();
    let causal = is_causal_lm(config, adapter.causal_lm_architectures);
    if causal {
        for required in ["input_embedding", "final_norm"] {
            if static_keys.get(required).is_none_or(Vec::is_empty) {
                return compile_failed();
            }
        }
        if config.get("tie_word_embeddings") == Some(&Value::Bool(true)) {
            let tied: Vec<String> = weight_map
                .keys()
                .filter(|k| adapter.tied_lm_head_source.iter().any(|p| k.starts_with(p)))
                .cloned()
                .collect();
            if tied.is_empty() {
                return compile_failed();
            }
            static_keys.insert("lm_head".into(), tied);
            component_aliases.insert("lm_head".into(), json!("input_embedding"));
        } else if static_keys.get("lm_head").is_none_or(Vec::is_empty) {
            return compile_failed();
        }
    }
    let mut component_files = Map::new();
    for (name, keys) in &static_keys {
        component_tensor_keys.insert(name.clone(), sorted_strings(keys.iter().cloned()));
        component_files.insert(
            name.clone(),
            sorted_strings(keys.iter().map(|k| weight_map[k].clone())),
        );
    }
    let expected = num_layers as i128;
    let config_value = Value::Object(config.clone());
    let normalize = |f: fn(&Value, i128) -> planner::py::PyResult<Value>| {
        f(&config_value, expected).or_else(|_| compile_failed())
    };
    let runtime_model = match adapter.architecture {
        "gpt2"
            if causal
                || ["n_embd", "n_head", "vocab_size", "n_positions"]
                    .iter()
                    .any(|f| config.contains_key(*f)) =>
        {
            Some(("gpt2", normalize(rt::normalize_gpt2_model_config)?))
        }
        "qwen2" if causal => Some(("qwen2", normalize(rt::normalize_qwen2_model_config)?)),
        "qwen3" if causal => Some(("qwen3", normalize(rt::normalize_qwen3_model_config)?)),
        "qwen3_5" if causal => Some(("qwen3_5", normalize(rt::normalize_qwen35_model_config)?)),
        "qwen3_5_moe" if causal => Some((
            "qwen3_5_moe",
            normalize(rt::normalize_qwen35_moe_model_config)?,
        )),
        _ => None,
    };
    if !adapter.layer_type_tensor_suffixes.is_empty() {
        validate_layer_typed_decoder(adapter, config, &keys_of_layer, block_prefix_template)?;
    }
    let mut manifest = Map::new();
    manifest.insert("protocol".into(), json!("mycelium.model_manifest.v1"));
    manifest.insert("model_id".into(), json!(model_id));
    manifest.insert("source".into(), json!("huggingface"));
    manifest.insert("requested_revision".into(), json!(requested_revision));
    manifest.insert("resolved_commit".into(), json!(resolved_commit));
    manifest.insert("format".into(), json!("safetensors_sharded"));
    manifest.insert("index_file".into(), json!(index_file));
    manifest.insert("architecture".into(), json!(adapter.architecture));
    manifest.insert("num_layers".into(), json!(num_layers));
    manifest.insert("block_prefix_template".into(), json!(block_prefix_template));
    manifest.insert("components".into(), Value::Object(components));
    manifest.insert(
        "component_tensor_keys".into(),
        Value::Object(component_tensor_keys),
    );
    manifest.insert("component_aliases".into(), Value::Object(component_aliases));
    manifest.insert("component_files".into(), Value::Object(component_files));
    manifest.insert("files".into(), Value::Array(files));
    manifest.insert("layer_files".into(), Value::Object(layer_files));
    manifest.insert(
        "tensor_keys_by_layer".into(),
        Value::Object(tensor_keys_by_layer),
    );
    if !adapter.excluded_tensor_prefixes.is_empty() {
        manifest.insert("excluded_tensor_keys".into(), sorted_strings(excluded));
    }
    if let Some((architecture, model_config)) = runtime_model {
        manifest.insert(
            "runtime_model".into(),
            json!({"architecture": architecture, "model_config": model_config}),
        );
    }
    let digest = canonical_digest(&manifest)?;
    manifest.insert("manifest_digest".into(), digest);
    Ok(Value::Object(manifest))
}
/// `_validate_layer_typed_decoder`.
fn validate_layer_typed_decoder(
    adapter: &Adapter,
    config: &Map<String, Value>,
    keys_by_layer: &[Vec<String>],
    template: &str,
) -> R<()> {
    let text = config
        .get("text_config")
        .and_then(Value::as_object)
        .unwrap_or(config);
    let Some(layer_types) = text
        .get("layer_types")
        .and_then(Value::as_array)
        .filter(|t| t.len() == keys_by_layer.len())
    else {
        return compile_failed();
    };
    for (layer, kind) in layer_types.iter().enumerate() {
        let Some(extra) = kind.as_str().and_then(|k| {
            adapter
                .layer_type_tensor_suffixes
                .iter()
                .find(|(name, _)| *name == k)
                .map(|(_, s)| *s)
        }) else {
            return compile_failed();
        };
        let prefix = format_layer(template, layer);
        let mut expected: Vec<String> = adapter
            .decoder_tensor_suffixes
            .iter()
            .chain(extra.iter())
            .map(|s| format!("{prefix}{s}"))
            .collect();
        expected.sort();
        if keys_by_layer[layer] != expected {
            return compile_failed();
        }
    }
    Ok(())
}
fn non_runtime_gpt2_tensor(name: &str) -> bool {
    // ^(?:transformer\.)?h\.\d+\.attn\.(?:bias|masked_bias)$
    let rest = name.strip_prefix("transformer.").unwrap_or(name);
    let Some(rest) = rest.strip_prefix("h.") else {
        return false;
    };
    let digits = rest.bytes().take_while(u8::is_ascii_digit).count();
    digits > 0 && matches!(&rest[digits..], ".attn.bias" | ".attn.masked_bias")
}

/// `compile_local_model_manifest(model_source)`.
pub fn compile_local_model_manifest(source: &LocalModelSource) -> R<Value> {
    let root = validate_source(source)?;
    let config_relative = safe_relative(Some("config.json"), "config")?;
    let Value::Object(config) = read_source_json(&root, &config_relative)? else {
        return invalid("config_not_object");
    };
    let (source_weight_map, _synthesized) = load_checkpoint_index(&root)?;
    let referenced: BTreeSet<&String> = source_weight_map.values().collect();
    if referenced.is_empty() {
        return invalid("checkpoint_index_empty");
    }
    let mut file_metadata: BTreeMap<String, FileMeta> = BTreeMap::new();
    for filename in referenced {
        let relative = safe_relative(Some(filename), "weight")?;
        let Some(path) = resolve_source_path(&root, &relative, true)? else {
            return invalid("source_path_missing");
        };
        let metadata = stat_regular(&path)?;
        let header_names = read_safetensors_header(&path)?;
        let mapped: Vec<&String> = source_weight_map
            .iter()
            .filter(|(_, f)| *f == filename)
            .map(|(n, _)| n)
            .collect();
        if mapped.len() != header_names.len()
            || mapped.iter().zip(&header_names).any(|(a, b)| *a != b)
        {
            return invalid("checkpoint_index_tensor_mismatch");
        }
        file_metadata.insert(
            filename.clone(),
            FileMeta {
                size_bytes: metadata.size(),
                sha256: sha256_file(&path)?,
            },
        );
    }
    let runtime_weight_map: WeightMap = source_weight_map
        .iter()
        .filter(|(name, _)| !non_runtime_gpt2_tensor(name))
        .map(|(n, f)| (n.clone(), f.clone()))
        .collect();
    if runtime_weight_map.is_empty() {
        return invalid("checkpoint_index_empty");
    }
    compile_model_manifest(
        &source.model_id,
        &source.requested_revision,
        &source.resolved_commit,
        &config,
        &runtime_weight_map,
        &file_metadata,
        MODEL_INDEX_FILE,
    )
}
