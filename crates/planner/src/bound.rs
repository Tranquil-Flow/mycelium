// SPDX-License-Identifier: AGPL-3.0-only
//! Faithful port of the bound compiler at f4e366e9: `layer_assignment.py`
//! (`compile_layer_assignments`), `route_contract.py` (manual provisioning route,
//! legacy upgrade), the manifest digest helpers of `model_manifest.py`, and
//! `planner_assignment.py` (`compile_bound_layer_assignments`,
//! `compile_bound_replica_assignments`, `validate_control_plane_tranche`).
//!
//! Boundary notes:
//! * Evidence-bundle parsing/derivation, the planner-snapshot digest binding and the
//!   assignment UUIDv5 identity are the shared L5 implementations
//!   (`protocol::gossip`, `protocol::assignment`); this module does not fork them.
//!   Evidence-bundle refusals surface with the oracle's exact per-field
//!   `EvidenceBundleError` text, which `protocol::gossip` returns (L5 99cd3f7).
//! * The oracle iterates dictionaries in insertion order. JSON documents here are
//!   sorted-key (`serde_json` without `preserve_order`), so component order for a
//!   manifest is the sorted-key order; wire manifests are sorted-key documents.
//! * Python `str.format` is supported for the `{layer}` field only.
use crate::py::{Failure, Kind, PyResult, get, has, int, is_bool, py_eq, py_repr, req, verr};
use crate::runtime::{MLX_RUNTIME_BASE_FIELDS, validate_normalized_runtime};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub const CONTROL_PLANE_BINDING_PROTOCOL: &str = "mycelium.control_plane_binding.v1";
pub const LAYER_ASSIGNMENT_PROTOCOL: &str = "mycelium.layer_assignment.v2";
pub const MANUAL_PROVISIONING_ROUTE_PROTOCOL: &str = "mycelium.manual_provisioning_route.v1";
pub const LEGACY_ROUTE_PLAN_PROTOCOL: &str = "mycelium.route_plan.v1";

fn is_lower_hex(s: &str, len: usize) -> bool {
    s.len() == len
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn str_of(v: &Value) -> String {
    crate::py::py_str(v)
}

pub fn validate_layer_range(layer_range: &Value, num_layers: &Value) -> PyResult<()> {
    let start = get(layer_range, "start_layer")?;
    let end = get(layer_range, "end_layer_exclusive")?;
    let count = get(layer_range, "layer_count")?;
    let (Some(start), Some(end), Some(count)) = (int(start), int(end), int(count)) else {
        return verr("layer range fields must be integers");
    };
    if start < 0 {
        return verr("start_layer must be non-negative");
    }
    if end <= start {
        return verr("end_layer_exclusive must be greater than start_layer");
    }
    match int(num_layers) {
        Some(n) if end > n => return verr("end_layer_exclusive exceeds num_layers"),
        None => return Err(Failure::other("TypeError: num_layers comparison")),
        _ => {}
    }
    if end - start != count {
        return verr("layer_count does not match half-open range");
    }
    Ok(())
}

fn python_strip_is_empty(s: &str) -> bool {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        .is_empty()
}

pub fn validate_manual_provisioning_route_v1(plan: &Value) -> PyResult<()> {
    let expected = format!("expected {MANUAL_PROVISIONING_ROUTE_PROTOCOL}");
    if !plan.is_object() || plan["protocol"] != json!(MANUAL_PROVISIONING_ROUTE_PROTOCOL) {
        return verr(expected);
    }
    if !is_bool(get(plan, "ok")?, true) {
        return verr("route plan is not successful");
    }
    let model = get(plan, "model")?;
    if !model.is_object() {
        return verr("model must be an object");
    }
    if get(model, "model_id")?.as_str().is_none_or(str::is_empty) {
        return verr("model_id required");
    }
    let num_layers = get(model, "num_layers")?;
    if int(num_layers).is_none_or(|n| n <= 0) {
        return verr("num_layers must be positive");
    }
    let digest = if has(model, "manifest_digest") {
        str_of(get(model, "manifest_digest")?)
    } else {
        String::new()
    };
    if !digest
        .strip_prefix("sha256:")
        .is_some_and(|h| is_lower_hex(h, 64))
    {
        return verr("manifest_digest must be sha256:<64 lowercase hex>");
    }
    let commit = if has(model, "resolved_commit") {
        str_of(get(model, "resolved_commit")?)
    } else {
        String::new()
    };
    if !is_lower_hex(&commit, 40) {
        return verr("resolved_commit must be immutable 40-hex");
    }
    if get(plan, "claim_boundary")?
        .as_str()
        .is_none_or(python_strip_is_empty)
    {
        return verr("claim_boundary must be a non-empty string");
    }
    let route = get(plan, "route")?;
    let Some(route) = route.as_array().filter(|r| !r.is_empty()) else {
        return verr("route must contain at least one stage");
    };
    let mut expected_start: i128 = 0;
    let mut seen = BTreeSet::new();
    for stage in route {
        let node_id = get(stage, "node_id")?;
        let Some(node) = node_id.as_str().filter(|s| !s.is_empty()) else {
            return verr("every route stage requires node_id");
        };
        if !seen.insert(node.to_owned()) {
            return verr(format!("duplicate primary route node: {node}"));
        }
        let layer_range = get(stage, "range")?;
        if !layer_range.is_object() {
            return verr(format!("route stage {node} requires range"));
        }
        validate_layer_range(layer_range, num_layers)?;
        let start = int(&layer_range["start_layer"]).unwrap_or(0);
        if start > expected_start {
            return verr(format!("route gap before layer {start}"));
        }
        if start < expected_start {
            return verr(format!("route overlap at layer {start}"));
        }
        expected_start = int(&layer_range["end_layer_exclusive"]).unwrap_or(0);
    }
    let layers = int(num_layers).unwrap_or(0);
    if expected_start < layers {
        return verr(format!("route gap after layer {}", expected_start - 1));
    }
    if expected_start > layers {
        return verr("route exceeds model layer count");
    }
    let order = get(plan, "node_order")?;
    if !order.is_null() {
        let nodes: Vec<Value> = route.iter().map(|s| s["node_id"].clone()).collect();
        if !py_eq(order, &Value::Array(nodes)) {
            return verr("node_order does not match route");
        }
    }
    Ok(())
}

pub fn upgrade_legacy_route_plan_v1(plan_v1: &Value) -> PyResult<Value> {
    if !plan_v1.is_object() || plan_v1["protocol"] != json!(LEGACY_ROUTE_PLAN_PROTOCOL) {
        return verr(format!("expected {LEGACY_ROUTE_PLAN_PROTOCOL}"));
    }
    let mut upgraded = plan_v1.as_object().cloned().unwrap_or_default();
    upgraded.insert("protocol".into(), json!(MANUAL_PROVISIONING_ROUTE_PROTOCOL));
    upgraded.insert("source_protocol".into(), json!(LEGACY_ROUTE_PLAN_PROTOCOL));
    upgraded.entry("claim_boundary").or_insert_with(|| {
        json!("legacy route conversion for manual provisioning only; not product Planner output")
    });
    let mut converted = Vec::new();
    let stages = match upgraded.get("route") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(a)) => a.clone(),
        Some(Value::Bool(false)) => Vec::new(),
        Some(_) => return Err(Failure::other("TypeError: route not iterable")),
    };
    for stage in stages {
        let mut stage = stage
            .as_object()
            .cloned()
            .ok_or_else(|| Failure::other("AttributeError: stage.pop"))?;
        let legacy = stage.remove("layers");
        let Some(pair) = legacy
            .as_ref()
            .and_then(Value::as_array)
            .filter(|p| p.len() == 2)
        else {
            return verr("v1 stage requires inclusive layers pair");
        };
        let (Some(start), Some(end_inclusive)) = (int(&pair[0]), int(&pair[1])) else {
            return verr("v1 layers must be integers");
        };
        let layer_count = stage.get("layer_count").cloned().unwrap_or(Value::Null);
        stage.insert(
            "range".into(),
            json!({
                "start_layer": start as i64,
                "end_layer_exclusive": (end_inclusive + 1) as i64,
                "layer_count": layer_count,
            }),
        );
        converted.push(Value::Object(stage));
    }
    upgraded.insert("route".into(), Value::Array(converted));
    let upgraded = Value::Object(upgraded);
    validate_manual_provisioning_route_v1(&upgraded)?;
    Ok(upgraded)
}

