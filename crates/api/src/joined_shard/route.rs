// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-signed serving route over independently verified member native proofs.
//!
//! `mycelium.joined_route.v1` (class A, new and versioned) binds each stage to the
//! exact enrolled member row, held offer, Iroh carrier endpoint and verified
//! resident-proof envelope. It is the producer of the M16 execution graph and the
//! request qualification for that route; it does NOT fabricate legacy
//! `layer_load_proof.v1` objects or promote physical readiness (`route_ready:false`).
//! Hops ride the unchanged Router v1 frames over the Router ALPN. The only new bytes
//! are the entry-stage token payload, versioned below under this route protocol.
use super::*;
use protocol::router_wire;

pub const ROUTE: &str = "mycelium.joined_route.v1";
pub const SCOPE: &str = "loopback_two_node_qualified";
const FIELDS: &[&str] = &[
    "protocol",
    "swarm_id",
    "deployment_id",
    "deployment_epoch",
    "topology_version",
    "model_id",
    "resolved_commit",
    "manifest_digest",
    "owner_endpoint_id",
    "stages",
    "issued_at",
    "expires_at",
    "route_ready",
    "scope",
];
const STAGE_FIELDS: &[&str] = &[
    "stage_index",
    "placement_id",
    "member",
    "carrier_endpoint_id",
    "run",
    "offer_digest",
    "proof_digest",
    "range",
    "worker_pid",
    "tokens",
    "memory",
];
/// Total decoder layers of the only supported family (Qwen3-0.6B).
const LAYERS: u64 = 28;
const TOKENS_MAGIC: &[u8; 5] = b"MYTK\x01";
fn int(v: &Value) -> Result<u64> {
    v.as_u64().ok_or(fail("joined_route_invalid"))
}
/// Issue a route only from offers whose signed proofs verify NOW against the
/// current seed rows and the owner's own nonces. `stages` = (offer, proof, nonce).
pub fn issue(
    coordinator: &seed::Coordinator,
    model: &Value,
    deployment_id: &str,
    epoch: u64,
    owner_endpoint_id: &str,
    stages: &[(Value, Value, String)],
    now: f64,
) -> Result<Value> {
    if stages.len() < 2 || owner_endpoint_id.parse::<iroh::EndpointId>().is_err() || epoch == 0 {
        return Err(fail("joined_route_invalid"));
    }
    let wall = budgets::BudgetId::MemberChosenWall
        .span(budgets::Entries::ZERO)
        .map_err(|_| fail("budget_unavailable"))?;
    let mut rows = Vec::new();
    let mut next = 0;
    for (i, (offer, proof, nonce)) in stages.iter().enumerate() {
        let resident = native::verify_resident(coordinator, offer, nonce, proof)?;
        let prepare = &proof["message"]["prepare"];
        let (start, end) = (int(&prepare["start"])?, int(&prepare["end"])?);
        if start != next || end <= start {
            return Err(fail("joined_route_range_invalid"));
        }
        next = end;
        let m = &offer["message"];
        let node = text(&m["member"], "node_id")?;
        rows.push(json!({"stage_index":i,"placement_id":format!("{node}:{start}-{end}"),
            "member":m["member"],"carrier_endpoint_id":m["registration"]["message"]["carrier_endpoint_id"],
            "run":m["run"],"offer_digest":digest(offer)?,"proof_digest":digest(proof)?,
            "range":[start,end],"worker_pid":resident["worker_pid"],"tokens":prepare["tokens"],
            "memory":prepare["memory"]}));
    }
    if next != LAYERS {
        return Err(fail("joined_route_range_invalid"));
    }
    signed(
        coordinator.signer(),
        json!({"protocol":ROUTE,"swarm_id":coordinator.swarm_id(),"deployment_id":deployment_id,
        "deployment_epoch":epoch,"topology_version":epoch,"model_id":model["model_id"],
        "resolved_commit":model["resolved_commit"],"manifest_digest":model["manifest_digest"],
        "owner_endpoint_id":owner_endpoint_id,"stages":rows,"issued_at":now,
        "expires_at":now + wall.millis() as f64 / 1000.0,"route_ready":false,"scope":SCOPE}),
    )
}
fn checked(route: &Value, key_digest: &str, now: f64) -> Result<Value> {
    let message = open(route, ROUTE, key_digest)?;
    exact(message, FIELDS)?;
    let stages = message["stages"]
        .as_array()
        .ok_or(fail("joined_route_invalid"))?;
    for (i, s) in stages.iter().enumerate() {
        exact(s, STAGE_FIELDS)?;
        if int(&s["stage_index"])? != i as u64 {
            return Err(fail("joined_route_invalid"));
        }
    }
    if stages.len() < 2
        || message["route_ready"] != false
        || message["scope"] != SCOPE
        || message["expires_at"]
            .as_f64()
            .is_none_or(|e| !e.is_finite() || e <= now)
    {
        return Err(fail("joined_route_expired"));
    }
    Ok(message.clone())
}
/// Owner-side re-verification: seed signature plus every stage still current.
pub fn verify_owner(coordinator: &seed::Coordinator, route: &Value, now: f64) -> Result<Value> {
    let message = checked(route, &coordinator.signer().verification_key_digest(), now)?;
    for s in message["stages"].as_array().into_iter().flatten() {
        current(coordinator, &s["member"])?;
    }
    Ok(message)
}
/// Member-side: seed-signed, names THIS live member with its own held offer and
/// its own currently resident proof. Returns the stage index and message.
pub fn verify_member(
    route: &Value,
    member: &Value,
    offer: &Value,
    proof_digest: &str,
    now: f64,
) -> Result<(usize, Value)> {
    let message = checked(route, text(member, "seed_key_digest")?, now)?;
    let offer_digest = digest(offer)?;
    let index = message["stages"]
        .as_array()
        .into_iter()
        .flatten()
        .position(|s| {
            s["member"] == *member
                && s["offer_digest"] == offer_digest
                && s["proof_digest"] == proof_digest
        })
        .ok_or(fail("joined_route_stage_mismatch"))?;
    if message["swarm_id"] != member["swarm_id"] {
        return Err(fail("joined_route_stage_mismatch"));
    }
    Ok((index, message))
}
/// Digest of the signed proof envelope a member returned, as the route binds it.
pub fn proof_digest(proof: &Value) -> Result<String> {
    digest(proof)
}
const KV_BYTES_PER_LAYER_TOKEN: u64 = 8 * 128 * 2 * 4;
/// Versioned M16 execution graph producer for a verified route. Each placement's
/// `load_proof_digest` is the verified joined-native proof envelope digest.
pub fn graph(message: &Value) -> Result<protocol::execution_graph::Graph> {
    let stages = message["stages"]
        .as_array()
        .ok_or(fail("joined_route_invalid"))?;
    let mut graph_stages = Vec::new();
    for s in stages {
        let (start, end) = (int(&s["range"][0])?, int(&s["range"][1])?);
        let layers = end - start;
        let mut roles = vec![json!("decoder")];
        if start == 0 {
            roles.insert(0, json!("embedding"));
        }
        if end == LAYERS {
            roles.push(json!("head"));
        }
        graph_stages.push(json!({"stage_id":format!("stage-{}",s["stage_index"]),
            "range":{"start_layer":start,"end_layer_exclusive":end,"layer_count":layers},
            "component_roles":roles,
            "stage_cost":{"prefill_work_units_per_prompt_token":layers,"decode_work_units_per_token":layers,
                "kv_bytes_per_context_token":layers*KV_BYTES_PER_LAYER_TOKEN},
            "placements":[{"placement_id":s["placement_id"],"node_id":s["member"]["node_id"],
                "replica_group_id":format!("stage-{}",s["stage_index"]),"assignment_id":s["run"],
                "stage_signature":s["offer_digest"],"load_proof_digest":s["proof_digest"],
                "runtime_backend":"mlx","runtime_endpoint":format!("iroh-router://{}",text(s,"carrier_endpoint_id")?)}]}));
    }
    let edges: Vec<Value> = stages
        .windows(2)
        .map(|w| {
            json!({"edge_id":format!("edge-{}-{}",w[0]["stage_index"],w[1]["stage_index"]),
            "from_placement_id":w[0]["placement_id"],"to_placement_id":w[1]["placement_id"],
            "link_id":format!("owner-relay-{}-{}",w[0]["stage_index"],w[1]["stage_index"])})
        })
        .collect();
    let last = stages.len() - 1;
    protocol::execution_graph::parse(&json!({"protocol":"mycelium.execution_graph.v1",
        "deployment_id":message["deployment_id"],"deployment_epoch":message["deployment_epoch"],
        "topology_version":message["topology_version"],"model_id":message["model_id"],
        "resolved_commit":message["resolved_commit"],"manifest_digest":message["manifest_digest"],
        "entry_stage_id":"stage-0","final_stage_id":format!("stage-{last}"),"hidden_size":1024,
        "activation_bytes":4,"token_envelope_bytes":4,"stages":graph_stages,"edges":edges,
        "loopback_edges":[{"edge_id":"loopback","from_placement_id":stages[last]["placement_id"],
            "to_placement_id":stages[0]["placement_id"],"link_id":"owner-relay-loopback"}]}))
    .map_err(|_| fail("joined_route_graph_invalid"))
}
/// The request qualification an owner may serve for exactly this route.
pub fn qualification(message: &Value) -> Result<Value> {
    let digests: Vec<Value> = message["stages"]
        .as_array()
        .ok_or(fail("joined_route_invalid"))?
        .iter()
        .map(|s| s["proof_digest"].clone())
        .collect();
    let route_digest = digest(message)?;
    Ok(
        json!({"qualification_id":format!("joined-route-{}",&route_digest[7..23]),
        "qualification_digest":route_digest,"deployment_id":message["deployment_id"],
        "deployment_epoch":message["deployment_epoch"],"topology_version":message["topology_version"],
        "model_id":message["model_id"],"resolved_commit":message["resolved_commit"],
        "manifest_digest":message["manifest_digest"],
        "path_manifest_digest":digest(&message["stages"])?,"stage_load_proof_digests":digests}),
    )
}
/// Request/path identity fixed by M16 dispatch.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Path {
    pub request_id: String,
    pub path_id: String,
    pub path_attempt: u64,
}
pub fn phase(token_index: u64) -> &'static str {
    if token_index == 0 {
        "PREFILL"
    } else {
        "DECODE"
    }
}
/// The exact Router v1 HopHeader body each hop must carry. The entry hop of a
/// decode step follows the graph's loopback edge from the final placement.
pub fn hop(message: &Value, path: &Path, token_index: u64, hop_index: usize) -> Result<Value> {
    let stages = message["stages"]
        .as_array()
        .ok_or(fail("joined_route_invalid"))?;
    let destination = stages.get(hop_index).ok_or(fail("joined_route_invalid"))?;
    let source = match (hop_index, token_index) {
        (0, 0) => json!(""),
        (0, _) => stages[stages.len() - 1]["placement_id"].clone(),
        (i, _) => stages[i - 1]["placement_id"].clone(),
    };
    Ok(
        json!({"request_id":path.request_id,"path_id":path.path_id,"path_attempt":path.path_attempt,
        "phase":phase(token_index),"token_index":token_index,"hop_index":hop_index,
        "source_placement_id":source,"destination_placement_id":destination["placement_id"],
        "topology_version":message["topology_version"],
        "idempotency_key":format!("{}:{}:{token_index}:{hop_index}",path.request_id,path.path_attempt)}),
    )
}
pub fn cancellation(message: &Value, path: &Path) -> Value {
    json!({"request_id":path.request_id,"path_id":path.path_id,"path_attempt":path.path_attempt,
        "topology_version":message["topology_version"]})
}
pub fn token_event(path: &Path, token_index: u64, token_id: u32) -> Value {
    json!({"request_id":path.request_id,"path_id":path.path_id,"path_attempt":path.path_attempt,
        "token_index":token_index,"token_id":token_id,"sampling_counter":token_index + 1})
}
/// Normalized body equality, so defaulted fields cannot hide a mismatch.
pub fn body_matches(frame: &router_wire::DecodedFrame, kind: &str, expected: &Value) -> bool {
    frame.message_type() == kind
        && router_wire::encode_frame(kind, expected, &[])
            .and_then(|bytes| router_wire::decode_frame(&bytes))
            .is_ok_and(|normal| normal.body() == frame.body())
}
pub fn entry_payload(ids: &[u32]) -> Result<Vec<u8>> {
    let count = u32::try_from(ids.len()).map_err(|_| fail("joined_route_tokens_invalid"))?;
    let mut out = TOKENS_MAGIC.to_vec();
    out.extend(count.to_be_bytes());
    for id in ids {
        out.extend(id.to_be_bytes());
    }
    Ok(out)
}
pub fn entry_tokens(payload: &[u8]) -> Result<Vec<u32>> {
    let bad = || fail("joined_route_tokens_invalid");
    let rest = payload.strip_prefix(TOKENS_MAGIC).ok_or_else(bad)?;
    let count = u32::from_be_bytes(
        rest.get(..4)
            .ok_or_else(bad)?
            .try_into()
            .map_err(|_| bad())?,
    );
    let body = &rest[4..];
    if count == 0 || body.len() != count as usize * 4 {
        return Err(bad());
    }
    Ok(body
        .chunks_exact(4)
        .map(|c| u32::from_be_bytes([c[0], c[1], c[2], c[3]]))
        .collect())
}
