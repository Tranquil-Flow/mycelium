// SPDX-License-Identifier: AGPL-3.0-only
//! Signed, expiring, privacy-reduced member model inventories, byte-faithful to v0.1.4
//! `mycelium_member_models.py` (desktop serving -> `reconcile_member_model_catalog` ->
//! `validate_member_model_inventory`).
//!
//! The catalog document itself (`mycelium_model_catalog.catalog_document`) belongs to the
//! model-catalog owner: [`reconcile_member_discovery`] returns exactly the discovery bundle the
//! v0.1.4 reconcile passes to it (`discovered_entries`, `discovery`, `entry_discovery`).
use crate::identity::{self, Identity};
use crate::{canonical, invite::py_space};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

pub const MEMBER_MODEL_INVENTORY_PROTOCOL: &str = "mycelium.member_model_inventory.v1";
pub const MEMBER_MODEL_INVENTORY_STATEMENT_PROTOCOL: &str =
    "mycelium.member_model_inventory_statement.v1";

const ENVELOPE_FIELDS: [&str; 4] = ["protocol", "statement", "signature", "verification_key"];
const STATEMENT_FIELDS: [&str; 7] = [
    "protocol",
    "inventory_id",
    "member_id",
    "membership_generation",
    "observed_at_unix_ms",
    "valid_until_unix_ms",
    "entries",
];
const ENTRY_FIELDS: [&str; 15] = [
    "model_id",
    "revision",
    "artifact_digest",
    "state",
    "architecture",
    "adapter_id",
    "checkpoint_format",
    "quantization",
    "num_layers",
    "weight_bytes",
    "exact_tensor_accounting",
    "required_file_count",
    "present_file_count",
    "reasons",
    "serving_representations",
];
const REPRESENTATION_FIELDS: [&str; 7] = [
    "quantization",
    "quantizer",
    "runtime_dtype",
    "resident_weight_bytes",
    "load_peak_weight_bytes",
    "preparation_required",
    "representation_digest",
];
const AUTHORITY_FIELDS: [&str; 3] = ["member_id", "membership_generation", "verification_key"];
const SIGNATURE_FIELDS: [&str; 5] = [
    "algorithm",
    "signer_endpoint_id",
    "verification_key_digest",
    "signed_statement_digest",
    "signature",
];
const OWNER_RECONCILIATION_REASON: &str = "owner_metadata_reconciliation_required";

/// Source limits of `mycelium_member_models.py:80-86` (inventory bounds, not durations).
const MAX_ENTRIES: usize = 256;
const MAX_REASONS: usize = 32;
const MAX_REPRESENTATIONS: usize = 8;
const MAX_TTL_MS: i128 = 86_400_000;
const MAX_FUTURE_SKEW_MS: i128 = 5_000;
const MAX_SAFE_INTEGER: i128 = (1 << 53) - 1;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct MemberModelError(pub &'static str);

const ENTRY_INVALID: MemberModelError = MemberModelError("member_model_inventory_entry_invalid");
const REPRESENTATION_INVALID: MemberModelError =
    MemberModelError("member_model_inventory_representation_invalid");
const STATEMENT_INVALID: MemberModelError =
    MemberModelError("member_model_inventory_statement_invalid");
const AUTHORITY_INVALID: MemberModelError =
    MemberModelError("member_model_inventory_authority_invalid");

fn fail(condition: bool, error: MemberModelError) -> Result<(), MemberModelError> {
    if condition { Ok(()) } else { Err(error) }
}

fn exact_fields(value: &Value, fields: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|f| m.contains_key(*f)))
}

/// `type(value) is int and minimum <= value <= 2**53 - 1` (JSON `1.0`, `1e3` and bools are not ints).
fn integer(value: &Value, minimum: i128) -> Option<i128> {
    let number = value.as_number()?;
    let text = number.to_string();
    if text.bytes().any(|c| matches!(c, b'.' | b'e' | b'E')) {
        return None;
    }
    text.parse::<i128>()
        .ok()
        .filter(|n| (minimum..=MAX_SAFE_INTEGER).contains(n))
}