/// `model_manifest.manifest_digest_ref`.
pub fn manifest_digest_ref(manifest: &Value) -> PyResult<String> {
    let digest = get(manifest, "manifest_digest")?;
    let falsy = digest.is_null()
        || digest.as_object().is_some_and(Map::is_empty)
        || is_bool(digest, false)
        || digest.as_str() == Some("");
    let empty = json!({});
    let digest = if falsy { &empty } else { digest };
    let value = if has(digest, "value") {
        str_of(get(digest, "value")?)
    } else {
        String::new()
    };
    if get(digest, "algorithm")? != &json!("sha256") || !is_lower_hex(&value, 64) {
        return verr("manifest has invalid manifest_digest");
    }
    Ok(format!("sha256:{value}"))
}

/// `model_manifest.verify_manifest_digest`.
pub fn verify_manifest_digest(manifest: &Value) -> PyResult<bool> {
    let Some(map) = manifest.as_object() else {
        return Err(Failure::other("AttributeError: manifest.pop"));
    };
    let mut unsigned = map.clone();
    let supplied = unsigned.remove("manifest_digest").unwrap_or(Value::Null);
    let text = crate::canonical(&Value::Object(unsigned), false)
        .map_err(|_| Failure::value("Out of range float values are not JSON compliant"))?;
    let value = {
        use sha2::{Digest, Sha256};
        format!("{:x}", Sha256::digest(text.as_bytes()))
    };
    Ok(py_eq(
        &supplied,
        &json!({"algorithm": "sha256", "value": value}),
    ))
}

/// Python `uuid.UUID(text)` for the canonical, braced, `urn:uuid:` and bare-hex
/// spellings (`int(hex, 16)` oddities such as underscores are refused).
fn python_uuid(text: &str) -> Option<[u8; 16]> {
    let compact = text.replace("urn:", "").replace("uuid:", "");
    let compact = compact.trim_matches(['{', '}']).replace('-', "");
    if compact.len() != 32 || !compact.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let mut out = [0u8; 16];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&compact[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(out)
}

fn uuid_text(b: &[u8; 16]) -> String {
    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &h[..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..]
    )
}

/// `layer_assignment.assignment_id_for` via the shared L5 identity.
pub fn assignment_id_for(assignment: &Value) -> PyResult<String> {
    protocol::assignment::id_for(assignment).map_err(Failure::value)
}

/// `layer_assignment.validate_assignment_identity` via the shared L5 identity.
pub fn validate_assignment_identity(assignment: &Value) -> PyResult<()> {
    protocol::assignment::validate_identity(assignment).map_err(Failure::value)
}

