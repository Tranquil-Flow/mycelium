// SPDX-License-Identifier: AGPL-3.0-only
//! The multi-session request-gateway HOST: `RequestGatewayService` (mycelium_request_gateway/service.py:277-1453 and
//! qualification.py @ f4e366e9) around the pure [`Registry`]. Admission is per request: the qualification is
//! captured on EVERY submit (the binding matrix of qualification.py:70-91), a non-blocking pending slot is taken, then
//! under the lock the gateway-closed check, session room, the request id (drawn only now) and its validity and
//! uniqueness. A bounded pool of workers runs the admitted requests; each request revalidates its captured
//! qualification at run start, on every token, and before the `completed` terminal. The pending slot is released when
//! the worker finishes (service.py:888), not when the terminal is published.
//!
//! Checked against the executed oracle by `tests/gateway_host.rs` (15 scenarios, `oracle_gateway_admission.json`).
//!
//! Not ported (named, not claimed): the A6 replay checkpoint store, `recovery_public_projection`, lifecycle events of
//! the v2 event protocol, per-token `token_id` / encoded-prompt binding, the backend's `cancel_with_deadline` and
//! `update_publisher_generation` hooks, and `terminal_failure_code`. A backend `cancel` that REFUSES (returns false)
//! is recorded as `rejected`, so the worker publishes `completed` after a revalidation as in `_run`, but the
//! provisional cancellation latch is not undone (the Registry has no such undo yet). Backend cancellation is
//! infallible here (no panic isolation), so the `unproven` branch can only arise from the cleanup deadline.
use crate::registry::{
    AdmissionError, Config as RegistryConfig, LastEventId, Pull, Registry, Subscription,
};
use budgets::{BudgetId, Entries};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::VecDeque,
    sync::{
        Arc, Condvar, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

/// `QualificationBinding` (contracts.py): what a submission pins and what the current record is compared to.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Binding {
    pub deployment_id: String,
    pub deployment_epoch: u64,
    pub topology_version: u64,
    pub path_manifest_digest: String,
    pub qualification_id: String,
    pub model_id: String,
    pub resolved_commit: String,
    pub manifest_digest: String,
    pub qualification_digest: String,
    pub stage_load_proof_digests: Vec<String>,
}
/// The current route qualification record the source answers with.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Qualification {
    pub binding: Binding,
    pub route_ready: bool,
}
/// The source raised, or answered something that is not a qualification record.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Unavailable;
/// `QualificationSource`: read-only access to the qualifier's current exact record. `request_id` is `Some` when
/// an admitted request is revalidated (a source may scope authority per request), `None` at admission.
pub trait QualificationSource: Send + Sync {
    fn current(&self, request_id: Option<&str>) -> Result<Option<Qualification>, Unavailable>;
    /// The current record as its public `mycelium.route_qualification.v1` document, as the product
    /// application reads it (`route_qualification_to_dict(qualification_source.current())`,
    /// supervisor.py:2166-2168 @ f4e366e9). Default: the current record is read (a source that
    /// cannot be read answers `Unavailable`) and has no such document (`None`).
    fn record(&self) -> Result<Option<Value>, Unavailable> {
        self.current(None).map(|_| None)
    }
}
/// `InferenceSubmission` (the parts the host reads).
#[derive(Clone, Debug)]
pub struct Submission {
    pub binding: Binding,
    pub max_new_tokens: u64,
}
/// A closed refusal code (`AdmissionError.code`) raised inside a worker or an `emit`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Refusal(pub &'static str);
/// What a backend run ended as (`"completed" | "cancelled" | other`).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Outcome {
    Completed,
    Cancelled,
    Failed,
}
/// `InferenceBackend`.
pub trait Backend: Send + Sync {
    /// Run one request, delivering tokens through `emit(index, text)`; an `Err` from `emit` must end the run and
    /// be returned. `is_cancelled` is the request's stop latch.
    fn run(
        &self,
        request_id: &str,
        emit: &mut dyn FnMut(u64, &str) -> Result<(), Refusal>,
        is_cancelled: &dyn Fn() -> bool,
    ) -> Result<Outcome, Refusal>;
    /// Cancel a started request; false when the backend had already committed another terminal state.
    fn cancel(&self, request_id: &str) -> bool;
}
/// Construction parameters (service.py:307-330 ranges are enforced by [`Gateway::new`]).
#[derive(Clone, Copy, Debug)]
pub struct Config {
    pub max_buffered_events: usize,
    pub max_sessions: usize,
    pub max_concurrent_requests: usize,
    pub max_pending_requests: usize,
    /// `SESSION_EVICTION_GRACE_SECONDS`.
    pub eviction_grace: f64,
}
pub type IdSource = Box<dyn FnMut() -> String + Send>;
pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;

