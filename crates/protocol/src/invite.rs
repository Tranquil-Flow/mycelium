// SPDX-License-Identifier: AGPL-3.0-only
//! Signed single-use swarm invite tokens and seed-pinned invite bundles, byte-faithful to
//! v0.1.4 `mycelium_invite/token.py` and `bundle.py` (verification side).
use crate::identity;
use serde_json::Value;

pub const INVITE_PROTOCOL: &str = "mycelium.invite.v1";
pub const INVITE_BUNDLE_PROTOCOL: &str = "mycelium.invite_bundle.v1";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct InviteError(pub &'static str);

#[derive(Debug, Clone, PartialEq)]
pub struct VerifiedBundle {
    pub payload: Value,
    pub seed_key_digest: String,
    pub seed_key_records: Vec<Value>,
}

const PAYLOAD_FIELDS: [&str; 6] = [
    "protocol",
    "swarm_id",
    "seed_url",
    "nonce",
    "issued_at",
    "expires_at",
];
const BUNDLE_FIELDS: [&str; 4] = ["protocol", "seed_key_digest", "seed_key_records", "token"];

fn refuse<T>(code: &'static str) -> Result<T, InviteError> {
    Err(InviteError(code))
}

/// `base64.urlsafe_b64decode(value + padding)` as CPython's non-strict `a2b_base64` runs it:
/// `-`/`_` map to `+`/`/`, every other non-alphabet byte is skipped, a completed pad quad ends
/// the input, leftover bits are ignored, and an incomplete final quad is an error. A non-ASCII
/// character fails the `str -> bytes` step first.
fn py_urlsafe_b64decode(value: &str) -> Result<Vec<u8>, InviteError> {
    if !value.is_ascii() {
        return refuse("invite_malformed");
    }
    let mut padded = value.as_bytes().to_vec();
    padded.extend(std::iter::repeat_n(b'=', (4 - value.len() % 4) % 4));
    let (mut out, mut quad, mut left, mut pads, mut padded_out) =
        (Vec::new(), 0u8, 0u8, 0u8, false);
    for byte in padded {
        let c = match byte {
            b'-' => b'+',
            b'_' => b'/',
            other => other,
        };
        if c == b'=' {
            pads = pads.saturating_add(1);
            if quad >= 2 && quad + pads >= 4 {
                padded_out = true;
                break;
            }
            continue;
        }
        let Some(six) = (match c {
            b'A'..=b'Z' => Some(c - b'A'),
            b'a'..=b'z' => Some(c - b'a' + 26),
            b'0'..=b'9' => Some(c - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }) else {
            continue;
        };
        pads = 0;
        match quad {
            0 => {
                quad = 1;
                left = six;
            }
            1 => {
                quad = 2;
                out.push((left << 2) | (six >> 4));
                left = six & 0xf;
            }
            2 => {
                quad = 3;
                out.push((left << 4) | (six >> 2));
                left = six & 3;
            }
            _ => {
                quad = 0;
                out.push((left << 6) | six);
                left = 0;
            }
        }
    }
    if quad != 0 && !padded_out {
        return refuse("invite_malformed");
    }
    Ok(out)
}

fn strict_object(raw: &[u8]) -> Result<Value, InviteError> {
    crate::strict::canonical_object(raw, raw.len().max(1))
        .map_err(|_| InviteError("invite_malformed"))
}

/// Python `str.strip()` whitespace: Unicode White_Space plus U+001C..U+001F.
pub(crate) fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}
fn clean_text(v: &Value) -> bool {
    v.as_str()
        .is_some_and(|s| !s.is_empty() && !s.starts_with(py_space) && !s.ends_with(py_space))
}
/// Finite Python int/float that is not a bool, as `float(x)`.
fn finite_number(v: &Value) -> Option<f64> {
    v.as_number()?.as_f64().filter(|f| f.is_finite())
}

/// `verify_invite`: the seed's trusted key record(s) judge a detached evidence signature over the
/// canonical payload; expiry is judged after the signature.
pub fn verify_invite(token: &str, key_records: &[Value], now: f64) -> Result<Value, InviteError> {
    if !now.is_finite() {
        return refuse("invite_time_invalid");
    }
    if token.matches('.').count() != 1 {
        return refuse("invite_malformed");
    }
    let (encoded_body, encoded_signature) = token.split_once('.').unwrap_or_default();
    if encoded_body.is_empty() || encoded_signature.is_empty() {
        return refuse("invite_malformed");
    }
    let payload = strict_object(&py_urlsafe_b64decode(encoded_body)?)?;
    let signature = strict_object(&py_urlsafe_b64decode(encoded_signature)?)?;
    if payload["protocol"] != INVITE_PROTOCOL {
        return refuse("invite_protocol_invalid");
    }
    let fields = payload.as_object().map_or(0, serde_json::Map::len);
    if fields != PAYLOAD_FIELDS.len() || !PAYLOAD_FIELDS.iter().all(|f| payload.get(*f).is_some()) {
        return refuse("invite_malformed");
    }
    if !["swarm_id", "seed_url", "nonce"]
        .iter()
        .all(|f| clean_text(&payload[*f]))
    {
        return refuse("invite_malformed");
    }
    let (Some(issued_at), Some(expires_at)) = (
        finite_number(&payload["issued_at"]),
        finite_number(&payload["expires_at"]),
    ) else {
        return refuse("invite_malformed");
    };
    if expires_at <= issued_at {
        return refuse("invite_malformed");
    }
    // build_ed25519_verifier: every record loads, digests are unique, the keyring is non-empty.
    let mut keyring: Vec<(String, &Value)> = Vec::new();
    for record in key_records {
        let digest = identity::key_record_digest(record)
            .map_err(|_| InviteError("invite_verifier_invalid"))?;
        if keyring.iter().any(|(d, _)| *d == digest) {
            return refuse("invite_verifier_invalid");
        }
        keyring.push((digest, record));
    }
    if keyring.is_empty() {
        return refuse("invite_verifier_invalid");
    }
    let record = signature["verification_key_digest"]
        .as_str()
        .and_then(|d| keyring.iter().find(|(k, _)| k == d))
        .map(|(_, r)| *r);
    let Some(record) = record else {
        return refuse("invite_signature_invalid");
    };
    identity::verify(&payload, &signature, record)
        .map_err(|_| InviteError("invite_signature_invalid"))?;
    if now > expires_at {
        return refuse("invite_expired");
    }
    Ok(payload)
}

/// `verify_invite_bundle`: strict shape, key pin, token signature, expiry.
pub fn verify_invite_bundle(bundle: &Value, now: f64) -> Result<VerifiedBundle, InviteError> {
    let shape = bundle.as_object().is_some_and(|m| {
        m.len() == BUNDLE_FIELDS.len() && BUNDLE_FIELDS.iter().all(|f| m.contains_key(*f))
    });
    if !shape {
        return refuse("invite_bundle_malformed");
    }
    if bundle["protocol"] != INVITE_BUNDLE_PROTOCOL {
        return refuse("invite_bundle_protocol_invalid");
    }
    let record = match bundle["seed_key_records"].as_array().map(Vec::as_slice) {
        Some([record]) if record.is_object() => record,
        _ => return refuse("invite_bundle_key_records_invalid"),
    };
    let pinned = match bundle["seed_key_digest"].as_str() {
        Some(p) if !p.is_empty() && record["verification_key_digest"] == p => p,
        _ => return refuse("invite_bundle_key_pin_mismatch"),
    };
    let token = match bundle["token"].as_str() {
        Some(t) if !t.is_empty() => t,
        _ => return refuse("invite_bundle_malformed"),
    };
    let payload = verify_invite(token, std::slice::from_ref(record), now)?;
    Ok(VerifiedBundle {
        payload,
        seed_key_digest: pinned.to_owned(),
        seed_key_records: vec![record.clone()],
    })
}

/// `mint_invite_bundle` (`mycelium_invite/bundle.py`): a seed-pinned bundle carrying one signed single-use
/// token (`token.py` `mint_invite`: strict text fields, positive integer ttl, finite issue time). The token is
/// `base64url(canonical payload) "." base64url(canonical signature)`, both unpadded.
pub fn mint_bundle(
    signer: &identity::Identity,
    swarm_id: &str,
    seed_url: &str,
    ttl_seconds: &Value,
    nonce: &str,
    issued_at: f64,
) -> Result<Value, InviteError> {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    use serde_json::json;
    let ttl = match ttl_seconds {
        Value::Number(n) => n.to_string().parse::<u64>().ok().filter(|t| *t > 0),
        _ => None,
    }
    .ok_or(InviteError("invite_ttl_invalid"))?;
    if ![swarm_id, seed_url, nonce]
        .iter()
        .all(|t| !t.is_empty() && !t.starts_with(py_space) && !t.ends_with(py_space))
    {
        return refuse("invite_field_invalid");
    }
    if !issued_at.is_finite() {
        return refuse("invite_time_invalid");
    }
    let payload = json!({
        "protocol": INVITE_PROTOCOL, "swarm_id": swarm_id, "seed_url": seed_url, "nonce": nonce,
        "issued_at": issued_at, "expires_at": issued_at + ttl as f64,
    });
    let signature = signer
        .sign(&payload)
        .map_err(|_| InviteError("invite_field_invalid"))?;
    let encode = |value: &Value| -> Result<String, InviteError> {
        crate::canonical::to_bytes(value)
            .map(|raw| URL_SAFE_NO_PAD.encode(raw))
            .map_err(|_| InviteError("invite_field_invalid"))
    };
    Ok(json!({
        "protocol": INVITE_BUNDLE_PROTOCOL, "seed_key_digest": signer.verification_key_digest(),
        "seed_key_records": [signer.public_key_record()],
        "token": format!("{}.{}", encode(&payload)?, encode(&signature)?),
    }))
}
