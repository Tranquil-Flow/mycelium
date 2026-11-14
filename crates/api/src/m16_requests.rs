// SPDX-License-Identifier: AGPL-3.0-only
//! M16 admission -> dispatch -> cleanup -> registry terminal, behind `requests::Service`.
//!
//! A host supplies current qualification, tokenizer-bound contexts and owner-token entropy.
//! An executor supplies bounded, nonblocking start/poll/cleanup effects. Calls are serialized;
//! executor/host callbacks must not re-enter this service. `tick` belongs to the host's pump.
//! Cleanup failure retains the executor, dispatch slot and ledger reservations for retry.
//! This is NOT a signed worker/MLX implementation or installed-host registration.
//!
//! History and ALL coordinator admission attempts (including rejections) are bounded by
//! the supplied registry max_sessions. Exhaustion refuses until runtime rotation. This owner does
//! not evict sessions: the existing coordinator retains request identities for its lifetime.
//! Rotate the whole runtime only after all requests have settled. Dropping a live owner is
//! not cleanup proof; the host must cancel, pump and inspect debt before dropping it.
use crate::{
    http::Request,
    registry::{self, Config, LastEventId, Pull, Registry},
    requests::{Accepted, Service, Subscription},
    schema::PublicError,
};
use budgets::{BudgetId, Entries};
use runtime::{
    m16_coordinator::{CoordinatorError, RouteIdentity},
    m16_factory::LiveM16Runtime,
    m16_ledger::{PlacementStatus, Reservation},
    m16_router_contracts::{PyReal, RequestContext},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, Condvar, Mutex, MutexGuard},
    time::{Duration, Instant},
};

