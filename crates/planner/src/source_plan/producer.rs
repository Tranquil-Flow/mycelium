// SPDX-License-Identifier: AGPL-3.0-only
//! Source-bound metadata assignments. Never native load/challenge evidence.
use super::*;

#[derive(Debug)]
pub struct AssignmentDocuments {
    pub route: Value,
    pub assignments: Vec<Value>,
}

/// Produce metadata assignments from held source bytes, not a ready route.
/// The caller supplies freshly admitted current bindings and owns path confinement.
/// This whole-source boundary does not emit a stage-sharded representation.
/// Production model/resource admission remains `chain_compile::prepare`'s job.
pub fn compile_source_assignments(
    request: &BuildRequest,
    current_request: &BuildRequest,
    source: PreparedSource<'_>,
    epoch: u64,
    cache_roots: &Value,
    runtime_by_node: &Value,
) -> Result<AssignmentDocuments> {
    validate_binding(request, current_request)?;
    if request.stage_sharded {
        return Err(Error::new("native_stage_shard_producer_unavailable"));
    }
    let manifest = source.manifest;
    if !matches!(manifest["architecture"].as_str(), Some("qwen2" | "qwen3"))
        || !crate::bound::verify_manifest_digest(manifest).unwrap_or(false)
        || crate::bound::manifest_digest_ref(manifest).ok().as_deref()
            != Some(source.manifest_digest)
        || manifest["model_id"] != request.model_id
        || manifest["resolved_commit"] != request.resolved_commit
    {
        return Err(Error::new("slice_source_manifest_invalid"));
    }
    let bad = || Error::new("source_plan_assignment_mismatch");
    let nodes = &request.node_ids;
    let stages = request.layer_split["stages"].as_array().ok_or_else(bad)?;
    let layers = manifest["num_layers"].as_u64().ok_or_else(bad)?;
    if nodes.is_empty()
        || nodes.windows(2).any(|p| p[0] >= p[1])
        || nodes.len() != request.managed_layer_ranges.len()
        || nodes.len() != stages.len()
    {
        return Err(bad());
    }
    // Exact node sets: extra cache/runtime identities cannot silently disappear.
    for map in [cache_roots, runtime_by_node] {
        if map.as_object().ok_or_else(bad)?.keys().collect::<Vec<_>>()
            != nodes.iter().collect::<Vec<_>>()
        {
            return Err(bad());
        }
    }
    let (_, tensors) = source_index(&source)?;
    let mut route = Vec::new();
    let mut cursor = 0;
    let mut owned = BTreeSet::new();
    for (i, node) in nodes.iter().enumerate() {
        let [start, end] = request.managed_layer_ranges[i];
        if !valid_node(node)
            || start as u64 != cursor
            || end <= start
            || end as u64 > layers
            || stages[i]["node_id"] != *node
            || stages[i]["start"] != json!(start)
            || stages[i]["end"] != json!(end)
        {
            return Err(bad());
        }
        cursor = end as u64;
        let names = expected_tensors(manifest, [start, end], i == 0, i + 1 == nodes.len())?;
        if names.iter().any(|name| !tensors.contains_key(name)) {
            return Err(Error::new("slice_source_tensor_missing"));
        }
        owned.extend(names);
        route.push(json!({"node_id":node,"range":{"start_layer":start,"end_layer_exclusive":end,"layer_count":end-start}}));
    }
    if cursor != layers {
        return Err(bad());
    }
    verify_coverage(&source, &tensors, &owned)?;
    let route = json!({
        "protocol":"mycelium.manual_provisioning_route.v1", "ok":true,
        "claim_boundary":"metadata only; not a loaded route",
        "model":{"model_id":request.model_id,"resolved_commit":request.resolved_commit,
            "manifest_digest":source.manifest_digest,"num_layers":layers},
        "route":route
    });
    let assignments = crate::compile::compile_assignments(&json!({
        "manifest":manifest,"route_plan":route,"deployment_id":request.deployment_id,
        "deployment_epoch":epoch,"cache_roots":cache_roots,"runtime_by_node":runtime_by_node
    }))?
    .as_array()
    .cloned()
    .ok_or_else(bad)?;
    for assignment in &assignments {
        crate::compile::validate_assignment(assignment)?;
        let mut required_files = BTreeSet::new();
        for name in assignment["expected_tensor_keys"]
            .as_array()
            .ok_or_else(bad)?
        {
            let tensor = tensors
                .get(name.as_str().ok_or_else(bad)?)
                .ok_or_else(bad)?;
            required_files.insert(tensor.path.as_str());
        }
        let assigned_files = assignment["files"]
            .as_array()
            .ok_or_else(bad)?
            .iter()
            .map(|file| file["path"].as_str().ok_or_else(bad))
            .collect::<Result<BTreeSet<_>>>()?;
        if assigned_files != required_files {
            return Err(bad());
        }
    }
    // Rehash after construction. This detects drift, not an immutable snapshot.
    source_index(&source)?;
    Ok(AssignmentDocuments { route, assignments })
}