fn normalize_node_runtime(runtime: &Value, node_id: &str, manifest: &Value) -> PyResult<Value> {
    if !runtime.is_object() {
        return verr(format!("runtime identity missing for {node_id}"));
    }
    let keys: BTreeSet<&str> = runtime
        .as_object()
        .map(|m| m.keys().map(String::as_str).collect())
        .unwrap_or_default();
    if keys != MLX_RUNTIME_BASE_FIELDS.iter().copied().collect() {
        return verr(format!(
            "runtime fields for {node_id} must be backend, dtype, and quantization"
        ));
    }
    for field in ["backend", "dtype", "quantization"] {
        if runtime[field].as_str().is_none_or(str::is_empty) {
            return verr(format!("runtime {field} missing for {node_id}"));
        }
    }
    let mut normalized = runtime.as_object().cloned().unwrap_or_default();
    let backend = runtime["backend"].as_str().unwrap_or("");
    if ["mlx", "numpy", "mobile-rust-qwen2"].contains(&backend) {
        let runtime_model = get(manifest, "runtime_model")?;
        if !runtime_model.is_object() {
            return verr(format!(
                "manifest lacks normalized {backend} runtime model for {node_id}"
            ));
        }
        normalized.extend(runtime_model.as_object().cloned().unwrap_or_default());
        return validate_normalized_runtime(&Value::Object(normalized), Some(backend));
    }
    Ok(Value::Object(normalized))
}

fn string_list(v: &Value) -> PyResult<Vec<String>> {
    v.as_array()
        .ok_or_else(|| Failure::other("TypeError: not iterable"))?
        .iter()
        .map(|x| {
            x.as_str()
                .map(str::to_owned)
                .ok_or_else(|| Failure::other("TypeError: unhashable or non-string"))
        })
        .collect()
}

fn truthy_list(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
        Value::String(s) => !s.is_empty(),
        Value::Bool(b) => *b,
        Value::Number(n) => crate::py::number(n) != crate::py::Num::Int(0),
    }
}