fn refuse<T>(code: &'static str) -> Result<T, Refusal> {
    Err(Refusal(code))
}
/// qualification.py:`_read_current`.
fn read_current(
    source: &dyn QualificationSource,
    request_id: Option<&str>,
) -> Result<Qualification, Refusal> {
    match source.current(request_id) {
        Err(Unavailable) => refuse("qualification_unavailable"),
        Ok(None) => refuse("route_dropped"),
        Ok(Some(q)) if !q.route_ready => refuse("readiness_revoked"),
        Ok(Some(q)) => Ok(q),
    }
}
/// qualification.py:`_compare`, in the oracle's order.
fn compare(requested: &Binding, actual: &Binding) -> Result<(), Refusal> {
    if requested.deployment_id != actual.deployment_id {
        return refuse("qualification_mismatch");
    }
    if requested.deployment_epoch != actual.deployment_epoch {
        return refuse("deployment_epoch_changed");
    }
    if requested.topology_version != actual.topology_version
        || requested.path_manifest_digest != actual.path_manifest_digest
    {
        return refuse("path_changed");
    }
    if requested.qualification_id != actual.qualification_id {
        return refuse("stale_qualification");
    }
    if requested.model_id != actual.model_id
        || requested.resolved_commit != actual.resolved_commit
        || requested.manifest_digest != actual.manifest_digest
        || requested.stage_load_proof_digests != actual.stage_load_proof_digests
        || requested.qualification_digest != actual.qualification_digest
    {
        return refuse("qualification_mismatch");
    }
    Ok(())
}

#[derive(Default)]
struct Start {
    cancellation_started: bool,
    backend_started: bool,
    backend_cancelled: bool,
    backend_cancel_complete: bool,
    backend_accepted: Option<bool>,
    cancellation_started_at: Option<f64>,
    /// Wall deadline of the cancellation + cleanup proof.
    cancellation_deadline: Option<Instant>,
}
#[derive(Default)]
struct Tokens {
    expected: u64,
    digests: Vec<[u8; 32]>,
}
/// One admitted request (`_Session` minus the event log, which the Registry owns).
struct Meta {
    id: String,
    captured: Mutex<Option<Binding>>,
    max_new_tokens: u64,
    start: Mutex<Start>,
    start_changed: Condvar,
    stop: AtomicBool,
    tokens: Mutex<Tokens>,
}
struct State {
    registry: Registry,
    /// Insertion order, as the oracle's `_sessions` dict.
    metas: Vec<Arc<Meta>>,
    queue: VecDeque<Arc<Meta>>,
    pending: usize,
    active_workers: usize,
    closed: bool,
    shutdown: bool,
}
struct Shared {
    state: Mutex<State>,
    changed: Condvar,
    source: Arc<dyn QualificationSource>,
    backend: Arc<dyn Backend>,
    ids: Mutex<IdSource>,
    clock: Clock,
    config: Config,
}
/// The gateway host. Dropping it closes it.
pub struct Gateway {
    shared: Arc<Shared>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}
fn cleanup_budget() -> Duration {
    BudgetId::CancelAndCleanup
        .span(Entries::new(0))
        .map_or(Duration::ZERO, |s| Duration::from_millis(s.millis()))
}