/// Account for the physical duplicate present in pinned tied-embedding sources.
/// This is not permission to ignore arbitrary unassigned tensors: require the
/// explicit component alias AND identical dtype, shape and every payload byte.
fn verify_coverage(
    source: &PreparedSource<'_>,
    tensors: &BTreeMap<String, SourceTensor>,
    owned: &BTreeSet<String>,
) -> Result<()> {
    let all: BTreeSet<_> = tensors.keys().cloned().collect();
    if *owned == all {
        return Ok(());
    }
    let bad = || Error::new("source_plan_assignment_mismatch");
    let extra: Vec<_> = all.difference(owned).map(String::as_str).collect();
    if !owned.is_subset(&all)
        || extra != ["lm_head.weight"]
        || source.manifest["component_aliases"]["lm_head"] != "input_embedding"
        || source.manifest["component_tensor_keys"]["lm_head"]
            != json!(["model.embed_tokens.weight"])
        || source.manifest["component_tensor_keys"]["input_embedding"]
            != json!(["model.embed_tokens.weight"])
    {
        return Err(bad());
    }
    let head = tensors.get("lm_head.weight").ok_or_else(bad)?;
    let embedding = tensors.get("model.embed_tokens.weight").ok_or_else(bad)?;
    let mismatch = || Error::new("source_plan_redundant_tensor_mismatch");
    if head.tensor.length != embedding.tensor.length
        || head.tensor.record["dtype"] != embedding.tensor.record["dtype"]
        || head.tensor.record["shape"] != embedding.tensor.record["shape"]
    {
        return Err(mismatch());
    }
    let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
        .bytes()
        .map_err(|_| bad())?
        .get();
    let head_file = source.files.get(&head.path).ok_or_else(bad)?;
    let embedding_file = source.files.get(&embedding.path).ok_or_else(bad)?;
    let mut offset = 0;
    while offset < head.tensor.length {
        let length = cap.min(head.tensor.length - offset);
        if bytes::read(head_file, crate::add(head.offset, offset)?, length)?
            != bytes::read(
                embedding_file,
                crate::add(embedding.offset, offset)?,
                length,
            )?
        {
            return Err(mismatch());
        }
        offset = crate::add(offset, length)?;
    }
    Ok(())
}

/// Real shard bytes and transfer pins, but no loaded-stage or execution proof.
#[derive(Debug)]
pub struct ProducedShards {
    pub files: BTreeMap<String, File>,
    pub node_files: BTreeMap<String, Vec<Value>>,
    pub node_files_digest: String,
    pub slices: SliceDocuments,
}