/// `layer_assignment.compile_layer_assignments`.
pub fn compile_layer_assignments(
    route_plan: &Value,
    manifest: &Value,
    deployment_id: &Value,
    deployment_epoch: &Value,
    cache_roots: &Value,
    runtime_by_node: &Value,
    control_plane_binding: Option<&Value>,
) -> PyResult<Vec<Value>> {
    let upgraded;
    let route_plan = if get(route_plan, "protocol")? == &json!(LEGACY_ROUTE_PLAN_PROTOCOL) {
        upgraded = upgrade_legacy_route_plan_v1(route_plan)?;
        &upgraded
    } else {
        route_plan
    };
    validate_manual_provisioning_route_v1(route_plan)?;
    if !verify_manifest_digest(manifest)? {
        return verr("manifest digest mismatch");
    }
    let route_model = if has(route_plan, "model") {
        get(route_plan, "model")?.clone()
    } else {
        json!({})
    };
    let digest_ref = manifest_digest_ref(manifest)?;
    let identity = [
        ("model_id", get(manifest, "model_id")?.clone()),
        ("num_layers", get(manifest, "num_layers")?.clone()),
        ("manifest_digest", json!(digest_ref)),
        ("resolved_commit", get(manifest, "resolved_commit")?.clone()),
    ];
    for (field, expected) in &identity {
        if !py_eq(get(&route_model, field)?, expected) {
            return verr(format!("route model {field} does not match manifest"));
        }
    }
    let namespace = match deployment_id {
        Value::String(s) => python_uuid(s),
        Value::Null => None,
        _ => return Err(Failure::other("AttributeError: deployment_id")),
    };
    let Some(namespace) = namespace else {
        return verr("deployment_id must be a UUID");
    };
    let epoch_ok = int(deployment_epoch).is_some_and(|e| e >= 0);
    if !epoch_ok {
        return verr("deployment_epoch must be a non-negative integer");
    }
    if let Some(binding) = control_plane_binding
        && !binding.is_null()
        && !binding.is_object()
    {
        return verr("control_plane_binding must be an object or null");
    }

    let mut file_records = BTreeMap::new();
    for item in req(manifest, "files")?
        .as_array()
        .ok_or_else(|| Failure::other("TypeError: files"))?
    {
        let path = req(item, "path")?
            .as_str()
            .ok_or_else(|| Failure::other("TypeError: unhashable path"))?;
        file_records.insert(path.to_owned(), item);
    }
    let static_tensor_keys = if has(manifest, "component_tensor_keys") {
        get(manifest, "component_tensor_keys")?.clone()
    } else {
        json!({})
    };
    let template = req(manifest, "block_prefix_template")?
        .as_str()
        .ok_or_else(|| Failure::other("AttributeError: format"))?
        .to_owned();
    let mut assignments = Vec::new();
    for stage in req(route_plan, "route")?
        .as_array()
        .cloned()
        .unwrap_or_default()
    {
        let node_id = req(&stage, "node_id")?
            .as_str()
            .ok_or_else(|| Failure::other("TypeError: node_id"))?
            .to_owned();
        let layer_range = req(&stage, "range")?.clone();
        if !layer_range.is_object() {
            return Err(Failure::other("TypeError: dict(range)"));
        }
        validate_layer_range(&layer_range, req(manifest, "num_layers")?)?;
        let cache_root_raw = get(cache_roots, &node_id)?;
        protocol::assignment::validate_cache_root(cache_root_raw, &node_id)
            .map_err(Failure::value)?;
        let runtime = normalize_node_runtime(get(runtime_by_node, &node_id)?, &node_id, manifest)?;

        let start = int(&layer_range["start_layer"]).unwrap_or(0);
        let end = int(&layer_range["end_layer_exclusive"]).unwrap_or(0);
        let num_layers = int(&manifest["num_layers"]).unwrap_or(0);
        let mut components = vec!["decoder".to_owned()];
        if start == 0 && truthy_list(get(&static_tensor_keys, "input_embedding")?) {
            components.insert(0, "input_embedding".into());
        }
        if end == num_layers {
            for (component, keys) in static_tensor_keys.as_object().into_iter().flatten() {
                if component != "input_embedding" && truthy_list(keys) {
                    components.push(component.clone());
                }
            }
        }
        let layers: Vec<i128> = (start..end).collect();
        let mut tensor_prefixes = Vec::new();
        for layer in &layers {
            tensor_prefixes.push(crate::py::format_layer(&template, *layer as u64)?);
        }
        let mut decoder_keys = BTreeSet::new();
        let mut covering = BTreeSet::new();
        for layer in &layers {
            let key = layer.to_string();
            for tensor in string_list(req(req(manifest, "tensor_keys_by_layer")?, &key)?)? {
                decoder_keys.insert(tensor);
            }
            for path in string_list(req(req(manifest, "layer_files")?, &key)?)? {
                covering.insert(path);
            }
        }
        let mut component_tensor_keys = Map::new();
        for component in &components {
            let keys: Vec<String> = if component == "decoder" {
                decoder_keys.iter().cloned().collect()
            } else {
                string_list(req(&static_tensor_keys, component)?)?
            };
            component_tensor_keys.insert(component.clone(), json!(keys));
        }
        let mut component_aliases = Map::new();
        if let Some(aliases) = get(manifest, "component_aliases")?.as_object() {
            for (component, target) in aliases {
                if components.contains(component) {
                    component_aliases.insert(component.clone(), target.clone());
                }
            }
        }
        let tensor_keys: BTreeSet<String> = component_tensor_keys
            .values()
            .flat_map(|v| v.as_array().into_iter().flatten())
            .filter_map(|v| v.as_str().map(str::to_owned))
            .collect();
        for component in &components {
            if component != "decoder" {
                for path in string_list(req(req(manifest, "component_files")?, component)?)? {
                    covering.insert(path);
                }
            }
        }
        let mut files = Vec::new();
        for path in &covering {
            let record = file_records
                .get(path)
                .ok_or_else(|| Failure::other(format!("KeyError: {path}")))?;
            let digest = req(record, "content_digest")?;
            files.push(json!({
                "path": path,
                "size_bytes": req(record, "size_bytes")?,
                "content_digest": format!(
                    "{}:{}",
                    str_of(req(digest, "algorithm")?),
                    str_of(req(digest, "value")?)
                ),
            }));
        }
        let mut semantic = json!({
            "protocol": LAYER_ASSIGNMENT_PROTOCOL,
            "deployment_id": uuid_text(&namespace),
            "deployment_epoch": deployment_epoch,
            "node_id": node_id,
            "manifest_digest": digest_ref,
            "model_id": req(manifest, "model_id")?,
            "resolved_commit": req(manifest, "resolved_commit")?,
            "range": layer_range,
            "components": components,
            "component_tensor_keys": component_tensor_keys,
            "component_aliases": component_aliases,
            "expected_tensor_prefixes": tensor_prefixes,
            "expected_tensor_keys": tensor_keys,
            "files": files,
            "artifact_cache_root": cache_root_raw,
            "runtime": runtime,
        });
        if let Some(binding) = control_plane_binding
            && !binding.is_null()
        {
            semantic["control_plane_binding"] = binding.clone();
        }
        let id = assignment_id_for(&semantic)?;
        let mut assignment = semantic;
        assignment["assignment_id"] = json!(id);
        assignment["route_ready"] = json!(false);
        assignment["claim_boundary"] =
            json!("minimal upstream shards assigned; runtime has not loaded or probed layers");
        assignments.push(assignment);
    }
    Ok(assignments)
}

fn route_wire(route_plan: &Value) -> PyResult<Value> {
    if route_plan.is_object() {
        Ok(route_plan.clone())
    } else {
        verr("route_plan must be a RoutePlanV2 or mapping")
    }
}

fn lineage_binding(
    bundle: &Value,
    snapshot_digest: &str,
    deployment_id: &Value,
    deployment_epoch: &Value,
) -> Value {
    json!({
        "protocol": CONTROL_PLANE_BINDING_PROTOCOL,
        "evidence_bundle_digest": bundle["evidence_bundle_digest"],
        "planner_snapshot_digest": snapshot_digest,
        "snapshot_generation": bundle["snapshot_generation"],
        "swarm_id": bundle["swarm_id"],
        "deployment_id": deployment_id,
        "deployment_epoch": deployment_epoch,
    })
}

/// `planner_gossip_adapter.planner_snapshot_digest`.
pub fn planner_snapshot_digest(snapshot: &Value) -> PyResult<String> {
    crate::digest(snapshot, false)
        .map_err(|_| Failure::value("planner snapshot must be canonical JSON"))
}

/// Shared evidence parsing (`evidence_bundle_to_dict(evidence_bundle_from_dict(x))`).
fn parse_evidence(bundle: &Value, limits: protocol::gossip::Limits) -> PyResult<Value> {
    protocol::gossip::parse_bundle(bundle, limits).map_err(|e| Failure::value(e.0))
}

