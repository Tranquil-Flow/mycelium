// SPDX-License-Identifier: AGPL-3.0-only
//! S1 serving root R-C. The packaged request service is the oracle-checked
//! [`api::gateway::Gateway`] (v0.1.4 `RequestGatewayService`, service.py:277-1453 @ f4e366e9,
//! checked by `api::gateway_host`) with [`RouterPort`] as its backend over the node service's
//! driver job (`RouterSessionBackend`, backend.py:186-375, 703-761): `run` starts the job,
//! relays its tokens and returns the job's own terminal once the driver settled and its run
//! returned; `cancel` hands the request's cancellation to the driver once. The gateway captures
//! the qualification on every submit and revalidates it at run start, per token and before
//! `completed` (qualification.py:30-91). [`NodeService`] stays the one-run-per-generation driver
//! host behind the port; the chat edge (plan, rate, policy answer, busy) stays in front.
//!
//! [`Admission`] is the slot where `M16RuntimeCoordinator.admit / next_dispatch / complete /
//! cancel` (mycelium_live/router_port.py:742-830, 936-955, 1123-1210) will sit. Until
//! `l1b-m16-registry-runtime` lands, [`AdmitAsToday`] is its only implementation and admits
//! every request the gateway admitted: no new policy.
use crate::service::{NodeService, random_id};
use api::{
    actions::{Action, Backend as Actions},
    gateway::{
        Backend, Binding, Config, Gateway, IdSource, Outcome, Qualification, QualificationSource,
        Refusal, Submission, Unavailable,
    },
    registry::{AdmissionError, Pull, Subscription},
    requests::{Accepted, Service as _, Subscription as NodeStream},
    schema::PublicError,
};
use budgets::BudgetId;
use serde_json::{Map, Value, json};
use std::{
    collections::HashMap,
    sync::{
        Arc, Condvar, Mutex, MutexGuard,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

fn conflict(code: &'static str) -> PublicError {
    PublicError::new(409, code)
}
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}
fn span(id: BudgetId) -> Duration {
    crate::service::duration(id)
}
/// The re-check spacing of the bounded waits below (the gateway producer's stall poll).
fn poll() -> Duration {
    span(BudgetId::PyMyceliumDeadlinesGatewayProducerStallPollSecondsL244)
}
/// Wait until the driver's run returned (its generation released), reading `run_active`:
/// `Ok(true)`, or `Ok(false)` once `deadline` passed first (`None`: no deadline). A driver state
/// that cannot be read is never a return: the run fails `backend_failed`, the code the oracle
/// gateway publishes for a raising backend without a retained code (service.py:851-860,
/// `_backend_failure_code` :1353-1364 @ f4e366e9; backend.py:624-648 retains none for it).
fn await_driver(
    run_active: &dyn Fn() -> Result<bool, PublicError>,
    deadline: Option<Instant>,
) -> Result<bool, Refusal> {
    loop {
        match run_active() {
            Ok(false) => return Ok(true),
            Ok(true) if deadline.is_some_and(|d| Instant::now() >= d) => return Ok(false),
            Ok(true) => std::thread::sleep(poll()),
            Err(e) => {
                eprintln!("router port: driver state unreadable: {}", e.code);
                return Err(Refusal("backend_failed"));
            }
        }
    }
}

/// `QualificationBinding` read from the node service's qualification document.
pub fn binding(document: &Value) -> Result<Binding, &'static str> {
    let text = |key: &str| {
        document[key]
            .as_str()
            .map(str::to_owned)
            .ok_or("qualification_document_invalid")
    };
    let number = |key: &str| {
        document[key]
            .as_u64()
            .ok_or("qualification_document_invalid")
    };
    Ok(Binding {
        deployment_id: text("deployment_id")?,
        deployment_epoch: number("deployment_epoch")?,
        topology_version: number("topology_version")?,
        path_manifest_digest: text("path_manifest_digest")?,
        qualification_id: text("qualification_id")?,
        model_id: text("model_id")?,
        resolved_commit: text("resolved_commit")?,
        manifest_digest: text("manifest_digest")?,
        qualification_digest: text("qualification_digest")?,
        stage_load_proof_digests: document["stage_load_proof_digests"]
            .as_array()
            .ok_or("qualification_document_invalid")?
            .iter()
            .map(|d| d.as_str().map(str::to_owned))
            .collect::<Option<Vec<_>>>()
            .ok_or("qualification_document_invalid")?,
    })
}
/// The qualification document a native job carries for `binding`.
pub fn document(binding: &Binding) -> Value {
    json!({
        "deployment_id": binding.deployment_id,
        "deployment_epoch": binding.deployment_epoch,
        "topology_version": binding.topology_version,
        "path_manifest_digest": binding.path_manifest_digest,
        "qualification_id": binding.qualification_id,
        "model_id": binding.model_id,
        "resolved_commit": binding.resolved_commit,
        "manifest_digest": binding.manifest_digest,
        "qualification_digest": binding.qualification_digest,
        "stage_load_proof_digests": binding.stage_load_proof_digests,
    })
}

