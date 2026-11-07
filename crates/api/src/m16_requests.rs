// SPDX-License-Identifier: AGPL-3.0-only
//! M16 admission -> dispatch -> cleanup -> registry terminal, behind `requests::Service`.
//!
//! A host supplies current qualification, tokenizer-bound contexts and owner-token entropy.
//! An executor supplies bounded, nonblocking start/poll/cleanup effects. Calls are serialized;
//! executor/host callbacks must not re-enter this service. `tick` belongs to the host's pump.
//! Cleanup failure retains the executor, dispatch slot and ledger reservations for retry.
//! This is NOT a signed worker/MLX implementation or installed-host registration.
//!
//! History and total admissions are bounded by the supplied registry config. This owner does
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
}
#[derive(Clone, Debug)]
pub struct Dispatch {
    pub context: RequestContext,
    pub identity: RouteIdentity,
    pub manifest: Value,
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
    Terminal,
}
struct Entry {
    qualification: Value,
    phase: Phase,
    outcome: &'static str,
    failure: Option<&'static str>,
    cleanup_refusal: Option<&'static str>,
    next_token: u64,
    max_tokens: u64,
}
struct State {
    runtime: LiveM16Runtime,
    registry: Registry,
    entries: BTreeMap<String, Entry>,
    executor: Box<dyn Executor>,
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
        s.entries.get_mut(id).ok_or(error("unknown_request"))?.phase = Phase::Terminal;
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
        self.publish(s, id)
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
        for id in ids {
            let phase = s.entries[&id].phase;
            if phase == Phase::Terminal {
                continue;
            }
            if phase == Phase::Cleanup {
                let must_stop = s.entries[&id].outcome != "completed";
                match s.executor.cleanup(&id, must_stop) {
                    Err(e) => {
                        s.entries
                            .get_mut(&id)
                            .ok_or(error("unknown_request"))?
                            .cleanup_refusal = Some(e.code);
                        return Err(e);
                    }
                    Ok(false) => continue,
                    Ok(true) => {}
                }
                if s.entries[&id].outcome == "completed"
                    && let Err(e) = sh.current(&s.entries[&id].qualification)
                {
                    let entry = s.entries.get_mut(&id).ok_or(error("unknown_request"))?;
                    entry.outcome = "failed";
                    entry.failure = Some(e.code);
                }
                let outcome = s.entries[&id].outcome;
                s.runtime
                    .coordinator
                    .complete(&id, outcome)
                    .map_err(coordinator)?;
                s.entries
                    .get_mut(&id)
                    .ok_or(error("unknown_request"))?
                    .cleanup_refusal = None;
                sh.publish(&mut s, &id)?;
                continue;
            }
            if let Err(e) = sh.current(&s.entries[&id].qualification) {
                if phase == Phase::Queued {
                    sh.discard_queued(&mut s, &id, "failed", Some(e.code))?;
                } else {
                    sh.begin_cleanup(&mut s, &id, "failed", Some(e.code))?;
                }
                continue;
            }
            if phase == Phase::Queued {
                continue;
            }
            // Reserve completion and terminal slots; never poll a token that cannot be kept.
            s.registry
                .trim_acknowledged(&id, sh.config.max_buffered_events - 3)
                .map_err(registry)?;
            if s.registry.buffered_event_count(&id).map_err(registry)?
                >= sh.config.max_buffered_events - 2
            {
                continue;
            }
            match s.executor.poll(&id) {
                Ok(Execution::Pending) => {}
                Ok(Execution::Token { token_id, text }) => {
                    let e = &s.entries[&id];
                    if e.next_token >= e.max_tokens {
                        sh.begin_cleanup(&mut s, &id, "failed", Some("token_limit_exceeded"))?;
                        continue;
                    }
                    if let Err(e) = sh.current(&s.entries[&id].qualification) {
                        sh.begin_cleanup(&mut s, &id, "failed", Some(e.code))?;
                        continue;
                    }
                    let payload =
                        json!({"token_id":token_id,"text":text,"token_index":e.next_token});
                    s.runtime
                        .coordinator
                        .mark_phase(&id, "decode")
                        .map_err(coordinator)?;
                    sh.append(&mut s, &id, "token", payload)?;
                    s.entries
                        .get_mut(&id)
                        .ok_or(error("unknown_request"))?
                        .next_token += 1;
                }
                Ok(Execution::Completed) => sh.begin_cleanup(&mut s, &id, "completed", None)?,
                Err(e) => sh.begin_cleanup(&mut s, &id, "failed", Some(e.code))?,
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
            sh.publish(&mut s, &id)?;
        }
        Ok(())
    }
    pub fn terminal(&self, id: &str) -> Result<Option<String>, PublicError> {
        Ok(self.shared.lock()?.registry.terminal(id).map(str::to_owned))
    }
    pub fn cleanup_debt(&self, id: &str) -> Result<Option<&'static str>, PublicError> {
        Ok(self
            .shared
            .lock()?
            .entries
            .get(id)
            .filter(|e| e.phase == Phase::Cleanup)
            .map(|e| e.cleanup_refusal.unwrap_or("cleanup_pending")))
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
        let now = Shared::now(&s)?;
        let id = p.context.request_id.clone();
        let profile = document["workload_profile_id"]
            .as_str()
            .ok_or(error("invalid_workload_profile_id"))?;
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
                qualification: document["qualification"].clone(),
                phase: Phase::Queued,
                outcome: "completed",
                failure: None,
                cleanup_refusal: None,
                next_token: 0,
                max_tokens: document["max_new_tokens"]
                    .as_u64()
                    .ok_or(error("invalid_max_new_tokens"))?,
            },
        );
        sh.changed.notify_all();
        Ok(Accepted {
            request_id: id,
            session_token: p.owner_token,
        })
    }
    fn cancel(&self, id: &str, token: Option<&str>) -> Result<bool, PublicError> {
        let sh = &self.shared;
        let mut s = sh.lock()?;
        if !s.registry.cancel(id, token).map_err(registry)? {
            return Ok(false);
        }
        match s.entries.get(id).ok_or(error("unknown_request"))?.phase {
            Phase::Queued => sh.discard_queued(&mut s, id, "cancelled", None)?,
            Phase::Active => sh.begin_cleanup(&mut s, id, "cancelled", None)?,
            Phase::Cleanup => {
                s.entries
                    .get_mut(id)
                    .ok_or(error("unknown_request"))?
                    .outcome = "cancelled";
            }
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
