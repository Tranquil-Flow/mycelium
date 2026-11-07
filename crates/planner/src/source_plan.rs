// SPDX-License-Identifier: AGPL-3.0-only
//! Source-bound slice documents, not an operator plan, launch, or native build.
//!
//! The caller resolves paths under its admitted prepared-source/emitted-shard roots
//! and retains regular file handles. This module never opens a path or follows a link.
//! `current_request` must come from a fresh `chain_compile::prepare` with the host's
//! current verified membership/facts/public keys; a copied old request is not a
//! currentness check. Publication must recheck that binding after this worker ends.
//!
//! Inputs must be held immutable for the worker lifetime by the admitting caller.
//! Rehashing detects drift but is not a filesystem snapshot or a write lock. The
//! caller owns root/HF-link confinement before opening these handles. The low-level
//! tiny fixture path does not bypass `prepare`'s registered-model/slice admission.
//! `sourceDocsNativeMissing`: these recipes prove neither native load expectations,
//! challenge tokens, signatures, operator-plan completion nor route readiness.
//! Complete document shaping is `slice_acquisition.py:239-308 @ f4e366e9`;
//! manifest/current-request/assignment checks bind its already-verified inputs.
use crate::{Error, Result, chain_compile::BuildRequest};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
};
mod bytes;
pub mod producer;

/// Already-open files from the prepared source. Keys are manifest-relative paths.
/// Handles, not names, are read, and content/size are revalidated here.
pub struct PreparedSource<'a> {
    pub manifest: &'a Value,
    pub manifest_digest: &'a str,
    pub files: &'a BTreeMap<String, File>,
    pub checkpoint: &'a dyn Fn() -> Result<()>,
}
/// Actual emitted shards and independently pinned operator node transfer rows.
/// `node_files` has the shape returned by v0.1.4 `node_weight_files`.
pub struct EmittedShards<'a> {
    pub files: &'a BTreeMap<String, File>,
    pub node_files: &'a BTreeMap<String, Vec<Value>>,
    pub node_files_digest: &'a str,
    /// v0.1.4 compile_slice_plan option, at most its registered 16 MiB maximum.
    pub max_segment_bytes: u64,
}
#[derive(Debug)]
pub struct SliceDocuments {
    pub plan: Value,
    pub node_plans: BTreeMap<String, Value>,
}