/// `re.compile(...).fullmatch` for `[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z`.
fn identifier(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        let b = s.as_bytes();
        (1..=256).contains(&b.len())
            && b[0].is_ascii_alphanumeric()
            && b.iter()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b':' | b'/' | b'-'))
    })
}

/// Python `==` between two JSON values where the left is a validated int: an int equals a float
/// of the same value (`3 == 3.0`), nothing else compares equal across types.
fn python_number_eq(left: &Value, right: &Value) -> bool {
    match (integer(left, 0), right) {
        (Some(i), Value::Number(n)) => match integer(right, 0) {
            Some(j) => i == j,
            None => n
                .as_f64()
                .is_some_and(|f| f.fract() == 0.0 && f.abs() < 1.0e30 && f as i128 == i),
        },
        (Some(_), _) => false,
        (None, _) => left == right,
    }
}

fn lower_hex(s: &str) -> bool {
    s.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
}

fn digest(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        s.strip_prefix("sha256:")
            .is_some_and(|h| h.len() == 64 && lower_hex(h))
    })
}

fn revision(value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|s| s.len() == 40 && lower_hex(s))
}

/// `[a-z][a-z0-9_:>.=-]{0,127}\Z`.
fn reason(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        let b = s.as_bytes();
        (1..=128).contains(&b.len())
            && b[0].is_ascii_lowercase()
            && b.iter().all(|c| {
                c.is_ascii_lowercase()
                    || c.is_ascii_digit()
                    || matches!(c, b'_' | b':' | b'>' | b'.' | b'=' | b'-')
            })
    })
}

/// `str(value)` for the JSON scalars a catalog reason can carry; containers and floats of a
/// producer-side projection are refused (the oracle prints a Python `repr`).
fn python_str(value: &Value) -> Result<String, MemberModelError> {
    match value {
        Value::String(s) => Ok(s.clone()),
        Value::Null => Ok("None".to_owned()),
        Value::Bool(true) => Ok("True".to_owned()),
        Value::Bool(false) => Ok("False".to_owned()),
        Value::Number(n) => {
            if let Some(f) = n
                .as_f64()
                .filter(|_| n.to_string().contains(['.', 'e', 'E']))
            {
                canonical::python_float_repr(f).map_err(|_| ENTRY_INVALID)
            } else {
                Ok(n.to_string())
            }
        }
        _ => Err(ENTRY_INVALID),
    }
}

/// `_safe_reason`.
fn safe_reason(value: &Value) -> Result<String, MemberModelError> {
    let lowered = python_str(value)?.trim_matches(py_space).to_lowercase();
    let mut normalized = String::new();
    let mut in_run = false;
    for c in lowered.chars() {
        if matches!(c, 'a'..='z' | '0'..='9' | '_' | ':' | '>' | '.' | '=' | '-') {
            normalized.push(c);
            in_run = false;
        } else if !in_run {
            normalized.push('_');
            in_run = true;
        }
    }
    let mut normalized = normalized.trim_matches('_').to_owned();
    if !normalized
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_lowercase())
    {
        normalized = format!(
            "member_{}",
            if normalized.is_empty() {
                "unspecified"
            } else {
                &normalized
            }
        );
    }
    let truncated: String = normalized.chars().take(128).collect();
    let trimmed = truncated.trim_end_matches('_');
    Ok(if trimmed.is_empty() {
        "member_unspecified".to_owned()
    } else {
        trimmed.to_owned()
    })
}

fn string_set_sorted_unique(values: &[Value]) -> Option<bool> {
    let strings: Option<Vec<&str>> = values.iter().map(Value::as_str).collect();
    strings.map(|s| s.windows(2).all(|w| w[0] < w[1]))
}

