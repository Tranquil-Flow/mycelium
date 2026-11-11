// SPDX-License-Identifier: AGPL-3.0-only
//! The shipped chat service (`mycelium_a13_macos/chat.py:211-500 @ f4e366e9`) over the run's
//! route gateway. Chain: `_Handler.do_POST` app.py:1936-1950 -> `DemoApp.chat_route` app.py:1926
//! -> `chat.handle` chat.py:553 -> `ChatService.send` :300-340 -> `_work` :342-398 ->
//! `RegistryClient.stream` :106-147 (qualification, `POST` inference, SSE events, `DELETE` Stop);
//! `events` :454-474, `state` :476-504, `cancel`/`cancel_all` :418-452; the owner run's teardown
//! (demo_chain.py:1322-1335) cancels every chat through [`ChatTeardown`].
//!
//! v0.2 differences: the run hands the service its route gateway through [`RunRoute`] (resolved
//! once per run, EP-PLAN U7) instead of a registry origin and a client per chat; the caller
//! validates the body (`chat_edge::plan` in node-service, chat.py:303-313); the `rejoining` wait
//! (chat.py:257-283) is not ported (the packaged owner has no sharing fence); a completed chat
//! appends one JSON line `{ttft_ms, decode_millitokens_s, request_ms, run_id}` (the fields of
//! chat.py:392-395, v0.1.4 keeps them in memory only) to the owner's timing file, which is
//! refused under v0.1.4's app folder (rulings 20/25).
use crate::{chat::ChatError, schema::PublicError, sse_client};
use base64::Engine as _;
use budgets::{BudgetId, Entries};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, VecDeque},
    io::Write as _,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, MutexGuard},
    thread::JoinHandle,
    time::{Duration, Instant},
};