impl Gateway {
    /// service.py:307-345.
    pub fn new(
        config: Config,
        source: Arc<dyn QualificationSource>,
        backend: Arc<dyn Backend>,
        ids: IdSource,
        clock: Clock,
    ) -> Result<Self, &'static str> {
        let registry = RegistryConfig::new(
            config.max_buffered_events,
            config.max_sessions,
            config.eviction_grace,
        )
        .map_err(|e| match e.0 {
            "invalid_max_buffered_events" => "invalid_max_buffered_events",
            _ => "invalid_max_sessions",
        })?;
        if !(1..=64).contains(&config.max_concurrent_requests) {
            return Err("invalid_max_concurrent_requests");
        }
        if !(config.max_concurrent_requests..=65_536).contains(&config.max_pending_requests) {
            return Err("invalid_max_pending_requests");
        }
        let shared = Arc::new(Shared {
            state: Mutex::new(State {
                registry: Registry::new(registry),
                metas: Vec::new(),
                queue: VecDeque::new(),
                pending: 0,
                active_workers: 0,
                closed: false,
                shutdown: false,
            }),
            changed: Condvar::new(),
            source,
            backend,
            ids: Mutex::new(ids),
            clock,
            config,
        });
        for _ in 0..config.max_concurrent_requests {
            let worker = shared.clone();
            std::thread::Builder::new()
                .name("mycelium-request-worker".into())
                .spawn(move || worker.work())
                .map_err(|_| "worker_spawn_failed")?;
        }
        Ok(Self { shared })
    }
    /// service.py:`submit`.
    pub fn submit(
        &self,
        submission: &Submission,
        owner_token: Option<&str>,
    ) -> Result<String, AdmissionError> {
        self.shared.submit(submission, owner_token)
    }
    pub fn subscribe(
        &self,
        request_id: &str,
        last_event_id: Option<LastEventId>,
        owner_token: Option<&str>,
    ) -> Result<Subscription, AdmissionError> {
        let now = (self.shared.clock)();
        let mut state = lock(&self.shared.state);
        state.registry.subscribe(
            request_id,
            last_event_id,
            owner_token,
            now,
            &mut |_, _, _| Ok(true),
        )
    }
    /// The next event, waiting up to `timeout` for one (the host owns waiting).
    pub fn next_event(
        &self,
        sub: &mut Subscription,
        timeout: Duration,
    ) -> Result<Pull, AdmissionError> {
        let deadline = Instant::now() + timeout;
        let mut state = lock(&self.shared.state);
        loop {
            match state.registry.next_event(sub)? {
                Pull::Pending => {
                    let left = deadline.saturating_duration_since(Instant::now());
                    if left.is_zero() {
                        return Ok(Pull::Pending);
                    }
                    state = self
                        .shared
                        .changed
                        .wait_timeout(state, left)
                        .unwrap_or_else(std::sync::PoisonError::into_inner)
                        .0;
                }
                other => return Ok(other),
            }
        }
    }
    pub fn ack(&self, sub: &mut Subscription, sequence: i64) -> Result<(), AdmissionError> {
        let now = (self.shared.clock)();
        let result = lock(&self.shared.state).registry.ack(sub, sequence, now);
        self.shared.changed.notify_all();
        result
    }
    pub fn close_subscription(&self, sub: &mut Subscription) -> Result<(), AdmissionError> {
        let result = lock(&self.shared.state).registry.close_subscription(sub);
        self.shared.changed.notify_all();
        result
    }
    /// service.py:`cancel`.
    pub fn cancel(
        &self,
        request_id: &str,
        owner_token: Option<&str>,
    ) -> Result<bool, AdmissionError> {
        let meta = {
            let mut state = lock(&self.shared.state);
            if !state.registry.cancel(request_id, owner_token)? {
                return Ok(false);
            }
            state.metas.iter().find(|m| m.id == request_id).cloned()
        };
        if let Some(meta) = meta {
            self.shared.cancel_session(&meta);
        }
        Ok(true)
    }
    pub fn contains(&self, request_id: &str) -> bool {
        lock(&self.shared.state).registry.contains(request_id)
    }
    /// `None` when the request is unknown (never admitted or evicted); otherwise whether its worker has ended.
    pub fn worker_done(&self, request_id: &str) -> Option<bool> {
        lock(&self.shared.state).registry.worker_done(request_id)
    }
    pub fn buffered_event_count(&self, request_id: &str) -> Result<usize, AdmissionError> {
        lock(&self.shared.state)
            .registry
            .buffered_event_count(request_id)
    }
    /// service.py:`close`: no more admissions, every non-terminal request cancelled, the workers given the
    /// cancellation-and-cleanup budget to end, then the pool shut down.
    pub fn close(&self) {
        self.shared.close();
    }
}
impl Drop for Gateway {
    fn drop(&mut self) {
        self.shared.close();
    }
}

