// SPDX-License-Identifier: AGPL-3.0-only
//! Output-only producers from governance, seed inventory and live-route incidents.
//! All source observations are independent trusted inputs; these functions do not
//! run a governance gate, acquire a seed lease, persist authority or qualify a route.
use crate::{
    ContractError, canonical,
    checks::{python_equal, require},
    membership,
};
use serde_json::{Value, json};
use std::collections::VecDeque;
const C: &str = "operator_projection_invalid";
fn field<'a>(v: &'a Value, key: &str) -> Result<&'a Value, ContractError> {
    v.get(key).ok_or(ContractError(C))
}
fn float(v: &Value) -> Result<f64, ContractError> {
    let n = if let Some(b) = v.as_bool() {
        if b { 1.0 } else { 0.0 }
    } else if let Some(s) = v.as_str() {
        crate::seed_transition::numeric_text(s)
            .map_err(|_| ContractError(C))?
            .parse()
            .map_err(|_| ContractError(C))?
    } else {
        v.as_f64().ok_or(ContractError(C))?
    };
    Ok(n)
}
fn int_float(n: f64) -> Result<Value, ContractError> {
    require(n.is_finite(), C)?;
    let s = if n == 0.0 {
        "0".to_owned()
    } else {
        format!("{:.0}", n.trunc())
    };
    serde_json::from_str(&s).map_err(|_| ContractError(C))
}
fn integer(v: &Value) -> Result<Value, ContractError> {
    crate::seed_transition::coerced_integer(v).map_err(|_| ContractError(C))
}
fn len(v: &Value) -> Result<usize, ContractError> {
    match v {
        Value::Array(a) => Ok(a.len()),
        Value::Object(m) => Ok(m.len()),
        Value::String(s) => Ok(s.chars().count()),
        _ => Err(ContractError(C)),
    }
}
fn list(v: &Value) -> Result<Vec<Value>, ContractError> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::Object(m) => Ok(m.keys().map(|k| json!(k)).collect()),
        Value::String(s) => Ok(s.chars().map(|c| json!(c.to_string())).collect()),
        _ => Err(ContractError(C)),
    }
}
// Governance reads permissive source JSON, not signed strict ingress. Retain
// insertion order when Python list(dict) projects exclusions; repeated keys keep
// their first position and last value. This does not change the strict decoder.
struct OrderedFields(Vec<(String, Box<serde_json::value::RawValue>)>);
impl<'de> serde::Deserialize<'de> for OrderedFields {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Fields;
        impl<'de> serde::de::Visitor<'de> for Fields {
            type Value = OrderedFields;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("an object")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> Result<Self::Value, A::Error> {
                let mut fields = Vec::new();
                while let Some(entry) = map.next_entry()? {
                    fields.push(entry);
                }
                Ok(OrderedFields(fields))
            }
        }
        deserializer.deserialize_map(Fields)
    }
}
fn exclusions(ledger: &Value, raw: &[u8]) -> Result<Vec<Value>, ContractError> {
    let value = field(ledger, "release_exclusions")?;
    if !value.is_object() {
        return list(value);
    }
    let fields: OrderedFields = serde_json::from_slice(raw).map_err(|_| ContractError(C))?;
    let (_, raw) = fields
        .0
        .into_iter()
        .rev()
        .find(|(k, _)| k == "release_exclusions")
        .ok_or(ContractError(C))?;
    let fields: OrderedFields = serde_json::from_str(raw.get()).map_err(|_| ContractError(C))?;
    let mut seen = std::collections::BTreeSet::new();
    Ok(fields
        .0
        .into_iter()
        .filter_map(|(k, _)| seen.insert(k.clone()).then_some(Value::String(k)))
        .collect())
}
// json.loads(bytes) detects UTF-8/16/32 independently of the bytes used by the
// source digest. Decode only for parsing; do not normalize the committed input.
fn source_json_text(raw: &[u8]) -> Result<std::borrow::Cow<'_, str>, ContractError> {
    let (width, little, skip) = if raw.starts_with(&[0, 0, 254, 255]) {
        (4, false, 4)
    } else if raw.starts_with(&[255, 254, 0, 0]) {
        (4, true, 4)
    } else if raw.starts_with(&[254, 255]) {
        (2, false, 2)
    } else if raw.starts_with(&[255, 254]) {
        (2, true, 2)
    } else if raw.starts_with(&[239, 187, 191]) {
        (1, false, 3)
    } else if raw.len() >= 4 && raw[0] == 0 {
        if raw[1] == 0 {
            (4, false, 0)
        } else {
            (2, false, 0)
        }
    } else if raw.len() >= 4 && raw[1] == 0 {
        if raw[2] == 0 && raw[3] == 0 {
            (4, true, 0)
        } else {
            (2, true, 0)
        }
    } else if raw.len() == 2 && raw[0] == 0 {
        (2, false, 0)
    } else if raw.len() == 2 && raw[1] == 0 {
        (2, true, 0)
    } else {
        (1, false, 0)
    };
    let bytes = &raw[skip..];
    if width == 1 {
        return std::str::from_utf8(bytes)
            .map(std::borrow::Cow::Borrowed)
            .map_err(|_| ContractError(C));
    }
    require(bytes.len().is_multiple_of(width), C)?;
    let text = if width == 2 {
        char::decode_utf16(bytes.chunks_exact(2).map(|b| {
            if little {
                u16::from_le_bytes([b[0], b[1]])
            } else {
                u16::from_be_bytes([b[0], b[1]])
            }
        }))
        .collect::<Result<String, _>>()
        .map_err(|_| ContractError(C))?
    } else {
        bytes
            .chunks_exact(4)
            .map(|b| {
                let bytes = [b[0], b[1], b[2], b[3]];
                char::from_u32(if little {
                    u32::from_le_bytes(bytes)
                } else {
                    u32::from_be_bytes(bytes)
                })
                .ok_or(ContractError(C))
            })
            .collect::<Result<String, _>>()?
    };
    Ok(std::borrow::Cow::Owned(text))
}
/// Preserve raw file bytes in digests, not reserialized JSON. `observed` carries
/// separately sampled revision/status process receipts, gate result and clock.
pub fn governance(
    ledger_bytes: &[u8],
    manifest_bytes: &[u8],
    observed: &Value,
) -> Result<Value, ContractError> {
    let ledger_text = source_json_text(ledger_bytes)?;
    let manifest_text = source_json_text(manifest_bytes)?;
    let ledger: Value = serde_json::from_str(&ledger_text).map_err(|_| ContractError(C))?;
    let manifest: Value = serde_json::from_str(&manifest_text).map_err(|_| ContractError(C))?;
    let revision = field(observed, "revision")?;
    let status = field(observed, "status")?;
    let gate = field(observed, "gate")?;
    let source = field(revision, "stdout")?
        .as_str()
        .ok_or(ContractError(C))?
        .trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c));
    let valid = python_equal(field(revision, "returncode")?, &json!(0))
        && source.len() == 40
        && source
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    let clean = python_equal(field(status, "returncode")?, &json!(0))
        && !crate::execution_graph::truthy(field(status, "stdout")?);
    Ok(
        json!({"protocol":"mycelium.governance_readiness.v1","observed_at_unix_ms":integer(field(observed,"now")?)?,"source_kind":"source_control","source_commit":if valid{json!(source)}else{Value::Null},"source_worktree_clean":clean,"ledger_protocol":field(&ledger,"protocol")?,"ledger_digest":canonical::sha256_prefixed(ledger_bytes),"contract_manifest_protocol":field(&manifest,"protocol")?,"contract_manifest_digest":canonical::sha256_prefixed(manifest_bytes),"governance_gate_protocol":field(gate,"protocol")?,"governance_gate_ok":field(gate,"ok")?,"authorized_product_action_count":len(field(&ledger,"authorized_product_actions")?)?,"capability_count":len(field(&ledger,"capabilities")?)?,"milestone_count":len(field(&ledger,"milestones")?)?,"release_exclusions":exclusions(&ledger,ledger_text.as_bytes())?,"release_ready":false}),
    )
}
/// Projection of a caller-held durable membership snapshot, not seed authority.
pub fn seed_inventory(
    binding: &Value,
    members: &Value,
    now: &Value,
) -> Result<Value, ContractError> {
    let now = float(now)?;
    let mut rows = Vec::new();
    for member in list(members)? {
        let lifecycle = field(&member, "lifecycle_state")?;
        let fresh = float(field(&member, "lease_expires_at")?)? > now;
        let policy = membership::peer_runtime_is_activation_eligible(
            field(&member, "peer_class")?,
            field(&member, "runtime_capability")?,
        )
        .map_err(|_| ContractError("seed_operator_inventory_invalid"))?;
        // Python set membership itself refuses unhashable lifecycle objects.
        if policy && fresh {
            require(!lifecycle.is_array() && !lifecycle.is_object(), C)?;
        }
        rows.push(json!({"node_id":field(&member,"node_id")?,"peer_class":field(&member,"peer_class")?,"generation":field(&member,"generation")?,"incarnation":field(&member,"incarnation")?,"lifecycle_state":lifecycle,"lease_freshness":if fresh{"fresh"}else{"expired"},"activation_eligible":policy&&fresh&&(lifecycle=="CONFIGURED"||lifecycle=="RUNNING"),"revocation_state":if lifecycle=="STOPPED"{"revoked"}else{"active"}}));
    }
    Ok(
        json!({"protocol":"mycelium.seed_operator_inventory.v1","swarm_id":field(binding,"swarm_id")?,"seed_node_id":field(binding,"seed_node_id")?,"seed_key_digest":field(binding,"seed_key_digest")?,"observed_at_unix_ms":int_float(now*1000.0)?,"members":rows,"route_ready":false}),
    )
}
/// Replay the source incident projection over observed events. Limits must be
/// supplied from the source-specific registry (history entries and Unicode chars).
/// Dedupe intentionally compares the full next reason against the retained,
/// truncated previous reason, preserving the pinned long-reason behavior.
pub fn incidents(
    deployment: &Value,
    events: &Value,
    now: &Value,
    history_entries: usize,
    reason_chars: usize,
) -> Result<Value, ContractError> {
    let mut rows: VecDeque<Value> = VecDeque::new();
    let mut sequence = 0usize;
    for event in list(events)? {
        require(
            event.as_object().is_some_and(|m| {
                m.len() == 3
                    && ["state", "reason", "request_id"]
                        .iter()
                        .all(|k| m.contains_key(*k))
            }),
            C,
        )?;
        if rows.back().is_some_and(|last| {
            ["state", "reason", "request_id"]
                .iter()
                .all(|f| python_equal(&last[*f], &event[*f]))
        }) {
            continue;
        }
        sequence = sequence.checked_add(1).ok_or(ContractError(C))?;
        let reason = match &event["reason"] {
            Value::String(s) => json!(s.chars().take(reason_chars).collect::<String>()),
            Value::Array(a) => json!(a.iter().take(reason_chars).collect::<Vec<_>>()),
            _ => return Err(ContractError(C)),
        };
        let timestamp = if let Some(text) = now.as_str() {
            // The source multiplies time.time() BEFORE int(): an injected string
            // is repeated, not parsed as a float. Preserve that observable boundary.
            integer(&Value::String(text.repeat(1000)))?
        } else if now
            .as_number()
            .is_some_and(|n| !n.as_str().contains(['.', 'e', 'E']))
            || now.is_boolean()
        {
            let n = integer(now)?;
            if n == 0 {
                n
            } else {
                serde_json::from_str(&format!("{n}000")).map_err(|_| ContractError(C))?
            }
        } else {
            int_float(float(now)? * 1000.0)?
        };
        if history_entries > 0 {
            if rows.len() == history_entries {
                rows.pop_front();
            }
            rows.push_back(json!({"protocol":"mycelium.live_route_incident.v1","incident_id":format!("route-incident-{sequence}"),"deployment_id":deployment,"request_id":event["request_id"],"state":event["state"],"reason":reason,"observed_at_unix_ms":timestamp}));
        }
    }
    Ok(json!({"sequence":sequence,"incidents":rows}))
}

