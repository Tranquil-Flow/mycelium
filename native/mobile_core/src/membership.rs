// SPDX-License-Identifier: AGPL-3.0-only
//! A9 v1 membership messages and signed envelopes
//! (`mycelium_membership/contracts.py`), plus verification of seed-signed
//! statements against the seed key pinned by the invitation.

use serde_json::{Value, json};

use crate::identity::{self, Identity};

pub const SIGNED_MESSAGE_PROTOCOL: &str = "mycelium.membership.signed_message.v1";
pub const JOIN_REQUEST_PROTOCOL: &str = "mycelium.membership.join_request.v1";
pub const JOIN_ACCEPTANCE_PROTOCOL: &str = "mycelium.membership.join_acceptance.v1";
pub const RESUME_REQUEST_PROTOCOL: &str = "mycelium.membership.resume_request.v1";
pub const RESUME_ACCEPTANCE_PROTOCOL: &str = "mycelium.membership.resume_acceptance.v1";
pub const SEED_RESUME_HTTP_PROTOCOL: &str = "mycelium.seed.resume_http.v1";
pub const HEARTBEAT_PROTOCOL: &str = "mycelium.membership.heartbeat.v1";
pub const LEASE_RENEWAL_PROTOCOL: &str = "mycelium.membership.lease_renewal.v1";
pub const CAPABILITY_REPORT_PROTOCOL: &str = "mycelium.membership.capability_report.v1";
pub const SEED_IDENTITY_PROTOCOL: &str = "mycelium.seed.identity.v1";
pub const SEED_JOIN_HTTP_PROTOCOL: &str = "mycelium.seed.join_http.v1";
pub const SEED_MEMBER_HTTP_PROTOCOL: &str = "mycelium.seed.member_http.v1";
pub const ROUTER_ACTIVATION_PROTOCOL: &str = "mycelium.router_wire.v1";
pub const MAX_MESSAGE_TTL_SECONDS: f64 = 3600.0;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct MembershipError(pub &'static str);

/// Who this member says it is, fixed for one incarnation.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct MemberConfig {
    pub swarm_id: String,
    pub node_id: String,
    pub seed_node_id: String,
    pub incarnation: String,
    pub software_version: String,
    pub peer_class: String,
    pub runtime_capability: Value,
    pub ttl_seconds: f64,
}

pub fn envelope(identity: &Identity, message: Value) -> Result<Value, MembershipError> {
    if message["sender_endpoint_id"] != identity.endpoint_id() {
        return Err(MembershipError("membership_signer_endpoint_mismatch"));
    }
    let signature = identity.sign(&message).map_err(|_| MembershipError("membership_signature_invalid"))?;
    Ok(json!({
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "message": message,
        "signature": signature,
        "verification_key": identity.public_key_record(),
    }))
}

fn common(cfg: &MemberConfig, identity: &Identity, protocol: &str, message_id: &str, generation: u64, now: f64) -> Value {
    json!({
        "protocol": protocol,
        "message_id": message_id,
        "swarm_id": cfg.swarm_id,
        "sender_node_id": cfg.node_id,
        "sender_endpoint_id": identity.endpoint_id(),
        "recipient_node_id": cfg.seed_node_id,
        "incarnation": cfg.incarnation,
        "generation": generation,
        "issued_at": now,
        "expires_at": now + cfg.ttl_seconds.min(MAX_MESSAGE_TTL_SECONDS),
    })
}

pub fn join_request(
    cfg: &MemberConfig,
    identity: &Identity,
    message_id: &str,
    invite_nonce: &str,
    endpoint_addrs: &[String],
    now: f64,
) -> Result<Value, MembershipError> {
    if endpoint_addrs.is_empty() || endpoint_addrs.len() > 16 {
        return Err(MembershipError("membership_endpoint_addr_invalid"));
    }
    let mut m = common(cfg, identity, JOIN_REQUEST_PROTOCOL, message_id, 0, now);
    m["invite_nonce"] = json!(invite_nonce);
    m["software_version"] = json!(cfg.software_version);
    m["peer_class"] = json!(cfg.peer_class);
    m["runtime_capability"] = cfg.runtime_capability.clone();
    m["endpoint_addr"] = json!({"id": identity.endpoint_id(), "addrs": endpoint_addrs});
    envelope(identity, m)
}