/// A prompt the caller already validated as `ChatService.send` does (chat.py:303-313).
#[derive(Clone, Debug)]
pub struct Prompt {
    pub text: String,
    pub max_new_tokens: u64,
    pub clamped: bool,
}
/// How one gateway request ended (`RegistryClient.stream`'s result, chat.py:142-145).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Streamed {
    /// `completed` / `cancelled` / `failed`; `None`: the stream ended without a terminal.
    pub terminal: Option<&'static str>,
    pub code: Option<String>,
}
/// A failure of one gateway request: its bounded public code (`ChatError.code` or
/// `failure_code`, chat.py:184-207).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GatewayError {
    pub code: String,
}
impl GatewayError {
    fn new(code: impl Into<String>) -> Self {
        Self { code: code.into() }
    }
}
type CancelSend = Box<dyn FnOnce() -> Result<bool, GatewayError> + Send>;
#[derive(Default)]
struct CancelState {
    wanted: bool,
    sent: bool,
    send: Option<CancelSend>,
}
/// One request's cancel (`RegistryClient._arm_cancel` / `request_cancel` / `cancel`,
/// chat.py:149-181): a cancel before the gateway accepted the request is remembered and sent
/// on acceptance; the gateway's `DELETE` is sent at most once.
#[derive(Clone, Default)]
pub struct RequestCancel(Arc<Mutex<CancelState>>);
impl RequestCancel {
    fn state(&self) -> MutexGuard<'_, CancelState> {
        self.0.lock().unwrap_or_else(|p| p.into_inner())
    }
    /// The gateway accepted the request: `send` cancels it. A cancel wanted earlier goes now.
    pub fn arm(&self, send: CancelSend) {
        let now = {
            let mut state = self.state();
            if state.sent {
                return;
            }
            if state.wanted {
                state.sent = true;
                Some(send)
            } else {
                state.send = Some(send);
                None
            }
        };
        if let Some(send) = now
            && let Err(e) = send()
        {
            eprintln!("chat: early cancel not delivered: {}", e.code);
        }
    }
    /// The request ended: nothing is left to cancel (`_cancel_path = None`, chat.py:141-142).
    pub fn disarm(&self) {
        self.state().send = None;
    }
    /// Cancel now when the request is accepted and running, else remember it (chat.py:423-440).
    /// `false` only when the gateway could not be told.
    pub fn cancel(&self) -> bool {
        let send = {
            let mut state = self.state();
            match state.send.take() {
                Some(send) if !state.sent => {
                    state.sent = true;
                    send
                }
                _ => {
                    state.wanted = true;
                    return true;
                }
            }
        };
        match send() {
            Ok(_) => true,
            Err(e) => {
                eprintln!("chat: cancel not delivered: {}", e.code);
                false
            }
        }
    }
}
/// The run's route gateway: ONE object per run (EP-PLAN U7), one `stream` call per chat.
pub trait RouteGateway: Send + Sync {
    /// `RegistryClient.stream` (chat.py:106-147): qualification, submit, the event stream until
    /// its terminal. `cancel` is armed once the gateway accepted the request.
    fn stream(
        &self,
        prompt: &str,
        max_new_tokens: u64,
        cancel: &RequestCancel,
        on_token: &mut dyn FnMut(&str),
    ) -> Result<Streamed, GatewayError>;
}
/// The owner run as the chat service sees it (the seam the run's work port fills).
pub trait RunRoute: Send + Sync {
    /// `run.status()` (owner_run.py:188-214): `None` without a run. Read: `run_id`, `state`,
    /// `step`, `error_code`, `reason`, `rollover.state`, `participants[{label,node_id,layers}]`.
    fn status(&self) -> Option<Value>;
    /// The run's route gateway while `run_id` serves (replaces `registry_origin`). Asked once per
    /// run: the service keeps the answer for every chat of that run.
    fn gateway(&self, run_id: &str) -> Option<Arc<dyn RouteGateway>>;
    /// `app.run_model_contract().model_id` (chat.py:507-511).
    fn model_id(&self) -> Option<String>;
}
/// Why the owner run's teardown cancels every chat (demo_chain.py:1322-1335 @ f4e366e9):
/// `cancel_all('chat_switching_runs' if rollover_requested else (monitor_error_code or
/// 'owner_run_failed') if failed else 'chat_cancelled')`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Teardown {
    /// `/api/demo/stop` (owner_run.py:275-282).
    Stopped,
    /// Rollover to a fresh run (owner_run.py:352-402).
    SwitchingRuns,
    /// The execution ceiling (demo_chain.py:1143-1164).
    CeilingReached,
    /// Any other failed run: the monitor's code, else `owner_run_failed`.
    Failed(String),
}
impl Teardown {
    /// The code the page shows (chat.js:37-41; owner_run.EXECUTION_CEILING_CODE).
    pub fn code(&self) -> &str {
        match self {
            Self::Stopped => "chat_cancelled",
            Self::SwitchingRuns => "chat_switching_runs",
            Self::CeilingReached => "execution_ceiling_reached",
            Self::Failed(code) => code,
        }
    }
    /// The reason of a teardown from the run's own status (`owner_driver::status_json`) and the
    /// teardown's `failed` flag, as demo_chain.py:1329-1331 decides it.
    /// `rollover_requested` is the status's `rollover.state == "switching"` (both are set together,
    /// owner-run machine.rs `serving_poll`); the monitor's code is the `error_code` of a run that
    /// failed at its serving step (`fail_serving`: ceiling, monitor), any other failure is
    /// `owner_run_failed`.
    pub fn of_run(failed: bool, status: &Value) -> Self {
        if status["rollover"]["state"] == "switching" {
            return Self::SwitchingRuns;
        }
        if !failed {
            return Self::Stopped;
        }
        match status["error_code"].as_str() {
            Some("execution_ceiling_reached") if status["failed_step"] == "serving" => {
                Self::CeilingReached
            }
            Some(code) if status["failed_step"] == "serving" => Self::Failed(code.to_owned()),
            _ => Self::Failed("owner_run_failed".into()),
        }
    }
}
/// The hook the owner run's teardown calls: `chat.cancel_all(code)`. Returns the chats cancelled.
pub trait ChatTeardown: Send + Sync {
    fn cancel_all(&self, reason: &Teardown) -> usize;
}
/// The service's monotonic clock (`ChatService._clock`, chat.py:213).
pub trait Clock: Send + Sync {
    fn now(&self) -> Duration;
}
/// The production clock: time since construction.
pub struct MonotonicClock(Instant);
impl Default for MonotonicClock {
    fn default() -> Self {
        Self(Instant::now())
    }
}
impl Clock for MonotonicClock {
    fn now(&self) -> Duration {
        self.0.elapsed()
    }
}

