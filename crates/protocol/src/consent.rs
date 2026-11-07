// SPDX-License-Identifier: AGPL-3.0-only
//! Pure standing-consent caps and admission predicates, not an authority token.
//! Source: mycelium_a13_macos/standing_consent.py:93-111,191-211 and
//! model_resources.py:424-427 @ f4e366e9. L6 owns grant persistence, membership
//! locking, current resource observations, withdrawal and mailbox transitions.
use crate::{ContractError, checks::*, execution_graph::numeric_cmp, public::fields};
use serde_json::{Map, Value};
use std::cmp::Ordering;

fn signed_integer(v: &Value) -> bool {
    v.as_number().is_some_and(|n| {
        let s = n.to_string();
        let d = s.strip_prefix('-').unwrap_or(&s);
        !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit())
    })
}
fn in_range(v: &Value, min: u64, max: u64) -> bool {
    v.as_u64().is_some_and(|n| (min..=max).contains(&n))
}
/// Validate caller-selected caps against its policy and optional measured RAM.
/// Source time values below are allowed wire cap ranges, not operational timers.
pub fn caps(
    v: &Value,
    policy: &Value,
    device_memory_bytes: &Value,
) -> Result<Value, ContractError> {
    let c = "standing_consent_caps_invalid";
    fields(
        v,
        "storage_bytes max_memory_bytes max_wall_seconds max_commands",
        c,
    )?;
    require(
        policy.is_object()
            && signed_integer(&policy["storage_bytes"])
            && integer(&v["storage_bytes"], 100_000_000)
            && numeric_cmp(&v["storage_bytes"], &policy["storage_bytes"])
                .is_some_and(|o| o != Ordering::Greater)
            && in_range(
                &v["max_memory_bytes"],
                256 * 1024 * 1024,
                512 * 1024 * 1024 * 1024,
            )
            && in_range(&v["max_wall_seconds"], 60, 3600)
            && in_range(&v["max_commands"], 1, 10000),
        c,
    )?;
    if integer(device_memory_bytes, 1) {
        // An out-of-u64 positive integer is necessarily above the fixed cap.
        if let Some(device) = device_memory_bytes.as_u64() {
            let requested = v["max_memory_bytes"].as_u64().ok_or(ContractError(c))?;
            require(
                u128::from(requested) * 4 <= u128::from(device) * 3,
                "standing_consent_memory_exceeds_device",
            )?;
        }
    }
    Ok(v.clone())
}
fn member<'a>(v: &'a Value, key: &str) -> Result<&'a Value, ContractError> {
    v.as_object()
        .and_then(|o| o.get(key))
        .ok_or(ContractError("standing_consent_context_invalid"))
}
// Preserve dict(binding) at the source predicate boundary. Normal callers supply
// a mapping; malformed/non-string-key iterables are never identity authority.
fn mapping_equal(binding: &Value, expected: &Value) -> Result<bool, ContractError> {
    let c = "standing_consent_context_invalid";
    if binding.is_object() {
        return Ok(python_equal(binding, expected));
    }
    let values: Vec<Value> = match binding {
        Value::Array(a) => a.clone(),
        Value::String(s) => s.chars().map(|c| Value::from(c.to_string())).collect(),
        _ => return Err(ContractError(c)),
    };
    let mut out = Map::new();
    let mut non_string = false;
    for pair in values {
        let pair = match pair {
            Value::Array(a) => a,
            Value::String(s) => s.chars().map(|c| Value::from(c.to_string())).collect(),
            _ => return Err(ContractError(c)),
        };
        require(
            pair.len() == 2 && !pair[0].is_object() && !pair[0].is_array(),
            c,
        )?;
        if let Some(k) = pair[0].as_str() {
            out.insert(k.to_owned(), pair[1].clone());
        } else {
            non_string = true;
        }
    }
    Ok(!non_string && python_equal(&Value::Object(out), expected))
}
/// `Ok(None)` is the pure automatic-admission predicate, not execution approval.
/// Grant and binding must be current caller-owned state under L6's locks. A
/// returned reason is a refusal; malformed predicate context is an explicit Err.
pub fn standing_admission(
    grant: &Value,
    binding: &Value,
    offer: &Value,
    resource_violation: Option<&str>,
) -> Result<Option<String>, ContractError> {
    let refusal = |s: &str| Ok(Some(s.to_owned()));
    if grant.is_null() {
        return refusal("standing_consent_missing");
    }
    let state = member(grant, "state")?;
    if state == "withdrawn" {
        return refusal("standing_consent_withdrawn");
    }
    if state != "granted" || member(grant, "binding")?.is_null() {
        return refusal("standing_consent_unbound");
    }
    if binding.is_null() {
        return refusal("membership_required");
    }
    if !mapping_equal(binding, member(grant, "binding")?)? {
        return refusal("standing_consent_owner_mismatch");
    }
    require(offer.is_object(), "standing_consent_context_invalid")?;
    if !python_equal(&offer["target_node_id"], member(binding, "node_id")?) {
        return refusal("standing_offer_not_for_this_member");
    }
    if !signed_integer(&offer["archive_bytes"]) {
        return refusal("standing_offer_exceeds_caps");
    }
    let storage = member(member(grant, "caps")?, "storage_bytes")?;
    let compare = numeric_cmp(&offer["archive_bytes"], storage)
        .ok_or(ContractError("standing_consent_context_invalid"))?;
    if compare == Ordering::Greater {
        return refusal("standing_offer_exceeds_caps");
    }
    Ok(resource_violation
        .filter(|s| !s.is_empty())
        .map(str::to_owned))
}
