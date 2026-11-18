// SPDX-License-Identifier: AGPL-3.0-only
//! `SeedCoordinator` of v0.1.4 (`mycelium_seed/coordinator.py`, membership contract v1): the stateful, signing,
//! durable composition of the pure decisions in `protocol::{seed_coordinator, seed_members}`.
//!
//! One `Coordinator` owns the member table (`protocol::seed_members::SeedTable`), the seed signer and one sqlite
//! file (`crate::store::Store`, private). Every mutation is committed in one transaction together with the exact signed
//! answer, so a retry receives the same bytes and a restart resumes exactly (RED/GREEN against the executed
//! oracle in `tests/oracle_server.rs`). Not in this slice: route assignment, `assignment_offer`, authority
//! rotation endpoints, the membership-v2 profile contract (all refuse or are absent, never approximated).
use crate::store::{Binding, Consume, Envelope, Store};
use budgets::{BudgetId, Entries};
use protocol::{
    TimeLimits, canonical,
    identity::Identity,
    invite, membership,
    seed_coordinator::{self, SeedCoordinatorError, SeedFrame, bounded_deadline},
    seed_members::{self, DurableMember, SeedTable, Timing},
};
use serde_json::{Value, json};
use std::{
    path::Path,
    sync::{Arc, Mutex},
};

pub type Clock = Box<dyn Fn() -> f64 + Send + Sync>;

#[cfg(test)]
mod guard_tests;
pub type IdSource = Box<dyn FnMut() -> String + Send>;

pub const SEED_SIGNED_ENVELOPE_PROTOCOL: &str = "mycelium.seed.signed_envelope.v1";
pub const SEED_IDENTITY_PROTOCOL: &str = "mycelium.seed.identity.v1";
pub const SEED_RECEIPT_PROTOCOL: &str = "mycelium.seed.receipt.v1";

/// A stable refusal code (`SeedCoordinatorError.code`, `InviteError.code`, `MembershipContractError.code`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{code}")]
pub struct SeedError {
    pub code: String,
    /// The oracle raised a Python built-in (`ValueError`, ...) with no `.code`: HTTP answers 500 on the join
    /// route and `seed_http_request_invalid` elsewhere.
    pub accident: bool,
}

impl SeedError {
    pub fn new(code: &str) -> Self {
        Self {
            code: code.to_owned(),
            accident: false,
        }
    }
    pub fn accident(code: &str) -> Self {
        Self {
            code: code.to_owned(),
            accident: true,
        }
    }
}
impl From<SeedCoordinatorError> for SeedError {
    fn from(e: SeedCoordinatorError) -> Self {
        Self::new(e.0)
    }
}
impl From<invite::InviteError> for SeedError {
    fn from(e: invite::InviteError) -> Self {
        Self::new(e.0)
    }
}
impl From<protocol::ContractError> for SeedError {
    fn from(e: protocol::ContractError) -> Self {
        Self::new(e.0)
    }
}

/// The constructor arguments of `SeedCoordinator` that are values.
#[derive(Debug, Clone)]
pub struct Options {
    pub swarm_id: String,
    pub seed_node_id: String,
    pub incarnation: String,
    pub seed_url: Option<String>,
    pub lease_seconds: f64,
    pub message_ttl_seconds: f64,
    pub keepalive_interval_seconds: f64,
    pub evidence_freshness_seconds: f64,
}

fn seconds(id: BudgetId) -> Result<f64, SeedError> {
    id.span(Entries::new(0))
        .map(|s| s.millis() as f64 / 1000.0)
        .map_err(|_| SeedError::new("seed_budget_unavailable"))
}

impl Options {
    /// The v0.1.4 defaults (`coordinator.py:180-183`) from the budget registry.
    pub fn new(swarm_id: &str, seed_node_id: &str, incarnation: &str) -> Result<Self, SeedError> {
        Ok(Self {
            swarm_id: swarm_id.to_owned(),
            seed_node_id: seed_node_id.to_owned(),
            incarnation: incarnation.to_owned(),
            seed_url: None,
            lease_seconds: seconds(BudgetId::PyFinalMyceliumSeedCoordinatorLeaseSecondsL180C31)?,
            message_ttl_seconds: seconds(
                BudgetId::PyFinalMyceliumSeedCoordinatorMessageTtlSecondsL181C37,
            )?,
            keepalive_interval_seconds: seconds(
                BudgetId::PyFinalMyceliumSeedCoordinatorKeepaliveIntervalSecondsL182C44,
            )?,
            evidence_freshness_seconds: seconds(
                BudgetId::PyFinalMyceliumSeedCoordinatorEvidenceFreshnessSecondsL183C44,
            )?,
        })
    }
}

