// SPDX-License-Identifier: AGPL-3.0-only
//! Pure evidence-bound planner input projection. No planner execution, runtime
//! authority, registry or network IO. Signed capacity admission remains separate.
use crate::{ContractError, canonical, checks::*, execution_graph::numeric_cmp, gossip};
use serde_json::{Value, json};
use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
};
const C: &str = "planner_snapshot_invalid";
fn array(v: &Value) -> Result<&Vec<Value>, ContractError> {
    v.as_array().ok_or(ContractError(C))
}
fn mapping(v: &Value) -> Result<Value, ContractError> {
    if v.is_object() {
        return Ok(v.clone());
    }
    if v.as_str() == Some("") {
        return Ok(json!({}));
    }
    let mut out = json!({});
    for pair in array(v)? {
        let p = array(pair)?;
        require(p.len() == 2, C)?;
        out[p[0].as_str().ok_or(ContractError(C))?] = p[1].clone();
    }
    Ok(out)
}
fn measure(v: &Value, positive: bool) -> Result<f64, ContractError> {
    let n = number(v, C)?;
    require(n >= 0.0 && (!positive || n > 0.0), C)?;
    Ok(n)
}
/// Rebuild the complete unsigned snapshot, preserving the pinned source's
/// calibrated float representation and all independent model/workload inputs.
pub fn derive(
    bundle: &Value,
    model: &Value,
    workload: &Value,
    policy: &Value,
    limits: gossip::Limits,
) -> Result<Value, ContractError> {
    let bundle = gossip::parse_bundle(bundle, limits)?;
    let model = mapping(model)?;
    let policy = mapping(policy)?;
    for (field, source) in [
        ("model_id", "model_id"),
        ("num_layers", "num_layers"),
        ("revision", "resolved_commit"),
        ("weight_digest", "manifest_digest"),
    ] {
        require(python_equal(&model[field], &bundle["model"][source]), C)?;
    }
    require(
        python_equal(&policy["memory_reserve_fraction"], &json!(0)),
        C,
    )?;
    let mut statuses = BTreeMap::new();
    let mut memberships = BTreeMap::new();
    for r in array(&bundle["records"])? {
        let id = r["origin_node_id"].as_str().ok_or(ContractError(C))?;
        if r["kind"] == "status" {
            require(statuses.insert(id, &r["payload"]).is_none(), C)?;
        }
        if r["kind"] == "membership" && r["payload"]["subject_node_id"] == r["origin_node_id"] {
            require(memberships.insert(id, r).is_none(), C)?;
        }
    }
    let mut nodes = Vec::new();
    let mut runtimes = json!({});
    let mut mode = None;
    let mut ids = BTreeSet::new();
    let mut eligible: Vec<&Value> = array(&bundle["allocator_view"]["nodes"])?
        .iter()
        .filter(|n| n["eligible"] == true)
        .collect();
    eligible.sort_by_key(|n| n["node_id"].as_str());
    for n in eligible {
        let id = n["node_id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or(ContractError(C))?;
        let status = statuses.get(id).ok_or(ContractError(C))?;
        let membership = memberships.get(id).ok_or(ContractError(C))?;
        require(
            membership["payload"]["state"] == "alive"
                && membership["payload"]["reporter_node_id"] == id
                && python_equal(
                    &membership["payload"]["subject_incarnation"],
                    &n["incarnation"],
                )
                && python_equal(&membership["incarnation"], &n["incarnation"]),
            C,
        )?;
        let p = &status["performance"];
        require(p.is_object(), C)?;
        let mut node = json!({"node_id":id,"fast_memory_bytes":n["fast_allocatable_bytes"],"total_memory_bytes":n["total_allocatable_bytes"]});
        for (f, positive) in [
            ("prefill_ms_per_layer_token", false),
            ("decode_ms_per_layer_token", false),
            ("memory_bandwidth_Bps", true),
            ("spill_bandwidth_Bps", true),
        ] {
            node[f] = json!(measure(&p[f], positive)?);
        }
        let decode = p["decode_mode"].as_str().ok_or(ContractError(C))?;
        let backend = p["backend"].as_str().ok_or(ContractError(C))?;
        require(
            match backend {
                "mlx" => ["stage_local_kv", "complete_context_replay"].contains(&decode),
                "numpy" | "pixel-stdlib" | "mobile-rust-qwen2" => {
                    decode == "complete_context_replay"
                }
                _ => false,
            },
            C,
        )?;
        let confidence = measure(&p["calibration_confidence"], false)?;
        require(confidence <= 1.0, C)?;
        node["calibration_confidence"] = json!(confidence);
        require(
            integer(&n["total_allocatable_bytes"], 1)
                && integer(&n["fast_allocatable_bytes"], 1)
                && numeric_cmp(&n["fast_allocatable_bytes"], &n["total_allocatable_bytes"])
                    .is_some_and(|o| o != Ordering::Greater),
            C,
        )?;
        require(mode.is_none_or(|m| m == decode), C)?;
        mode = Some(decode);
        runtimes[id] = json!({"backend":backend,"decode_mode":decode});
        ids.insert(id);
        nodes.push(node);
    }
    let decode = mode.ok_or(ContractError(C))?;
    let mut links = BTreeMap::new();
    for r in array(&bundle["records"])? {
        if r["kind"] != "link" {
            continue;
        }
        let p = &r["payload"];
        let src = p["src_node_id"].as_str().ok_or(ContractError(C))?;
        let dst = p["dst_node_id"].as_str().ok_or(ContractError(C))?;
        if !ids.contains(src) || !ids.contains(dst) {
            continue;
        }
        require(
            !links.contains_key(&(src, dst)) && p["reachable"] == true,
            C,
        )?;
        let rtt = if p["rtt_p95_ms"].is_null() {
            &p["connect_rtt_ema_ms"]
        } else {
            &p["rtt_p95_ms"]
        };
        let rtt = measure(rtt, false)?;
        let jitter = measure(&p["jitter_ms"], false)?;
        let loss = measure(&p["loss_ratio"], false)?;
        require(loss <= 1.0, C)?;
        // Wire-unit conversion, not an operational size/time budget.
        let bandwidth = measure(&p["goodput_mbps"], true)? * 1_000_000.0 / 8.0;
        require(bandwidth.is_finite(), C)?;
        links.insert((src,dst),json!({"src":src,"dst":dst,"rtt_ms":rtt,"jitter_ms":jitter,"bandwidth_Bps":bandwidth,"loss_ratio":loss,"inferred":false,"stale":false}));
    }
    for src in &ids {
        for dst in &ids {
            require(src == dst || links.contains_key(&(*src, *dst)), C)?;
        }
    }
    let out = json!({"protocol":"mycelium.layer_planner_snapshot.v1","swarm_id":bundle["swarm_id"],"deployment":bundle["deployment"],"snapshot_generation":bundle["snapshot_generation"],"evidence_bundle_digest":bundle["evidence_bundle_digest"],"model":model,"nodes":nodes,"links":links.into_values().collect::<Vec<_>>(),"workload":mapping(workload)?,"policy":policy,"placement_provenance":"planner_v2","decode_mode":decode,"node_runtime":runtimes,"capacity_profile_bindings":{}});
    canonical::to_bytes(&out).map_err(|_| ContractError(C))?;
    Ok(out)
}
/// Independent source evidence is mandatory; a self-asserted snapshot hash is
/// not a substitute. Like the source, this compares the whole rebuilt document.
pub fn validate_binding(
    snapshot: &Value,
    bundle: &Value,
    limits: gossip::Limits,
) -> Result<(), ContractError> {
    require(
        ["model", "workload", "policy"]
            .iter()
            .all(|f| snapshot[*f].is_object()),
        C,
    )?;
    let expected = derive(
        bundle,
        &snapshot["model"],
        &snapshot["workload"],
        &snapshot["policy"],
        limits,
    )?;
    require(python_equal(snapshot, &expected), C)
}