/// Resume an enrolled durable member after a process restart: a fresh
/// incarnation, the previous incarnation and the last membership generation.
#[allow(clippy::too_many_arguments)]
pub fn resume_request(
    cfg: &MemberConfig,
    identity: &Identity,
    message_id: &str,
    previous_incarnation: &str,
    generation: u64,
    endpoint_addrs: &[String],
    now: f64,
) -> Result<Value, MembershipError> {
    if previous_incarnation == cfg.incarnation {
        return Err(MembershipError("membership_resume_incarnation_invalid"));
    }
    if generation < 1 {
        return Err(MembershipError("membership_generation_invalid"));
    }
    if endpoint_addrs.is_empty() || endpoint_addrs.len() > 16 {
        return Err(MembershipError("membership_endpoint_addr_invalid"));
    }
    let mut m = common(cfg, identity, RESUME_REQUEST_PROTOCOL, message_id, generation, now);
    m["previous_incarnation"] = json!(previous_incarnation);
    m["software_version"] = json!(cfg.software_version);
    m["peer_class"] = json!(cfg.peer_class);
    m["runtime_capability"] = cfg.runtime_capability.clone();
    m["endpoint_addr"] = json!({"id": identity.endpoint_id(), "addrs": endpoint_addrs});
    envelope(identity, m)
}

/// Scheduled heartbeat. `route_ready` is always false: only the seed/planner can
/// later place a member (A12 `route_ready_must_start_false`).
#[allow(clippy::too_many_arguments)]
pub fn heartbeat(
    cfg: &MemberConfig,
    identity: &Identity,
    message_id: &str,
    generation: u64,
    sequence: u64,
    lifecycle_state: &str,
    active_requests: u64,
    now: f64,
) -> Result<Value, MembershipError> {
    const STATES: [&str; 6] = ["NEW", "CONFIGURED", "RUNNING", "DRAINING", "STOPPING", "STOPPED"];
    if !STATES.contains(&lifecycle_state) {
        return Err(MembershipError("membership_lifecycle_invalid"));
    }
    if generation < 1 {
        return Err(MembershipError("membership_generation_invalid"));
    }
    let mut m = common(cfg, identity, HEARTBEAT_PROTOCOL, message_id, generation, now);
    m["heartbeat_sequence"] = json!(sequence);
    m["lifecycle_state"] = json!(lifecycle_state);
    m["route_ready"] = json!(false);
    m["active_requests"] = json!(active_requests);
    m["liveness_source"] = json!("scheduled_heartbeat");
    m["activity_receipt_digest"] = Value::Null;
    m["activity_peer_node_id"] = Value::Null;
    envelope(identity, m)
}

#[allow(clippy::too_many_arguments)]
pub fn capability_report(
    cfg: &MemberConfig,
    identity: &Identity,
    message_id: &str,
    generation: u64,
    platform: &str,
    architecture: &str,
    memory_bytes: u64,
    available_storage_bytes: u64,
    now: f64,
) -> Result<Value, MembershipError> {
    let mut m = common(cfg, identity, CAPABILITY_REPORT_PROTOCOL, message_id, generation, now);
    m["platform"] = json!(platform);
    m["architecture"] = json!(architecture);
    m["memory_bytes"] = json!(memory_bytes);
    m["available_storage_bytes"] = json!(available_storage_bytes);
    m["backends"] = json!([cfg.runtime_capability["runtime_backend"]]);
    m["precisions"] = json!(["float32"]);
    envelope(identity, m)
}

/// Verify a membership envelope signed by the seed whose key the invite pinned.
pub fn verify_seed_envelope(env: &Value, pinned_seed_key: &Value, expected_protocol: &str) -> Result<Value, MembershipError> {
    let map = env.as_object().ok_or(MembershipError("membership_envelope_invalid"))?;
    if map.len() != 4 || map.get("protocol").and_then(Value::as_str) != Some(SIGNED_MESSAGE_PROTOCOL) {
        return Err(MembershipError("membership_envelope_invalid"));
    }
    if map.get("verification_key") != Some(pinned_seed_key) {
        return Err(MembershipError("membership_seed_key_mismatch"));
    }
    let message = &map["message"];
    if message["protocol"] != expected_protocol {
        return Err(MembershipError("membership_protocol_invalid"));
    }
    identity::verify(message, &map["signature"], pinned_seed_key)
        .map_err(|_| MembershipError("membership_signature_invalid"))?;
    Ok(message.clone())
}

/// Verify the seed's signed identity statement (`GET /seed/identity`).
pub fn verify_seed_identity(env: &Value, pinned_seed_key: &Value, now: f64) -> Result<Value, MembershipError> {
    let map = env.as_object().ok_or(MembershipError("seed_identity_invalid"))?;
    if map.get("verification_key") != Some(pinned_seed_key) {
        return Err(MembershipError("membership_seed_key_mismatch"));
    }
    let statement = &map["statement"];
    if statement["protocol"] != SEED_IDENTITY_PROTOCOL {
        return Err(MembershipError("seed_identity_invalid"));
    }
    identity::verify(statement, &map["signature"], pinned_seed_key)
        .map_err(|_| MembershipError("membership_signature_invalid"))?;
    let expires = statement["expires_at"].as_f64().unwrap_or(0.0);
    if now > expires {
        return Err(MembershipError("seed_identity_expired"));
    }
    Ok(statement.clone())
}
