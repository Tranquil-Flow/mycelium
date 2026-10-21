// SPDX-License-Identifier: AGPL-3.0-only
//! QR onboarding over the A12 Android pairing handoff
//! (`mycelium_mobile/android_onboarding.py`, protocols
//! `mycelium.pairing_request.v1` and `mycelium.encrypted_invitation_handoff.v1`).
//!
//! The device creates a signed pairing request carrying a recipient-only X25519
//! key; the owner encrypts the invite bundle to it; the device decrypts and checks
//! every binding before it may join. No seed URL, EndpointID or token is shown to
//! the user.

use base64::{
    Engine,
    engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD},
};
use chacha20poly1305::{ChaCha20Poly1305, KeyInit, aead::Aead, aead::Payload};
use hkdf::Hkdf;
use serde_json::{Map, Value, json};
use sha2::Sha256;
use x25519_dalek::{PublicKey, StaticSecret};

use crate::canonical::{self, sha256_prefixed};
use crate::identity::{self, Identity};

pub const PAIRING_REQUEST_PROTOCOL: &str = "mycelium.pairing_request.v1";
pub const ENCRYPTED_HANDOFF_PROTOCOL: &str = "mycelium.encrypted_invitation_handoff.v1";
pub const HANDOFF_PAYLOAD_PROTOCOL: &str = "mycelium.encrypted_invitation_payload.v1";
pub const INVITE_BUNDLE_PROTOCOL: &str = "mycelium.invite_bundle.v1";
pub const INVITE_PROTOCOL: &str = "mycelium.invite.v1";
pub const ANDROID_PEER_CLASS: &str = "android_native_iroh";
pub const ANDROID_QWEN2_PEER_CLASS: &str = "android_native_qwen2_iroh";
pub const NATIVE_CARRIER: &str = "signed_native_application";
pub const QR_SCHEME: &str = "mycelium:";
const REQUEST_TTL_MAX: f64 = 600.0;
const MAX_HANDOFF_BYTES: usize = 64 * 1024;
const MAX_QUOTA_BYTES: i64 = 1 << 40;
const MAX_THERMAL_CEILING_C: f64 = 80.0;
const HKDF_INFO_PREFIX: &[u8] = b"mycelium.a12.pairing.handoff.v1\0";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum OnboardingError {
    #[error("android_onboarding_request_invalid")]
    RequestInvalid,
    #[error("android_onboarding_request_ttl_invalid")]
    RequestTtl,
    #[error("android_onboarding_request_signature_invalid")]
    RequestSignature,
    #[error("android_onboarding_request_expired")]
    RequestExpired,
    #[error("android_onboarding_handoff_invalid")]
    HandoffInvalid,
    #[error("android_onboarding_handoff_too_large")]
    HandoffTooLarge,
    #[error("android_onboarding_request_mismatch")]
    RequestMismatch,
    #[error("android_onboarding_handoff_expired")]
    HandoffExpired,
    #[error("android_onboarding_recipient_mismatch")]
    RecipientMismatch,
    #[error("android_onboarding_quota_invalid")]
    QuotaInvalid,
    #[error("android_onboarding_handoff_binding_invalid")]
    BindingInvalid,
    #[error("android_onboarding_carrier_excluded")]
    CarrierExcluded,
    #[error("invite_{0}")]
    Invite(&'static str),
    #[error("android_onboarding_randomness_unavailable")]
    Random,
    #[error("android_onboarding_qr_invalid")]
    Qr,
}

fn b64(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}

/// Strict unpadded base64url, as `_b64decode` in the Python module.
fn unb64(value: &Value, len: Option<usize>) -> Result<Vec<u8>, OnboardingError> {
    let s = value.as_str().ok_or(OnboardingError::HandoffInvalid)?;
    if s.is_empty() || s.contains('=') {
        return Err(OnboardingError::HandoffInvalid);
    }
    let raw = URL_SAFE_NO_PAD.decode(s).map_err(|_| OnboardingError::HandoffInvalid)?;
    if b64(&raw) != s || len.is_some_and(|l| raw.len() != l) {
        return Err(OnboardingError::HandoffInvalid);
    }
    Ok(raw)
}

fn segment_ok(s: &str) -> bool {
    let bytes = s.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes.iter().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

fn finite(v: Option<&Value>) -> Option<f64> {
    let v = v?;
    if v.is_boolean() {
        return None;
    }
    v.as_f64().filter(|f| f.is_finite())
}

fn random<const N: usize>() -> Result<[u8; N], OnboardingError> {
    let mut out = [0u8; N];
    getrandom::getrandom(&mut out).map_err(|_| OnboardingError::Random)?;
    Ok(out)
}

/// A pending pairing: the signed request plus the recipient-only X25519 secret.
pub struct PairingSession {
    pub request: Value,
    recipient: StaticSecret,
}

impl PairingSession {
    /// `new_android_pairing_request`.
    pub fn new(
        identity: &Identity,
        device_ref: &str,
        now: f64,
        ttl_seconds: u32,
        nonce: Option<&str>,
    ) -> Result<Self, OnboardingError> {
        Self::new_for_class(identity, device_ref, now, ttl_seconds, nonce, ANDROID_PEER_CLASS)
    }

    pub fn new_for_class(identity: &Identity, device_ref: &str, now: f64, ttl_seconds: u32,
        nonce: Option<&str>, peer_class: &str) -> Result<Self, OnboardingError> {
        let recipient = StaticSecret::from(random::<32>()?);
        let nonce = match nonce {
            Some(n) => n.to_owned(),
            None => format!("pair-{}", b64(&random::<24>()?)),
        };
        Self::with_recipient_for_class(identity, device_ref, now, ttl_seconds, &nonce, recipient, peer_class)
    }

    pub fn with_recipient(
        identity: &Identity,
        device_ref: &str,
        now: f64,
        ttl_seconds: u32,
        nonce: &str,
        recipient: StaticSecret,
    ) -> Result<Self, OnboardingError> {
        Self::with_recipient_for_class(identity, device_ref, now, ttl_seconds, nonce, recipient, ANDROID_PEER_CLASS)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn with_recipient_for_class(identity: &Identity, device_ref: &str, now: f64, ttl_seconds: u32,
        nonce: &str, recipient: StaticSecret, peer_class: &str) -> Result<Self, OnboardingError> {
        if ![ANDROID_PEER_CLASS, ANDROID_QWEN2_PEER_CLASS].contains(&peer_class) {
            return Err(OnboardingError::RequestInvalid);
        }
        if !segment_ok(device_ref) || !segment_ok(nonce) || !now.is_finite() {
            return Err(OnboardingError::RequestInvalid);
        }
        if !(1..=REQUEST_TTL_MAX as u32).contains(&ttl_seconds) {
            return Err(OnboardingError::RequestTtl);
        }
        let statement = json!({
            "protocol": PAIRING_REQUEST_PROTOCOL,
            "device_ref": device_ref,
            "identity_endpoint_id": identity.endpoint_id(),
            "identity_key_digest": identity.verification_key_digest(),
            "identity_verification_key": identity.public_key_record(),
            "peer_class": peer_class,
            "carrier": NATIVE_CARRIER,
            "invite_protocol": INVITE_BUNDLE_PROTOCOL,
            "recipient_encryption_key": b64(PublicKey::from(&recipient).as_bytes()),
            "pairing_nonce": nonce,
            "issued_at": now,
            "expires_at": now + f64::from(ttl_seconds),
        });
        let signature = identity.sign(&statement).map_err(|_| OnboardingError::RequestInvalid)?;
        let mut request = statement;
        request["signature"] = signature;
        Ok(Self { request, recipient })
    }

    pub fn request_digest(&self) -> Result<String, OnboardingError> {
        canonical::digest(&self.request).map_err(|_| OnboardingError::RequestInvalid)
    }

    /// The device QR: `mycelium:` + base64url(canonical request).
    pub fn request_qr(&self) -> Result<String, OnboardingError> {
        encode_qr(&self.request)
    }

    /// `decrypt_android_invitation_handoff` for this session's request.
    pub fn decrypt(&self, handoff: &Value, now: f64) -> Result<Value, OnboardingError> {
        decrypt_handoff(&self.request, handoff, &self.recipient, now)
    }
}

pub fn encode_qr(document: &Value) -> Result<String, OnboardingError> {
    let bytes = canonical::to_bytes(document).map_err(|_| OnboardingError::Qr)?;
    Ok(format!("{QR_SCHEME}{}", b64(&bytes)))
}

pub fn decode_qr(text: &str) -> Result<Value, OnboardingError> {
    let body = text.trim().strip_prefix(QR_SCHEME).ok_or(OnboardingError::Qr)?;
    let raw = URL_SAFE_NO_PAD.decode(body).map_err(|_| OnboardingError::Qr)?;
    strict_canonical_object(&raw).map_err(|_| OnboardingError::Qr)
}

/// Parse JSON that must already be canonical (rejects duplicate keys, reordering
/// and whitespace by re-encoding and comparing, like `_strict_json_object`).
fn strict_canonical_object(raw: &[u8]) -> Result<Value, OnboardingError> {
    let value: Value = serde_json::from_slice(raw).map_err(|_| OnboardingError::HandoffInvalid)?;
    if !value.is_object() || canonical::to_bytes(&value).ok().as_deref() != Some(raw) {
        return Err(OnboardingError::HandoffInvalid);
    }
    Ok(value)
}

/// `verify_android_pairing_request`.
pub fn verify_request(request: &Value, now: f64) -> Result<(), OnboardingError> {
    const FIELDS: [&str; 13] = [
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
    let map = closed(request, &FIELDS).ok_or(OnboardingError::RequestInvalid)?;
    if map["protocol"] != PAIRING_REQUEST_PROTOCOL {
        return Err(OnboardingError::RequestInvalid);
    }
    for f in ["device_ref", "identity_endpoint_id", "pairing_nonce"] {
        if !map[f].as_str().is_some_and(segment_ok) {
            return Err(OnboardingError::RequestInvalid);
        }
    }
    let key_digest = map["identity_key_digest"].as_str().unwrap_or("");
    if !is_digest(key_digest)
        || map["identity_verification_key"].get("verification_key_digest")
            != Some(&map["identity_key_digest"])
    {
        return Err(OnboardingError::RequestInvalid);
    }
    unb64(&map["recipient_encryption_key"], Some(32))?;
    let issued = finite(map.get("issued_at")).ok_or(OnboardingError::RequestInvalid)?;
    let expires = finite(map.get("expires_at")).ok_or(OnboardingError::RequestInvalid)?;
    if expires <= issued || expires - issued > REQUEST_TTL_MAX {
        return Err(OnboardingError::RequestInvalid);
    }
    let mut statement = map.clone();
    let signature = statement.remove("signature").unwrap_or(Value::Null);
    if signature.get("signer_endpoint_id") != Some(&map["identity_endpoint_id"]) {
        return Err(OnboardingError::RequestSignature);
    }
    identity::verify(&Value::Object(statement), &signature, &map["identity_verification_key"])
        .map_err(|_| OnboardingError::RequestSignature)?;
    if (map["peer_class"] != ANDROID_PEER_CLASS && map["peer_class"] != ANDROID_QWEN2_PEER_CLASS) || map["invite_protocol"] != INVITE_BUNDLE_PROTOCOL {
        return Err(OnboardingError::RequestInvalid);
    }
    require_carrier(&map["carrier"])?;
    if now > expires {
        return Err(OnboardingError::RequestExpired);
    }
    Ok(())
}

fn require_carrier(carrier: &Value) -> Result<(), OnboardingError> {
    match carrier.as_str() {
        Some("termux" | "adb" | "ssh") => Err(OnboardingError::CarrierExcluded),
        Some(NATIVE_CARRIER) => Ok(()),
        _ => Err(OnboardingError::BindingInvalid),
    }
}

fn is_digest(s: &str) -> bool {
    s.len() == 71
        && s.starts_with("sha256:")
        && s[7..].bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn closed<'a>(value: &'a Value, fields: &[&str]) -> Option<&'a Map<String, Value>> {
    let map = value.as_object()?;
    (map.len() == fields.len() && fields.iter().all(|f| map.contains_key(*f))).then_some(map)
}

fn derive_key(shared: &[u8], salt: &[u8], request_digest: &str) -> [u8; 32] {
    let mut info = HKDF_INFO_PREFIX.to_vec();
    info.extend_from_slice(request_digest.as_bytes());
    let mut key = [0u8; 32];
    Hkdf::<Sha256>::new(Some(salt), shared)
        .expand(&info, &mut key)
        .expect("32 bytes is a valid HKDF-SHA256 output length");
    key
}

/// `decrypt_android_invitation_handoff`: decrypt and check every binding.
pub fn decrypt_handoff(
    request: &Value,
    handoff: &Value,
    recipient: &StaticSecret,
    now: f64,
) -> Result<Value, OnboardingError> {
    verify_request(request, now)?;
    const FIELDS: [&str; 8] = [
        "protocol",
        "request_digest",
        "recipient_key_digest",
        "sender_ephemeral_key",
        "kdf_salt",
        "aead_nonce",
        "expires_at",
        "ciphertext",
    ];
    let map = closed(handoff, &FIELDS).ok_or(OnboardingError::HandoffInvalid)?;
    if canonical::to_bytes(handoff).map_err(|_| OnboardingError::HandoffInvalid)?.len()
        > MAX_HANDOFF_BYTES
    {
        return Err(OnboardingError::HandoffTooLarge);
    }
    let request_digest = canonical::digest(request).map_err(|_| OnboardingError::RequestInvalid)?;
    if map["request_digest"] != request_digest.as_str() {
        return Err(OnboardingError::RequestMismatch);
    }
    if map["protocol"] != ENCRYPTED_HANDOFF_PROTOCOL {
        return Err(OnboardingError::HandoffInvalid);
    }
    let expires_at = finite(map.get("expires_at")).ok_or(OnboardingError::HandoffInvalid)?;
    if now > expires_at {
        return Err(OnboardingError::HandoffExpired);
    }
    let recipient_public = unb64(&request["recipient_encryption_key"], Some(32))?;
    if map["recipient_key_digest"] != sha256_prefixed(&recipient_public).as_str() {
        return Err(OnboardingError::RecipientMismatch);
    }
    if PublicKey::from(recipient).as_bytes().as_slice() != recipient_public.as_slice() {
        return Err(OnboardingError::RecipientMismatch);
    }
    let sender: [u8; 32] = unb64(&map["sender_ephemeral_key"], Some(32))?.try_into().unwrap();
    let salt = unb64(&map["kdf_salt"], Some(32))?;
    let nonce = unb64(&map["aead_nonce"], Some(12))?;
    let ciphertext = unb64(&map["ciphertext"], None)?;
    let mut header = map.clone();
    header.remove("ciphertext");
    let aad = canonical::to_bytes(&Value::Object(header)).map_err(|_| OnboardingError::HandoffInvalid)?;
    let shared = recipient.diffie_hellman(&PublicKey::from(sender));
    let key = derive_key(shared.as_bytes(), &salt, &request_digest);
    let raw = ChaCha20Poly1305::new((&key).into())
        .decrypt(nonce.as_slice().into(), Payload { msg: &ciphertext, aad: &aad })
        .map_err(|_| OnboardingError::HandoffInvalid)?;
    let payload = strict_canonical_object(&raw)?;
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
    let p = closed(&payload, &PAYLOAD_FIELDS).ok_or(OnboardingError::HandoffInvalid)?;
    if p["protocol"] != HANDOFF_PAYLOAD_PROTOCOL {
        return Err(OnboardingError::HandoffInvalid);
    }
    let invite = verify_invite_bundle(&p["invite_bundle"], now)
        .map_err(|_| OnboardingError::HandoffInvalid)?;
    validate_quota(&p["quota"])?;
    require_carrier(&p["carrier"])?;
    let bound = p["request_digest"] == request_digest.as_str()
        && p["device_ref"] == request["device_ref"]
        && p["identity_key_digest"] == request["identity_key_digest"]
        && p["pairing_nonce"] == request["pairing_nonce"]
        && p["peer_class"] == request["peer_class"]
        && p["carrier"] == request["carrier"]
        && finite(p.get("expires_at")) == Some(expires_at)
        && finite(invite.payload.get("expires_at")) == Some(expires_at)
        && invite.payload["nonce"] == request["pairing_nonce"]
        && p["seed_key_digest"] == invite.seed_key_digest.as_str();
    if !bound {
        return Err(OnboardingError::BindingInvalid);
    }
    Ok(payload)
}

/// `validate_android_quota`.
pub fn validate_quota(quota: &Value) -> Result<(), OnboardingError> {
    let fields = [
        "storage_bytes",
        "network_bytes_per_day",
        "background_allowed",
        "battery_floor_percent",
        "thermal_ceiling_c",
    ];
    let q = closed(quota, &fields).ok_or(OnboardingError::QuotaInvalid)?;
    for f in ["storage_bytes", "network_bytes_per_day"] {
        match q[f].as_i64() {
            Some(v) if (0..=MAX_QUOTA_BYTES).contains(&v) => {}
            _ => return Err(OnboardingError::QuotaInvalid),
        }
    }
    if !q["background_allowed"].is_boolean() {
        return Err(OnboardingError::QuotaInvalid);
    }
    match q["battery_floor_percent"].as_i64() {
        Some(v) if (0..=100).contains(&v) => {}
        _ => return Err(OnboardingError::QuotaInvalid),
    }
    match finite(q.get("thermal_ceiling_c")) {
        Some(v) if v > 0.0 && v <= MAX_THERMAL_CEILING_C => Ok(()),
        _ => Err(OnboardingError::QuotaInvalid),
    }
}

pub struct VerifiedInvite {
    pub payload: Map<String, Value>,
    pub seed_key_digest: String,
    pub seed_key_record: Value,
}

impl VerifiedInvite {
    pub fn seed_url(&self) -> &str {
        self.payload["seed_url"].as_str().unwrap_or_default()
    }
    pub fn swarm_id(&self) -> &str {
        self.payload["swarm_id"].as_str().unwrap_or_default()
    }
    pub fn nonce(&self) -> &str {
        self.payload["nonce"].as_str().unwrap_or_default()
    }
}

/// `verify_invite_bundle` + `verify_invite`.
pub fn verify_invite_bundle(bundle: &Value, now: f64) -> Result<VerifiedInvite, OnboardingError> {
    let b = closed(bundle, &["protocol", "seed_key_digest", "seed_key_records", "token"])
        .ok_or(OnboardingError::Invite("bundle_malformed"))?;
    if b["protocol"] != INVITE_BUNDLE_PROTOCOL {
        return Err(OnboardingError::Invite("bundle_protocol_invalid"));
    }
    let records = b["seed_key_records"].as_array().filter(|r| r.len() == 1 && r[0].is_object());
    let record = records.ok_or(OnboardingError::Invite("bundle_key_records_invalid"))?[0].clone();
    let pinned = b["seed_key_digest"].as_str().filter(|s| !s.is_empty());
    let pinned = pinned.ok_or(OnboardingError::Invite("bundle_key_pin_mismatch"))?;
    if record.get("verification_key_digest").and_then(Value::as_str) != Some(pinned) {
        return Err(OnboardingError::Invite("bundle_key_pin_mismatch"));
    }
    let token = b["token"].as_str().filter(|t| !t.is_empty());
    let token = token.ok_or(OnboardingError::Invite("bundle_malformed"))?;
    let (body, sig) = token.split_once('.').ok_or(OnboardingError::Invite("malformed"))?;
    if body.is_empty() || sig.is_empty() || sig.contains('.') {
        return Err(OnboardingError::Invite("malformed"));
    }
    let lenient = |s: &str| {
        let padded = format!("{s}{}", "=".repeat((4 - s.len() % 4) % 4));
        URL_SAFE.decode(padded).map_err(|_| OnboardingError::Invite("malformed"))
    };
    let payload = strict_canonical_object(&lenient(body)?).map_err(|_| OnboardingError::Invite("malformed"))?;
    let signature = strict_canonical_object(&lenient(sig)?).map_err(|_| OnboardingError::Invite("malformed"))?;
    if payload["protocol"] != INVITE_PROTOCOL {
        return Err(OnboardingError::Invite("protocol_invalid"));
    }
    let p = closed(&payload, &["protocol", "swarm_id", "seed_url", "nonce", "issued_at", "expires_at"])
        .ok_or(OnboardingError::Invite("malformed"))?;
    for f in ["swarm_id", "seed_url", "nonce"] {
        if !p[f].as_str().is_some_and(|s| !s.is_empty() && s == s.trim()) {
            return Err(OnboardingError::Invite("malformed"));
        }
    }
    let issued = finite(p.get("issued_at")).ok_or(OnboardingError::Invite("malformed"))?;
    let expires = finite(p.get("expires_at")).ok_or(OnboardingError::Invite("malformed"))?;
    if expires <= issued {
        return Err(OnboardingError::Invite("malformed"));
    }
    identity::verify(&payload, &signature, &record).map_err(|_| OnboardingError::Invite("signature_invalid"))?;
    if now > expires {
        return Err(OnboardingError::Invite("expired"));
    }
    Ok(VerifiedInvite { payload: p.clone(), seed_key_digest: pinned.to_owned(), seed_key_record: record })
}