/// `_entry`: one closed, bounded inventory entry.
fn entry(value: &Value) -> Result<Value, MemberModelError> {
    fail(exact_fields(value, &ENTRY_FIELDS), ENTRY_INVALID)?;
    let item = value;
    fail(identifier(&item["model_id"]), ENTRY_INVALID)?;
    fail(revision(&item["revision"]), ENTRY_INVALID)?;
    fail(digest(&item["artifact_digest"]), ENTRY_INVALID)?;
    fail(
        matches!(
            item["state"].as_str(),
            Some("incomplete" | "discovered" | "compatible")
        ),
        ENTRY_INVALID,
    )?;
    for field in ["architecture", "checkpoint_format", "quantization"] {
        fail(identifier(&item[field]), ENTRY_INVALID)?;
    }
    fail(
        item["adapter_id"].is_null() || identifier(&item["adapter_id"]),
        ENTRY_INVALID,
    )?;
    fail(
        item["num_layers"].is_null() || integer(&item["num_layers"], 1).is_some(),
        ENTRY_INVALID,
    )?;
    let mut counts = [0i128; 3];
    for (slot, field) in
        counts
            .iter_mut()
            .zip(["weight_bytes", "required_file_count", "present_file_count"])
    {
        *slot = integer(&item[field], 0).ok_or(ENTRY_INVALID)?;
    }
    fail(counts[2] <= counts[1], ENTRY_INVALID)?;
    fail(item["exact_tensor_accounting"].is_boolean(), ENTRY_INVALID)?;
    let reasons = item["reasons"].as_array().ok_or(ENTRY_INVALID)?;
    fail(
        reasons.len() <= MAX_REASONS
            && string_set_sorted_unique(reasons) == Some(true)
            && reasons.iter().all(reason),
        ENTRY_INVALID,
    )?;
    let representations = item["serving_representations"]
        .as_array()
        .ok_or(ENTRY_INVALID)?;
    fail(representations.len() <= MAX_REPRESENTATIONS, ENTRY_INVALID)?;
    let mut seen = BTreeSet::new();
    for representation in representations {
        fail(
            exact_fields(representation, &REPRESENTATION_FIELDS),
            REPRESENTATION_INVALID,
        )?;
        for field in ["quantization", "quantizer", "runtime_dtype"] {
            fail(identifier(&representation[field]), REPRESENTATION_INVALID)?;
        }
        for field in ["resident_weight_bytes", "load_peak_weight_bytes"] {
            fail(
                integer(&representation[field], 0).is_some(),
                REPRESENTATION_INVALID,
            )?;
        }
        fail(
            representation["preparation_required"].is_boolean(),
            REPRESENTATION_INVALID,
        )?;
        let id = &representation["representation_digest"];
        fail(
            digest(id) && id.as_str().is_some_and(|s| seen.insert(s.to_owned())),
            REPRESENTATION_INVALID,
        )?;
    }
    let digests: Vec<&str> = representations
        .iter()
        .filter_map(|r| r["representation_digest"].as_str())
        .collect();
    fail(digests.windows(2).all(|w| w[0] <= w[1]), ENTRY_INVALID)?;
    Ok(item.clone())
}

fn identity_key(entry: &Value) -> (String, String) {
    (
        entry["model_id"].as_str().unwrap_or_default().to_owned(),
        entry["revision"].as_str().unwrap_or_default().to_owned(),
    )
}