/// Build complete canonical recipes from actual bytes, without allocating tensors.
/// Does not create/sign/reconstruct an operator plan or a `BuildOutput`.
pub fn compile_slices(
    request: &BuildRequest,
    current_request: &BuildRequest,
    source: PreparedSource<'_>,
    shards: EmittedShards<'_>,
) -> Result<SliceDocuments> {
    validate_binding(request, current_request)?;
    let limit = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
        .bytes()
        .map_err(|_| Error::new("slice_plan_request_invalid"))?
        .get();
    if shards.max_segment_bytes == 0
        || shards.max_segment_bytes > limit
        || !request.stage_sharded
        || !request.model_id.contains('/')
        || !lower_hex(&request.resolved_commit, 40)
    {
        return Err(Error::new("slice_plan_request_invalid"));
    }
    let manifest = source.manifest;
    if !crate::bound::verify_manifest_digest(manifest).unwrap_or(false)
        || crate::bound::manifest_digest_ref(manifest).ok().as_deref()
            != Some(source.manifest_digest)
        || manifest["model_id"] != request.model_id
        || manifest["resolved_commit"] != request.resolved_commit
    {
        return Err(Error::new("slice_source_manifest_invalid"));
    }
    let assignment_bad = || Error::new("source_plan_assignment_mismatch");
    let ordered: Vec<_> = shards.node_files.keys().cloned().collect();
    if request.node_ids.is_empty()
        || request.node_ids != ordered
        || request.managed_layer_ranges.len() != ordered.len()
    {
        return Err(assignment_bad());
    }
    let stages = request.layer_split["stages"]
        .as_array()
        .ok_or_else(assignment_bad)?;
    if stages.len() != ordered.len() {
        return Err(assignment_bad());
    }
    let layers = manifest["num_layers"].as_u64().ok_or_else(assignment_bad)?;
    let mut cursor = 0;
    for (i, node) in ordered.iter().enumerate() {
        let [start, end] = request.managed_layer_ranges[i];
        if !valid_node(node)
            || start as u64 != cursor
            || end <= start
            || end as u64 > layers
            || stages[i]["node_id"] != *node
            || stages[i]["start"] != json!(start)
            || stages[i]["end"] != json!(end)
        {
            return Err(assignment_bad());
        }
        cursor = end as u64;
    }
    if cursor != layers {
        return Err(assignment_bad());
    }
    if crate::digest(&json!(shards.node_files), true)? != shards.node_files_digest {
        return Err(Error::new("slice_plan_file_digest_mismatch"));
    }
    let (sources, tensors) = source_index(&source)?;
    let mut nodes = Map::new();
    let mut used = BTreeSet::new();
    for (i, node) in ordered.iter().enumerate() {
        let mut rows = shards.node_files[node].clone();
        // slice_acquisition.py:65,66,103,106: original structural bounds;
        // no new policy ceiling or alias to another source-family budget.
        if rows.is_empty() || rows.len() > MAX_FILES_PER_NODE {
            return Err(Error::new("slice_plan_request_invalid"));
        }
        rows.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
        let mut files = Vec::new();
        let mut inline = 0;
        let mut owned = BTreeSet::new();
        let mut paths = BTreeSet::new();
        for row in rows {
            let path = relative(&row["path"])?;
            let parts: Vec<_> = path.split('/').collect();
            if parts.len() != 2 || parts[0] != "deployment" || !path.ends_with(".safetensors") {
                return Err(Error::new("slice_plan_request_invalid"));
            }
            if !paths.insert(path.to_owned()) {
                return Err(assignment_bad());
            }
            let bad = || Error::new("slice_plan_file_digest_mismatch");
            let file = shards.files.get(parts[1]).ok_or_else(bad)?;
            let size = row["size_bytes"].as_u64().ok_or_else(bad)?;
            let digest = row["content_digest"].as_str().ok_or_else(bad)?;
            bytes::verify(
                file,
                size,
                digest,
                "slice_plan_file_digest_mismatch",
                source.checkpoint,
            )?;
            let header = bytes::header(file, source.checkpoint)?;
            for tensor in &header.tensors {
                if !owned.insert(tensor.name.clone()) {
                    return Err(assignment_bad());
                }
            }
            let segments = file_segments(
                file,
                header,
                source.files,
                &tensors,
                shards.max_segment_bytes,
                source.checkpoint,
            )?;
            for s in &segments {
                if let Some(path) = s["source_path"].as_str() {
                    used.insert(path.to_owned());
                } else {
                    inline = crate::add(inline, s["length"].as_u64().ok_or_else(bad)?)?;
                }
            }
            // Rehash after range reads: never silently publish changed source bytes.
            bytes::verify(
                file,
                size,
                digest,
                "slice_plan_file_digest_mismatch",
                source.checkpoint,
            )?;
            files.push(
                json!({"path":path,"size_bytes":size,"content_digest":digest,"segments":segments}),
            );
        }
        if owned
            != expected_tensors(
                manifest,
                request.managed_layer_ranges[i],
                i == 0,
                i + 1 == ordered.len(),
            )?
        {
            return Err(assignment_bad());
        }
        let maximum = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxInlineBytesPerNodeL64
            .bytes()
            .map_err(|_| Error::new("slice_inline_limit_exceeded"))?
            .get();
        if inline > maximum {
            return Err(Error::new("slice_inline_limit_exceeded"));
        }
        nodes.insert(node.clone(), json!({"files":files}));
    }
    for (path, row) in &sources {
        bytes::verify(
            &source.files[path],
            row["size_bytes"]
                .as_u64()
                .ok_or_else(|| Error::new("slice_source_manifest_invalid"))?,
            row["content_digest"]
                .as_str()
                .ok_or_else(|| Error::new("slice_source_manifest_invalid"))?,
            "slice_source_manifest_invalid",
            source.checkpoint,
        )?;
    }
    let source_files: Vec<_> = used.iter().map(|p| sources[p].clone()).collect();
    let mut plan = json!({"protocol":"mycelium.a13_slice_plan.v1","model_id":request.model_id,"resolved_commit":request.resolved_commit,"source_files":source_files,"nodes":nodes});
    plan["plan_digest"] = json!(crate::digest(&plan, true)?);
    let mut node_plans = BTreeMap::new();
    for node in ordered {
        node_plans.insert(node.clone(), node_slice_plan(&plan, &node)?);
    }
    Ok(SliceDocuments { plan, node_plans })
}

