// SPDX-License-Identifier: AGPL-3.0-only
//! Membership parsing/verification, ported from mycelium_membership/contracts.py.
use crate::ContractError;
use serde_json::Value;

pub const SIGNED_MESSAGE_PROTOCOL: &str = "mycelium.membership.signed_message.v1";

/// Caller-selected pins. Absence preserves the oracle's optional identity checks.
#[derive(Clone, Copy)]
pub struct Expected<'a> {
    pub protocol: &'a str,
    pub key_digest: &'a str,
    pub swarm_id: Option<&'a str>,
    pub sender_node_id: Option<&'a str>,
    pub sender_endpoint_id: Option<&'a str>,
    pub recipient_node_id: Option<&'a str>,
}

use crate::{TimeLimits, checks::*, identity};

const COMMON: &[&str] = &[
    "protocol",
    "message_id",
    "swarm_id",
    "sender_node_id",
    "sender_endpoint_id",
    "recipient_node_id",
    "incarnation",
    "generation",
    "issued_at",
    "expires_at",
];

fn kind(message: &Value) -> Result<&str, ContractError> {
    let p = message["protocol"]
        .as_str()
        .ok_or(ContractError("membership_protocol_invalid"))?;
    p.strip_prefix("mycelium.membership.")
        .and_then(|s| s.strip_suffix(".v1"))
        .ok_or(ContractError("membership_protocol_invalid"))
}

fn specific(k: &str) -> Result<&'static [&'static str], ContractError> {
    match k {
        "join_request" => Ok(&[
            "invite_nonce",
            "endpoint_addr",
            "software_version",
            "peer_class",
            "runtime_capability",
        ]),
        "resume_request" => Ok(&[
            "previous_incarnation",
            "endpoint_addr",
            "software_version",
            "peer_class",
            "runtime_capability",
        ]),
        "join_acceptance" => Ok(&[
            "request_message_id",
            "accepted_node_id",
            "accepted_incarnation",
            "membership_generation",
            "lease_expires_at",
        ]),
        "resume_acceptance" => Ok(&[
            "request_message_id",
            "accepted_node_id",
            "accepted_incarnation",
            "previous_membership_generation",
            "membership_generation",
            "lease_expires_at",
        ]),
        "capability_report" => Ok(&[
            "platform",
            "architecture",
            "memory_bytes",
            "available_storage_bytes",
            "backends",
            "precisions",
        ]),
        "link_probe_report" => Ok(&[
            "target_node_id",
            "target_endpoint_id",
            "reachable",
            "rtt_ms",
            "goodput_bytes_per_second",
            "probe_sequence",
        ]),
        "heartbeat" => Ok(&[
            "heartbeat_sequence",
            "lifecycle_state",
            "route_ready",
            "active_requests",
            "liveness_source",
            "activity_receipt_digest",
            "activity_peer_node_id",
        ]),
        "lease_renewal" => Ok(&[
            "heartbeat_message_id",
            "member_incarnation",
            "membership_generation",
            "lease_expires_at",
        ]),
        "assignment_offer" => Ok(&[
            "deployment_id",
            "deployment_epoch",
            "assignment_id",
            "assignment_digest",
            "stage_pack_digest",
            "graph_digest",
            "load_generation",
            "placement_provenance",
            "peer_endpoint_records",
        ]),
        "assignment_result" => Ok(&[
            "deployment_id",
            "deployment_epoch",
            "assignment_id",
            "accepted",
            "result_code",
            "load_proof_digest",
            "runtime_endpoint",
        ]),
        "drain_ack" => Ok(&[
            "drain_id",
            "active_requests",
            "last_request_id",
            "completed_at",
        ]),
        "seed_rotation_ack" => Ok(&["authority_generation", "transition_digest"]),
        _ => Err(ContractError("membership_protocol_invalid")),
    }
}