/// The packaged host's qualification source: the node service's own development record.
pub struct DevelopmentRecord(Qualification);
impl DevelopmentRecord {
    /// The record `NodeService::submit` admits today (equality with its development
    /// qualification), so the gateway admits exactly what the node service admits. `route_ready`
    /// is the gateway's admission readiness of that record (`_read_current`); it is not a
    /// route-ready claim: the published status keeps `serving.route_ready: false`
    /// (`development_unqualified`).
    pub fn of(node: &NodeService) -> Result<Self, PublicError> {
        let record = node.qualification()?;
        Ok(Self(Qualification {
            binding: binding(&record).map_err(conflict)?,
            route_ready: true,
        }))
    }
}
impl QualificationSource for DevelopmentRecord {
    fn current(&self, _request_id: Option<&str>) -> Result<Option<Qualification>, Unavailable> {
        Ok(Some(self.0.clone()))
    }
}

/// The M16 admission slot (see the module docs).
pub trait Admission: Send + Sync {
    /// `coordinator.admit(request)` (router_port.py:776): a closed refusal ends the request.
    fn admit(&self, request_id: &str) -> Result<(), Refusal>;
    /// `coordinator.next_dispatch()` (router_port.py:944): the admitted request starts now.
    fn dispatch(&self, request_id: &str) -> Result<(), Refusal>;
    /// `coordinator.complete(request_id, state=...)` (router_port.py:1123-1210).
    fn complete(&self, request_id: &str, outcome: Result<Outcome, Refusal>);
    /// `coordinator.cancel(request_id)` (router_port.py:794-829).
    fn cancel(&self, request_id: &str);
    /// `recovery_public_projection` (router_port.py:2347-2420): the coordinator's request facts as
    /// `mycelium.full_context_recovery_runtime.v1`. Default: no M16 coordinator, v0.1.4's
    /// `recovery_projection_unavailable` (router_port.py:2353-2356). Not ported with a coordinator yet.
    fn recovery_projection(&self) -> Result<Value, &'static str> {
        Err("recovery_projection_unavailable")
    }
}
/// Admits every request the gateway admitted, as the node service's host does today.
pub struct AdmitAsToday;
impl Admission for AdmitAsToday {
    fn admit(&self, _: &str) -> Result<(), Refusal> {
        Ok(())
    }
    fn dispatch(&self, _: &str) -> Result<(), Refusal> {
        Ok(())
    }
    fn complete(&self, _: &str, _: Result<Outcome, Refusal>) {}
    fn cancel(&self, _: &str) {}
}

/// How many cancellations the router port handed to the driver (observation only).
#[derive(Clone, Default)]
pub struct Ledger {
    cancels: Arc<AtomicUsize>,
}
impl Ledger {
    pub fn driver_cancels(&self) -> usize {
        self.cancels.load(Ordering::SeqCst)
    }
}

