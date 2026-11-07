// SPDX-License-Identifier: AGPL-3.0-only
//! Android pairing onboarding verifiers, byte-faithful to v0.1.4
//! `mycelium_mobile/android_onboarding.py` (seed `pairing` handler and preflight:
//! `validate_android_quota`, `verify_android_pairing_request`, `approve_android_pairing_request`;
//! device side: `decrypt_android_invitation_handoff`). The filesystem join
//! (`join_android_member`) is device-local state, not a verifier, and is not ported.
//!
//! Entropy is the caller's: [`HandoffEntropy`] carries the owner's ephemeral X25519 key, the HKDF
//! salt and the AEAD nonce, so the protocol crate owns no RNG.
use crate::{canonical, handoff_crypto as crypto, identity, invite};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use serde_json::{Number, Value, json};
use sha2::{Digest, Sha256};

pub const PAIRING_REQUEST_PROTOCOL: &str = "mycelium.pairing_request.v1";
pub const ENCRYPTED_HANDOFF_PROTOCOL: &str = "mycelium.encrypted_invitation_handoff.v1";
const HANDOFF_PAYLOAD_PROTOCOL: &str = "mycelium.encrypted_invitation_payload.v1";
const PEER_CLASSES: [&str; 2] = ["android_native_iroh", "android_native_qwen2_iroh"];
const NATIVE_CARRIER: &str = "signed_native_application";
const EXCLUDED_CARRIERS: [&str; 3] = ["termux", "adb", "ssh"];
const HKDF_INFO_PREFIX: &[u8] = b"mycelium.a12.pairing.handoff.v1\0";

/// Budget registry rows `PyFinalMyceliumMobileAndroidOnboardingRequestTtlMaxL111C0` (600 s),
/// `PyMyceliumMobileAndroidOnboardingMaxHandoffBytesL112` (64 KiB),
/// `PyMyceliumMobileAndroidOnboardingMaxQuotaBytesL113` (1 TiB).
const REQUEST_TTL_MAX: f64 = 600.0;
const MAX_HANDOFF_BYTES: usize = 64 * 1024;
const MAX_QUOTA_BYTES: i128 = 1 << 40;
const MAX_THERMAL_CEILING_C: f64 = 80.0;

const REQUEST_FIELDS: [&str; 13] = [
    "protocol",
    "device_ref",
    "identity_endpoint_id",
    "identity_key_digest",
    "identity_verification_key",
    "peer_class",
    "carrier",
    "invite_protocol",
    "recipient_encryption_key",
    "pairing_nonce",
    "issued_at",
    "expires_at",
    "signature",
];
const HANDOFF_FIELDS: [&str; 8] = [
    "protocol",
    "request_digest",
    "recipient_key_digest",
    "sender_ephemeral_key",
    "kdf_salt",
    "aead_nonce",
    "expires_at",
    "ciphertext",
];
const PAYLOAD_FIELDS: [&str; 11] = [
    "protocol",
    "request_digest",
    "device_ref",
    "identity_key_digest",
    "pairing_nonce",
    "peer_class",
    "carrier",
    "expires_at",
    "seed_key_digest",
    "quota",
    "invite_bundle",
];
const QUOTA_FIELDS: [&str; 5] = [
    "storage_bytes",
    "network_bytes_per_day",
    "background_allowed",
    "battery_floor_percent",
    "thermal_ceiling_c",
];

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct OnboardingError(pub &'static str);

/// The three random draws `approve_android_pairing_request` makes in v0.1.4.
pub struct HandoffEntropy {
    pub sender_private: [u8; 32],
    pub salt: [u8; 32],
    pub aead_nonce: [u8; 12],
}

fn refuse<T>(code: &'static str) -> Result<T, OnboardingError> {
    Err(OnboardingError(code))
}

fn has_exact_fields(value: &Value, fields: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|f| m.contains_key(*f)))
}

/// `_SEGMENT_RE.fullmatch`: `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (ASCII only).
fn segment(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        let b = s.as_bytes();
        (1..=128).contains(&b.len())
            && b[0].is_ascii_alphanumeric()
            && b.iter()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
    })
}

/// `_DIGEST_RE.fullmatch`: `sha256:` plus 64 lower-case hex digits.
fn digest_text(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        s.strip_prefix("sha256:").is_some_and(|h| {
            h.len() == 64 && h.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
        })
    })
}

/// `_finite`: a non-bool Python int/float that is finite (an int beyond the float range is a
/// Python `OverflowError`; v0.2 refuses it as a time error).
fn finite(value: Option<&Value>) -> Result<f64, OnboardingError> {
    value
        .and_then(Value::as_number)
        .and_then(Number::as_f64)
        .filter(|f| f.is_finite())
        .map_or_else(|| refuse("android_onboarding_time_invalid"), Ok)
}