/// `MAX_MESSAGE_TTL_SECONDS` and the clock skew of `mycelium_membership.contracts`.
pub fn limits() -> Result<TimeLimits, SeedError> {
    Ok(TimeLimits {
        max_ttl_seconds: seconds(BudgetId::PyMyceliumMembershipContractsMaxMessageTtlSecondsL30)?,
        clock_skew_seconds: seconds(BudgetId::PyMyceliumMembershipContractsMaxClockSkewSecondsL31)?,
    })
}

pub(crate) fn is_segment(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}

fn require_segment(s: &str) -> Result<(), SeedError> {
    if is_segment(s) {
        Ok(())
    } else {
        Err(SeedError::accident("seed_argument_invalid"))
    }
}

struct Inner {
    table: SeedTable,
    /// The snapshot row of `table` ([`SeedTable::core_snapshot`]: everything but the append-only journals) as
    /// this process last committed or loaded it (what a commit is diffed against). Its size does not grow with
    /// the heartbeat history; the journals' unsaved rows are tracked by the table itself.
    baseline: Value,
    store: Store,
    seed_url: Option<String>,
    rotation_envelope: Option<Value>,
}

pub struct Coordinator {
    options: Options,
    limits: TimeLimits,
    timing: Timing,
    signer: Arc<Identity>,
    key_record: Value,
    clock: Clock,
    ids: Arc<Mutex<IdSource>>,
    inner: Mutex<Inner>,
}

fn id_source(ids: &Arc<Mutex<IdSource>>) -> Box<dyn FnMut() -> String + Send> {
    let shared = ids.clone();
    Box::new(move || match shared.lock() {
        Ok(mut draw) => (draw)(),
        Err(poisoned) => (poisoned.into_inner())(),
    })
}

/// A table of the stored snapshot row alone (members, replay, rotations ...; no journal rows): what the
/// member-authority guard compares.
fn table_from(
    ids: &Arc<Mutex<IdSource>>,
    snapshot: Option<&Value>,
) -> Result<SeedTable, SeedError> {
    let source = id_source(ids);
    match snapshot {
        Some(s) => Ok(SeedTable::restore(s, source)?),
        None => Ok(SeedTable::new(source)),
    }
}

fn is_v2_form(envelope: &Value) -> bool {
    envelope["message"]
        .as_object()
        .is_some_and(|m| m.contains_key("peer_profile"))
}

fn renewal_key(node: &str, generation: u64, heartbeat_id: &str) -> String {
    format!("{node}|{generation}|{heartbeat_id}")
}

impl Coordinator {
    /// `SeedCoordinator(...)` over one database file: validates the arguments, binds the database to
    /// (swarm, seed node, seed key) and loads the committed members.
    pub fn open(
        database: &Path,
        signer: Identity,
        options: Options,
        clock: Clock,
        ids: IdSource,
    ) -> Result<Self, SeedError> {
        Self::open_inner(database, signer, options, clock, ids, None)
    }

    /// [`Coordinator::open`] with `busy` as the busy timeout of every database connection instead of v0.1.4's 30 s
    /// (a locked database then answers within `busy`: `invite_registry_unavailable` at open, `seed_state_unavailable`
    /// on a later write).
    pub fn open_with_busy_timeout(
        database: &Path,
        signer: Identity,
        options: Options,
        clock: Clock,
        ids: IdSource,
        busy: std::time::Duration,
    ) -> Result<Self, SeedError> {
        Self::open_inner(database, signer, options, clock, ids, Some(busy))
    }

