// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-only restart capability. No member mutation or live transport exists here.
use crate::{Host, Result, effects::Clock};
use budgets::Span;
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[derive(Clone, Copy, Debug)]
pub enum OwnerAction {
    Stop,
    Launch,
    SignIn,
}
pub trait RestartIo: Clock {
    fn owner_lifecycle(&mut self, action: OwnerAction, bound: Span) -> Result<()>;
    fn status(&mut self, host: Host, bound: Span) -> Result<Value>;
    fn owner_post(&mut self, path: &'static str, body: Value, bound: Span) -> Result<()>;
}
#[derive(Clone, Copy)]
pub struct Limits {
    pub total: Span,
    pub request: Span,
    pub poll: Span,
    pub relay_wall: Span,
}
#[derive(Debug, PartialEq, Eq)]
pub struct Options {
    pub resume: String,
    pub relay: String,
}
impl Default for Options {
    fn default() -> Self {
        Self {
            resume: "user".into(),
            relay: "quick".into(),
        }
    }
}
impl Options {
    /// Parses only restart options; does not enable a live soak command.
    pub fn parse(args: &[&str]) -> Result<Self> {
        let mut result = Self::default();
        let mut seen = std::collections::BTreeSet::new();
        let mut flags = args.chunks_exact(2);
        for pair in &mut flags {
            if !seen.insert(pair[0]) {
                return Err(crate::Fault::new(
                    "duplicate_restart_option",
                    crate::FaultKind::Input,
                ));
            }
            match (pair[0], pair[1]) {
                ("--restart-resume", value @ ("user" | "automatic")) => {
                    result.resume = value.into()
                }
                ("--restart-relay", value @ ("quick" | "demo")) => result.relay = value.into(),
                _ => {
                    return Err(crate::Fault::new(
                        "invalid_restart_option",
                        crate::FaultKind::Input,
                    ));
                }
            }
        }
        if !flags.remainder().is_empty() {
            return Err(crate::Fault::new(
                "missing_restart_option_value",
                crate::FaultKind::Input,
            ));
        }
        Ok(result)
    }
}
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct Receipt {
    pub recovered: bool,
    pub resume: String,
    pub owner_actions: Vec<String>,
    pub recovery_ms: u64,
    pub error: Option<String>,
    pub observation_errors: Vec<String>,
    pub release_qualified: bool,
}
pub fn relay_body(relay: &str, wall: Span) -> Result<Value> {
    use crate::{Fault, FaultKind};
    if !matches!(relay, "quick" | "demo") {
        return Err(Fault::new("invalid_restart_relay_mode", FaultKind::Input));
    }
    if wall == Span::ZERO || !wall.millis().is_multiple_of(1000) {
        return Err(Fault::new("invalid_gui_duration", FaultKind::Input));
    }
    let mut b = serde_json::json!({"relay_acknowledged":true,"max_seconds":wall.millis()/1000});
    if relay == "demo" {
        for (k, v) in [
            ("provider", Value::from("external")),
            ("public_origin", Value::from("https://demo.mycelium.now")),
            ("proxy_port", Value::from(4385)),
            ("cloudflare_front", Value::from(true)),
            ("long_lived", Value::from(true)),
        ] {
            b[k] = v;
        }
    }
    Ok(b)
}
fn bounded<I: RestartIo, T>(
    io: &mut I,
    end: u64,
    request: Span,
    f: impl FnOnce(&mut I, Span) -> Result<T>,
) -> Result<T> {
    use crate::{Fault, FaultKind};
    let now = io.now();
    if now >= end {
        return Err(Fault::new(
            "owner_restart_recovery_timeout",
            FaultKind::Observation,
        ));
    }
    let result = f(io, request.min(Span::from_millis(end - now)));
    if io.now() < now || io.now() > end {
        return Err(Fault::new(
            "restart_observation_timeout",
            FaultKind::Observation,
        ));
    }
    result
}
pub fn restart<I: RestartIo>(
    io: &mut I,
    resume: &str,
    relay: &str,
    limits: Limits,
) -> Result<Receipt> {
    use crate::{Fault, FaultKind};
    if !matches!(resume, "user" | "automatic") {
        return Err(Fault::new("invalid_restart_resume_mode", FaultKind::Input));
    }
    let body = relay_body(relay, limits.relay_wall)?;
    let start = io.now();
    let end = start
        .checked_add(limits.total.millis())
        .ok_or(Fault::new("invalid_restart_budget", FaultKind::Input))?;
    if limits.total == Span::ZERO || limits.request == Span::ZERO || limits.poll == Span::ZERO {
        return Err(Fault::new("invalid_restart_budget", FaultKind::Input));
    }
    let mut receipt = Receipt {
        resume: resume.into(),
        ..Receipt::default()
    };
    let result = (|| -> Result<()> {
        for action in [OwnerAction::Stop, OwnerAction::Launch, OwnerAction::SignIn] {
            bounded(io, end, limits.request, |io, b| {
                io.owner_lifecycle(action, b)
            })?;
        }
        if resume == "user" {
            let s = bounded(io, end, limits.request, |io, b| io.status(Host::Owner, b))?;
            if matches!(s["network"]["state"].as_str(), Some("private" | "stopped")) {
                bounded(io, end, limits.request, |io, b| {
                    io.owner_post("/api/network/start", body, b)
                })?;
                receipt.owner_actions.push("network_start".into());
            }
            if !crate::soak::Run::from_status(&s).serving() {
                bounded(io, end, limits.request, |io, b| {
                    io.owner_post("/api/demo/start", serde_json::json!({}), b)
                })?;
                receipt.owner_actions.push("demo_start".into());
            }
        }
        while io.now() < end {
            let observation = (|| -> Result<bool> {
                let owner = bounded(io, end, limits.request, |io, b| io.status(Host::Owner, b))?;
                if !crate::soak::Run::from_status(&owner).serving() {
                    return Ok(false);
                }
                let member = bounded(io, end, limits.request, |io, b| io.status(Host::Member, b))?;
                Ok(member["membership_state"] == "current"
                    && member["lease_state"] == "fresh"
                    && member["capability_state"] == "self_report_accepted")
            })();
            match observation {
                Ok(true) => {
                    receipt.recovered = true;
                    return Ok(());
                }
                Ok(false) => {}
                Err(e) if e.incomplete() => return Err(e),
                Err(e) => receipt.observation_errors.push(e.code.into()),
            }
            let now = io.now();
            if now >= end {
                break;
            }
            io.sleep(limits.poll.min(Span::from_millis(end - now)))?;
            if io.now() <= now {
                return Err(Fault::new("clock_not_advancing", FaultKind::Observation));
            }
        }
        Err(Fault::new(
            "owner_restart_recovery_timeout",
            FaultKind::Observation,
        ))
    })();
    if let Err(e) = result {
        receipt.error = Some(e.code.into());
    }
    receipt.recovery_ms = io
        .now()
        .checked_sub(start)
        .ok_or(Fault::new("clock_went_backwards", FaultKind::Observation))?;
    Ok(receipt)
}
