// SPDX-License-Identifier: AGPL-3.0-only
//! Managed control envelope authentication (not mailbox execution).
use crate::ContractError;
use serde_json::Value;

pub const SIGNED_ENVELOPE_PROTOCOL: &str = "mycelium.managed_control.signed_envelope.v1";
/// Frozen v0.1.4 managed-control frame bound, not an operational timeout.
pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;

/// Common request schema for the initial owner-command/target-poll slice.
/// This is deliberately NOT native-command payload or execution authorization.
pub fn validate_request_common(statement: &Value) -> Result<(), ContractError> {
    use crate::checks::*;
    let mut fields = vec![
        "protocol",
        "request_id",
        "swarm_id",
        "sequence",
        "nonce",
        "issued_at",
        "expires_at",
        "target_node_id",
        "target_endpoint_id",
        "target_key_digest",
        "membership_generation",
        "member_incarnation",
        "run_id",
        "deployment_id",
    ];
    match statement["protocol"].as_str() {
        Some("mycelium.managed_control.owner_command.v1") => {
            fields.extend(["owner_node_id", "native_command"]);
        }
        Some("mycelium.managed_control.target_poll.v1") => {}
        Some("mycelium.managed_control.token_progress.v1") => {
            fields.extend(["command_id", "claim_token", "receipt"]);
            for f in ["command_id", "claim_token"] {
                require(segment(&statement[f]), "managed_control_identifier_invalid")?;
            }
        }
        _ => return Err(ContractError("managed_control_protocol_invalid")),
    }
    exact(statement, &fields, "managed_control_fields_invalid")?;
    for f in ["request_id", "swarm_id", "nonce"] {
        require(segment(&statement[f]), "managed_control_identifier_invalid")?;
    }
    require(
        integer(&statement["sequence"], 1),
        "managed_control_sequence_invalid",
    )?;
    validate_binding(statement)
}

/// Validate the seven managed member/run binding fields, allowing unrelated fields.
/// Matches managed_control.py:570-590 at f4e366e9; not request authorization.
pub fn validate_binding(statement: &Value) -> Result<(), ContractError> {
    use crate::checks::*;
    for f in [
        "target_node_id",
        "target_endpoint_id",
        "member_incarnation",
        "run_id",
        "deployment_id",
    ] {
        require(
            segment(&statement[f]),
            "managed_control_member_binding_invalid",
        )?;
    }
    require(
        digest(&statement["target_key_digest"]) && integer(&statement["membership_generation"], 1),
        "managed_control_member_binding_invalid",
    )
}

pub fn verify(
    env: &Value,
    protocol: &str,
    key: &Value,
    endpoint: &str,
    now: f64,
    limits: crate::TimeLimits,
) -> Result<Value, ContractError> {
    use crate::checks::*;
    limits.validate()?;
    exact(
        env,
        &["protocol", "statement", "signature", "verification_key"],
        "managed_control_envelope_invalid",
    )?;
    require(
        env["protocol"] == SIGNED_ENVELOPE_PROTOCOL
            && env["statement"].is_object()
            && env["signature"].is_object()
            && env["verification_key"].is_object(),
        "managed_control_envelope_invalid",
    )?;
    let statement = &env["statement"];
    require(
        statement["protocol"] == protocol,
        "managed_control_protocol_invalid",
    )?;
    require(
        key.is_object() && &env["verification_key"] == key,
        "managed_control_key_pin_mismatch",
    )?;
    require(
        env["signature"]["signer_endpoint_id"] == endpoint,
        "managed_control_signature_invalid",
    )?;
    crate::identity::verify(statement, &env["signature"], key)
        .map_err(|_| ContractError("managed_control_signature_invalid"))?;
    let issued = number(&statement["issued_at"], "managed_control_time_invalid")?;
    let expires = number(&statement["expires_at"], "managed_control_time_invalid")?;
    require(now.is_finite(), "managed_control_clock_invalid")?;
    require(issued < expires, "managed_control_time_invalid")?;
    require(
        expires - issued <= limits.max_ttl_seconds,
        "managed_control_ttl_invalid",
    )?;
    require(
        now <= expires + limits.clock_skew_seconds,
        "managed_control_message_expired",
    )?;
    require(
        now + limits.clock_skew_seconds >= issued,
        "managed_control_message_from_future",
    )?;
    let detached = crate::canonical::to_bytes(statement)
        .map_err(|_| ContractError("managed_control_json_invalid"))?;
    require(
        detached.len() <= MAX_FRAME_BYTES,
        "managed_control_frame_too_large",
    )?;
    Ok(statement.clone())
}