fn validate_lineage(
    route_plan: &Value,
    planner_snapshot: &Value,
    evidence_bundle: &Value,
    manifest: &Value,
    deployment_id: &Value,
    deployment_epoch: &Value,
    limits: protocol::gossip::Limits,
) -> PyResult<(Value, String)> {
    let bundle = parse_evidence(evidence_bundle, limits)?;
    if get(planner_snapshot, "protocol")? != &json!("mycelium.layer_planner_snapshot.v1") {
        return verr("planner snapshot protocol mismatch");
    }
    let snapshot_digest = planner_snapshot_digest(planner_snapshot)?;
    if get(route_plan, "protocol")? != &json!("mycelium.route_plan.v2") {
        return verr("expected mycelium.route_plan.v2");
    }
    if get(route_plan, "snapshot_digest")? != &json!(snapshot_digest) {
        return verr("route snapshot_digest does not bind the supplied planner snapshot");
    }
    if !py_eq(
        get(planner_snapshot, "evidence_bundle_digest")?,
        &bundle["evidence_bundle_digest"],
    ) {
        return verr("planner snapshot evidence_bundle_digest mismatch");
    }
    if !py_eq(
        get(planner_snapshot, "snapshot_generation")?,
        &bundle["snapshot_generation"],
    ) {
        return verr("planner snapshot snapshot_generation mismatch");
    }
    if !py_eq(get(planner_snapshot, "swarm_id")?, &bundle["swarm_id"]) {
        return verr("planner snapshot swarm_id mismatch");
    }
    if !py_eq(get(planner_snapshot, "deployment")?, &bundle["deployment"]) {
        return verr("planner snapshot deployment identity mismatch");
    }
    if !py_eq(deployment_id, &bundle["deployment"]["deployment_id"]) {
        return verr("deployment_id does not match evidence bundle");
    }
    if !py_eq(deployment_epoch, &bundle["deployment"]["deployment_epoch"]) {
        return verr("deployment_epoch does not match evidence bundle");
    }
    if !verify_manifest_digest(manifest)? {
        return verr("manifest digest mismatch");
    }
    let manifest_identity = [
        ("model_id", get(manifest, "model_id")?.clone()),
        ("num_layers", get(manifest, "num_layers")?.clone()),
        ("revision", get(manifest, "resolved_commit")?.clone()),
        ("weight_digest", json!(manifest_digest_ref(manifest)?)),
    ];
    let route_model = get(route_plan, "model")?;
    let snapshot_model = get(planner_snapshot, "model")?;
    if !route_model.is_object() || !snapshot_model.is_object() {
        return verr("route and planner snapshot model identities are required");
    }
    for (field, expected) in &manifest_identity {
        if !py_eq(get(route_model, field)?, expected) {
            return verr(format!("route model {field} does not match manifest"));
        }
        if !py_eq(get(snapshot_model, field)?, expected) {
            return verr(format!("planner model {field} does not match manifest"));
        }
    }
    let bundle_identity = [
        ("model_id", manifest_identity[0].1.clone()),
        ("num_layers", manifest_identity[1].1.clone()),
        ("resolved_commit", manifest_identity[2].1.clone()),
        ("manifest_digest", manifest_identity[3].1.clone()),
    ];
    for (field, expected) in &bundle_identity {
        if !py_eq(get(&bundle["model"], field)?, expected) {
            return verr(format!(
                "evidence bundle model {field} does not match manifest"
            ));
        }
    }
    if !py_eq(route_model, snapshot_model) {
        return verr("route model does not equal bound planner snapshot model");
    }
    Ok((bundle, snapshot_digest))
}

fn manual_route_model(manifest: &Value) -> PyResult<Value> {
    Ok(json!({
        "model_id": req(manifest, "model_id")?,
        "num_layers": req(manifest, "num_layers")?,
        "manifest_digest": manifest_digest_ref(manifest)?,
        "resolved_commit": req(manifest, "resolved_commit")?,
    }))
}

fn stage_json(node_id: &Value, start: i128, end: i128) -> Value {
    json!({
        "node_id": node_id,
        "range": {
            "start_layer": start as i64,
            "end_layer_exclusive": end as i64,
            "layer_count": (end - start) as i64,
        },
    })
}

