// SPDX-License-Identifier: AGPL-3.0-only
//! Process-local token grants and verify-before-publish receipt cursor.
//! No transport, durable replay, stage liveness, or cleanup authority is implied.
use crate::{ContractError, checks::*};
use serde_json::Value;
use std::collections::BTreeSet;
const MAX_INT: u64 = (1 << 53) - 1;
pub const GRANT_PROTOCOL: &str = "mycelium.token_path_grant.v1";
pub const RECEIPT_PROTOCOL: &str = "mycelium.token_path_receipt.v1";
fn int(v: &Value, min: u64, max: u64) -> bool {
    v.as_u64().is_some_and(|n| n >= min && n <= max)
}
// This source contract bounds Unicode code points, not UTF-8 bytes or whitespace.
fn name(v: &Value) -> bool {
    v.as_str()
        .is_some_and(|s| !s.is_empty() && s.chars().count() <= 256)
}
fn canonical(v: &Value, code: &'static str) -> Result<Vec<u8>, ContractError> {
    let b = crate::canonical::to_bytes(v).map_err(|_| ContractError(code))?;
    require(
        b.len()
            <= if code.starts_with("token_path_receipt_") {
                crate::native::MAX_RECEIPT_BYTES
            } else {
                16384
            },
        code,
    )?;
    Ok(b)
}
fn verify(
    env: &Value,
    key: &Value,
    endpoint: &str,
    key_digest: &Value,
    code: &'static str,
) -> Result<Value, ContractError> {
    exact(env, &["statement", "signature"], code)?;
    canonical(env, code)?;
    let body = &env["statement"];
    let sig = &env["signature"];
    require(
        body.is_object()
            && sig.is_object()
            && sig["signer_endpoint_id"] == endpoint
            && &sig["verification_key_digest"] == key_digest,
        code,
    )?;
    crate::identity::verify(body, sig, key).map_err(|_| ContractError(code))?;
    Ok(body.clone())
}
fn validate_scope(v: &Value) -> Result<(), ContractError> {
    let c = "token_path_grant_invalid";
    exact(
        v,
        &[
            "run_id",
            "request_id",
            "requester_id",
            "owner_endpoint_id",
            "owner_key_digest",
            "entry_node_id",
            "entry_endpoint_id",
            "entry_key_digest",
            "participants",
            "control",
            "model_digest",
            "request_digest",
        ],
        c,
    )?;
    for f in [
        "run_id",
        "request_id",
        "requester_id",
        "owner_endpoint_id",
        "entry_node_id",
        "entry_endpoint_id",
    ] {
        require(name(&v[f]), c)?;
    }
    for f in [
        "owner_key_digest",
        "entry_key_digest",
        "model_digest",
        "request_digest",
    ] {
        require(digest(&v[f]), c)?;
    }
    let control = &v["control"];
    exact(
        control,
        &[
            "deployment_id",
            "deployment_epoch",
            "qualification_digest",
            "command_id",
            "publisher_generation",
            "absolute_deadline_ms",
            "request_attempt",
            "path_id",
            "path_attempt",
            "path_digest",
            "topology_generation",
            "cancellation_generation",
        ],
        c,
    )?;
    for f in ["qualification_digest", "path_digest"] {
        require(digest(&control[f]), c)?;
    }
    for f in ["deployment_id", "command_id", "path_id"] {
        require(name(&control[f]), c)?;
    }
    for f in [
        "deployment_epoch",
        "publisher_generation",
        "absolute_deadline_ms",
        "request_attempt",
        "topology_generation",
    ] {
        require(int(&control[f], 1, MAX_INT), c)?;
    }
    for f in ["path_attempt", "cancellation_generation"] {
        require(int(&control[f], 0, MAX_INT), c)?;
    }
    require(
        control["cancellation_generation"] == 0,
        "token_path_grant_stale",
    )?;
    let peers = v["participants"].as_array().ok_or(ContractError(c))?;
    require(!peers.is_empty() && peers.len() <= 64, c)?;
    let mut nodes = BTreeSet::new();
    let mut endpoints = BTreeSet::new();
    for peer in peers {
        exact(peer, &["node_id", "endpoint_id", "generation"], c)?;
        require(
            name(&peer["node_id"])
                && name(&peer["endpoint_id"])
                && int(&peer["generation"], 1, MAX_INT)
                && nodes.insert(peer["node_id"].as_str())
                && endpoints.insert(peer["endpoint_id"].as_str()),
            c,
        )?;
    }
    require(
        peers.iter().any(|p| {
            p["node_id"] == v["entry_node_id"] && p["endpoint_id"] == v["entry_endpoint_id"]
        }),
        c,
    )
}
fn stops(v: &Value, unique: bool) -> bool {
    let mut seen = BTreeSet::new();
    v.as_array().is_some_and(|a| {
        a.len() <= 128
            && a.iter()
                .all(|t| int(t, 0, i32::MAX as u64) && (!unique || seen.insert(t.as_u64())))
    })
}
/// All clocks/ceilings come from the authenticated host and budget registry.
/// Signed wall-clock expiry is translated once; a retry never restarts a lease.
#[derive(Clone, Copy, Debug)]
pub struct GrantWindow {
    pub now_unix_ms: u64,
    pub now_monotonic_s: f64,
    pub process_deadline_monotonic_s: f64,
    pub lease_deadline_monotonic_s: f64,
    pub clock_uncertainty_ms: u64,
}
pub struct GrantExpectation<'a> {
    pub scope: &'a Value,
    pub owner_key: &'a Value,
    pub max_new_tokens: u64,
    pub stop_token_ids: &'a Value,
    pub window: GrantWindow,
}
pub struct GrantRegistry {
    capacity: usize,
    seen: BTreeSet<(String, String, u64)>,
}
/// No Clone or Deserialize: only registry admission can construct this capability.
pub struct AdmittedGrant {
    statement: Value,
    digest: String,
    deadline: f64,
    claimed: bool,
}
impl AdmittedGrant {
    pub fn deadline_monotonic_s(&self) -> f64 {
        self.deadline
    }
    pub fn digest(&self) -> &str {
        &self.digest
    }
    pub fn statement(&self) -> &Value {
        &self.statement
    }
    pub fn claim_execution(&mut self) -> Result<(), ContractError> {
        require(!self.claimed, "token_path_grant_replay")?;
        self.claimed = true;
        Ok(())
    }
}
impl GrantRegistry {
    pub fn new(capacity: usize) -> Result<Self, ContractError> {
        require((1..=4096).contains(&capacity), "token_path_grant_capacity")?;
        Ok(Self {
            capacity,
            seen: BTreeSet::new(),
        })
    }
    pub fn admit(
        &mut self,
        env: &Value,
        e: &GrantExpectation<'_>,
    ) -> Result<AdmittedGrant, ContractError> {
        let c = "token_path_grant_invalid";
        let scope = e.scope;
        canonical(scope, c)?;
        validate_scope(scope)?;
        let body = verify(
            env,
            e.owner_key,
            scope["owner_endpoint_id"]
                .as_str()
                .ok_or(ContractError(c))?,
            &scope["owner_key_digest"],
            "token_path_grant_signature",
        )?;
        exact(
            &body,
            &[
                "protocol",
                "scope",
                "max_new_tokens",
                "stop_token_ids",
                "expires_at_unix_ms",
            ],
            c,
        )?;
        require(
            body["protocol"] == GRANT_PROTOCOL
                && int(&body["max_new_tokens"], 1, 65536)
                && int(&body["expires_at_unix_ms"], 1, MAX_INT)
                && stops(&body["stop_token_ids"], true)
                && (1..=65536).contains(&e.max_new_tokens)
                && stops(e.stop_token_ids, false),
            c,
        )?;
        let max = body["max_new_tokens"].as_u64().ok_or(ContractError(c))?;
        require(max <= e.max_new_tokens, "token_path_grant_over_budget")?;
        require(
            canonical(&body["scope"], c)? == canonical(scope, c)?
                && &body["stop_token_ids"] == e.stop_token_ids,
            "token_path_grant_mismatch",
        )?;
        let w = e.window;
        require(
            w.now_unix_ms <= MAX_INT
                && w.clock_uncertainty_ms <= MAX_INT
                && [
                    w.now_monotonic_s,
                    w.process_deadline_monotonic_s,
                    w.lease_deadline_monotonic_s,
                ]
                .iter()
                .all(|n| n.is_finite() && *n >= 0.0),
            c,
        )?;
        let expiry = body["expires_at_unix_ms"]
            .as_u64()
            .ok_or(ContractError(c))?;
        let remaining = expiry as i128 - w.now_unix_ms as i128 - w.clock_uncertainty_ms as i128;
        let deadline = (w.now_monotonic_s + remaining as f64 / 1000.0)
            .min(w.process_deadline_monotonic_s)
            .min(w.lease_deadline_monotonic_s);
        require(deadline > w.now_monotonic_s, "token_path_grant_expired")?;
        let encoded = canonical(&body, c)?;
        let subject = (
            scope["run_id"].as_str().ok_or(ContractError(c))?.to_owned(),
            scope["request_id"]
                .as_str()
                .ok_or(ContractError(c))?
                .to_owned(),
            scope["control"]["request_attempt"]
                .as_u64()
                .ok_or(ContractError(c))?,
        );
        require(!self.seen.contains(&subject), "token_path_grant_replay")?;
        require(self.seen.len() < self.capacity, "token_path_grant_capacity")?;
        self.seen.insert(subject);
        Ok(AdmittedGrant {
            statement: body,
            digest: crate::canonical::sha256_prefixed(&encoded),
            deadline,
            claimed: false,
        })
    }
}
/// Immutable verified receipt for downstream evidence sealing. Possession proves
/// this cursor's signature/scope/order checks, not an ACK, liveness or cleanup.
#[derive(Debug)]
pub struct VerifiedReceipt {
    sequence: u64,
    token_ids: Vec<u32>,
    status: String,
    error_code: Option<String>,
    receipt_digest: String,
    envelope_bytes: Vec<u8>,
}
impl VerifiedReceipt {
    pub fn sequence(&self) -> u64 {
        self.sequence
    }
    pub fn token_ids(&self) -> &[u32] {
        &self.token_ids
    }
    pub fn status(&self) -> &str {
        &self.status
    }
    pub fn error_code(&self) -> Option<&str> {
        self.error_code.as_deref()
    }
    pub fn receipt_digest(&self) -> &str {
        &self.receipt_digest
    }
    pub fn envelope_bytes(&self) -> &[u8] {
        &self.envelope_bytes
    }
}

