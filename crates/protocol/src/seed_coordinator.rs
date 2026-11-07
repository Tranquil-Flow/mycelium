// SPDX-License-Identifier: AGPL-3.0-only
//! The relation decisions of v0.1.4 `SeedCoordinator.accept_join` / `accept_resume`
//! (`mycelium_seed/coordinator.py:523-899`, membership contract v1) and `_bounded_deadline`.
//!
//! Pure over a caller-held member table: no SQLite, RNG, clock or message-id source (the caller
//! supplies `now`, assigns the acceptance `message_id`, signs and commits). Envelope checks are
//! the already-ported `membership::verify` / `invite::verify_invite`; this module adds exactly what
//! the coordinator layers on top: identity reuse, key/endpoint conflict, join mismatch, resume
//! identity and generation fencing, and the bounded lease/message deadlines.
//!
//! Not ported (state, not verifiers): the retry shortcuts that replay a committed acceptance, the
//! SQLite commit, message-id reservation, heartbeat/lease-renewal/assignment paths, the v2 profile
//! contract (`membership_v2_before_cutover` is the only v2 outcome of a v1 seed).
use crate::{TimeLimits, invite, membership};
use serde_json::{Value, json};

pub const JOIN_REQUEST_PROTOCOL: &str = "mycelium.membership.join_request.v1";
pub const RESUME_REQUEST_PROTOCOL: &str = "mycelium.membership.resume_request.v1";
pub const JOIN_ACCEPTANCE_PROTOCOL: &str = "mycelium.membership.join_acceptance.v1";
pub const RESUME_ACCEPTANCE_PROTOCOL: &str = "mycelium.membership.resume_acceptance.v1";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct SeedCoordinatorError(pub &'static str);

/// What the coordinator knows about itself (its own key, swarm, node and deadlines).
pub struct SeedFrame<'a> {
    pub swarm_id: &'a str,
    pub seed_node_id: &'a str,
    pub seed_url: Option<&'a str>,
    pub seed_key_record: &'a Value,
    /// `max_ttl_seconds` is the coordinator's `MAX_MESSAGE_TTL_SECONDS`.
    pub limits: TimeLimits,
    pub lease_seconds: f64,
    pub message_ttl_seconds: f64,
}

/// One row of the coordinator's member table (`_Member`, the fields the decisions read or set).
#[derive(Debug, Clone, PartialEq)]
pub struct Member {
    pub node_id: String,
    pub endpoint_id: String,
    pub endpoint_addrs: Vec<String>,
    pub peer_class: String,
    pub runtime_capability: Value,
    pub verification_key_digest: String,
    pub incarnation: String,
    pub generation: u64,
    pub lease_expires_at: f64,
    pub lifecycle_state: String,
}

/// An admitted join or resume: the new member row, the unsigned acceptance (without `message_id`,
/// `swarm_id`, sender fields, which the caller's signer owns), and whether a resume was already
/// applied (a retransmission).
#[derive(Debug, Clone, PartialEq)]
pub struct Admission {
    pub member: Member,
    pub acceptance: Value,
    pub already_advanced: bool,
}

fn refuse<T>(code: &'static str) -> Result<T, SeedCoordinatorError> {
    Err(SeedCoordinatorError(code))
}

fn float_value(f: f64) -> Value {
    serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number)
}

/// `SeedCoordinator._bounded_deadline`.
pub fn bounded_deadline(
    issued_at: f64,
    ttl_seconds: f64,
    previous_deadline: Option<f64>,
    reserve_headroom: bool,
    max_ttl_seconds: f64,
) -> Result<f64, SeedCoordinatorError> {
    const UNAVAILABLE: SeedCoordinatorError = SeedCoordinatorError("seed_deadline_unavailable");
    let ceiling = issued_at + max_ttl_seconds;
    let requested = issued_at + ttl_seconds;
    if !ceiling.is_finite() {
        return Err(UNAVAILABLE);
    }
    let mut deadline = if ceiling < requested {
        ceiling
    } else {
        requested
    };
    if reserve_headroom && deadline >= ceiling {
        deadline = ceiling.next_down();
    }
    if let Some(previous) = previous_deadline {
        if deadline <= previous {
            deadline = previous;
        }
        if deadline <= previous {
            let advanced = previous.next_up();
            if !advanced.is_finite() || advanced > ceiling {
                return Err(UNAVAILABLE);
            }
            deadline = advanced;
        }
    }
    if !deadline.is_finite() || deadline <= issued_at || deadline > ceiling {
        return Err(UNAVAILABLE);
    }
    Ok(deadline)
}