/// `planner_assignment.product_route_to_manual_provisioning_route`.
pub fn product_route_to_manual_provisioning_route(
    route_plan: &Value,
    manifest: &Value,
) -> PyResult<Value> {
    let route = route_wire(route_plan)?;
    if get(&route, "protocol")? != &json!("mycelium.route_plan.v2") {
        return verr("expected mycelium.route_plan.v2");
    }
    if get(&route, "handoff_state")? != &json!("placement_intent_only") {
        return verr("product route handoff_state must remain placement_intent_only");
    }
    let tracks = get(&route, "legal_tracks")?;
    let single = tracks.as_array().filter(|t| t.len() == 1);
    let Some(track) = single.filter(|t| py_eq(&t[0]["traffic_fraction"], &json!(1.0))) else {
        return verr("MVP assignment requires exactly one legal track with traffic_fraction 1.0");
    };
    let track = &track[0];
    let placement_ids = get(track, "placement_ids")?;
    let ids_ok = placement_ids
        .as_array()
        .is_some_and(|ids| !ids.is_empty() && unique_len(ids) == ids.len());
    if !ids_ok {
        return verr("legal track placement_ids must be unique and non-empty");
    }
    let placements_raw = get(&route, "placements")?;
    let Some(placements_raw) = placements_raw.as_array().filter(|p| !p.is_empty()) else {
        return verr("route placements must be non-empty");
    };
    for placement in placements_raw {
        if !is_bool(get(placement, "primary")?, true) {
            return verr("replica placements are not supported by the MVP assignment adapter");
        }
    }
    let mut placements: BTreeMap<String, &Value> = BTreeMap::new();
    for placement in placements_raw {
        let id = get(placement, "placement_id")?;
        let Some(id) = id.as_str().filter(|s| !s.is_empty()) else {
            return verr("route placement IDs must be unique and non-empty");
        };
        if placements.insert(id.to_owned(), placement).is_some() {
            return verr("route placement IDs must be unique and non-empty");
        }
    }
    let wanted: BTreeSet<String> = placement_ids
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(str::to_owned))
        .collect();
    let have: BTreeSet<String> = placements.keys().cloned().collect();
    // `set(placement_ids)`: lists/dicts are unhashable (TypeError); null/number/bool ids hash
    // fine but can never equal a string placement key, so they fall into the ValueError below.
    let ids = placement_ids.as_array().map_or(&[][..], Vec::as_slice);
    if ids.iter().any(|v| v.is_array() || v.is_object()) {
        return Err(Failure::other("TypeError: unhashable placement id"));
    }
    if ids.iter().any(|v| !v.is_string()) || wanted != have {
        return verr(
            "MVP legal track must reference every placement exactly once; replicas or branches are unsupported",
        );
    }
    let mut expected_start: i128 = 0;
    let mut seen_nodes = BTreeSet::new();
    let mut stages = Vec::new();
    for id in placement_ids.as_array().into_iter().flatten() {
        let pid = id.as_str().unwrap_or("");
        let Some(placement) = placements.get(pid) else {
            return verr(format!("legal track references unknown placement {pid}"));
        };
        if !is_bool(get(placement, "primary")?, true) {
            return verr("every tracked placement must be primary");
        }
        let node_id = get(placement, "node_id")?;
        let Some(node) = node_id.as_str().filter(|s| !s.is_empty()) else {
            return verr("tracked placement node_id is invalid");
        };
        if !seen_nodes.insert(node.to_owned()) {
            return verr(format!("duplicate node in provisioning track: {node}"));
        }
        let layer_range = get(placement, "layer_range")?;
        if !layer_range.is_object() {
            return verr(format!("placement {pid} layer_range is invalid"));
        }
        let (Some(start), Some(end)) = (
            int(get(layer_range, "start")?),
            int(get(layer_range, "end")?),
        ) else {
            return verr("product route contains a layer gap or overlap");
        };
        if start != expected_start || end <= start {
            return verr("product route contains a layer gap or overlap");
        }
        stages.push(stage_json(node_id, start, end));
        expected_start = end;
    }
    if !py_eq(&json!(expected_start as i64), get(manifest, "num_layers")?) {
        return verr("product route contains a layer gap or overlap");
    }
    let node_order: Vec<Value> = stages.iter().map(|s| s["node_id"].clone()).collect();
    let manual = json!({
        "ok": true,
        "protocol": MANUAL_PROVISIONING_ROUTE_PROTOCOL,
        "source_protocol": "mycelium.route_plan.v2",
        "model": manual_route_model(manifest)?,
        "route": stages,
        "node_order": node_order,
        "claim_boundary": "deterministic single-track provisioning projection; runtime layers remain unloaded",
    });
    validate_manual_provisioning_route_v1(&manual)?;
    Ok(manual)
}

fn unique_len(ids: &[Value]) -> usize {
    let mut seen: Vec<&Value> = Vec::new();
    for id in ids {
        if !seen.iter().any(|s| py_eq(s, id)) {
            seen.push(id);
        }
    }
    seen.len()
}

/// Everything the bound compiler needs besides the route/snapshot/evidence documents.
pub struct BoundInputs<'a> {
    pub route_plan: &'a Value,
    pub planner_snapshot: &'a Value,
    pub evidence_bundle: &'a Value,
    pub manifest: &'a Value,
    pub deployment_id: &'a Value,
    pub deployment_epoch: &'a Value,
    pub cache_roots: &'a Value,
    pub runtime_by_node: &'a Value,
    /// Caller-owned evidence wire hard cap (L1-registered upstream; never defaulted here).
    pub limits: protocol::gossip::Limits,
}

/// `planner_assignment.compile_bound_layer_assignments`.
pub fn compile_bound_layer_assignments(inputs: &BoundInputs<'_>) -> PyResult<Vec<Value>> {
    let route = route_wire(inputs.route_plan)?;
    let (bundle, snapshot_digest) = validate_lineage(
        &route,
        inputs.planner_snapshot,
        inputs.evidence_bundle,
        inputs.manifest,
        inputs.deployment_id,
        inputs.deployment_epoch,
        inputs.limits,
    )?;
    let manual = product_route_to_manual_provisioning_route(&route, inputs.manifest)?;
    let binding = lineage_binding(
        &bundle,
        &snapshot_digest,
        inputs.deployment_id,
        inputs.deployment_epoch,
    );
    let assignments = compile_layer_assignments(
        &manual,
        inputs.manifest,
        inputs.deployment_id,
        inputs.deployment_epoch,
        inputs.cache_roots,
        inputs.runtime_by_node,
        Some(&binding),
    )?;
    for assignment in &assignments {
        if !py_eq(get(assignment, "control_plane_binding")?, &binding) {
            return verr("assignment lost control-plane lineage");
        }
        validate_assignment_identity(assignment)?;
    }
    Ok(assignments)
}

