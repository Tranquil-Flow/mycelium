// SPDX-License-Identifier: AGPL-3.0-only
//! Durable Ed25519 member identity, byte-compatible with
//! `mycelium_qualification/signing.py` (`Ed25519EvidenceSigner`).

use base64::{Engine, engine::general_purpose::STANDARD};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde_json::{Value, json};
use zeroize::Zeroizing;

use crate::canonical::{self, sha256_prefixed};

pub const SIGNATURE_DOMAIN: &str = "mycelium.ed25519_evidence_signature.v1";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum IdentityError {
    #[error("invalid_signer_endpoint_id")]
    EndpointId,
    #[error("invalid_signed_statement")]
    Statement,
    #[error("invalid_verification_key_record")]
    KeyRecord,
    #[error("verification_key_digest_mismatch")]
    KeyDigest,
    #[error("signature_invalid")]
    Signature,
    #[error("randomness_unavailable")]
    Random,
}

/// A member's durable signing identity. The secret never leaves this type except
/// through [`Identity::secret_bytes`], which the platform layer seals with the OS
/// keystore/keychain.
pub struct Identity {
    endpoint_id: String,
    key: SigningKey,
}

impl Identity {
    pub fn generate(endpoint_id: &str) -> Result<Self, IdentityError> {
        let mut seed = Zeroizing::new([0u8; 32]);
        getrandom::getrandom(&mut *seed).map_err(|_| IdentityError::Random)?;
        Self::from_secret(endpoint_id, &seed)
    }

    pub fn from_secret(endpoint_id: &str, secret: &[u8; 32]) -> Result<Self, IdentityError> {
        if endpoint_id.trim().is_empty() {
            return Err(IdentityError::EndpointId);
        }
        Ok(Self { endpoint_id: endpoint_id.to_owned(), key: SigningKey::from_bytes(secret) })
    }

    pub fn secret_bytes(&self) -> Zeroizing<[u8; 32]> {
        Zeroizing::new(self.key.to_bytes())
    }

    pub fn endpoint_id(&self) -> &str {
        &self.endpoint_id
    }

    pub fn public_key_bytes(&self) -> [u8; 32] {
        self.key.verifying_key().to_bytes()
    }

    pub fn verification_key_digest(&self) -> String {
        sha256_prefixed(&self.public_key_bytes())
    }

    pub fn public_key_record(&self) -> Value {
        json!({
            "algorithm": "ed25519",
            "encoding": "base64",
            "verification_key": STANDARD.encode(self.public_key_bytes()),
            "verification_key_digest": self.verification_key_digest(),
        })
    }

    /// `Ed25519EvidenceSigner.sign`: sign the domain-separated statement digest.
    pub fn sign(&self, statement: &Value) -> Result<Value, IdentityError> {
        if !statement.is_object() {
            return Err(IdentityError::Statement);
        }
        let statement_digest =
            sha256_prefixed(&canonical::to_bytes(statement).map_err(|_| IdentityError::Statement)?);
        let key_digest = self.verification_key_digest();
        let payload = signature_payload(&self.endpoint_id, &key_digest, &statement_digest);
        let signature = self.key.sign(&payload);
        Ok(json!({
            "algorithm": "ed25519",
            "signer_endpoint_id": self.endpoint_id,
            "verification_key_digest": key_digest,
            "signed_statement_digest": statement_digest,
            "signature": STANDARD.encode(signature.to_bytes()),
        }))
    }
}

fn signature_payload(endpoint_id: &str, key_digest: &str, statement_digest: &str) -> Vec<u8> {
    canonical::to_bytes(&json!({
        "algorithm": "ed25519",
        "protocol": SIGNATURE_DOMAIN,
        "signed_statement_digest": statement_digest,
        "signer_endpoint_id": endpoint_id,
        "verification_key_digest": key_digest,
    }))
    .expect("fixed-shape payload is canonical")
}

/// Verify one detached evidence signature against a public key record
/// (`build_ed25519_verifier` semantics).
pub fn verify(statement: &Value, signature: &Value, key_record: &Value) -> Result<(), IdentityError> {
    let record = key_record.as_object().ok_or(IdentityError::KeyRecord)?;
    let fields = ["algorithm", "encoding", "verification_key", "verification_key_digest"];
    if record.len() != fields.len() || !fields.iter().all(|f| record.contains_key(*f)) {
        return Err(IdentityError::KeyRecord);
    }
    if record["algorithm"] != "ed25519" || record["encoding"] != "base64" {
        return Err(IdentityError::KeyRecord);
    }
    let raw = STANDARD
        .decode(record["verification_key"].as_str().ok_or(IdentityError::KeyRecord)?)
        .map_err(|_| IdentityError::KeyRecord)?;
    let raw: [u8; 32] = raw.try_into().map_err(|_| IdentityError::KeyRecord)?;
    let key_digest = sha256_prefixed(&raw);
    if record["verification_key_digest"] != key_digest.as_str() {
        return Err(IdentityError::KeyDigest);
    }
    let key = VerifyingKey::from_bytes(&raw).map_err(|_| IdentityError::KeyRecord)?;
    let sig = signature.as_object().ok_or(IdentityError::Signature)?;
    let sig_fields = [
        "algorithm",
        "signer_endpoint_id",
        "verification_key_digest",
        "signed_statement_digest",
        "signature",
    ];
    if sig.len() != sig_fields.len() || !sig_fields.iter().all(|f| sig.contains_key(*f)) {
        return Err(IdentityError::Signature);
    }
    if sig["algorithm"] != "ed25519" || sig["verification_key_digest"] != key_digest.as_str() {
        return Err(IdentityError::Signature);
    }
    let statement_digest =
        sha256_prefixed(&canonical::to_bytes(statement).map_err(|_| IdentityError::Statement)?);
    if sig["signed_statement_digest"] != statement_digest.as_str() {
        return Err(IdentityError::Signature);
    }
    let endpoint_id = sig["signer_endpoint_id"].as_str().ok_or(IdentityError::Signature)?;
    let bytes = STANDARD
        .decode(sig["signature"].as_str().ok_or(IdentityError::Signature)?)
        .map_err(|_| IdentityError::Signature)?;
    let bytes: [u8; 64] = bytes.try_into().map_err(|_| IdentityError::Signature)?;
    let payload = signature_payload(endpoint_id, &key_digest, &statement_digest);
    key.verify(&payload, &Signature::from_bytes(&bytes)).map_err(|_| IdentityError::Signature)
}
