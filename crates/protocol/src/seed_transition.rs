// SPDX-License-Identifier: AGPL-3.0-only
//! Seed-key continuity proof, not rotation execution or overlap admission.
//! Mirrors operator.py:160–248 at f4e366e9, including numeric coercions and the
//! fact that `now` is validated but NOT compared to the overlap window here.
use crate::{ContractError, checks::*};
use serde_json::Value;
pub const PROTOCOL: &str = "mycelium.seed_key_transition.v1";
// Unicode 16 decimal digits recorded by the pinned external Python oracle.
pub const DECIMAL_ZEROES: &[u32] = &[
    48, 1632, 1776, 1984, 2406, 2534, 2662, 2790, 2918, 3046, 3174, 3302, 3430, 3558, 3664, 3792,
    3872, 4160, 4240, 6112, 6160, 6470, 6608, 6784, 6800, 6992, 7088, 7232, 7248, 42528, 43216,
    43264, 43472, 43504, 43600, 44016, 65296, 66720, 68912, 68928, 69734, 69872, 69942, 70096,
    70384, 70736, 70864, 71248, 71360, 71376, 71386, 71472, 71904, 72016, 72688, 72784, 73040,
    73120, 73552, 90416, 92768, 92864, 93008, 93552, 118000, 120782, 120792, 120802, 120812,
    120822, 123200, 123632, 124144, 124401, 125264, 130032,
];
const INVALID: &str = "seed_operator_rotation_record_invalid";
pub(crate) fn numeric_text(s: &str) -> Result<String, ContractError> {
    let normalized: String = s
        .trim()
        .chars()
        .map(|ch| {
            let n = ch as u32;
            if let Some(z) = DECIMAL_ZEROES.iter().find(|z| n >= **z && n - **z < 10) {
                char::from(b'0' + (n - z) as u8)
            } else {
                ch
            }
        })
        .collect();
    let b = normalized.as_bytes();
    require(
        b.iter().enumerate().all(|(i, c)| {
            *c != b'_'
                || (i > 0
                    && i + 1 < b.len()
                    && b[i - 1].is_ascii_digit()
                    && b[i + 1].is_ascii_digit())
        }),
        INVALID,
    )?;
    Ok(normalized.replace('_', ""))
}
fn float(v: &Value) -> Result<f64, ContractError> {
    let result = if let Some(b) = v.as_bool() {
        Some(f64::from(u8::from(b)))
    } else if let Some(s) = v.as_str() {
        Some(
            numeric_text(s)?
                .parse()
                .map_err(|_| ContractError(INVALID))?,
        )
    } else {
        v.as_f64()
    };
    result
        .filter(|n| n.is_finite())
        .ok_or(ContractError(INVALID))
}
pub(crate) fn coerced_integer(v: &Value) -> Result<Value, ContractError> {
    let text = if let Some(b) = v.as_bool() {
        u8::from(b).to_string()
    } else if let Some(s) = v.as_str() {
        numeric_text(s)?
    } else if let Some(n) = v.as_number() {
        let s = n.to_string();
        if s.contains(['.', 'e', 'E']) {
            format!("{:.0}", float(v)?.trunc())
        } else {
            s
        }
    } else {
        return Err(ContractError(INVALID));
    };
    let negative = text.starts_with('-');
    let digits = text.strip_prefix(['+', '-']).unwrap_or(&text);
    require(
        !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()),
        INVALID,
    )?;
    let digits = digits.trim_start_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let normalized = format!(
        "{}{}",
        if negative && digits != "0" { "-" } else { "" },
        digits
    );
    serde_json::from_str(&normalized).map_err(|_| ContractError(INVALID))
}
pub(crate) fn reason_valid(value: &Value) -> bool {
    value.as_str().is_some_and(|r| {
        !r.is_empty()
            && r.len() <= 64
            && r.as_bytes()[0].is_ascii_lowercase()
            && r.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
    })
}
pub fn verify(
    env: &Value,
    now: &Value,
    expected_old_digest: Option<&str>,
) -> Result<Value, ContractError> {
    exact(
        env,
        &[
            "protocol",
            "transition",
            "old_signature",
            "old_verification_key",
            "new_signature",
            "new_verification_key",
        ],
        INVALID,
    )?;
    require(env["protocol"] == PROTOCOL, INVALID)?;
    let t = &env["transition"];
    exact(
        t,
        &[
            "swarm_id",
            "seed_node_id",
            "previous_generation",
            "authority_generation",
            "old_seed_key_digest",
            "new_seed_key_digest",
            "initiated_at",
            "effective_at",
            "overlap_expires_at",
            "reason",
        ],
        INVALID,
    )?;
    let initiated = float(&t["initiated_at"])?;
    let effective = float(&t["effective_at"])?;
    let expires = float(&t["overlap_expires_at"])?;
    let previous = coerced_integer(&t["previous_generation"])?;
    let generation = coerced_integer(&t["authority_generation"])?;
    require(
        now.is_number()
            && now.as_f64().is_some_and(f64::is_finite)
            && next_integer(&previous, &generation)
            && initiated <= effective
            && effective < expires,
        INVALID,
    )?;
    require(reason_valid(&t["reason"]), INVALID)?;
    let old = &env["old_verification_key"];
    let new = &env["new_verification_key"];
    require(
        old.is_object()
            && new.is_object()
            && old["verification_key_digest"] == t["old_seed_key_digest"]
            && new["verification_key_digest"] == t["new_seed_key_digest"]
            && t["old_seed_key_digest"] != t["new_seed_key_digest"]
            && expected_old_digest.is_none_or(|d| t["old_seed_key_digest"] == d),
        INVALID,
    )?;
    for key in [old, new] {
        crate::identity::load_public_key(key)
            .map_err(|_| ContractError("seed_operator_rotation_key_invalid"))?;
    }
    crate::identity::verify(t, &env["old_signature"], old)
        .map_err(|_| ContractError("seed_operator_rotation_old_signature_invalid"))?;
    crate::identity::verify(t, &env["new_signature"], new)
        .map_err(|_| ContractError("seed_operator_rotation_new_signature_invalid"))?;
    Ok(t.clone())
}
