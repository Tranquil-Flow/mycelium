// SPDX-License-Identifier: AGPL-3.0-only
//! Signed, single-admission request service for the native composition root.
//!
//! The host supplies an authenticated/prepared submission and an L5 AdmittedGrant,
//! not a display snapshot. Tokenization, qualification capture, transport, native
//! dispatch and cleanup observation remain host effects. This adapter consumes real
//! L9 terminal ownership; neither native success nor HTTP cancellation is cleanup.
//!
//! Scope: one request. Replay history, single-consumer attachment, acknowledgement and the
//! publisher-generation rebinding are the request registry's (`registry::Registry`,
//! service.py:508-620,1208-1453); this adapter never decides settlement. L9's grant is
//! immutable, so a rebinding to a new generation succeeds only through the host's authenticated
//! `GenerationSync` handoff (the signed backend's `update_publisher_generation`); without one the
//! first stream (the grant's own generation) works and any later stream refuses
//! `publisher_generation_sync_failed` and cancels the request, as the oracle does.
use crate::{
    http::Request,
    registry::{self, LastEventId, Pull as RegistryPull, Registry},
    requests::{Accepted, Service, Subscription},
    schema::PublicError,
};
use budgets::{BudgetId, Entries, MonoInstant};
use protocol::{ContractError, token::AdmittedGrant};
use router::cleanup::{CleanupProof, Terminal};
use runtime::composition::{Pull, Session};
use serde_json::{Value, json};
use std::{
    collections::{BTreeSet, VecDeque},
    sync::{Arc, Condvar, Mutex, MutexGuard},
    time::Duration,
};