const MAX_FILES_PER_NODE: usize = 64; // slice_acquisition.py:65 @ f4e366e9
const MAX_SEGMENTS_PER_FILE: usize = 16384; // :66
const MAX_PATH_CHARS: usize = 1024; // :103 (Python characters)
const MAX_PATH_PARTS: usize = 16; // :106
fn lower_hex(s: &str, n: usize) -> bool {
    s.len() == n
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn valid_node(s: &str) -> bool {
    // _NODE_RE, slice_acquisition.py:69 @ f4e366e9.
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
}
fn relative(value: &Value) -> Result<&str> {
    let bad = || Error::new("slice_path_invalid");
    let s = value.as_str().ok_or_else(bad)?;
    let parts: Vec<_> = s.split('/').collect();
    if s.is_empty()
        || s.contains('\0')
        || s.chars().count() > MAX_PATH_CHARS
        || parts.len() > MAX_PATH_PARTS
        || parts.iter().any(|p| p.is_empty() || p.starts_with('.'))
    {
        return Err(bad());
    }
    Ok(s)
}
fn validate_binding(a: &BuildRequest, b: &BuildRequest) -> Result<()> {
    if a.model_id != b.model_id
        || a.resolved_commit != b.resolved_commit
        || a.deployment_id != b.deployment_id
        || a.route_label != b.route_label
        || a.stage_sharded != b.stage_sharded
        || a.compilation_cache_key != b.compilation_cache_key
        || a.node_ids != b.node_ids
        || a.managed_layer_ranges != b.managed_layer_ranges
        || a.layer_split != b.layer_split
    {
        return Err(Error::new("source_plan_binding_changed"));
    }
    Ok(())
}
struct SourceTensor {
    path: String,
    offset: u64,
    tensor: bytes::Tensor,
}
type SourceIndex = (BTreeMap<String, Value>, BTreeMap<String, SourceTensor>);
fn source_index(source: &PreparedSource<'_>) -> Result<SourceIndex> {
    let bad = || Error::new("slice_source_manifest_invalid");
    let rows = source.manifest["files"].as_array().ok_or_else(bad)?;
    let mut sources = BTreeMap::new();
    let mut tensors = BTreeMap::new();
    let mut total = 0;
    let maximum = budgets::BudgetId::PyMyceliumA13MacosServingSliceFetchMaxSourceBytesL57
        .bytes()
        .map_err(|_| bad())?
        .get();
    for row in rows {
        let path = relative(&row["path"])?;
        if !path.ends_with(".safetensors") {
            continue;
        }
        let size = row["size_bytes"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or_else(bad)?;
        let digest = row["content_digest"]["value"].as_str().ok_or_else(bad)?;
        if row["content_digest"]["algorithm"] != "sha256" || !lower_hex(digest, 64) {
            return Err(bad());
        }
        total = crate::add(total, size)?;
        if total > maximum {
            return Err(Error::new("slice_fetch_model_too_large"));
        }
        if sources
            .insert(
                path.to_owned(),
                json!({"path":path,"size_bytes":size,"content_digest":format!("sha256:{digest}")}),
            )
            .is_some()
        {
            return Err(bad());
        }
    }
    if sources.is_empty() {
        return Err(bad());
    }
    for (path, row) in &sources {
        let file = source.files.get(path).ok_or_else(bad)?;
        bytes::verify(
            file,
            row["size_bytes"].as_u64().ok_or_else(bad)?,
            row["content_digest"].as_str().ok_or_else(bad)?,
            "slice_source_manifest_invalid",
            source.checkpoint,
        )?;
        let h = bytes::header(file, source.checkpoint)?;
        for tensor in h.tensors {
            let offset = crate::add(h.bytes.len() as u64, tensor.start)?;
            if tensors
                .insert(
                    tensor.name.clone(),
                    SourceTensor {
                        path: path.clone(),
                        offset,
                        tensor,
                    },
                )
                .is_some()
            {
                return Err(Error::new("slice_source_tensor_duplicate"));
            }
        }
    }
    Ok((sources, tensors))
}
fn expected_tensors(
    manifest: &Value,
    range: [usize; 2],
    first: bool,
    last: bool,
) -> Result<BTreeSet<String>> {
    let bad = || Error::new("source_plan_assignment_mismatch");
    let mut names = BTreeSet::new();
    let mut add = |v: &Value| -> Result<()> {
        for n in v.as_array().ok_or_else(bad)? {
            names.insert(n.as_str().ok_or_else(bad)?.to_owned());
        }
        Ok(())
    };
    for layer in range[0]..range[1] {
        add(&manifest["tensor_keys_by_layer"][layer.to_string()])?;
    }
    if first {
        add(&manifest["component_tensor_keys"]["input_embedding"])?;
    }
    if last {
        add(&manifest["component_tensor_keys"]["final_norm"])?;
        add(&manifest["component_tensor_keys"]["lm_head"])?;
    }
    Ok(names)
}
struct Run {
    offset: u64,
    length: u64,
    path: String,
    source_offset: u64,
}
fn file_segments(
    file: &File,
    header: bytes::Header,
    source_files: &BTreeMap<String, File>,
    tensors: &BTreeMap<String, SourceTensor>,
    segment_bytes: u64,
    check: &dyn Fn() -> Result<()>,
) -> Result<Vec<Value>> {
    let data_start = header.bytes.len() as u64;
    let mut segments = vec![
        json!({"offset":0,"length":data_start,"sha256":bytes::sha(&header.bytes),"inline_base64":bytes::base64(&header.bytes)}),
    ];
    let mut runs: Vec<Run> = Vec::new();
    for t in header.tensors {
        let source = tensors
            .get(&t.name)
            .ok_or_else(|| Error::new("slice_source_tensor_missing"))?;
        if source.tensor.record["dtype"] != t.record["dtype"]
            || source.tensor.record["shape"] != t.record["shape"]
            || source.tensor.length != t.length
        {
            return Err(Error::new("slice_source_tensor_mismatch"));
        }
        let offset = crate::add(data_start, t.start)?;
        if let Some(last) = runs.last_mut().filter(|r| {
            r.path == source.path
                && r.offset.checked_add(r.length) == Some(offset)
                && r.source_offset.checked_add(r.length) == Some(source.offset)
        }) {
            last.length = crate::add(last.length, t.length)?;
        } else {
            runs.push(Run {
                offset,
                length: t.length,
                path: source.path.clone(),
                source_offset: source.offset,
            });
        }
    }
    for r in runs {
        let mut done = 0;
        while done < r.length {
            let size = segment_bytes.min(r.length - done);
            let offset = crate::add(r.offset, done)?;
            let source_offset = crate::add(r.source_offset, done)?;
            let data = bytes::read(file, offset, size, check)?;
            if bytes::read(&source_files[&r.path], source_offset, size, check)? != data {
                return Err(Error::new("slice_source_tensor_mismatch"));
            }
            segments.push(json!({"offset":offset,"length":size,"sha256":bytes::sha(&data),"source_path":r.path,"source_offset":source_offset}));
            if segments.len() > MAX_SEGMENTS_PER_FILE {
                return Err(Error::new("slice_segment_count_exceeded"));
            }
            done += size;
        }
    }
    Ok(segments)
}

/// Exact single-node projection of a digest-pinned complete slice plan.
/// Like the oracle this validates the parent digest, not authority/signatures.
pub fn node_slice_plan(plan: &Value, node_id: &str) -> Result<Value> {
    let bad = || Error::new("slice_plan_digest_mismatch");
    let mut body = plan.as_object().cloned().ok_or_else(bad)?;
    body.remove("plan_digest");
    if plan["protocol"] != "mycelium.a13_slice_plan.v1"
        || plan["plan_digest"] != crate::digest(&Value::Object(body), true)?
    {
        return Err(bad());
    }
    let node = plan["nodes"]
        .get(node_id)
        .ok_or_else(|| Error::new("slice_plan_node_unknown"))?;
    let files = node["files"].as_array().ok_or_else(bad)?;
    let mut used = BTreeSet::new();
    let mut download = 0;
    let mut inline = 0;
    for f in files {
        for s in f["segments"].as_array().ok_or_else(bad)? {
            let len = s["length"].as_u64().ok_or_else(bad)?;
            if let Some(path) = s["source_path"].as_str() {
                used.insert(path);
                download = crate::add(download, len)?;
            }
            if s.get("inline_base64").is_some() {
                inline = crate::add(inline, len)?;
            }
        }
    }
    let sources: Vec<_> = plan["source_files"]
        .as_array()
        .ok_or_else(bad)?
        .iter()
        .filter(|r| r["path"].as_str().is_some_and(|p| used.contains(p)))
        .cloned()
        .collect();
    let mut value = json!({"protocol":"mycelium.a13_node_slice_plan.v1","plan_digest":plan["plan_digest"],"model_id":plan["model_id"],"resolved_commit":plan["resolved_commit"],"node_id":node_id,"source_files":sources,"files":files,"download_bytes":download,"inline_bytes":inline});
    value["node_plan_digest"] = json!(crate::digest(&value, true)?);
    Ok(value)
}