    fn open_inner(
        database: &Path,
        signer: Identity,
        options: Options,
        clock: Clock,
        ids: IdSource,
        busy: Option<std::time::Duration>,
    ) -> Result<Self, SeedError> {
        for value in [
            &options.swarm_id,
            &options.seed_node_id,
            &options.incarnation,
        ] {
            require_segment(value)?;
        }
        if let Some(url) = &options.seed_url
            && (url.is_empty() || url.trim() != url)
        {
            return Err(SeedError::accident("seed_option_invalid"));
        }
        let limits = limits()?;
        let positive = |v: f64| v.is_finite() && v > 0.0;
        if !positive(options.lease_seconds)
            || !positive(options.message_ttl_seconds)
            || options.lease_seconds > limits.max_ttl_seconds
            || options.message_ttl_seconds > limits.max_ttl_seconds
            || !positive(options.keepalive_interval_seconds)
            || !positive(options.evidence_freshness_seconds)
            || options.evidence_freshness_seconds <= options.keepalive_interval_seconds
        {
            return Err(SeedError::accident("seed_option_invalid"));
        }
        let store = Store::open_with(
            database,
            &Binding {
                swarm_id: &options.swarm_id,
                seed_node_id: &options.seed_node_id,
                seed_key_digest: &signer.verification_key_digest(),
            },
            busy,
        )?;
        let ids = Arc::new(Mutex::new(ids));
        let table = match store.load_table()? {
            Some(stored) => SeedTable::restore_journaled(
                &stored.core,
                stored.renewals,
                stored.emitted,
                id_source(&ids),
            )?,
            None => SeedTable::new(id_source(&ids)),
        };
        let timing = Timing {
            keepalive_interval_seconds: options.keepalive_interval_seconds,
            evidence_freshness_seconds: options.evidence_freshness_seconds,
        };
        let key_record = signer.public_key_record();
        let seed_url = options.seed_url.clone();
        Ok(Self {
            options,
            limits,
            timing,
            signer: Arc::new(signer),
            key_record,
            clock,
            ids,
            inner: Mutex::new(Inner {
                baseline: table.core_snapshot(),
                table,
                store,
                seed_url,
                rotation_envelope: None,
            }),
        })
    }