/// `inventory_entry_from_catalog_projection`: reduce one local catalog projection to the signed
/// discovery contract.
pub fn inventory_entry_from_catalog_projection(
    projection: &Value,
) -> Result<Value, MemberModelError> {
    let object = projection.as_object().ok_or(ENTRY_INVALID)?;
    let get = |key: &str| object.get(key).cloned().unwrap_or(Value::Null);
    let reasons: Vec<Value> = match object.get("reasons") {
        None => Vec::new(),
        Some(Value::Array(items)) => items.clone(),
        Some(Value::String(text)) => text.chars().map(|c| Value::String(c.to_string())).collect(),
        Some(Value::Object(map)) => map.keys().map(|k| Value::String(k.clone())).collect(),
        Some(_) => return Err(ENTRY_INVALID),
    };
    let normalized: BTreeSet<String> = reasons.iter().map(safe_reason).collect::<Result<_, _>>()?;
    let mut representations = match object.get("serving_representations") {
        None => Vec::new(),
        Some(Value::Array(items)) => items.clone(),
        // `sorted()` over an empty string / mapping is an empty list; anything longer yields
        // non-mapping items (an `AttributeError` in the oracle).
        Some(Value::String(text)) if text.is_empty() => Vec::new(),
        Some(Value::Object(map)) if map.is_empty() => Vec::new(),
        Some(_) => return Err(REPRESENTATION_INVALID),
    };
    if representations.iter().any(|r| !r.is_object()) {
        return Err(REPRESENTATION_INVALID);
    }
    // Sorting by `record.get("representation_digest", "")`: a non-text key is a Python sort
    // error or a later `representation_invalid`, never an accepted entry.
    if representations.iter().any(|r| {
        r.get("representation_digest")
            .is_some_and(|d| !d.is_string())
    }) {
        return Err(REPRESENTATION_INVALID);
    }
    representations.sort_by(|a, b| {
        let key = |v: &Value| {
            v.get("representation_digest")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned()
        };
        key(a).cmp(&key(b))
    });
    let mut candidate = Map::new();
    for field in [
        "model_id",
        "revision",
        "artifact_digest",
        "state",
        "architecture",
        "adapter_id",
        "checkpoint_format",
        "quantization",
        "num_layers",
        "weight_bytes",
        "exact_tensor_accounting",
        "required_file_count",
        "present_file_count",
    ] {
        candidate.insert(field.to_owned(), get(field));
    }
    candidate.insert(
        "reasons".to_owned(),
        Value::Array(normalized.into_iter().map(Value::String).collect()),
    );
    candidate.insert(
        "serving_representations".to_owned(),
        Value::Array(representations),
    );
    entry(&Value::Object(candidate))
}

fn sort_key(entry: &Value) -> (String, String) {
    identity_key(entry)
}

/// `_validate_statement`.
fn validate_statement(value: &Value) -> Result<Value, MemberModelError> {
    fail(exact_fields(value, &STATEMENT_FIELDS), STATEMENT_INVALID)?;
    let mut statement = value.clone();
    fail(
        statement["protocol"] == MEMBER_MODEL_INVENTORY_STATEMENT_PROTOCOL,
        STATEMENT_INVALID,
    )?;
    fail(identifier(&statement["inventory_id"]), STATEMENT_INVALID)?;
    fail(identifier(&statement["member_id"]), STATEMENT_INVALID)?;
    fail(
        integer(&statement["membership_generation"], 1).is_some(),
        MemberModelError("member_model_inventory_generation_invalid"),
    )?;
    let time_invalid = MemberModelError("member_model_inventory_time_invalid");
    let observed = integer(&statement["observed_at_unix_ms"], 1).ok_or(time_invalid.clone())?;
    let until = integer(&statement["valid_until_unix_ms"], 1).ok_or(time_invalid.clone())?;
    fail(
        observed < until && until - observed <= MAX_TTL_MS,
        time_invalid,
    )?;
    let entries = statement["entries"]
        .as_array()
        .filter(|e| e.len() <= MAX_ENTRIES)
        .ok_or(MemberModelError("member_model_inventory_limit_exceeded"))?;
    let checked: Vec<Value> = entries.iter().map(entry).collect::<Result<_, _>>()?;
    fail(
        checked
            .windows(2)
            .all(|w| sort_key(&w[0]) <= sort_key(&w[1])),
        MemberModelError("member_model_inventory_entries_unsorted"),
    )?;
    fail(
        checked
            .iter()
            .map(identity_key)
            .collect::<BTreeSet<_>>()
            .len()
            == checked.len(),
        MemberModelError("member_model_inventory_duplicate_identity"),
    )?;
    if let Some(map) = statement.as_object_mut() {
        map.insert("entries".to_owned(), Value::Array(checked));
    }
    Ok(statement)
}

