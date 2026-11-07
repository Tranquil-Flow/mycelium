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
    if cursor != layers || owned != tensors.keys().cloned().collect() {
        return Err(bad());
    }
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