/// Exact serialized manifest boundary after the caller's owner-file check.
/// Preserve the source's shape-only field policy: do not invent type constraints.
pub fn backup_manifest(raw: &[u8]) -> Result<Value, ContractError> {
    const ERROR: ContractError = ContractError("seed_operator_backup_invalid");
    let document: Value = serde_json::from_slice(raw).map_err(|_| ERROR)?;
    let keys = [
        "protocol",
        "backup_id",
        "created_at_unix_ms",
        "swarm_id",
        "seed_node_id",
        "seed_key_digest",
        "authority_generation",
        "database_digest",
        "member_count",
    ];
    let map = document.as_object().ok_or(ERROR)?;
    if !(keys.iter().all(|k| map.contains_key(*k))
        && (map.len() == keys.len()
            || (map.len() == keys.len() + 1 && map.contains_key("product_pseudonym_digest"))))
        || document["protocol"] != "mycelium.seed_backup.v1"
        || canonical::to_bytes(&document).map_err(|_| ERROR)? != raw
    {
        return Err(ERROR);
    }
    Ok(document)
}
/// Read-only projection over a caller-held authority snapshot and its independently
/// loaded acknowledgements. This neither starts nor acknowledges/completes rotation.
pub fn seed_rotation_status(
    binding: &Value,
    authority: &Value,
    acknowledgements: &Value,
) -> Result<Value, ContractError> {
    let rotation = field(authority, "rotation")?;
    let mut output = json!({"protocol":"mycelium.seed_operator_rotation.v1","swarm_id":field(binding,"swarm_id")?,"route_ready":false});
    if rotation.is_null() {
        output["event"] = json!("rotation_absent");
        output["seed_key_digest"] = field(binding, "seed_key_digest")?.clone();
        output["authority_generation"] = field(authority, "authority_generation")?.clone();
    } else {
        // Preserve source lookup order and omission of fields on the `rotation_none` path.
        let status = field(rotation, "status")?
            .as_str()
            .ok_or(ContractError(C))?
            .to_lowercase();
        output["event"] = json!(format!("rotation_{status}"));
        for key in [
            "previous_generation",
            "authority_generation",
            "old_seed_key_digest",
            "new_seed_key_digest",
        ] {
            output[key] = field(rotation, key)?.clone();
        }
        output["effective_at_unix_ms"] =
            int_float(float(field(rotation, "effective_at")?)? * 1000.0)?;
        output["overlap_expires_at_unix_ms"] =
            int_float(float(field(rotation, "overlap_expires_at")?)? * 1000.0)?;
        let members=list(acknowledgements)?.into_iter().map(|a|Ok(json!({"node_id":field(&a,"node_id")?,"member_generation":field(&a,"member_generation")?,"acknowledged_at_unix_ms":int_float(float(field(&a,"acknowledged_at")?)?*1000.0)?}))).collect::<Result<Vec<_>,ContractError>>()?;
        output["acknowledged_members"] = json!(members);
    }
    Ok(output)
}

/// Source precondition: call before loading authority or advancing a generation.
pub fn validate_seed_revocation_reason(reason: &Value) -> Result<(), ContractError> {
    require(
        crate::seed_transition::reason_valid(reason),
        "seed_operator_reason_invalid",
    )
}
/// Projection only: the member must come from a separately committed generation advance.
pub fn seed_revocation_projection(
    binding: &Value,
    committed_member: &Value,
    expected_generation: &Value,
    reason: &Value,
    now: &Value,
) -> Result<Value, ContractError> {
    validate_seed_revocation_reason(reason)?;
    Ok(
        json!({"protocol":"mycelium.seed_operator_revocation.v1","swarm_id":field(binding,"swarm_id")?,"seed_key_digest":field(binding,"seed_key_digest")?,"node_id":field(committed_member,"node_id")?,"previous_generation":expected_generation,"generation":field(committed_member,"generation")?,"lifecycle_state":field(committed_member,"lifecycle_state")?,"reason":reason,"revoked_at_unix_ms":int_float(float(now)?*1000.0)?,"route_ready":false}),
    )
}
