// SPDX-License-Identifier: AGPL-3.0-only
//! The shipped chat service (`mycelium_a13_macos/chat.py:211-500 @ f4e366e9`) over the run's
//! route gateway. SCAFFOLD (RED): types and seams only; every operation refuses.
use crate::{chat::ChatError, schema::PublicError};
use serde_json::Value;
use std::{
    net::SocketAddr,
    path::PathBuf,
    sync::{Arc, Mutex},
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
/// A failure of one gateway request: its bounded public code (`failure_code`, chat.py:203-207).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GatewayError {
    pub code: String,
}
type CancelSend = Box<dyn FnOnce() -> Result<bool, GatewayError> + Send>;
#[derive(Default)]
struct CancelState {
    wanted: bool,
    sent: bool,
    send: Option<CancelSend>,
}
/// One request's cancel: remembered until the gateway accepted the request, then sent once.
#[derive(Clone, Default)]
pub struct RequestCancel(Arc<Mutex<CancelState>>);
impl RequestCancel {
    pub fn arm(&self, _send: CancelSend) {}
    pub fn disarm(&self) {}
    pub fn cancel(&self) -> bool {
        false
    }
}
/// The run's route gateway: one object per run (EP-PLAN U7), one call per chat.
pub trait RouteGateway: Send + Sync {
    fn stream(
        &self,
        prompt: &str,
        max_new_tokens: u64,
        cancel: &RequestCancel,
        on_token: &mut dyn FnMut(&str),
    ) -> Result<Streamed, GatewayError>;
}
/// The owner run as the chat service sees it.
pub trait RunRoute: Send + Sync {
    fn status(&self) -> Option<Value>;
    fn gateway(&self, run_id: &str) -> Option<Arc<dyn RouteGateway>>;
    fn model_id(&self) -> Option<String>;
}
/// Why the run's teardown cancels every chat (demo_chain.py:1322-1335).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Teardown {
    Stopped,
    SwitchingRuns,
    CeilingReached,
}
impl Teardown {
    pub fn code(self) -> &'static str {
        match self {
            Self::Stopped => "chat_cancelled",
            Self::SwitchingRuns => "chat_switching_runs",
            Self::CeilingReached => "execution_ceiling_reached",
        }
    }
}
/// The hook the owner run's teardown calls.
pub trait ChatTeardown: Send + Sync {
    fn cancel_all(&self, reason: Teardown) -> usize;
}
/// The service's monotonic clock (`ChatService._clock`).
pub trait Clock: Send + Sync {
    fn now(&self) -> Duration;
}
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
/// The request gateway of a route epoch over loopback HTTP.
pub struct HttpRouteGateway {
    addr: SocketAddr,
    bearer: String,
    idle: budgets::BudgetId,
}
impl HttpRouteGateway {
    pub fn new(addr: SocketAddr, bearer: String) -> Self {
        Self::with_idle(
            addr,
            bearer,
            budgets::BudgetId::PyMyceliumA13MacosChatRegistryclientInitTimeoutL70,
        )
    }
    pub fn with_idle(addr: SocketAddr, bearer: String, idle: budgets::BudgetId) -> Self {
        Self { addr, bearer, idle }
    }
}
impl RouteGateway for HttpRouteGateway {
    fn stream(
        &self,
        _prompt: &str,
        _max_new_tokens: u64,
        _cancel: &RequestCancel,
        _on_token: &mut dyn FnMut(&str),
    ) -> Result<Streamed, GatewayError> {
        let _ = (&self.addr, &self.bearer, self.idle);
        Err(GatewayError {
            code: "chat_service_not_ported".into(),
        })
    }
}
/// `ChatService` (chat.py:211).
#[derive(Clone)]
pub struct ChatService {
    run: Arc<dyn RunRoute>,
    clock: Arc<dyn Clock>,
    timing: Option<PathBuf>,
}
const NOT_PORTED: ChatError = ChatError::new(503, "chat_service_not_ported");
impl ChatService {
    pub fn new(
        run: Arc<dyn RunRoute>,
        clock: Arc<dyn Clock>,
        timing: Option<PathBuf>,
    ) -> Result<Self, PublicError> {
        Ok(Self { run, clock, timing })
    }
    pub fn serving(&self) -> bool {
        let _ = (&self.run, &self.clock, &self.timing);
        false
    }
    pub fn busy(&self) -> bool {
        false
    }
    pub fn knows(&self, _request_id: &str) -> bool {
        false
    }
    pub fn send(&self, _client: &str, _prompt: Prompt) -> Result<Value, ChatError> {
        Err(NOT_PORTED)
    }
    pub fn events(&self, _body: &Value) -> Result<Value, ChatError> {
        Err(NOT_PORTED)
    }
    pub fn state(&self) -> Value {
        Value::Null
    }
    pub fn cancel(&self, _request_id: &str, _code: &'static str) -> Result<bool, ChatError> {
        Err(NOT_PORTED)
    }
    pub fn cancel_all(&self, _code: &'static str) -> usize {
        0
    }
}
impl ChatTeardown for ChatService {
    fn cancel_all(&self, reason: Teardown) -> usize {
        ChatService::cancel_all(self, reason.code())
    }
}
