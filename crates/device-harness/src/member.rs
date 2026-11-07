// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Result, effects::Clock, http::Http};
use budgets::Span;
use serde::Serialize;
use serde_json::Value;
#[derive(Clone, Copy)]
pub struct Limits {
    pub total: Span,
    pub request: Span,
    pub poll: Span,
    pub run_wall: Span,
}
#[derive(Debug, Default, Serialize)]
pub struct Joined {
    pub membership_state: String,
    pub status_latencies_ms: Vec<u64>,
}
#[derive(Debug, serde::Serialize)]
pub struct StatusObservation {
    pub latency_ms: u64,
    pub status: Value,
}
pub fn observed_status<I: Http, C: Clock>(
    io: &mut I,
    clock: &C,
    bound: Span,
) -> Result<StatusObservation> {
    let before = clock.now();
    let value = perform(io, Action::Status, bound)?;
    let elapsed = clock.now().checked_sub(before).ok_or(crate::Fault::new(
        "clock_went_backwards",
        crate::FaultKind::Observation,
    ))?;
    if elapsed > bound.millis() {
        return Err(crate::Fault::new(
            "status_observation_timeout",
            crate::FaultKind::Observation,
        ));
    }
    Ok(StatusObservation {
        latency_ms: elapsed,
        status: value,
    })
}
pub enum Action {
    Status,
    Start,
    Capability,
}
pub fn status_bound(default: Span, api: Option<Span>, alias: Option<Span>) -> Span {
    alias.or(api).unwrap_or(default)
}
fn call<I: Http>(
    io: &mut I,
    path: &'static str,
    body: Option<Value>,
    bound: Span,
) -> Result<Value> {
    use crate::{
        Fault, FaultKind,
        http::{Method, Request},
    };
    if bound == Span::ZERO {
        return Err(Fault::new("join_timeout", FaultKind::Observation));
    }
    let (code, value) = io.call(
        Request {
            path,
            method: if body.is_some() {
                Method::Post
            } else {
                Method::Get
            },
            body,
            chat: false,
        },
        bound,
    )?;
    if code != 200 || !value["error"].is_null() {
        return Err(Fault::new("join_terminal_error", FaultKind::Product));
    }
    Ok(value)
}
pub fn perform<I: Http>(io: &mut I, action: Action, bound: Span) -> Result<Value> {
    let (path, body) = match action {
        Action::Status => ("/api/status", None),
        Action::Start => ("/api/start", Some(serde_json::json!({}))),
        Action::Capability => ("/api/capability/report", Some(serde_json::json!({}))),
    };
    let value = call(io, path, body, bound)?;
    Ok(crate::observe::redact(&value, &[]))
}
pub fn join<I: Http, C: Clock>(
    io: &mut I,
    clock: &mut C,
    link: &str,
    limits: Limits,
) -> Result<Joined> {
    use crate::{Fault, FaultKind};
    let timeout = || Fault::new("join_timeout", FaultKind::Observation);
    if limits.total == Span::ZERO || limits.request == Span::ZERO || limits.poll == Span::ZERO {
        return Err(timeout());
    }
    let end = clock
        .now()
        .checked_add(limits.total.millis())
        .ok_or_else(timeout)?;
    let mut result = Joined::default();
    let mut read = |io: &mut I, clock: &C, path, body| -> Result<Value> {
        let before = clock.now();
        if before >= end {
            return Err(timeout());
        }
        let value = call(
            io,
            path,
            body,
            limits.request.min(Span::from_millis(end - before)),
        )?;
        let elapsed = clock.now().checked_sub(before).ok_or_else(timeout)?;
        if clock.now() > end {
            return Err(timeout());
        }
        if path == "/api/status" {
            result.status_latencies_ms.push(elapsed);
        }
        Ok(value)
    };
    let first = read(io, clock, "/api/status", None)?;
    let digest = first["join_flow"]["standing_disclosure"]["digest"]
        .as_str()
        .ok_or(Fault::new(
            "standing_disclosure_missing",
            FaultKind::Observation,
        ))?;
    let body = crate::http::join_body(link, digest, limits.run_wall)?;
    let mut state = read(io, clock, "/api/join/window", Some(body))?;
    loop {
        if state["state"] == "joined" || state["membership_state"] == "current" {
            let status = read(io, clock, "/api/status", None)?;
            if status["authority"]["membership_state"] == "current" {
                result.membership_state = "current".into();
                return Ok(result);
            }
        } else {
            if !matches!(
                state["state"].as_str(),
                Some("waiting_for_owner" | "waiting")
            ) {
                return Err(Fault::new("join_terminal_error", FaultKind::Product));
            }
            state = read(
                io,
                clock,
                "/api/join/window/poll",
                Some(serde_json::json!({})),
            )?;
            if state["state"] == "joined" || state["membership_state"] == "current" {
                continue;
            }
        }
        let now = clock.now();
        if now >= end {
            return Err(timeout());
        }
        clock.sleep(limits.poll.min(Span::from_millis(end - now)))?;
        if clock.now() <= now {
            return Err(Fault::new("clock_not_advancing", FaultKind::Observation));
        }
    }
}