    /// The coordinator lock, probed (log-only, [`crate::probe`]): a wait or hold of a second or more
    /// names the caller of the public method that took it (RV42 S1 relay-502 RCA).
    #[track_caller]
    fn lock(&self) -> crate::probe::Probed<'_, Inner> {
        crate::probe::lock_or_poisoned(&self.inner)
    }

    fn now(&self) -> Result<f64, SeedError> {
        let now = (self.clock)();
        if now.is_finite() {
            Ok(now)
        } else {
            Err(SeedError::new("seed_clock_invalid"))
        }
    }

    pub fn swarm_id(&self) -> &str {
        &self.options.swarm_id
    }
    pub fn seed_node_id(&self) -> &str {
        &self.options.seed_node_id
    }
    pub fn signer(&self) -> &Identity {
        &self.signer
    }
    /// Share the original immutable signing identity without copying key material.
    /// Retaining this capability does not establish current seed authority: callers
    /// must still check currentness and revocation at the operation boundary.
    pub fn shared_signer(&self) -> Arc<Identity> {
        Arc::clone(&self.signer)
    }
    #[track_caller]
    pub fn seed_url(&self) -> Option<String> {
        self.lock().seed_url.clone()
    }

    /// `bind_seed_url`: the first binding wins; a different later one is `seed_url_already_bound`.
    #[track_caller]
    pub fn bind_seed_url(&self, url: &str) -> Result<(), SeedError> {
        if url.is_empty() || url.trim() != url {
            return Err(SeedError::accident("seed_option_invalid"));
        }
        let mut inner = self.lock();
        match &inner.seed_url {
            Some(existing) if existing != url => Err(SeedError::new("seed_url_already_bound")),
            _ => {
                inner.seed_url = Some(url.to_owned());
                Ok(())
            }
        }
    }

    fn require_url(inner: &Inner) -> Result<String, SeedError> {
        inner
            .seed_url
            .clone()
            .ok_or_else(|| SeedError::new("seed_url_unbound"))
    }

    /// `mint_invite`: a seed-pinned bundle with one signed single-use token.
    #[track_caller]
    pub fn mint_invite(&self, nonce: &str, ttl_seconds: u64) -> Result<Value, SeedError> {
        let inner = self.lock();
        let url = Self::require_url(&inner)?;
        let now = self.now()?;
        Ok(invite::mint_bundle(
            &self.signer,
            &self.options.swarm_id,
            &url,
            &json!(ttl_seconds),
            nonce,
            now,
        )?)
    }

    fn signed_statement(
        &self,
        inner: &Inner,
        protocol: &str,
        extra: Option<(&str, &str)>,
    ) -> Result<Value, SeedError> {
        let url = Self::require_url(inner)?;
        let now = self.now()?;
        let expires_at = bounded_deadline(
            now,
            self.options.message_ttl_seconds,
            None,
            false,
            self.limits.max_ttl_seconds,
        )?;
        let mut statement = json!({
            "protocol": protocol,
            "swarm_id": self.options.swarm_id,
            "seed_node_id": self.options.seed_node_id,
            "seed_endpoint_id": self.signer.endpoint_id(),
            "seed_url": url,
            "issued_at": now,
            "expires_at": expires_at,
        });
        if let (Some((key, value)), Some(map)) = (extra, statement.as_object_mut()) {
            map.insert(key.to_owned(), json!(value));
        }
        let signature = self
            .signer
            .sign(&statement)
            .map_err(|_| SeedError::new("seed_signature_unavailable"))?;
        Ok(json!({
            "protocol": SEED_SIGNED_ENVELOPE_PROTOCOL,
            "statement": statement,
            "signature": signature,
            "verification_key": self.key_record,
        }))
    }

    /// `identity_envelope`: the seed's signed, time-windowed self statement.
    #[track_caller]
    pub fn identity_envelope(&self) -> Result<Value, SeedError> {
        let inner = self.lock();
        self.signed_statement(&inner, SEED_IDENTITY_PROTOCOL, None)
    }

    /// `receipt_envelope`: the seed's signed receipt for one accepted member message.
    #[track_caller]
    pub fn receipt_envelope(&self, message_id: &str) -> Result<Value, SeedError> {
        require_segment(message_id)?;
        let inner = self.lock();
        self.signed_statement(
            &inner,
            SEED_RECEIPT_PROTOCOL,
            Some(("accepted_message_id", message_id)),
        )
    }

    fn authority<'a>(&'a self, seed_url: Option<&'a str>) -> seed_members::Coordinator<'a> {
        seed_members::Coordinator {
            frame: SeedFrame {
                swarm_id: &self.options.swarm_id,
                seed_node_id: &self.options.seed_node_id,
                seed_url,
                seed_key_record: &self.key_record,
                limits: self.limits,
                lease_seconds: self.options.lease_seconds,
                message_ttl_seconds: self.options.message_ttl_seconds,
            },
            timing: self.timing,
            seed_endpoint_id: self.signer.endpoint_id(),
            seed_incarnation: &self.options.incarnation,
        }
    }

    /// Commit the table (and the optional invite consumption and signed envelopes): the snapshot row and the
    /// journal rows recorded since the last commit, before the caller answers. When the database refuses, the
    /// in-memory table goes back to the last committed state so memory never leads disk.
    fn persist(
        &self,
        inner: &mut Inner,
        consume: Option<&Consume<'_>>,
        envelopes: &[Envelope<'_>],
    ) -> Result<(), SeedError> {
        let snapshot = inner.table.core_snapshot();
        let journal = inner.table.unsaved_journal();
        match inner
            .store
            .commit(&snapshot, &inner.baseline, &journal, consume, envelopes)
        {
            Ok(_) => {
                inner.baseline = snapshot;
                inner.table.mark_journal_saved();
                Ok(())
            }
            Err(error) => {
                self.forget_uncommitted(inner)?;
                Err(error)
            }
        }
    }

    /// Memory never leads disk: drop what the failed operation changed, back to the last view this process
    /// committed or loaded (a stale process keeps its stale view, as v0.1.4 keeps its `_members`).
    fn forget_uncommitted(&self, inner: &mut Inner) -> Result<(), SeedError> {
        Ok(inner.table.forget_unsaved(&inner.baseline)?)
    }

    /// The members the database holds now, for `_ensure_current_member`.
    fn durable_view(inner: &Inner) -> Result<Vec<DurableMember>, SeedError> {
        let snapshot = inner.store.load_core()?;
        let members = snapshot
            .as_ref()
            .and_then(|s| s["members"].as_array().cloned())
            .unwrap_or_default();
        let text = |m: &Value, k: &str| m[k].as_str().unwrap_or_default().to_owned();
        Ok(members
            .iter()
            .map(|m| DurableMember {
                node_id: text(m, "node_id"),
                endpoint_id: text(m, "endpoint_id"),
                verification_key_digest: text(m, "verification_key_digest"),
                incarnation: text(m, "incarnation"),
                generation: m["generation"].as_u64().unwrap_or_default(),
            })
            .collect())
    }

    fn acceptance_message(&self, admission: &seed_coordinator::Admission, id: &str) -> Value {
        let mut message = admission.acceptance.clone();
        if let Some(map) = message.as_object_mut() {
            map.insert("message_id".into(), json!(id));
            map.insert("swarm_id".into(), json!(self.options.swarm_id));
            map.insert("sender_node_id".into(), json!(self.options.seed_node_id));
            map.insert(
                "sender_endpoint_id".into(),
                json!(self.signer.endpoint_id()),
            );
            map.insert("incarnation".into(), json!(self.options.incarnation));
        }
        message
    }

    /// `accept_join` with a string token.
    #[track_caller]
    pub fn accept_join(
        &self,
        invite_token: &str,
        join_envelope: &Value,
    ) -> Result<Value, SeedError> {
        self.accept_join_value(&Value::String(invite_token.to_owned()), join_envelope)
    }

    /// `accept_join` for the HTTP body's untyped `invite_token` / `join_envelope` (a non-string token skips
    /// the committed-retry lookup, as `isinstance(invite_token, str)` does, and is `invite_malformed`).
    #[track_caller]
    pub fn accept_join_value(
        &self,
        invite_token: &Value,
        join_envelope: &Value,
    ) -> Result<Value, SeedError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        let (token, typed) = match invite_token.as_str() {
            Some(t) => (t, true),
            None => ("", false),
        };
        if typed
            && join_envelope.is_object()
            && !is_v2_form(join_envelope)
            && inner.table.replays_join(token, join_envelope)?
        {
            let nonce = join_envelope["message"]["invite_nonce"]
                .as_str()
                .unwrap_or_default();
            return inner
                .store
                .load_envelope("join", nonce)?
                .ok_or_else(|| SeedError::new("seed_state_corrupt"));
        }
        let now = self.now()?;
        let admission = {
            let authority = self.authority(inner.seed_url.as_deref());
            seed_coordinator::evaluate_join(
                &authority.frame,
                &inner.table.rows(),
                token,
                join_envelope,
                now,
            )?
        };
        let verified = invite::verify_invite(token, std::slice::from_ref(&self.key_record), now)?;
        let nonce = verified["nonce"].as_str().unwrap_or_default().to_owned();
        let id = inner.table.draw_message_id()?;
        let message = self.acceptance_message(&admission, &id);
        let signed = membership::sign(&self.signer, &message, self.limits)?;
        inner
            .table
            .commit_join(id, token, join_envelope, &admission, now, &self.timing);
        let expires_at = verified["expires_at"].as_f64().unwrap_or_default();
        self.persist(
            inner,
            Some(&Consume {
                nonce: &nonce,
                consumed_at: now,
                expires_at,
            }),
            &[Envelope {
                kind: "join",
                key: &nonce,
                value: &signed,
            }],
        )?;
        Ok(signed)
    }

    /// `SqliteInviteRegistry.consume`: atomically consume one live nonce (the other pairing paths use this
    /// registry too); a nonce already consumed is `invite_replayed`.
    #[track_caller]
    pub fn consume_invite(&self, nonce: &str, now: f64, expires_at: f64) -> Result<(), SeedError> {
        if nonce.is_empty() {
            return Err(SeedError::new("invite_field_invalid"));
        }
        if !now.is_finite() || !expires_at.is_finite() {
            return Err(SeedError::new("invite_time_invalid"));
        }
        if now > expires_at {
            return Err(SeedError::new("invite_expired"));
        }
        self.lock().store.consume(nonce, now, expires_at)
    }

    /// `accept_resume`: fence one durable principal's next incarnation, no invite consumed.
    #[track_caller]
    pub fn accept_resume(&self, resume_envelope: &Value) -> Result<Value, SeedError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        let request_id = resume_envelope["message"]["message_id"]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        if resume_envelope.is_object()
            && !is_v2_form(resume_envelope)
            && inner.table.replays_resume(resume_envelope)?
        {
            return inner
                .store
                .load_envelope("resume", &request_id)?
                .ok_or_else(|| SeedError::new("seed_state_corrupt"));
        }
        let now = self.now()?;
        let admission = {
            let authority = self.authority(inner.seed_url.as_deref());
            seed_coordinator::evaluate_resume(
                &authority.frame,
                &inner.table.rows(),
                resume_envelope,
                now,
            )?
        };
        let id = inner.table.draw_message_id()?;
        let message = self.acceptance_message(&admission, &id);
        let signed = membership::sign(&self.signer, &message, self.limits)?;
        inner
            .table
            .commit_resume(id, resume_envelope, &admission, now, &self.timing);
        self.persist(
            inner,
            None,
            &[Envelope {
                kind: "resume",
                key: &request_id,
                value: &signed,
            }],
        )?;
        Ok(signed)
    }

    /// `receive_member_message`: verify and record one member message; a heartbeat commits its signed
    /// lease renewal (read back with [`Coordinator::lease_renewal`]).
    #[track_caller]
    pub fn receive_member_message(
        &self,
        envelope: &Value,
        expected_protocol: &str,
    ) -> Result<Value, SeedError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        let now = self.now()?;
        inner
            .table
            .set_durable_view(Some(Self::durable_view(inner)?));
        let received = {
            let authority = self.authority(inner.seed_url.as_deref());
            seed_members::receive_member_message(
                &mut inner.table,
                &authority,
                envelope,
                expected_protocol,
                now,
            )
        };
        inner.table.set_durable_view(None);
        let message = match received {
            Ok(message) => message,
            Err(refusal) => {
                // RU-035: a rotation acknowledgement row is committed before a full replay window refuses.
                if inner.table.take_row_kept_by_refusal() {
                    self.persist(inner, None, &[])?;
                }
                return Err(refusal.into());
            }
        };
        let mut signed_renewal: Option<(String, Value)> = None;
        if expected_protocol == seed_members::HEARTBEAT_PROTOCOL {
            let node = message["sender_node_id"].as_str().unwrap_or_default();
            let heartbeat_id = message["message_id"].as_str().unwrap_or_default();
            let generation = message["generation"].as_u64().unwrap_or_default();
            let key = renewal_key(node, generation, heartbeat_id);
            if let Some(stored) = inner.store.load_envelope("renewal", &key)? {
                // A retry: `load_heartbeat_renewal` decodes and verifies the stored answer before anything else.
                self.verify_stored_renewal(inner, &stored, node, generation, heartbeat_id)?;
            } else {
                let unsigned = inner.table.renewal(node, heartbeat_id)?;
                match membership::sign(&self.signer, &unsigned, self.limits) {
                    Ok(signed) => signed_renewal = Some((key, signed)),
                    Err(e) => {
                        self.forget_uncommitted(inner)?;
                        return Err(e.into());
                    }
                }
            }
        }
        let envelopes: Vec<Envelope<'_>> = signed_renewal
            .iter()
            .map(|(key, value)| Envelope {
                kind: "renewal",
                key,
                value,
            })
            .collect();
        self.persist(inner, None, &envelopes)?;
        Ok(message)
    }

    /// `lease_renewal`: the signed renewal the seed committed for one heartbeat.
    #[track_caller]
    pub fn lease_renewal(
        &self,
        node_id: &str,
        heartbeat_message_id: &str,
    ) -> Result<Value, SeedError> {
        let mut inner = self.lock();
        let view = Self::durable_view(&inner)?;
        inner.table.set_durable_view(Some(view));
        let current = inner.table.ensure_current_member(node_id);
        inner.table.set_durable_view(None);
        current?;
        inner.table.renewal(node_id, heartbeat_message_id)?;
        let generation = inner
            .table
            .rows()
            .iter()
            .find(|m| m.node_id == node_id)
            .map_or(0, |m| m.generation);
        let stored = inner
            .store
            .load_envelope(
                "renewal",
                &renewal_key(node_id, generation, heartbeat_message_id),
            )?
            .ok_or_else(|| SeedError::new("seed_state_corrupt"))?;
        self.verify_stored_renewal(&inner, &stored, node_id, generation, heartbeat_message_id)?;
        Ok(stored)
    }

    /// `_decode_heartbeat_renewal_row` -> `_verify_renewal_envelope`: a stored renewal is returned only when it
    /// still verifies against the key the database is bound to NOW (after a completed key rotation a renewal
    /// signed by the previous key is `seed_state_corrupt`, as in v0.1.4) and names this member and heartbeat.
    fn verify_stored_renewal(
        &self,
        inner: &Inner,
        stored: &Value,
        node_id: &str,
        generation: u64,
        heartbeat_message_id: &str,
    ) -> Result<(), SeedError> {
        let corrupt = || SeedError::new("seed_state_corrupt");
        let bound = inner.store.seed_key_digest()?;
        let issued_at = stored["message"]["issued_at"]
            .as_f64()
            .filter(|t| t.is_finite())
            .ok_or_else(corrupt)?;
        let message = membership::verify(
            stored,
            membership::Expected {
                protocol: seed_members::LEASE_RENEWAL_PROTOCOL,
                key_digest: &bound,
                swarm_id: Some(&self.options.swarm_id),
                sender_node_id: Some(&self.options.seed_node_id),
                sender_endpoint_id: None,
                recipient_node_id: Some(node_id),
            },
            issued_at,
            self.limits,
        )
        .map_err(|_| corrupt())?;
        let member_incarnation = inner
            .table
            .rows()
            .iter()
            .find(|m| m.node_id == node_id)
            .map(|m| m.incarnation.clone());
        if message["generation"].as_u64() != Some(generation)
            || message["membership_generation"].as_u64() != Some(generation)
            || message["heartbeat_message_id"] != heartbeat_message_id
            || member_incarnation
                .as_deref()
                .is_none_or(|i| message["member_incarnation"] != i)
        {
            return Err(corrupt());
        }
        Ok(())
    }

    /// `member`: one member's projection with its liveness at the coordinator clock.
    #[track_caller]
    pub fn member(&self, node_id: &str) -> Result<Value, SeedError> {
        require_segment(node_id)?;
        let inner = self.lock();
        let now = self.now()?;
        inner
            .table
            .projection(node_id, now, &self.timing)
            .ok_or_else(|| SeedError::new("seed_member_unknown"))
    }

    /// Execute bounded verification and commit while member authority is stable.
    ///
    /// Lock order: coordinator mutex, then SQLite `BEGIN IMMEDIATE`. Like v0.1.4's
    /// `member_authority_guard`, the local member MUST match the durable identity,
    /// generation, class, lease and lifecycle. Missing local member is
    /// `seed_member_unknown`; a missing/different durable member is
    /// `seed_state_member_stale`. The trusted clock is sampled AFTER both locks.
    /// The callback checks request binding, eligible states and lease against this
    /// member and time; a request-supplied member is never an authority input.
    ///
    /// Callback MUST be bounded, nonblocking and non-reentrant: do not call other
    /// coordinator methods, network, external observers, or acquire an outer lock.
    /// Prepare slow work before entry; defer external observers until return. The
    /// signer()/swarm_id()/seed_node_id() accessors are lock-free and may be borrowed.
    ///
    /// Serializes local threads and SQLite writers to THIS database, including an
    /// operator in another process. Callback side effects are NOT atomically durable
    /// with this read-only transaction, nor rolled back on error/panic/commit failure.
    /// This is not a cross-database journal or a global crash-atomic capability.
    #[track_caller]
    pub fn with_current_member<R>(
        &self,
        node_id: &str,
        callback: impl FnOnce(&Value, f64) -> Result<R, SeedError>,
    ) -> Result<R, SeedError> {
        require_segment(node_id)?;
        let mut inner = self.lock();
        let local = inner
            .table
            .rows()
            .into_iter()
            .find(|m| m.node_id == node_id)
            .ok_or_else(|| SeedError::new("seed_member_unknown"))?;
        inner.store.with_authority(|snapshot| {
            let table = table_from(&self.ids, snapshot.as_ref())?;
            let persisted = table
                .rows()
                .into_iter()
                .find(|m| m.node_id == node_id)
                .ok_or_else(|| SeedError::new("seed_state_member_stale"))?;
            if persisted.node_id != local.node_id
                || persisted.endpoint_id != local.endpoint_id
                || persisted.verification_key_digest != local.verification_key_digest
                || persisted.incarnation != local.incarnation
                || persisted.generation != local.generation
                || persisted.peer_class != local.peer_class
                || persisted.lease_expires_at != local.lease_expires_at
                || persisted.lifecycle_state != local.lifecycle_state
            {
                return Err(SeedError::new("seed_state_member_stale"));
            }
            let now = self.now()?;
            let member = table
                .projection(node_id, now, &self.timing)
                .ok_or_else(|| SeedError::new("seed_state_member_stale"))?;
            callback(&member, now)
        })
    }

    /// `members`: every projection ordered by node id, optionally one peer class.
    #[track_caller]
    pub fn members(&self, peer_class: Option<&str>) -> Result<Vec<Value>, SeedError> {
        if let Some(class) = peer_class {
            require_segment(class)?;
        }
        let inner = self.lock();
        let now = self.now()?;
        let mut rows = inner.table.rows();
        rows.sort_by(|a, b| a.node_id.cmp(&b.node_id));
        Ok(rows
            .iter()
            .filter(|m| peer_class.is_none_or(|c| m.peer_class == c))
            .filter_map(|m| inner.table.projection(&m.node_id, now, &self.timing))
            .collect())
    }

    /// `advance_member_generation`: durably fence a member into STOPPING / STOPPED under its next
    /// generation (revocation).
    #[track_caller]
    pub fn advance_member_generation(
        &self,
        node_id: &str,
        expected_generation: u64,
        lifecycle_state: &str,
    ) -> Result<Value, SeedError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        let now = self.now()?;
        inner
            .table
            .set_durable_view(Some(Self::durable_view(inner)?));
        let advanced = seed_members::advance_member_generation(
            &mut inner.table,
            node_id,
            expected_generation,
            lifecycle_state,
            now,
            &self.timing,
        );
        inner.table.set_durable_view(None);
        let projection = advanced.map_err(|e| {
            if e.0 == "seed_argument_invalid" {
                SeedError::accident(e.0)
            } else {
                SeedError::from(e)
            }
        })?;
        self.persist(inner, None, &[])?;
        Ok(projection)
    }
}

