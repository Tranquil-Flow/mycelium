// SPDX-License-Identifier: AGPL-3.0-only
//! Signed document wire format, mycelium_swarm_directory/wire.py @ f4e366e9.
//! Canonical bytes, key grammar, fingerprint and the domain-separated Ed25519
//! check (`mycelium-swarm-sig-v1\n` + canonical(doc - sig)).
use super::json::{J, dumps_canonical, parse};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use sha2::{Digest, Sha256};

pub const SIG_PREFIX: &[u8] = b"mycelium-swarm-sig-v1\n";
pub const KEY_PREFIX: &str = "ed25519:";
pub const MAX_SAFE_INT: i64 = 9_007_199_254_740_991;

/// A closed, public error code with its HTTP status.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct WireError {
    pub code: &'static str,
    pub status: u16,
}
impl WireError {
    pub const fn new(code: &'static str, status: u16) -> Self {
        Self { code, status }
    }
    pub const fn bad(code: &'static str) -> Self {
        Self { code, status: 400 }
    }
}
/// `verifier(public_key, message, signature) -> ok`; injected like the source.
pub type Verifier = dyn Fn(&[u8], &[u8], &[u8]) -> bool + Send + Sync;

pub fn b64url(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}
fn b64url_char(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'_' || c == b'-'
}
pub fn unb64url(
    value: &J,
    length: Option<usize>,
    code: &'static str,
) -> Result<Vec<u8>, WireError> {
    let text = match value {
        J::Str(s) if !s.is_empty() && s.len() <= 4096 && s.bytes().all(b64url_char) => s,
        _ => return Err(WireError::bad(code)),
    };
    let raw = URL_SAFE_NO_PAD
        .decode(text)
        .map_err(|_| WireError::bad(code))?;
    if b64url(&raw) != *text || length.is_some_and(|n| raw.len() != n) {
        return Err(WireError::bad(code));
    }
    Ok(raw)
}
fn key_char(c: u8, first: bool) -> bool {
    c.is_ascii_lowercase() || (!first && (c.is_ascii_digit() || c == b'_'))
}
/// Python `_check_value`: depth, safe integers, text and key grammar.
pub fn check_value(v: &J, depth: usize) -> Result<(), WireError> {
    if depth > 6 {
        return Err(WireError::bad("document_too_deep"));
    }
    match v {
        J::Null | J::Bool(_) => Ok(()),
        J::Int(n) if (-MAX_SAFE_INT..=MAX_SAFE_INT).contains(n) => Ok(()),
        J::Int(_) | J::Huge => Err(WireError::bad("integer_out_of_range")),
        J::Str(s) if s.chars().any(char::is_control) => Err(WireError::bad("text_invalid")),
        J::Str(_) => Ok(()),
        J::Surrogate => Err(WireError::bad("text_invalid")),
        J::Deep => Err(WireError::bad("document_too_deep")),
        J::Arr(items) => items.iter().try_for_each(|x| check_value(x, depth + 1)),
        J::Obj(pairs) => pairs.iter().try_for_each(|(k, x)| {
            let b = k.as_bytes();
            if b.is_empty()
                || b.len() > 40
                || !key_char(b[0], true)
                || !b.iter().all(|c| key_char(*c, false))
            {
                return Err(WireError::bad("key_invalid"));
            }
            check_value(x, depth + 1)
        }),
    }
}
pub fn canonical(v: &J) -> Result<Vec<u8>, WireError> {
    check_value(v, 0)?;
    Ok(dumps_canonical(v).into_bytes())
}
/// wire.py:96-122 `parse_json_object`.
pub fn parse_json_object(raw: &[u8], limit: usize) -> Result<J, WireError> {
    if raw.len() > limit {
        return Err(WireError::new("body_too_large", 413));
    }
    let value = parse(raw)?;
    if !matches!(value, J::Obj(_)) {
        return Err(WireError::bad("json_invalid"));
    }
    check_value(&value, 0)?;
    Ok(value)
}
pub fn key_bytes(key: &J) -> Result<Vec<u8>, WireError> {
    let rest = match key {
        J::Str(s) if s.starts_with(KEY_PREFIX) => &s[KEY_PREFIX.len()..],
        _ => return Err(WireError::bad("key_invalid")),
    };
    unb64url(&J::str(rest), Some(32), "key_invalid")
}
pub fn fingerprint(key: &J) -> Result<String, WireError> {
    let digest = Sha256::digest(key_bytes(key)?);
    let hex: String = digest.iter().take(8).map(|b| format!("{b:02X}")).collect();
    Ok(hex
        .as_bytes()
        .chunks(4)
        .map(|c| String::from_utf8_lossy(c).into_owned())
        .collect::<Vec<_>>()
        .join("-"))
}
pub fn signing_bytes(document: &J) -> Result<Vec<u8>, WireError> {
    let mut out = SIG_PREFIX.to_vec();
    out.extend(canonical(&document.without("sig"))?);
    Ok(out)
}
pub fn verify_document(document: &J, signer_key: &J, verifier: &Verifier) -> Result<(), WireError> {
    let signature = unb64url(
        document.get("sig").unwrap_or(&J::Null),
        Some(64),
        "signature_invalid",
    )?;
    let public = key_bytes(signer_key)?;
    // Source: canonicalisation failures inside the try block count as a bad signature.
    let ok = signing_bytes(document).is_ok_and(|m| verifier(&public, &m, &signature));
    if ok {
        Ok(())
    } else {
        Err(WireError::new("signature_invalid", 403))
    }
}
pub fn exact_keys(document: &J, protocol: &str, keys: &[&str]) -> Result<(), WireError> {
    match document {
        J::Obj(p) if document.get("protocol").and_then(J::as_str) == Some(protocol) => {
            if p.len() == keys.len() && keys.iter().all(|k| document.get(k).is_some()) {
                Ok(())
            } else {
                Err(WireError::bad("document_fields_invalid"))
            }
        }
        _ => Err(WireError::bad("protocol_invalid")),
    }
}
pub fn text(v: &J, low: usize, high: usize, code: &'static str) -> Result<(), WireError> {
    match v {
        J::Str(s) => {
            let n = s.chars().count();
            if !(low..=high).contains(&n) || s != s.trim() {
                return Err(WireError::bad(code));
            }
            check_value(v, 0)
        }
        _ => Err(WireError::bad(code)),
    }
}
pub fn integer(v: &J, low: i64, high: i64, code: &'static str) -> Result<i64, WireError> {
    match v {
        J::Int(n) if (low..=high).contains(n) => Ok(*n),
        _ => Err(WireError::bad(code)),
    }
}
pub fn key_text(raw: &[u8]) -> Result<String, WireError> {
    if raw.len() != 32 {
        return Err(WireError::bad("key_invalid"));
    }
    Ok(format!("{KEY_PREFIX}{}", b64url(raw)))
}
/// The production `Verifier`: Ed25519 over the already domain-separated message.
/// Malformed keys/signatures verify false (source: `cryptography` ValueError → False).
pub fn ed25519_verify(public: &[u8], message: &[u8], signature: &[u8]) -> bool {
    use ed25519_dalek::{Signature, Verifier as _, VerifyingKey};
    let (Ok(public), Ok(signature)) = (
        <[u8; 32]>::try_from(public),
        <[u8; 64]>::try_from(signature),
    ) else {
        return false;
    };
    VerifyingKey::from_bytes(&public).is_ok_and(|key| {
        key.verify(message, &Signature::from_bytes(&signature))
            .is_ok()
    })
}
