// SPDX-License-Identifier: AGPL-3.0-only
//! `demo_app.action` (`demo_app.py:96-133 @ f4e366e9`): the refusal ladder in front of the run.
//!
//! Order (each rung returns before the next): `owner_role_required`; the body (`start` takes
//! `{}` or exactly `{"execution_ceiling": ...}`, a bad ceiling is `execution_ceiling_invalid`,
//! any other truthy body `invalid_request`); `owner_swarm_required` when the run exchange cannot
//! be mounted; `relay_required` (start and rebuild only, while joined members wait for the relay);
//! then the run's own refusals (`owner_run_active`, `owner_run_rebuild_requires_run`,
//! `owner_run_rebuild_in_progress`), whose messages the oracle drops (`DemoError(error.code)`).
use crate::ceiling;
use crate::machine::{Clock, Cmd, OwnerRun};
use serde_json::{Map, Value};

/// `OWNER_ROUTES` (`demo_app.py:18`).
pub const OWNER_ROUTES: [&str; 3] = ["/api/demo/start", "/api/demo/stop", "/api/demo/rebuild"];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DemoPath {
    Start,
    Stop,
    Rebuild,
}

impl DemoPath {
    pub fn from_path(path: &str) -> Option<Self> {
        match path {
            "/api/demo/start" => Some(Self::Start),
            "/api/demo/stop" => Some(Self::Stop),
            "/api/demo/rebuild" => Some(Self::Rebuild),
            _ => None,
        }
    }
}

/// `DemoError(code)` / `DemoActionError(code, message=, fix=)`: HTTP 400 `{"error": code[, message, fix]}`
/// (`app.py:2011`, `action_error_body`).
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Refusal {
    pub code: String,
    pub message: Option<String>,
    pub fix: Option<Map<String, Value>>,
}

impl Refusal {
    fn code(code: &str) -> Self {
        Self {
            code: code.to_owned(),
            message: None,
            fix: None,
        }
    }
}

/// What the host knows (the `app` argument of `action`).
pub trait DemoFacts {
    /// `app.installation_role == 'owner'`.
    fn owner(&self) -> bool;
    /// `ensure_exchange(app) is not None`: the owner has an authority with a control mailbox and
    /// the run exchange and model-source server are mounted on it (idempotent; the host mounts).
    fn ensure_exchange(&mut self) -> bool;
    /// `app.members_joined_without_relay()`; `None` for a stand-in app without the method.
    fn members_joined_without_relay(&self) -> Option<u64>;
}

/// The effect of an admitted action on the run.
#[derive(Clone, Debug, PartialEq)]
pub enum Plan {
    Start { ceiling: Option<Value> },
    Stop,
    Rebuild,
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// The ladder before the run is touched. `body` is the parsed request body (`null` for none).
pub fn admit(path: DemoPath, body: &Value, facts: &mut dyn DemoFacts) -> Result<Plan, Refusal> {
    if !facts.owner() {
        return Err(Refusal::code("owner_role_required"));
    }
    let mut plan_ceiling = None;
    let only_ceiling =
        matches!(body, Value::Object(m) if m.len() == 1 && m.contains_key("execution_ceiling"));
    if path == DemoPath::Start && only_ceiling {
        let checked = ceiling::checked(&body["execution_ceiling"])
            .map_err(|_| Refusal::code(ceiling::INVALID))?;
        plan_ceiling = Some(checked.to_value());
    } else if truthy(body) {
        return Err(Refusal::code("invalid_request"));
    }
    if !facts.ensure_exchange() {
        return Err(Refusal::code("owner_swarm_required"));
    }
    if path != DemoPath::Stop {
        relay_gate(facts)?;
    }
    Ok(match path {
        DemoPath::Start => Plan::Start {
            ceiling: plan_ceiling,
        },
        DemoPath::Stop => Plan::Stop,
        DemoPath::Rebuild => Plan::Rebuild,
    })
}

/// `relay_gate`: serving only with the relay up (`demo_app.py:96-106`).
fn relay_gate(facts: &dyn DemoFacts) -> Result<(), Refusal> {
    let count = facts.members_joined_without_relay().unwrap_or(0);
    if count == 0 {
        return Ok(());
    }
    let mut fix = Map::new();
    fix.insert("action".into(), "start_relay".into());
    Err(Refusal {
        code: "relay_required".into(),
        message: Some(format!(
            "{count} member{} joined, but the public relay is not running, so they cannot serve yet. \
             Start the public relay, then try again.",
            if count == 1 { "" } else { "s" }
        )),
        fix: Some(fix),
    })
}

/// What an admitted action did: the commands the driver performs, and whether the answer carries
/// `rebuild_requested` (`rebuild_async`).
#[derive(Debug, PartialEq)]
pub struct Applied {
    pub cmds: Vec<Cmd>,
    pub rebuild_requested: bool,
}

/// Apply a [`Plan`] to the run; the run's own refusals surface as `DemoError(code)` (no message).
pub fn apply(plan: &Plan, run: &mut OwnerRun, clock: Clock) -> Result<Applied, Refusal> {
    let refuse = |e: crate::machine::OwnerRunError| Refusal::code(&e.code);
    match plan {
        Plan::Start { ceiling } => run
            .start("start", ceiling.as_ref(), clock)
            .map(|cmds| Applied {
                cmds,
                rebuild_requested: false,
            })
            .map_err(refuse),
        Plan::Stop => Ok(Applied {
            cmds: run.stop(),
            rebuild_requested: false,
        }),
        Plan::Rebuild => run
            .rebuild_async(clock)
            .map(|cmds| Applied {
                cmds,
                rebuild_requested: true,
            })
            .map_err(refuse),
    }
}