/// The cursor checks signature, current authority, deadline, sequence, chained
/// digest, token budget and terminality before changing any local state.
pub struct ReceiptCursor {
    grant: AdmittedGrant,
    key: Value,
    sequence: u64,
    index: u64,
    previous: String,
    terminal: Option<String>,
}
impl ReceiptCursor {
    pub fn new(grant: AdmittedGrant, key: Value) -> Self {
        let previous = grant.digest.clone();
        Self {
            grant,
            key,
            sequence: 0,
            index: 0,
            previous,
            terminal: None,
        }
    }
    pub fn terminal(&self) -> Option<&str> {
        self.terminal.as_deref()
    }
    pub fn accept(
        &mut self,
        env: &Value,
        observe: impl FnOnce() -> (f64, bool),
    ) -> Result<Vec<u32>, ContractError> {
        Ok(self.accept_verified(env, observe)?.token_ids)
    }
    /// Private-pipe demultiplexing plus receipt verification; no inbox, ACK or
    /// remote cancellation is implied. Host code independently pins node/command.
    pub fn accept_progress(
        &mut self,
        raw: &[u8],
        command_id: &str,
        node_id: &str,
        observe: impl FnOnce() -> (f64, bool),
    ) -> Result<VerifiedReceipt, ContractError> {
        let c = "token_progress_frame_invalid";
        let frame = crate::strict::object(raw, crate::native::MAX_RECEIPT_BYTES + 1024)
            .map_err(|_| ContractError(c))?;
        crate::native::validate_progress(&frame, node_id)?;
        require(frame["command_id"] == command_id, c)?;
        self.accept_verified(&frame["receipt"], observe)
    }
    pub fn accept_verified(
        &mut self,
        env: &Value,
        observe: impl FnOnce() -> (f64, bool),
    ) -> Result<VerifiedReceipt, ContractError> {
        let scope = &self.grant.statement["scope"];
        let body = verify(
            env,
            &self.key,
            scope["entry_endpoint_id"]
                .as_str()
                .ok_or(ContractError("token_path_receipt_signature"))?,
            &scope["entry_key_digest"],
            "token_path_receipt_signature",
        )?;
        let (now, current) = observe();
        require(current, "token_path_grant_stale")?;
        require(
            now.is_finite() && now >= 0.0 && now < self.grant.deadline,
            "token_path_grant_expired",
        )?;
        require(self.terminal.is_none(), "token_path_receipt_terminal")?;
        let c = "token_path_receipt_invalid";
        exact(
            &body,
            &[
                "protocol",
                "grant_digest",
                "sequence",
                "start_index",
                "end_index",
                "token_ids",
                "status",
                "error_code",
                "stage_receipt_refs",
                "previous_receipt_digest",
            ],
            c,
        )?;
        require(body["protocol"] == RECEIPT_PROTOCOL, c)?;
        require(
            body["grant_digest"] == self.grant.digest,
            "token_path_receipt_subject",
        )?;
        for f in ["sequence", "start_index", "end_index"] {
            require(int(&body[f], 0, MAX_INT), c)?;
        }
        require(
            body["sequence"] == self.sequence && body["start_index"] == self.index,
            "token_path_receipt_order",
        )?;
        require(
            body["previous_receipt_digest"] == self.previous,
            "token_path_receipt_chain",
        )?;
        let tokens = body["token_ids"].as_array().ok_or(ContractError(c))?;
        let max = self.grant.statement["max_new_tokens"]
            .as_u64()
            .ok_or(ContractError(c))?;
        require(
            tokens.len() as u64 <= max && tokens.iter().all(|t| int(t, 0, i32::MAX as u64)),
            c,
        )?;
        let end = self
            .index
            .checked_add(tokens.len() as u64)
            .ok_or(ContractError(c))?;
        require(body["end_index"] == end && end <= max, c)?;
        let stops = self.grant.statement["stop_token_ids"]
            .as_array()
            .ok_or(ContractError(c))?;
        require(tokens.iter().all(|t| !stops.contains(t)), c)?;
        let status = body["status"].as_str().ok_or(ContractError(c))?;
        require(
            ["DECODING", "COMPLETED", "CANCELLED", "FAILED"].contains(&status)
                && (status != "DECODING" || !tokens.is_empty())
                && (!["CANCELLED", "FAILED"].contains(&status) || tokens.is_empty())
                && if status == "FAILED" {
                    name(&body["error_code"])
                } else {
                    body["error_code"].is_null()
                },
            c,
        )?;
        let refs = body["stage_receipt_refs"]
            .as_array()
            .ok_or(ContractError(c))?;
        require(refs.len() <= 64, c)?;
        let mut seen = BTreeSet::new();
        let participants = scope["participants"].as_array().ok_or(ContractError(c))?;
        for r in refs {
            exact(r, &["node_id", "receipt_digest"], c)?;
            require(
                r["node_id"].is_string()
                    && participants.iter().any(|p| p["node_id"] == r["node_id"])
                    && digest(&r["receipt_digest"])
                    && seen.insert(r["node_id"].as_str()),
                c,
            )?;
        }
        let previous = crate::canonical::sha256_prefixed(&canonical(&body, c)?);
        let next = self.sequence.checked_add(1).ok_or(ContractError(c))?;
        let output = tokens
            .iter()
            .map(|t| t.as_u64().map(|n| n as u32).ok_or(ContractError(c)))
            .collect::<Result<Vec<_>, _>>()?;
        let receipt = VerifiedReceipt {
            sequence: self.sequence,
            token_ids: output,
            status: status.to_owned(),
            error_code: body["error_code"].as_str().map(str::to_owned),
            receipt_digest: previous.clone(),
            envelope_bytes: canonical(env, "token_path_receipt_signature")?,
        };
        self.previous = previous;
        self.sequence = next;
        self.index = end;
        if status != "DECODING" {
            self.terminal = Some(status.to_owned());
        }
        Ok(receipt)
    }
}
