// SPDX-License-Identifier: AGPL-3.0-only
//! The recipient-encrypted pairing handoff of v0.1.4 (pairing.py:155-433, mycelium_invite
//! token.py/bundle.py, authority.py:877-934 `approve_pairing`, peer.py:296-324 `_translate`
//! @ f4e366e9): the owner encrypts one invite bundle to the target's X25519 request key
//! (HKDF-SHA256, ChaCha20-Poly1305 with the canonical header as AAD); the target decrypts it
//! and verifies every binding. Every function is compared with the executed oracle in
//! `tests/handoff.rs`, handoff bytes included.
//!
//! The primitives in [`crypto`] delegate to `protocol::handoff_crypto` (audited X25519 / HKDF /
//! ChaCha20-Poly1305 crates; one implementation for the workspace, SE-CRYPTO-DEDUPE).
use crate::{
    join_link::{self, py_strip},
    pairing,
    preferences::private_state,
    schema::{PublicError, Raised},
    wire::Json,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use protocol::{
    canonical,
    identity::{self, Identity, IdentityError},
};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

const REQUEST_FIELDS: [&str; 12] = [
    "protocol",
    "node_id",
    "identity_endpoint_id",
    "identity_key_digest",
    "identity_verification_key",
    "peer_class",
    "invite_protocol",
    "recipient_encryption_key",
    "pairing_nonce",
    "issued_at",
    "expires_at",
    "signature",
];
const HANDOFF_PROTOCOL: &str = "mycelium.encrypted_invitation_handoff.v1";
const PAYLOAD_PROTOCOL: &str = "mycelium.encrypted_invitation_payload.v1";
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
const PAYLOAD_FIELDS: [&str; 10] = [
    "protocol",
    "request_digest",
    "node_id",
    "identity_key_digest",
    "pairing_nonce",
    "peer_class",
    "expires_at",
    "seed_key_digest",
    "quota",
    "invite_bundle",
];
const INVITE_PROTOCOL: &str = "mycelium.invite.v1";
const BUNDLE_PROTOCOL: &str = "mycelium.invite_bundle.v1";
const BUNDLE_FIELDS: [&str; 4] = ["protocol", "seed_key_digest", "seed_key_records", "token"];
const INVITE_FIELDS: [&str; 6] = [
    "protocol",
    "swarm_id",
    "seed_url",
    "nonce",
    "issued_at",
    "expires_at",
];
const APPROVAL_PROTOCOL: &str = "mycelium.demo_pairing_approval.v1";
const HKDF_INFO: &[u8] = b"mycelium.a13.pairing.handoff.v1\0";

pub mod crypto {
    //! The oracle's calling conventions over `protocol::handoff_crypto`, the workspace's ONE handoff
    //! crypto implementation (SE-CRYPTO-DEDUPE, RU-010). Nothing is computed here.
    use protocol::handoff_crypto as shared;

    /// `X25519PrivateKey.exchange`: `None` for the all-zero output (a low-order point).
    pub fn x25519(scalar: &[u8; 32], point: &[u8; 32]) -> Option<[u8; 32]> {
        shared::x25519_exchange(scalar, point)
    }
    /// `X25519PrivateKey.public_key().public_bytes_raw()`.
    pub fn x25519_public(scalar: &[u8; 32]) -> [u8; 32] {
        shared::x25519_public(scalar)
    }
    /// `HKDF(SHA256, length=32, salt, info).derive(ikm)` (named for what it returns: the lint reserves `hkdf_*` for primitives).
    pub fn derive_key_32(ikm: &[u8], salt: &[u8], info: &[u8]) -> [u8; 32] {
        shared::hkdf_sha256_32(ikm, salt, info)
    }
    /// `ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)`: ciphertext then the 16-byte tag.
    pub fn aead_seal(key: &[u8; 32], nonce: &[u8; 12], plaintext: &[u8], aad: &[u8]) -> Vec<u8> {
        shared::aead_encrypt(key, nonce, plaintext, aad)
    }
    /// `decrypt`: `None` for any authentication failure (`InvalidTag`).
    pub fn aead_open(
        key: &[u8; 32],
        nonce: &[u8; 12],
        sealed: &[u8],
        aad: &[u8],
    ) -> Option<Vec<u8>> {
        shared::aead_decrypt(key, nonce, sealed, aad)
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn pairing_error(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(Raised::Pairing)
}
fn invite_error(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(Raised::Invite)
}
fn handoff_invalid() -> PublicError {
    pairing_error("pairing_handoff_invalid")
}
fn builtin_value_error() -> PublicError {
    PublicError::new(409, "action_not_completed").raised_by(Raised::Builtin("ValueError"))
}
fn digest_of(value: &Value) -> Result<String, PublicError> {
    canonical::digest(value).map_err(|_| builtin_value_error())
}
fn canonical_bytes(value: &Value) -> Result<Vec<u8>, PublicError> {
    canonical::to_bytes(value).map_err(|_| builtin_value_error())
}
/// `pairing._b64decode(value, length=...)`: unpadded URL-safe base64 in its one canonical form.
fn b64_strict(value: &Value, length: Option<usize>) -> Result<Vec<u8>, PublicError> {
    let text = value
        .as_str()
        .filter(|t| !t.is_empty() && !t.contains('='))
        .ok_or_else(handoff_invalid)?;
    let raw = URL_SAFE_NO_PAD
        .decode(text)
        .map_err(|_| handoff_invalid())?;
    if length.is_some_and(|n| raw.len() != n) {
        return Err(handoff_invalid());
    }
    Ok(raw)
}
fn key_of(value: &Value) -> Result<[u8; 32], PublicError> {
    b64_strict(value, Some(32))?
        .try_into()
        .map_err(|_| handoff_invalid())
}
fn float_of(value: &Value) -> f64 {
    value
        .as_f64()
        .or_else(|| value.to_string().parse().ok())
        .unwrap_or(f64::NAN)
}

/// The three random values `approve_pairing_request` draws (a fresh sender key, the HKDF salt,
/// the AEAD nonce), injected so every byte is reproducible.
#[derive(Clone, Debug)]
pub struct Entropy {
    pub sender_secret: [u8; 32],
    pub salt: [u8; 32],
    pub aead_nonce: [u8; 12],
}
impl Entropy {
    /// Fresh operating-system randomness.
    pub fn os() -> Result<Self, PublicError> {
        let mut raw = [0u8; 76];
        std::io::Read::read_exact(
            &mut std::fs::File::open("/dev/urandom")
                .map_err(|e| crate::preferences::os_error(&e))?,
            &mut raw,
        )
        .map_err(|e| crate::preferences::os_error(&e))?;
        let part = |from: usize, len: usize| raw[from..from + len].to_vec();
        Ok(Self {
            sender_secret: part(0, 32).try_into().map_err(|_| builtin_value_error())?,
            salt: part(32, 32).try_into().map_err(|_| builtin_value_error())?,
            aead_nonce: part(64, 12).try_into().map_err(|_| builtin_value_error())?,
        })
    }
}
/// The seed's signing identity and address: what `coordinator.mint_invite` signs with.
pub struct Coordinator {
    pub signer: Identity,
    pub swarm_id: String,
    pub seed_url: String,
}

/// `pairing.validate_quota`.
pub fn validate_quota(quota: &Value) -> Result<Value, PublicError> {
    let invalid = || pairing_error("pairing_quota_invalid");
    let map = quota.as_object().ok_or_else(invalid)?;
    let fields = [
        "storage_bytes",
        "network_bytes_per_day",
        "background_allowed",
    ];
    if map.len() != fields.len() || !fields.iter().all(|f| map.contains_key(*f)) {
        return Err(invalid());
    }
    let maximum = budgets::BudgetId::PyMyceliumA13MacosPairingMaxQuotaBytesL47
        .bytes()
        .map(|b| b.get())
        .map_err(|_| builtin_value_error())?;
    for field in ["storage_bytes", "network_bytes_per_day"] {
        let ok = match &map[field] {
            Value::Number(n) => n.to_string().parse::<u64>().is_ok_and(|v| v <= maximum),
            _ => false,
        };
        if !ok {
            return Err(invalid());
        }
    }
    if !map["background_allowed"].is_boolean() {
        return Err(invalid());
    }
    Ok(quota.clone())
}

/// Python's lenient `base64.urlsafe_b64decode(value + padding)` (token.py:42-47): characters
/// outside the alphabet are skipped, a completed quantum ends the data, trailing bits are
/// ignored; only a dangling quantum is an error.
fn py_urlsafe_b64decode(text: &str) -> Option<Vec<u8>> {
    let padded = format!("{text}{}", "=".repeat((4 - text.chars().count() % 4) % 4));
    let (mut out, mut quad, mut left, mut pads) = (Vec::new(), 0u8, 0u32, 0u8);
    for c in padded.chars() {
        if c == '=' {
            if quad >= 2 {
                pads += 1;
                if quad + pads >= 4 {
                    return Some(out);
                }
            }
            continue;
        }
        let digit = match c {
            'A'..='Z' => c as u32 - 'A' as u32,
            'a'..='z' => c as u32 - 'a' as u32 + 26,
            '0'..='9' => c as u32 - '0' as u32 + 52,
            '-' | '+' => 62,
            '_' | '/' => 63,
            _ => continue,
        };
        pads = 0;
        match quad {
            0 => {
                left = digit;
                quad = 1;
            }
            1 => {
                out.push(((left << 2) | (digit >> 4)) as u8);
                left = digit & 0xf;
                quad = 2;
            }
            2 => {
                out.push(((left << 4) | (digit >> 2)) as u8);
                left = digit & 3;
                quad = 3;
            }
            _ => {
                out.push(((left << 6) | digit) as u8);
                quad = 0;
            }
        }
    }
    (quad == 0).then_some(out)
}
fn b64url(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}
/// `_strict_json_object`: UTF-8 JSON object, no duplicate key, no constant, canonical bytes.
fn strict_object(raw: &[u8], error: impl Fn() -> PublicError) -> Result<Value, PublicError> {
    let value = crate::schema::unique_value(raw).map_err(|_| error())?;
    if !value.is_object() || canonical::to_bytes(&value).map_err(|_| error())? != raw {
        return Err(error());
    }
    Ok(value)
}
fn plain_text(value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|t| !t.is_empty() && t == py_strip(t))
}
/// `isinstance(now, int|float)` and finite (never a bool), else `code`.
fn finite_number(value: &Value, code: &'static str) -> Result<f64, PublicError> {
    match value {
        Value::Number(_) if float_of(value).is_finite() => Ok(float_of(value)),
        _ => Err(invite_error(code)),
    }
}

/// `mint_invite_bundle`.
pub fn mint_invite_bundle(
    signer: &Identity,
    swarm_id: &str,
    seed_url: &str,
    ttl_seconds: &Value,
    nonce: &str,
    issued_at: f64,
) -> Result<Value, PublicError> {
    let ttl = match ttl_seconds {
        Value::Number(n) => n.to_string().parse::<u64>().ok().filter(|t| *t > 0),
        _ => None,
    }
    .ok_or_else(|| invite_error("invite_ttl_invalid"))?;
    if ![swarm_id, seed_url, nonce]
        .iter()
        .all(|t| !t.is_empty() && *t == py_strip(t))
    {
        return Err(invite_error("invite_field_invalid"));
    }
    if !issued_at.is_finite() {
        return Err(invite_error("invite_time_invalid"));
    }
    let payload = json!({
        "protocol": INVITE_PROTOCOL, "swarm_id": swarm_id, "seed_url": seed_url, "nonce": nonce,
        "issued_at": issued_at, "expires_at": issued_at + ttl as f64,
    });
    let signature = signer
        .sign(&payload)
        .map_err(|_| invite_error("invite_field_invalid"))?;
    let token = format!(
        "{}.{}",
        b64url(&canonical_bytes(&payload)?),
        b64url(&canonical_bytes(&signature)?)
    );
    Ok(json!({
        "protocol": BUNDLE_PROTOCOL, "seed_key_digest": signer.verification_key_digest(),
        "seed_key_records": [signer.public_key_record()], "token": token,
    }))
}

/// A verified invite: its payload and the pinned seed key digest.
pub struct VerifiedInvite {
    pub payload: Value,
    pub seed_key_digest: String,
}
/// `verify_invite` (token.py:100-150).
fn verify_invite(token: &Value, record: &Value, now: &Value) -> Result<Value, PublicError> {
    let instant = finite_number(now, "invite_time_invalid")?;
    let malformed = || invite_error("invite_malformed");
    let token = token
        .as_str()
        .filter(|t| t.matches('.').count() == 1)
        .ok_or_else(malformed)?;
    let (encoded_body, encoded_signature) = token.split_once('.').ok_or_else(malformed)?;
    if encoded_body.is_empty() || encoded_signature.is_empty() {
        return Err(malformed());
    }
    let body = py_urlsafe_b64decode(encoded_body).ok_or_else(malformed)?;
    let payload = strict_object(&body, malformed)?;
    let signature_raw = py_urlsafe_b64decode(encoded_signature).ok_or_else(malformed)?;
    let signature = strict_object(&signature_raw, malformed)?;
    let fields = payload.as_object().ok_or_else(malformed)?;
    if fields.get("protocol") != Some(&Value::from(INVITE_PROTOCOL)) {
        return Err(invite_error("invite_protocol_invalid"));
    }
    if fields.len() != INVITE_FIELDS.len() || !INVITE_FIELDS.iter().all(|f| fields.contains_key(*f))
    {
        return Err(malformed());
    }
    if !["swarm_id", "seed_url", "nonce"]
        .iter()
        .all(|f| plain_text(&fields[*f]))
    {
        return Err(malformed());
    }
    let issued = finite_number(&fields["issued_at"], "invite_malformed")?;
    let expires = finite_number(&fields["expires_at"], "invite_malformed")?;
    if expires <= issued {
        return Err(malformed());
    }
    match identity::verify(&payload, &signature, record) {
        Ok(()) => {}
        Err(IdentityError::KeyRecord | IdentityError::KeyDigest) => {
            return Err(invite_error("invite_verifier_invalid"));
        }
        Err(_) => return Err(invite_error("invite_signature_invalid")),
    }
    if instant > expires {
        return Err(invite_error("invite_expired"));
    }
    Ok(payload)
}
/// `verify_invite_bundle`: strict shape, key pin, token signature and expiry.
pub fn verify_invite_bundle(bundle: &Value, now: &Value) -> Result<VerifiedInvite, PublicError> {
    let fields = bundle
        .as_object()
        .filter(|m| {
            m.len() == BUNDLE_FIELDS.len() && BUNDLE_FIELDS.iter().all(|f| m.contains_key(*f))
        })
        .ok_or_else(|| invite_error("invite_bundle_malformed"))?;
    if fields["protocol"] != BUNDLE_PROTOCOL {
        return Err(invite_error("invite_bundle_protocol_invalid"));
    }
    let record = match fields["seed_key_records"].as_array().map(Vec::as_slice) {
        Some([record]) if record.is_object() => record,
        _ => return Err(invite_error("invite_bundle_key_records_invalid")),
    };
    let pinned = fields["seed_key_digest"]
        .as_str()
        .filter(|d| {
            !d.is_empty() && record.get("verification_key_digest") == Some(&Value::from(*d))
        })
        .ok_or_else(|| invite_error("invite_bundle_key_pin_mismatch"))?;
    if fields["token"].as_str().is_none_or(str::is_empty) {
        return Err(invite_error("invite_bundle_malformed"));
    }
    let payload = verify_invite(&fields["token"], record, now)?;
    Ok(VerifiedInvite {
        payload,
        seed_key_digest: pinned.to_owned(),
    })
}

fn derive_key(shared: &[u8; 32], salt: &[u8], request_digest: &str) -> [u8; 32] {
    let mut info = HKDF_INFO.to_vec();
    info.extend_from_slice(request_digest.as_bytes());
    crypto::derive_key_32(shared, salt, &info)
}
/// `verify_pairing_request`, as a `PairingError`.
fn verified_request(request: &Value, now: f64) -> Result<Value, PublicError> {
    pairing::preview(request, now).map_err(|e| e.raised_by(Raised::Pairing))?;
    debug_assert!(REQUEST_FIELDS.iter().all(|f| request.get(*f).is_some()));
    Ok(request.clone())
}

/// `approve_pairing_request`: verify the owner's inputs and encrypt one complete invite bundle
/// to the target. The handoff keeps the source's key order (header, then `ciphertext`).
pub fn approve_pairing_request(
    request: &Value,
    invite_bundle: &Value,
    quota: &Value,
    now: &Value,
    entropy: &Entropy,
) -> Result<Json, PublicError> {
    let instant = join_link::finite(now)?;
    let request = verified_request(request, instant)?;
    let approved_quota = validate_quota(quota)?;
    let binding = || pairing_error("pairing_invite_binding_invalid");
    let verified = verify_invite_bundle(invite_bundle, &json!(instant)).map_err(|_| binding())?;
    let invite = &verified.payload;
    if invite["nonce"] != request["pairing_nonce"]
        || float_of(&invite["expires_at"]) > float_of(&request["expires_at"])
    {
        return Err(binding());
    }
    let request_digest = digest_of(&request)?;
    let recipient = key_of(&request["recipient_encryption_key"])?;
    let sender_public = crypto::x25519_public(&entropy.sender_secret);
    let expires_at = float_of(&invite["expires_at"]);
    let recipient_digest = format!("sha256:{}", hex(&Sha256::digest(recipient)));
    let header = json!({
        "protocol": HANDOFF_PROTOCOL, "request_digest": request_digest,
        "recipient_key_digest": recipient_digest, "sender_ephemeral_key": b64url(&sender_public),
        "kdf_salt": b64url(&entropy.salt), "aead_nonce": b64url(&entropy.aead_nonce),
        "expires_at": expires_at,
    });
    let payload = json!({
        "protocol": PAYLOAD_PROTOCOL, "request_digest": request_digest, "node_id": request["node_id"],
        "identity_key_digest": request["identity_key_digest"], "pairing_nonce": request["pairing_nonce"],
        "peer_class": request["peer_class"], "expires_at": expires_at,
        "seed_key_digest": verified.seed_key_digest, "quota": approved_quota, "invite_bundle": invite_bundle,
    });
    let shared =
        crypto::x25519(&entropy.sender_secret, &recipient).ok_or_else(builtin_value_error)?;
    let key = derive_key(&shared, &entropy.salt, &request_digest);
    let sealed = crypto::aead_seal(
        &key,
        &entropy.aead_nonce,
        &canonical_bytes(&payload)?,
        &canonical_bytes(&header)?,
    );
    // The source builds `{**header, "ciphertext": ...}`: header order, then the ciphertext.
    let mut handoff: Vec<(String, Json)> = [
        "protocol",
        "request_digest",
        "recipient_key_digest",
        "sender_ephemeral_key",
        "kdf_salt",
        "aead_nonce",
        "expires_at",
    ]
    .iter()
    .map(|key| ((*key).to_owned(), Json::from(&header[*key])))
    .collect();
    handoff.push(("ciphertext".to_owned(), Json::from(b64url(&sealed))));
    let wire = Json::Obj(handoff);
    let limit = budgets::BudgetId::PyMyceliumA13MacosPairingMaxHandoffBytesL46
        .bytes()
        .map(|b| b.get())
        .map_err(|_| builtin_value_error())?;
    if canonical_bytes(&wire.to_value())?.len() as u64 > limit {
        return Err(pairing_error("pairing_handoff_too_large"));
    }
    Ok(wire)
}

/// `decrypt_invitation_handoff`: recipient-decrypt and verify every request, invite, quota and
/// pin binding. `recipient_private` is `None` when the source value is not 32 bytes.
pub fn decrypt_invitation_handoff(
    request: &Value,
    handoff: &Value,
    recipient_private: Option<&[u8]>,
    now: &Value,
) -> Result<Value, PublicError> {
    let instant = join_link::finite(now)?;
    let request = verified_request(request, instant)?;
    let fields = handoff
        .as_object()
        .filter(|m| {
            m.len() == HANDOFF_FIELDS.len() && HANDOFF_FIELDS.iter().all(|f| m.contains_key(*f))
        })
        .ok_or_else(handoff_invalid)?;
    let request_digest = digest_of(&request)?;
    if fields["request_digest"] != request_digest.as_str() {
        return Err(pairing_error("pairing_handoff_request_mismatch"));
    }
    if fields["protocol"] != HANDOFF_PROTOCOL {
        return Err(handoff_invalid());
    }
    let expires_at = join_link::finite(&fields["expires_at"])?;
    if instant > expires_at {
        return Err(pairing_error("pairing_handoff_expired"));
    }
    let recipient_public = key_of(&request["recipient_encryption_key"])?;
    let mismatch = || pairing_error("pairing_handoff_recipient_mismatch");
    if fields["recipient_key_digest"]
        != format!("sha256:{}", hex(&Sha256::digest(recipient_public))).as_str()
    {
        return Err(mismatch());
    }
    let private: [u8; 32] = recipient_private
        .and_then(|k| k.try_into().ok())
        .ok_or_else(mismatch)?;
    if crypto::x25519_public(&private) != recipient_public {
        return Err(mismatch());
    }
    let sender_public = key_of(&fields["sender_ephemeral_key"])?;
    let salt = b64_strict(&fields["kdf_salt"], Some(32))?;
    let nonce: [u8; 12] = b64_strict(&fields["aead_nonce"], Some(12))?
        .try_into()
        .map_err(|_| handoff_invalid())?;
    let ciphertext = b64_strict(&fields["ciphertext"], None)?;
    let header: Map<String, Value> = fields
        .iter()
        .filter(|(k, _)| k.as_str() != "ciphertext")
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let shared = crypto::x25519(&private, &sender_public).ok_or_else(handoff_invalid)?;
    let key = derive_key(&shared, &salt, &request_digest);
    let raw = crypto::aead_open(
        &key,
        &nonce,
        &ciphertext,
        &canonical_bytes(&Value::Object(header))?,
    )
    .ok_or_else(handoff_invalid)?;
    let payload = strict_object(&raw, handoff_invalid)?;
    let body = payload.as_object().ok_or_else(handoff_invalid)?;
    if body.len() != PAYLOAD_FIELDS.len()
        || !PAYLOAD_FIELDS.iter().all(|f| body.contains_key(*f))
        || body["protocol"] != PAYLOAD_PROTOCOL
    {
        return Err(handoff_invalid());
    }
    let verified = verify_invite_bundle(&body["invite_bundle"], &json!(instant))
        .map_err(|_| handoff_invalid())?;
    let invite = &verified.payload;
    validate_quota(&body["quota"])?;
    let same_time = |a: &Value, b: f64| float_of(a) == b;
    let expected = body["request_digest"] == request_digest.as_str()
        && body["node_id"] == request["node_id"]
        && body["identity_key_digest"] == request["identity_key_digest"]
        && body["pairing_nonce"] == request["pairing_nonce"]
        && body["peer_class"] == request["peer_class"]
        && same_time(&body["expires_at"], expires_at)
        && same_time(&invite["expires_at"], expires_at)
        && invite["nonce"] == request["pairing_nonce"]
        && body["seed_key_digest"] == verified.seed_key_digest.as_str();
    if !expected {
        return Err(pairing_error("pairing_handoff_binding_invalid"));
    }
    Ok(payload)
}

/// `_private_directory(path, create=True)`: a 0700 directory owned by this user.
fn private_directory(path: &Path) -> Result<(), PublicError> {
    use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
    let bad = || PublicError::new(409, "authority_state_path_invalid").raised_by(Raised::Authority);
    if std::fs::symlink_metadata(path).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound) {
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)
            .map_err(|_| bad())?;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
            .map_err(|_| bad())?;
    }
    let meta = std::fs::symlink_metadata(path).map_err(|_| bad())?;
    if !meta.file_type().is_dir()
        || meta.uid() != rustix::process::getuid().as_raw()
        || meta.mode() & 0o7777 != 0o700
    {
        return Err(bad());
    }
    Ok(())
}
fn authority_error(error: PublicError) -> PublicError {
    PublicError::new(error.status, error.code).raised_by(Raised::Authority)
}
fn approval_path(state_root: &Path, request_digest: &str) -> Result<PathBuf, PublicError> {
    let approvals = state_root.join("seed").join("approvals");
    private_directory(&approvals)?;
    Ok(approvals.join(format!(
        "{}.json",
        request_digest.trim_start_matches("sha256:")
    )))
}
/// `DemoAuthority.approve_pairing` (authority.py:877-934): the owner approves one request once;
/// a retry with the same lifetime and quota gets the stored handoff back, any change is
/// `pairing_approval_changed`. A stored handoff is returned as stored (keys sorted).
#[allow(clippy::too_many_arguments)]
pub fn approve_pairing(
    state_root: &Path,
    started: bool,
    coordinator: Option<&Coordinator>,
    request: &Value,
    ttl_seconds: &Value,
    quota: &Value,
    now: f64,
    entropy: &dyn Fn() -> Result<Entropy, PublicError>,
) -> Result<Json, PublicError> {
    let state = |code: &'static str| PublicError::new(409, code).raised_by(Raised::Authority);
    if !started {
        return Err(state("authority_not_started"));
    }
    let coordinator = coordinator.ok_or_else(|| state("authority_swarm_missing"))?;
    let at = json!(now);
    let verified = verified_request(request, now).map_err(authority_error)?;
    let approved_quota = validate_quota(quota).map_err(authority_error)?;
    let ttl = match ttl_seconds {
        Value::Number(n) => n.to_string().parse::<i64>().ok().filter(|t| *t > 0),
        _ => None,
    };
    let ttl = ttl
        .filter(|t| now + *t as f64 <= float_of(&verified["expires_at"]))
        .ok_or_else(|| state("pairing_approval_ttl_invalid"))?;
    let request_digest = digest_of(&verified)?;
    let path = approval_path(state_root, &request_digest)?;
    let quota_digest = digest_of(&approved_quota)?;
    let limit = private_state::private_json_limit().map_err(|e| state(e.0))?;
    let existing = private_state::load(&path, limit).map_err(|e| state(e.0))?;
    if let Some(existing) = existing {
        if existing.get("protocol") != Some(&Value::from(APPROVAL_PROTOCOL))
            || existing.get("request_digest") != Some(&Value::from(request_digest.as_str()))
        {
            return Err(state("authority_state_invalid"));
        }
        if existing.get("ttl_seconds") != Some(&Value::from(ttl))
            || existing.get("quota_digest") != Some(&Value::from(quota_digest.as_str()))
        {
            return Err(state("pairing_approval_changed"));
        }
        return match existing.get("handoff") {
            Some(handoff) if handoff.is_object() => Ok(Json::from(handoff)),
            _ => Err(state("authority_state_invalid")),
        };
    }
    let bundle = mint_invite_bundle(
        &coordinator.signer,
        &coordinator.swarm_id,
        &coordinator.seed_url,
        &json!(ttl),
        verified["pairing_nonce"].as_str().unwrap_or_default(),
        now,
    )?;
    let handoff = approve_pairing_request(&verified, &bundle, &approved_quota, &at, &entropy()?)
        .map_err(authority_error)?;
    let record = json!({
        "protocol": APPROVAL_PROTOCOL, "request_digest": request_digest, "ttl_seconds": ttl,
        "quota_digest": quota_digest, "handoff": handoff.to_value(),
    });
    private_state::store(&path, &record, limit).map_err(|e| state(e.0))?;
    Ok(handoff)
}
/// `RemoteMacMember._translate`'s code mapping (the public codes of a target-side failure).
pub fn peer_translate(code: Option<&str>) -> String {
    let code = code.unwrap_or("peer_operation_failed");
    match code {
        "seed_http_unreachable" | "bootstrap_unavailable" => "peer_transport_unreachable",
        "seed_resume_member_revoked" | "seed_member_lifecycle_ineligible" | "revoked" => {
            "peer_member_revoked"
        }
        "seed_resume_generation_stale" | "seed_member_generation_stale" | "generation_stale" => {
            "peer_generation_stale"
        }
        "seed_member_unknown" => "peer_member_unknown",
        "seed_member_lease_expired" | "membership_lease_expired" | "lease_expired" => {
            "peer_member_expired"
        }
        other => other,
    }
    .to_owned()
}
