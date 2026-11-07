// SPDX-License-Identifier: AGPL-3.0-only
//! Closed native command/progress grammar. Authentication and execution remain
//! distinct: accepting a payload is not permission to run it or credit tokens.
use crate::{ContractError, checks::*};
use serde_json::Value;
use std::collections::BTreeSet;
type Result = std::result::Result<(), ContractError>;
pub const CONTROL_PROTOCOL: &str = "mycelium.physical_node_control.v1";
pub const PROGRESS_PROTOCOL: &str = "mycelium.physical_node_progress.v1";
pub const POLICY_PROTOCOL: &str = "mycelium.token_path_route_policy.v1";
pub const MAX_RECEIPT_BYTES: usize = 1024 * 1024;
fn shape(v: &Value, names: &str) -> bool {
    let fields: Vec<_> = names.split_whitespace().collect();
    v.as_object()
        .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|k| m.contains_key(*k)))
}
fn size(v: &Value, max: usize, code: &'static str) -> Result {
    let b = crate::canonical::to_bytes(v).map_err(|_| ContractError(code))?;
    require(b.len() <= max, code)
}
fn reject_execution(v: &Value) -> Result {
    if let Some(m) = v.as_object() {
        for (k, v) in m {
            let k = k.to_lowercase().replace('-', "_");
            require(
                ![
                    "argv",
                    "cwd",
                    "exec",
                    "executable",
                    "executable_path",
                    "file_path",
                    "path",
                    "python",
                    "python_code",
                    "python_command",
                    "script",
                    "script_path",
                    "shell",
                    "shell_command",
                    "subprocess",
                    "working_directory",
                ]
                .contains(&k.as_str()),
                "managed_control_execution_input_forbidden",
            )?;
            reject_execution(v)?;
        }
    } else if let Some(a) = v.as_array() {
        for v in a {
            reject_execution(v)?;
        }
    }
    Ok(())
}
pub fn validate_command(v: &Value) -> Result {
    let c = "managed_control_native_command_invalid";
    require(
        shape(
            v,
            "protocol command_id run_id deployment_id command payload",
        ),
        c,
    )?;
    size(
        v,
        crate::managed::MAX_FRAME_BYTES,
        "managed_control_frame_too_large",
    )?;
    require(
        v["protocol"] == CONTROL_PROTOCOL
            && ["command_id", "run_id", "deployment_id"]
                .iter()
                .all(|k| segment(&v[*k]))
            && v["command"].is_string(),
        c,
    )?;
    let op = v["command"].as_str().ok_or(ContractError(c))?;
    let shapes: &[&str] = match op {
        "hello" | "health" | "stop" => &[""],
        "configure" => &[
            "assignment_file artifact_report_file graph device_states load_generation",
            "assignment_file manifest_file stage_pack_file graph device_states load_generation",
            "assignment_file pixel_stage_pack_file graph device_states load_generation",
        ],
        "start" => &["peer local_generation", "peer peers local_generation"],
        "snapshot" => &["", "cleanup_subject", "cleanup_subject receipt_only"],
        "install_token_path" => &["policy"],
        "cancel" => &["request_id"],
        "bind_request_control" => &["request_id control", "request_id control token_path"],
        "update_request_control" => &["request_id control"],
        "infer_start" => &[
            "request",
            "request excluded_placement_ids",
            "request control path_manifest",
            "request control path_manifest excluded_placement_ids",
            "request control path_manifest token_grant",
        ],
        "infer_decode" => &["request_id count", "request_id count control"],
        "infer_cancel" => &["request_id", CANCEL_FIELDS],
        "infer_cancel_wait" => &[CANCEL_FIELDS],
        "rotate" => &["peer"],
        _ => return Err(ContractError("managed_control_operation_unsupported")),
    };
    let p = &v["payload"];
    require(p.is_object(), c)?;
    reject_execution(p)?;
    require(shapes.iter().any(|s| shape(p, s)), c)?;
    if op == "install_token_path" {
        validate_policy_payload(p).map_err(|_| ContractError(c))?;
    }
    if op == "bind_request_control" && p.get("token_path").is_some() {
        let t = &p["token_path"];
        require(
            p["request_id"].is_string()
                && p["control"].is_object()
                && shape(t, "request path_manifest token_grant")
                && ["request", "path_manifest", "token_grant"]
                    .iter()
                    .all(|k| t[*k].is_object())
                && t["request"]["request_id"] == p["request_id"]
                && shape(&t["token_grant"], "statement signature"),
            c,
        )?;
        size(&t["token_grant"], 16384, c)?;
    }
    if op == "infer_start" && p.get("token_grant").is_some() {
        require(
            ["request", "control", "path_manifest", "token_grant"]
                .iter()
                .all(|k| p[*k].is_object())
                && shape(&p["token_grant"], "statement signature")
                && p["token_grant"]["statement"].is_object()
                && p["token_grant"]["signature"].is_object(),
            c,
        )?;
        size(&p["token_grant"], 16384, c)?;
    }
    if op == "cancel" {
        require(segment(&p["request_id"]), c)?;
    }
    Ok(())
}
pub const CANCEL_FIELDS: &str = "request_id deployment_id deployment_epoch qualification_digest command_id publisher_generation absolute_deadline_ms request_attempt path_id path_attempt path_digest topology_generation cancellation_generation deadline_budget_ms";
pub fn validate_response(v: &Value, command: &str, node: &str, disposition: &str) -> Result {
    let c = "managed_control_native_response_invalid";
    require(v.is_object(), c)?;
    size(
        v,
        crate::managed::MAX_FRAME_BYTES,
        "managed_control_frame_too_large",
    )?;
    require(
        shape(
            v,
            if v["ok"] == true {
                "protocol command_id node_id ok route_ready result"
            } else {
                "protocol command_id node_id ok route_ready error"
            },
        ) && v["protocol"] == CONTROL_PROTOCOL
            && v["command_id"] == command
            && v["node_id"] == node
            && v["route_ready"] == false
            && v["ok"].is_boolean(),
        c,
    )?;
    if disposition == "completed" {
        require(v["ok"] == true, c)?;
    } else {
        require(v["ok"] == false && shape(&v["error"], "code"), c)?;
        // Python applies str() to the error code; retain numeric/null/bool spellings.
        let code = &v["error"]["code"];
        let rendered = match code {
            Value::String(s) => s.clone(),
            Value::Null => "None".into(),
            Value::Bool(b) => if *b { "True" } else { "False" }.into(),
            _ => code.to_string(),
        };
        require(segment(&Value::String(rendered)), c)?;
    }
    Ok(())
}
pub fn validate_progress(v: &Value, node: &str) -> Result {
    let c = "token_progress_frame_invalid";
    require(
        shape(v, "protocol command_id node_id receipt")
            && v["protocol"] == PROGRESS_PROTOCOL
            && v["node_id"] == node,
        c,
    )?;
    for f in ["command_id", "node_id"] {
        require(
            v[f].as_str().is_some_and(|s| {
                !s.is_empty()
                    && s.len() <= 256
                    && s.as_bytes()[0].is_ascii_alphanumeric()
                    && s.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
            }),
            c,
        )?;
    }
    require(shape(&v["receipt"], "statement signature"), c)?;
    size(&v["receipt"], MAX_RECEIPT_BYTES, c)
}
pub fn validate_policy_payload(v: &Value) -> Result {
    let c = "token_path_policy_invalid";
    require(
        shape(v, "policy") && shape(&v["policy"], "statement signature"),
        c,
    )?;
    let env = &v["policy"];
    size(env, 65536, c)?;
    validate_policy(&env["statement"])?;
    let sig = &env["signature"];
    require(
        shape(
            sig,
            "algorithm signer_endpoint_id verification_key_digest signed_statement_digest signature",
        ) && sig
            .as_object()
            .is_some_and(|m| m.values().all(Value::is_string)),
        c,
    )
}
pub fn validate_policy(v: &Value) -> Result {
    let c = "token_path_policy_invalid";
    require(
        shape(
            v,
            "protocol run_id deployment_id model_digest requester_id owner_endpoint_id members max_new_tokens stop_token_ids expires_at_unix_ms",
        ) && v["protocol"] == POLICY_PROTOCOL,
        c,
    )?;
    for f in [
        "run_id",
        "deployment_id",
        "requester_id",
        "owner_endpoint_id",
    ] {
        require(text(&v[f], 256), c)?;
    }
    require(
        digest(&v["model_digest"])
            && v["max_new_tokens"]
                .as_u64()
                .is_some_and(|n| (1..=65536).contains(&n))
            && integer(&v["expires_at_unix_ms"], 1),
        c,
    )?;
    let stops = v["stop_token_ids"].as_array().ok_or(ContractError(c))?;
    let mut ids = BTreeSet::new();
    require(
        stops.len() <= 128
            && stops.iter().all(|t| {
                t.as_u64()
                    .is_some_and(|n| n <= i32::MAX as u64 && ids.insert(n))
            }),
        c,
    )?;
    let members = v["members"].as_object().ok_or(ContractError(c))?;
    require(!members.is_empty() && members.len() <= 64, c)?;
    let mut endpoints = BTreeSet::new();
    let mut controls = BTreeSet::new();
    for (node, m) in members {
        require(
            text(&Value::String(node.clone()), 256)
                && shape(
                    m,
                    "node_id endpoint_id generation control_endpoint_id verification_key",
                )
                && m["node_id"] == node.as_str()
                && text(&m["endpoint_id"], 256)
                && text(&m["control_endpoint_id"], 256)
                && m["generation"].as_u64().is_some_and(|n| n >= 1),
            c,
        )?;
        crate::identity::load_public_key(&m["verification_key"]).map_err(|_| ContractError(c))?;
        require(
            endpoints.insert(m["endpoint_id"].as_str())
                && controls.insert(m["control_endpoint_id"].as_str()),
            c,
        )?;
    }
    Ok(())
}