enum Phase {
    /// Admitted with its job document; the worker has not taken it.
    Bound(Value),
    /// The worker took it; no driver job yet.
    Taken,
    /// The node service accepted its driver job.
    Started(Accepted),
    /// The backend run returned.
    Ended,
}
struct Run {
    phase: Phase,
    /// The node service accepted a driver job for it.
    reached: bool,
    /// The node service refused to start its job (the send answers this code, as today).
    refused: Option<&'static str>,
    /// Sticky until the job exists (backend.py:735-741).
    cancel_wanted: bool,
    cancel_dispatched: bool,
}
#[derive(Default)]
struct Runs {
    staged: Option<Value>,
    bound: Option<String>,
    draw_failed: Option<&'static str>,
    runs: HashMap<String, Run>,
}
/// `RouterSessionBackend` over the node service's driver job.
pub struct RouterPort {
    node: NodeService,
    admission: Arc<dyn Admission>,
    runs: Mutex<Runs>,
    changed: Condvar,
    ledger: Ledger,
}
/// How the send's wait for its driver job ended.
enum Start {
    Reached,
    /// The gateway ended it without a job (run-start revalidation, cancellation before start).
    Unstarted,
    /// Still queued after the worker-startup budget; it may start later.
    Pending,
}
impl RouterPort {
    pub fn new(node: NodeService, admission: Arc<dyn Admission>) -> Self {
        Self {
            node,
            admission,
            runs: Mutex::default(),
            changed: Condvar::new(),
            ledger: Ledger::default(),
        }
    }
    /// The gateway's request-id source: draws the id and binds the staged job document to it
    /// inside the gateway's admission lock, before any worker can take the request.
    fn ids(self: &Arc<Self>) -> IdSource {
        let port = Arc::clone(self);
        Box::new(move || {
            let mut runs = lock(&port.runs);
            let id = match random_id() {
                Ok(id) => id,
                Err(e) => {
                    runs.draw_failed = Some(e.code);
                    // Not a valid request id: the gateway refuses `invalid_request_id`.
                    return String::new();
                }
            };
            if !runs.runs.contains_key(&id)
                && let Some(document) = runs.staged.take()
            {
                runs.runs.insert(
                    id.clone(),
                    Run {
                        phase: Phase::Bound(document),
                        reached: false,
                        refused: None,
                        cancel_wanted: false,
                        cancel_dispatched: false,
                    },
                );
                runs.bound = Some(id.clone());
            }
            id
        })
    }
    fn stage(&self, document: Value) {
        let mut runs = lock(&self.runs);
        runs.staged = Some(document);
        runs.bound = None;
        runs.draw_failed = None;
    }
    /// A refused submit: drop its staged document and whatever this submit bound.
    fn unstage(&self) -> Option<&'static str> {
        let mut runs = lock(&self.runs);
        runs.staged = None;
        if let Some(id) = runs.bound.take() {
            runs.runs.remove(&id);
        }
        runs.draw_failed.take()
    }
    fn set(&self, request_id: &str, change: impl FnOnce(&mut Run)) {
        if let Some(run) = lock(&self.runs).runs.get_mut(request_id) {
            change(run);
        }
        self.changed.notify_all();
    }
    /// Wait (worker-startup budget) until the request reached its driver job, or ended.
    fn await_start(&self, request_id: &str, gateway: &Gateway) -> Result<Start, PublicError> {
        let deadline = Instant::now() + span(BudgetId::WorkerStartup);
        let mut runs = lock(&self.runs);
        loop {
            let Some(run) = runs.runs.get(request_id) else {
                return Ok(Start::Unstarted);
            };
            if let Some(code) = run.refused {
                return Err(conflict(code));
            }
            if run.reached {
                return Ok(Start::Reached);
            }
            if matches!(run.phase, Phase::Ended) {
                return Ok(Start::Unstarted);
            }
            drop(runs);
            if gateway.worker_done(request_id) != Some(false) {
                // Ended without calling the backend; re-read in case it started meanwhile.
                let runs = lock(&self.runs);
                return Ok(match runs.runs.get(request_id) {
                    Some(run) if run.reached => Start::Reached,
                    _ => Start::Unstarted,
                });
            }
            if Instant::now() >= deadline {
                return Ok(Start::Pending);
            }
            runs = self
                .changed
                .wait_timeout(lock(&self.runs), poll())
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .0;
        }
    }
    /// Forget the requests whose gateway worker finished (or that the gateway evicted).
    fn prune(&self, gateway: &Gateway) {
        let ids: Vec<String> = lock(&self.runs).runs.keys().cloned().collect();
        let finished: Vec<String> = ids
            .into_iter()
            .filter(|id| gateway.worker_done(id) != Some(false))
            .collect();
        let mut runs = lock(&self.runs);
        for id in finished {
            runs.runs.remove(&id);
        }
    }
    /// The driver canceller, at most once per request: the node service's cancellation of the
    /// request's job (`RouterSessionBackend._cancel_once` -> `router.cancel`). True unless the
    /// job had already committed another terminal (or is unknown to the node service).
    fn dispatch_cancel(&self, request_id: &str) -> bool {
        let job = {
            let mut runs = lock(&self.runs);
            let Some(run) = runs.runs.get_mut(request_id) else {
                return false;
            };
            run.cancel_wanted = true;
            let Phase::Started(job) = &run.phase else {
                return !matches!(run.phase, Phase::Ended);
            };
            if run.cancel_dispatched {
                return true;
            }
            run.cancel_dispatched = true;
            (job.request_id.clone(), job.session_token.clone())
        };
        self.ledger.cancels.fetch_add(1, Ordering::SeqCst);
        match self.node.cancel(&job.0, Some(&job.1)) {
            Ok(accepted) => accepted,
            Err(e) => {
                eprintln!("router port: driver cancel refused: {}", e.code);
                false
            }
        }
    }
    fn drive(
        &self,
        request_id: &str,
        document: Value,
        emit: &mut dyn FnMut(u64, &str) -> Result<(), Refusal>,
        is_cancelled: &dyn Fn() -> bool,
    ) -> Result<Outcome, Refusal> {
        self.admission.admit(request_id)?;
        // backend.py:192-193.
        if is_cancelled() {
            return Ok(Outcome::Cancelled);
        }
        self.admission.dispatch(request_id)?;
        let job = match self.node.submit(document) {
            Ok(job) => job,
            Err(e) => {
                self.set(request_id, |run| run.refused = Some(e.code));
                return Err(Refusal(e.code));
            }
        };
        let stream = self
            .node
            .subscribe(&job.request_id, None, Some(&job.session_token));
        let wanted = {
            let mut runs = lock(&self.runs);
            let wanted = runs.runs.get_mut(request_id).is_some_and(|run| {
                run.phase = Phase::Started(Accepted {
                    request_id: job.request_id.clone(),
                    session_token: job.session_token.clone(),
                });
                run.reached = true;
                run.cancel_wanted
            });
            self.changed.notify_all();
            wanted
        };
        // A cancellation that arrived before the job existed reaches the driver now.
        if wanted || is_cancelled() {
            self.dispatch_cancel(request_id);
        }
        let outcome = match stream {
            Ok(mut stream) => self.relay(request_id, &mut *stream, emit),
            Err(e) => {
                self.dispatch_cancel(request_id);
                Err(Refusal(e.code))
            }
        };
        let run_active = || self.node.run_active();
        match outcome {
            // The job's own terminal ends the run only once the driver's run returned, however
            // long that takes: the gateway publishes it after `backend.run` returned
            // (service.py:803-822), and the router session's run returns only at the router's
            // terminal status, with no wall bound (backend.py:317-331). A Stop meanwhile still
            // reaches the driver once, through `Backend::cancel` (service.py:596-616).
            Ok(outcome) => await_driver(&run_active, None).map(|_| outcome),
            // A failure keeps its own code (the oracle backend cancels and raises at once,
            // backend.py:380-383); its driver gets the stage-cleanup budget to return.
            Err(refusal) => {
                let deadline = Instant::now() + span(BudgetId::StageCleanup);
                if await_driver(&run_active, Some(deadline)) != Ok(true) {
                    eprintln!("router port: driver not returned after {}", refusal.0);
                }
                Err(refusal)
            }
        }
    }
    /// The driver job's events into the gateway, each acknowledged once the gateway took it; the
    /// job's own terminal ends the run.
    fn relay(
        &self,
        request_id: &str,
        stream: &mut dyn NodeStream,
        emit: &mut dyn FnMut(u64, &str) -> Result<(), Refusal>,
    ) -> Result<Outcome, Refusal> {
        // A refusal from the gateway cancels the job; the run still ends at the job's terminal.
        let mut refused: Option<Refusal> = None;
        loop {
            let event = match stream.next() {
                Ok(Some(event)) => event,
                Ok(None) => return Err(refused.unwrap_or(Refusal("request_cleanup_unproven"))),
                Err(e) => {
                    self.dispatch_cancel(request_id);
                    return Err(refused.unwrap_or(Refusal(e.code)));
                }
            };
            let sequence = match (self.node.validate_event(&event), event["sequence"].as_u64()) {
                (Ok(()), Some(sequence)) => sequence,
                _ => return self.abandon(request_id, stream, "invalid_native_event"),
            };
            let kind = event["type"].as_str().unwrap_or_default();
            if kind == "token" {
                if refused.is_none() {
                    let index = event["token_index"].as_u64().unwrap_or(u64::MAX);
                    let text = event["text"].as_str().unwrap_or_default();
                    if let Err(refusal) = emit(index, text) {
                        refused = Some(refusal);
                        self.dispatch_cancel(request_id);
                    }
                }
                if let Err(e) = stream.ack(sequence) {
                    return self.abandon(request_id, stream, e.code);
                }
                continue;
            }
            if let Err(e) = stream.ack(sequence) {
                return self.abandon(request_id, stream, e.code);
            }
            return match kind {
                "completed" => refused.map_or(Ok(Outcome::Completed), Err),
                "cancelled" => refused
                    .filter(|r| r.0 != "request_cancelled")
                    .map_or(Ok(Outcome::Cancelled), Err),
                // The page keeps v0.1.4's failure codes (as the node-service host did).
                _ => Err(refused.unwrap_or(Refusal(match event["code"].as_str() {
                    Some("worker_native_exited") => "worker_native_exited",
                    _ => "slice_native_failed",
                }))),
            };
        }
    }
    /// An event the port cannot relay: cancel the job and release its stream.
    fn abandon(
        &self,
        request_id: &str,
        stream: &mut dyn NodeStream,
        code: &'static str,
    ) -> Result<Outcome, Refusal> {
        self.dispatch_cancel(request_id);
        if let Err(e) = stream.close() {
            eprintln!("router port: driver stream close refused: {}", e.code);
        }
        Err(Refusal(code))
    }
}
impl Backend for RouterPort {
    fn run(
        &self,
        request_id: &str,
        emit: &mut dyn FnMut(u64, &str) -> Result<(), Refusal>,
        is_cancelled: &dyn Fn() -> bool,
    ) -> Result<Outcome, Refusal> {
        let document = {
            let mut runs = lock(&self.runs);
            let run = runs
                .runs
                .get_mut(request_id)
                .ok_or(Refusal("request_state_released"))?;
            match std::mem::replace(&mut run.phase, Phase::Taken) {
                Phase::Bound(document) => document,
                other => {
                    run.phase = other;
                    return Err(Refusal("request_state_released"));
                }
            }
        };
        let outcome = self.drive(request_id, document, emit, is_cancelled);
        self.admission.complete(request_id, outcome);
        self.set(request_id, |run| run.phase = Phase::Ended);
        outcome
    }
    fn cancel(&self, request_id: &str) -> bool {
        self.admission.cancel(request_id);
        self.dispatch_cancel(request_id)
    }
}