fn span(id: BudgetId) -> Result<Duration, GatewayError> {
    id.span(Entries::ZERO)
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| GatewayError::new("chat_budget_unavailable"))
}
/// `_bounded` (chat.py:184-185): lowercase, runs of non-`[a-z0-9]` -> `_`, at most 96 chars.
fn bounded(text: &str) -> String {
    let mut out = String::new();
    for c in text.to_lowercase().chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            out.push(c);
        } else if !out.ends_with('_') {
            out.push('_');
        }
    }
    let trimmed: String = out.trim_matches('_').chars().take(96).collect();
    trimmed.trim_end_matches('_').to_owned()
}
/// `_http_code` (chat.py:188-199): `chat_http_<method>_<route>_<status>_<code>`.
fn http_code(method: &str, path: &str, status: u16, body: &Value) -> GatewayError {
    let route = path
        .strip_prefix("/v1/")
        .and_then(|p| p.split('/').next())
        .unwrap_or("events");
    let code = match &body["error"] {
        Value::String(s) => s.as_str(),
        Value::Object(o) => o.get("code").and_then(Value::as_str).unwrap_or(""),
        _ => "",
    };
    GatewayError::new(bounded(&format!(
        "chat_http_{method}_{route}_{status}_{code}"
    )))
}
/// `failure_code` for an I/O failure (chat.py:203-207): the Python exception the shipped client
/// raises in that case (`socket.timeout` is `TimeoutError`; urllib wraps connect failures).
fn io_code(error: &std::io::Error, connecting: bool) -> GatewayError {
    use std::io::ErrorKind::*;
    let name = match error.kind() {
        _ if connecting => "urlerror",
        TimedOut | WouldBlock => "timeouterror",
        ConnectionReset => "connectionreseterror",
        ConnectionAborted => "connectionabortederror",
        BrokenPipe => "brokenpipeerror",
        UnexpectedEof => "incompleteread",
        _ => "oserror",
    };
    GatewayError::new(format!("chat_generation_failed_{name}"))
}