/// `create_member_model_inventory`: a deterministic, signed member inventory.
pub fn create_member_model_inventory(
    member_id: &Value,
    membership_generation: &Value,
    entries: &[Value],
    observed_at_unix_ms: &Value,
    valid_until_unix_ms: &Value,
    signer: &Identity,
) -> Result<Value, MemberModelError> {
    let mut checked: Vec<Value> = entries.iter().map(entry).collect::<Result<_, _>>()?;
    checked.sort_by_key(sort_key);
    fail(
        checked.len() <= MAX_ENTRIES,
        MemberModelError("member_model_inventory_limit_exceeded"),
    )?;
    fail(
        checked
            .iter()
            .map(identity_key)
            .collect::<BTreeSet<_>>()
            .len()
            == checked.len(),
        MemberModelError("member_model_inventory_duplicate_identity"),
    )?;
    let seed = json!({
        "member_id": member_id,
        "membership_generation": membership_generation,
        "observed_at_unix_ms": observed_at_unix_ms,
        "entries": checked,
    });
    let bytes = canonical::to_bytes(&seed).map_err(|_| STATEMENT_INVALID)?;
    let statement = json!({
        "protocol": MEMBER_MODEL_INVENTORY_STATEMENT_PROTOCOL,
        "inventory_id": format!("inventory-{}", &hex::encode(Sha256::digest(&bytes))[..32]),
        "member_id": member_id,
        "membership_generation": membership_generation,
        "observed_at_unix_ms": observed_at_unix_ms,
        "valid_until_unix_ms": valid_until_unix_ms,
        "entries": checked,
    });
    validate_statement(&statement)?;
    let signature = signer.sign(&statement).map_err(|_| STATEMENT_INVALID)?;
    Ok(json!({
        "protocol": MEMBER_MODEL_INVENTORY_PROTOCOL,
        "statement": statement,
        "signature": signature,
        "verification_key": signer.public_key_record(),
    }))
}

/// `validate_member_model_inventory`: signature, current member key/generation, bounded currency.
pub fn validate_member_model_inventory(
    envelope: &Value,
    authority: &Value,
    now_unix_ms: i64,
) -> Result<Value, MemberModelError> {
    fail(
        exact_fields(authority, &AUTHORITY_FIELDS),
        AUTHORITY_INVALID,
    )?;
    let envelope_invalid = MemberModelError("member_model_inventory_envelope_invalid");
    fail(
        exact_fields(envelope, &ENVELOPE_FIELDS),
        envelope_invalid.clone(),
    )?;
    fail(
        envelope["protocol"] == MEMBER_MODEL_INVENTORY_PROTOCOL,
        envelope_invalid,
    )?;
    let statement = validate_statement(&envelope["statement"])?;
    fail(
        statement["member_id"] == authority["member_id"],
        MemberModelError("member_model_inventory_member_mismatch"),
    )?;
    fail(
        python_number_eq(
            &statement["membership_generation"],
            &authority["membership_generation"],
        ),
        MemberModelError("member_model_inventory_generation_mismatch"),
    )?;
    let now = i128::from(now_unix_ms);
    fail(
        (1..=MAX_SAFE_INTEGER).contains(&now),
        MemberModelError("member_model_inventory_clock_invalid"),
    )?;
    let observed = integer(&statement["observed_at_unix_ms"], 1).unwrap_or(i128::MAX);
    let until = integer(&statement["valid_until_unix_ms"], 1).unwrap_or(0);
    fail(
        observed <= now + MAX_FUTURE_SKEW_MS && now < until,
        MemberModelError("member_model_inventory_stale"),
    )?;
    let key = &envelope["verification_key"];
    let signature = &envelope["signature"];
    fail(
        key.is_object() && *key == authority["verification_key"],
        MemberModelError("member_model_inventory_key_mismatch"),
    )?;
    let signature_invalid = MemberModelError("member_model_inventory_signature_invalid");
    fail(
        exact_fields(signature, &SIGNATURE_FIELDS),
        signature_invalid.clone(),
    )?;
    identity::key_record_digest(key).map_err(|_| AUTHORITY_INVALID)?;
    identity::verify(&statement, signature, key).map_err(|_| signature_invalid)?;
    Ok(json!({
        "protocol": MEMBER_MODEL_INVENTORY_PROTOCOL,
        "statement": statement,
        "signature": signature,
        "verification_key": key,
    }))
}

