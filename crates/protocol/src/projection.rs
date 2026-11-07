// SPDX-License-Identifier: AGPL-3.0-only
//! Public evidence envelopes and immutable history derivation. Neither declares
//! observed runtime freshness, qualifies a gate, or authenticates source authority.
use crate::{
    ContractError, canonical,
    checks::*,
    public::{fields, safe_integer},
};
use serde_json::{Value, json};
use std::collections::BTreeMap;
pub const PROJECTION: &str = "mycelium.evidence_projection.v1";
pub const HISTORY: &str = "mycelium.evidence_history.v1";
pub const MAX_HISTORY_RECORDS: usize = 128;
const CAPS: &[&str] = &[
    "route_execution",
    "replicated_serving",
    "scoped_recovery",
    "speculative_decoding",
    "heterogeneous_participation",
    "release_closure",
    "stage_local_kv",
];
type Result<T = ()> = std::result::Result<T, ContractError>;
fn bounded(v: &Value) -> bool {
    v.as_str()
        .is_some_and(|s| !s.is_empty() && s.chars().count() <= 256)
}
fn space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}
fn word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}
fn decimal(c: char) -> bool {
    let n = c as u32;
    crate::seed_transition::DECIMAL_ZEROES
        .iter()
        .any(|z| n >= *z && n - *z < 10)
}
fn private_text(s: &str) -> bool {
    let original: Vec<_> = s.chars().collect();
    let folded: String = original
        .iter()
        .map(|c| match c {
            'İ' | 'ı' => 'i',
            'ſ' => 's',
            'K' => 'k',
            _ => c.to_ascii_lowercase(),
        })
        .collect();
    if folded.contains("http://") || folded.contains("https://") {
        return true;
    }
    for (i, (byte, _)) in folded.char_indices().enumerate() {
        let tail = &folded[byte..];
        if (i == 0 || space(original[i - 1]))
            && ["/users/", "/home/", "/private/", "/tmp/"]
                .iter()
                .any(|p| tail.starts_with(p))
        {
            return true;
        }
        if i > 0 && word(original[i - 1]) {
            continue;
        }
        if tail.starts_with("bearer") {
            let rest: Vec<_> = tail.chars().skip(6).collect();
            let whitespace = rest.iter().take_while(|c| space(**c)).count();
            if whitespace > 0 && whitespace < rest.len() {
                return true;
            }
        }
        let mut at = i;
        let mut matched = true;
        for group in 0..4 {
            let start = at;
            while at < original.len() && at - start < 3 && decimal(original[at]) {
                at += 1;
            }
            if at == start {
                matched = false;
                break;
            }
            if group < 3 {
                if original.get(at) != Some(&'.') {
                    matched = false;
                    break;
                }
                at += 1;
            }
        }
        if matched && original.get(at).is_none_or(|c| !word(*c)) {
            return true;
        }
    }
    false
}
fn privacy(v: &Value) -> Result {
    let code = "evidence_payload_privacy_invalid";
    match v {
        Value::Object(o) => {
            for (k, x) in o {
                require(
                    ![
                        "prompt",
                        "completion",
                        "token_ids",
                        "token_text",
                        "logits",
                        "activations",
                        "kv_contents",
                        "weights",
                        "local_path",
                        "endpoint",
                        "hostname",
                        "ip_address",
                        "credential",
                        "private_key",
                        "invite",
                        "bearer_token",
                        "secret",
                    ]
                    .contains(&k.to_lowercase().as_str()),
                    code,
                )?;
                privacy(x)?;
            }
        }
        Value::Array(a) => {
            for x in a {
                privacy(x)?;
            }
        }
        Value::String(s) => require(!private_text(s), code)?,
        _ => {}
    }
    Ok(())
}
pub fn validate(v: &Value) -> Result {
    fields(
        v,
        "protocol record_id capability source_kind authority generation captured_at_unix_ms observed_at_unix_ms valid_until_unix_ms freshness payload_protocol payload",
        "evidence_projection_shape_invalid",
    )?;
    require(
        v["protocol"] == PROJECTION,
        "evidence_projection_protocol_invalid",
    )?;
    require(bounded(&v["record_id"]), "record_id_invalid")?;
    privacy(&v["record_id"])?;
    require(
        bounded(&v["capability"]) && one_of(&v["capability"], CAPS),
        "capability_invalid",
    )?;
    require(
        bounded(&v["source_kind"])
            && one_of(
                &v["source_kind"],
                &[
                    "live_runtime",
                    "planner_intent",
                    "sealed_historical",
                    "replay",
                    "fixture",
                ],
            ),
        "source_kind_invalid",
    )?;
    require(bounded(&v["freshness"]), "freshness_invalid")?;
    let current = one_of(&v["source_kind"], &["live_runtime", "planner_intent"]);
    require(
        if current {
            one_of(&v["freshness"], &["current", "degraded", "stale"])
        } else {
            v["freshness"]
                == if v["source_kind"] == "sealed_historical" {
                    "historical"
                } else if v["source_kind"] == "replay" {
                    "replay"
                } else {
                    "fixture"
                }
        },
        "source_freshness_mismatch",
    )?;
    require(bounded(&v["authority"]), "authority_invalid")?;
    privacy(&v["authority"])?;
    require(safe_integer(&v["generation"], 0), "generation_invalid")?;
    require(
        safe_integer(&v["captured_at_unix_ms"], 1),
        "captured_at_unix_ms_invalid",
    )?;
    require(
        safe_integer(&v["observed_at_unix_ms"], 1),
        "observed_at_unix_ms_invalid",
    )?;
    require(
        v["captured_at_unix_ms"].as_u64() >= v["observed_at_unix_ms"].as_u64(),
        "capture_precedes_observation",
    )?;
    if current {
        require(
            safe_integer(&v["valid_until_unix_ms"], 1),
            "valid_until_unix_ms_invalid",
        )?;
        require(
            v["valid_until_unix_ms"].as_u64() >= v["captured_at_unix_ms"].as_u64(),
            "evidence_already_expired_at_capture",
        )?;
    } else {
        require(
            v["valid_until_unix_ms"].is_null(),
            "immutable_source_cannot_expire",
        )?;
    }
    require(bounded(&v["payload_protocol"]), "payload_protocol_invalid")?;
    require(
        v["payload"].is_object() && v["payload"]["protocol"] == v["payload_protocol"],
        "payload_protocol_mismatch",
    )?;
    privacy(&v["payload"])
}
/// Normalize source history exactly: validate all immutable inputs, refuse
/// conflicting IDs, deduplicate identical canonical records, cap unique records,
/// then sort by original observation time and ID before optional filtering.
/// This is not a validator invented for an output-only registry document.
pub fn history(records: &[Value], capability: Option<&str>) -> Result<Value> {
    let mut by_id = BTreeMap::new();
    for record in records {
        validate(record)?;
        require(
            record["source_kind"] == "sealed_historical",
            "historical_register_source_invalid",
        )?;
        let id = record["record_id"]
            .as_str()
            .ok_or(ContractError("record_id_invalid"))?;
        let bytes =
            canonical::to_bytes(record).map_err(|_| ContractError("evidence_history_invalid"))?;
        if let Some((old, _)) = by_id.get(id) {
            require(*old == bytes, "historical_record_conflict")?;
        }
        by_id.insert(id, (bytes, record.clone()));
        require(
            by_id.len() <= MAX_HISTORY_RECORDS,
            "evidence_history_invalid",
        )?;
    }
    if let Some(c) = capability {
        require(CAPS.contains(&c), "evidence_capability_invalid")?;
    }
    let mut records: Vec<_> = by_id
        .into_values()
        .map(|(_, v)| v)
        .filter(|v| capability.is_none_or(|c| v["capability"] == c))
        .collect();
    records.sort_by(|a, b| {
        a["observed_at_unix_ms"]
            .as_u64()
            .cmp(&b["observed_at_unix_ms"].as_u64())
            .then_with(|| a["record_id"].as_str().cmp(&b["record_id"].as_str()))
    });
    Ok(json!({"protocol":HISTORY,"records":records}))
}
