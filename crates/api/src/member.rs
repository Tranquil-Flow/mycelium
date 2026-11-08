// SPDX-License-Identifier: AGPL-3.0-only
//! One node's membership session as `RemoteMacMember` uses it (`mycelium_node/membership.py:121-440`
//! @ f4e366e9): the signed `join_request`, `accept_join` of the seed's acceptance, and the live
//! state a joined member holds (generation, seed key pin, lease) - the source of
//! `RemoteMacMember.membership_binding()` and of the live `status()` membership projection.
use crate::{schema::PublicError, seed::SeedClient};
use budgets::{BudgetId, Entries};
use protocol::{identity::Identity, membership};
use serde_json::{Value, json};
use std::{collections::HashMap, sync::Arc};

const JOIN_REQUEST_PROTOCOL: &str = "mycelium.membership.join_request.v1";
const JOIN_ACCEPTANCE_PROTOCOL: &str = "mycelium.membership.join_acceptance.v1";
const RESUME_REQUEST_PROTOCOL: &str = "mycelium.membership.resume_request.v1";
const RESUME_ACCEPTANCE_PROTOCOL: &str = "mycelium.membership.resume_acceptance.v1";
pub const SOFTWARE_VERSION: &str = "mycelium-a13-macos-demo/1";
const PEER_CLASS: &str = "mac_mlx_iroh";
const HEARTBEAT_PROTOCOL: &str = "mycelium.membership.heartbeat.v1";
const LEASE_RENEWAL_PROTOCOL: &str = "mycelium.membership.lease_renewal.v1";
/// `_LIFECYCLE_STATES` (mycelium_node/membership.py:47).
const LIFECYCLE_STATES: [&str; 6] = [
    "NEW",
    "CONFIGURED",
    "RUNNING",
    "DRAINING",
    "STOPPING",
    "STOPPED",
];
/// `_MAX_REPLAY_IDS` (mycelium_node/membership.py:46 @ f4e366e9): a CPython count literal that is not in
/// the L1 registry yet (seam request SE-L10-REPLAY-BOUND). The full-window branch has no oracle vector
/// (it needs 4096 live ids); the constant is the source's.
const MAX_REPLAY_IDS: usize = 4096;

/// `NodeMembershipError(code)`, or the `MembershipContractError(code)` the contract verifier raised
/// (the source's `accept_handoff` catches only the first).
#[derive(Clone, Debug, PartialEq)]
pub enum MemberError {
    Membership(String),
    Contract(String),
    /// A builtin `ValueError` without a closed code (`_segment`, `previous_generation`).
    Value,
}
impl MemberError {
    fn membership(code: &str) -> Self {
        Self::Membership(code.to_owned())
    }
    pub fn code(&self) -> &str {
        match self {
            Self::Membership(code) | Self::Contract(code) => code,
            Self::Value => "peer_operation_failed",
        }
    }
}
impl From<MemberError> for PublicError {
    /// Only for callers that do not translate (never the source's `except` clause).
    fn from(_: MemberError) -> Self {
        PublicError::new(409, "action_not_completed")
    }
}

/// `{swarm_id, node_id, seed_key_digest}` of `RemoteMacMember.membership_binding()`.
#[derive(Clone, Debug, PartialEq)]
pub struct Binding {
    pub swarm_id: String,
    pub node_id: String,
    pub seed_key_digest: String,
}

pub fn limits() -> protocol::TimeLimits {
    let seconds = |id: BudgetId| {
        id.span(Entries::new(0))
            .map_or(0.0, |s| s.millis() as f64 / 1000.0)
    };
    protocol::TimeLimits {
        max_ttl_seconds: seconds(BudgetId::PyMyceliumMembershipContractsMaxMessageTtlSecondsL30),
        clock_skew_seconds: seconds(BudgetId::PyMyceliumMembershipContractsMaxClockSkewSecondsL31),
    }
}
fn ttl() -> f64 {
    BudgetId::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
        .span(Entries::new(0))
        .map_or(0.0, |s| s.millis() as f64 / 1000.0)
}