/// `_b64decode`: non-empty unpadded URL-safe base64 that re-encodes to itself (so canonical).
fn b64_canonical(value: Option<&Value>, length: Option<usize>) -> Result<Vec<u8>, OnboardingError> {
    const INVALID: OnboardingError = OnboardingError("android_onboarding_handoff_invalid");
    let text = value.and_then(Value::as_str).ok_or(INVALID)?;
    if text.is_empty() || text.contains('=') {
        return Err(INVALID);
    }
    let raw = URL_SAFE_NO_PAD.decode(text).map_err(|_| INVALID)?;
    if length.is_some_and(|n| raw.len() != n) {
        return Err(INVALID);
    }
    Ok(raw)
}

fn b64(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}

fn sha256_digest(raw: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(raw)))
}

fn request_digest(document: &Value) -> Result<String, OnboardingError> {
    canonical::to_bytes(document)
        .map(|bytes| sha256_digest(&bytes))
        .map_err(|_| OnboardingError("android_onboarding_request_invalid"))
}

/// A JSON number that Python parsed as `int` (no fraction or exponent), as an `i128`;
/// `Err(negative)` when it does not fit (magnitude far beyond any bound here).
fn python_int(value: &Value) -> Option<Result<i128, bool>> {
    let number = value.as_number()?;
    let text = number.to_string();
    if text.bytes().any(|c| matches!(c, b'.' | b'e' | b'E')) {
        return None;
    }
    Some(text.parse::<i128>().map_err(|_| text.starts_with('-')))
}

/// `validate_android_quota`: closed, bounded consent quota.
pub fn validate_android_quota(quota: &Value) -> Result<Value, OnboardingError> {
    const INVALID: OnboardingError = OnboardingError("android_onboarding_quota_invalid");
    if !has_exact_fields(quota, &QUOTA_FIELDS) {
        return Err(INVALID);
    }
    for field in ["storage_bytes", "network_bytes_per_day"] {
        match python_int(&quota[field]) {
            Some(Ok(n)) if (0..=MAX_QUOTA_BYTES).contains(&n) => {}
            _ => return Err(INVALID),
        }
    }
    if !quota["background_allowed"].is_boolean() {
        return Err(INVALID);
    }
    match python_int(&quota["battery_floor_percent"]) {
        Some(Ok(n)) if (0..=100).contains(&n) => {}
        _ => return Err(INVALID),
    }
    let ceiling = quota["thermal_ceiling_c"]
        .as_number()
        .and_then(Number::as_f64)
        .filter(|f| f.is_finite() && *f > 0.0 && *f <= MAX_THERMAL_CEILING_C);
    if ceiling.is_none() {
        return Err(INVALID);
    }
    Ok(quota.clone())
}

fn require_carrier(carrier: &Value) -> Result<(), OnboardingError> {
    if EXCLUDED_CARRIERS.iter().any(|c| carrier == *c) {
        return refuse("android_onboarding_carrier_excluded");
    }
    if carrier != NATIVE_CARRIER {
        return refuse("android_onboarding_carrier_invalid");
    }
    Ok(())
}

/// `verify_android_pairing_request`: closed request, permanent identity signature and life.
pub fn verify_android_pairing_request(request: &Value, now: f64) -> Result<Value, OnboardingError> {
    const INVALID: OnboardingError = OnboardingError("android_onboarding_request_invalid");
    if !has_exact_fields(request, &REQUEST_FIELDS)
        || request["protocol"] != PAIRING_REQUEST_PROTOCOL
    {
        return Err(INVALID);
    }
    for field in ["device_ref", "identity_endpoint_id", "pairing_nonce"] {
        if !segment(&request[field]) {
            return Err(INVALID);
        }
    }
    let key_record = &request["identity_verification_key"];
    if !digest_text(&request["identity_key_digest"])
        || !key_record.is_object()
        || key_record.get("verification_key_digest") != Some(&request["identity_key_digest"])
    {
        return Err(INVALID);
    }
    b64_canonical(request.get("recipient_encryption_key"), Some(32))?;

    let issued = finite(request.get("issued_at"))?;
    let expires = finite(request.get("expires_at"))?;
    if !now.is_finite() {
        return refuse("android_onboarding_time_invalid");
    }
    if expires <= issued || expires - issued > REQUEST_TTL_MAX {
        return Err(INVALID);
    }

    let mut statement = request.clone();
    let signature = statement
        .as_object_mut()
        .and_then(|m| m.remove("signature"))
        .unwrap_or(Value::Null);
    let signed = signature.get("signer_endpoint_id") == Some(&request["identity_endpoint_id"])
        && identity::verify(&statement, &signature, key_record).is_ok();
    if !signed {
        return refuse("android_onboarding_request_signature_invalid");
    }

    if !PEER_CLASSES.iter().any(|c| request["peer_class"] == *c) {
        return refuse("android_onboarding_peer_class_unsupported");
    }
    if request["invite_protocol"] != invite::INVITE_BUNDLE_PROTOCOL {
        return Err(INVALID);
    }
    require_carrier(&request["carrier"])?;
    if now > expires {
        return refuse("android_onboarding_request_expired");
    }
    Ok(request.clone())
}