impl Shared {
    fn now(&self) -> f64 {
        (self.clock)()
    }
    fn submit(
        &self,
        submission: &Submission,
        owner_token: Option<&str>,
    ) -> Result<String, AdmissionError> {
        let fail = |r: Refusal| AdmissionError(r.0);
        if owner_token.is_some_and(|t| !(32..=256).contains(&t.len())) {
            return Err(AdmissionError("invalid_session_token"));
        }
        // QualificationGate.capture: on EVERY submit, before any slot or id.
        let current = read_current(&*self.source, None).map_err(fail)?;
        compare(&submission.binding, &current.binding).map_err(fail)?;
        let mut cancelled_ids = Vec::new();
        let (result, evicted) = {
            let mut state = lock(&self.state);
            if state.pending >= self.config.max_pending_requests {
                return Err(AdmissionError("gateway_capacity_exhausted"));
            }
            let before: Vec<Arc<Meta>> = state.metas.clone();
            let now = self.now();
            let result = {
                let State { registry, .. } = &mut *state;
                let mut ids = lock(&self.ids);
                registry.submit_drawing(&mut || (*ids)(), owner_token, now, 0, &mut cancelled_ids)
            };
            let evicted: Vec<Arc<Meta>> = before
                .into_iter()
                .filter(|m| cancelled_ids.iter().any(|id| *id == m.id))
                .collect();
            // Drop what the Registry no longer holds (evicted terminal sessions).
            let State {
                registry, metas, ..
            } = &mut *state;
            metas.retain(|m| registry.contains(&m.id));
            if let Ok(id) = &result {
                let meta = Arc::new(Meta {
                    id: id.clone(),
                    captured: Mutex::new(Some(current.binding)),
                    max_new_tokens: submission.max_new_tokens,
                    start: Mutex::new(Start::default()),
                    start_changed: Condvar::new(),
                    stop: AtomicBool::new(false),
                    tokens: Mutex::new(Tokens::default()),
                });
                state.metas.push(meta.clone());
                state.queue.push_back(meta);
                state.pending += 1;
                self.changed.notify_all();
            }
            (result, evicted)
        };
        // `_make_session_room_locked` cancelled the abandoned sessions before the id was drawn.
        for meta in evicted {
            self.cancel_session(&meta);
        }
        result
    }

    /// service.py:`_cancel_session` for a request whose Registry latch is already set (or gone).
    fn cancel_session(&self, meta: &Arc<Meta>) {
        let started = {
            let mut start = lock(&meta.start);
            if start.cancellation_started {
                return;
            }
            start.cancellation_started = true;
            start.cancellation_started_at = Some(self.now());
            start.cancellation_deadline = Some(Instant::now() + cleanup_budget());
            start.backend_started
        };
        if started {
            self.cancel_backend_once(meta);
        }
        meta.stop.store(true, Ordering::SeqCst);
        self.changed.notify_all();
    }
    /// service.py:`_cancel_backend_once`.
    fn cancel_backend_once(&self, meta: &Arc<Meta>) {
        {
            let mut start = lock(&meta.start);
            if !start.backend_started || start.backend_cancelled {
                return;
            }
            start.backend_cancelled = true;
        }
        let accepted = self.backend.cancel(&meta.id);
        let mut start = lock(&meta.start);
        start.backend_accepted = Some(accepted);
        start.backend_cancel_complete = true;
        meta.start_changed.notify_all();
    }
    fn close(&self) {
        let metas = {
            let mut state = lock(&self.state);
            if state.closed {
                return;
            }
            state.closed = true;
            state.registry.close();
            state.metas.clone()
        };
        for meta in &metas {
            let live = {
                let mut state = lock(&self.state);
                let live = state.registry.terminal(&meta.id).is_none();
                if live {
                    // The gateway's own cancellation has no owner check; a gone session needs no latch.
                    state
                        .registry
                        .cancel_unchecked(&meta.id)
                        .unwrap_or_default();
                }
                live
            };
            if live {
                self.cancel_session(meta);
            }
        }
        let deadline = Instant::now() + cleanup_budget();
        let mut state = lock(&self.state);
        while (!state.queue.is_empty() || state.active_workers > 0) && Instant::now() < deadline {
            let left = deadline.saturating_duration_since(Instant::now());
            state = self
                .changed
                .wait_timeout(state, left)
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .0;
        }
        // `shutdown(wait=False, cancel_futures=True)`.
        state.queue.clear();
        state.shutdown = true;
        self.changed.notify_all();
    }

