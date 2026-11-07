// SPDX-License-Identifier: AGPL-3.0-only
//! Legacy graph metadata, never proof of load, consent, or route readiness.
use crate::{Error, Result, compile::assignment_id, digest};
use serde_json::{Value, json};
use stage_contract::{Architecture, NumericsProfile, StageDescriptor};
use std::collections::BTreeSet;
/// Compatibility costs are explicitly injected oracle placeholders, not measured throughput.
pub struct LegacyGraphCosts {
    pub prefill_work_units: f64,
    pub decode_work_units: f64,
    pub kv_bytes_per_token: u64,
}
/// Serialized placement ACTIVE is legacy metadata only. Promotion requires L5/L6
/// authenticated current load proofs, route challenge, and current consent.
pub struct GraphCandidate {
    metadata: Value,
}
impl GraphCandidate {
    pub fn metadata(&self) -> &Value {
        &self.metadata
    }
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty() && s.trim() == *s)
        .ok_or_else(|| Error::new("execution_graph_inputs_invalid"))
}
fn number(v: &Value, key: &str) -> Result<u64> {
    v[key]
        .as_u64()
        .ok_or_else(|| Error::new("execution_graph_inputs_invalid"))
}
fn scheme(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
}
fn activation_bytes(a: &Value) -> Result<u64> {
    match a["runtime"]["dtype"].as_str() {
        Some("float32") => Ok(4),
        // v0.1.4 `_RUNTIME_DTYPE_BYTES`: float16, bfloat16, float32.
        Some("float16" | "bfloat16") => Ok(2),
        _ => Err(Error::new("unsupported_execution_graph_runtime_dtype")),
    }
}
pub fn compile_graph(
    assignments: &[Value],
    proofs: &[Value],
    link: &str,
    runtime: &str,
    costs: LegacyGraphCosts,
) -> Result<GraphCandidate> {
    if assignments.len() < 2 || assignments.len() != proofs.len() {
        return Err(Error::new("execution_graph_requires_matching_assignments"));
    }
    if !scheme(link) || !scheme(runtime) {
        return Err(Error::new("invalid_execution_graph_scheme"));
    }
    if !costs.prefill_work_units.is_finite()
        || !costs.decode_work_units.is_finite()
        || costs.prefill_work_units <= 0.0
        || costs.decode_work_units <= 0.0
        || costs.kv_bytes_per_token == 0
    {
        return Err(Error::new("execution_graph_cost_invalid"));
    }
    let first = &assignments[0];
    let hidden = number(&first["runtime"]["model_config"], "n_embd")?;
    let total = number(&first["runtime"]["model_config"], "n_layer")?;
    let width = activation_bytes(first)?;
    let mut stages = Vec::new();
    let mut nodes = BTreeSet::new();
    let mut cursor = 0;
    for (i, (a, p)) in assignments.iter().zip(proofs).enumerate() {
        if a["protocol"] != "mycelium.layer_assignment.v2"
            || a["assignment_id"] != assignment_id(a)?
        {
            return Err(Error::new("execution_graph_assignment_identity"));
        }
        for field in [
            "deployment_id",
            "deployment_epoch",
            "model_id",
            "resolved_commit",
            "manifest_digest",
        ] {
            if a[field] != first[field] || a[field] != p[field] {
                return Err(Error::new("execution_graph_proof_binding"));
            }
        }
        for (assignment, proof) in [
            ("assignment_id", "assignment_id"),
            ("node_id", "node_id"),
            ("range", "loaded_range"),
            ("components", "loaded_components"),
            ("expected_tensor_keys", "loaded_tensor_keys"),
            ("component_aliases", "resolved_component_aliases"),
            ("runtime", "runtime"),
        ] {
            if a[assignment] != p[proof] {
                return Err(Error::new("execution_graph_proof_binding"));
            }
        }
        // v0.1.4 accepts any non-negative integer generation (`--load-generation 0` is a valid
        // operator input; `_validate_load_proof` and `runtime_contracts` refuse only `< 0`).
        if p["protocol"] != "mycelium.layer_load_proof.v1" || number(p, "load_generation").is_err()
        {
            return Err(Error::new("execution_graph_proof_binding"));
        }
        let node = text(a, "node_id")?;
        if !nodes.insert(node) || node.contains(['/', ':', '?', '#']) {
            return Err(Error::new("execution_graph_node_invalid"));
        }
        // v0.1.4 compares the dtype STRINGS (float16 + bfloat16 is a mismatch although both are
        // two bytes wide), then maps the one dtype to its width.
        if activation_bytes(a)? != width
            || a["runtime"]["dtype"] != first["runtime"]["dtype"]
            || a["runtime"]["model_config"] != first["runtime"]["model_config"]
        {
            return Err(Error::new("execution_graph_runtime_dtype_mismatch"));
        }
        let range = &a["range"];
        let start = number(range, "start_layer")?;
        let end = number(range, "end_layer_exclusive")?;
        if start != cursor
            || end <= start
            || end > total
            || number(range, "layer_count")? != end - start
        {
            return Err(Error::new("execution_graph_range_invalid"));
        }
        cursor = end;
        let components = a["components"]
            .as_array()
            .ok_or_else(|| Error::new("execution_graph_inputs_invalid"))?;
        if !components.contains(&json!("decoder"))
            || components.contains(&json!("input_embedding")) != (i == 0)
            || components.contains(&json!("lm_head")) != (i + 1 == assignments.len())
            || components.contains(&json!("final_norm")) != (i + 1 == assignments.len())
        {
            return Err(Error::new("execution_graph_components_invalid"));
        }
        let stage_id = format!("stage-{i:03}");
        let placement_id = format!("placement-{i:03}");
        let backend = text(&a["runtime"], "backend")?;
        let signature = digest(
            &json!({"protocol":"mycelium.stage_signature.v2","deployment_id":a["deployment_id"],"deployment_epoch":a["deployment_epoch"],"model_id":a["model_id"],"resolved_commit":a["resolved_commit"],"manifest_digest":a["manifest_digest"],"stage_id":stage_id,"range":range,"components":components,"runtime_backend":backend,"hidden_size":hidden,"dtype_bytes":width}),
            false,
        )?;
        let assignment = text(a, "assignment_id")?;
        stages.push(json!({"stage_id":stage_id,"range":range,"component_roles":components,"stage_cost":{"prefill_work_units_per_prompt_token":costs.prefill_work_units,"decode_work_units_per_token":costs.decode_work_units,"kv_bytes_per_context_token":costs.kv_bytes_per_token},"placements":[{"placement_id":placement_id,"node_id":node,"replica_group_id":format!("{stage_id}-replicas"),"assignment_id":assignment,"stage_signature":signature,"load_proof_digest":digest(p,false)?,"runtime_backend":backend,"runtime_endpoint":format!("{runtime}://{node}/{assignment}"),"lifecycle_state":"ACTIVE"}]}));
    }
    if cursor != total {
        return Err(Error::new("execution_graph_range_invalid"));
    }
    let edge = |from: usize, to: usize, kind: &str| -> Result<Value> {
        let fp = format!("placement-{from:03}");
        let tp = format!("placement-{to:03}");
        Ok(
            json!({"edge_id":format!("{kind}:{fp}->{tp}"),"from_placement_id":fp,"to_placement_id":tp,"link_id":format!("{link}:{}->{}",text(&assignments[from],"node_id")?,text(&assignments[to],"node_id")?)}),
        )
    };
    let edges: Result<Vec<_>> = (0..assignments.len() - 1)
        .map(|i| edge(i, i + 1, "forward"))
        .collect();
    let metadata = json!({"protocol":"mycelium.execution_graph.v1","deployment_id":first["deployment_id"],"deployment_epoch":first["deployment_epoch"],"topology_version":1,"model_id":first["model_id"],"resolved_commit":first["resolved_commit"],"manifest_digest":first["manifest_digest"],"entry_stage_id":"stage-000","final_stage_id":format!("stage-{:03}",assignments.len()-1),"hidden_size":hidden,"activation_bytes":width,"token_envelope_bytes":9,"stages":stages,"edges":edges?,"loopback_edges":[edge(assignments.len()-1,0,"loopback")?]});
    protocol::execution_graph::parse(&metadata).map_err(|e| Error::new(e.0))?;
    Ok(GraphCandidate { metadata })
}
pub fn fast_mode(operator: Option<&str>, app_default: Option<&str>) -> Result<bool> {
    match operator.or(app_default).unwrap_or("off") {
        "on" => Ok(true),
        "off" => Ok(false),
        _ => Err(Error::new("token_path_activation_invalid")),
    }
}
pub fn require_fast_owner(enabled: bool, provisioned: bool) -> Result<()> {
    if enabled && !provisioned {
        Err(Error::new("token_path_activation_unprovisioned"))
    } else {
        Ok(())
    }
}
/// Bind roles/range without inventing an artifact or promoting numerics. The
/// returned descriptor is still subject to L8's pinned backend admission.
pub fn bind_stage(template: &StageDescriptor, a: &Value) -> Result<StageDescriptor> {
    if a["assignment_id"] != assignment_id(a)? || a["resolved_commit"] != template.revision {
        return Err(Error::new("stage_assignment_binding"));
    }
    let expected_arch = match a["runtime"]["architecture"].as_str() {
        Some("qwen2") => Architecture::Qwen2,
        Some("qwen3") => Architecture::Qwen3,
        Some("qwen3_5") => Architecture::Qwen35DenseHybrid,
        _ => return Err(Error::new("stage_architecture_unsupported")),
    };
    let expected_profile = match (
        a["runtime"]["backend"].as_str(),
        a["runtime"]["dtype"].as_str(),
        a["runtime"]["quantization"].as_str(),
    ) {
        (Some("mlx"), Some("float32"), Some("none")) => NumericsProfile::MlxExplicitF32,
        (Some("mlx"), Some("float32"), Some("int8-weight-only")) => NumericsProfile::MlxPackedInt8,
        (Some("cpu"), Some("float32"), Some("int8-weight-only")) => NumericsProfile::CpuInt8F32,
        _ => return Err(Error::new("stage_numerics_unsupported")),
    };
    if template.architecture != expected_arch || template.profile != expected_profile {
        return Err(Error::new("stage_numerics_binding"));
    }
    let n = |v: &Value, k: &str| {
        u32::try_from(number(v, k)?).map_err(|_| Error::new("stage_range_invalid"))
    };
    let mut d = template.clone();
    d.layers = n(&a["range"], "start_layer")?..n(&a["range"], "end_layer_exclusive")?;
    let components = a["components"]
        .as_array()
        .ok_or_else(|| Error::new("stage_assignment_binding"))?;
    d.embedding = components.contains(&json!("input_embedding"));
    d.head = components.contains(&json!("lm_head"));
    d.validate(n(&a["runtime"]["model_config"], "n_layer")?)
        .map_err(|_| Error::new("stage_range_invalid"))?;
    Ok(d)
}
