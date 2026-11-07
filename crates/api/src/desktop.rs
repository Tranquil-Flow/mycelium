// SPDX-License-Identifier: AGPL-3.0-only
//! Desktop transport contract; no fake authority/planner/runtime in product.
use crate::{
    http::{MAX_BODY, Reply, Request},
    native_session::{
        MonotonicClock, NativeLauncher, SessionClock, launch_lifetime, session_lifetime,
        valid_token,
    },
    schema::{PublicError, strict_object},
    status::PublishedStatus,
    wire::{Json, Text},
};
use serde_json::{Map, Value};
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";
/// L6/L9 own actions, durability, cancellation and admission, NOT this adapter.
/// Implementations return public code-only errors, never raw backend strings.
/// A successful action must publish its completed display state before returning.
pub trait Service: Send + Sync {
    fn action(&self, path: &str, body: &Map<String, Value>) -> Result<Value, PublicError>;
    /// Same as `action` for services that answer in the source's insertion order (ruling (a)).
    fn action_json(&self, path: &str, body: &Map<String, Value>) -> Result<Json, PublicError> {
        self.action(path, body).map(Json::from)
    }
    fn chat(&self, _request: Request) -> Result<Reply, PublicError> {
        Reply::error(PublicError::new(404, "not_found"))
    }
    fn asset(&self, path: &str) -> Result<Option<Reply>, PublicError> {
        Ok(crate::assets::desktop(path))
    }
}
/// Entropy/rotation is host-owned. Supply fresh unguessable URL-safe tokens; this
/// type never generates a credential or writes private application state.
pub struct Session {
    launch: Option<String>,
    session: String,
    launch_expires: Instant,
    session_expires: Option<Instant>,
    lifetime: Duration,
    clock: Arc<dyn SessionClock>,
    native: Option<Native>,
}
struct Native {
    credential: String,
    launcher: Box<dyn NativeLauncher>,
}
impl Session {
    pub fn new(
        launch: String,
        session: String,
        launch_expires: Instant,
        lifetime: Duration,
    ) -> Result<Self, PublicError> {
        if !valid_token(&launch) || !valid_token(&session) || lifetime.is_zero() {
            return Err(PublicError::new(500, "session_configuration_invalid"));
        }
        Ok(Self {
            launch: Some(launch),
            session,
            launch_expires,
            session_expires: None,
            lifetime,
            clock: Arc::new(MonotonicClock),
            native: None,
        })
    }
    /// Production lifetime is the exact oracle registry value, not a host
    /// convenience timeout. Obtain the initial launch via /native-session.
    pub fn native(
        credential: String,
        launcher: Box<dyn NativeLauncher>,
        clock: Arc<dyn SessionClock>,
    ) -> Result<Self, PublicError> {
        if !valid_token(&credential) {
            return Err(PublicError::new(500, "session_configuration_invalid"));
        }
        Ok(Self {
            launch: None,
            session: String::new(),
            launch_expires: clock.now(),
            session_expires: None,
            lifetime: session_lifetime()?,
            clock,
            native: Some(Native {
                credential,
                launcher,
            }),
        })
    }
    fn authorized(&self, r: &Request) -> bool {
        if self.session_expires.is_none_or(|t| self.clock.now() >= t) {
            return false;
        }
        r.header("cookie")
            .and_then(|raw| {
                raw.split(';')
                    .filter_map(|part| part.trim().split_once('='))
                    .rfind(|(k, _)| *k == "a13session")
            })
            .is_some_and(|(_, token)| same(token, &self.session))
    }
}
fn same(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |diff, (x, y)| diff | (x ^ y))
            == 0
}
pub struct Desktop<S: Service> {
    host: String,
    origin: String,
    service: Arc<S>,
    status: Arc<PublishedStatus>,
    session: Mutex<Session>,
}
impl<S: Service> Desktop<S> {
    pub fn new(
        host: String,
        service: Arc<S>,
        status: Arc<PublishedStatus>,
        session: Session,
    ) -> Self {
        Self {
            origin: format!("http://{host}"),
            host,
            service,
            status,
            session: Mutex::new(session),
        }
    }
    pub fn service(&self) -> &S {
        &self.service
    }
    fn guard(&self, r: &Request) -> Result<(), PublicError> {
        let hosts: Vec<_> = r.values("host").collect();
        if hosts != [self.host.as_str()] {
            return Err(PublicError::new(403, "host_rejected"));
        }
        if r.target.contains(['?', '#', '%', '\\']) {
            return Err(PublicError::new(400, "path_rejected"));
        }
        let public_asset = r.method == "GET"
            && matches!(
                r.target.as_str(),
                "/" | "/app.js" | "/style.css" | "/run.js" | "/swarm.js"
            );
        let public_chat = (r.method == "GET" && r.target == "/chat")
            || (matches!(r.method.as_str(), "GET" | "POST") && r.target.starts_with("/chat/"));
        if public_asset || public_chat || (r.method == "POST" && r.target == "/session") {
            return Ok(());
        }
        if r.method == "POST" && r.target == "/native-session" {
            let session = self
                .session
                .lock()
                .map_err(|_| PublicError::new(503, "session_unavailable"))?;
            if !session.native.as_ref().is_some_and(|native| {
                same(
                    r.header("x-native-authorization").unwrap_or(""),
                    &native.credential,
                )
            }) {
                return Err(PublicError::new(403, "native_authorization_required"));
            }
            if r.header("origin").is_some() {
                return Err(PublicError::new(403, "native_only"));
            }
            return Ok(());
        }
        // Unknown GETs are 404 before session, unlike known protected status/QR.
        if r.method == "GET"
            && r.target != "/api/status"
            && !r.target.starts_with("/api/pair/window/qr/")
        {
            return Ok(());
        }
        if !self
            .session
            .lock()
            .map_err(|_| PublicError::new(503, "session_unavailable"))?
            .authorized(r)
        {
            return Err(PublicError::new(401, "open_from_native_launcher"));
        }
        if r.method == "POST"
            && (r.header("origin") != Some(&self.origin)
                || r.header("x-mycelium-action") != Some("1"))
        {
            return Err(PublicError::new(403, "origin_rejected"));
        }
        Ok(())
    }
    pub fn preflight(&self, r: &Request) -> Result<Option<Reply>, PublicError> {
        match self.guard(r) {
            Ok(()) => Ok(None),
            Err(e) => self.decorate(Reply::error(e)?).map(Some),
        }
    }
    pub fn handle(&self, r: Request) -> Result<Reply, PublicError> {
        let reply = match self.dispatch(r) {
            Ok(reply) => reply,
            Err(e) => Reply::error(e)?,
        };
        self.decorate(reply)
    }
    fn decorate(&self, mut reply: Reply) -> Result<Reply, PublicError> {
        if reply.header("content-security-policy").is_none() {
            reply
                .headers
                .push(("content-security-policy".into(), CSP.into()));
        }
        Ok(reply)
    }
    fn dispatch(&self, r: Request) -> Result<Reply, PublicError> {
        self.guard(&r)?;
        if r.method == "GET" {
            if r.target == "/chat" || r.target.starts_with("/chat/") {
                return self.service.chat(r);
            }
            if r.target == "/api/status" {
                return Reply::ordered(200, &self.status.read_json()?, Text::Ascii);
            }
            if matches!(
                r.target.as_str(),
                "/" | "/app.js" | "/style.css" | "/run.js" | "/swarm.js"
            ) {
                return self
                    .service
                    .asset(&r.target)?
                    .ok_or(PublicError::new(503, "assets_unavailable"));
            }
            return Err(PublicError::new(404, "not_found"));
        }
        if r.method != "POST" {
            return Err(PublicError::new(501, "unsupported_method"));
        }
        let lengths: Vec<_> = r.values("content-length").collect();
        if r.header("transfer-encoding").is_some() || lengths.len() != 1 {
            return Err(PublicError::new(400, "framing_rejected"));
        }
        let length = lengths[0]
            .parse::<usize>()
            .map_err(|_| PublicError::new(400, "framing_rejected"))?;
        if length > MAX_BODY {
            return Err(PublicError::new(413, "body_too_large"));
        }
        if length != r.body.len() {
            return Err(PublicError::new(400, "incomplete_body"));
        }
        if r.target.starts_with("/chat/") {
            return self.service.chat(r);
        }
        if r.target == "/session" {
            return self.sign_in(&r);
        }
        if r.target == "/native-session" {
            return self.reopen(&r);
        }
        if r.header("content-type") != Some("application/json") {
            return Err(PublicError::new(415, "content_type_rejected"));
        }
        let body = strict_object(&r.body)?;
        // No status/session lock is held across ANY service call.
        Reply::ordered(
            200,
            &self.service.action_json(&r.target, &body)?,
            Text::Ascii,
        )
    }
    fn sign_in(&self, r: &Request) -> Result<Reply, PublicError> {
        if r.header("content-type") != Some("application/x-www-form-urlencoded") {
            return Err(PublicError::new(415, "content_type_rejected"));
        }
        if !matches!(r.header("origin"), Some("null")) && r.header("origin") != Some(&self.origin) {
            return Err(PublicError::new(403, "origin_rejected"));
        }
        let token = crate::native_session::launch_form(&r.body)?;
        let mut session = self
            .session
            .lock()
            .map_err(|_| PublicError::new(503, "session_unavailable"))?;
        if session.native.as_ref().is_some_and(|n| n.launcher.closed()) {
            return Err(PublicError::new(409, "application_closed"));
        }
        let token = token
            .as_deref()
            .ok_or(PublicError::new(403, "launch_expired"))?;
        if session.clock.now() >= session.launch_expires
            || !session.launch.as_ref().is_some_and(|v| same(v, token))
        {
            return Err(PublicError::new(403, "launch_expired"));
        }
        session.launch = None;
        if let Some(native) = &mut session.native {
            native.launcher.consume_launch()?;
            let token = native.launcher.fresh_token()?;
            if !valid_token(&token) {
                return Err(PublicError::new(503, "native_control_unavailable"));
            }
            session.session = token;
        }
        let expiry = session
            .clock
            .now()
            .checked_add(session.lifetime)
            .ok_or(PublicError::new(503, "session_unavailable"))?;
        session.session_expires = Some(expiry);
        let mut reply = Reply::json(303, &Value::Null)?;
        reply.body.clear();
        reply.headers.push(("location".into(), "/".into()));
        reply.headers.push((
            "set-cookie".into(),
            format!(
                "a13session={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}",
                session.session,
                session.lifetime.as_secs()
            ),
        ));
        Ok(reply)
    }
    fn reopen(&self, r: &Request) -> Result<Reply, PublicError> {
        if r.body != b"{}" || r.header("content-type") != Some("application/json") {
            return Err(PublicError::new(400, "invalid_request"));
        }
        let mut session = self
            .session
            .lock()
            .map_err(|_| PublicError::new(503, "session_unavailable"))?;
        let native = session
            .native
            .as_mut()
            .ok_or(PublicError::new(403, "native_authorization_required"))?;
        if native.launcher.closed() {
            return Err(PublicError::new(409, "application_closed"));
        }
        let token = native.launcher.fresh_token()?;
        if !valid_token(&token) {
            return Err(PublicError::new(503, "native_control_unavailable"));
        }
        session.launch_expires = session
            .clock
            .now()
            .checked_add(launch_lifetime()?)
            .ok_or(PublicError::new(503, "session_unavailable"))?;
        session.launch = Some(token.clone());
        session
            .native
            .as_mut()
            .ok_or(PublicError::new(503, "session_unavailable"))?
            .launcher
            .publish_launch(&self.origin, &token)?;
        Reply::json(200, &serde_json::json!({"launch_file_ready":true}))
    }
}