#[derive(Debug)]
pub struct Session {
    node_id: String,
    swarm_id: String,
    seed_node_id: String,
    endpoint_id: String,
    signer_key_digest: String,
    incarnation: String,
    pending_join_message_id: Option<String>,
    emitted: Vec<String>,
    seen: HashMap<String, f64>,
    generation: Option<u64>,
    seed_key_digest: Option<String>,
    seed_endpoint_id: Option<String>,
    lease_expires_at: Option<f64>,
    /// `_pending_resume`: the request message id, previous generation and previous incarnation.
    pending_resume: Option<(String, u64, String)>,
    /// `_heartbeat_sequence`.
    heartbeat_sequence: u64,
    /// `_pending_liveness`: heartbeat message id -> its `expires_at` (the liveness source only matters to
    /// `activation_receipt`, which is not ported: every heartbeat here is `scheduled_heartbeat`).
    pending_liveness: HashMap<String, f64>,
    /// `RemoteMacMember._seed_client`: the pinned HTTPS client this membership was joined or resumed over.
    client: Option<Arc<SeedClient>>,
}

impl Session {
    pub fn new(
        node_id: &str,
        swarm_id: &str,
        seed_node_id: &str,
        signer: &Identity,
        incarnation: &str,
    ) -> Self {
        Self {
            node_id: node_id.to_owned(),
            swarm_id: swarm_id.to_owned(),
            seed_node_id: seed_node_id.to_owned(),
            endpoint_id: signer.endpoint_id().to_owned(),
            signer_key_digest: signer.verification_key_digest(),
            incarnation: incarnation.to_owned(),
            pending_join_message_id: None,
            emitted: Vec::new(),
            seen: HashMap::new(),
            generation: None,
            seed_key_digest: None,
            seed_endpoint_id: None,
            lease_expires_at: None,
            pending_resume: None,
            heartbeat_sequence: 0,
            pending_liveness: HashMap::new(),
            client: None,
        }
    }
    /// Keep the seed client of this membership (`self._seed_client = client`).
    pub fn attach_client(&mut self, client: SeedClient) {
        self.client = Some(Arc::new(client));
    }
    pub fn client(&self) -> Option<Arc<SeedClient>> {
        self.client.clone()
    }
    pub fn node_id(&self) -> &str {
        &self.node_id
    }
    pub fn generation(&self) -> Option<u64> {
        self.generation
    }
    pub fn lease_expires_at(&self) -> Option<f64> {
        self.lease_expires_at
    }
    pub fn seed_key_digest(&self) -> Option<&str> {
        self.seed_key_digest.as_deref()
    }
    /// `membership_binding()`: only a joined member has one.
    pub fn binding(&self) -> Option<Binding> {
        Some(Binding {
            swarm_id: self.swarm_id.clone(),
            node_id: self.node_id.clone(),
            seed_key_digest: self.seed_key_digest.clone()?,
        })
    }

    /// Additive transfer identity, derived only from an accepted live session.
    /// This is not a serving grant or a resource/consent admission.
    pub fn shard_identity(&self, signer: &Identity, now: f64) -> Result<Value, MemberError> {
        if signer.endpoint_id() != self.endpoint_id
            || signer.verification_key_digest() != self.signer_key_digest
        {
            return Err(MemberError::membership("joined_shard_identity_mismatch"));
        }
        if !now.is_finite() || self.lease_expires_at.is_none_or(|lease| lease <= now) {
            return Err(MemberError::membership("joined_shard_lease_expired"));
        }
        let generation = self
            .generation
            .ok_or_else(|| MemberError::membership("joined_shard_not_joined"))?;
        Ok(json!({"swarm_id":self.swarm_id,"node_id":self.node_id,
            "endpoint_id":self.endpoint_id,"verification_key_digest":self.signer_key_digest,
            "incarnation":self.incarnation,"generation":generation,
            "seed_key_digest":self.seed_key_digest}))
    }