fn derive_key(shared: &[u8; 32], salt: &[u8], request_digest: &str) -> [u8; 32] {
    let info = [HKDF_INFO_PREFIX, request_digest.as_bytes()].concat();
    crypto::hkdf_sha256_32(shared, salt, &info)
}

fn array<const N: usize>(raw: &[u8]) -> Result<[u8; N], OnboardingError> {
    raw.try_into()
        .map_err(|_| OnboardingError("android_onboarding_handoff_invalid"))
}

fn float_value(f: f64) -> Value {
    Number::from_f64(f).map_or(Value::Null, Value::Number)
}

/// `approve_android_pairing_request`: owner-side approval; encrypts one complete A8 invite bundle
/// to the device's ephemeral recipient key.
pub fn approve_android_pairing_request(
    request: &Value,
    invite_bundle: &Value,
    quota: &Value,
    now: f64,
    entropy: &HandoffEntropy,
) -> Result<Value, OnboardingError> {
    const BINDING: OnboardingError = OnboardingError("android_onboarding_invite_binding_invalid");
    if !now.is_finite() {
        return refuse("android_onboarding_time_invalid");
    }
    let verified_request = verify_android_pairing_request(request, now)?;
    let approved_quota = validate_android_quota(quota)?;
    let verified_invite = invite::verify_invite_bundle(invite_bundle, now).map_err(|_| BINDING)?;
    let invite_expires = finite(verified_invite.payload.get("expires_at")).map_err(|_| BINDING)?;
    let request_expires = finite(verified_request.get("expires_at"))?;
    if verified_invite.payload["nonce"] != verified_request["pairing_nonce"]
        || invite_expires > request_expires
    {
        return Err(BINDING);
    }

    let digest = request_digest(&verified_request)?;
    let recipient_bytes: [u8; 32] = array(&b64_canonical(
        verified_request.get("recipient_encryption_key"),
        Some(32),
    )?)?;
    let sender_public = crypto::x25519_public(&entropy.sender_private);
    let header = json!({
        "protocol": ENCRYPTED_HANDOFF_PROTOCOL,
        "request_digest": digest,
        "recipient_key_digest": sha256_digest(&recipient_bytes),
        "sender_ephemeral_key": b64(&sender_public),
        "kdf_salt": b64(&entropy.salt),
        "aead_nonce": b64(&entropy.aead_nonce),
        "expires_at": float_value(invite_expires),
    });
    let payload = json!({
        "protocol": HANDOFF_PAYLOAD_PROTOCOL,
        "request_digest": digest,
        "device_ref": verified_request["device_ref"],
        "identity_key_digest": verified_request["identity_key_digest"],
        "pairing_nonce": verified_request["pairing_nonce"],
        "peer_class": verified_request["peer_class"],
        "carrier": verified_request["carrier"],
        "expires_at": float_value(invite_expires),
        "seed_key_digest": verified_invite.seed_key_digest,
        "quota": approved_quota,
        "invite_bundle": invite_bundle,
    });
    // `X25519PrivateKey.exchange` raises ValueError for a low-order recipient (all-zero secret).
    let shared = crypto::x25519_exchange(&entropy.sender_private, &recipient_bytes)
        .ok_or(OnboardingError("android_onboarding_recipient_mismatch"))?;
    let key = derive_key(&shared, &entropy.salt, &digest);
    let sealed = crypto::aead_encrypt(
        &key,
        &entropy.aead_nonce,
        &canonical::to_bytes(&payload).map_err(|_| BINDING)?,
        &canonical::to_bytes(&header).map_err(|_| BINDING)?,
    );
    let mut handoff = header;
    if let Some(map) = handoff.as_object_mut() {
        map.insert("ciphertext".to_owned(), Value::String(b64(&sealed)));
    }
    let size = canonical::to_bytes(&handoff).map_err(|_| BINDING)?.len();
    if size > MAX_HANDOFF_BYTES {
        return refuse("android_onboarding_handoff_too_large");
    }
    Ok(handoff)
}