fn replica_track_to_manual(
    route_plan: &Value,
    manifest: &Value,
    track: &Value,
) -> PyResult<(Value, Vec<String>)> {
    let ids = get(track, "placement_ids")?;
    let ids_ok = ids
        .as_array()
        .is_some_and(|a| !a.is_empty() && unique_len(a) == a.len());
    if !ids_ok {
        return verr("replica track placement_ids must be unique and non-empty");
    }
    let raw = get(route_plan, "placements")?;
    let Some(raw) = raw.as_array().filter(|p| !p.is_empty()) else {
        return verr("route placements must be non-empty");
    };
    let mut placements: BTreeMap<String, &Value> = BTreeMap::new();
    for item in raw {
        if let Some(id) = item.get("placement_id").and_then(Value::as_str) {
            placements.insert(id.to_owned(), item);
        }
    }
    if placements.len() != raw.len() {
        return verr("route placement IDs must be unique and non-empty");
    }
    let mut expected_start: i128 = 0;
    let mut seen_groups = BTreeSet::new();
    let mut seen_nodes = BTreeSet::new();
    let mut stages = Vec::new();
    let mut order = Vec::new();
    for id in ids.as_array().into_iter().flatten() {
        // Only string ids exist in the placement map: any other hashable id (null, bool,
        // number) is "unknown", reported with Python's `str(value)` (`repr` for non-strings).
        // Unhashable ids (list/object) never reach here: the uniqueness check raises first.
        let Some(pid) = id.as_str() else {
            return verr(format!(
                "replica track references unknown placement {}",
                py_repr(id)
            ));
        };
        let Some(placement) = placements.get(pid) else {
            return verr(format!("replica track references unknown placement {pid}"));
        };
        let group = get(placement, "replica_group_id")?;
        let node = get(placement, "node_id")?;
        let layer_range = get(placement, "layer_range")?;
        let Some(group_s) = group.as_str().filter(|s| !s.is_empty()) else {
            return verr("replica track must select one placement per group");
        };
        if !seen_groups.insert(group_s.to_owned()) {
            return verr("replica track must select one placement per group");
        }
        let Some(node_s) = node.as_str().filter(|s| !s.is_empty()) else {
            return verr("replica track must use one placement per node");
        };
        if !seen_nodes.insert(node_s.to_owned()) {
            return verr("replica track must use one placement per node");
        }
        if !layer_range.is_object() {
            return verr("replica track layer range is invalid");
        }
        let (Some(start), Some(end)) = (
            int(get(layer_range, "start")?),
            int(get(layer_range, "end")?),
        ) else {
            return verr("replica track contains a layer gap or overlap");
        };
        if start != expected_start || end <= start {
            return verr("replica track contains a layer gap or overlap");
        }
        stages.push(stage_json(node, start, end));
        order.push(pid.to_owned());
        expected_start = end;
    }
    if !py_eq(&json!(expected_start as i64), get(manifest, "num_layers")?) {
        return verr("replica track contains a layer gap or overlap");
    }
    let node_order: Vec<Value> = stages.iter().map(|s| s["node_id"].clone()).collect();
    let manual = json!({
        "ok": true,
        "protocol": MANUAL_PROVISIONING_ROUTE_PROTOCOL,
        "source_protocol": "mycelium.route_plan.v2",
        "model": manual_route_model(manifest)?,
        "route": stages,
        "node_order": node_order,
        "claim_boundary": "one complete replica track projected for assignment-local provisioning; runtime qualification remains separate",
    });
    validate_manual_provisioning_route_v1(&manual)?;
    Ok((manual, order))
}

