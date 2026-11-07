// SPDX-License-Identifier: AGPL-3.0-only
//! `serving.prepare_local_source` (f4e366e9, serving.py:554-615): hash-free admission of one pinned local
//! Hugging Face snapshot before the native manifest compiler runs. Everything before and after the compile
//! (snapshot identity, `config.json`, architecture and dtype, weight bytes, manifest identity) is ported exactly
//! and checked against the executed oracle (`tests/fixtures/oracle_prepare.json`, 42 cases); the compile itself
//! is the injected [`Compiler`] (`physical_deployment.compile_local_model_manifest`, SE-L10-MANIFEST-COMPILE).
use crate::model_catalog::Model;
use budgets::BudgetId;
use serde_json::{Map, Value};
use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};

/// `LocalModelSource(root, model_id, requested_revision='main', resolved_commit)`.
#[derive(Clone, Debug)]
pub struct LocalSource {
    pub root: PathBuf,
    pub model_id: &'static str,
    pub requested_revision: &'static str,
    pub resolved_commit: &'static str,
}
/// Why the compile (or the whole preparation) failed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Failure {
    /// `ServingPreparationError(code)`.
    Serving(String),
    /// Any other exception class (`AttributeError`, ...): the caller records `acquisition_failed`.
    Other,
}
fn serving<T>(code: &str) -> Result<T, Failure> {
    Err(Failure::Serving(code.to_owned()))
}
/// The native manifest compiler: a manifest mapping, or the reason it failed.
pub type Compiler<'a> = &'a dyn Fn(&LocalSource) -> Result<Value, Failure>;
/// `PreparedSource` (the parts a caller uses).
#[derive(Clone, Debug)]
pub struct Prepared {
    pub source_root: PathBuf,
    pub source_bytes: u64,
    pub manifest: Map<String, Value>,
    /// `sha256:<64 hex>`.
    pub manifest_digest: String,
}
/// `ModelContract.architecture` (`_MODEL_ARCHITECTURES`, default `qwen2`).
pub fn architecture(model: &Model) -> &'static str {
    if model.model_id.starts_with("Qwen/Qwen3-") {
        "qwen3"
    } else {
        "qwen2"
    }
}
fn max_json_bytes() -> u64 {
    BudgetId::PyMyceliumA13MacosServingMaxJsonBytesL360
        .bytes()
        .map_or(0, |b| b.get())
}
/// `_read_json_object`: a regular, bounded, UTF-8 JSON object, else `reason`.
fn read_json_object(path: &Path, reason: &str) -> Result<Map<String, Value>, Failure> {
    let Ok(meta) = fs::symlink_metadata(path) else {
        return serving(reason);
    };
    if meta.file_type().is_symlink() || !meta.is_file() || meta.len() > max_json_bytes() {
        return serving(reason);
    }
    let Ok(text) = fs::read_to_string(path) else {
        return serving(reason);
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(map)) => Ok(map),
        _ => serving(reason),
    }
}
/// `_weight_bytes`: the sizes of the distinct `*.safetensors` files directly in the snapshot.
fn weight_bytes(root: &Path) -> Result<u64, Failure> {
    let mut weights: Vec<PathBuf> = Vec::new();
    if let Ok(entries) = fs::read_dir(root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path
                .file_name()
                .is_some_and(|n| n.to_string_lossy().ends_with(".safetensors"))
            {
                weights.push(path);
            }
        }
    }
    if weights.is_empty() {
        return serving("source_safetensors_missing");
    }
    let mut seen: BTreeSet<(u64, u64)> = BTreeSet::new();
    let mut total = 0u64;
    for path in weights {
        let Ok(resolved) = fs::canonicalize(&path) else {
            return serving("source_safetensors_invalid");
        };
        let Ok(meta) = fs::metadata(&resolved) else {
            return serving("source_safetensors_invalid");
        };
        if !meta.is_file() {
            return serving("source_safetensors_invalid");
        }
        if seen.insert((meta.dev(), meta.ino())) {
            total += meta.len();
        }
    }
    Ok(total)
}
/// `prepare_local_source(source_root, model=..., compiler=...)`.
pub fn prepare_local_source(
    source_root: &Path,
    model: &Model,
    compiler: Compiler,
) -> Result<Prepared, Failure> {
    if fs::symlink_metadata(source_root).is_ok_and(|m| m.file_type().is_symlink()) {
        return serving("source_root_symlink");
    }
    let Ok(root) = fs::canonicalize(source_root) else {
        return serving("source_root_missing");
    };
    if !root.is_dir() {
        return serving("source_root_not_directory");
    }
    // `_snapshot_identity`
    let name = |p: Option<&Path>| {
        p.and_then(Path::file_name)
            .map(|n| n.to_string_lossy().into_owned())
    };
    let parent = root.parent();
    if name(Some(&root)).as_deref() != Some(model.resolved_commit)
        || name(parent).as_deref() != Some("snapshots")
        || name(parent.and_then(Path::parent)).as_deref()
            != Some(format!("models--{}", model.model_id.replace('/', "--")).as_str())
    {
        return serving("source_revision_mismatch");
    }
    let config = read_json_object(&root.join("config.json"), "source_config_invalid")?;
    if config.get("model_type").and_then(Value::as_str) != Some(architecture(model))
        || !matches!(
            config.get("torch_dtype").and_then(Value::as_str),
            Some("bfloat16" | "bf16")
        )
    {
        return serving("source_architecture_mismatch");
    }
    let source_bytes = weight_bytes(&root)?;
    let source = LocalSource {
        root: root.clone(),
        model_id: model.model_id,
        requested_revision: "main",
        resolved_commit: model.resolved_commit,
    };
    // `dict((compiler or _native_compiler)(source))`
    let manifest: Map<String, Value> = match compiler(&source) {
        Err(Failure::Serving(code)) => return Err(Failure::Serving(code)),
        Err(Failure::Other) => return serving("native_manifest_compile_failed"),
        Ok(Value::Object(map)) => map,
        Ok(Value::Array(pairs)) => {
            let mut map = Map::new();
            for pair in pairs {
                match pair.as_array().map(Vec::as_slice) {
                    Some([Value::String(key), value]) => {
                        map.insert(key.clone(), value.clone());
                    }
                    _ => return serving("native_manifest_compile_failed"),
                }
            }
            map
        }
        Ok(_) => return serving("native_manifest_compile_failed"),
    };
    let digest = manifest.get("manifest_digest").and_then(Value::as_object);
    let manifest_digest = match digest {
        Some(d) if d.get("algorithm").and_then(Value::as_str) == Some("sha256") => format!(
            "sha256:{}",
            match d.get("value") {
                None => "None".to_owned(),
                Some(Value::String(s)) => s.clone(),
                Some(other) => other.to_string(),
            }
        ),
        _ => String::new(),
    };
    let identity_mismatch = manifest.get("model_id").and_then(Value::as_str)
        != Some(model.model_id)
        || manifest.get("resolved_commit").and_then(Value::as_str) != Some(model.resolved_commit)
        || match manifest.get("runtime_model") {
            // `manifest.get('runtime_model', {}).get('architecture')`: a present non-mapping raises.
            Some(Value::Object(m)) => {
                m.get("architecture").and_then(Value::as_str) != Some(architecture(model))
            }
            None => true,
            Some(_) => return Err(Failure::Other),
        }
        || manifest_digest.chars().count() != 71;
    if identity_mismatch {
        return serving("native_manifest_identity_mismatch");
    }
    Ok(Prepared {
        source_root: root,
        source_bytes,
        manifest,
        manifest_digest,
    })
}