/// Strict schema validation, not authority admission. In particular peer-profile
/// v2 inner signatures and activation eligibility remain the seed's obligation.
pub fn validate(m: &Value, limits: TimeLimits) -> Result<(), ContractError> {
    limits.validate()?;
    require(m.is_object(), "membership_message_invalid")?;
    let k = kind(m)?;
    let mut fields = COMMON.to_vec();
    fields.extend_from_slice(specific(k)?);
    if matches!(k, "join_request" | "resume_request") && m.get("peer_profile").is_some() {
        fields.retain(|f| !["peer_class", "runtime_capability"].contains(f));
        fields.push("peer_profile");
    }
    exact(m, &fields, "membership_fields_invalid")?;
    for f in [
        "message_id",
        "swarm_id",
        "sender_node_id",
        "sender_endpoint_id",
        "recipient_node_id",
        "incarnation",
    ] {
        require(segment(&m[f]), "membership_identifier_invalid")?;
    }
    require(
        integer(&m["generation"], 0),
        "membership_generation_invalid",
    )?;
    let issued = number(&m["issued_at"], "membership_time_invalid")?;
    let expires = number(&m["expires_at"], "membership_time_invalid")?;
    require(expires > issued, "membership_time_invalid")?;
    require(
        expires - issued <= limits.max_ttl_seconds,
        "membership_ttl_invalid",
    )?;
    if k == "join_request" {
        require(m["generation"] == 0, "membership_join_generation_invalid")?;
    } else {
        require(
            integer(&m["generation"], 1),
            "membership_generation_invalid",
        )?;
    }
    match k {
        "join_request" | "resume_request" => declaration(m, k),
        "join_acceptance" | "resume_acceptance" => {
            for f in [
                "request_message_id",
                "accepted_node_id",
                "accepted_incarnation",
            ] {
                require(segment(&m[f]), "membership_identifier_invalid")?;
            }
            require(
                m["accepted_node_id"] == m["recipient_node_id"],
                "membership_recipient_mismatch",
            )?;
            require(
                integer(&m["membership_generation"], 1)
                    && m["membership_generation"] == m["generation"],
                "membership_generation_invalid",
            )?;
            if k == "resume_acceptance" {
                require(
                    next_integer(
                        &m["previous_membership_generation"],
                        &m["membership_generation"],
                    ),
                    "membership_generation_invalid",
                )?;
            }
            let lease = number(
                &m["lease_expires_at"],
                if k == "join_acceptance" {
                    "membership_time_invalid"
                } else {
                    "membership_lease_ttl_invalid"
                },
            )?;
            require(
                lease > issued,
                if k == "join_acceptance" {
                    "membership_time_invalid"
                } else {
                    "membership_lease_ttl_invalid"
                },
            )?;
            require(
                lease - issued <= limits.max_ttl_seconds,
                "membership_lease_ttl_invalid",
            )
        }
        "lease_renewal" => {
            for f in ["heartbeat_message_id", "member_incarnation"] {
                require(segment(&m[f]), "membership_identifier_invalid")?;
            }
            require(
                integer(&m["membership_generation"], 1)
                    && m["membership_generation"] == m["generation"],
                "membership_generation_invalid",
            )?;
            let lease = number(&m["lease_expires_at"], "membership_lease_ttl_invalid")?;
            require(
                lease > issued && lease - issued <= limits.max_ttl_seconds,
                "membership_lease_ttl_invalid",
            )
        }
        "heartbeat" => heartbeat(m),
        "capability_report" => {
            require(text(&m["platform"], 128), "membership_text_invalid")?;
            require(segment(&m["architecture"]), "membership_identifier_invalid")?;
            for f in ["memory_bytes", "available_storage_bytes"] {
                require(integer(&m[f], 0), "membership_integer_invalid")?;
            }
            for f in ["backends", "precisions"] {
                let values = m[f]
                    .as_array()
                    .ok_or(ContractError("membership_array_invalid"))?;
                let unique: std::collections::BTreeSet<_> =
                    values.iter().filter_map(Value::as_str).collect();
                require(
                    !values.is_empty()
                        && values.len() <= 64
                        && values.iter().all(segment)
                        && unique.len() == values.len(),
                    "membership_array_invalid",
                )?;
            }
            Ok(())
        }
        "link_probe_report" => {
            for f in ["target_node_id", "target_endpoint_id"] {
                require(segment(&m[f]), "membership_identifier_invalid")?;
            }
            require(
                m["target_node_id"] == m["recipient_node_id"],
                "membership_recipient_mismatch",
            )?;
            require(m["reachable"].is_boolean(), "membership_boolean_invalid")?;
            for f in ["rtt_ms", "goodput_bytes_per_second"] {
                require(
                    number(&m[f], "membership_number_invalid")? >= 0.0,
                    "membership_number_invalid",
                )?;
            }
            require(
                integer(&m["probe_sequence"], 0),
                "membership_integer_invalid",
            )
        }
        "assignment_offer" => assignment_offer(m, issued, expires),
        "assignment_result" => {
            assignment_identity(m)?;
            require(m["accepted"].is_boolean(), "membership_boolean_invalid")?;
            require(segment(&m["result_code"]), "membership_identifier_invalid")?;
            if m["accepted"] == true {
                require(digest(&m["load_proof_digest"]), "membership_digest_invalid")?;
                require(
                    text(&m["runtime_endpoint"], 1024),
                    "membership_text_invalid",
                )
            } else {
                require(
                    m["load_proof_digest"].is_null() && m["runtime_endpoint"].is_null(),
                    "membership_result_shape_invalid",
                )
            }
        }
        "drain_ack" => {
            require(segment(&m["drain_id"]), "membership_identifier_invalid")?;
            require(
                integer(&m["active_requests"], 0) && m["active_requests"] == 0,
                "membership_active_requests_invalid",
            )?;
            require(
                m["last_request_id"].is_null() || segment(&m["last_request_id"]),
                "membership_identifier_invalid",
            )?;
            let completed = number(&m["completed_at"], "membership_time_invalid")?;
            require(
                issued <= completed && completed <= expires,
                "membership_time_invalid",
            )
        }
        "seed_rotation_ack" => {
            require(
                integer(&m["authority_generation"], 2),
                "membership_authority_generation_invalid",
            )?;
            require(digest(&m["transition_digest"]), "membership_digest_invalid")
        }
        _ => Err(ContractError("membership_protocol_invalid")),
    }
}

