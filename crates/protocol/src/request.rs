// SPDX-License-Identifier: AGPL-3.0-only
//! Frozen public request/event wire shapes. No admission or terminal state owner.
use crate::{ContractError, checks::*};
use serde_json::Value;
fn nonempty(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}
fn request_id(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 128
            && s.as_bytes()[0].is_ascii_alphanumeric()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._~-".contains(&b))
    })
}
fn error_code(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 64
            && s.as_bytes()[0].is_ascii_lowercase()
            && s.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
    })
}
fn qualification(v: &Value) -> Result<(), ContractError> {
    let c = "invalid_qualification_binding";
    exact(
        v,
        &[
            "qualification_id",
            "qualification_digest",
            "deployment_id",
            "deployment_epoch",
            "topology_version",
            "model_id",
            "resolved_commit",
            "manifest_digest",
            "path_manifest_digest",
            "stage_load_proof_digests",
        ],
        c,
    )?;
    let stages = v["stage_load_proof_digests"]
        .as_array()
        .ok_or(ContractError(c))?;
    for f in [
        "qualification_id",
        "deployment_id",
        "model_id",
        "resolved_commit",
    ] {
        require(nonempty(&v[f]), c)?;
    }
    for f in [
        "qualification_digest",
        "manifest_digest",
        "path_manifest_digest",
    ] {
        require(digest(&v[f]), c)?;
    }
    require(
        stages.iter().all(digest)
            && (0..stages.len()).all(|i| !stages[..i].contains(&stages[i]))
            && integer(&v["deployment_epoch"], 0)
            && integer(&v["topology_version"], 0),
        c,
    )
}
pub fn submission(v: &Value) -> Result<(), ContractError> {
    let c = "invalid_submission";
    require(v.is_object(), c)?;
    let v2 = v["protocol"] == "mycelium.request_gateway.v2";
    let mut fields = vec!["protocol", "prompt", "max_new_tokens", "qualification"];
    if v2 {
        fields.extend(["workload_profile_id", "qos_class"]);
        if v.get("speculation_preference").is_some() {
            require(
                !v["speculation_preference"].is_null(),
                "invalid_speculation_preference",
            )?;
            fields.push("speculation_preference");
        }
    }
    exact(v, &fields, c)?;
    require(
        v2 || v["protocol"] == "mycelium.request_gateway.v1",
        "unsupported_request_protocol",
    )?;
    qualification(&v["qualification"])?;
    require(nonempty(&v["prompt"]), "invalid_prompt")?;
    require(
        v["prompt"].as_str().is_some_and(|s| s.len() <= 131072),
        "prompt_too_large",
    )?;
    require(
        v["max_new_tokens"]
            .as_u64()
            .is_some_and(|n| (1..=4096).contains(&n)),
        "invalid_max_new_tokens",
    )?;
    if let Some(s) = v.get("speculation_preference") {
        require(
            one_of(s, &["target_only", "speculative_requested"]),
            "invalid_speculation_preference",
        )?;
    }
    if v2 {
        require(
            request_id(&v["workload_profile_id"])
                && one_of(&v["qos_class"], &["interactive", "batch"]),
            "invalid_workload_binding",
        )?;
    }
    Ok(())
}
pub fn event(v: &Value) -> Result<(), ContractError> {
    let c = "invalid_stream_event";
    require(
        v.is_object()
            && one_of(
                &v["protocol"],
                &["mycelium.request_event.v1", "mycelium.request_event.v2"],
            ),
        c,
    )?;
    let v2 = v["protocol"] == "mycelium.request_event.v2";
    let kind = v["type"].as_str().ok_or(ContractError(c))?;
    let mut fields = vec![
        "protocol",
        "request_id",
        "sequence",
        "type",
        "publisher_generation",
    ];
    match kind {
        "token" => {
            fields.extend(["token_index", "text"]);
            if v.get("token_id").is_some() {
                require(v2 && integer(&v["token_id"], 0), c)?;
                fields.push("token_id");
            }
        }
        "failed" => fields.push("code"),
        "lifecycle" => fields.push("phase"),
        _ => {}
    }
    exact(v, &fields, c)?;
    require(
        nonempty(&v["request_id"])
            && integer(&v["publisher_generation"], 0)
            && integer(&v["sequence"], 0),
        c,
    )?;
    match kind {
        "token" => require(integer(&v["token_index"], 0) && v["text"].is_string(), c),
        "failed" => require(error_code(&v["code"]), c),
        "lifecycle" => require(
            v2 && one_of(
                &v["phase"],
                &[
                    "admission",
                    "queue",
                    "prefill",
                    "first_token",
                    "decode",
                    "completion",
                ],
            ),
            c,
        ),
        "accepted" | "completed" | "cancelled" => Ok(()),
        _ => Err(ContractError(c)),
    }
}