/// `validate_member_authorities`: closed, unique, key-checked member authorities.
pub fn validate_member_authorities(
    values: &[Value],
) -> Result<BTreeMap<String, Value>, MemberModelError> {
    let mut result = BTreeMap::new();
    for value in values {
        fail(exact_fields(value, &AUTHORITY_FIELDS), AUTHORITY_INVALID)?;
        let member = &value["member_id"];
        fail(
            identifier(member) && member.as_str().is_some_and(|m| !result.contains_key(m)),
            AUTHORITY_INVALID,
        )?;
        fail(
            integer(&value["membership_generation"], 1).is_some()
                && value["verification_key"].is_object(),
            AUTHORITY_INVALID,
        )?;
        identity::key_record_digest(&value["verification_key"]).map_err(|_| AUTHORITY_INVALID)?;
        result.insert(
            member.as_str().unwrap_or_default().to_owned(),
            value.clone(),
        );
    }
    Ok(result)
}

/// A local catalog entry as the reconcile reads it (`entry.model_id`, `entry.revision`,
/// `entry.projection()`).
pub struct LocalModel {
    pub model_id: String,
    pub revision: String,
    pub projection: Value,
}

/// The discovery bundle `reconcile_member_model_catalog` hands to `catalog_document`.
pub struct Reconciled {
    pub discovered_entries: Vec<Value>,
    /// In the local entries' first-seen identity order (a Python dict).
    pub entry_discovery: Vec<((String, String), Value)>,
    pub discovery: Value,
}

fn representation_identity(entry: &Value) -> Vec<String> {
    entry["serving_representations"]
        .as_array()
        .map(|records| {
            records
                .iter()
                .map(|r| {
                    r["representation_digest"]
                        .as_str()
                        .unwrap_or_default()
                        .to_owned()
                })
                .collect()
        })
        .unwrap_or_default()
}