impl Coordinator {
    /// `SqliteSeedState.authority_state`: the current authority generation and the latest rotation.
    #[track_caller]
    pub fn authority_state(&self) -> Result<Value, SeedError> {
        Ok(self.lock().table.authority_state())
    }

    /// `SqliteSeedState.begin_authority_rotation`: persist one monotonic pending authority transition (the
    /// operator's `begin_seed_key_rotation` builds and signs the transition; this is its state write).
    #[track_caller]
    pub fn begin_authority_rotation(&self, transition: &Value) -> Result<(), SeedError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        let bound = inner.store.seed_key_digest()?;
        inner.table.begin_authority_rotation(transition, &bound)?;
        self.persist(inner, None, &[])
    }

    /// `SqliteSeedState.complete_authority_rotation`: atomically promote one pending transition in durable
    /// metadata. The running coordinator keeps signing with its current key until it is restarted under the new
    /// one (a restart under the old key is then refused `seed_state_identity_mismatch`).
    #[track_caller]
    pub fn complete_authority_rotation(
        &self,
        authority_generation: u64,
        new_seed_key_digest: &str,
    ) -> Result<(), SeedError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        let bound = inner.store.seed_key_digest()?;
        let completion = inner.table.complete_authority_rotation(
            authority_generation,
            new_seed_key_digest,
            &bound,
        )?;
        if completion == seed_members::Completion::AlreadyCompleted {
            return Ok(());
        }
        let snapshot = inner.table.core_snapshot();
        let journal = inner.table.unsaved_journal();
        match inner.store.commit_rotation_completion(
            &snapshot,
            &journal,
            new_seed_key_digest,
            authority_generation,
        ) {
            Ok(()) => {
                inner.baseline = snapshot;
                inner.table.mark_journal_saved();
                Ok(())
            }
            Err(error) => {
                self.forget_uncommitted(inner)?;
                Err(error)
            }
        }
    }

    /// `SqliteSeedState.seed_rotation_acknowledgements`: the durable acknowledgement rows of one authority
    /// generation, ordered by node id.
    #[track_caller]
    pub fn rotation_acknowledgements(&self, authority_generation: u64) -> Vec<Value> {
        self.lock()
            .table
            .rotation_acknowledgements(authority_generation)
    }

    /// `SqliteSeedState.load_members`: the committed member rows (operator use).
    #[track_caller]
    pub fn member_rows(&self) -> Vec<seed_coordinator::Member> {
        self.lock().table.rows()
    }

    /// The envelope `GET /seed/rotation` serves (`SeedHTTPServer(rotation_envelope=...)`; `None` answers
    /// `404 seed_rotation_absent`). The caller verifies it (`protocol::seed_transition::verify`) before setting it.
    #[track_caller]
    pub fn set_rotation_envelope(&self, envelope: Option<Value>) {
        self.lock().rotation_envelope = envelope;
    }

    #[track_caller]
    pub fn rotation_envelope(&self) -> Option<Value> {
        self.lock().rotation_envelope.clone()
    }
}

/// The canonical bytes of a document (`canonical_json_bytes`).
pub fn canonical_bytes(value: &Value) -> Result<Vec<u8>, SeedError> {
    canonical::to_bytes(value).map_err(|_| SeedError::new("seed_http_internal_error"))
}
