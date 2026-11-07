// SPDX-License-Identifier: AGPL-3.0-only
use super::{C, add_int, repr, schema::array};
use crate::{ContractError, checks::python_equal};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
fn version(r: &Value) -> Value {
    if r.is_null() {
        Value::Null
    } else {
        json!({"incarnation":r["incarnation"],"sequence":r["sequence"],"payload_hash":r["payload_hash"]})
    }
}
fn identity(r: &Value, p: &Value) -> bool {
    r.is_null()
        || (python_equal(&r["incarnation"], &p["incarnation"]) && r["boot_id"] == p["boot_id"])
}
fn project(v: &Value, fields: &str) -> Value {
    let mut out = json!({});
    for f in fields.split_whitespace() {
        out[f] = v[f].clone();
    }
    out
}
fn sorted(mut values: Vec<Value>, fields: &[&str]) -> Vec<Value> {
    values.sort_by(|a, b| {
        fields
            .iter()
            .map(|f| a[*f].as_str())
            .cmp(fields.iter().map(|f| b[*f].as_str()))
    });
    values
}
pub(super) fn derive(input: &Value) -> Result<(Value, Value), ContractError> {
    let mut profiles = BTreeMap::new();
    let mut statuses = BTreeMap::new();
    let mut offerings: BTreeMap<&str, Vec<&Value>> = BTreeMap::new();
    let mut links = Vec::new();
    let mut nodes = BTreeSet::new();
    for r in array(&input["records"])? {
        let origin = r["origin_node_id"].as_str().ok_or(ContractError(C))?;
        nodes.insert(origin);
        match r["kind"].as_str() {
            Some("profile") => {
                profiles.insert(origin, r);
            }
            Some("status") => {
                statuses.insert(origin, r);
            }
            Some("offering") => offerings.entry(origin).or_default().push(r),
            Some("link") => {
                nodes.insert(
                    r["payload"]["dst_node_id"]
                        .as_str()
                        .ok_or(ContractError(C))?,
                );
                links.push(r);
            }
            _ => {}
        }
    }
    let mut peers = BTreeMap::new();
    for p in array(&input["peer_states"])? {
        let id = p["node_id"].as_str().ok_or(ContractError(C))?;
        peers.insert(id, p);
        nodes.insert(id);
    }
    let mut qp = BTreeSet::new();
    let mut qo = BTreeSet::new();
    let mut qe = BTreeSet::new();
    for q in array(&input["quarantines"])? {
        let key = array(&q["key"])?;
        match q["scope"].as_str() {
            Some("peer") => {
                qp.insert(
                    key.get(1)
                        .ok_or(ContractError(C))?
                        .as_str()
                        .ok_or(ContractError(C))?,
                );
            }
            Some("offering") => {
                qo.insert((
                    key.get(1)
                        .ok_or(ContractError(C))?
                        .as_str()
                        .ok_or(ContractError(C))?,
                    key.get(2)
                        .ok_or(ContractError(C))?
                        .as_str()
                        .ok_or(ContractError(C))?,
                ));
            }
            Some("edge") => {
                qe.insert(
                    key.iter()
                        .skip(1)
                        .map(|x| x.as_str().ok_or(ContractError(C)))
                        .collect::<Result<Vec<_>, _>>()?,
                );
            }
            _ => return Err(ContractError(C)),
        }
    }
    let mut router_nodes = Vec::new();
    let mut allocator_nodes = Vec::new();
    for id in nodes {
        let profile = profiles.get(id).copied().unwrap_or(&Value::Null);
        let status = statuses.get(id).copied().unwrap_or(&Value::Null);
        let peer = peers.get(id).copied().unwrap_or(&Value::Null);
        let mut reasons = Vec::new();
        if profile.is_null() {
            reasons.push("profile_missing");
        }
        if status.is_null() {
            reasons.push("status_missing");
        }
        if peer["state"] != "alive" {
            reasons.push("peer_not_alive");
        }
        if !peer.is_null() && (!identity(profile, peer) || !identity(status, peer)) {
            reasons.push("identity_mismatch");
        }
        if !status.is_null() && status["payload"]["lifecycle"] != "ready" {
            reasons.push("status_not_ready");
        }
        let mut allocator_reasons = reasons.clone();
        if !status.is_null() && status["payload"]["concurrency_limit"] == 0 {
            allocator_reasons.push("no_concurrency_capacity");
        }
        if qp.contains(id) {
            reasons.push("peer_quarantined");
            allocator_reasons.push("peer_quarantined");
        }
        let endpoints = if profile.is_null() {
            Vec::new()
        } else {
            sorted(
                array(&profile["payload"]["endpoints"])?
                    .iter()
                    .map(|e| project(e, "endpoint_id transport host port scope inbound"))
                    .collect(),
                &["endpoint_id"],
            )
        };
        let endpoint_ids: BTreeSet<_> = endpoints
            .iter()
            .filter_map(|e| e["endpoint_id"].as_str())
            .collect();
        let mut ready = Vec::new();
        if let Some(entries) = offerings.get(id) {
            for r in entries {
                let p = &r["payload"];
                if peer.is_null()
                    || !identity(r, peer)
                    || p["readiness_state"] != "loaded_and_probed"
                    || !endpoint_ids.contains(
                        p["inference_endpoint_id"]
                            .as_str()
                            .ok_or(ContractError(C))?,
                    )
                    || qo.contains(&(id, p["assignment_id"].as_str().ok_or(ContractError(C))?))
                {
                    continue;
                }
                let mut out = project(
                    p,
                    "deployment_id deployment_epoch assignment_id manifest_digest resolved_commit model_id start_layer end_layer_exclusive runtime_instance_id load_generation proof_digest inference_endpoint_id",
                );
                out["version"] = version(r);
                ready.push(out);
            }
        }
        ready = sorted(
            ready,
            &["deployment_id", "assignment_id", "inference_endpoint_id"],
        );
        if ready.is_empty() {
            reasons.push("no_ready_offering");
        }
        let common = json!({"node_id":id,"incarnation":peer["incarnation"],"boot_id":peer["boot_id"],"peer_state":peer["state"],"profile_version":version(profile),"status_version":version(status),"queue_depth":status["payload"]["queue_depth"],"in_flight":status["payload"]["in_flight"],"concurrency_limit":status["payload"]["concurrency_limit"]});
        let mut node = common.clone();
        node["eligible"] = reasons.is_empty().into();
        node["exclusion_reasons"] = json!(reasons);
        node["endpoints"] = json!(endpoints);
        node["offerings"] = json!(ready);
        router_nodes.push(node);
        let mut domains = Vec::new();
        let mut total = json!(0);
        let mut fast = json!(0);
        if !status.is_null() {
            for d in array(&status["payload"]["memory_domains"])? {
                let mut domain = project(d, "memory_domain_id kind total_bytes");
                for f in [
                    "allocatable_after_reservations_bytes",
                    "committed_bytes",
                    "reclaimable_bytes",
                    "reservation_generation",
                ] {
                    domain[f] = d.get(f).cloned().unwrap_or(json!(0));
                }
                total = add_int(&total, &domain["allocatable_after_reservations_bytes"])?;
                if ["unified", "vram", "accelerator"]
                    .contains(&d["kind"].as_str().ok_or(ContractError(C))?)
                {
                    fast = add_int(&fast, &domain["allocatable_after_reservations_bytes"])?;
                }
                domains.push(domain);
            }
        }
        if fast == 0 {
            fast = total.clone();
        }
        if !status.is_null() && total == 0 {
            allocator_reasons.push("no_allocatable_memory");
        }
        let mut alloc = common;
        alloc["eligible"] = allocator_reasons.is_empty().into();
        alloc["exclusion_reasons"] = json!(allocator_reasons);
        alloc["memory_domains"] = json!(sorted(domains, &["memory_domain_id"]));
        alloc["fast_allocatable_bytes"] = fast;
        alloc["total_allocatable_bytes"] = total;
        allocator_nodes.push(alloc);
    }
    let by_id: BTreeMap<_, _> = router_nodes
        .iter()
        .map(|n| (n["node_id"].as_str().expect("validated node"), n))
        .collect();
    links.sort_by(|a, b| {
        [
            a["origin_node_id"].as_str(),
            a["payload"]["src_endpoint_id"].as_str(),
            a["payload"]["dst_node_id"].as_str(),
            a["payload"]["dst_endpoint_id"].as_str(),
        ]
        .cmp(&[
            b["origin_node_id"].as_str(),
            b["payload"]["src_endpoint_id"].as_str(),
            b["payload"]["dst_node_id"].as_str(),
            b["payload"]["dst_endpoint_id"].as_str(),
        ])
    });
    let mut edges = Vec::new();
    for r in links {
        let p = &r["payload"];
        let src = r["origin_node_id"].as_str().ok_or(ContractError(C))?;
        let dst = p["dst_node_id"].as_str().ok_or(ContractError(C))?;
        let se = p["src_endpoint_id"].as_str().ok_or(ContractError(C))?;
        let de = p["dst_endpoint_id"].as_str().ok_or(ContractError(C))?;
        let mut reasons = Vec::new();
        if p["reachable"] == false {
            reasons.push("unreachable");
        }
        if by_id.get(src).is_none_or(|n| n["eligible"] != true) {
            reasons.push("source_ineligible");
        }
        if by_id.get(dst).is_none_or(|n| n["eligible"] != true) {
            reasons.push("destination_ineligible");
        }
        for (id, endpoint, reason) in [
            (src, se, "source_endpoint_missing"),
            (dst, de, "destination_endpoint_missing"),
        ] {
            if by_id.get(id).is_none_or(|n| {
                n["endpoints"]
                    .as_array()
                    .is_none_or(|a| !a.iter().any(|e| e["endpoint_id"] == endpoint))
            }) {
                reasons.push(reason);
            }
        }
        if qe.contains(&vec![src, se, dst, de]) {
            reasons.push("edge_quarantined");
        }
        let mut edge = project(
            p,
            "src_node_id src_endpoint_id dst_node_id dst_endpoint_id reachable sample_count measurement_method",
        );
        for f in [
            "connect_rtt_ema_ms",
            "rtt_p95_ms",
            "jitter_ms",
            "loss_ratio",
            "goodput_mbps",
        ] {
            edge[f] = if p[f].is_null() {
                Value::Null
            } else {
                json!(p[f].as_f64().ok_or(ContractError(C))?)
            };
        }
        edge["eligible"] = reasons.is_empty().into();
        edge["exclusion_reasons"] = json!(reasons);
        edge["version"] = version(r);
        edges.push(edge);
    }
    let router_nodes = json!(router_nodes);
    let edges = json!(edges);
    let allocator_nodes = json!(allocator_nodes);
    let rg = repr::digest(&repr::tuple(vec![
        repr::sequence("RouterNodeEvidence", &router_nodes)?,
        repr::sequence("RouterEdgeEvidence", &edges)?,
    ]));
    let ag = repr::digest(&repr::sequence("AllocatorNodeEvidence", &allocator_nodes)?);
    Ok((
        json!({"protocol":"mycelium.gossip.router_view.v1","snapshot_generation":input["snapshot_generation"],"eligibility_generation":rg,"nodes":router_nodes,"edges":edges}),
        json!({"protocol":"mycelium.gossip.allocator_view.v1","snapshot_generation":input["snapshot_generation"],"eligibility_generation":ag,"nodes":allocator_nodes}),
    ))
}
