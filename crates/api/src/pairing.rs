// SPDX-License-Identifier: AGPL-3.0-only
//! Signed preview action: pairing.py:200-248, app.py:1673-1677 @ f4e366e9.
//! Verification conveys identity, NOT membership, consent or an approved invite.
//! The host Backend still owns closed/role/lifecycle fencing and publication.
use crate::{
    actions::{Action, Backend},
    schema::PublicError,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};
const FIELDS: &[&str] = &[
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
fn fail(code: &'static str) -> PublicError {
    PublicError::new(409, code)
}
fn segment(v: &Value) -> bool {
    // Closed identifier grammar, not a configurable resource budget.
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 128
            && s.as_bytes()[0].is_ascii_alphanumeric()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    })
}
fn digest(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.strip_prefix("sha256:").is_some_and(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
    })
}
fn finite(v: &Value) -> Result<f64, PublicError> {
    v.as_f64()
        .filter(|v| v.is_finite())
        .ok_or(fail("pairing_time_invalid"))
}
/// Strict source validation order matters (e.g. a bad signature wins over expiry).
pub fn preview(request: &Value, now: f64) -> Result<Value, PublicError> {
    let object = request.as_object().ok_or(fail("pairing_request_invalid"))?;
    if object.len() != FIELDS.len()
        || !FIELDS.iter().all(|f| object.contains_key(*f))
        || request["protocol"] != "mycelium.pairing_request.v1"
    {
        return Err(fail("pairing_request_invalid"));
    }
    if ["node_id", "identity_endpoint_id", "pairing_nonce"]
        .iter()
        .any(|f| !segment(&request[*f]))
        || !digest(&request["identity_key_digest"])
        || !request["identity_verification_key"].is_object()
        || request["identity_verification_key"]["verification_key_digest"]
            != request["identity_key_digest"]
    {
        return Err(fail("pairing_request_invalid"));
    }
    let key = request["recipient_encryption_key"]
        .as_str()
        .ok_or(fail("pairing_handoff_invalid"))?;
    let raw = URL_SAFE_NO_PAD
        .decode(key)
        .map_err(|_| fail("pairing_handoff_invalid"))?;
    // X25519's fixed wire width; no key material is decrypted or retained here.
    if raw.len() != 32 || URL_SAFE_NO_PAD.encode(&raw) != key {
        return Err(fail("pairing_handoff_invalid"));
    }
    let issued = finite(&request["issued_at"])?;
    let expires = finite(&request["expires_at"])?;
    if !now.is_finite() {
        return Err(fail("pairing_time_invalid"));
    }
    let ttl = BudgetId::PyFinalMyceliumA13MacosPairingRequestTtlMaxL45C0
        .span(Entries::ZERO)
        .map_err(|_| fail("budget_unavailable"))?
        .millis() as f64
        / 1000.0;
    if expires <= issued || expires - issued > ttl {
        return Err(fail("pairing_request_invalid"));
    }
    let mut statement = object.clone();
    statement.remove("signature");
    if request["signature"]["signer_endpoint_id"] != request["identity_endpoint_id"]
        || protocol::identity::verify(
            &Value::Object(statement),
            &request["signature"],
            &request["identity_verification_key"],
        )
        .is_err()
    {
        return Err(fail("pairing_request_signature_invalid"));
    }
    if !matches!(
        request["peer_class"].as_str(),
        Some("mac_mlx_iroh" | "windows_native_iroh" | "linux_native_iroh")
    ) || request["invite_protocol"] != "mycelium.invite_bundle.v1"
    {
        return Err(fail("pairing_request_invalid"));
    }
    // The source accepts exact expiry; do not silently 'fix' this in v0.2.
    if now > expires {
        return Err(fail("pairing_request_expired"));
    }
    Ok(project_preview(request).to_value())
}
/// Additive adapter: all other typed actions remain with the same host backend.
/// Its prepare(PairPreview) must check closed then owner role under the shared
/// action/lifecycle fence; no display snapshot can supply that authority.
pub struct SignedBackend<B, C> {
    backend: B,
    clock: C,
}
impl<B, C> SignedBackend<B, C> {
    pub fn new(backend: B, clock: C) -> Self {
        Self { backend, clock }
    }
}
impl<B: Backend, C: Fn() -> f64 + Send> Backend for SignedBackend<B, C> {
    fn prepare(&mut self, a: Action, b: &Map<String, Value>) -> Result<(), PublicError> {
        self.backend.prepare(a, b)
    }
    fn execute(&mut self, a: Action, b: &Map<String, Value>) -> Result<Value, PublicError> {
        self.execute_json(a, b).map(|j| j.to_value())
    }
    fn execute_json(
        &mut self,
        a: Action,
        b: &Map<String, Value>,
    ) -> Result<crate::wire::Json, PublicError> {
        if a == Action::PairPreview {
            preview_json(&b["request"], (self.clock)())
        } else {
            self.backend.execute_json(a, b)
        }
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.backend.publish()
    }
    fn apply_first_consent_choice(&mut self, enabled: bool) -> Result<(), PublicError> {
        self.backend.apply_first_consent_choice(enabled)
    }
    fn record_failure(&mut self, path: &str, error: &PublicError) {
        self.backend.record_failure(path, error);
    }
    fn republish_after_failure(&mut self, action: Action) {
        self.backend.republish_after_failure(action);
    }
}

/// `{key: value[key] for key in ('node_id', 'identity_key_digest', 'peer_class', 'expires_at')}`
/// (app.py:1676), in that order.
pub fn project_preview(request: &Value) -> crate::wire::Json {
    crate::jobj! {
        "node_id" => &request["node_id"],
        "identity_key_digest" => &request["identity_key_digest"],
        "peer_class" => &request["peer_class"],
        "expires_at" => &request["expires_at"],
    }
}
/// `preview` with the response in the source's key order.
pub fn preview_json(request: &Value, now: f64) -> Result<crate::wire::Json, PublicError> {
    preview(request, now).map(|_| project_preview(request))
}