/// The gateway's construction parameters from the budget table (service.py:307-310 defaults).
fn config() -> Result<Config, PublicError> {
    let count = |id: BudgetId| {
        id.count()
            .ok()
            .and_then(|c| usize::try_from(c.get()).ok())
            .ok_or(conflict("gateway_budget_unregistered"))
    };
    let pending = count(BudgetId::PyFinalMyceliumRequestGatewayServiceMaxPendingRequestsL310C36)?;
    Ok(Config {
        max_buffered_events: count(
            BudgetId::PyFinalMyceliumRequestGatewayServiceMaxBufferedEventsL307C35,
        )?,
        // service.py:308's default has no budget-table entry; this host holds one live chat at
        // a time, so the pending bound also bounds the sessions it retains.
        max_sessions: pending,
        max_concurrent_requests: count(
            BudgetId::PyFinalMyceliumRequestGatewayServiceMaxConcurrentRequestsL309C39,
        )?,
        max_pending_requests: pending,
        eviction_grace: span(BudgetId::PyMyceliumDeadlinesGatewaySessionEvictionGraceSecondsL254)
            .as_secs_f64(),
    })
}
#[derive(Clone)]
struct Live {
    id: String,
    token: String,
}
/// One admitted packaged chat request.
pub struct Admitted {
    pub request_id: String,
    pub subscription: Subscription,
    /// Its driver job may exist (it reached the node service, or may still start).
    pub may_hold_run: bool,
}
/// The serving root: the gateway, its router port and the qualification source.
pub struct Serving {
    gateway: Gateway,
    port: Arc<RouterPort>,
    source: Arc<dyn QualificationSource>,
    live: Mutex<Option<Live>>,
    admitting: Mutex<()>,
}
impl Serving {
    pub fn new(
        node: NodeService,
        source: Arc<dyn QualificationSource>,
    ) -> Result<Self, PublicError> {
        let port = Arc::new(RouterPort::new(node, Arc::new(AdmitAsToday)));
        let started = Instant::now();
        let gateway = Gateway::new(
            config()?,
            source.clone(),
            port.clone(),
            port.ids(),
            Arc::new(move || started.elapsed().as_secs_f64()),
        )
        .map_err(conflict)?;
        Ok(Self {
            gateway,
            port,
            source,
            live: Mutex::new(None),
            admitting: Mutex::new(()),
        })
    }
    pub fn ledger(&self) -> Ledger {
        self.port.ledger.clone()
    }
    /// The router port's recovery projection (the request gateway's `recovery_public_projection`,
    /// service.py:386-400 -> backend.py:582-596 -> [`Admission::recovery_projection`]).
    pub fn recovery_projection(&self) -> Result<Value, &'static str> {
        self.port.admission.recovery_projection()
    }
    /// The qualification source's current record as `mycelium.route_qualification.v1`
    /// ([`QualificationSource::record`]), for the product application's qualification source.
    pub fn qualification_record(&self) -> Result<Option<Value>, Unavailable> {
        self.source.record()
    }
    /// The packaged send after the chat edge: the chat client reads the current record and
    /// submits its binding (chat.py:111-124), the gateway captures and admits (service.py:440-507).
    /// Returns once the request reached its driver job (or ended without one), so the run is the
    /// node service's active run when the page gets its id, as before.
    pub fn submit(&self, prompt: &str, max_new_tokens: u64) -> Result<Admitted, PublicError> {
        let _one = lock(&self.admitting);
        self.port.prune(&self.gateway);
        let binding = match self.source.current(None) {
            Ok(Some(current)) if current.route_ready => current.binding,
            _ => return Err(PublicError::new(503, "chat_route_not_qualified")),
        };
        let job = json!({"protocol":"mycelium.request_gateway.v1","prompt":prompt,"max_new_tokens":max_new_tokens,"qualification":document(&binding)});
        protocol::request::submission(&job).map_err(|e| PublicError::new(400, e.0))?;
        let token = random_id()?;
        self.port.stage(job);
        let request_id = match self.gateway.submit(
            &Submission {
                binding,
                max_new_tokens,
            },
            Some(&token),
        ) {
            Ok(id) => id,
            Err(AdmissionError(code)) => {
                let draw = self.port.unstage();
                return Err(conflict(draw.unwrap_or(code)));
            }
        };
        let mut subscription = self
            .gateway
            .subscribe(&request_id, None, Some(&token))
            .map_err(|AdmissionError(code)| conflict(code))?;
        *lock(&self.live) = Some(Live {
            id: request_id.clone(),
            token,
        });
        let may_hold_run = match self.port.await_start(&request_id, &self.gateway) {
            Ok(start) => !matches!(start, Start::Unstarted),
            Err(refused) => {
                self.close(&mut subscription);
                return Err(refused);
            }
        };
        Ok(Admitted {
            request_id,
            subscription,
            may_hold_run,
        })
    }
    /// The next event, waiting up to `wait`.
    pub fn next(&self, sub: &mut Subscription, wait: Duration) -> Result<Pull, AdmissionError> {
        self.gateway.next_event(sub, wait)
    }
    pub fn ack(&self, sub: &mut Subscription, sequence: i64) -> Result<(), AdmissionError> {
        self.gateway.ack(sub, sequence)
    }
    /// Release a page subscription (closing a stream never cancels inference).
    pub fn close(&self, sub: &mut Subscription) {
        if let Err(AdmissionError(code)) = self.gateway.close_subscription(sub) {
            eprintln!("serving: subscription close refused: {code}");
        }
    }
    /// Cancel the live request through the gateway (which hands it to the driver once).
    fn cancel_live(&self) -> Option<String> {
        let live = lock(&self.live).clone()?;
        if let Err(AdmissionError(code)) = self.gateway.cancel(&live.id, Some(&live.token)) {
            eprintln!("serving: cancel refused: {code}");
        }
        Some(live.id)
    }
    /// Stop: every owner Stop cancels the running chat through the gateway before the run stops
    /// (demo_chain.py:1324-1336, `chat.cancel_all` -> the gateway's cancel), then waits, within
    /// the stage-cleanup budget, for the gateway to publish the request's terminal.
    pub fn stop_live(&self) {
        let Some(id) = self.cancel_live() else {
            return;
        };
        let deadline = Instant::now() + span(BudgetId::StageCleanup);
        while self.gateway.worker_done(&id) == Some(false) && Instant::now() < deadline {
            std::thread::sleep(poll());
        }
    }
    /// Shutdown: cancel the live request without waiting.
    pub fn begin_shutdown(&self) {
        self.cancel_live();
    }
    /// The live request's gateway worker has ended.
    pub fn settled(&self) -> bool {
        lock(&self.live)
            .as_ref()
            .is_none_or(|live| self.gateway.worker_done(&live.id) != Some(false))
    }
}