fn declaration(m: &Value, k: &str) -> Result<(), ContractError> {
    if k == "join_request" {
        require(segment(&m["invite_nonce"]), "membership_identifier_invalid")?;
    } else {
        require(
            segment(&m["previous_incarnation"]),
            "membership_identifier_invalid",
        )?;
        require(
            m["previous_incarnation"] != m["incarnation"],
            "membership_resume_incarnation_invalid",
        )?;
    }
    require(text(&m["software_version"], 128), "membership_text_invalid")?;
    if let Some(e) = m.get("peer_profile") {
        exact(
            e,
            &["protocol", "profile", "signature", "verification_key"],
            "membership_peer_profile_invalid",
        )?;
        require(
            e["protocol"] == "mycelium.membership.peer_profile.v2" && e["profile"].is_object(),
            "membership_peer_profile_invalid",
        )?;
        for (f, other) in [
            ("node_id", "sender_node_id"),
            ("swarm_id", "swarm_id"),
            ("incarnation", "incarnation"),
        ] {
            require(
                e["profile"][f] == m[other],
                "membership_peer_profile_binding_mismatch",
            )?;
        }
    } else {
        peer_runtime_is_activation_eligible(&m["peer_class"], &m["runtime_capability"])?;
    }
    exact(
        &m["endpoint_addr"],
        &["id", "addrs"],
        "membership_endpoint_addr_invalid",
    )?;
    require(
        m["endpoint_addr"]["id"] == m["sender_endpoint_id"],
        "membership_endpoint_id_mismatch",
    )?;
    require(
        m["endpoint_addr"]["addrs"]
            .as_array()
            .is_some_and(|a| !a.is_empty() && a.len() <= 16 && a.iter().all(|v| text(v, 1024))),
        "membership_endpoint_addr_invalid",
    )
}

fn safe_in(value: &Value, choices: &[&str], code: &'static str) -> Result<(), ContractError> {
    require(
        !value.is_array() && !value.is_object(),
        "membership_field_unusable",
    )?;
    require(one_of(value, choices), code)
}

fn heartbeat(m: &Value) -> Result<(), ContractError> {
    require(
        integer(&m["heartbeat_sequence"], 0),
        "membership_integer_invalid",
    )?;
    safe_in(
        &m["lifecycle_state"],
        &[
            "NEW",
            "CONFIGURED",
            "RUNNING",
            "DRAINING",
            "STOPPING",
            "STOPPED",
        ],
        "membership_lifecycle_invalid",
    )?;
    require(
        m["route_ready"].is_boolean(),
        "membership_route_ready_invalid",
    )?;
    require(
        integer(&m["active_requests"], 0),
        "membership_integer_invalid",
    )?;
    require(m["route_ready"] == false, "membership_route_ready_invalid")?;
    safe_in(
        &m["liveness_source"],
        &["scheduled_heartbeat", "activation_receipt"],
        "membership_liveness_source_invalid",
    )?;
    require(
        if m["liveness_source"] == "scheduled_heartbeat" {
            m["activity_receipt_digest"].is_null() && m["activity_peer_node_id"].is_null()
        } else {
            digest(&m["activity_receipt_digest"]) && segment(&m["activity_peer_node_id"])
        },
        "membership_liveness_shape_invalid",
    )
}