    // ----- workers -----
    fn work(self: Arc<Self>) {
        loop {
            let meta = {
                let mut state = lock(&self.state);
                loop {
                    if let Some(meta) = state.queue.pop_front() {
                        state.active_workers += 1;
                        break meta;
                    }
                    if state.shutdown {
                        return;
                    }
                    state = self
                        .changed
                        .wait(state)
                        .unwrap_or_else(std::sync::PoisonError::into_inner);
                }
            };
            self.run(&meta);
            let mut state = lock(&self.state);
            state.active_workers -= 1;
            self.changed.notify_all();
        }
    }
    /// service.py:`_run`.
    fn run(&self, meta: &Arc<Meta>) {
        if let Err(Refusal(code)) = self.run_request(meta) {
            if code == "request_cancelled" && meta.stop.load(Ordering::SeqCst) {
                self.append_cancellation_terminal(meta);
            } else {
                meta.stop.store(true, Ordering::SeqCst);
                self.cancel_backend_once(meta);
                self.append_terminal(meta, "failed", Some(code));
            }
        }
        // `finally`: drop the request state, mark the worker done, release the pending slot.
        *lock(&meta.captured) = None;
        {
            let mut tokens = lock(&meta.tokens);
            tokens.digests.clear();
        }
        let mut state = lock(&self.state);
        // A request evicted meanwhile is simply unknown to the Registry.
        state
            .registry
            .mark_worker_done(&meta.id)
            .unwrap_or_default();
        state.pending -= 1;
        self.changed.notify_all();
    }
    fn captured(&self, meta: &Arc<Meta>) -> Result<Binding, Refusal> {
        lock(&meta.captured)
            .clone()
            .map_or_else(|| refuse("request_state_released"), Ok)
    }
    /// QualificationGate.revalidate.
    fn revalidate(&self, meta: &Arc<Meta>) -> Result<(), Refusal> {
        let captured = self.captured(meta)?;
        let current = read_current(&*self.source, Some(&meta.id))?;
        compare(&captured, &current.binding)
    }
    fn run_request(&self, meta: &Arc<Meta>) -> Result<(), Refusal> {
        self.revalidate(meta)?;
        let cancelled_before_start = {
            let mut start = lock(&meta.start);
            if start.cancellation_started {
                true
            } else {
                // The logical backend-start linearization point.
                start.backend_started = true;
                false
            }
        };
        if cancelled_before_start {
            self.append_terminal(meta, "cancelled", None);
            return Ok(());
        }
        let outcome = self.backend.run(
            &meta.id,
            &mut |index, text| self.accept_token(meta, index, text),
            &|| meta.stop.load(Ordering::SeqCst),
        )?;
        match outcome {
            Outcome::Completed => match self.await_backend_cancellation(meta) {
                Resolution::Accepted => self.append_cancellation_terminal(meta),
                Resolution::Unproven => self.append_cancellation_failure(meta),
                Resolution::Rejected | Resolution::NotStarted => {
                    self.revalidate(meta)?;
                    self.append_terminal(meta, "completed", None);
                }
            },
            Outcome::Cancelled => match self.await_backend_cancellation(meta) {
                Resolution::Unproven => self.append_cancellation_failure(meta),
                _ => self.append_cancellation_terminal(meta),
            },
            Outcome::Failed => self.append_terminal(meta, "failed", Some("backend_failed")),
        }
        Ok(())
    }
    /// service.py:`_await_backend_cancellation`.
    fn await_backend_cancellation(&self, meta: &Arc<Meta>) -> Resolution {
        let mut start = lock(&meta.start);
        if !start.cancellation_started {
            return Resolution::NotStarted;
        }
        if !start.backend_started {
            return Resolution::Accepted;
        }
        let deadline = start.cancellation_deadline;
        while !start.backend_cancel_complete {
            let left = deadline.map_or(Duration::ZERO, |d| {
                d.saturating_duration_since(Instant::now())
            });
            if left.is_zero() {
                return Resolution::Unproven;
            }
            start = meta
                .start_changed
                .wait_timeout(start, left)
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .0;
        }
        if start.backend_accepted == Some(false) {
            Resolution::Rejected
        } else {
            Resolution::Accepted
        }
    }
    /// service.py:`_append_cancellation_terminal`: `cancelled` inside the cancellation-and-cleanup bound.
    fn append_cancellation_terminal(&self, meta: &Arc<Meta>) {
        let started_at = lock(&meta.start).cancellation_started_at;
        match started_at {
            Some(at) if self.now() - at > cleanup_budget().as_secs_f64() => {
                self.append_cancellation_failure(meta);
            }
            _ => self.append_terminal(meta, "cancelled", None),
        }
    }
    fn append_cancellation_failure(&self, meta: &Arc<Meta>) {
        self.append_terminal(
            meta,
            "failed",
            Some("cancellation_cleanup_deadline_exceeded"),
        );
    }
    fn append_terminal(&self, meta: &Arc<Meta>, kind: &str, code: Option<&str>) {
        let payload = code.map_or(Value::Null, |c| json!({ "code": c }));
        let now = self.now();
        let mut state = lock(&self.state);
        // An evicted request (unknown to the Registry) has nobody left to tell.
        state
            .registry
            .append_terminal(&meta.id, kind, payload, now)
            .unwrap_or_default();
        self.changed.notify_all();
    }
    /// service.py:`_accept_token` (text bound, per-token revalidation, order, bounded buffer, vanished and stalled
    /// consumers).
    fn accept_token(&self, meta: &Arc<Meta>, index: u64, text: &str) -> Result<(), Refusal> {
        let max_text = BudgetId::PyShiftMyceliumRequestGatewayServiceMaxBackendTokenTextBytesL37C0
            .bytes()
            .map_or(0, |b| b.get());
        if text.len() as u64 > max_text {
            return refuse("invalid_backend_token");
        }
        let digest: [u8; 32] = Sha256::digest(format!("None:{text}").as_bytes()).into();
        let wait = Duration::from_millis(
            BudgetId::PyMyceliumRequestGatewayServiceRequestgatewayserviceAcceptTokenTimeoutL1022
                .span(Entries::new(0))
                .map_or(0, |s| s.millis()),
        );
        let stall = BudgetId::PyMyceliumDeadlinesGatewaySessionSilentSubscriberStallSecondsL258
            .span(Entries::new(0))
            .map_or(0.0, |s| s.millis() as f64 / 1000.0);
        loop {
            self.revalidate(meta)?;
            let mut state = lock(&self.state);
            if meta.stop.load(Ordering::SeqCst) || state.registry.terminal(&meta.id).is_some() {
                return refuse("request_cancelled");
            }
            {
                let tokens = lock(&meta.tokens);
                if index < tokens.expected {
                    // A re-delivered token must be byte-identical.
                    return if tokens.digests.get(index as usize) == Some(&digest) {
                        Ok(())
                    } else {
                        refuse("token_order_violation")
                    };
                }
                if index != tokens.expected || index >= meta.max_new_tokens {
                    return refuse("token_order_violation");
                }
            }
            // Preserve one replay event when capacity permits, but always reserve one slot for the terminal.
            let capacity = self.config.max_buffered_events;
            let now = self.now();
            state
                .registry
                .trim_acknowledged(&meta.id, capacity - 2)
                .map_err(|_| Refusal("request_cancelled"))?;
            let buffered = state
                .registry
                .buffered_event_count(&meta.id)
                .map_err(|_| Refusal("request_cancelled"))?;
            if buffered < capacity - 1 {
                state
                    .registry
                    .append(
                        &meta.id,
                        "token",
                        json!({"token_index": index, "text": text}),
                        now,
                    )
                    .map_err(|_| Refusal("event_backpressure"))?;
                let mut tokens = lock(&meta.tokens);
                tokens.digests.push(digest);
                tokens.expected += 1;
                self.changed.notify_all();
                return Ok(());
            }
            if !state.registry.active_subscription(&meta.id) {
                // The consumer vanished: drop the oldest unacknowledged event so the worker is never pinned.
                state
                    .registry
                    .drop_oldest_unacknowledged(&meta.id)
                    .map_err(|_| Refusal("request_cancelled"))?;
                continue;
            }
            if state
                .registry
                .last_ack_at(&meta.id)
                .ok()
                .flatten()
                .is_some_and(|at| now - at > stall)
            {
                // Attached but silent while the buffer is full: reclaim the slot; the request is never cancelled.
                state
                    .registry
                    .detach_subscription(&meta.id)
                    .unwrap_or_default();
                state
                    .registry
                    .drop_oldest_unacknowledged(&meta.id)
                    .map_err(|_| Refusal("request_cancelled"))?;
                continue;
            }
            drop(
                self.changed
                    .wait_timeout(state, wait)
                    .unwrap_or_else(std::sync::PoisonError::into_inner),
            );
        }
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Resolution {
    NotStarted,
    Accepted,
    Rejected,
    Unproven,
}