/// The action delegate behind the packaged action root: the node service, with the packaged
/// Stop (`/api/serving/stop`, the probes' Stop) routed through the serving root first.
/// `/api/demo/stop` stays the packaged host's own (`PackagedHost::demo_action`, app-host lane):
/// it reaches the node service's Stop directly, whose cancellation still ends the gateway
/// request `cancelled` from the driver's own terminal.
#[derive(Clone)]
pub struct ServingActions {
    node: NodeService,
    serving: Result<Arc<Serving>, PublicError>,
}
impl ServingActions {
    pub fn new(node: NodeService, serving: Result<Arc<Serving>, PublicError>) -> Self {
        Self { node, serving }
    }
}
impl Actions for ServingActions {
    fn prepare(&mut self, action: Action, body: &Map<String, Value>) -> Result<(), PublicError> {
        self.node.prepare(action, body)
    }
    fn execute(&mut self, action: Action, body: &Map<String, Value>) -> Result<Value, PublicError> {
        if matches!(action, Action::ServingStop)
            && let Ok(serving) = &self.serving
        {
            serving.stop_live();
        }
        self.node.execute(action, body)
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.node.publish()
    }
}

/// The packaged host over `node` with `source` as its qualification source.
#[cfg(any(feature = "desktop-shell", feature = "packaged"))]
pub fn packaged_host(
    node: NodeService,
    source: Arc<dyn QualificationSource>,
    capacity: std::num::NonZeroUsize,
    private: &std::path::Path,
) -> (crate::desktop_host::DesktopServices, Ledger) {
    let host =
        crate::desktop_host::DesktopServices::with_qualification(node, capacity, private, source);
    let ledger = host.ledger();
    (host, ledger)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    /// A driver state that cannot be read is never the driver's return, with or without a
    /// deadline: the run fails `backend_failed` (service.py:851-860, 1353-1364 @ f4e366e9). The
    /// node service's state is unreadable only behind a poisoned lock, which no public or
    /// crate-visible node-service API can produce, so this reads the state through the seam.
    #[test]
    fn unreadable_driver_state_is_never_a_return() {
        let unreadable = || Err(PublicError::new(409, "request_poisoned"));
        assert_eq!(
            await_driver(&unreadable, None),
            Err(Refusal("backend_failed"))
        );
        assert_eq!(
            await_driver(&unreadable, Some(Instant::now())),
            Err(Refusal("backend_failed"))
        );
    }
    /// Only the driver's return ends the wait; a deadline's expiry is reported as not returned.
    #[test]
    fn only_the_driver_return_ends_the_wait() {
        let reads = Cell::new(0);
        let returns_at_third_read = || {
            reads.set(reads.get() + 1);
            Ok(reads.get() < 3)
        };
        assert_eq!(await_driver(&returns_at_third_read, None), Ok(true));
        assert_eq!(reads.get(), 3);
        assert_eq!(await_driver(&|| Ok(true), Some(Instant::now())), Ok(false));
    }
}