pub struct Prepared {
    pub context: RequestContext,
    pub owner_token: String,
    pub excluded_placements: Vec<Value>,
}
pub trait Host: Send + Sync {
    fn authorized(&self, request: &Request) -> bool;
    /// Exact current qualification binding; refuse if readiness/authority is revoked.
    /// This is a trusted host observation, never the client's echoed submission.
    fn qualification(&self) -> Result<Value, PublicError>;
    /// Tokenize/validate against the selected model, generate id and secret owner token.
    fn prepare(&self, submission: &Value) -> Result<Prepared, PublicError>;
    /// The route's startup challenge: validate exact prompt token ids (no tokenizer) for
    /// `max_new_tokens`, generate id and secret owner token. A host that cannot run one refuses.
    fn prepare_tokens(
        &self,
        _prompt: &[u32],
        _max_new_tokens: u64,
        _qos_class: &str,
    ) -> Result<Prepared, PublicError> {
        Err(error("startup_challenge_unsupported"))
    }
}
#[derive(Clone, Debug)]
pub struct Dispatch {
    pub context: RequestContext,
    pub identity: RouteIdentity,
    pub manifest: Value,
    /// The route's product stop tokens end this request. False ONLY for the route's startup
    /// challenge (`_stop_token_ids_override=frozenset()`, route.py:4563-4590 @ f4e366e9): a
    /// request-local override, never a change of the route-wide stop set.
    pub product_stop_tokens: bool,
}
pub enum Execution {
    Pending,
    Token { token_id: u32, text: String },
    Completed,
}
pub trait Executor: Send {
    /// Even a failed start may own partial resources: cleanup will still be required.
    fn start(&mut self, dispatch: &Dispatch) -> Result<(), PublicError>;
    fn poll(&mut self, request_id: &str) -> Result<Execution, PublicError>;
    /// Stop if requested, reap/unload, then return true ONLY after cleanup is proven.
    /// False is still pending. Err retains debt and is retried on a subsequent tick.
    fn cleanup(&mut self, request_id: &str, cancel: bool) -> Result<bool, PublicError>;
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Queued,
    Active,
    Cleanup,
    CleanupProven,
    PublicationPending,
    Terminal,
}
struct Entry {
    qualification: Value,
    phase: Phase,
    outcome: &'static str,
    failure: Option<&'static str>,
    progress_refusal: Option<&'static str>,
    next_token: u64,
    max_tokens: u64,
    /// [`Dispatch::product_stop_tokens`].
    product_stop_tokens: bool,
}
struct State {
    runtime: LiveM16Runtime,
    registry: Registry,
    entries: BTreeMap<String, Entry>,
    executor: Box<dyn Executor>,
    admission_attempts: usize,
}
struct Shared {
    state: Mutex<State>,
    changed: Condvar,
    host: Arc<dyn Host>,
    config: Config,
    poll: Duration,
}
#[derive(Clone)]
pub struct M16Requests {
    shared: Arc<Shared>,
}
fn error(code: &'static str) -> PublicError {
    PublicError::new(409, code)
}
fn registry(e: registry::AdmissionError) -> PublicError {
    error(e.0)
}
fn coordinator(e: CoordinatorError) -> PublicError {
    match e {
        CoordinatorError::Admission(e) => error(e.code),
        CoordinatorError::Value(code) | CoordinatorError::Python(code) => error(code),
        CoordinatorError::Ledger(e) => error(e.0),
        CoordinatorError::Config(e) => error(e.0),
        // These failures are not admission evidence. Never convert them into success.
        CoordinatorError::PathBuilder(e) => error(e.code()),
        CoordinatorError::Scheduler(_) => error("m16_scheduler_failed"),
        CoordinatorError::Digest(_) => error("m16_digest_failed"),
    }
}
impl Shared {
    fn lock(&self) -> Result<MutexGuard<'_, State>, PublicError> {
        self.state.lock().map_err(|_| error("m16_owner_poisoned"))
    }
    fn now(s: &State) -> Result<f64, PublicError> {
        let now = s.runtime.clock.now();
        if now.is_finite() {
            Ok(now)
        } else {
            Err(error("m16_clock_invalid"))
        }
    }
    fn current(&self, expected: &Value) -> Result<(), PublicError> {
        if self.host.qualification()? != *expected {
            return Err(error("stale_qualification"));
        }
        Ok(())
    }
    fn append(
        &self,
        s: &mut State,
        id: &str,
        kind: &str,
        payload: Value,
    ) -> Result<(), PublicError> {
        let now = Self::now(s)?;
        s.registry
            .trim_acknowledged(id, self.config.max_buffered_events.saturating_sub(3))
            .map_err(registry)?;
        s.registry
            .append(id, kind, payload, now)
            .map_err(registry)?;
        Ok(())
    }
    fn begin_cleanup(
        &self,
        s: &mut State,
        id: &str,
        outcome: &'static str,
        failure: Option<&'static str>,
    ) -> Result<(), PublicError> {
        s.runtime
            .coordinator
            .mark_phase(id, "cleanup")
            .map_err(coordinator)?;
        let e = s.entries.get_mut(id).ok_or(error("unknown_request"))?;
        e.phase = Phase::Cleanup;
        e.outcome = outcome;
        e.failure = failure;
        self.append(s, id, "lifecycle", json!({"phase":"completion"}))
    }
    fn publish(&self, s: &mut State, id: &str) -> Result<(), PublicError> {
        let e = s.entries.get(id).ok_or(error("unknown_request"))?;
        let payload = if e.outcome == "failed" {
            json!({"code":e.failure.unwrap_or("backend_failed")})
        } else {
            json!({})
        };
        let now = Self::now(s)?;
        s.registry
            .append_terminal(id, e.outcome, payload, now)
            .map_err(registry)?;
        s.registry.mark_worker_done(id).map_err(registry)?;
        let e = s.entries.get_mut(id).ok_or(error("unknown_request"))?;
        e.phase = Phase::Terminal;
        e.progress_refusal = None;
        Ok(())
    }
    /// Advance each owner independently; retained phase markers fence irreversible steps.
    fn advance(&self, s: &mut State, id: &str) -> Result<(), PublicError> {
        if s.entries[id].phase == Phase::Terminal {
            return Ok(());
        }
        if s.entries[id].phase == Phase::Cleanup {
            let must_stop = s.entries[id].outcome != "completed";
            if !s.executor.cleanup(id, must_stop)? {
                return Ok(());
            }
            let entry = s.entries.get_mut(id).ok_or(error("unknown_request"))?;
            entry.phase = Phase::CleanupProven;
            entry.progress_refusal = None;
        }
        if s.entries[id].phase == Phase::CleanupProven {
            if s.entries[id].outcome == "completed"
                && let Err(e) = self.current(&s.entries[id].qualification)
            {
                let entry = s.entries.get_mut(id).ok_or(error("unknown_request"))?;
                entry.outcome = "failed";
                entry.failure = Some(e.code);
            }
            s.runtime
                .coordinator
                .complete(id, s.entries[id].outcome)
                .map_err(coordinator)?;
            let entry = s.entries.get_mut(id).ok_or(error("unknown_request"))?;
            entry.phase = Phase::PublicationPending;
            entry.progress_refusal = None;
        }
        if s.entries[id].phase == Phase::PublicationPending {
            return self.publish(s, id);
        }
        let phase = s.entries[id].phase;
        if let Err(e) = self.current(&s.entries[id].qualification) {
            if phase == Phase::Queued {
                self.discard_queued(s, id, "failed", Some(e.code))?;
            } else {
                self.begin_cleanup(s, id, "failed", Some(e.code))?;
            }
            return Ok(());
        }
        if phase == Phase::Queued {
            return Ok(());
        }
        // Reserve completion and terminal slots; never poll a token that cannot be kept.
        s.registry
            .trim_acknowledged(id, self.config.max_buffered_events - 3)
            .map_err(registry)?;
        if s.registry.buffered_event_count(id).map_err(registry)?
            >= self.config.max_buffered_events - 2
        {
            return Ok(());
        }
        match s.executor.poll(id) {
            Ok(Execution::Pending) => {}
            Ok(Execution::Token { token_id, text }) => {
                let e = &s.entries[id];
                if e.next_token >= e.max_tokens {
                    self.begin_cleanup(s, id, "failed", Some("token_limit_exceeded"))?;
                    return Ok(());
                }
                if let Err(e) = self.current(&s.entries[id].qualification) {
                    self.begin_cleanup(s, id, "failed", Some(e.code))?;
                    return Ok(());
                }
                let payload = json!({"token_id":token_id,"text":text,"token_index":e.next_token});
                s.runtime
                    .coordinator
                    .mark_phase(id, "decode")
                    .map_err(coordinator)?;
                self.append(s, id, "token", payload)?;
                s.entries
                    .get_mut(id)
                    .ok_or(error("unknown_request"))?
                    .next_token += 1;
            }
            Ok(Execution::Completed) => self.begin_cleanup(s, id, "completed", None)?,
            Err(e) => self.begin_cleanup(s, id, "failed", Some(e.code))?,
        }
        Ok(())
    }
    fn discard_queued(
        &self,
        s: &mut State,
        id: &str,
        outcome: &'static str,
        failure: Option<&'static str>,
    ) -> Result<(), PublicError> {
        if !s.runtime.coordinator.cancel(id) {
            return Err(error("m16_pending_cancel_failed"));
        }
        let e = s.entries.get_mut(id).ok_or(error("unknown_request"))?;
        e.outcome = outcome;
        e.failure = failure;
        e.phase = Phase::PublicationPending;
        self.publish(s, id)
    }
    /// Admit a prepared request (coordinator, registry, owner entry): the one admission tail of
    /// every request on this route, product or startup challenge.
    fn admit(
        &self,
        s: &mut State,
        qualification: Value,
        p: Prepared,
        profile: &str,
        max_tokens: u64,
        product_stop_tokens: bool,
    ) -> Result<Accepted, PublicError> {
        let now = Self::now(s)?;
        let id = p.context.request_id.clone();
        // Consume before calling: even a rejected path can retain ledger/history records.
        s.admission_attempts += 1;
        s.runtime
            .coordinator
            .admit(&p.context, profile, &p.excluded_placements)
            .map_err(coordinator)?;
        let mut evicted = Vec::new();
        if let Err(e) = s
            .registry
            .submit(&id, Some(&p.owner_token), now, &mut evicted)
        {
            if !s.runtime.coordinator.cancel(&id) {
                return Err(error("m16_admission_rollback_failed"));
            }
            return Err(registry(e));
        }
        // Room was checked before admission; this owner never permits registry eviction.
        if !evicted.is_empty() {
            return Err(error("m16_unexpected_registry_eviction"));
        }
        s.entries.insert(
            id.clone(),
            Entry {
                qualification,
                phase: Phase::Queued,
                outcome: "completed",
                failure: None,
                progress_refusal: None,
                next_token: 0,
                max_tokens,
                product_stop_tokens,
            },
        );
        self.changed.notify_all();
        Ok(Accepted {
            request_id: id,
            session_token: p.owner_token,
        })
    }
}
impl M16Requests {
    pub fn new(
        runtime: LiveM16Runtime,
        host: Arc<dyn Host>,
        executor: Box<dyn Executor>,
        config: Config,
    ) -> Result<Self, PublicError> {
        let config = Config::new(
            config.max_buffered_events,
            config.max_sessions,
            config.eviction_grace,
        )
        .map_err(registry)?;
        if config.max_buffered_events < 4 {
            return Err(error("invalid_max_buffered_events"));
        }
        let poll = BudgetId::OwnerPoll
            .span(Entries::new(0))
            .map_err(|_| error("m16_poll_budget_unavailable"))?;
        Ok(Self {
            shared: Arc::new(Shared {
                state: Mutex::new(State {
                    runtime,
                    registry: Registry::new(config),
                    entries: BTreeMap::new(),
                    executor,
                    admission_attempts: 0,
                }),
                changed: Condvar::new(),
                host,
                config,
                poll: Duration::from_millis(poll.millis()),
            }),
        })
    }
    /// One bounded pump step. Error does not erase retained executor cleanup ownership.
    pub fn tick(&self) -> Result<(), PublicError> {
        let result = self.tick_inner();
        self.shared.changed.notify_all();
        result
    }
    fn tick_inner(&self) -> Result<(), PublicError> {
        let sh = &self.shared;
        let mut s = sh.lock()?;
        let ids: Vec<String> = s.entries.keys().cloned().collect();
        let mut refusals = Vec::new();
        for id in ids {
            if let Err(e) = sh.advance(&mut s, &id) {
                s.entries
                    .get_mut(&id)
                    .ok_or(error("unknown_request"))?
                    .progress_refusal = Some(e.code);
                refusals.push(e);
            }
        }
        if let Some(id) = s.runtime.coordinator.next_dispatch().map_err(coordinator)? {
            let dispatch = Dispatch {
                context: s
                    .runtime
                    .coordinator
                    .request_context(&id)
                    .map_err(|e| error(e.code))?,
                identity: s
                    .runtime
                    .coordinator
                    .route_identity(&id)
                    .map_err(|e| error(e.code))?,
                manifest: s
                    .runtime
                    .coordinator
                    .path_manifest(&id)
                    .map_err(|e| error(e.code))?,
                product_stop_tokens: s
                    .entries
                    .get(&id)
                    .ok_or(error("m16_dispatch_unknown_request"))?
                    .product_stop_tokens,
            };
            s.entries
                .get_mut(&id)
                .ok_or(error("m16_dispatch_unknown_request"))?
                .phase = Phase::Active;
            sh.append(&mut s, &id, "lifecycle", json!({"phase":"prefill"}))?;
            if let Err(e) = s.executor.start(&dispatch) {
                sh.begin_cleanup(&mut s, &id, "failed", Some(e.code))?;
            }
        }
        // next_dispatch can expire queued reservations without invoking the executor.
        let expired: Vec<String> = s
            .entries
            .iter()
            .filter(|(id, e)| {
                e.phase == Phase::Queued && s.runtime.coordinator.terminal_state(id).is_some()
            })
            .map(|(id, _)| id.clone())
            .collect();
        for id in expired {
            let e = s.entries.get_mut(&id).ok_or(error("unknown_request"))?;
            e.outcome = "failed";
            e.failure = Some("reservation_expired");
            e.phase = Phase::PublicationPending;
            sh.publish(&mut s, &id)?;
        }
        match refusals.len() {
            0 => Ok(()),
            1 => Err(refusals.remove(0)),
            _ => Err(error("m16_multiple_progress_refusals")),
        }
    }
    /// The route's startup challenge (`infer_startup_challenge`, route.py:4563-4590 @ f4e366e9):
    /// the exact `prompt` token ids for exactly `max_new_tokens` new tokens, under the host's
    /// CURRENT qualification, admitted, dispatched, executed and settled as every request on this
    /// route is, with the product stop tokens disabled for this request only. The caller reads
    /// the tokens that came back through [`Service::subscribe`] with the returned owner token,
    /// and must cancel it if it stops reading before its terminal.
    pub fn submit_startup_challenge(
        &self,
        prompt: &[u32],
        max_new_tokens: u64,
    ) -> Result<Accepted, PublicError> {
        const PROFILE: &str = "interactive";
        // `infer`: `max_new_tokens < 1` is invalid (route.py:4621-4628); the workflow counts at
        // most 4096 challenge tokens (workflow.py:206).
        if prompt.is_empty() || !(1..=4096).contains(&max_new_tokens) {
            return Err(error("invalid_inference_request"));
        }
        let sh = &self.shared;
        let mut s = sh.lock()?;
        let qualification = sh.host.qualification()?;
        for field in [
            "deployment_id",
            "deployment_epoch",
            "topology_version",
            "model_id",
            "resolved_commit",
            "manifest_digest",
        ] {
            if qualification[field] != s.runtime.graph.as_value()[field] {
                return Err(error("qualification_mismatch"));
            }
        }
        if s.entries.len() >= sh.config.max_sessions {
            return Err(error("gateway_capacity_exhausted"));
        }
        if s.admission_attempts >= sh.config.max_sessions {
            return Err(error("m16_admission_attempt_budget_exhausted"));
        }
        let p = sh.host.prepare_tokens(prompt, max_new_tokens, PROFILE)?;
        if !registry::valid_request_id(&p.context.request_id) {
            return Err(error("invalid_request_id"));
        }
        if !crate::requests::valid_owner_token(&p.owner_token) {
            return Err(error("invalid_session_token"));
        }
        if s.entries.contains_key(&p.context.request_id) {
            return Err(error("duplicate_request_id"));
        }
        if p.context.qos_class != PROFILE
            || Some(p.context.max_new_tokens.clone()) != PyReal::from_json(&json!(max_new_tokens))
            || p.context.prompt_token_ids.len() != prompt.len()
        {
            return Err(error("m16_prepared_request_mismatch"));
        }
        sh.current(&qualification)?;
        sh.admit(&mut s, qualification, p, PROFILE, max_new_tokens, false)
    }
    /// All retained per-owner refusal codes, including settlement/publication failures.
    /// A tick still advances other owners before returning a refusal (aggregate if multiple).
    pub fn progress_refusals(&self) -> Result<BTreeMap<String, &'static str>, PublicError> {
        Ok(self
            .shared
            .lock()?
            .entries
            .iter()
            .filter_map(|(id, e)| e.progress_refusal.map(|code| (id.clone(), code)))
            .collect())
    }
    /// Outstanding work is distinct from executor cleanup debt after cleanup is proven.
    pub fn pending_work(&self, id: &str) -> Result<Option<&'static str>, PublicError> {
        let s = self.shared.lock()?;
        let e = s.entries.get(id).ok_or(error("unknown_request"))?;
        Ok(match e.phase {
            Phase::Cleanup => Some("cleanup_pending"),
            Phase::CleanupProven => Some("coordinator_settlement_pending"),
            Phase::PublicationPending => Some("terminal_publication_pending"),
            _ => None,
        })
    }
    pub fn terminal(&self, id: &str) -> Result<Option<String>, PublicError> {
        Ok(self.shared.lock()?.registry.terminal(id).map(str::to_owned))
    }
    /// Watch-only observation: never wait behind an executor/host callback.
    /// Busy is not terminal proof; the owner must keep watching its Stop token.
    pub fn try_terminal(&self, id: &str) -> Result<Option<String>, PublicError> {
        let state = self.shared.state.try_lock().map_err(|e| match e {
            std::sync::TryLockError::WouldBlock => error("m16_owner_busy"),
            std::sync::TryLockError::Poisoned(_) => error("m16_owner_poisoned"),
        })?;
        Ok(state.registry.terminal(id).map(str::to_owned))
    }
    pub fn cleanup_debt(&self, id: &str) -> Result<Option<&'static str>, PublicError> {
        Ok(self
            .shared
            .lock()?
            .entries
            .get(id)
            .filter(|e| e.phase == Phase::Cleanup)
            .map(|e| e.progress_refusal.unwrap_or("cleanup_pending")))
    }
    /// Retained coordinator incidents; observation only, never drives its lifecycle.
    pub fn incidents(&self) -> Result<Vec<runtime::m16_coordinator::Incident>, PublicError> {
        Ok(self.shared.lock()?.runtime.coordinator.incidents())
    }
    pub fn reservations(&self, id: &str) -> Result<Vec<Reservation>, PublicError> {
        Ok(self
            .shared
            .lock()?
            .runtime
            .coordinator
            .request_reservations(id))
    }
    pub fn placement_status(&self) -> Result<Vec<PlacementStatus>, PublicError> {
        self.shared
            .lock()?
            .runtime
            .coordinator
            .placement_status()
            .map_err(|e| error(e.0))
    }
    pub fn route_identity(&self, id: &str) -> Result<RouteIdentity, PublicError> {
        self.shared
            .lock()?
            .runtime
            .coordinator
            .route_identity(id)
            .map_err(|e| error(e.code))
    }
}
impl Service for M16Requests {
    fn authorized(&self, r: &Request) -> bool {
        self.shared.host.authorized(r)
    }
    fn health(&self) -> Result<Value, PublicError> {
        Ok(json!({"status":"ok"}))
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        self.shared.host.qualification()
    }
    fn recovery(&self) -> Result<Value, PublicError> {
        Err(error("m16_recovery_unavailable"))
    }
    fn submit(&self, document: Value) -> Result<Accepted, PublicError> {
        protocol::request::submission(&document).map_err(|e| error(e.0))?;
        if document["protocol"] != "mycelium.request_gateway.v2" {
            return Err(error("unsupported_request_protocol"));
        }
        let sh = &self.shared;
        let mut s = sh.lock()?;
        sh.current(&document["qualification"])?;
        // Bind the host's qualified deployment/model to this immutable factory graph.
        for field in [
            "deployment_id",
            "deployment_epoch",
            "topology_version",
            "model_id",
            "resolved_commit",
            "manifest_digest",
        ] {
            if document["qualification"][field] != s.runtime.graph.as_value()[field] {
                return Err(error("qualification_mismatch"));
            }
        }
        if s.entries.len() >= sh.config.max_sessions {
            return Err(error("gateway_capacity_exhausted"));
        }
        if s.admission_attempts >= sh.config.max_sessions {
            return Err(error("m16_admission_attempt_budget_exhausted"));
        }
        let p = sh.host.prepare(&document)?;
        if !registry::valid_request_id(&p.context.request_id) {
            return Err(error("invalid_request_id"));
        }
        if !crate::requests::valid_owner_token(&p.owner_token) {
            return Err(error("invalid_session_token"));
        }
        if s.entries.contains_key(&p.context.request_id) {
            return Err(error("duplicate_request_id"));
        }
        if p.context.qos_class != document["qos_class"]
            || Some(p.context.max_new_tokens.clone())
                != PyReal::from_json(&document["max_new_tokens"])
        {
            return Err(error("m16_prepared_request_mismatch"));
        }
        sh.current(&document["qualification"])?;
        let profile = document["workload_profile_id"]
            .as_str()
            .ok_or(error("invalid_workload_profile_id"))?;
        let max_tokens = document["max_new_tokens"]
            .as_u64()
            .ok_or(error("invalid_max_new_tokens"))?;
        sh.admit(
            &mut s,
            document["qualification"].clone(),
            p,
            profile,
            max_tokens,
            true,
        )
    }
    fn cancel(&self, id: &str, token: Option<&str>) -> Result<bool, PublicError> {
        let sh = &self.shared;
        let mut s = sh.lock()?;
        // Registry false also means already cancelling, NOT necessarily terminal.
        s.registry.cancel(id, token).map_err(registry)?;
        if s.registry.terminal(id).is_some() {
            return Ok(false);
        }
        match s.entries.get(id).ok_or(error("unknown_request"))?.phase {
            Phase::Queued => sh.discard_queued(&mut s, id, "cancelled", None)?,
            Phase::Active => sh.begin_cleanup(&mut s, id, "cancelled", None)?,
            Phase::Cleanup | Phase::CleanupProven => {
                s.entries
                    .get_mut(id)
                    .ok_or(error("unknown_request"))?
                    .outcome = "cancelled";
            }
            // Settlement fixed the outcome; cancellation cannot rewrite pending publication.
            Phase::PublicationPending => {}
            Phase::Terminal => return Ok(false),
        }
        sh.changed.notify_all();
        Ok(true)
    }
    fn subscribe(
        &self,
        id: &str,
        cursor: Option<(u64, u64)>,
        token: Option<&str>,
    ) -> Result<Box<dyn Subscription>, PublicError> {
        let mut s = self.shared.lock()?;
        let cursor = cursor
            .map(|(g, n)| {
                Ok::<LastEventId, PublicError>(LastEventId::Bound(
                    i64::try_from(g).map_err(|_| error("invalid_last_event_id"))?,
                    i64::try_from(n).map_err(|_| error("invalid_last_event_id"))?,
                ))
            })
            .transpose()?;
        let now = Shared::now(&s)?;
        // No signed executor generation is asserted here; registry generations own delivery only.
        let sub = s
            .registry
            .subscribe(id, cursor, token, now, &mut |_, _, _| Ok(true))
            .map_err(registry)?;
        Ok(Box::new(Stream {
            shared: self.shared.clone(),
            sub,
        }))
    }
    fn validate_event(&self, event: &Value) -> Result<(), PublicError> {
        protocol::request::event(event).map_err(|e| error(e.0))
    }
}
struct Stream {
    shared: Arc<Shared>,
    sub: registry::Subscription,
}
impl Subscription for Stream {
    fn next(&mut self) -> Result<Option<Value>, PublicError> {
        let deadline = Instant::now() + self.shared.poll;
        let mut s = self.shared.lock()?;
        loop {
            match s.registry.next_event(&mut self.sub).map_err(registry)? {
                Pull::End => return Ok(None),
                Pull::Event(e) => {
                    let mut value = json!({"protocol":"mycelium.request_event.v2","request_id":self.sub.request_id,"sequence":e.sequence,"publisher_generation":self.sub.publisher_generation,"type":e.kind});
                    if let Some(fields) = e.payload.as_object() {
                        for (key, item) in fields {
                            value[key] = item.clone();
                        }
                    }
                    protocol::request::event(&value).map_err(|e| error(e.0))?;
                    return Ok(Some(value));
                }
                Pull::Pending => {
                    let left = deadline.saturating_duration_since(Instant::now());
                    if left.is_zero() {
                        return Err(error("m16_stream_poll_timeout"));
                    }
                    s = self
                        .shared
                        .changed
                        .wait_timeout(s, left)
                        .map_err(|_| error("m16_owner_poisoned"))?
                        .0;
                }
            }
        }
    }
    fn publisher_generation(&self) -> Option<u64> {
        Some(self.sub.publisher_generation)
    }
    fn ack(&mut self, sequence: u64) -> Result<(), PublicError> {
        let mut s = self.shared.lock()?;
        let now = Shared::now(&s)?;
        s.registry
            .ack(
                &mut self.sub,
                i64::try_from(sequence).map_err(|_| error("invalid_ack"))?,
                now,
            )
            .map_err(registry)
    }
    fn close(&mut self) -> Result<(), PublicError> {
        self.shared
            .lock()?
            .registry
            .close_subscription(&mut self.sub)
            .map_err(registry)
    }
}
