// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Fault, FaultKind, Result, effects::Clock};
use budgets::Span;
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Method {
    Get,
    Post,
}
/// Never Debug or Serialize a request: prompt, join material and session cookies
/// are not evidence. A concrete adapter owns the in-memory private session.
pub struct Request {
    pub path: &'static str,
    pub method: Method,
    pub body: Option<Value>,
    pub chat: bool,
}
pub trait Http {
    fn call(&mut self, request: Request, bound: Span) -> Result<(u16, Value)>;
}
pub trait Guard {
    fn check(&mut self) -> Result<()>;
}
#[derive(Clone, Copy, Debug)]
pub struct ChatLimits {
    pub total: Span,
    pub request: Span,
    pub poll: Span,
}
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Chat {
    pub done: bool,
    pub terminal: Option<String>,
    pub error: Option<String>,
    pub switching: bool,
    #[serde(default)]
    pub submit_http: Option<u16>,
    #[serde(default)]
    pub submit_code: Option<String>,
    pub n_tokens: u64,
    pub ttft_ms: Option<u64>,
    pub total_ms: u64,
    pub decode_tok_s: Option<f64>,
    pub start_ms: u64,
    pub end_ms: u64,
}
impl Chat {
    pub fn ok(&self) -> bool {
        self.done
            && self.terminal.as_deref() == Some("completed")
            && self.error.is_none()
            && self.n_tokens > 0
    }
}
pub fn switching(page: &Value) -> bool {
    ["error", "code"]
        .iter()
        .any(|k| page[*k] == "chat_switching_runs")
        || ["message", "error", "detail"].iter().any(|k| {
            page[*k]
                .as_str()
                .is_some_and(|s| s.trim().eq_ignore_ascii_case("switching runs"))
        })
}
fn request(path: &'static str, body: Value) -> Request {
    Request {
        path,
        method: Method::Post,
        body: Some(body),
        chat: true,
    }
}
pub fn chat<I: Http, C: Clock, G: Guard>(
    io: &mut I,
    clock: &mut C,
    guard: &mut G,
    prompt: &str,
    n: u32,
    limits: ChatLimits,
) -> Result<Chat> {
    let invalid = || Fault::new("invalid_chat_budget", FaultKind::Input);
    if limits.total == Span::ZERO || limits.request == Span::ZERO || limits.poll == Span::ZERO {
        return Err(invalid());
    }
    let start = clock.now();
    let end = start
        .checked_add(limits.total.millis())
        .ok_or_else(invalid)?;
    let mut c = Chat {
        start_ms: start,
        ..Chat::default()
    };
    guard.check()?;
    let (http, submitted) = io.call(
        request(
            "/chat/api/send",
            serde_json::json!({"prompt":prompt,"max_new_tokens":n}),
        ),
        limits.request.min(limits.total),
    )?;
    if switching(&submitted) {
        c.submit_http = Some(http);
        c.submit_code = Some("chat_switching_runs".into());
        c.switching = true;
        c.error = Some("switching_runs_message".into());
    } else if http != 200 || submitted["request_id"].as_str().is_none_or(str::is_empty) {
        c.submit_http = Some(http);
        c.submit_code = submitted["error"]
            .as_str()
            .filter(|s| {
                !s.is_empty()
                    && s.len() <= 160
                    && s.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
            })
            .map(str::to_owned);
        c.error = Some("chat_submission_failed".into());
    } else {
        let rid = &submitted["request_id"];
        let mut after = 0u64;
        let mut first = None;
        let mut last = None;
        while clock.now() < end {
            guard.check()?;
            let remaining = Span::from_millis(end - clock.now());
            let (http, page) = io.call(
                request(
                    "/chat/api/events",
                    serde_json::json!({"request_id":rid,"after":after}),
                ),
                limits.request.min(remaining),
            )?;
            if clock.now() > end {
                break;
            }
            if http != 200 {
                c.error = Some("chat_events_failed".into());
                break;
            }
            c.switching = switching(&page);
            if let Some(tokens) = page["tokens"].as_array() {
                let count = tokens
                    .iter()
                    .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
                    .count() as u64;
                if count > 0 {
                    first.get_or_insert(clock.now());
                    last = Some(clock.now());
                    c.n_tokens = c
                        .n_tokens
                        .checked_add(count)
                        .ok_or(Fault::new("token_count_overflow", FaultKind::Observation))?;
                }
            }
            if let Some(next) = page["next"].as_u64() {
                if next < after {
                    c.error = Some("chat_cursor_regressed".into());
                    break;
                }
                after = next;
            }
            if page["done"] == true {
                c.done = true;
                c.terminal = match page["terminal"].as_str() {
                    Some(s @ ("completed" | "failed" | "cancelled")) => Some(s.into()),
                    _ => None,
                };
                if !page["error"].is_null() && page["error"] != "" {
                    c.error = Some(
                        if c.switching {
                            "chat_switching_runs"
                        } else {
                            "redacted_product_error"
                        }
                        .into(),
                    );
                }
                break;
            }
            let before = clock.now();
            clock.sleep(limits.poll.min(Span::from_millis(end - before)))?;
            if clock.now() <= before {
                return Err(Fault::new("clock_not_advancing", FaultKind::Observation));
            }
        }
        if !c.done && c.error.is_none() {
            c.error = Some("chat_timeout".into());
        }
        c.ttft_ms = first.and_then(|t: u64| t.checked_sub(start));
        if let (Some(f), Some(l)) = (first, last)
            && c.n_tokens > 1
            && l > f
        {
            c.decode_tok_s = Some((c.n_tokens - 1) as f64 * 1000. / (l - f) as f64);
        }
    }
    c.end_ms = clock.now();
    c.total_ms = c
        .end_ms
        .checked_sub(start)
        .ok_or(Fault::new("clock_went_backwards", FaultKind::Observation))?;
    if c.end_ms > end {
        return Err(Fault::new(
            "chat_observation_timeout",
            FaultKind::Observation,
        ));
    }
    Ok(c)
}
pub fn consent() -> Value {
    serde_json::json!({"peer_visibility":true,"power":"battery_allowed","storage_bytes":2_000_000_000u64,"network":"metered_allowed","background":false,"diagnostics":false,"thermal":"stop_on_serious"})
}
fn seconds(span: Span) -> Result<u64> {
    if span == Span::ZERO || !span.millis().is_multiple_of(1000) {
        return Err(Fault::new("invalid_gui_duration", FaultKind::Input));
    }
    Ok(span.millis() / 1000)
}
pub fn create_body(quick: bool, relay_wall: Span) -> Result<Value> {
    let mut b = serde_json::json!({"consent":consent()});
    if quick {
        b["relay_acknowledged"] = Value::Bool(true);
        b["max_seconds"] = Value::from(seconds(relay_wall)?);
    }
    Ok(b)
}
pub fn join_body(link: &str, digest: &str, run_limit: Span) -> Result<Value> {
    use budgets::{BudgetId, Entries};
    let choices = [
        BudgetId::MemberChosenWall,
        BudgetId::WorkerDefaultWall,
        BudgetId::WorkerMaximumWall,
    ];
    let allowed = choices
        .into_iter()
        .map(|id| id.span(Entries::ZERO))
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(|_| Fault::new("gui_budget_unavailable", FaultKind::Input))?;
    if !allowed.contains(&run_limit) {
        return Err(Fault::new("run_limit_not_a_gui_option", FaultKind::Input));
    }
    if link.is_empty() || digest.is_empty() {
        return Err(Fault::new("join_material_missing", FaultKind::Input));
    }
    Ok(
        serde_json::json!({"consent":consent(),"adaptive_sharing":false,"join":link,"standing_consent":{"acknowledged":true,"disclosure_digest":digest,"caps":{"storage_bytes":2_000_000_000u64,"max_memory_bytes":4_294_967_296u64,"max_wall_seconds":seconds(run_limit)?,"max_commands":5000}}}),
    )
}
