// SPDX-License-Identifier: AGPL-3.0-only
//! Binds the concrete directory authority to the HTTP boundary `directory::Service`
//! (server.py:144-185 @ f4e366e9). `/config` is server-level (no rate limit); the
//! QR renderer is `NativeQr` (byte-identical to the vendored encoder) or an injected `QrSvg`.
use super::{
    core::{Directory, PRIVACY_NOTE},
    docs,
    json::{J, dumps_response},
    store::{Store, StoreError},
};
use crate::{directory::Service, schema::PublicError};
use serde_json::{Map, Value};

pub const DEMO_LABEL: &str = "Demo. Hosted on one volunteer Mac through a tunnel: it may sleep. No accounts, no email, no tracking by this site. Keys live only in this browser. Traffic passes through Cloudflare, which can see your IP address, timing and prompts.";
/// hosted_defaults.py HELPER_DOWNLOAD: unavailable until a signed package is served.
pub fn helper_download() -> J {
    J::obj(vec![
        ("available", J::Bool(false)),
        ("url", J::Null),
        ("sha256", J::Null),
        ("version", J::str("0.1.0-preview.2")),
        ("notarized", J::Bool(false)),
    ])
}
/// `qr_svg(link)` of server.py:55-64.
pub trait QrSvg: Send + Sync {
    fn svg(&self, text: &str) -> Result<Vec<u8>, PublicError>;
}
/// The in-process renderer: the vendored Nayuki encoder of v0.1.4, ported in `crate::qr`.
/// `DataTooLongError` is not caught by the source handler, so the connection is dropped.
pub struct NativeQr;
impl QrSvg for NativeQr {
    fn svg(&self, text: &str) -> Result<Vec<u8>, PublicError> {
        crate::qr::svg(text).map_err(|_| PublicError::dropped("qr_data_too_long"))
    }
}
pub struct SwarmService<S: Store, Q: QrSvg> {
    directory: Directory<S>,
    public_origin: String,
    download: J,
    qr: Q,
}
/// v0.1.4 does not catch storage exceptions: the connection is dropped, no status is sent. An
/// explicit 503 is a user-visible change recorded for v0.3 (BACKLOG), not shipped here.
fn store(e: StoreError) -> PublicError {
    PublicError::dropped(e.0)
}
impl<S: Store, Q: QrSvg> SwarmService<S, Q> {
    pub fn new(directory: Directory<S>, public_origin: &str, download: J, qr: Q) -> Self {
        Self {
            directory,
            public_origin: public_origin.to_string(),
            download,
            qr,
        }
    }
    pub fn directory(&self) -> &Directory<S> {
        &self.directory
    }
    fn config(&self) -> J {
        J::obj(vec![
            ("directory", J::str(&self.public_origin)),
            ("privacy", J::str(PRIVACY_NOTE)),
            ("demo", J::str(DEMO_LABEL)),
            ("download", self.download.clone()),
            (
                "limits",
                J::obj(vec![
                    ("max_prompt_chars", J::Int(docs::MAX_PROMPT_CHARS)),
                    ("max_new_tokens", J::Int(docs::MAX_NEW_TOKENS)),
                ]),
            ),
        ])
    }
    fn answer(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        client: &str,
    ) -> Result<Vec<u8>, PublicError> {
        let (status, value) = self
            .directory
            .handle(method, path, body, client)
            .map_err(store)?;
        if status == 200 {
            return Ok(dumps_response(&value).into_bytes());
        }
        let code = value
            .get("error")
            .and_then(J::as_str)
            .unwrap_or("directory_error");
        Err(PublicError::new(status, known_code(code)))
    }
}
fn known_code(code: &str) -> &'static str {
    ERROR_CODES
        .iter()
        .find(|c| **c == code)
        .copied()
        .unwrap_or("directory_error")
}
/// Every public error code the directory can emit (closed set, wire/docs/core).
pub const ERROR_CODES: &[&str] = &[
    "access_mode_invalid",
    "body_too_large",
    "clock_skew",
    "description_invalid",
    "directory_busy",
    "directory_full",
    "document_fields_invalid",
    "document_too_deep",
    "encoding_invalid",
    "entry_invalid",
    "entry_max_new_tokens_invalid",
    "entry_origin_invalid",
    "entry_offers_full",
    "entry_prompt_required",
    "entry_prompt_too_long",
    "entry_request_invalid",
    "grant_expired",
    "grant_invalid",
    "grant_issuer_not_owner",
    "grant_lifetime_invalid",
    "grant_quota_invalid",
    "grantee_grant_cap",
    "grantee_key_invalid",
    "heartbeat_signer_not_authorized",
    "heartbeat_stale",
    "integer_out_of_range",
    "json_duplicate_key",
    "json_float_refused",
    "json_invalid",
    "key_invalid",
    "kill_engaged_invalid",
    "members_invalid",
    "method_not_allowed",
    "model_invalid",
    "name_invalid",
    "nonce_invalid",
    "nonce_replayed",
    "note_invalid",
    "open_quota_invalid",
    "owner_key_invalid",
    "owner_swarm_cap",
    "protocol_invalid",
    "rate_limited_ip",
    "rate_limited_key",
    "record_seq_stale",
    "record_too_large",
    "request_unknown",
    "requester_key_invalid",
    "route_unknown",
    "seq_invalid",
    "signature_invalid",
    "signer_key_invalid",
    "status_invalid",
    "swarm_id_invalid",
    "swarm_id_retired",
    "swarm_invite_only",
    "swarm_is_open",
    "swarm_owner_mismatch",
    "swarm_request_queue_full",
    "swarm_unknown",
    "text_invalid",
    "time_invalid",
    "entry_key_invalid",
    "key_invalid",
    "value_type_invalid",
    "budget_unavailable",
];
fn plain(bytes: &[u8]) -> Result<Value, PublicError> {
    serde_json::from_slice(bytes).map_err(|_| PublicError::new(500, "response_encoding_failed"))
}
impl<S: Store, Q: QrSvg> Service for SwarmService<S, Q> {
    fn get(&self, path: &str) -> Result<Value, PublicError> {
        plain(&self.get_body_for_client(path, "ip:unknown")?)
    }
    fn signed(&self, path: &str, document: &Map<String, Value>) -> Result<Value, PublicError> {
        let body = serde_json::to_vec(document)
            .map_err(|_| PublicError::new(500, "response_encoding_failed"))?;
        plain(&self.post_for_client(path, &body, "ip:unknown")?)
    }
    fn get_body_for_client(&self, path: &str, client: &str) -> Result<Vec<u8>, PublicError> {
        if path == "/config" {
            return Ok(dumps_response(&self.config()).into_bytes());
        }
        self.answer("GET", path, b"", client)
    }
    fn post_for_client(
        &self,
        path: &str,
        body: &[u8],
        client: &str,
    ) -> Result<Vec<u8>, PublicError> {
        self.answer("POST", path, body, client)
    }
    fn allow_loopback(&self) -> bool {
        self.directory.allow_loopback()
    }
    fn link_svg(&self, swarm_id: &str, _client: &str) -> Result<Vec<u8>, PublicError> {
        let owner = self
            .directory
            .owner_key_of(swarm_id)
            .map_err(store)?
            .ok_or(PublicError::new(404, "swarm_unknown"))?;
        let link = docs::swarm_link(&self.public_origin, swarm_id, &owner)
            .map_err(|e| PublicError::new(e.status, known_code(e.code)))?;
        self.qr.svg(&link)
    }
}