/// A route epoch's request gateway over loopback HTTP (`api::requests` routes; the shipped
/// client's request shape, chat.py:106-147: request gateway v2, `interactive_chat_v1`).
pub struct HttpRouteGateway {
    addr: SocketAddr,
    bearer: String,
    idle: BudgetId,
}
impl HttpRouteGateway {
    /// With the shipped client's 300 s socket timeout (chat.py:70) as the idle bound.
    pub fn new(addr: SocketAddr, bearer: String) -> Self {
        Self::with_idle(
            addr,
            bearer,
            BudgetId::PyMyceliumA13MacosChatRegistryclientInitTimeoutL70,
        )
    }
    /// With `idle` (a typed budget row) as the bound on any single wait for the gateway.
    pub fn with_idle(addr: SocketAddr, bearer: String, idle: BudgetId) -> Self {
        Self { addr, bearer, idle }
    }
    fn exchange(
        &self,
        method: &str,
        path: &str,
        extra: &str,
        body: &str,
        idle: Duration,
    ) -> Result<(u16, Value), GatewayError> {
        let auth = format!("Authorization: Bearer {}\r\n{extra}", self.bearer);
        sse_client::request(self.addr, method, path, &auth, body, idle)
            .map_err(|(e, c)| io_code(&e, c))
    }
}
impl RouteGateway for HttpRouteGateway {
    fn stream(
        &self,
        prompt: &str,
        max_new_tokens: u64,
        cancel: &RequestCancel,
        on_token: &mut dyn FnMut(&str),
    ) -> Result<Streamed, GatewayError> {
        let idle = span(self.idle)?;
        // 503 is the gateway's answer while no route is qualified: name the state (chat.py:114-121).
        let qualification = match self.exchange("GET", "/v1/qualification/current", "", "", idle)? {
            (200, q) if q.is_object() => q,
            (503, _) => return Err(GatewayError::new("chat_route_not_qualified")),
            (status, body) => {
                return Err(http_code("get", "/v1/qualification/current", status, &body));
            }
        };
        let document = json!({"protocol":"mycelium.request_gateway.v2","prompt":prompt,
            "max_new_tokens":max_new_tokens,"qualification":qualification,
            "workload_profile_id":"interactive_chat_v1","qos_class":"interactive"});
        let accepted =
            match self.exchange("POST", "/v1/inference", "", &document.to_string(), idle)? {
                (202, accepted) => accepted,
                (status, body) => return Err(http_code("post", "/v1/inference", status, &body)),
            };
        let field = |name: &str| {
            accepted[name]
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| GatewayError::new("chat_gateway_answer_invalid"))
        };
        let (stream_path, cancel_path, token) = (
            field("stream_path")?,
            field("cancel_path")?,
            field("session_token")?,
        );
        let session = format!("X-Mycelium-Session: {token}\r\n");
        let (addr, bearer, delete_session) = (self.addr, self.bearer.clone(), session.clone());
        cancel.arm(Box::new(move || {
            let auth = format!("Authorization: Bearer {bearer}\r\n{delete_session}");
            match sse_client::request(addr, "DELETE", &cancel_path, &auth, "", idle) {
                Ok((status, _)) => Ok(status == 202),
                Err((e, c)) => Err(io_code(&e, c)),
            }
        }));
        let auth = format!("Authorization: Bearer {}\r\n{session}", self.bearer);
        let result = sse_client::events(self.addr, &stream_path, &auth, idle, &mut |event| {
            match event["type"].as_str() {
                Some("token") => {
                    on_token(event["text"].as_str().unwrap_or_default());
                    None
                }
                Some(kind @ ("completed" | "cancelled" | "failed")) => Some(Streamed {
                    terminal: Some(match kind {
                        "completed" => "completed",
                        "cancelled" => "cancelled",
                        _ => "failed",
                    }),
                    code: event["code"].as_str().map(str::to_owned),
                }),
                _ => None,
            }
        });
        cancel.disarm();
        match result {
            Ok(Some(streamed)) => Ok(streamed),
            Ok(None) => Ok(Streamed {
                terminal: None,
                code: None,
            }),
            Err(sse_client::EventsError::Refused(status, body)) => {
                Err(http_code("get", &stream_path, status, &body))
            }
            Err(sse_client::EventsError::Io(e, connecting)) => Err(io_code(&e, connecting)),
            Err(sse_client::EventsError::Invalid) => {
                Err(GatewayError::new("chat_generation_failed_jsondecodeerror"))
            }
        }
    }
}