fn assignment_identity(m: &Value) -> Result<(), ContractError> {
    for f in ["deployment_id", "assignment_id"] {
        require(segment(&m[f]), "membership_identifier_invalid")?;
    }
    require(
        integer(&m["deployment_epoch"], 0),
        "membership_integer_invalid",
    )
}
fn assignment_offer(m: &Value, issued: f64, expires: f64) -> Result<(), ContractError> {
    assignment_identity(m)?;
    for f in ["assignment_digest", "stage_pack_digest", "graph_digest"] {
        require(digest(&m[f]), "membership_digest_invalid")?;
    }
    require(
        integer(&m["load_generation"], 1),
        "membership_integer_invalid",
    )?;
    safe_in(
        &m["placement_provenance"],
        &[
            "frozen_fixture",
            "planner_v2",
            "target_local_physical_preload",
        ],
        "membership_placement_provenance_invalid",
    )?;
    let records = m["peer_endpoint_records"]
        .as_array()
        .filter(|a| a.len() <= 256)
        .ok_or(ContractError("membership_peer_endpoint_records_invalid"))?;
    let mut seen = std::collections::BTreeSet::new();
    let mut ordered = Vec::new();
    for r in records {
        exact(
            r,
            &[
                "node_id",
                "endpoint_id",
                "deployment_epoch",
                "membership_generation",
                "valid_from",
                "valid_until",
            ],
            "membership_peer_endpoint_record_invalid",
        )?;
        require(
            segment(&r["node_id"])
                && segment(&r["endpoint_id"])
                && r["node_id"] != m["recipient_node_id"],
            "membership_peer_endpoint_identity_invalid",
        )?;
        let node = r["node_id"]
            .as_str()
            .ok_or(ContractError("membership_peer_endpoint_identity_invalid"))?;
        require(seen.insert(node), "membership_peer_endpoint_duplicate")?;
        ordered.push(node);
        require(
            integer(&r["deployment_epoch"], 1)
                && r["deployment_epoch"] == m["deployment_epoch"]
                && integer(&r["membership_generation"], 1),
            "membership_peer_endpoint_generation_invalid",
        )?;
        let from = number(
            &r["valid_from"],
            "membership_peer_endpoint_validity_invalid",
        )?;
        let until = number(
            &r["valid_until"],
            "membership_peer_endpoint_validity_invalid",
        )?;
        require(
            issued <= from && from < until && until <= expires,
            "membership_peer_endpoint_validity_invalid",
        )?;
    }
    require(
        ordered.windows(2).all(|w| w[0] < w[1]),
        "membership_peer_endpoint_order_invalid",
    )
}

/// `sign_membership_message` (`contracts.py:776-793`): validate one message, require the signer to be its
/// sender endpoint, and wrap it with the signature and the public verification key record.
pub fn sign(
    signer: &identity::Identity,
    message: &Value,
    limits: TimeLimits,
) -> Result<Value, ContractError> {
    validate(message, limits)?;
    require(
        message["sender_endpoint_id"] == signer.endpoint_id(),
        "membership_signer_endpoint_mismatch",
    )?;
    let signature = signer
        .sign(message)
        .map_err(|_| ContractError("membership_signature_invalid"))?;
    Ok(serde_json::json!({
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "message": message,
        "signature": signature,
        "verification_key": signer.public_key_record(),
    }))
}

pub fn verify(
    env: &Value,
    expected: Expected<'_>,
    now: f64,
    limits: TimeLimits,
) -> Result<Value, ContractError> {
    require(
        digest(&Value::String(expected.key_digest.to_owned())),
        "membership_key_pin_invalid",
    )?;
    verify_pinned(env, Some(expected.key_digest), expected, now, limits)
}

/// `verify_join_request`: the envelope is judged against the key record it carries (`expected_key_digest`
/// is `None`). The carried digest only has to be a non-empty string at this point; any other malformation
/// surfaces later, as `membership_verifier_invalid`. `expected.key_digest` is ignored.
pub fn verify_self_pinned(
    env: &Value,
    expected: Expected<'_>,
    now: f64,
    limits: TimeLimits,
) -> Result<Value, ContractError> {
    verify_pinned(env, None, expected, now, limits)
}

