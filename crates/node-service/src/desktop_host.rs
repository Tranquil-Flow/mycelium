// SPDX-License-Identifier: AGPL-3.0-only
//! Development-only L11 host over the actual native service. No public relay,
//! fixture inference, or browser-asserted consent. Callbacks run off the GUI thread.
//! `/chat/api/send`, its events and Stop go through the serving root
//! ([`crate::router_port::Serving`]: the oracle-checked request gateway over the node service).
mod chat_boundary;
use crate::{
    app_host::{FastModeDefault, FastModeProcess, PackagedHost},
    router_port::{self, Serving, ServingActions},
    service::NodeService,
};
use api::{
    actions::Control,
    gateway::QualificationSource,
    persistent::PersistentBackend,
    registry::{AdmissionError, Pull, Subscription},
    requests::Service as _,
    schema::PublicError,
};
use desktop::{
    core::{Completion, Reply},
    lifecycle::{CleanupError, Poll},
    services::{ServiceRequest, Services},
};
use serde_json::{Value, json};
use std::{
    num::NonZeroUsize,
    path::Path,
    sync::{Arc, Mutex},
    thread::JoinHandle,
};

#[cfg(feature = "desktop-shell")]
pub fn run(
    checkpoint: &std::path::Path,
    tokenizer: &std::path::Path,
    evidence: &std::path::Path,
    private: &std::path::Path,
    capacity: NonZeroUsize,
) -> crate::loopback::Result<()> {
    run_host(checkpoint, tokenizer, evidence, private, capacity, false)
}
#[cfg(all(feature = "desktop-shell", debug_assertions))]
pub fn run_probe(
    checkpoint: &std::path::Path,
    tokenizer: &std::path::Path,
    evidence: &std::path::Path,
    private: &std::path::Path,
    capacity: NonZeroUsize,
) -> crate::loopback::Result<()> {
    run_host(checkpoint, tokenizer, evidence, private, capacity, true)
}
/// The composed adapter pair the window needs: the real node service behind the bounded
/// `desktop::services` queue, and the cleanup that proves its owned resources are gone.
pub type Composed = (
    Arc<desktop::services::ServiceCore<DesktopServices>>,
    desktop::services::ServiceCleanup<DesktopServices>,
);
/// Build the native driver (fresh `evidence` directory, persisted `private` state) and the
/// node service over it. Starts nothing until the first request. No window, no refusal of
/// release builds: the packaged executable calls this; the development hosts add their own.
pub fn compose(
    checkpoint: &std::path::Path,
    tokenizer: &std::path::Path,
    evidence: &std::path::Path,
    private: &std::path::Path,
    capacity: NonZeroUsize,
) -> crate::loopback::Result<Composed> {
    std::fs::create_dir(evidence)?;
    let driver = crate::native::configured_driver(checkpoint, tokenizer, evidence, private)?;
    let node = NodeService::new(
        driver,
        crate::service::random_id().map_err(|e| e.code)?,
        crate::native::development_qualification()?,
    );
    let services = Arc::new(DesktopServices::new(node, capacity, private));
    Ok(desktop::services::adapt(services, capacity))
}
#[cfg(feature = "desktop-shell")]
fn run_host(
    checkpoint: &std::path::Path,
    tokenizer: &std::path::Path,
    evidence: &std::path::Path,
    private: &std::path::Path,
    capacity: NonZeroUsize,
    probe: bool,
) -> crate::loopback::Result<()> {
    if !cfg!(debug_assertions) {
        return Err("development_shell_refused_in_release".into());
    }
    let (core, cleanup) = compose(checkpoint, tokenizer, evidence, private, capacity)?;
    #[cfg(debug_assertions)]
    if probe {
        let fixture: Value = serde_json::from_str(include_str!("../data/qwen2-oracle.json"))?;
        // The user's plain question, as typed into v0.1.4's chat; the chat edge wraps it.
        let script = probe_script(
            &serde_json::to_string(crate::chat_template::ORACLE_USER_PROMPT)?,
            &serde_json::to_string(
                &fixture["tokens"]
                    .as_array()
                    .ok_or("oracle_tokens")?
                    .len()
                    .to_string(),
            )?,
            page_receipts_enabled(),
        );
        return desktop::shell::run_development_with_probe(
            core,
            cleanup,
            desktop::lifecycle::quit_allowance().map_err(|e| format!("cleanup budget: {e:?}"))?,
            script,
        )
        .map_err(Into::into);
    }
    #[cfg(not(debug_assertions))]
    if probe {
        return Err("development_probe_refused_in_release".into());
    }
    desktop::shell::run_development(
        core,
        cleanup,
        desktop::lifecycle::quit_allowance().map_err(|e| format!("cleanup budget: {e:?}"))?,
        false,
    )
    .map_err(Into::into)
}
/// Test-owned probe script with its placeholders substituted. `page_receipts` switches only
/// the per-fetch title receipts; the scripted chat flow is identical either way.
#[cfg(debug_assertions)]
pub fn probe_script(prompt_json: &str, tokens_json: &str, page_receipts: bool) -> String {
    include_str!("desktop_host/probe.js")
        .replace("__PROMPT_JSON__", prompt_json)
        .replace("__TOKENS_JSON__", tokens_json)
        .replace("__PAGE_RECEIPTS__", &page_receipts.to_string())
}
/// Page-fetch receipts are on unless the qualifier explicitly sets the variable to `0`.
#[cfg(all(feature = "desktop-shell", debug_assertions))]
fn page_receipts_enabled() -> bool {
    std::env::var("MYCELIUM_SLICE_PAGE_RECEIPTS").as_deref() != Ok("0")
}
fn err(code: &'static str) -> PublicError {
    PublicError::new(503, code)
}
struct Gate {
    closing: bool,
    tasks: Vec<JoinHandle<()>>,
    failed: bool,
}
struct Chat {
    id: String,
    /// The page's gateway subscription. None: the gateway's credential-theft policy answered it
    /// and no request exists.
    sub: Option<Subscription>,
    /// The node service may hold this (or an earlier) request's run: the next send rolls it over.
    holds_run: bool,
    tokens: Vec<String>,
    done: bool,
    /// The gateway terminal the page saw (v0.1.4's `terminal`, chat.py:363-369).
    terminal: Option<&'static str>,
    error: Option<String>,
}
/// The packaged action root: the desktop action backend over the packaged host, with the node
/// service (Stop routed through the serving root first) as the delegate for everything the
/// backend does not own.
type Actions = Control<PersistentBackend<PackagedHost, ServingActions>>;
type Source = Result<Arc<dyn QualificationSource>, PublicError>;
/// The node service's own development record as the gateway's qualification source.
fn development_source(node: &NodeService) -> Source {
    router_port::DevelopmentRecord::of(node).map(|r| Arc::new(r) as Arc<dyn QualificationSource>)
}
/// What the packaged executable's chat waits for: the native service over a located model.
pub trait ModelGate: Send + Sync {
    /// `Ok` once the native driver is composed over the model; else the refusal `/chat/*` answers.
    fn ready(&self) -> Result<(), PublicError>;
}
#[derive(Clone)]
pub struct DesktopServices {
    node: NodeService,
    /// The packaged executable's [`ModelGate`]; none in the development hosts (always composed).
    model: Option<Arc<dyn ModelGate>>,
    /// The request gateway over the node service (its construction failure answers every send).
    serving: Result<Arc<Serving>, PublicError>,
    /// Every `POST /api/*`, in one action transaction at a time (v0.1.4's `app.guard`).
    actions: Arc<Actions>,
    fast_mode: FastModeProcess,
    gate: Arc<Mutex<Gate>>,
    chat: Arc<Mutex<Option<Chat>>>,
    /// v0.1.4's `ChatService._rate` windows (`chat_edge`), on this host's own monotonic clock.
    rate: Arc<Mutex<crate::chat_edge::RateWindow>>,
    started: std::time::Instant,
    capacity: NonZeroUsize,
}
impl DesktopServices {
    /// `private` is the packaged private state root: the preference files, the recorded action
    /// failure and every later persisted action state live there.
    pub fn new(node: NodeService, capacity: NonZeroUsize, private: &Path) -> Self {
        let source = development_source(&node);
        Self::with_host(
            node.clone(),
            capacity,
            private,
            PackagedHost::new(node, private.to_owned()),
            source,
        )
    }
    /// [`DesktopServices::new`] with the gateway's qualification source given (a test scripts it).
    pub fn with_qualification(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        source: Arc<dyn QualificationSource>,
    ) -> Self {
        Self::with_host(
            node.clone(),
            capacity,
            private,
            PackagedHost::new(node, private.to_owned()),
            Ok(source),
        )
    }
    /// [`DesktopServices::new`] with the owner's lease renewal period read from `period`
    /// (`PackagedHost::with_renewal_period`): a test runs the same composition on a shorter budget.
    pub fn with_renewal_period(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        period: budgets::BudgetId,
    ) -> Self {
        let host = PackagedHost::with_renewal_period(node.clone(), private.to_owned(), period);
        let source = development_source(&node);
        Self::with_host(node, capacity, private, host, source)
    }
    /// [`DesktopServices::new`] with the packaged host's run-model acquisition over `source`
    /// (`PackagedHost::with_model_source`): `/api/model/download` and `/api/model/cancel`.
    pub fn with_model_source(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        source: crate::app_host::ModelSource,
    ) -> Self {
        let host = PackagedHost::with_model_source(node.clone(), private.to_owned(), source);
        let qualification = development_source(&node);
        Self::with_host(node, capacity, private, host, qualification)
    }
    /// Every `/chat/*` request first asks `gate` (on its request thread, never the window's):
    /// the packaged executable's chat waits for its native service over a prepared model.
    pub fn gated(mut self, gate: Arc<dyn ModelGate>) -> Self {
        self.model = Some(gate);
        self
    }
    fn with_host(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        host: PackagedHost,
        source: Source,
    ) -> Self {
        let serving = source
            .and_then(|source| Serving::new(node.clone(), source))
            .map(Arc::new);
        let fast_mode = host.fast_mode_process();
        Self {
            actions: Arc::new(Control::new(PersistentBackend::new(
                private.to_owned(),
                host,
                ServingActions::new(node.clone(), serving.clone()),
            ))),
            serving,
            fast_mode,
            node,
            model: None,
            gate: Arc::new(Mutex::new(Gate {
                closing: false,
                tasks: Vec::new(),
                failed: false,
            })),
            chat: Arc::new(Mutex::new(None)),
            rate: Arc::default(),
            started: std::time::Instant::now(),
            capacity,
        }
    }
    /// What the serving root's router port handed to the driver (observation only).
    pub fn ledger(&self) -> router_port::Ledger {
        self.serving
            .as_ref()
            .map(|s| s.ledger())
            .unwrap_or_default()
    }
    /// The next run's fast-mode default as the packaged host last refreshed it.
    pub fn fast_mode_default(&self) -> FastModeDefault {
        self.fast_mode.read()
    }
    fn json(value: Value) -> Result<Reply, PublicError> {
        Ok(Reply {
            status: 200,
            content_type: "application/json",
            body: serde_json::to_vec(&value).map_err(|_| err("native_json_failed"))?,
        })
    }
    fn deliver(complete: Completion, result: Result<Reply, PublicError>) {
        complete(result.unwrap_or_else(|e| Reply::error(e.status, e.code)));
    }
    fn complete_error(complete: Completion, error: PublicError) {
        Self::deliver(complete, Err(error));
    }
    fn work(&self, request: ServiceRequest, complete: Completion) {
        crate::phase::mark(
            "page_request",
            &format!("{}:{}", request.method, request.target),
        );
        if request.target.starts_with("/chat/") {
            if let Some(Err(refusal)) = self.model.as_ref().map(|gate| gate.ready()) {
                Self::complete_error(complete, refusal);
                return;
            }
            chat_boundary::handle(self, request, complete);
            return;
        }
        Self::deliver(complete, self.dispatch(request));
    }
    fn dispatch(&self, request: ServiceRequest) -> Result<Reply, PublicError> {
        let body: Value = if request.body.is_empty() {
            json!({})
        } else {
            serde_json::from_slice(&request.body)
                .map_err(|_| PublicError::new(400, "invalid_json"))?
        };
        match (request.method.as_str(), request.target.as_str()) {
            ("GET", "/api/status") => Self::json(self.node.published_status()?),
            ("POST", "/chat/api/state") if body == json!({}) => {
                let closed = self
                    .gate
                    .lock()
                    .map_err(|_| err("desktop_poisoned"))?
                    .closing;
                let busy = self
                    .chat
                    .try_lock()
                    .map(|c| c.as_ref().is_some_and(|c| !c.done))
                    .unwrap_or(true);
                Self::json(
                    json!({"available":!closed,"busy":busy,"model_id":"Qwen2.5-0.5B-Instruct","served_by":[],"switching_runs":false,"rejoining":false,"warming_up":false,"limits":crate::chat_edge::limits_json()}),
                )
            }
            ("POST", "/chat/api/send") => {
                // v0.1.4's `ChatService.send` decides body, prompt and token limits first.
                let plan = crate::chat_edge::plan(&body)?;
                let tokens = plan.max_new_tokens;
                let policy = crate::chat_edge::policy_response(&plan.prompt);
                // Use the native request contract's validation, not an alternate
                // browser-only generation policy.
                let prompt = crate::chat_template::qwen2_chat_prompt(&plan.prompt);
                let document = json!({"protocol":"mycelium.request_gateway.v1","prompt":prompt,"max_new_tokens":tokens,"qualification":self.node.qualification()?});
                protocol::request::submission(&document).map_err(|e| PublicError::new(400, e.0))?;
                let gate = self.gate.lock().map_err(|_| err("desktop_poisoned"))?;
                if gate.closing {
                    return Err(err("application_closed"));
                }
                let mut chat = self
                    .chat
                    .try_lock()
                    .map_err(|_| PublicError::new(429, "chat_busy"))?;
                if chat.as_ref().is_some_and(|c| !c.done) {
                    return Err(PublicError::new(429, "chat_busy"));
                }
                // The owner's own page is the one local client (`chat.client_key`).
                self.rate
                    .lock()
                    .map_err(|_| err("desktop_poisoned"))?
                    .admit("local", self.started.elapsed().as_secs_f64())?;
                let reply = |request_id: &str| {
                    Self::json(
                        json!({"request_id":request_id,"max_new_tokens":tokens,"clamped":plan.clamped,"served_by":[]}),
                    )
                };
                if let Some(refusal) = policy {
                    // The gateway emits token 0 = the refusal and completes, before any model work.
                    let id = crate::service::random_id()?;
                    let holds_run = chat.as_mut().is_some_and(|previous| {
                        self.release(previous);
                        previous.holds_run
                    });
                    *chat = Some(Chat {
                        id: id.clone(),
                        sub: None,
                        holds_run,
                        tokens: vec![refusal.to_owned()],
                        done: true,
                        terminal: Some("completed"),
                        error: None,
                    });
                    return reply(&id);
                }
                let serving = self.serving.as_ref().map_err(|e| *e)?;
                if let Some(previous) = chat.as_mut() {
                    if previous.holds_run {
                        self.node.rollover()?;
                        previous.holds_run = false;
                    }
                    self.release(previous);
                }
                let admitted = serving.submit(&prompt, tokens)?;
                *chat = Some(Chat {
                    id: admitted.request_id.clone(),
                    sub: Some(admitted.subscription),
                    holds_run: admitted.may_hold_run,
                    tokens: Vec::new(),
                    done: false,
                    terminal: None,
                    error: None,
                });
                reply(&admitted.request_id)
            }
            ("POST", path) if path.starts_with("/api/") => {
                use api::desktop::Service as _;
                let value = self.actions.action(
                    path,
                    body.as_object()
                        .ok_or(PublicError::new(400, "invalid_request"))?,
                )?;
                Self::json(value)
            }
            _ => Err(PublicError::new(404, "not_found")),
        }
    }
    fn events(&self, request: ServiceRequest, complete: Completion) {
        let body: Value = match serde_json::from_slice(&request.body) {
            Ok(v) => v,
            Err(_) => {
                Self::complete_error(complete, PublicError::new(400, "invalid_json"));
                return;
            }
        };
        if request.method != "POST"
            || body.as_object().is_none_or(|b| b.len() != 2)
            || !body["request_id"].is_string()
            || !body["after"].is_u64()
        {
            Self::complete_error(complete, PublicError::new(400, "invalid_request"));
            return;
        }
        let Ok(mut chat) = self.chat.lock() else {
            Self::complete_error(complete, err("desktop_chat_poisoned"));
            return;
        };
        let Some(chat) = chat
            .as_mut()
            .filter(|c| Some(c.id.as_str()) == body["request_id"].as_str())
        else {
            Self::complete_error(complete, PublicError::new(404, "chat_request_unknown"));
            return;
        };
        let Some(after) = body["after"]
            .as_u64()
            .and_then(|v| usize::try_from(v).ok())
            .filter(|v| *v <= chat.tokens.len())
        else {
            Self::complete_error(complete, PublicError::new(400, "invalid_request"));
            return;
        };
        crate::phase::mark(
            "events_wait",
            &format!(
                "after={after}:tokens={}:done={}",
                chat.tokens.len(),
                chat.done
            ),
        );
        let mut sequence = None;
        if after == chat.tokens.len() && !chat.done {
            sequence = self.pull(chat);
        }
        let reply = Self::json(
            json!({"tokens":chat.tokens[after..],"next":chat.tokens.len(),"done":chat.done,"error":chat.error,"terminal":chat.terminal,"seconds":Value::Null}),
        );
        let delivered = reply.is_ok();
        crate::phase::mark(
            "events_reply",
            &format!("sequence={sequence:?}:delivered={delivered}"),
        );
        // Completion hands the entire frame to the native channel. Keep the
        // subscription lock through ACK so another page cannot race delivery.
        Self::deliver(complete, reply);
        crate::phase::mark("events_delivered", "");
        if delivered
            && let Some(sequence) = sequence
            && let (Ok(serving), Some(sub)) = (&self.serving, chat.sub.as_mut())
            && let Err(AdmissionError(code)) = serving.ack(sub, sequence)
        {
            chat.done = true;
            chat.error = Some(code.to_owned());
        }
    }
    /// The next page-visible gateway event of `chat`, waiting at most one gateway SSE poll step
    /// (v0.1.4's events answer at once with what the request has, chat.py:460-474). `accepted`
    /// is acknowledged here; a token or the terminal is acknowledged after its reply is
    /// delivered (its sequence is returned).
    fn pull(&self, chat: &mut Chat) -> Option<i64> {
        let Some(sub) = chat.sub.as_mut() else {
            // A policy answer carries all of its tokens and is done from the start.
            chat.done = true;
            return None;
        };
        let serving = match &self.serving {
            Ok(serving) => serving,
            Err(e) => {
                chat.done = true;
                chat.error = Some(e.code.to_owned());
                return None;
            }
        };
        let deadline = std::time::Instant::now()
            + crate::service::duration(
                budgets::BudgetId::PyMyceliumDeadlinesGatewaySseDisconnectPollSecondsL240,
            );
        loop {
            let wait = deadline.saturating_duration_since(std::time::Instant::now());
            let event = match serving.next(sub, wait) {
                Ok(Pull::Event(event)) => event,
                Ok(Pull::Pending) => return None,
                Ok(Pull::End) => {
                    chat.done = true;
                    return None;
                }
                Err(AdmissionError(code)) => {
                    chat.done = true;
                    chat.error = Some(code.to_owned());
                    return None;
                }
            };
            let terminal = match event.kind.as_str() {
                "token" => {
                    chat.tokens.push(
                        event.payload["text"]
                            .as_str()
                            .unwrap_or_default()
                            .to_owned(),
                    );
                    return Some(event.sequence);
                }
                "completed" => "completed",
                "cancelled" => "cancelled",
                "failed" => "failed",
                // `accepted` (and any lifecycle event) carries nothing for the page.
                _ => {
                    if let Err(AdmissionError(code)) = serving.ack(sub, event.sequence) {
                        chat.done = true;
                        chat.error = Some(code.to_owned());
                        return None;
                    }
                    continue;
                }
            };
            chat.done = true;
            chat.terminal = Some(terminal);
            if terminal == "failed" {
                // v0.1.4: a failed terminal shows its code (chat.py:366-367).
                chat.error = Some(
                    event.payload["code"]
                        .as_str()
                        .unwrap_or("chat_generation_failed")
                        .to_owned(),
                );
            }
            return Some(event.sequence);
        }
    }
    /// Release a replaced chat's gateway subscription (closing never cancels inference).
    fn release(&self, chat: &mut Chat) {
        if let (Ok(serving), Some(mut sub)) = (&self.serving, chat.sub.take()) {
            serving.close(&mut sub);
        }
    }
}
impl Services for DesktopServices {
    fn submit(&self, request: ServiceRequest, complete: Completion) {
        let Ok(mut gate) = self.gate.lock() else {
            Self::complete_error(complete, err("desktop_poisoned"));
            return;
        };
        if gate.closing {
            Self::complete_error(complete, err("application_closed"));
            return;
        }
        for task in std::mem::take(&mut gate.tasks) {
            if task.is_finished() {
                if task.join().is_err() {
                    gate.failed = true;
                }
            } else {
                gate.tasks.push(task);
            }
        }
        if gate.failed {
            Self::complete_error(complete, err("desktop_callback_panicked"));
            return;
        }
        if gate.tasks.len() >= self.capacity.get() {
            crate::phase::mark("callback_capacity", "refused");
            Self::complete_error(complete, err("desktop_callback_capacity"));
            return;
        }
        let callback = Arc::new(Mutex::new(Some(complete)));
        let owned = callback.clone();
        let this = self.clone();
        match std::thread::Builder::new()
            .name("slice-desktop-request".into())
            .spawn(move || {
                if let Ok(mut callback) = owned.lock()
                    && let Some(callback) = callback.take()
                {
                    this.work(request, callback);
                }
            }) {
            Ok(task) => gate.tasks.push(task),
            Err(_) => {
                if let Ok(mut callback) = callback.lock()
                    && let Some(callback) = callback.take()
                {
                    Self::complete_error(callback, err("desktop_thread_start_failed"));
                }
            }
        }
    }
    fn begin_shutdown(&self, _: std::time::Instant) -> Result<(), CleanupError> {
        self.gate.lock().map_err(|_| CleanupError::Begin)?.closing = true;
        if let Ok(serving) = &self.serving {
            serving.begin_shutdown();
        }
        self.node
            .request_shutdown()
            .map_err(|_| CleanupError::Begin)
    }
    fn poll_shutdown(&self) -> Result<Poll, CleanupError> {
        let mut gate = self.gate.lock().map_err(|_| CleanupError::Poll)?;
        if !gate.closing {
            return Err(CleanupError::Poll);
        }
        if gate.tasks.iter().any(|t| !t.is_finished()) {
            return Ok(Poll::Pending);
        }
        for task in std::mem::take(&mut gate.tasks) {
            if task.join().is_err() {
                gate.failed = true;
            }
        }
        if gate.failed {
            return Err(CleanupError::Poll);
        }
        // The live request's gateway worker ends after its driver job (or its refusal).
        if self.serving.as_ref().is_ok_and(|s| !s.settled()) {
            return Ok(Poll::Pending);
        }
        Ok(
            if self.node.poll_shutdown().map_err(|_| CleanupError::Poll)? {
                Poll::Verified
            } else {
                Poll::Pending
            },
        )
    }
}