/// One chat (`ChatService.send`'s record, chat.py:325-331).
struct Record {
    tokens: Vec<String>,
    done: bool,
    error: Option<String>,
    terminal: Option<&'static str>,
    cancel: RequestCancel,
    cancel_reason: Option<String>,
    max_new_tokens: u64,
    clamped: bool,
    served_by: Value,
    run_id: Option<String>,
    started: Duration,
    finished: Option<Duration>,
    first_token_at: Option<Duration>,
    last_token_at: Option<Duration>,
}
#[derive(Default)]
struct State {
    requests: HashMap<String, Record>,
    order: VecDeque<String>,
    per_client: HashMap<String, VecDeque<Duration>>,
    global: VecDeque<Duration>,
    busy: Option<String>,
    /// The current run's gateway, asked once per run (EP-PLAN U7).
    route: Option<(String, Arc<dyn RouteGateway>)>,
}
struct Inner {
    run: Arc<dyn RunRoute>,
    clock: Arc<dyn Clock>,
    timing: Option<PathBuf>,
    state: Mutex<State>,
    workers: Mutex<Vec<JoinHandle<()>>>,
}
/// `ChatService` (chat.py:211).
#[derive(Clone)]
pub struct ChatService {
    inner: Arc<Inner>,
}
/// A client that holds a /64 or many addresses must not be a fresh budget per request; more
/// than this many tracked clients prunes the empty buckets (chat.py:297).
const MAX_TRACKED_CLIENTS: usize = 4096;
/// Owner-run steps during which the route is activated/qualified (chat.py:47).
const WARMING_STEPS: [&str; 2] = ["activate", "serving"];
fn count(id: BudgetId) -> Result<usize, ChatError> {
    id.count()
        .ok()
        .and_then(|n| usize::try_from(n.get()).ok())
        .ok_or(ChatError::new(503, "chat_budget_unavailable"))
}
fn window(id: BudgetId) -> Result<Duration, ChatError> {
    span(id).map_err(|_| ChatError::new(503, "chat_budget_unavailable"))
}
/// `swarm_stats.milliseconds` (swarm_stats.py:27-30): 0..=86400 s -> round(s*1000), else null.
fn milliseconds(value: f64) -> Value {
    if value.is_finite() && (0.0..=86_400.0).contains(&value) {
        json!((value * 1000.0).round_ties_even() as u64)
    } else {
        Value::Null
    }
}
fn served_by(status: &Value) -> Value {
    Value::Array(
        status["participants"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|p| {
                let node: String = p["node_id"]
                    .as_str()
                    .unwrap_or("")
                    .chars()
                    .take(12)
                    .collect();
                json!({"label":p["label"],"node":node,"layers":p["layers"]})
            })
            .collect(),
    )
}
fn switching(status: &Value) -> bool {
    matches!(
        status["rollover"]["state"].as_str(),
        Some("switching" | "backoff")
    )
}
fn warming(status: &Value) -> bool {
    status["state"] == "running"
        && status["step"]
            .as_str()
            .is_some_and(|s| WARMING_STEPS.contains(&s))
}
/// `_ID` (chat.py:45): `[A-Za-z0-9_-]{16,64}`.
fn valid_id(id: &str) -> bool {
    (16..=64).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
/// Rulings 20/25: no v0.2 file under v0.1.4's app folder (`Mycelium A13 macOS Demo`).
fn v014_folder(path: &Path) -> bool {
    path.components()
        .any(|c| c.as_os_str().to_string_lossy().starts_with("Mycelium A13"))
}
impl ChatService {
    /// `timing`: the owner's per-chat timing file (JSONL, appended); refused under v0.1.4's folder.
    pub fn new(
        run: Arc<dyn RunRoute>,
        clock: Arc<dyn Clock>,
        timing: Option<PathBuf>,
    ) -> Result<Self, PublicError> {
        if timing.as_deref().is_some_and(v014_folder) {
            return Err(PublicError::new(500, "chat_timing_path_refused"));
        }
        Ok(Self {
            inner: Arc::new(Inner {
                run,
                clock,
                timing,
                state: Mutex::default(),
                workers: Mutex::default(),
            }),
        })
    }
    fn state_lock(&self) -> MutexGuard<'_, State> {
        self.inner.state.lock().unwrap_or_else(|p| p.into_inner())
    }
    /// `_serving` (chat.py:228-233): the run serves and has a route gateway. The gateway is asked
    /// once per run id and kept for the run's later chats.
    fn serving_route(&self) -> Option<(Value, Arc<dyn RouteGateway>)> {
        let status = self.inner.run.status()?;
        if status["state"] != "serving" {
            return None;
        }
        let run_id = status["run_id"].as_str()?.to_owned();
        if let Some((id, gateway)) = &self.state_lock().route
            && *id == run_id
        {
            return Some((status, gateway.clone()));
        }
        let gateway = self.inner.run.gateway(&run_id)?;
        self.state_lock().route = Some((run_id, gateway.clone()));
        Some((status, gateway))
    }
    /// `available()` (chat.py:235-236).
    pub fn serving(&self) -> bool {
        self.serving_route().is_some()
    }
    /// A chat is in flight (`_busy`).
    pub fn busy(&self) -> bool {
        self.state_lock().busy.is_some()
    }
    /// This service holds the record `request_id`.
    pub fn knows(&self, request_id: &str) -> bool {
        self.state_lock().requests.contains_key(request_id)
    }
    /// `_rate` (chat.py:285-298).
    fn rate(&self, state: &mut State, client: &str, now: Duration) -> Result<(), ChatError> {
        let window = window(BudgetId::ChatRateWindow)?;
        let (per_client, global) = (
            count(BudgetId::ChatPerClientPerWindow)?,
            count(BudgetId::ChatGlobalPerWindow)?,
        );
        let expired = |queue: &mut VecDeque<Duration>| {
            while queue
                .front()
                .is_some_and(|t| now.saturating_sub(*t) >= window)
            {
                queue.pop_front();
            }
        };
        let bucket = state.per_client.entry(client.to_owned()).or_default();
        expired(bucket);
        expired(&mut state.global);
        let bucket = state.per_client.entry(client.to_owned()).or_default();
        if bucket.len() >= per_client {
            return Err(ChatError::new(429, "chat_rate_limited"));
        }
        if state.global.len() >= global {
            return Err(ChatError::new(429, "chat_swarm_rate_limited"));
        }
        bucket.push_back(now);
        state.global.push_back(now);
        if state.per_client.len() > MAX_TRACKED_CLIENTS {
            state.per_client.retain(|_, v| !v.is_empty());
        }
        Ok(())
    }
    /// `send` (chat.py:300-340) after the body checks.
    pub fn send(&self, client: &str, prompt: Prompt) -> Result<Value, ChatError> {
        let Some((serving, gateway)) = self.serving_route() else {
            let status = self.inner.run.status().unwrap_or(Value::Null);
            if switching(&status) {
                return Err(ChatError {
                    retry_after: Some(window(
                        BudgetId::PyFinalMyceliumA13MacosChatRetryAfterSL315C60,
                    )?),
                    ..ChatError::new(503, "chat_switching_runs")
                });
            }
            return Err(ChatError::new(
                503,
                if warming(&status) {
                    "chat_warming_up"
                } else {
                    "chat_not_ready"
                },
            ));
        };
        let kept = count(BudgetId::PyMyceliumA13MacosChatMaxKeptRequestsL44)?;
        let (id, record_view) = {
            let mut state = self.state_lock();
            if state.busy.is_some() {
                return Err(ChatError::new(429, "chat_busy"));
            }
            let now = self.inner.clock.now();
            self.rate(&mut state, client, now)?;
            let id = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(
                crate::app_host::random(18).map_err(|e| ChatError {
                    error: e,
                    retry_after: None,
                })?,
            );
            let record = Record {
                tokens: Vec::new(),
                done: false,
                error: None,
                terminal: None,
                cancel: RequestCancel::default(),
                cancel_reason: None,
                max_new_tokens: prompt.max_new_tokens,
                clamped: prompt.clamped,
                served_by: served_by(&serving),
                run_id: serving["run_id"].as_str().map(str::to_owned),
                started: now,
                finished: None,
                first_token_at: None,
                last_token_at: None,
            };
            let view = json!({"request_id":id,"max_new_tokens":record.max_new_tokens,
                "clamped":record.clamped,"served_by":record.served_by});
            state.requests.insert(id.clone(), record);
            state.order.push_back(id.clone());
            while state.order.len() > kept {
                if let Some(old) = state.order.pop_front() {
                    state.requests.remove(&old);
                }
            }
            state.busy = Some(id.clone());
            (id, view)
        };
        let this = self.clone();
        let worker_id = id.clone();
        let spawned = std::thread::Builder::new()
            .name("a13-chat".into())
            .spawn(move || this.work(&worker_id, gateway, &prompt.text));
        let mut workers = self.inner.workers.lock().unwrap_or_else(|p| p.into_inner());
        workers.retain(|w| !w.is_finished());
        match spawned {
            Ok(handle) => workers.push(handle),
            Err(_) => {
                self.finish(&id, Some("failed"), "chat_worker_start_failed".into());
                return Err(ChatError::new(503, "chat_worker_start_failed"));
            }
        }
        Ok(record_view)
    }
    /// Record a chat's end that never reached the gateway.
    fn finish(&self, id: &str, terminal: Option<&'static str>, error: String) {
        let mut state = self.state_lock();
        if let Some(record) = state.requests.get_mut(id) {
            record.terminal = terminal;
            record.error = Some(error);
            record.done = true;
            record.finished = Some(self.inner.clock.now());
        }
        if state.busy.as_deref() == Some(id) {
            state.busy = None;
        }
    }
    /// `_work` (chat.py:342-398).
    fn work(&self, id: &str, gateway: Arc<dyn RouteGateway>, prompt: &str) {
        let (run_id, max_new_tokens, cancel) = {
            let state = self.state_lock();
            let Some(record) = state.requests.get(id) else {
                return;
            };
            (
                record.run_id.clone(),
                record.max_new_tokens,
                record.cancel.clone(),
            )
        };
        let outcome = match self.ended_by_ceiling(run_id.as_deref()) {
            Some(ended) => Err(GatewayError::new(ended)),
            None => gateway.stream(prompt, max_new_tokens, &cancel, &mut |text| {
                let stamp = self.inner.clock.now();
                if let Some(record) = self.state_lock().requests.get_mut(id) {
                    record.first_token_at.get_or_insert(stamp);
                    record.last_token_at = Some(stamp);
                    record.tokens.push(text.to_owned());
                }
            }),
        };
        let ended = match &outcome {
            Ok(streamed) if streamed.terminal == Some("failed") => {
                self.ended_by_ceiling(run_id.as_deref())
            }
            Ok(_) => None,
            Err(_) => self.ended_by_ceiling(run_id.as_deref()),
        };
        let finished = self.inner.clock.now();
        let mut state = self.state_lock();
        let mut timing = None;
        if let Some(record) = state.requests.get_mut(id) {
            match outcome {
                Ok(streamed) => {
                    record.terminal = streamed.terminal;
                    match streamed.terminal {
                        Some("failed") => {
                            record.error = Some(
                                record
                                    .cancel_reason
                                    .clone()
                                    .or(ended.map(str::to_owned))
                                    .or(streamed.code)
                                    .unwrap_or_else(|| "chat_generation_failed".into()),
                            );
                        }
                        Some("cancelled") => {
                            record.error = Some(
                                record
                                    .cancel_reason
                                    .clone()
                                    .unwrap_or_else(|| Teardown::Stopped.code().to_owned()),
                            );
                        }
                        _ => {}
                    }
                }
                Err(error) => {
                    let reason = record.cancel_reason.clone().or(ended.map(str::to_owned));
                    record.terminal = Some(if reason.is_some() {
                        "cancelled"
                    } else {
                        "failed"
                    });
                    record.error = Some(reason.unwrap_or(error.code));
                }
            }
            record.finished = Some(finished);
            if record.terminal == Some("completed") && record.cancel_reason.is_none() {
                timing = Some(Self::timing_line(record, finished));
            }
        }
        drop(state);
        // The record is complete (`finally`, chat.py:383-398) once its timing line is written.
        if let (Some(line), Some(path)) = (timing, &self.inner.timing) {
            Self::append(path, &line);
        }
        let mut state = self.state_lock();
        if let Some(record) = state.requests.get_mut(id) {
            record.done = true;
        }
        if state.busy.as_deref() == Some(id) {
            state.busy = None;
        }
    }
    /// The performance record of a completed chat (chat.py:386-395).
    fn timing_line(record: &Record, finished: Duration) -> Value {
        let seconds = |d: Duration| d.as_secs_f64();
        let ttft = record.first_token_at.map_or(Value::Null, |first| {
            milliseconds(seconds(first) - seconds(record.started))
        });
        let rate = match (record.first_token_at, record.last_token_at) {
            (Some(first), Some(last)) if last > first => {
                let span = seconds(last) - seconds(first);
                let n = record.tokens.len().saturating_sub(1) as f64;
                json!((n * 1000.0 / span).round_ties_even() as u64)
            }
            _ => Value::Null,
        };
        json!({"ttft_ms":ttft,"decode_millitokens_s":rate,
            "request_ms":milliseconds(seconds(finished) - seconds(record.started)),
            "run_id":record.run_id})
    }
    /// Append one JSON line (a failure is reported on stderr; it never fails the chat).
    fn append(path: &Path, line: &Value) {
        use std::os::unix::fs::OpenOptionsExt;
        let written = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(path)
            .and_then(|mut file| file.write_all(format!("{line}\n").as_bytes()));
        if let Err(e) = written {
            eprintln!("chat: timing record not written: {:?}", e.kind());
        }
    }
    /// `_run_ended_by_ceiling` (chat.py:400-416).
    fn ended_by_ceiling(&self, run_id: Option<&str>) -> Option<&'static str> {
        let status = self.inner.run.status()?;
        if switching(&status)
            || (run_id.is_some()
                && status["run_id"].as_str() != run_id
                && status["reason"] == "rollover")
        {
            return Some("chat_switching_runs");
        }
        (status["error_code"] == "execution_ceiling_reached").then_some("execution_ceiling_reached")
    }
    /// `cancel` (chat.py:418-440). `Ok(false)`: already done or cancelled, or the gateway could
    /// not be told (the generation then runs on).
    pub fn cancel(&self, request_id: &str, code: &str) -> Result<bool, ChatError> {
        if !valid_id(request_id) {
            return Err(ChatError::new(400, "chat_request_invalid"));
        }
        let cancel = {
            let mut state = self.state_lock();
            let record = state
                .requests
                .get_mut(request_id)
                .ok_or(ChatError::new(404, "chat_request_unknown"))?;
            if record.done || record.cancel_reason.is_some() {
                return Ok(false);
            }
            record.cancel_reason = Some(code.to_owned());
            record.cancel.clone()
        };
        if cancel.cancel() {
            return Ok(true);
        }
        if let Some(record) = self.state_lock().requests.get_mut(request_id) {
            record.cancel_reason = None;
        }
        Ok(false)
    }
    /// `cancel_all` (chat.py:442-451): every chat not done; returns how many were cancelled.
    pub fn cancel_all(&self, code: &str) -> usize {
        let running: Vec<String> = self
            .state_lock()
            .requests
            .iter()
            .filter(|(_, r)| !r.done)
            .map(|(id, _)| id.clone())
            .collect();
        running
            .iter()
            .filter(|id| matches!(self.cancel(id, code), Ok(true)))
            .count()
    }
    /// `events` (chat.py:454-474).
    pub fn events(&self, body: &Value) -> Result<Value, ChatError> {
        let invalid = ChatError::new(400, "chat_request_invalid");
        let Some(fields) = body.as_object() else {
            return Err(invalid);
        };
        if fields.len() != 2 || !fields.contains_key("request_id") || !fields.contains_key("after")
        {
            return Err(invalid);
        }
        let Some(id) = body["request_id"].as_str().filter(|id| valid_id(id)) else {
            return Err(invalid);
        };
        let Some(after) = body["after"].as_u64() else {
            return Err(invalid);
        };
        let state = self.state_lock();
        let record = state
            .requests
            .get(id)
            .ok_or(ChatError::new(404, "chat_request_unknown"))?;
        let from = usize::try_from(after)
            .unwrap_or(usize::MAX)
            .min(record.tokens.len());
        let tokens = &record.tokens[from..];
        let seconds = record.finished.map_or(Value::Null, |finished| {
            let s = finished.saturating_sub(record.started).as_secs_f64();
            json!((s * 100.0).round_ties_even() / 100.0)
        });
        Ok(
            json!({"tokens":tokens,"next":after.saturating_add(tokens.len() as u64),
            "done":record.done,"error":record.error,"terminal":record.terminal,
            "served_by":record.served_by,"seconds":seconds}),
        )
    }
    /// `state` (chat.py:476-504). `rejoining` is always false (no sharing fence in v0.2).
    pub fn state(&self) -> Value {
        let status = self.inner.run.status().unwrap_or(Value::Null);
        let serving = self.serving_route().map(|(status, _)| status);
        let busy = self.busy();
        let limits = json!({
            "max_prompt_chars": BudgetId::ChatMaxPromptChars.count().map(|n| n.get()).ok(),
            "max_new_tokens": BudgetId::ChatMaxNewTokensCap.count().map(|n| n.get()).ok(),
            "per_client_per_minute": BudgetId::ChatPerClientPerWindow.count().map(|n| n.get()).ok(),
            "swarm_per_minute": BudgetId::ChatGlobalPerWindow.count().map(|n| n.get()).ok(),
        });
        json!({"available":serving.is_some(),"busy":busy,
            "switching_runs":serving.is_none() && switching(&status),
            "warming_up":serving.is_none() && warming(&status),
            "rejoining":false,
            "model_id":if serving.is_some() { self.inner.run.model_id().map_or(Value::Null, Value::String) } else { Value::Null },
            "served_by":serving.as_ref().map_or(json!([]), served_by),
            "limits":limits})
    }
}
impl ChatTeardown for ChatService {
    fn cancel_all(&self, reason: &Teardown) -> usize {
        ChatService::cancel_all(self, reason.code())
    }
}
