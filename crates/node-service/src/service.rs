// SPDX-License-Identifier: AGPL-3.0-only
//! S9 development host: one admitted request per generation, one unacknowledged
//! public event. No replay/history promise; rollover must explicitly reap driver.
use api::{
    actions::{Action, Backend},
    requests::{Accepted, Service, Subscription},
    schema::PublicError,
};
use budgets::{BudgetId, Entries};
use protocol::ContractError;
use router::cleanup::{CleanupProof, Terminal};
use runtime::composition::Session;
use serde_json::{Map, Value, json};
use std::{
    io::Read,
    sync::{Arc, Condvar, Mutex},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
fn error(code: &'static str) -> PublicError {
    PublicError::new(409, code)
}
pub fn duration(id: BudgetId) -> Duration {
    Duration::from_millis(
        id.span(Entries::ZERO)
            .expect("registered duration")
            .millis(),
    )
}
pub fn random_id() -> Result<String, PublicError> {
    let mut bytes = [0u8; 32];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| error("entropy_unavailable"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
struct State {
    session: Option<Session>,
    slot: Option<Value>,
    delivered: bool,
    acked: Option<u64>,
    cancelled: bool,
    disconnected: bool,
    done: bool,
    failure: Option<PublicError>,
    subscribed: bool,
    verified_tokens: usize,
    emitted_tokens: usize,
    lifecycle: Option<Value>,
    reap: Option<crate::reap::ReapProof>,
}
impl State {
    /// The driver failed and its cleanup is proven: either by local reaping of every child it
    /// spawned, or by a real signed cleanup quorum that settled the session as `Failed`. This
    /// is evidence, not a gate: v0.1.4 lets the next Start and the Quit proceed without it.
    fn failed_clean(&self) -> bool {
        self.failure.is_some()
            && (self.reap.is_some() || self.session.as_ref().and_then(Session::terminal).is_some())
    }
}
struct Shared {
    state: Mutex<State>,
    changed: Condvar,
}
pub struct Job {
    pub id: String,
    pub generation: u64,
    pub document: Value,
    pub deadline: Instant,
    shared: Arc<Shared>,
    sequence: u64,
}
impl Job {
    pub fn request_cancel(&self) -> Result<(), PublicError> {
        cancel_shared(&self.shared).map(|_| ())
    }
    /// Cancellation callable from another thread (the worker-death watch): it releases a
    /// driver blocked waiting for the page's acknowledgement.
    pub fn canceller(&self) -> impl Fn() + Send + Sync + 'static {
        let shared = Arc::clone(&self.shared);
        move || {
            if let Err(refused) = cancel_shared(&shared) {
                eprintln!("worker-death cancel refused: {refused}");
            }
        }
    }
    /// A failed run that left no process and no staged copy (see [`crate::reap`]) may release its
    /// generation: v0.1.4 ends such a run `failed` and allows the next one. Only a failure uses it.
    pub fn record_reap(&self, proof: crate::reap::ReapProof) -> Result<(), PublicError> {
        let mut s = self
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        s.reap = Some(proof);
        Ok(())
    }
    pub fn record_lifecycle(&self, state: &node::State) -> Result<(), PublicError> {
        let mut shared = self
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        shared.lifecycle = Some(
            json!({"epoch":state.epoch,"run":state.run,"phase":format!("{:?}",state.phase),"members":state.members.len(),"reserved_workers":state.workers.len()}),
        );
        Ok(())
    }
    /// The session's terminal, if a real quorum settled it.
    pub fn terminal(&self) -> Option<Terminal> {
        self.shared
            .state
            .lock()
            .ok()
            .and_then(|s| s.session.as_ref().and_then(Session::terminal))
    }
    pub fn cancelled(&self) -> bool {
        self.shared
            .state
            .lock()
            .map(|s| s.cancelled)
            .unwrap_or(true)
            || Instant::now() >= self.deadline
    }
    pub fn install(&self, mut session: Session) -> Result<(), PublicError> {
        let mut s = self
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        if s.cancelled {
            return Err(error("request_cancelled"));
        }
        if s.session.is_some() {
            return Err(error("session_already_owned"));
        }
        session.start(|| Ok(())).map_err(|e| error(e.0))?;
        s.session = Some(session);
        Ok(())
    }
    /// Receipt signatures/order/generation are checked by the actual L9 Session;
    /// a native result cannot inject tokens through this method.
    pub fn receipt(
        &self,
        envelope: Value,
        now: f64,
        seal: impl FnOnce(&[u8]) -> Result<(), ContractError>,
    ) -> Result<Vec<u32>, PublicError> {
        // The seal is caller I/O: take the session out and release the request lock so a
        // slow or hung seal can never starve status, admission, cancel or shutdown.
        let mut session = {
            let mut s = self
                .shared
                .state
                .lock()
                .map_err(|_| error("request_poisoned"))?;
            if s.cancelled {
                return Err(error("request_cancelled"));
            }
            s.session.take().ok_or(error("session_missing"))?
        };
        let outcome = session
            .pull(|| Ok(envelope), || (now, true), seal)
            .map_err(|e| error(e.0))
            .and_then(|_| {
                session
                    .poll(|| (now, true))
                    .map_err(|e| error(e.0))?
                    .ok_or(error("receipt_missing"))
            });
        let mut s = self
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        // A cancel that arrived while the session was out only set the flag; apply it now.
        let cancel = if s.cancelled && session.terminal().is_none() {
            session.cancel(|| Ok(true)).map_err(|e| error(e.0))
        } else {
            Ok(())
        };
        s.session = Some(session);
        cancel?;
        let ids = outcome?.token_ids().to_vec();
        s.verified_tokens = s
            .verified_tokens
            .checked_add(ids.len())
            .ok_or(error("token_count_overflow"))?;
        Ok(ids)
    }
    pub fn token(&mut self, index: usize, text: String) -> Result<(), PublicError> {
        {
            let mut state = self
                .shared
                .state
                .lock()
                .map_err(|_| error("request_poisoned"))?;
            if index != state.emitted_tokens || index >= state.verified_tokens {
                return Err(error("token_not_verified"));
            }
            state.emitted_tokens = state
                .emitted_tokens
                .checked_add(1)
                .ok_or(error("token_count_overflow"))?;
        }
        self.publish(json!({"protocol":"mycelium.request_event.v1","request_id":self.id,"publisher_generation":self.generation,"sequence":self.sequence,"type":"token","token_index":index,"text":text}),false)
    }
    fn publish(&mut self, event: Value, terminal: bool) -> Result<(), PublicError> {
        protocol::request::event(&event).map_err(|e| error(e.0))?;
        let mut s = self
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        while s.slot.is_some() && terminal && !s.disconnected {
            if Instant::now() >= self.deadline {
                return Err(error("subscriber_deadline"));
            }
            let (next, _) = self
                .shared
                .changed
                .wait_timeout(s, duration(BudgetId::RegistryPoll))
                .map_err(|_| error("request_poisoned"))?;
            s = next;
        }
        if s.disconnected {
            return if terminal {
                Ok(())
            } else {
                Err(error("request_cancelled"))
            };
        }
        if s.slot.is_some() {
            return Err(error("unacknowledged_event"));
        }
        if s.cancelled && !terminal {
            return Err(error("request_cancelled"));
        }
        let sequence = self.sequence;
        self.sequence = self
            .sequence
            .checked_add(1)
            .ok_or(error("sequence_overflow"))?;
        s.slot = Some(event);
        s.delivered = false;
        crate::phase::mark("page_ack_wait", &format!("sequence={sequence}"));
        self.shared.changed.notify_all();
        while s.acked != Some(sequence) {
            if (!terminal && s.cancelled) || Instant::now() >= self.deadline {
                return Err(error("request_cancelled"));
            }
            let (next, _) = self
                .shared
                .changed
                .wait_timeout(
                    s,
                    duration(BudgetId::PyMyceliumDeadlinesGatewayProducerStallPollSecondsL244),
                )
                .map_err(|_| error("request_poisoned"))?;
            s = next;
        }
        Ok(())
    }
    /// Only a real quorum plus local residue proof can produce a terminal.
    pub fn settle(
        &mut self,
        proof: CleanupProof,
        outcome: Terminal,
        now: budgets::MonoInstant,
    ) -> Result<(), PublicError> {
        self.settle_with(proof, outcome, "native_failed", now)
    }
    /// A failed run settled by a real signed quorum (RU-020: the member agent survives its dead
    /// native worker and signs). The terminal event carries the run's own error code.
    pub fn settle_failed(
        &mut self,
        proof: CleanupProof,
        code: &'static str,
        now: budgets::MonoInstant,
    ) -> Result<(), PublicError> {
        self.settle_with(proof, Terminal::Failed, code, now)
    }
    fn settle_with(
        &mut self,
        proof: CleanupProof,
        outcome: Terminal,
        code: &'static str,
        now: budgets::MonoInstant,
    ) -> Result<(), PublicError> {
        {
            let mut s = self
                .shared
                .state
                .lock()
                .map_err(|_| error("request_poisoned"))?;
            let session = s.session.as_mut().ok_or(error("session_missing"))?;
            if outcome == Terminal::Completed {
                session.native_result(Ok(())).map_err(|e| error(e.0))?;
            }
            session
                .settle(proof, outcome, || (now, true))
                .map_err(|e| error(e.0))?;
            // Preserve an already-written token until L10 acknowledges its flush.
            // A disconnected stream clears its slot and suppresses further output.
        }
        let kind = match outcome {
            Terminal::Completed => "completed",
            Terminal::Cancelled => "cancelled",
            Terminal::Failed => "failed",
        };
        let mut event = json!({"protocol":"mycelium.request_event.v1","request_id":self.id,"publisher_generation":self.generation,"sequence":self.sequence,"type":kind});
        if outcome == Terminal::Failed {
            event["code"] = json!(code);
        }
        self.publish(event, true)
    }
}
pub trait Driver: Send + Sync + 'static {
    fn run(&self, job: &mut Job) -> Result<(), PublicError>;
}
fn status_document(
    generation: u64,
    running: bool,
    failure: Option<&str>,
    lifecycle: Option<Value>,
) -> api::schema::Snapshot {
    let mut value: Map<String, Value> = api::schema::STATUS_FIELDS
        .iter()
        .map(|k| ((*k).into(), Value::Null))
        .collect();
    for (key, v) in [
        ("label", json!("Rust loopback composition")),
        ("role", json!("development_owner")),
        ("installation_role", json!("owner")),
        ("running", json!(running)),
        ("network", json!({"state":"loopback_only"})),
        ("distribution", json!("unsigned development harness")),
        (
            "authority",
            json!({"swarm_created":lifecycle.is_some(),"membership_state":"signed_loopback_join_with_local_standing_consent"}),
        ),
        (
            "owner_run",
            json!({"publisher_generation":generation,"failure":failure,"lifecycle":lifecycle}),
        ),
        (
            "serving",
            json!({"route_ready":false,"state":"development_unqualified"}),
        ),
        ("test_seams_active", json!(true)),
        (
            "test_seams",
            json!({"transport":if cfg!(feature="iroh-loopback") { "Iroh-signed-NodeControl/ArtifactChunk/Router" } else { "loopback-TCP-development" },"l6_coordinator":lifecycle.is_some(),"qualification":"not-certified"}),
        ),
    ] {
        value.insert(key.into(), v);
    }
    api::schema::Snapshot::parse(Value::Object(value)).expect("all frozen schema fields present")
}
struct Active {
    id: String,
    token: String,
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
}
struct Host {
    generation: u64,
    active: Option<Active>,
    closed: bool,
    /// Stops answered so far ([`NodeService::stops`]).
    stops: u64,
}
#[derive(Clone)]
pub struct NodeService {
    host: Arc<Mutex<Host>>,
    driver: Arc<dyn Driver>,
    bearer: Arc<String>,
    qualification: Value,
    publication: Arc<crate::status::Projection>,
}
impl NodeService {
    pub fn published_status(&self) -> Result<Value, PublicError> {
        self.publication.read()
    }
    pub fn new(driver: Arc<dyn Driver>, bearer: String, qualification: Value) -> Self {
        Self {
            host: Arc::new(Mutex::new(Host {
                generation: 1,
                active: None,
                closed: false,
                stops: 0,
            })),
            driver,
            bearer: Arc::new(bearer),
            qualification,
            publication: Arc::new(crate::status::Projection::new(status_document(
                1, false, None, None,
            ))),
        }
    }
    fn publish_snapshot(&self) -> Result<(), PublicError> {
        let h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        let (running, failure, lifecycle) = if let Some(active) = &h.active {
            let s = active
                .shared
                .state
                .lock()
                .map_err(|_| error("request_poisoned"))?;
            (
                !s.done && s.session.as_ref().and_then(Session::terminal).is_none(),
                s.failure.map(|e| e.code),
                s.lifecycle.clone(),
            )
        } else {
            (false, None, None)
        };
        let snapshot = status_document(h.generation, running, failure, lifecycle);
        drop(h);
        self.publication.publish(snapshot)
    }
    /// Fence admissions and signal cancellation without waiting on the GUI thread.
    pub fn request_shutdown(&self) -> Result<(), PublicError> {
        let mut h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        h.closed = true;
        if let Some(active) = &h.active {
            cancel_shared(&active.shared)?;
            let mut s = active
                .shared
                .state
                .lock()
                .map_err(|_| error("request_poisoned"))?;
            s.disconnected = true;
            s.slot = None;
            s.delivered = false;
            active.shared.changed.notify_all();
        }
        Ok(())
    }
    /// Reap only an already-finished driver. Never infer cleanup from cancellation.
    pub fn poll_shutdown(&self) -> Result<bool, PublicError> {
        let mut h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        if !h.closed {
            return Err(error("shutdown_not_requested"));
        }
        if let Some(active) = &h.active {
            if active.thread.as_ref().is_some_and(|t| !t.is_finished()) {
                return Ok(false);
            }
            let s = active
                .shared
                .state
                .lock()
                .map_err(|_| error("request_poisoned"))?;
            // v0.1.4 (`FX-SLICE1-quit-after-failure` q1/q2, executed): Quit stops only an ACTIVE run
            // (`owner_run.py:48`); a FAILED run is left alone and the launcher exits 0 whatever its
            // `cleanup_unproven` says (`demo_app.py:199-209`, `app.py:1815`). Only a driver that
            // ended neither failed nor settled has no v0.1.4 counterpart and stays unverified
            // (V03-SLICE-1-1: v0.1.4's run is `stopped` or `failed`, never finished-unsettled).
            if s.failure.is_none()
                && (!s.done || s.session.as_ref().and_then(Session::terminal).is_none())
            {
                return Err(error("cleanup_unproven"));
            }
        }
        if let Some(mut active) = h.active.take()
            && let Some(thread) = active.thread.take()
        {
            thread.join().map_err(|_| error("driver_panicked"))?;
        }
        Ok(true)
    }
    /// Whether the current host generation's failed run holds a cleanup proof (a local reap
    /// proof or a signed quorum). Diagnostic only; false without a failed run.
    pub fn failed_run_cleanup_proven(&self) -> Result<bool, PublicError> {
        let h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        let Some(active) = &h.active else {
            return Ok(false);
        };
        let s = active
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        Ok(s.failed_clean())
    }
    pub fn stop(&self) -> Result<(), PublicError> {
        let shared = {
            let mut host = self.host.lock().map_err(|_| error("host_poisoned"))?;
            // Counted with the run it finds, under one lock: a run whose job registers after
            // this Stop sees it counted ([`Self::stops`]).
            host.stops = host.stops.wrapping_add(1);
            host.active.as_ref().map(|a| a.shared.clone())
        };
        if let Some(shared) = shared {
            cancel_shared(&shared)?;
            let deadline = Instant::now() + duration(BudgetId::StageCleanup);
            let mut s = shared.state.lock().map_err(|_| error("request_poisoned"))?;
            while !s.done && s.session.as_ref().and_then(Session::terminal).is_none() {
                if Instant::now() >= deadline {
                    return Err(error("cleanup_unproven"));
                }
                let (next, _) = shared
                    .changed
                    .wait_timeout(s, duration(BudgetId::RegistryPoll))
                    .map_err(|_| error("request_poisoned"))?;
                s = next;
            }
            // RU-020 / v0.1.4: Stop on a failed run does not raise, whatever the failure was.
            if s.failure.is_some() {
                return Ok(());
            }
            if s.session.as_ref().and_then(Session::terminal).is_none() {
                return Err(error("cleanup_unproven"));
            }
        }
        Ok(())
    }
    pub fn rollover(&self) -> Result<u64, PublicError> {
        let mut h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        if let Some(active) = &h.active {
            let s = active
                .shared
                .state
                .lock()
                .map_err(|_| error("request_poisoned"))?;
            // RU-020 / v0.1.4: after ANY failed run the next Start is allowed (also with an
            // unproven cleanup); a run that did not fail still needs its terminal.
            if !s.done
                || (s.failure.is_none() && s.session.as_ref().and_then(Session::terminal).is_none())
            {
                return Err(error("cleanup_unproven"));
            }
        }
        if let Some(mut active) = h.active.take()
            && let Some(thread) = active.thread.take()
        {
            thread.join().map_err(|_| error("driver_panicked"))?;
        }
        h.generation = h
            .generation
            .checked_add(1)
            .ok_or(error("generation_overflow"))?;
        Ok(h.generation)
    }
    pub fn generation(&self) -> Result<u64, PublicError> {
        Ok(self
            .host
            .lock()
            .map_err(|_| error("host_poisoned"))?
            .generation)
    }
    /// How many Stops ([`Self::stop`]) were answered: a request admitted meanwhile, whose job did
    /// not exist yet when one ran, is cancelled by its admission as that Stop cancels a run.
    pub fn stops(&self) -> Result<u64, PublicError> {
        Ok(self.host.lock().map_err(|_| error("host_poisoned"))?.stops)
    }
    /// Shutdown was requested ([`Self::request_shutdown`]); never cleared.
    pub fn closed(&self) -> Result<bool, PublicError> {
        Ok(self.host.lock().map_err(|_| error("host_poisoned"))?.closed)
    }
    /// This generation's run still owns its driver: it is running or stopping. A run whose
    /// driver returned (completed, cancelled or failed) is not active, nor is a fresh generation.
    pub fn run_active(&self) -> Result<bool, PublicError> {
        let h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        let Some(active) = &h.active else {
            return Ok(false);
        };
        let s = active
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        Ok(!s.done)
    }
}
fn cancel_shared(shared: &Shared) -> Result<bool, PublicError> {
    let mut s = shared.state.lock().map_err(|_| error("request_poisoned"))?;
    if s.session.as_ref().and_then(Session::terminal).is_some() {
        return Ok(false);
    }
    if let Some(session) = s.session.as_mut() {
        session.cancel(|| Ok(true)).map_err(|e| error(e.0))?;
    }
    s.cancelled = true;
    shared.changed.notify_all();
    Ok(true)
}
impl Service for NodeService {
    fn authorized(&self, r: &api::http::Request) -> bool {
        let headers: Vec<_> = r.values("authorization").collect();
        headers.len() == 1 && headers[0] == format!("Bearer {}", self.bearer)
    }
    fn health(&self) -> Result<Value, PublicError> {
        Ok(json!({"ok":true,"mode":"slice_loopback_development","generation":self.generation()?}))
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        Ok(self.qualification.clone())
    }
    fn recovery(&self) -> Result<Value, PublicError> {
        Ok(json!({"supported":false}))
    }
    fn submit(&self, document: Value) -> Result<Accepted, PublicError> {
        protocol::request::submission(&document).map_err(|e| error(e.0))?;
        if document["qualification"] != self.qualification {
            return Err(error("qualification_mismatch"));
        }
        let mut h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        if h.closed || h.active.is_some() {
            return Err(error("gateway_capacity_exhausted"));
        }
        let id = random_id()?;
        let token = random_id()?;
        let shared = Arc::new(Shared {
            state: Mutex::new(State {
                session: None,
                slot: None,
                delivered: false,
                acked: None,
                cancelled: false,
                disconnected: false,
                done: false,
                failure: None,
                subscribed: false,
                verified_tokens: 0,
                emitted_tokens: 0,
                lifecycle: None,
                reap: None,
            }),
            changed: Condvar::new(),
        });
        let mut job = Job {
            id: id.clone(),
            generation: h.generation,
            document,
            deadline: Instant::now() + duration(BudgetId::MemberChosenWall),
            shared: shared.clone(),
            sequence: 0,
        };
        let driver = self.driver.clone();
        let done = shared.clone();
        let thread = thread::Builder::new()
            .name("slice-native-owner".into())
            .spawn(move || {
                let result = driver.run(&mut job);
                if let Ok(mut s) = done.state.lock() {
                    s.done = true;
                    s.failure = result.err();
                    done.changed.notify_all();
                }
            })
            .map_err(|_| error("driver_spawn_failed"))?;
        h.active = Some(Active {
            id: id.clone(),
            token: token.clone(),
            shared,
            thread: Some(thread),
        });
        Ok(Accepted {
            request_id: id,
            session_token: token,
        })
    }
    fn cancel(&self, id: &str, owner_token: Option<&str>) -> Result<bool, PublicError> {
        let h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        let a = h
            .active
            .as_ref()
            .filter(|a| a.id == id)
            .ok_or(error("unknown_request"))?;
        if owner_token != Some(a.token.as_str()) {
            return Err(error("invalid_session_token"));
        }
        cancel_shared(&a.shared)
    }
    fn subscribe(
        &self,
        id: &str,
        cursor: Option<(u64, u64)>,
        owner_token: Option<&str>,
    ) -> Result<Box<dyn Subscription>, PublicError> {
        if cursor.is_some() {
            return Err(error("replay_not_available"));
        }
        let h = self.host.lock().map_err(|_| error("host_poisoned"))?;
        let a = h
            .active
            .as_ref()
            .filter(|a| a.id == id)
            .ok_or(error("unknown_request"))?;
        if owner_token != Some(a.token.as_str()) {
            return Err(error("invalid_session_token"));
        }
        let mut s = a
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        if s.subscribed {
            return Err(error("subscriber_already_owned"));
        }
        s.subscribed = true;
        Ok(Box::new(Stream {
            shared: a.shared.clone(),
            deadline: Instant::now() + duration(BudgetId::MemberChosenWall),
        }))
    }
    fn validate_event(&self, event: &Value) -> Result<(), PublicError> {
        protocol::request::event(event).map_err(|e| error(e.0))
    }
}
struct Stream {
    shared: Arc<Shared>,
    deadline: Instant,
}
impl Subscription for Stream {
    fn next(&mut self) -> Result<Option<Value>, PublicError> {
        let mut s = self
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        loop {
            if !s.delivered
                && let Some(event) = s.slot.clone()
            {
                s.delivered = true;
                return Ok(Some(event));
            }
            if let Some(e) = s.failure {
                return Err(e);
            }
            if s.done {
                return Ok(None);
            }
            if Instant::now() >= self.deadline {
                return Err(error("subscriber_deadline"));
            }
            let (next, _) = self
                .shared
                .changed
                .wait_timeout(
                    s,
                    duration(BudgetId::PyMyceliumDeadlinesGatewaySseDisconnectPollSecondsL240),
                )
                .map_err(|_| error("request_poisoned"))?;
            s = next;
        }
    }
    fn ack(&mut self, sequence: u64) -> Result<(), PublicError> {
        let mut s = self
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        if !s.delivered || s.slot.as_ref().and_then(|e| e["sequence"].as_u64()) != Some(sequence) {
            return Err(error("ack_out_of_order"));
        }
        s.acked = Some(sequence);
        crate::phase::mark("page_ack", &format!("sequence={sequence}"));
        s.slot = None;
        s.delivered = false;
        self.shared.changed.notify_all();
        Ok(())
    }
    fn close(&mut self) -> Result<(), PublicError> {
        cancel_shared(&self.shared)?;
        let mut s = self
            .shared
            .state
            .lock()
            .map_err(|_| error("request_poisoned"))?;
        s.disconnected = true;
        s.slot = None;
        s.delivered = false;
        self.shared.changed.notify_all();
        Ok(())
    }
}
impl Backend for NodeService {
    fn prepare(&mut self, action: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        if self.host.lock().map_err(|_| error("host_poisoned"))?.closed {
            return Err(error("application_closed"));
        }
        if !matches!(
            action,
            Action::ServingStop | Action::DemoStop | Action::DemoRebuild | Action::ServingStatus
        ) {
            return Err(error("slice_action_unsupported"));
        }
        Ok(())
    }
    fn execute(&mut self, action: Action, _: &Map<String, Value>) -> Result<Value, PublicError> {
        match action {
            Action::ServingStop | Action::DemoStop => {
                self.stop()?;
                Ok(json!({"stopped":true}))
            }
            Action::DemoRebuild => Ok(json!({"generation":self.rollover()?})),
            Action::ServingStatus => self.health(),
            _ => Err(error("slice_action_unsupported")),
        }
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.publish_snapshot()
    }
}