fn text(value: &Value, field: &str) -> String {
    value[field].as_str().unwrap_or_default().to_owned()
}

fn addrs(request: &Value) -> Vec<String> {
    request["endpoint_addr"]["addrs"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

/// `_require_declaration_form` for a seed in membership contract v1.
fn require_v1_form(message: &Value) -> Result<(), SeedCoordinatorError> {
    if message
        .as_object()
        .is_some_and(|m| m.contains_key("peer_profile"))
    {
        return refuse("membership_v2_before_cutover");
    }
    Ok(())
}

/// `_accept_join` without its state steps: who may join, under which generation and lease.
pub fn evaluate_join(
    frame: &SeedFrame<'_>,
    members: &[Member],
    invite_token: &str,
    join_envelope: &Value,
    now: f64,
) -> Result<Admission, SeedCoordinatorError> {
    require_v1_form(&join_envelope["message"])?;
    if !now.is_finite() {
        return refuse("seed_clock_invalid");
    }
    let Some(seed_url) = frame.seed_url else {
        return refuse("seed_url_unbound");
    };
    let invite = invite::verify_invite(
        invite_token,
        std::slice::from_ref(frame.seed_key_record),
        now,
    )
    .map_err(|e| SeedCoordinatorError(e.0))?;
    // `verify_join_request`: the request is judged against the key it carries.
    if join_envelope["message"]["protocol"] != JOIN_REQUEST_PROTOCOL {
        return refuse("join_request_protocol_required");
    }
    let request = membership::verify_self_pinned(
        join_envelope,
        membership::Expected {
            protocol: JOIN_REQUEST_PROTOCOL,
            key_digest: "",
            swarm_id: None,
            sender_node_id: None,
            sender_endpoint_id: None,
            recipient_node_id: None,
        },
        now,
        frame.limits,
    )
    .map_err(|e| SeedCoordinatorError(e.0))?;
    if invite["swarm_id"] != frame.swarm_id
        || invite["seed_url"] != seed_url
        || request["swarm_id"] != frame.swarm_id
        || request["recipient_node_id"] != frame.seed_node_id
        || request["invite_nonce"] != invite["nonce"]
    {
        return refuse("seed_join_mismatch");
    }
    let record = &join_envelope["verification_key"];
    if !record.is_object() {
        return refuse("seed_join_key_invalid");
    }
    let key_digest = match record["verification_key_digest"].as_str() {
        Some(d) if !d.is_empty() => d.to_owned(),
        _ => return refuse("seed_join_key_invalid"),
    };
    let node_id = text(&request, "sender_node_id");
    let endpoint_id = text(&request, "sender_endpoint_id");
    let previous = members.iter().find(|m| m.node_id == node_id);
    if members.iter().any(|other| {
        other.node_id != node_id
            && (other.endpoint_id == endpoint_id || other.verification_key_digest == key_digest)
    }) {
        return refuse("seed_member_identity_reused");
    }
    if previous.is_some_and(|p| p.verification_key_digest != key_digest) {
        return refuse("seed_node_key_conflict");
    }
    if previous.is_some_and(|p| p.endpoint_id != endpoint_id) {
        return refuse("seed_node_endpoint_conflict");
    }
    let generation = previous.map_or(1, |p| p.generation + 1);
    let lease_expires_at = bounded_deadline(
        now,
        frame.lease_seconds,
        None,
        true,
        frame.limits.max_ttl_seconds,
    )?;
    let expires_at = bounded_deadline(
        now,
        frame.message_ttl_seconds,
        None,
        false,
        frame.limits.max_ttl_seconds,
    )?;
    let member = Member {
        node_id: node_id.clone(),
        endpoint_id,
        endpoint_addrs: addrs(&request),
        peer_class: text(&request, "peer_class"),
        runtime_capability: request["runtime_capability"].clone(),
        verification_key_digest: key_digest,
        incarnation: text(&request, "incarnation"),
        generation,
        lease_expires_at,
        lifecycle_state: "NEW".to_owned(),
    };
    let acceptance = json!({
        "protocol": JOIN_ACCEPTANCE_PROTOCOL,
        "recipient_node_id": node_id,
        "generation": generation,
        "issued_at": float_value(now),
        "expires_at": float_value(expires_at),
        "request_message_id": request["message_id"],
        "accepted_node_id": node_id,
        "accepted_incarnation": request["incarnation"],
        "membership_generation": generation,
        "lease_expires_at": float_value(lease_expires_at),
    });
    Ok(Admission {
        member,
        acceptance,
        already_advanced: false,
    })
}

/// `_accept_resume` without its state steps: fencing a durable principal's next incarnation.
pub fn evaluate_resume(
    frame: &SeedFrame<'_>,
    members: &[Member],
    resume_envelope: &Value,
    now: f64,
) -> Result<Admission, SeedCoordinatorError> {
    require_v1_form(&resume_envelope["message"])?;
    let untrusted = &resume_envelope["message"];
    if !untrusted.is_object() {
        return refuse("seed_resume_request_invalid");
    }
    let Some(node_id) = untrusted["sender_node_id"].as_str() else {
        return refuse("seed_resume_request_invalid");
    };
    let Some(previous) = members.iter().find(|m| m.node_id == node_id) else {
        return refuse("seed_member_unknown");
    };
    if !now.is_finite() {
        return refuse("seed_clock_invalid");
    }
    let request = membership::verify(
        resume_envelope,
        membership::Expected {
            protocol: RESUME_REQUEST_PROTOCOL,
            key_digest: &previous.verification_key_digest,
            swarm_id: Some(frame.swarm_id),
            sender_node_id: Some(node_id),
            sender_endpoint_id: None,
            recipient_node_id: Some(frame.seed_node_id),
        },
        now,
        frame.limits,
    )
    .map_err(|e| SeedCoordinatorError(e.0))?;
    let key_digest = resume_envelope["verification_key"]["verification_key_digest"].as_str();
    if request["sender_endpoint_id"] != previous.endpoint_id.as_str()
        || key_digest != Some(previous.verification_key_digest.as_str())
        || request["peer_class"] != previous.peer_class.as_str()
        || request["runtime_capability"] != previous.runtime_capability
    {
        return refuse("seed_resume_identity_mismatch");
    }
    if matches!(previous.lifecycle_state.as_str(), "STOPPING" | "STOPPED") {
        return refuse("seed_resume_member_revoked");
    }
    let request_generation = request["generation"].as_u64().unwrap_or_default();
    let incarnation = text(&request, "incarnation");
    let already_advanced =
        previous.generation == request_generation + 1 && previous.incarnation == incarnation;
    if !already_advanced
        && (previous.generation != request_generation
            || request["previous_incarnation"] != previous.incarnation.as_str())
    {
        return refuse("seed_resume_generation_stale");
    }
    let generation = request_generation + 1;
    let lease_expires_at = bounded_deadline(
        now,
        frame.lease_seconds,
        None,
        true,
        frame.limits.max_ttl_seconds,
    )?;
    let expires_at = bounded_deadline(
        now,
        frame.message_ttl_seconds,
        None,
        false,
        frame.limits.max_ttl_seconds,
    )?;
    let member = Member {
        node_id: node_id.to_owned(),
        endpoint_id: previous.endpoint_id.clone(),
        endpoint_addrs: addrs(&request),
        peer_class: previous.peer_class.clone(),
        runtime_capability: previous.runtime_capability.clone(),
        verification_key_digest: previous.verification_key_digest.clone(),
        incarnation,
        generation,
        lease_expires_at,
        lifecycle_state: "NEW".to_owned(),
    };
    let acceptance = json!({
        "protocol": RESUME_ACCEPTANCE_PROTOCOL,
        "recipient_node_id": node_id,
        "generation": generation,
        "issued_at": float_value(now),
        "expires_at": float_value(expires_at),
        "request_message_id": request["message_id"],
        "accepted_node_id": node_id,
        "accepted_incarnation": request["incarnation"],
        "previous_membership_generation": request_generation,
        "membership_generation": generation,
        "lease_expires_at": float_value(lease_expires_at),
    });
    Ok(Admission {
        member,
        acceptance,
        already_advanced,
    })
}
