// SPDX-License-Identifier: AGPL-3.0-only
//! Candidate-bound native reopen and browser-session adapter. All OS/file/HTTP
//! effects are injected; this module never opens a socket or launches an app.
use crate::{
    Fault, FaultKind, Result, effects::Clock, preflight::ProcessIdentity, stranger::Candidate,
};
use budgets::Span;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub candidate: Candidate,
    pub process: ProcessIdentity,
    pub launch_epoch_ms: u64,
    pub desktop_manifest_sha256: String,
    pub native_inputs_sha256: String,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Observation {
    pub binding: Binding,
    pub control_modified_ms: u64,
    pub port: u16,
}
/// No Debug/Serialize: never expose cookie, native credential or launch token.
#[derive(Clone, PartialEq, Eq)]
pub struct Session {
    pub binding: Binding,
    pub port: u16,
    pub cookie: String,
}
pub struct LaunchForm {
    pub port: u16,
    pub path: String,
    pub launch: String,
    pub modified_ms: u64,
}
pub struct Request {
    pub port: u16,
    pub path: &'static str,
    pub method: &'static str,
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
    pub follow_redirects: bool,
}
pub struct Reply {
    pub http: u16,
    pub json: Value,
    pub headers: Vec<(String, String)>,
}
/// The file adapter must enforce private regular owned single-link files with
/// no symlinks. Session persistence must replace with 0600 and read back exact
/// bytes. Observation must derive identities from trusted candidate/OS reads,
/// never trust an unauthenticated status document to attest its own executable.
pub trait Io {
    fn observe(&mut self, bound: Span) -> Result<Observation>;
    fn load_session(&mut self, bound: Span) -> Result<Option<Session>>;
    fn native_credential(&mut self, bound: Span) -> Result<String>;
    fn launch_form(&mut self, bound: Span) -> Result<LaunchForm>;
    fn exchange(&mut self, request: Request, bound: Span) -> Result<Reply>;
    fn save_session(&mut self, session: &Session, bound: Span) -> Result<()>;
}
#[derive(Clone, Debug, Serialize)]
pub struct Receipt {
    pub candidate: Candidate,
    pub status_http: u16,
    pub renewals: u8,
    pub elapsed_ms: u64,
    pub release_qualified: bool,
}
pub struct Status {
    pub value: Value,
    pub receipt: Receipt,
}
pub struct Adapter<I, C> {
    pub io: I,
    pub clock: C,
    binding: Binding,
}
fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}
fn token(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
}
fn valid_cookie(cookie: &str) -> bool {
    cookie.strip_prefix("a13session=").is_some_and(token)
}
impl<I: Io, C: Clock> crate::http::Http for Adapter<I, C> {
    fn call(&mut self, request: crate::http::Request, bound: Span) -> Result<(u16, Value)> {
        use crate::http::Method;
        if request.chat {
            return Err(fault("native_member_request_refused"));
        }
        if request.path == "/api/status" && request.method == Method::Get && request.body.is_none()
        {
            return self.check(bound).map(|s| (s.receipt.status_http, s.value));
        }
        if request.method != Method::Post
            || !matches!(
                request.path,
                "/api/start"
                    | "/api/capability/report"
                    | "/api/join/window"
                    | "/api/join/window/poll"
            )
            || !request.body.as_ref().is_some_and(Value::is_object)
        {
            return Err(fault("native_member_request_refused"));
        }
        let end = self
            .clock
            .now()
            .checked_add(bound.millis())
            .ok_or(fault("native_check_budget_invalid"))?;
        // Establish a usable candidate-bound session with the existing one-renewal
        // allowance. Only this observation can renew; the action is sent once.
        self.check(self.remaining(end)?)?;
        let reference = self.observe(end)?;
        let session = self
            .io
            .load_session(self.remaining(end)?)?
            .ok_or(fault("native_session_missing"))?;
        self.remaining(end)?;
        if session.binding != self.binding
            || session.port != reference.port
            || !valid_cookie(&session.cookie)
        {
            return Err(fault("native_session_binding_mismatch"));
        }
        let body = serde_json::to_vec(&request.body)
            .map_err(|_| fault("native_member_request_refused"))?;
        let reply = self.exchange(
            &reference,
            Request {
                port: reference.port,
                path: request.path,
                method: "POST",
                body,
                follow_redirects: false,
                headers: [
                    ("Content-Type".into(), "application/json".into()),
                    ("X-Mycelium-Action".into(), "1".into()),
                    (
                        "Origin".into(),
                        format!("http://127.0.0.1:{}", reference.port),
                    ),
                    ("Cookie".into(), session.cookie),
                ]
                .into(),
            },
            end,
        )?;
        // Ambiguous transport failures, 401 and other refusals are never replayed.
        Ok((reply.http, reply.json))
    }
}
impl<I: Io, C: Clock> Adapter<I, C> {
    pub fn new(io: I, clock: C, binding: Binding) -> Result<Self> {
        binding.candidate.validate()?;
        if binding.process.pid <= 1
            || binding.process.birth.is_empty()
            || !std::path::Path::new(&binding.process.executable).is_absolute()
            || !crate::preflight::valid_digest(&binding.desktop_manifest_sha256, 64)
            || !crate::preflight::valid_digest(&binding.native_inputs_sha256, 64)
        {
            return Err(fault("native_candidate_binding_invalid"));
        }
        Ok(Self { io, clock, binding })
    }
    pub fn binding(&self) -> &Binding {
        &self.binding
    }
    fn remaining(&self, end: u64) -> Result<Span> {
        let now = self.clock.now();
        if now >= end {
            return Err(fault("owner_status_timeout"));
        }
        Ok(Span::from_millis(end - now))
    }
    fn observe(&mut self, end: u64) -> Result<Observation> {
        let observation = self.io.observe(self.remaining(end)?)?;
        self.remaining(end)?;
        if observation.binding != self.binding
            || observation.port == 0
            || observation.control_modified_ms < self.binding.launch_epoch_ms
        {
            return Err(fault("native_candidate_binding_changed"));
        }
        Ok(observation)
    }
    fn exchange(&mut self, reference: &Observation, request: Request, end: u64) -> Result<Reply> {
        if &self.observe(end)? != reference {
            return Err(fault("native_candidate_binding_changed"));
        }
        let reply = self.io.exchange(request, self.remaining(end)?)?;
        self.remaining(end)?;
        if &self.observe(end)? != reference {
            return Err(fault("native_candidate_binding_changed"));
        }
        Ok(reply)
    }
    fn renew(&mut self, reference: &Observation, end: u64) -> Result<Session> {
        let credential = self.io.native_credential(self.remaining(end)?)?;
        self.remaining(end)?;
        if !token(&credential) {
            return Err(fault("native_credential_invalid"));
        }
        let native = self.exchange(
            reference,
            Request {
                port: reference.port,
                path: "/native-session",
                method: "POST",
                body: b"{}".to_vec(),
                follow_redirects: false,
                headers: [
                    ("Content-Type".into(), "application/json".into()),
                    ("X-Native-Authorization".into(), credential),
                ]
                .into(),
            },
            end,
        )?;
        if native.http != 200 || native.json["launch_file_ready"] != true {
            return Err(fault("native_reopen_refused"));
        }
        let form = self.io.launch_form(self.remaining(end)?)?;
        self.remaining(end)?;
        if form.port != reference.port
            || form.path != "/session"
            || !token(&form.launch)
            || form.modified_ms < reference.control_modified_ms
        {
            return Err(fault("native_launch_form_invalid"));
        }
        let reply = self.exchange(
            reference,
            Request {
                port: reference.port,
                path: "/session",
                method: "POST",
                body: format!("launch={}", form.launch).into_bytes(),
                follow_redirects: false,
                headers: [
                    (
                        "Content-Type".into(),
                        "application/x-www-form-urlencoded".into(),
                    ),
                    ("Origin".into(), "null".into()),
                ]
                .into(),
            },
            end,
        )?;
        if reply.http != 303 {
            return Err(fault("native_session_refused"));
        }
        let cookies: Vec<_> = reply
            .headers
            .iter()
            .filter(|(k, _)| k.eq_ignore_ascii_case("set-cookie"))
            .map(|(_, v)| v)
            .collect();
        if cookies.len() != 1 || cookies[0].contains(['\r', '\n', '\0']) {
            return Err(fault("native_session_cookie_invalid"));
        }
        let cookie = cookies[0]
            .split(';')
            .next()
            .ok_or(fault("native_session_cookie_invalid"))?;
        if !valid_cookie(cookie) {
            return Err(fault("native_session_cookie_invalid"));
        }
        let session = Session {
            binding: self.binding.clone(),
            port: reference.port,
            cookie: cookie.into(),
        };
        self.io.save_session(&session, self.remaining(end)?)?;
        self.remaining(end)?;
        let readback = self.io.load_session(self.remaining(end)?)?;
        self.remaining(end)?;
        if readback.as_ref() != Some(&session) {
            return Err(fault("native_session_readback_mismatch"));
        }
        Ok(session)
    }
    fn status(&mut self, reference: &Observation, session: &Session, end: u64) -> Result<Reply> {
        if session.binding != self.binding
            || session.port != reference.port
            || !valid_cookie(&session.cookie)
        {
            return Err(fault("native_session_binding_mismatch"));
        }
        self.exchange(
            reference,
            Request {
                port: reference.port,
                path: "/api/status",
                method: "GET",
                body: vec![],
                follow_redirects: false,
                headers: [("Cookie".into(), session.cookie.clone())].into(),
            },
            end,
        )
    }
    pub fn check(&mut self, total: Span) -> Result<Status> {
        self.check_with_allowance(total, &mut true)
    }
    /// Shared keeper/driver checks pass a single allowance across observations.
    pub fn check_with_allowance(&mut self, total: Span, allowance: &mut bool) -> Result<Status> {
        if total == Span::ZERO {
            return Err(fault("native_check_budget_invalid"));
        }
        let start = self.clock.now();
        let end = start
            .checked_add(total.millis())
            .ok_or(fault("native_check_budget_invalid"))?;
        let reference = self.observe(end)?;
        let cached = self.io.load_session(self.remaining(end)?)?;
        self.remaining(end)?;
        let mut renewals = 0;
        let mut session = match cached {
            Some(session) => session,
            None => {
                if !*allowance {
                    return Err(fault("owner_status_refused"));
                }
                *allowance = false;
                renewals = 1;
                self.renew(&reference, end)?
            }
        };
        let mut reply = self.status(&reference, &session, end)?;
        // Only the exact expiry response is a renewal trigger. Never replay
        // actions/Start, generic 401s, transport errors or a second refusal.
        if reply.http == 401 && reply.json["error"] == "open_from_native_launcher" && *allowance {
            *allowance = false;
            renewals = 1;
            session = self.renew(&reference, end)?;
            reply = self.status(&reference, &session, end)?;
        }
        if reply.http != 200 {
            return Err(fault("owner_status_refused"));
        }
        if !reply.json.is_object() {
            return Err(fault("owner_status_invalid"));
        }
        let elapsed_ms = self
            .clock
            .now()
            .checked_sub(start)
            .ok_or(fault("clock_went_backwards"))?;
        Ok(Status {
            value: reply.json,
            receipt: Receipt {
                candidate: self.binding.candidate.clone(),
                status_http: reply.http,
                renewals,
                elapsed_ms,
                release_qualified: false,
            },
        })
    }
}