pub trait Authority: Send + Sync {
    /// Current monotonic time and independently authenticated lifecycle authority.
    fn observe(&self) -> Result<(f64, bool), ContractError>;
}
/// The host's authenticated publisher-generation handoff (service.py:549-575 calling the signed
/// backend's `update_publisher_generation`): `Ok(true)` only when the signed command identity now
/// carries `new`. Anything else detaches the stream and cancels the request.
pub trait GenerationSync: Send + Sync {
    fn advance(&self, request_id: &str, expected: u64, new: u64) -> Result<bool, ContractError>;
}
/// Trusted composition input, never deserialized from an HTTP request. The host
/// binds tokenizer/model/prompt identity to the grant's request/model digest.
/// This layer also checks all shared qualification/control fields before use.
pub struct Admission {
    pub submission: Value,
    pub grant: AdmittedGrant,
    pub entry_key: Value,
    pub cleanup_subject: Value,
    pub bearer_token: String,
    pub owner_token: String,
}
struct State {
    session: Session,
    registry: Registry,
    owner_token: Option<String>,
    admitted: bool,
    cancelled: bool,
    next_token: u64,
    pending: VecDeque<(u32, String)>,
    output_failure: Option<&'static str>,
    phases: BTreeSet<&'static str>,
    terminal_published: bool,
}
struct Shared {
    state: Mutex<State>,
    changed: Condvar,
    authority: Arc<dyn Authority>,
    request_id: String,
    generation: u64,
    protocol: &'static str,
    deadline: f64,
    capacity: usize,
    poll: Duration,
    owner_digest: String,
}
/// The grant is immutable: only its own generation is authenticated, once.
struct ImmutableGrant(u64);
impl GenerationSync for ImmutableGrant {
    fn advance(&self, _: &str, expected: u64, new: u64) -> Result<bool, ContractError> {
        Ok(new == self.0 && expected.checked_add(1) == Some(new))
    }
}
#[derive(Clone)]
pub struct SignedService {
    shared: Arc<Shared>,
    sync: Arc<dyn GenerationSync>,
    qualification: Arc<Value>,
    submission_digest: Arc<String>,
    bearer_digest: Arc<String>,
}
fn error(code: &'static str) -> PublicError {
    PublicError::new(409, code)
}
fn contract(e: ContractError) -> PublicError {
    error(e.0)
}
fn equal(a: &str, b: &str) -> bool {
    a.len() == b.len() && a.bytes().zip(b.bytes()).fold(0u8, |n, (a, b)| n | (a ^ b)) == 0
}
impl Shared {
    fn lock(&self) -> Result<MutexGuard<'_, State>, PublicError> {
        self.state
            .lock()
            .map_err(|_| PublicError::new(503, "gateway_unavailable"))
    }
    fn current(&self) -> Result<(f64, bool), PublicError> {
        let (now, current) = self.authority.observe().map_err(contract)?;
        if !now.is_finite() || !current || now >= self.deadline {
            return Err(error("token_path_grant_stale"));
        }
        Ok((now, current))
    }
    fn owner(&self, state: &State, id: &str, token: Option<&str>) -> Result<(), PublicError> {
        if !state.admitted || id != self.request_id {
            return Err(error("unknown_request"));
        }
        let token = token.ok_or(error("session_owner_mismatch"))?;
        if !equal(
            &protocol::canonical::sha256_prefixed(token.as_bytes()),
            &self.owner_digest,
        ) {
            return Err(error("session_owner_mismatch"));
        }
        Ok(())
    }
    fn clock(&self) -> f64 {
        self.authority.observe().map_or(0.0, |(now, _)| now)
    }
    /// The wire form of one registry event (service.py `StreamEvent`): the registry owns
    /// sequence, history and the delivery generation; this adds the fixed protocol envelope.
    fn wire(&self, kind: &str, sequence: i64, generation: u64, payload: &Value) -> Value {
        let mut event = json!({"protocol":self.protocol,"request_id":self.request_id,"publisher_generation":generation,"sequence":sequence,"type":kind});
        if let Some(fields) = payload.as_object() {
            for (key, value) in fields {
                event[key.as_str()] = value.clone();
            }
        }
        event
    }
    fn registry(e: registry::AdmissionError) -> PublicError {
        error(e.0)
    }
    fn buffered(&self, s: &State) -> Result<usize, PublicError> {
        s.registry
            .buffered_event_count(&self.request_id)
            .map_err(Self::registry)
    }
    /// service.py:976,1192: preserve one replay event when capacity permits.
    fn trim(&self, s: &mut State) -> Result<(), PublicError> {
        s.registry
            .trim_acknowledged(&self.request_id, self.capacity.saturating_sub(2))
            .map_err(Self::registry)
    }
    fn append(
        &self,
        s: &mut State,
        kind: &str,
        fields: &[(&str, Value)],
    ) -> Result<(), PublicError> {
        let mut payload = serde_json::Map::new();
        for (key, value) in fields {
            payload.insert((*key).to_string(), value.clone());
        }
        let payload = Value::Object(payload);
        // Validated before it can reach history; sequence/generation are assigned by the
        // registry and the subscription, so the closed schema is checked with placeholders.
        protocol::request::event(&self.wire(kind, 0, self.generation, &payload))
            .map_err(contract)?;
        let now = self.clock();
        if kind == "accepted" {
            return Ok(());
        }
        let appended = if matches!(kind, "completed" | "failed" | "cancelled") {
            s.registry
                .append_terminal(&self.request_id, kind, payload, now)
                .map(|()| 0)
        } else {
            s.registry.append(&self.request_id, kind, payload, now)
        };
        appended.map(|_| ()).map_err(Self::registry)
    }
    fn lifecycle(&self, s: &mut State, phase: &'static str) -> Result<(), PublicError> {
        if self.protocol != "mycelium.request_event.v2"
            || s.phases.contains(phase)
            || s.terminal_published
        {
            return Ok(());
        }
        self.trim(s)?;
        if self.buffered(s)? >= self.capacity - 1 {
            return Err(error("event_backpressure"));
        }
        self.append(s, "lifecycle", &[("phase", json!(phase))])?;
        s.phases.insert(phase);
        Ok(())
    }
    fn pump(&self, s: &mut State) -> Result<(), PublicError> {
        loop {
            self.trim(s)?;
            if self.buffered(s)? >= self.capacity {
                break;
            }
            if !s.pending.is_empty() {
                if self.buffered(s)? >= self.capacity - 1 {
                    break;
                }
                if s.next_token == 0 {
                    self.lifecycle(s, "first_token")?;
                    self.lifecycle(s, "decode")?;
                }
                if self.buffered(s)? >= self.capacity - 1 {
                    break;
                }
            }
            if let Some((id, text)) = s.pending.pop_front() {
                let mut fields = vec![("token_index", json!(s.next_token)), ("text", json!(text))];
                if self.protocol == "mycelium.request_event.v2" {
                    fields.push(("token_id", json!(id)));
                }
                self.append(s, "token", &fields)?;
                s.next_token = s
                    .next_token
                    .checked_add(1)
                    .ok_or(error("invalid_stream_event"))?;
            } else if !s.terminal_published {
                if let Some(terminal) = s.session.terminal() {
                    self.lifecycle(s, "completion")?;
                    let (kind, fields) = match terminal {
                        Terminal::Completed => ("completed", vec![]),
                        Terminal::Cancelled => ("cancelled", vec![]),
                        // Only the closed code is public, never native error text.
                        Terminal::Failed => ("failed", vec![("code", json!("backend_failed"))]),
                    };
                    self.append(s, kind, &fields)?;
                    s.terminal_published = true;
                }
                break;
            } else {
                break;
            }
        }
        self.changed.notify_all();
        Ok(())
    }
}
impl SignedService {
    pub fn new(input: Admission, authority: Arc<dyn Authority>) -> Result<Self, PublicError> {
        protocol::request::submission(&input.submission).map_err(contract)?;
        if !crate::requests::valid_owner_token(&input.owner_token) {
            return Err(error("invalid_session_token"));
        }
        if input.bearer_token.is_empty() || input.bearer_token.contains(['\r', '\n']) {
            return Err(error("invalid_bearer_token"));
        }
        let scope = &input.grant.statement()["scope"];
        let control = &scope["control"];
        let q = &input.submission["qualification"];
        for (source, target) in [
            ("qualification_digest", "qualification_digest"),
            ("deployment_id", "deployment_id"),
            ("deployment_epoch", "deployment_epoch"),
            ("topology_generation", "topology_version"),
            ("path_digest", "path_manifest_digest"),
        ] {
            if control[source] != q[target] {
                return Err(error("qualification_mismatch"));
            }
        }
        if input.submission["max_new_tokens"] != input.grant.statement()["max_new_tokens"] {
            return Err(error("qualification_mismatch"));
        }
        let request_id = scope["request_id"]
            .as_str()
            .ok_or(error("invalid_request_id"))?
            .to_owned();
        let generation = control["publisher_generation"]
            .as_u64()
            .ok_or(error("invalid_stream_event"))?;
        let deadline = input.grant.deadline_monotonic_s();
        let session = Session::new(input.grant, input.entry_key, input.cleanup_subject, Some(1))
            .map_err(contract)?;
        let capacity = usize::try_from(
            BudgetId::PyFinalMyceliumRequestGatewayServiceMaxBufferedEventsL307C35
                .count()
                .map_err(|_| error("budget_unavailable"))?
                .get(),
        )
        .map_err(|_| error("budget_unavailable"))?;
        let poll = Duration::from_millis(BudgetId::PyMyceliumRequestGatewayAsgiRequestgatewayasgiapplicationStreamSubscriptionTimeoutL337.span(Entries::ZERO).map_err(|_| error("budget_unavailable"))?.millis());
        let protocol = if input.submission["protocol"] == "mycelium.request_gateway.v2" {
            "mycelium.request_event.v2"
        } else {
            "mycelium.request_event.v1"
        };
        // Retain binding digests, not the prompt or bearer. The one-use owner
        // token moves into the acceptance response and is then forgotten here.
        let owner_digest = protocol::canonical::sha256_prefixed(input.owner_token.as_bytes());
        let submission_digest = protocol::canonical::digest(&input.submission)
            .map_err(|_| error("invalid_submission"))?;
        Ok(Self {
            shared: Arc::new(Shared {
                state: Mutex::new(State {
                    session,
                    registry: Registry::new(
                        registry::Config::new(capacity, 1, 0.0)
                            .map_err(|_| error("budget_unavailable"))?,
                    ),
                    owner_token: Some(input.owner_token),
                    admitted: false,
                    cancelled: false,
                    next_token: 0,
                    pending: VecDeque::new(),
                    output_failure: None,
                    phases: BTreeSet::new(),
                    terminal_published: false,
                }),
                changed: Condvar::new(),
                authority,
                request_id,
                generation,
                protocol,
                deadline,
                capacity,
                poll,
                owner_digest,
            }),
            sync: Arc::new(ImmutableGrant(generation)),
            qualification: Arc::new(input.submission["qualification"].clone()),
            submission_digest: Arc::new(submission_digest),
            bearer_digest: Arc::new(protocol::canonical::sha256_prefixed(
                input.bearer_token.as_bytes(),
            )),
        })
    }
    /// Install the host's authenticated generation handoff. Call before sharing the service.
    /// The handoff runs while this request's state is locked: it must not call back into the
    /// service.
    pub fn with_generation_sync(mut self, sync: Arc<dyn GenerationSync>) -> Self {
        self.sync = sync;
        self
    }
    /// Reserve dispatch under the execution lock, then run slow host work without
    /// that lock. A concurrent cancellation is sticky; host dispatch must consult
    /// its native request latch as well. A failed dispatch is never replayed.
    pub fn start(
        &self,
        dispatch: impl FnOnce() -> Result<(), ContractError>,
    ) -> Result<(), PublicError> {
        {
            let mut s = self.shared.lock()?;
            if !s.admitted {
                return Err(error("unknown_request"));
            }
            if s.cancelled {
                return Err(error("request_cancelled"));
            }
            self.shared.current()?;
            s.session.start(|| Ok(())).map_err(contract)?;
            for phase in ["admission", "queue", "prefill"] {
                self.shared.lifecycle(&mut s, phase)?;
            }
            self.shared.changed.notify_all();
        }
        if let Err(e) = dispatch() {
            self.shared
                .lock()?
                .session
                .native_result(Err(e.clone()))
                .map_err(contract)?;
            return Err(contract(e));
        }
        Ok(())
    }
    pub fn native_result(&self, result: Result<(), ContractError>) -> Result<(), PublicError> {
        self.shared
            .lock()?
            .session
            .native_result(result)
            .map_err(contract)
    }
    /// Transport receives outside the lock. The real L9 cursor verifies signed
    /// scope/order/freshness and seals before any token can be decoded or emitted.
    pub fn accept_receipt(
        &self,
        envelope: Value,
        seal: impl FnOnce(&[u8]) -> Result<(), ContractError>,
        mut decode: impl FnMut(u32) -> Result<String, ContractError>,
    ) -> Result<(), PublicError> {
        let mut s = self.shared.lock()?;
        if !s.admitted {
            return Err(error("unknown_request"));
        }
        if s.cancelled {
            return Err(error("request_cancelled"));
        }
        if !s.pending.is_empty() {
            return Err(error("token_progress_capacity"));
        }
        if let Some(code) = s.output_failure {
            return Err(error(code));
        }
        let observation = self.shared.current()?;
        // A receipt consumed by L9 cannot be replayed if decoding/publication
        // fails. Keep that failure sticky through subsequent cleanup settlement.
        s.output_failure = Some("backend_failed");
        let mut failure = None;
        let pull = s.session.pull(
            || Ok(envelope),
            || match self.shared.authority.observe() {
                Ok(v) => v,
                Err(e) => {
                    failure = Some(e);
                    (observation.0, false)
                }
            },
            seal,
        );
        if let Some(e) = failure {
            return Err(contract(e));
        }
        if pull.map_err(contract)? == Pull::Backpressure {
            return Err(error("token_progress_capacity"));
        }
        let observation = self.shared.current()?;
        let receipt = s
            .session
            .poll(|| observation)
            .map_err(contract)?
            .ok_or(error("token_progress_terminal_missing"))?;
        let mut pending = VecDeque::new();
        for id in receipt.token_ids() {
            let text = decode(*id).map_err(|_| {
                s.output_failure = Some("invalid_backend_token");
                error("invalid_backend_token")
            })?;
            let limit = BudgetId::PyShiftMyceliumRequestGatewayServiceMaxBackendTokenTextBytesL37C0
                .bytes()
                .map_err(|_| error("budget_unavailable"))?
                .get();
            if u64::try_from(text.len()).map_err(|_| error("invalid_backend_token"))? > limit {
                s.output_failure = Some("invalid_backend_token");
                return Err(error("invalid_backend_token"));
            }
            pending.push_back((*id, text));
        }
        self.shared.current()?;
        s.pending = pending;
        s.output_failure = None;
        self.shared.pump(&mut s)
    }
    pub fn settle(
        &self,
        proof: CleanupProof,
        outcome: Terminal,
        observe: impl FnOnce() -> (MonoInstant, bool),
    ) -> Result<(), PublicError> {
        let mut s = self.shared.lock()?;
        if outcome == Terminal::Completed
            && !s.cancelled
            && let Some(code) = s.output_failure
        {
            return Err(error(code));
        }
        // service.py:811-822: accepted cancellation wins over a racing native
        // completed result; an already settled terminal remains immutable.
        let outcome = if s.cancelled && outcome == Terminal::Completed {
            Terminal::Cancelled
        } else {
            outcome
        };
        s.session
            .settle(proof, outcome, observe)
            .map_err(contract)?;
        self.shared.pump(&mut s)
    }
    pub fn terminal(&self) -> Result<Option<Terminal>, PublicError> {
        Ok(self.shared.lock()?.session.terminal())
    }
}
impl Service for SignedService {
    fn authorized(&self, r: &Request) -> bool {
        let values: Vec<_> = r.values("authorization").collect();
        values.len() == 1
            && values[0].strip_prefix("Bearer ").is_some_and(|v| {
                equal(
                    &protocol::canonical::sha256_prefixed(v.as_bytes()),
                    &self.bearer_digest,
                )
            })
    }
    fn health(&self) -> Result<Value, PublicError> {
        Ok(json!({"service":"mycelium-request-gateway","status":"ok"}))
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        Err(error("qualification_unavailable"))
    }
    fn recovery(&self) -> Result<Value, PublicError> {
        Err(error("recovery_projection_unavailable"))
    }
    fn submit(&self, document: Value) -> Result<Accepted, PublicError> {
        protocol::request::submission(&document).map_err(contract)?;
        // qualification.py:70-91 @ f4e366e9: ordered public refusal codes.
        let actual = &self.qualification;
        let requested = &document["qualification"];
        for (fields, code) in [
            (&["deployment_id"][..], "qualification_mismatch"),
            (&["deployment_epoch"][..], "deployment_epoch_changed"),
            (
                &["topology_version", "path_manifest_digest"][..],
                "path_changed",
            ),
            (&["qualification_id"][..], "stale_qualification"),
        ] {
            if fields.iter().any(|f| actual[*f] != requested[*f]) {
                return Err(error(code));
            }
        }
        if protocol::canonical::digest(&document).map_err(|_| error("invalid_submission"))?
            != *self.submission_digest
        {
            return Err(error("qualification_mismatch"));
        }
        let (now, _) = self.shared.current()?;
        let mut s = self.shared.lock()?;
        if s.admitted {
            return Err(error("gateway_capacity_exhausted"));
        }
        // The `accepted` event (sequence 0) is created by the registry; the closed schema is
        // checked first so a malformed envelope never enters history.
        self.shared.append(&mut s, "accepted", &[])?;
        let token = s.owner_token.take().ok_or(error("invalid_session_token"))?;
        let initial = self.shared.generation.saturating_sub(1);
        if let Err(e) = s.registry.submit_at_generation(
            &self.shared.request_id,
            Some(&token),
            now,
            initial,
            &mut Vec::new(),
        ) {
            s.owner_token = Some(token);
            return Err(error(e.0));
        }
        s.admitted = true;
        Ok(Accepted {
            request_id: self.shared.request_id.clone(),
            session_token: token,
        })
    }
    fn cancel(&self, id: &str, owner: Option<&str>) -> Result<bool, PublicError> {
        let mut s = self.shared.lock()?;
        self.shared.owner(&s, id, owner)?;
        if s.session.terminal().is_some() || s.cancelled {
            return Ok(false);
        }
        s.registry
            .cancel(&self.shared.request_id, owner)
            .map_err(Shared::registry)?;
        s.session.cancel(|| Ok(true)).map_err(contract)?;
        s.cancelled = true;
        self.shared.changed.notify_all();
        Ok(true)
    }
    fn subscribe(
        &self,
        id: &str,
        cursor: Option<(u64, u64)>,
        owner: Option<&str>,
    ) -> Result<Box<dyn Subscription>, PublicError> {
        let mut s = self.shared.lock()?;
        self.shared.owner(&s, id, owner)?;
        let last = match cursor {
            None => None,
            Some((generation, sequence)) => Some(LastEventId::Bound(
                i64::try_from(generation).map_err(|_| error("invalid_last_event_id"))?,
                i64::try_from(sequence).map_err(|_| error("invalid_last_event_id"))?,
            )),
        };
        let now = self.shared.clock();
        let sync = self.sync.clone();
        let mut handoff = |request: &str, expected: u64, new: u64| {
            sync.advance(request, expected, new).map_err(|_| ())
        };
        match s
            .registry
            .subscribe(&self.shared.request_id, last, owner, now, &mut handoff)
        {
            Ok(sub) => Ok(Box::new(Events {
                shared: self.shared.clone(),
                sub,
            })),
            Err(e) => {
                if e.0 == "publisher_generation_sync_failed" && !s.cancelled {
                    // service.py:569-575 `_cancel_session`: the registry latched the
                    // cancellation; the L9 session performs it.
                    if s.session.terminal().is_none() {
                        s.session.cancel(|| Ok(true)).map_err(contract)?;
                        s.cancelled = true;
                    }
                    self.shared.changed.notify_all();
                }
                Err(error(e.0))
            }
        }
    }
    fn validate_event(&self, event: &Value) -> Result<(), PublicError> {
        protocol::request::event(event).map_err(contract)
    }
}
struct Events {
    shared: Arc<Shared>,
    sub: registry::Subscription,
}
impl Subscription for Events {
    fn next(&mut self) -> Result<Option<Value>, PublicError> {
        let mut s = self.shared.lock()?;
        loop {
            match s
                .registry
                .next_event(&mut self.sub)
                .map_err(Shared::registry)?
            {
                RegistryPull::Event(event) => {
                    let generation = event
                        .publisher_generation
                        .unwrap_or(self.sub.publisher_generation);
                    return Ok(Some(self.shared.wire(
                        &event.kind,
                        event.sequence,
                        generation,
                        &event.payload,
                    )));
                }
                RegistryPull::End => return Ok(None),
                RegistryPull::Pending => {}
            }
            self.shared.current()?;
            s = self
                .shared
                .changed
                .wait_timeout(s, self.shared.poll)
                .map_err(|_| error("gateway_unavailable"))?
                .0;
        }
    }
    fn ack(&mut self, sequence: u64) -> Result<(), PublicError> {
        let sequence = i64::try_from(sequence).map_err(|_| error("invalid_stream_ack"))?;
        let mut s = self.shared.lock()?;
        let now = self.shared.clock();
        s.registry
            .ack(&mut self.sub, sequence, now)
            .map_err(Shared::registry)?;
        self.shared.pump(&mut s)
    }
    fn close(&mut self) -> Result<(), PublicError> {
        let mut s = self.shared.lock()?;
        s.registry
            .close_subscription(&mut self.sub)
            .map_err(Shared::registry)?;
        self.shared.changed.notify_all();
        Ok(())
    }
}