/// Python `==` between a JSON value and a float (bool is an int; ints compare exactly).
fn equals_float(value: &Value, f: f64) -> bool {
    match value {
        Value::Bool(b) => f64::from(u8::from(*b)) == f,
        Value::Number(n) => match python_int(value) {
            Some(Ok(i)) => f.fract() == 0.0 && f.abs() < 1.0e38 && i == f as i128,
            Some(Err(_)) => false,
            None => n.as_f64() == Some(f),
        },
        _ => false,
    }
}

/// `decrypt_android_invitation_handoff`: recipient-decrypt and verify every request, invite,
/// quota and pin binding.
pub fn decrypt_android_invitation_handoff(
    request: &Value,
    handoff: &Value,
    recipient_private_key: &[u8],
    now: f64,
) -> Result<Value, OnboardingError> {
    const INVALID: OnboardingError = OnboardingError("android_onboarding_handoff_invalid");
    const RECIPIENT: OnboardingError = OnboardingError("android_onboarding_recipient_mismatch");
    if !now.is_finite() {
        return refuse("android_onboarding_time_invalid");
    }
    let verified_request = verify_android_pairing_request(request, now)?;
    if !has_exact_fields(handoff, &HANDOFF_FIELDS) {
        return Err(INVALID);
    }
    let digest = request_digest(&verified_request)?;
    if handoff["request_digest"] != digest.as_str() {
        return refuse("android_onboarding_request_mismatch");
    }
    if handoff["protocol"] != ENCRYPTED_HANDOFF_PROTOCOL {
        return Err(INVALID);
    }
    let expires_at = finite(handoff.get("expires_at"))?;
    if now > expires_at {
        return refuse("android_onboarding_handoff_expired");
    }

    let recipient_public: [u8; 32] = array(&b64_canonical(
        verified_request.get("recipient_encryption_key"),
        Some(32),
    )?)?;
    if handoff["recipient_key_digest"] != sha256_digest(&recipient_public).as_str() {
        return Err(RECIPIENT);
    }
    let private: [u8; 32] = recipient_private_key.try_into().map_err(|_| RECIPIENT)?;
    if crypto::x25519_public(&private) != recipient_public {
        return Err(RECIPIENT);
    }
    let sender_public: [u8; 32] = array(&b64_canonical(
        handoff.get("sender_ephemeral_key"),
        Some(32),
    )?)?;
    let salt = b64_canonical(handoff.get("kdf_salt"), Some(32))?;
    let nonce: [u8; 12] = array(&b64_canonical(handoff.get("aead_nonce"), Some(12))?)?;
    let sealed = b64_canonical(handoff.get("ciphertext"), None)?;
    let mut header = handoff.clone();
    if let Some(map) = header.as_object_mut() {
        map.remove("ciphertext");
    }
    let shared = crypto::x25519_exchange(&private, &sender_public).ok_or(INVALID)?;
    let key = derive_key(&shared, &salt, &digest);
    let aad = canonical::to_bytes(&header).map_err(|_| INVALID)?;
    let raw = crypto::aead_decrypt(&key, &nonce, &sealed, &aad).ok_or(INVALID)?;

    let payload = crate::strict::canonical_object(&raw, raw.len().max(1)).map_err(|_| INVALID)?;
    if !has_exact_fields(&payload, &PAYLOAD_FIELDS)
        || payload["protocol"] != HANDOFF_PAYLOAD_PROTOCOL
    {
        return Err(INVALID);
    }
    let verified_invite =
        invite::verify_invite_bundle(&payload["invite_bundle"], now).map_err(|_| INVALID)?;
    validate_android_quota(&payload["quota"])?;
    require_carrier(&payload["carrier"])?;
    let invite_expires = finite(verified_invite.payload.get("expires_at"))?;
    let bound = payload["request_digest"] == digest.as_str()
        && payload["device_ref"] == verified_request["device_ref"]
        && payload["identity_key_digest"] == verified_request["identity_key_digest"]
        && payload["pairing_nonce"] == verified_request["pairing_nonce"]
        && payload["peer_class"] == verified_request["peer_class"]
        && payload["carrier"] == verified_request["carrier"]
        && equals_float(&payload["expires_at"], expires_at)
        && invite_expires == expires_at
        && verified_invite.payload["nonce"] == verified_request["pairing_nonce"]
        && payload["seed_key_digest"] == verified_invite.seed_key_digest.as_str();
    if !bound {
        return refuse("android_onboarding_handoff_binding_invalid");
    }
    Ok(payload)
}