/// Emit BF16/F16/F32 dense safetensors into a NEW private directory. The caller
/// owns the parent directory and source immutability, resource/storage consent,
/// and current member admission. On failure partial output remains unadmitted;
/// a retry must use a fresh directory, never overwrite or adopt existing bytes.
/// This performs no quantization, native load, signing or model execution.
pub fn emit_stage_shards(
    request: &BuildRequest,
    current_request: &BuildRequest,
    source: PreparedSource<'_>,
    output: &std::path::Path,
) -> Result<ProducedShards> {
    use sha2::{Digest, Sha256};
    use std::{
        fs,
        io::Write,
        os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    };
    validate_binding(request, current_request)?;
    let bad = || Error::new("source_plan_assignment_mismatch");
    let manifest = source.manifest;
    if !request.stage_sharded
        || !matches!(manifest["architecture"].as_str(), Some("qwen2" | "qwen3"))
        || !crate::bound::verify_manifest_digest(manifest).unwrap_or(false)
        || crate::bound::manifest_digest_ref(manifest).ok().as_deref()
            != Some(source.manifest_digest)
        || manifest["model_id"] != request.model_id
        || manifest["resolved_commit"] != request.resolved_commit
    {
        return Err(Error::new("slice_source_manifest_invalid"));
    }
    let (sources, tensors) = source_index(&source)?;
    let nodes = &request.node_ids;
    let stages = request.layer_split["stages"].as_array().ok_or_else(bad)?;
    let layers = manifest["num_layers"].as_u64().ok_or_else(bad)?;
    if nodes.is_empty()
        || nodes.len() > crate::chain_compile::MAX_HOSTS
        || nodes.windows(2).any(|p| p[0] >= p[1])
        || nodes.len() != stages.len()
        || nodes.len() != request.managed_layer_ranges.len()
    {
        return Err(bad());
    }
    let mut selected = Vec::new();
    let mut all = BTreeSet::new();
    let mut cursor = 0;
    for (i, node) in nodes.iter().enumerate() {
        let [start, end] = request.managed_layer_ranges[i];
        if !valid_node(node)
            || start as u64 != cursor
            || end <= start
            || end as u64 > layers
            || stages[i]["node_id"] != *node
            || stages[i]["start"] != start
            || stages[i]["end"] != end
        {
            return Err(bad());
        }
        let names = expected_tensors(manifest, [start, end], i == 0, i + 1 == nodes.len())?;
        for name in &names {
            let t = &tensors
                .get(name)
                .ok_or_else(|| Error::new("slice_source_tensor_missing"))?
                .tensor;
            let width = match t.record["dtype"].as_str() {
                Some("BF16" | "F16") => 2_u64,
                Some("F32") => 4,
                _ => return Err(Error::new("stage_shard_dtype_unsupported")),
            };
            let shape = t.record["shape"].as_array().ok_or_else(bad)?;
            let size = shape.iter().try_fold(width, |n, d| {
                n.checked_mul(d.as_u64().ok_or_else(bad)?).ok_or_else(bad)
            })?;
            if size != t.length {
                return Err(bad());
            }
        }
        all.extend(names.iter().cloned());
        selected.push(names);
        cursor = end as u64;
    }
    if cursor != layers {
        return Err(bad());
    }
    verify_coverage(&source, &tensors, &all)?;
    let maximum = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxHeaderBytesL67
        .bytes()
        .map_err(|_| bad())?
        .get();
    let chunk = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
        .bytes()
        .map_err(|_| bad())?
        .get();
    // Prepare every bounded header before the first filesystem mutation.
    let mut headers = Vec::new();
    for names in &selected {
        let mut header = Map::new();
        let mut offset = 0;
        for name in names {
            let t = &tensors[name].tensor;
            let end = crate::add(offset, t.length)?;
            header.insert(name.clone(),json!({"dtype":t.record["dtype"],"shape":t.record["shape"],"data_offsets":[offset,end]}));
            offset = end;
        }
        let mut bytes = serde_json::to_vec(&header).map_err(|_| bad())?;
        bytes.resize(bytes.len().div_ceil(8) * 8, b' ');
        if bytes.len() as u64 > maximum {
            return Err(Error::new("slice_safetensors_invalid"));
        }
        headers.push(bytes);
    }
    let io = || Error::new("stage_shard_output_invalid");
    let parent = output.parent().ok_or_else(io)?;
    if parent.canonicalize().map_err(|_| io())? != parent
        || output.file_name().is_none_or(|n| {
            n.to_str()
                .is_none_or(|s| s.is_empty() || s.starts_with('.'))
        })
    {
        return Err(io());
    }
    fs::DirBuilder::new()
        .mode(0o700)
        .create(output)
        .map_err(|_| io())?;
    let mut files = BTreeMap::new();
    let mut node_files = BTreeMap::new();
    for (i, ((node, names), header)) in nodes.iter().zip(&selected).zip(&headers).enumerate() {
        let name = format!("model-stage-{:03}-of-{:03}.safetensors", i + 1, nodes.len());
        let mut file = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(output.join(&name))
            .map_err(|_| io())?;
        let mut hash = Sha256::new();
        let prefix = (header.len() as u64).to_le_bytes();
        file.write_all(&prefix)
            .and_then(|()| file.write_all(header))
            .map_err(|_| io())?;
        hash.update(prefix);
        hash.update(header);
        let mut size = crate::add(8, header.len() as u64)?;
        for name in names {
            let t = &tensors[name];
            let mut done = 0;
            while done < t.tensor.length {
                let length = chunk.min(t.tensor.length - done);
                let data =
                    bytes::read(&source.files[&t.path], crate::add(t.offset, done)?, length)?;
                file.write_all(&data).map_err(|_| io())?;
                hash.update(&data);
                done += length;
            }
            size = crate::add(size, t.tensor.length)?;
        }
        file.sync_all().map_err(|_| io())?;
        let digest = format!("sha256:{:x}", hash.finalize());
        bytes::verify(&file, size, &digest, "stage_shard_output_changed")?;
        node_files.insert(node.clone(),vec![json!({"path":format!("deployment/{name}"),"size_bytes":size,"content_digest":digest})]);
        files.insert(name, file);
    }
    // Revalidate source after streaming and use the independent recipe compiler
    // to check tensor ownership and source/output equality before admission.
    for (path, row) in sources {
        bytes::verify(
            &source.files[&path],
            row["size_bytes"].as_u64().ok_or_else(bad)?,
            row["content_digest"].as_str().ok_or_else(bad)?,
            "slice_source_manifest_invalid",
        )?;
    }
    let node_files_digest = crate::digest(&json!(node_files), true)?;
    let slices = compile_slices(
        request,
        current_request,
        source,
        EmittedShards {
            files: &files,
            node_files: &node_files,
            node_files_digest: &node_files_digest,
            max_segment_bytes: chunk,
        },
    )?;
    File::open(output)
        .and_then(|file| file.sync_all())
        .map_err(|_| io())?;
    Ok(ProducedShards {
        files,
        node_files,
        node_files_digest,
        slices,
    })
}
