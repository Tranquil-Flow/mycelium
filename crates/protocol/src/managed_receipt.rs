// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticated managed-client receipt parsing, not mailbox mutation or work
//! authorization. Mirrors the owner client's response boundary in v0.1.4.
use crate::{ContractError, TimeLimits, checks::*};
use serde_json::Value;

#[derive(Clone, Copy, Debug)]
pub enum ReceiptKind {
    Enqueue,
    Poll,
    Ack,
    Result,
}
pub struct ExpectedReceipt<'a> {
    pub kind: ReceiptKind,
    pub owner_key: &'a Value,
    pub request_id: &'a str,
    pub swarm_id: &'a str,
    pub owner_node_id: &'a str,
    /// Independently obtained current member binding, not the receipt's fields.
    pub binding: &'a Value,
    pub native_command: &'a Value,
    pub now: f64,
}
const BINDING: &[&str] = &[
    "target_node_id",
    "target_endpoint_id",
    "target_key_digest",
    "membership_generation",
    "member_incarnation",
    "run_id",
    "deployment_id",
];
const DIAGNOSTICS: &[&str] = &[
    "token_path_policy_expired",
    "token_path_policy_stale",
    "token_path_policy_signature",
    "token_path_policy_mismatch",
    "token_path_policy_unconfigured",
    "token_path_host_pin_stale",
    "token_path_host_pin_invalid",
    "token_path_process_expired",
    "runtime_member_stale",
    "worker_membership_not_current",
];
fn document_digest(v: &Value) -> Result<String, ContractError> {
    Ok(crate::canonical::sha256_prefixed(
        &crate::canonical::to_bytes(v)
            .map_err(|_| ContractError("managed_control_json_invalid"))?,
    ))
}
pub fn verify(
    env: &Value,
    expected: &ExpectedReceipt<'_>,
    limits: TimeLimits,
) -> Result<Value, ContractError> {
    let protocol = match expected.kind {
        ReceiptKind::Enqueue => "mycelium.managed_control.enqueue_receipt.v1",
        ReceiptKind::Poll => "mycelium.managed_control.poll_receipt.v1",
        ReceiptKind::Ack => "mycelium.managed_control.ack_receipt.v1",
        ReceiptKind::Result => "mycelium.managed_control.result_receipt.v1",
    };
    // The existing client pins the owner key, and the signed body's owner
    // endpoint must match the detached signer. It does not pin a second endpoint.
    let endpoint = env["statement"]["owner_endpoint_id"].as_str().unwrap_or("");
    let body = crate::managed::verify(
        env,
        protocol,
        expected.owner_key,
        endpoint,
        expected.now,
        limits,
    )
    .map_err(|e| match e.0 {
        "managed_control_signature_invalid" | "managed_control_key_pin_mismatch" => {
            ContractError("managed_control_response_signature_invalid")
        }
        _ => e,
    })?;
    let c = "managed_control_response_invalid";
    let mut fields = vec![
        "protocol",
        "request_id",
        "swarm_id",
        "owner_node_id",
        "owner_endpoint_id",
        "issued_at",
        "expires_at",
    ];
    fields.extend(BINDING);
    match expected.kind {
        ReceiptKind::Enqueue => fields.extend(["command_id", "command_digest", "state"]),
        ReceiptKind::Result => fields.extend([
            "command_id",
            "command_digest",
            "state",
            "native_response",
            "native_response_digest",
        ]),
        ReceiptKind::Poll => fields.extend([
            "delivery_kind",
            "command_id",
            "claim_token",
            "command_envelope",
        ]),
        ReceiptKind::Ack => fields.extend(["command_id", "state", "native_response_digest"]),
    }
    if matches!(expected.kind, ReceiptKind::Ack | ReceiptKind::Result)
        && body.get("target_error_code").is_some()
    {
        fields.push("target_error_code");
        let disposition = body.get("disposition").unwrap_or(&body["state"]);
        require(
            one_of(&body["target_error_code"], DIAGNOSTICS)
                && disposition == "reconciliation_required"
                && body["native_response"].is_null(),
            "managed_control_target_diagnostic_invalid",
        )?;
    }
    exact(&body, &fields, c)?;
    require(
        body["request_id"] == expected.request_id
            && body["swarm_id"] == expected.swarm_id
            && body["owner_node_id"] == expected.owner_node_id
            && body["owner_endpoint_id"] == env["signature"]["signer_endpoint_id"],
        c,
    )?;
    for f in BINDING {
        require(body[*f] == expected.binding[*f], c)?;
    }
    match expected.kind {
        ReceiptKind::Result => {
            require(
                body["command_id"] == expected.native_command["command_id"]
                    && body["command_digest"] == document_digest(expected.native_command)?,
                c,
            )?;
            require(
                one_of(
                    &body["state"],
                    &[
                        "pending",
                        "claimed",
                        "reconciliation_required",
                        "completed",
                        "failed",
                        "expired",
                    ],
                ),
                c,
            )?;
            if one_of(&body["state"], &["completed", "failed"]) {
                crate::native::validate_response(
                    &body["native_response"],
                    expected.native_command["command_id"]
                        .as_str()
                        .ok_or(ContractError(c))?,
                    body["target_node_id"].as_str().ok_or(ContractError(c))?,
                    body["state"].as_str().ok_or(ContractError(c))?,
                )?;
                require(
                    body["native_response_digest"] == document_digest(&body["native_response"])?,
                    c,
                )?;
            } else {
                require(
                    body["native_response"].is_null() && body["native_response_digest"].is_null(),
                    c,
                )?;
            }
        }
        ReceiptKind::Poll => {
            require(
                one_of(
                    &body["delivery_kind"],
                    &["empty", "command", "reconciliation_required"],
                ),
                c,
            )?;
            if body["delivery_kind"] == "empty" {
                require(
                    body["command_id"].is_null()
                        && body["claim_token"].is_null()
                        && body["command_envelope"].is_null(),
                    c,
                )?;
            } else {
                require(
                    body["command_id"].is_string() && body["claim_token"].is_string(),
                    c,
                )?;
                if body["delivery_kind"] == "command" {
                    require(body["command_envelope"].is_object(), c)?;
                    let cmd = crate::managed::verify(
                        &body["command_envelope"],
                        "mycelium.managed_control.owner_command.v1",
                        expected.owner_key,
                        endpoint,
                        expected.now,
                        limits,
                    )?;
                    require(
                        cmd["native_command"]["command_id"] == body["command_id"]
                            && BINDING.iter().all(|f| cmd[*f] == body[*f]),
                        c,
                    )?;
                    crate::native::validate_command(&cmd["native_command"])?;
                } else {
                    require(body["command_envelope"].is_null(), c)?;
                }
            }
        }
        // v0.1.4 client checks only common binding/closed fields for these two.
        // Do not invent state or command-id policy absent from that boundary.
        ReceiptKind::Ack | ReceiptKind::Enqueue => {}
    }
    Ok(body)
}