/// `planner_assignment.compile_bound_replica_assignments`: placement id → assignment.
pub fn compile_bound_replica_assignments(
    inputs: &BoundInputs<'_>,
) -> PyResult<BTreeMap<String, Value>> {
    let route = route_wire(inputs.route_plan)?;
    let (bundle, snapshot_digest) = validate_lineage(
        &route,
        inputs.planner_snapshot,
        inputs.evidence_bundle,
        inputs.manifest,
        inputs.deployment_id,
        inputs.deployment_epoch,
        inputs.limits,
    )?;
    let tracks = get(&route, "legal_tracks")?;
    let Some(tracks) = tracks.as_array().filter(|t| t.len() >= 2) else {
        return verr("replica assignment requires two or more legal tracks");
    };
    let binding = lineage_binding(
        &bundle,
        &snapshot_digest,
        inputs.deployment_id,
        inputs.deployment_epoch,
    );
    let mut by_placement: BTreeMap<String, Value> = BTreeMap::new();
    for track in tracks {
        // `isinstance(x, (int, float))` accepts bool (an int subclass): True compares as 1.
        let fraction = match get(track, "traffic_fraction").unwrap_or(&Value::Null) {
            Value::Bool(b) => Some(f64::from(u8::from(*b))),
            other => crate::py::float(other),
        };
        if !track.is_object() || fraction.is_none_or(|f| f <= 0.0) {
            return verr("replica assignment track is invalid");
        }
        let (manual, ids) = replica_track_to_manual(&route, inputs.manifest, track)?;
        let assignments = compile_layer_assignments(
            &manual,
            inputs.manifest,
            inputs.deployment_id,
            inputs.deployment_epoch,
            inputs.cache_roots,
            inputs.runtime_by_node,
            Some(&binding),
        )?;
        if assignments.len() != ids.len() {
            return Err(Failure::other("ValueError: zip() argument lengths differ"));
        }
        for (pid, assignment) in ids.into_iter().zip(assignments) {
            if let Some(existing) = by_placement.get(&pid)
                && !py_eq(existing, &assignment)
            {
                return verr("shared replica placement compiled inconsistently");
            }
            by_placement.insert(pid, assignment);
        }
    }
    let planned: BTreeSet<String> = get(&route, "placements")?
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|p| {
            p.get("placement_id")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .collect();
    let compiled: BTreeSet<String> = by_placement.keys().cloned().collect();
    if compiled != planned {
        return verr("replica plan contains an unprovisioned zero-flow placement");
    }
    let mut groups: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for item in req(&route, "placements")?.as_array().into_iter().flatten() {
        groups
            .entry(str_of(req(item, "replica_group_id")?))
            .or_default()
            .push(item);
    }
    for (group_id, members) in &groups {
        let mut ranges: Vec<(Value, Value)> = Vec::new();
        let mut components: Vec<Value> = Vec::new();
        for item in members {
            let range = (
                req(&item["layer_range"], "start")?.clone(),
                req(&item["layer_range"], "end")?.clone(),
            );
            if !ranges
                .iter()
                .any(|r| py_eq(&r.0, &range.0) && py_eq(&r.1, &range.1))
            {
                ranges.push(range);
            }
            let pid = str_of(req(item, "placement_id")?);
            let comp = by_placement
                .get(&pid)
                .ok_or_else(|| Failure::other(format!("KeyError: {pid}")))?["components"]
                .clone();
            if !components.iter().any(|c| py_eq(c, &comp)) {
                components.push(comp);
            }
        }
        if ranges.len() != 1 || components.len() != 1 {
            return verr(format!("replica group ownership mismatch: {group_id}"));
        }
    }
    for assignment in by_placement.values() {
        validate_assignment_identity(assignment)?;
    }
    Ok(by_placement)
}

/// `planner_assignment.validate_control_plane_tranche`.
pub fn validate_control_plane_tranche(
    document: &Value,
    manifest: &Value,
    limits: protocol::gossip::Limits,
) -> PyResult<()> {
    if get(document, "protocol")? != &json!("mycelium.control_plane_tranche.v1") {
        return verr("control-plane tranche protocol mismatch");
    }
    let evidence_bundle = get(document, "evidence_bundle")?;
    let planner_snapshot = get(document, "planner_snapshot")?;
    let route_plan = get(document, "route_plan")?;
    let assignments = get(document, "assignments")?;
    if !evidence_bundle.is_object() {
        return verr("control-plane tranche evidence_bundle is required");
    }
    if !planner_snapshot.is_object() {
        return verr("control-plane tranche planner_snapshot is required");
    }
    if !route_plan.is_object() {
        return verr("control-plane tranche route_plan is required");
    }
    let Some(list) = assignments.as_array().filter(|a| !a.is_empty()) else {
        return verr("control-plane tranche assignments must be non-empty");
    };
    if list.iter().any(|a| !a.is_object()) {
        return verr("control-plane tranche assignments must be objects");
    }
    let first = &list[0];
    let deployment_id = get(first, "deployment_id")?;
    let deployment_epoch = get(first, "deployment_epoch")?;
    let mut cache_roots = Map::new();
    let mut runtime_by_node = Map::new();
    for assignment in list {
        if !py_eq(get(assignment, "deployment_id")?, deployment_id) {
            return verr("control-plane tranche mixes assignment deployment IDs");
        }
        if !py_eq(get(assignment, "deployment_epoch")?, deployment_epoch) {
            return verr("control-plane tranche mixes assignment deployment epochs");
        }
        let node_id = get(assignment, "node_id")?;
        let Some(node) = node_id.as_str().filter(|s| !s.is_empty()) else {
            return verr("control-plane tranche assignment node IDs must be unique");
        };
        if cache_roots.contains_key(node) {
            return verr("control-plane tranche assignment node IDs must be unique");
        }
        let cache_root = get(assignment, "artifact_cache_root")?;
        let runtime = get(assignment, "runtime")?;
        if !cache_root.is_string() || !runtime.is_object() {
            return verr("control-plane tranche assignment target identity is invalid");
        }
        cache_roots.insert(node.to_owned(), cache_root.clone());
        let has_all = MLX_RUNTIME_BASE_FIELDS.iter().all(|f| has(runtime, f));
        if !has_all {
            return verr("control-plane tranche assignment runtime identity is invalid");
        }
        let mut base = Map::new();
        for field in MLX_RUNTIME_BASE_FIELDS {
            base.insert(field.to_owned(), runtime[field].clone());
        }
        runtime_by_node.insert(node.to_owned(), Value::Object(base));
    }
    let cache_roots = Value::Object(cache_roots);
    let runtime_by_node = Value::Object(runtime_by_node);
    let expected = compile_bound_layer_assignments(&BoundInputs {
        route_plan,
        planner_snapshot,
        evidence_bundle,
        manifest,
        deployment_id,
        deployment_epoch,
        cache_roots: &cache_roots,
        runtime_by_node: &runtime_by_node,
        limits,
    })?;
    if !py_eq(assignments, &Value::Array(expected)) {
        return verr("control-plane tranche assignments do not match bound compiler output");
    }
    Ok(())
}

/// True when a failure is a deliberate oracle refusal (`ValueError`).
pub fn is_refusal(failure: &Failure) -> bool {
    failure.kind == Kind::Value
}

/// Debug helper for vectors: Python repr of a JSON value.
pub fn repr(v: &Value) -> String {
    py_repr(v)
}