    /// `join_request`: the signed request carrying the invite nonce; `message_id` is the one
    /// identifier the source's id source yields first.
    pub fn join_request(
        &mut self,
        signer: &Identity,
        now: f64,
        message_id: &str,
        invite_nonce: &str,
        endpoint_addrs: &[String],
    ) -> Result<Value, MemberError> {
        if self.generation.is_some() {
            return Err(MemberError::membership("membership_already_joined"));
        }
        if self.pending_join_message_id.is_some() {
            return Err(MemberError::membership("join_request_already_pending"));
        }
        if !now.is_finite() {
            return Err(MemberError::membership("membership_clock_invalid"));
        }
        if self.emitted.iter().any(|m| m == message_id) {
            return Err(MemberError::membership("membership_message_id_reused"));
        }
        self.emitted.push(message_id.to_owned());
        let message = json!({
            "protocol": JOIN_REQUEST_PROTOCOL,
            "message_id": message_id,
            "swarm_id": self.swarm_id,
            "sender_node_id": self.node_id,
            "sender_endpoint_id": self.endpoint_id,
            "recipient_node_id": self.seed_node_id,
            "incarnation": self.incarnation,
            "generation": 0,
            "issued_at": now,
            "expires_at": now + ttl(),
            "invite_nonce": invite_nonce,
            "software_version": SOFTWARE_VERSION,
            "endpoint_addr": {"id": self.endpoint_id, "addrs": endpoint_addrs},
            "peer_class": PEER_CLASS,
            "runtime_capability": {
                "runtime_backend": "mlx", "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
        });
        membership::validate(&message, limits())
            .map_err(|e| MemberError::Contract(e.0.to_owned()))?;
        let signature = signer
            .sign(&message)
            .map_err(|_| MemberError::Contract("membership_signature_invalid".into()))?;
        self.pending_join_message_id = Some(message_id.to_owned());
        Ok(json!({
            "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
            "signature": signature,
            "verification_key": signer.public_key_record(),
            "message": message,
        }))
    }

    /// `accept_join`: verify the seed's acceptance against the pinned key and this request.
    pub fn accept_join(
        &mut self,
        envelope: &Value,
        now: f64,
        seed_key_digest: &str,
    ) -> Result<Value, MemberError> {
        if !now.is_finite() {
            return Err(MemberError::membership("membership_clock_invalid"));
        }
        let message = membership::verify(
            envelope,
            membership::Expected {
                protocol: JOIN_ACCEPTANCE_PROTOCOL,
                key_digest: seed_key_digest,
                swarm_id: None,
                sender_node_id: None,
                sender_endpoint_id: None,
                recipient_node_id: None,
            },
            now,
            limits(),
        )
        .map_err(|e| MemberError::Contract(e.0.to_owned()))?;
        let id = message["message_id"].as_str().unwrap_or_default();
        if self.seen.contains_key(id) {
            return Err(MemberError::membership("membership_message_replayed"));
        }
        if self.generation.is_some() {
            return Err(MemberError::membership("membership_already_joined"));
        }
        let expected = self.pending_join_message_id.is_some()
            && message["swarm_id"] == self.swarm_id
            && message["sender_node_id"] == self.seed_node_id
            && message["recipient_node_id"] == self.node_id
            && Some(&message["request_message_id"])
                == self
                    .pending_join_message_id
                    .as_ref()
                    .map(|m| json!(m))
                    .as_ref()
            && message["accepted_node_id"] == self.node_id
            && message["accepted_incarnation"] == self.incarnation;
        if !expected {
            return Err(MemberError::membership("join_acceptance_mismatch"));
        }
        self.seen.insert(
            id.to_owned(),
            message["expires_at"].as_f64().unwrap_or_default(),
        );
        let lease = message["lease_expires_at"].as_f64().unwrap_or_default();
        if lease <= now {
            return Err(MemberError::membership("membership_lease_expired"));
        }
        self.generation = message["membership_generation"].as_u64();
        self.seed_key_digest = Some(seed_key_digest.to_owned());
        self.seed_endpoint_id = message["sender_endpoint_id"].as_str().map(str::to_owned);
        self.lease_expires_at = Some(lease);
        self.pending_join_message_id = None;
        Ok(message)
    }
}

/// `_SEGMENT_RE`: `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
fn is_segment(text: &str) -> bool {
    let mut chars = text.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && text.len() <= 128
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

impl Session {
    /// `resume_request`: sign a restart request under the durable device identity, naming the
    /// generation and incarnation the seed last accepted.
    pub fn resume_request(
        &mut self,
        signer: &Identity,
        now: f64,
        message_id: &str,
        previous_generation: u64,
        previous_incarnation: &str,
        endpoint_addrs: &[String],
    ) -> Result<Value, MemberError> {
        if self.generation.is_some() {
            return Err(MemberError::membership("membership_already_joined"));
        }
        if self.pending_join_message_id.is_some() || self.pending_resume.is_some() {
            return Err(MemberError::membership("resume_request_already_pending"));
        }
        if previous_generation < 1 || !is_segment(previous_incarnation) {
            return Err(MemberError::Value);
        }
        if previous_incarnation == self.incarnation {
            return Err(MemberError::membership(
                "membership_resume_incarnation_invalid",
            ));
        }
        if !now.is_finite() {
            return Err(MemberError::membership("membership_clock_invalid"));
        }
        if !is_segment(message_id) {
            return Err(MemberError::Value);
        }
        if self.emitted.iter().any(|m| m == message_id) {
            return Err(MemberError::membership("membership_message_id_reused"));
        }
        self.emitted.push(message_id.to_owned());
        let message = json!({
            "protocol": RESUME_REQUEST_PROTOCOL,
            "message_id": message_id,
            "swarm_id": self.swarm_id,
            "sender_node_id": self.node_id,
            "sender_endpoint_id": self.endpoint_id,
            "recipient_node_id": self.seed_node_id,
            "incarnation": self.incarnation,
            "generation": previous_generation,
            "issued_at": now,
            "expires_at": now + ttl(),
            "previous_incarnation": previous_incarnation,
            "software_version": SOFTWARE_VERSION,
            "endpoint_addr": {"id": self.endpoint_id, "addrs": endpoint_addrs},
            "peer_class": PEER_CLASS,
            "runtime_capability": {
                "runtime_backend": "mlx", "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
        });
        let signature = signer
            .sign(&message)
            .map_err(|_| MemberError::Contract("membership_signature_invalid".into()))?;
        self.pending_resume = Some((
            message_id.to_owned(),
            previous_generation,
            previous_incarnation.to_owned(),
        ));
        Ok(json!({
            "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
            "signature": signature,
            "verification_key": signer.public_key_record(),
            "message": message,
        }))
    }

    /// `accept_resume`: install the seed's authorised generation advance after a restart.
    pub fn accept_resume(
        &mut self,
        envelope: &Value,
        now: f64,
        seed_key_digest: &str,
    ) -> Result<Value, MemberError> {
        if !now.is_finite() {
            return Err(MemberError::membership("membership_clock_invalid"));
        }
        let message = membership::verify(
            envelope,
            membership::Expected {
                protocol: RESUME_ACCEPTANCE_PROTOCOL,
                key_digest: seed_key_digest,
                swarm_id: None,
                sender_node_id: None,
                sender_endpoint_id: None,
                recipient_node_id: None,
            },
            now,
            limits(),
        )
        .map_err(|e| MemberError::Contract(e.0.to_owned()))?;
        let id = message["message_id"].as_str().unwrap_or_default();
        if self.seen.contains_key(id) {
            return Err(MemberError::membership("membership_message_replayed"));
        }
        if self.generation.is_some() {
            return Err(MemberError::membership("membership_already_joined"));
        }
        let expected = self
            .pending_resume
            .as_ref()
            .is_some_and(|(request, previous, _)| {
                message["swarm_id"] == self.swarm_id
                    && message["sender_node_id"] == self.seed_node_id
                    && message["recipient_node_id"] == self.node_id
                    && message["request_message_id"] == request.as_str()
                    && message["accepted_node_id"] == self.node_id
                    && message["accepted_incarnation"] == self.incarnation
                    && message["previous_membership_generation"] == *previous
                    && message["membership_generation"] == *previous + 1
            });
        if !expected {
            return Err(MemberError::membership("resume_acceptance_mismatch"));
        }
        self.seen.insert(
            id.to_owned(),
            message["expires_at"].as_f64().unwrap_or_default(),
        );
        let lease = message["lease_expires_at"].as_f64().unwrap_or_default();
        if lease <= now {
            return Err(MemberError::membership("membership_lease_expired"));
        }
        self.generation = message["membership_generation"].as_u64();
        self.seed_key_digest = Some(seed_key_digest.to_owned());
        self.seed_endpoint_id = message["sender_endpoint_id"].as_str().map(str::to_owned);
        self.lease_expires_at = Some(lease);
        self.pending_resume = None;
        Ok(message)
    }
}

/// `_finite_now`: the session clock must give a finite number.
fn finite_now(clock: &dyn Fn() -> f64) -> Result<f64, MemberError> {
    let now = clock();
    if now.is_finite() {
        Ok(now)
    } else {
        Err(MemberError::membership("membership_clock_invalid"))
    }
}

impl Session {
    fn joined(&self) -> Option<(u64, f64)> {
        match (
            &self.seed_key_digest,
            &self.seed_endpoint_id,
            self.generation,
            self.lease_expires_at,
        ) {
            (Some(_), Some(_), Some(generation), Some(lease)) => Some((generation, lease)),
            _ => None,
        }
    }

    /// `_check_incoming`: a seen id is a replay while it has not expired; a full window refuses more.
    fn check_incoming(&self, message_id: &str, now: f64) -> Result<(), MemberError> {
        if self
            .seen
            .get(message_id)
            .is_some_and(|expiry| *expiry >= now)
        {
            return Err(MemberError::membership("membership_message_replayed"));
        }
        if self.seen.values().filter(|expiry| **expiry >= now).count() >= MAX_REPLAY_IDS {
            return Err(MemberError::membership("membership_replay_window_full"));
        }
        Ok(())
    }

    /// `_remember_incoming`: check, drop the expired ids, remember this one.
    fn remember_incoming(
        &mut self,
        message_id: &str,
        expires_at: f64,
        now: f64,
    ) -> Result<(), MemberError> {
        self.check_incoming(message_id, now)?;
        self.seen.retain(|_, expiry| *expiry >= now);
        self.seen.insert(message_id.to_owned(), expires_at);
        Ok(())
    }

    /// `heartbeat(lifecycle_state, active_requests, force=True)` = `_emit_liveness(scheduled_heartbeat)`
    /// (membership.py:850-910): `RemoteMacMember.renew` forces it, so the suppress flag (only an
    /// `activation_receipt` acceptance sets it) is not modelled. The clock is read twice, as the source
    /// does (pending-liveness pruning, then the common header); the id source once, after the lease check.
    pub fn heartbeat(
        &mut self,
        signer: &Identity,
        clock: &dyn Fn() -> f64,
        id_source: &dyn Fn() -> String,
        lifecycle_state: &str,
        active_requests: u64,
    ) -> Result<Value, MemberError> {
        if !LIFECYCLE_STATES.contains(&lifecycle_state) {
            return Err(MemberError::Value);
        }
        let now = finite_now(clock)?;
        self.pending_liveness
            .retain(|_, expires_at| *expires_at > now);
        self.heartbeat_sequence += 1;
        let sequence = self.heartbeat_sequence;
        let built = self.build_heartbeat(
            signer,
            clock,
            id_source,
            lifecycle_state,
            active_requests,
            sequence,
        );
        if built.is_err() {
            self.heartbeat_sequence -= 1;
        }
        built
    }

    fn build_heartbeat(
        &mut self,
        signer: &Identity,
        clock: &dyn Fn() -> f64,
        id_source: &dyn Fn() -> String,
        lifecycle_state: &str,
        active_requests: u64,
        sequence: u64,
    ) -> Result<Value, MemberError> {
        // `_post_join_common`
        let (Some((generation, lease)), Some(_)) = (self.joined(), &self.seed_endpoint_id) else {
            return Err(MemberError::membership("membership_not_joined"));
        };
        let now = finite_now(clock)?;
        if now >= lease {
            return Err(MemberError::membership("membership_lease_expired"));
        }
        let message_id = id_source();
        if !is_segment(&message_id) {
            return Err(MemberError::Value);
        }
        if self.emitted.contains(&message_id) {
            return Err(MemberError::membership("membership_message_id_reused"));
        }
        self.emitted.push(message_id.clone());
        let message = json!({
            "protocol": HEARTBEAT_PROTOCOL,
            "message_id": message_id,
            "swarm_id": self.swarm_id,
            "sender_node_id": self.node_id,
            "sender_endpoint_id": self.endpoint_id,
            "recipient_node_id": self.seed_node_id,
            "incarnation": self.incarnation,
            "generation": generation,
            "issued_at": now,
            "expires_at": (now + ttl()).min(lease),
            "heartbeat_sequence": sequence,
            "lifecycle_state": lifecycle_state,
            "route_ready": false,
            "active_requests": active_requests,
            "liveness_source": "scheduled_heartbeat",
            "activity_receipt_digest": Value::Null,
            "activity_peer_node_id": Value::Null,
        });
        membership::validate(&message, limits())
            .map_err(|e| MemberError::Contract(e.0.to_owned()))?;
        let signature = signer
            .sign(&message)
            .map_err(|_| MemberError::Contract("membership_signature_invalid".into()))?;
        self.pending_liveness.insert(
            message_id,
            message["expires_at"].as_f64().unwrap_or_default(),
        );
        Ok(json!({
            "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
            "signature": signature,
            "verification_key": signer.public_key_record(),
            "message": message,
        }))
    }

    /// `accept_lease_renewal` (membership.py:589-650): verify the seed's renewal of one heartbeat against
    /// the pinned key, the replay window and the lease ordering, then install the new lease. Seed key
    /// rotation (`trust_seed_rotation`) is not ported: the pinned key and endpoint are the only signer.
    pub fn accept_lease_renewal(
        &mut self,
        envelope: &Value,
        clock: &dyn Fn() -> f64,
        heartbeat_message_id: &str,
    ) -> Result<Value, MemberError> {
        if !is_segment(heartbeat_message_id) {
            return Err(MemberError::Value);
        }
        let (Some((generation, old_lease)), Some(digest), Some(endpoint)) = (
            self.joined(),
            self.seed_key_digest.clone(),
            self.seed_endpoint_id.clone(),
        ) else {
            return Err(MemberError::membership("membership_not_joined"));
        };
        let now = finite_now(clock)?;
        // `_seed_identity_for_envelope` without a rotation transition.
        let signer_digest = envelope
            .get("verification_key")
            .and_then(|r| r.get("verification_key_digest"));
        let signer_endpoint = envelope
            .get("signature")
            .and_then(|r| r.get("signer_endpoint_id"));
        if !envelope["verification_key"].is_object()
            || !envelope["signature"].is_object()
            || signer_digest != Some(&json!(digest))
            || signer_endpoint != Some(&json!(endpoint))
        {
            return Err(MemberError::membership("membership_seed_rotation_mismatch"));
        }
        let message = membership::verify(
            envelope,
            membership::Expected {
                protocol: LEASE_RENEWAL_PROTOCOL,
                key_digest: &digest,
                swarm_id: None,
                sender_node_id: None,
                sender_endpoint_id: None,
                recipient_node_id: None,
            },
            now,
            limits(),
        )
        .map_err(|e| MemberError::Contract(e.0.to_owned()))?;
        if message["swarm_id"] != self.swarm_id
            || message["sender_node_id"] != self.seed_node_id
            || message["sender_endpoint_id"] != endpoint.as_str()
            || message["recipient_node_id"] != self.node_id
            || message["generation"] != generation
        {
            return Err(MemberError::membership("membership_message_mismatch"));
        }
        let id = message["message_id"]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        self.check_incoming(&id, now)?;
        if message["heartbeat_message_id"] != heartbeat_message_id
            || message["member_incarnation"] != self.incarnation
            || message["membership_generation"] != generation
        {
            return Err(MemberError::membership("membership_lease_renewal_mismatch"));
        }
        if !self.pending_liveness.contains_key(heartbeat_message_id) {
            return Err(MemberError::membership("membership_lease_renewal_unknown"));
        }
        let renewed_until = message["lease_expires_at"].as_f64().unwrap_or_default();
        let expires_at = message["expires_at"].as_f64().unwrap_or_default();
        if message["issued_at"].as_f64().unwrap_or_default() >= old_lease {
            return Err(MemberError::membership(
                "membership_lease_renewal_causality",
            ));
        }
        if renewed_until <= old_lease || now >= renewed_until || expires_at < renewed_until {
            return Err(MemberError::membership("membership_lease_renewal_stale"));
        }
        self.remember_incoming(&id, expires_at, now)?;
        self.lease_expires_at = Some(renewed_until);
        self.pending_liveness.remove(heartbeat_message_id);
        Ok(message)
    }
}