fn verify_pinned(
    env: &Value,
    pin: Option<&str>,
    expected: Expected<'_>,
    now: f64,
    limits: TimeLimits,
) -> Result<Value, ContractError> {
    exact(
        env,
        &["protocol", "message", "signature", "verification_key"],
        "membership_envelope_invalid",
    )?;
    require(
        env["protocol"] == SIGNED_MESSAGE_PROTOCOL
            && env["message"].is_object()
            && env["signature"].is_object()
            && env["verification_key"].is_object(),
        "membership_envelope_invalid",
    )?;
    let m = &env["message"];
    validate(m, limits)?;
    require(
        m["protocol"] == expected.protocol,
        "membership_protocol_invalid",
    )?;
    match pin {
        Some(pin) => require(
            env["verification_key"]["verification_key_digest"] == pin,
            "membership_key_pin_mismatch",
        )?,
        None => require(
            env["verification_key"]["verification_key_digest"]
                .as_str()
                .is_some_and(|d| !d.is_empty()),
            "membership_key_pin_invalid",
        )?,
    }
    for (field, pin, code) in [
        ("swarm_id", expected.swarm_id, "membership_swarm_mismatch"),
        (
            "sender_node_id",
            expected.sender_node_id,
            "membership_sender_node_mismatch",
        ),
        (
            "sender_endpoint_id",
            expected.sender_endpoint_id,
            "membership_sender_endpoint_mismatch",
        ),
        (
            "recipient_node_id",
            expected.recipient_node_id,
            "membership_recipient_mismatch",
        ),
    ] {
        if let Some(pin) = pin {
            require(m[field] == pin, code)?;
        }
    }
    identity::load_public_key(&env["verification_key"])
        .map_err(|_| ContractError("membership_verifier_invalid"))?;
    require(now.is_finite(), "membership_time_invalid")?;
    require(
        now <= number(&m["expires_at"], "membership_time_invalid")?,
        "membership_message_expired",
    )?;
    require(
        now + limits.clock_skew_seconds >= number(&m["issued_at"], "membership_time_invalid")?,
        "membership_message_from_future",
    )?;
    identity::verify(m, &env["signature"], &env["verification_key"])
        .map_err(|_| ContractError("membership_signature_invalid"))?;
    Ok(m.clone())
}

/// Exact shared peer-class capability policy; membership is not activation eligibility.
pub fn peer_runtime_is_activation_eligible(
    peer_class: &Value,
    runtime_capability: &Value,
) -> Result<bool, ContractError> {
    require(
        !peer_class.is_array() && !peer_class.is_object(),
        "membership_field_unusable",
    )?;
    let (backend, transport, activation) = match peer_class.as_str() {
        Some("mac_mlx_iroh") => ("mlx", "iroh", true),
        Some("browser_http") => ("browser", "http", false),
        Some("pixel_http") => ("android", "http", false),
        Some("android_termux_iroh") => ("pixel-stdlib", "iroh", true),
        Some("linux_numpy_iroh" | "windows_native_iroh" | "linux_native_iroh") => {
            ("numpy", "iroh", true)
        }
        Some("linux_tbd") => ("tbd", "none", false),
        Some("artifact_source_https") => ("artifact-source", "https", false),
        Some("android_native_iroh") => ("mobile-rust-reference", "iroh", true),
        Some("android_native_qwen2_iroh") => ("mobile-rust-qwen2", "iroh", true),
        _ => return Err(ContractError("membership_peer_class_invalid")),
    };
    let c = runtime_capability;
    exact(
        c,
        &["runtime_backend", "transport", "activation_protocol"],
        "membership_runtime_capability_invalid",
    )?;
    require(
        c["runtime_backend"] == backend
            && c["transport"] == transport
            && if activation {
                c["activation_protocol"] == "mycelium.router_wire.v1"
            } else {
                c["activation_protocol"].is_null()
            },
        "membership_runtime_capability_mismatch",
    )?;
    Ok(matches!(
        peer_class.as_str(),
        Some(
            "mac_mlx_iroh"
                | "linux_numpy_iroh"
                | "android_native_iroh"
                | "windows_native_iroh"
                | "linux_native_iroh"
                | "android_native_qwen2_iroh"
        )
    ))
}
