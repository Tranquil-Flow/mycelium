// SPDX-License-Identifier: AGPL-3.0-only
//! Closed recovery documents from `mycelium_a6_recovery.py` at f4e366e9.
//! Self-digests and declared state are not owner authority, observed recovery,
//! successful cleanup, permission to retry, or a physical qualification result.
use crate::{ContractError, canonical, checks::*};
use serde_json::Value;
type Result = std::result::Result<(), ContractError>;
const INVALID: &str = "recovery_document_invalid";
const STATES: &[&str] = &[
    "active",
    "failure_observed",
    "checkpoint_frozen",
    "successor_pending",
    "recovery_prefill",
    "cutover_pending",
    "recovered_decode",
    "cleanup",
    "terminal",
    "aborted",
];
fn fields(v: &Value, names: &str) -> Result {
    exact(v, &names.split_whitespace().collect::<Vec<_>>(), INVALID)
}
fn bounded(v: &Value, max: usize) -> bool {
    v.as_str()
        .is_some_and(|s| !s.is_empty() && s.chars().count() <= max)
}
fn private(v: &Value) -> Result {
    match v {
        Value::Object(o) => {
            for (k, x) in o {
                require(
                    ![
                        "prompt",
                        "prompt_text",
                        "prompt_bytes",
                        "encoded_prompt",
                        "prefix",
                        "prefix_text",
                        "prefix_tokens",
                        "token",
                        "tokens",
                        "token_ids",
                        "token_text",
                        "output",
                        "output_text",
                        "response",
                        "response_text",
                        "logits",
                        "tensor",
                        "tensors",
                        "kv",
                        "kv_bytes",
                        "activations",
                        "credential",
                        "secret",
                        "private_key",
                        "endpoint",
                    ]
                    .contains(&k.to_lowercase().as_str()),
                    INVALID,
                )?;
                private(x)?;
            }
        }
        Value::Array(a) => {
            for x in a {
                private(x)?;
            }
        }
        _ => {}
    }
    Ok(())
}
fn binding(v: &Value) -> Result {
    fields(
        v,
        "deployment_id deployment_epoch topology_generation model_id model_revision representation_digest graph_digest",
    )?;
    require(
        ["deployment_id", "model_id", "model_revision"]
            .iter()
            .all(|f| bounded(&v[*f], 128))
            && ["representation_digest", "graph_digest"]
                .iter()
                .all(|f| digest(&v[*f]))
            && ["deployment_epoch", "topology_generation"]
                .iter()
                .all(|f| integer(&v[*f], 1)),
        INVALID,
    )
}
fn compatibility(v: &Value) -> Result {
    let names = "model_id model_revision representation_digest manifest_digest tokenizer_digest vocabulary_digest special_token_policy_digest architecture_digest context_policy_digest output_semantics_digest generation_settings_digest qualification_parity_policy_digest workload_envelope_digest";
    fields(v, names)?;
    require(
        names.split_whitespace().all(|f| {
            if matches!(f, "model_id" | "model_revision") {
                bounded(&v[f], 128)
            } else {
                digest(&v[f])
            }
        }),
        INVALID,
    )
}
fn unique_texts(v: &Value, min: usize, max: usize) -> bool {
    v.as_array().is_some_and(|a| {
        a.len() >= min
            && a.len() <= max
            && a.iter().all(|v| bounded(v, 128))
            && (0..a.len()).all(|i| !a[..i].contains(&a[i]))
    })
}
fn self_digest(v: &Value, key: &str) -> Result {
    require(digest(&v[key]), INVALID)?;
    let mut body = v.clone();
    body.as_object_mut()
        .ok_or(ContractError(INVALID))?
        .remove(key);
    let bytes = canonical::to_ascii_bytes(&body).map_err(|_| ContractError(INVALID))?;
    require(v[key] == canonical::sha256_prefixed(&bytes), INVALID)
}
fn intent(v: &Value) -> Result {
    fields(
        v,
        "protocol generated_at_unix_ms binding retained_path_ids candidate_path_ids hysteresis rejection_reasons breaker route_ready intent_digest",
    )?;
    require(
        v["route_ready"] == false && integer(&v["generated_at_unix_ms"], 1),
        INVALID,
    )?;
    binding(&v["binding"])?;
    require(
        [
            "retained_path_ids",
            "candidate_path_ids",
            "rejection_reasons",
        ]
        .iter()
        .all(|f| unique_texts(&v[*f], 0, 64)),
        INVALID,
    )?;
    fields(
        &v["hysteresis"],
        "equivalent_candidate_generations candidate_first_seen_unix_ms state",
    )?;
    require(
        one_of(
            &v["hysteresis"]["state"],
            &[
                "hysteresis_pending",
                "stable_candidate",
                "emergency_candidate",
            ],
        ),
        INVALID,
    )?;
    fields(&v["breaker"], "state")?;
    require(one_of(&v["breaker"]["state"], &["closed", "open"]), INVALID)?;
    self_digest(v, "intent_digest")
}
fn qualification(v: &Value) -> Result {
    fields(
        v,
        "protocol qualification_id binding request_binding successor_binding compatibility compatible freshness parity resource route fallback physical_challenge issued_at_unix_ms expiry_unix_ms reason_codes qualification_digest",
    )?;
    require(bounded(&v["qualification_id"], 128), INVALID)?;
    binding(&v["binding"])?;
    compatibility(&v["compatibility"])?;
    let r = &v["request_binding"];
    fields(
        r,
        "request_id source_attempt committed_watermark recovery_attempt owner_qualification_digest checkpoint_digest",
    )?;
    require(
        bounded(&r["request_id"], 128)
            && integer(&r["source_attempt"], 1)
            && integer(&r["committed_watermark"], 0)
            && integer(&r["recovery_attempt"], 0)
            && digest(&r["owner_qualification_digest"])
            && digest(&r["checkpoint_digest"]),
        INVALID,
    )?;
    let s = &v["successor_binding"];
    fields(
        s,
        "request_id request_attempt path_id path_attempt path_manifest_digest placement_ids",
    )?;
    require(
        bounded(&s["request_id"], 128)
            && bounded(&s["path_id"], 128)
            && integer(&s["request_attempt"], 1)
            && integer(&s["path_attempt"], 0)
            && digest(&s["path_manifest_digest"])
            && unique_texts(&s["placement_ids"], 1, 64),
        INVALID,
    )?;
    require(
        v["compatible"].is_boolean()
            && one_of(
                &v["physical_challenge"],
                &["unexecuted", "passed", "failed"],
            ),
        INVALID,
    )?;
    for (f, good) in [
        ("freshness", "current"),
        ("parity", "exact"),
        ("resource", "reserved"),
        ("route", "current"),
        ("fallback", "none"),
    ] {
        require(one_of(&v[f], &[good, "unverified"]), INVALID)?;
        if v["physical_challenge"] != "passed" {
            require(v[f] == "unverified", INVALID)?;
        } else if v["compatible"] == true {
            require(v[f] == good, INVALID)?;
        }
    }
    require(
        integer(&v["issued_at_unix_ms"], 1)
            && integer(&v["expiry_unix_ms"], 1)
            && greater(&v["expiry_unix_ms"], &v["issued_at_unix_ms"]),
        INVALID,
    )?;
    require(
        v["reason_codes"]
            .as_array()
            .is_some_and(|a| a.iter().all(Value::is_string)),
        INVALID,
    )?;
    self_digest(v, "qualification_digest")
}
fn greater(a: &Value, b: &Value) -> bool {
    let a = a.to_string();
    let b = b.to_string();
    a.len() > b.len() || a.len() == b.len() && a > b
}
fn runtime(v: &Value) -> Result {
    fields(v, "protocol binding requests breaker runtime_digest")?;
    let b = &v["binding"];
    fields(
        b,
        "deployment_id deployment_epoch qualification_digest a4_atomic_commit",
    )?;
    require(
        bounded(&b["deployment_id"], 128)
            && integer(&b["deployment_epoch"], 1)
            && digest(&b["qualification_digest"])
            && (b["a4_atomic_commit"].is_null()
                || b["a4_atomic_commit"]
                    .as_str()
                    .is_some_and(|s| s.chars().count() == 40)),
        INVALID,
    )?;
    let requests = v["requests"]
        .as_array()
        .filter(|a| a.len() <= 4096)
        .ok_or(ContractError(INVALID))?;
    let mut seen = std::collections::BTreeSet::new();
    for r in requests {
        fields(
            r,
            "request_id attempt path_id path_attempt phase committed_token_count recovery_attempt terminal cleanup_complete reconciled",
        )?;
        require(
            bounded(&r["request_id"], 128)
                && bounded(&r["path_id"], 128)
                && integer(&r["attempt"], 1)
                && integer(&r["path_attempt"], 0)
                && integer(&r["committed_token_count"], 0)
                && integer(&r["recovery_attempt"], 0)
                && one_of(&r["phase"], STATES)
                && (r["terminal"].is_null() || bounded(&r["terminal"], 64))
                && r["cleanup_complete"].is_boolean()
                && (r["reconciled"].is_null()
                    || one_of(
                        &r["reconciled"],
                        &["resumed", "aborted", "already_terminal"],
                    )),
            INVALID,
        )?;
        require(
            seen.insert(r["request_id"].as_str().ok_or(ContractError(INVALID))?),
            INVALID,
        )?;
    }
    fields(
        &v["breaker"],
        "state failure_observations_unix_ms open_until_unix_ms",
    )?;
    require(one_of(&v["breaker"]["state"], &["closed", "open"]), INVALID)?;
    self_digest(v, "runtime_digest")
}
fn gate(v: &Value) -> Result {
    fields(
        v,
        "protocol gate_id scenario_id gate_kind executed passed checks privacy_reduced_projection gate_digest",
    )?;
    require(
        bounded(&v["gate_id"], 128)
            && bounded(&v["scenario_id"], 128)
            && one_of(
                &v["gate_kind"],
                &[
                    "deterministic_positive",
                    "deterministic_negative",
                    "physical_browser_positive",
                    "physical_browser_negative",
                ],
            )
            && v["executed"].is_boolean()
            && v["passed"].is_boolean()
            && v["privacy_reduced_projection"].is_boolean()
            && (v["passed"] != true || v["executed"] == true),
        INVALID,
    )?;
    self_digest(v, "gate_digest")
}
/// The registered checkpoint is a privacy-reduced projection. Its digest binds
/// omitted private tokens and is checked for form only, exactly as the oracle.
fn checkpoint(v: &Value) -> Result {
    fields(
        v,
        "protocol store_identity request_id session_id source_attempt path_id path_digest path_attempt prompt_digest generation_settings_digest committed_token_count committed_prefix_digest delivery_cursor watermark publisher_generation deployment_id deployment_epoch topology_generation source_qualification_digest model_id representation_digest tokenizer_digest recovery_attempt terminal_authority created_at_unix_ms expiry_unix_ms reconciliation_state checkpoint_digest",
    )?;
    require(
        [
            "store_identity",
            "request_id",
            "session_id",
            "path_id",
            "deployment_id",
            "model_id",
        ]
        .iter()
        .all(|f| bounded(&v[*f], 128)),
        INVALID,
    )?;
    require(
        [
            "path_digest",
            "prompt_digest",
            "generation_settings_digest",
            "representation_digest",
            "tokenizer_digest",
            "checkpoint_digest",
            "source_qualification_digest",
        ]
        .iter()
        .all(|f| digest(&v[*f]))
            && (v["committed_prefix_digest"].is_null() || digest(&v["committed_prefix_digest"])),
        INVALID,
    )?;
    require(
        [
            "source_attempt",
            "deployment_epoch",
            "topology_generation",
            "created_at_unix_ms",
            "expiry_unix_ms",
        ]
        .iter()
        .all(|f| integer(&v[*f], 1))
            && [
                "path_attempt",
                "recovery_attempt",
                "committed_token_count",
                "watermark",
                "publisher_generation",
            ]
            .iter()
            .all(|f| integer(&v[*f], 0)),
        INVALID,
    )?;
    require(
        (integer(&v["delivery_cursor"], 0) || v["delivery_cursor"].as_i64() == Some(-1))
            && greater(&v["expiry_unix_ms"], &v["created_at_unix_ms"])
            && one_of(
                &v["reconciliation_state"],
                &["active", "frozen", "reconciled"],
            )
            && (v["terminal_authority"].is_null() || bounded(&v["terminal_authority"], 64)),
        INVALID,
    )
}
/// Validate the declared schema and self-digest, not the truth of its claims.
pub fn validate_as(protocol: &str, v: &Value) -> Result {
    let (code, validate): (&str, fn(&Value) -> Result) = match protocol {
        "mycelium.private_request_replay_checkpoint.v1" => {
            ("checkpoint_projection_invalid", checkpoint)
        }
        "mycelium.full_context_recovery_intent.v1" => ("recovery_intent_invalid", intent),
        "mycelium.full_context_recovery_qualification.v1" => {
            ("recovery_qualification_invalid", qualification)
        }
        "mycelium.full_context_recovery_runtime.v1" => ("recovery_runtime_invalid", runtime),
        "mycelium.full_context_recovery_gate.v1" => ("recovery_gate_invalid", gate),
        _ => return Err(ContractError("unsupported_contract_protocol")),
    };
    (|| {
        require(v.get("protocol").is_some_and(|p| p == protocol), INVALID)?;
        private(v)?;
        validate(v)
    })()
    .map_err(|_| ContractError(code))
}