fn remote_catalog_projection(entry: &Value, member_count: usize) -> Value {
    let mut reasons: BTreeSet<String> = entry["reasons"]
        .as_array()
        .map(|r| {
            r.iter()
                .filter_map(|x| x.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    reasons.insert(OWNER_RECONCILIATION_REASON.to_owned());
    let reasons: Vec<String> = reasons.into_iter().take(MAX_REASONS).collect();
    json!({
        "model_id": entry["model_id"],
        "revision": entry["revision"],
        "state": "discovered",
        "architecture": entry["architecture"],
        "adapter_id": entry["adapter_id"],
        "checkpoint_format": entry["checkpoint_format"],
        "quantization": entry["quantization"],
        "num_layers": entry["num_layers"],
        "weight_bytes": entry["weight_bytes"],
        "exact_tensor_accounting": entry["exact_tensor_accounting"],
        "required_file_count": entry["required_file_count"],
        "present_file_count": entry["present_file_count"],
        "serving_representations": entry["serving_representations"],
        "reasons": reasons,
        "artifact_digest": entry["artifact_digest"],
        "route_ready": false,
        "qualification_evaluated": false,
        "discovery_scope": ["member_inventory"],
        "discovered_member_count": member_count,
        "metadata_reconciled": false,
        "discovery_blockers": [OWNER_RECONCILIATION_REASON],
    })
}

/// `reconcile_member_model_catalog` up to (excluding) `catalog_document`: merge verified member
/// discovery without making remote bytes selectable.
pub fn reconcile_member_discovery(
    local: &[LocalModel],
    inventories: &[Value],
    authorities: &[Value],
    now_unix_ms: i64,
) -> Result<Reconciled, MemberModelError> {
    let authority_by_member = validate_member_authorities(authorities)?;
    let mut candidates: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    let mut invalid_without_member = 0usize;
    for inventory in inventories {
        match inventory["statement"]["member_id"].as_str() {
            Some(member) => candidates
                .entry(member.to_owned())
                .or_default()
                .push(inventory),
            None => invalid_without_member += 1,
        }
    }
    let mut accepted: BTreeMap<String, Value> = BTreeMap::new();
    let mut blockers: BTreeSet<String> = BTreeSet::new();
    let mut rejected = invalid_without_member;
    if invalid_without_member > 0 {
        blockers.insert("member_inventory_invalid".to_owned());
    }
    for (member, group) in &candidates {
        let Some(authority) = authority_by_member.get(member) else {
            rejected += 1;
            blockers.insert("member_inventory_unknown_member".to_owned());
            continue;
        };
        if group.len() != 1 {
            rejected += 1;
            blockers.insert("member_inventory_duplicate_member".to_owned());
            continue;
        }
        match validate_member_model_inventory(group[0], authority, now_unix_ms) {
            Ok(inventory) => {
                accepted.insert(member.clone(), inventory);
            }
            Err(error) => {
                rejected += 1;
                blockers.insert(error.0.to_owned());
            }
        }
    }

    let mut remote: Vec<((String, String), Vec<Value>)> = Vec::new();
    for inventory in accepted.values() {
        for item in inventory["statement"]["entries"]
            .as_array()
            .into_iter()
            .flatten()
        {
            let key = identity_key(item);
            match remote.iter_mut().find(|(k, _)| *k == key) {
                Some((_, peers)) => peers.push(item.clone()),
                None => remote.push((key, vec![item.clone()])),
            }
        }
    }

    let mut local_by_identity: Vec<((String, String), &LocalModel)> = Vec::new();
    for model in local {
        let key = (model.model_id.clone(), model.revision.clone());
        match local_by_identity.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = model,
            None => local_by_identity.push((key, model)),
        }
    }
    let mut entry_discovery = Vec::new();
    for (key, model) in &local_by_identity {
        let peers = remote
            .iter()
            .position(|(k, _)| k == key)
            .map(|at| remote.remove(at).1)
            .unwrap_or_default();
        let conflicts = peers.iter().any(|record| {
            record["artifact_digest"] != model.projection["artifact_digest"]
                || representation_identity(record) != representation_identity(&model.projection)
        });
        if conflicts {
            blockers.insert("member_inventory_identity_conflict".to_owned());
        }
        let mut scope = vec!["coordinator"];
        if !peers.is_empty() {
            scope.push("member_inventory");
        }
        entry_discovery.push((
            key.clone(),
            json!({
                "discovery_scope": scope,
                "discovered_member_count": peers.len(),
                "metadata_reconciled": !conflicts,
                "discovery_blockers": if conflicts { vec!["member_inventory_identity_conflict"] } else { vec![] },
            }),
        ));
    }

    remote.sort_by(|a, b| a.0.cmp(&b.0));
    let mut discovered = Vec::new();
    for (_, peers) in &remote {
        let first = &peers[0];
        if peers[1..].iter().any(|record| {
            record["artifact_digest"] != first["artifact_digest"]
                || representation_identity(record) != representation_identity(first)
        }) {
            blockers.insert("member_inventory_identity_conflict".to_owned());
            continue;
        }
        discovered.push(remote_catalog_projection(first, peers.len()));
    }
    Ok(Reconciled {
        discovered_entries: discovered,
        entry_discovery,
        discovery: json!({
            "scope": if accepted.is_empty() { "coordinator_only" } else { "coordinator_and_members" },
            "accepted_member_count": accepted.len(),
            "rejected_member_count": rejected,
            "blockers": blockers.into_iter().collect::<Vec<_>>(),
        }),
    })
}
