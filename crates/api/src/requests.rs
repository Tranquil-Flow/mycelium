// SPDX-License-Identifier: AGPL-3.0-only
//! Isolated request gateway routes at f4e366e9 request_gateway/asgi.py.
//! Not the browser BFF: bearer admission + request owner token, no cookie/CSRF
//! synthesis. L5 validates submissions/events; L9 owns terminal arbitration.
use crate::{
    http::{Reply, Request, Response, StreamingBody},
    schema::PublicError,
    sse::{EventStream, EventValidator, StreamError},
};
use serde_json::{Value, json};
use std::{io::Write, sync::Arc};
pub const MAX_REQUEST_BODY: usize = 262_144;
pub struct Accepted {
    pub request_id: String,
    pub session_token: String,
}
pub trait Subscription: Send {
    /// Must be bounded/cancellable by the host's L1 polling budget. EOF/reclaimed
    /// replay is an interrupted stream, NEVER an invented terminal event.
    fn next(&mut self) -> Result<Option<Value>, PublicError>;
    /// Only called after a complete frame was written and flushed.
    fn ack(&mut self, sequence: u64) -> Result<(), PublicError>;
    fn close(&mut self) -> Result<(), PublicError>;
    /// The generation this subscription publishes under, when the service
    /// advances it per subscribe (registry.subscribe). `None` keeps the
    /// client's Last-Event-ID generation as the resume binding.
    fn publisher_generation(&self) -> Option<u64> {
        None
    }
}
pub trait Service: Send + Sync + 'static {
    fn authorized(&self, request: &Request) -> bool;
    fn health(&self) -> Result<Value, PublicError>;
    fn qualification(&self) -> Result<Value, PublicError>;
    fn recovery(&self) -> Result<Value, PublicError>;
    /// Validate the L5 submission before admission. Generate owner-token entropy
    /// in the host and bind it atomically to the accepted request.
    fn submit(&self, document: Value) -> Result<Accepted, PublicError>;
    /// false means terminal: cancellation MUST NOT reverse completed authority.
    fn cancel(&self, id: &str, owner_token: Option<&str>) -> Result<bool, PublicError>;
    fn subscribe(
        &self,
        id: &str,
        cursor: Option<(u64, u64)>,
        owner_token: Option<&str>,
    ) -> Result<Box<dyn Subscription>, PublicError>;
    fn validate_event(&self, event: &Value) -> Result<(), PublicError>;
}
pub struct Gateway<S: Service> {
    service: Arc<S>,
}
#[derive(Clone, Copy)]
enum Route<'a> {
    Health,
    Qualification,
    Recovery,
    Submit,
    Events(&'a str),
    Cancel(&'a str),
}
fn route(target: &str) -> Option<Route<'_>> {
    let path = target.split('?').next()?;
    Some(match path {
        "/healthz" => Route::Health,
        "/v1/qualification/current" => Route::Qualification,
        "/v1/recovery/status" => Route::Recovery,
        "/v1/inference" => Route::Submit,
        _ => {
            let tail = path.strip_prefix("/v1/inference/")?;
            if let Some(id) = tail.strip_suffix("/events") {
                if id.is_empty() || id.contains('/') {
                    return None;
                }
                Route::Events(id)
            } else {
                if tail.is_empty() || tail.contains('/') {
                    return None;
                }
                Route::Cancel(tail)
            }
        }
    })
}
fn error(status: u16, code: &'static str) -> Result<Reply, PublicError> {
    Reply::error(PublicError::new(status, code))
}
impl<S: Service> Gateway<S> {
    pub fn new(service: S) -> Self {
        Self {
            service: Arc::new(service),
        }
    }
    pub fn preflight(&self, r: &Request) -> Result<Option<Reply>, PublicError> {
        let Some(route) = route(&r.target) else {
            return error(404, "not_found").map(Some);
        };
        if !matches!(route, Route::Health) && !self.service.authorized(r) {
            let mut reply = error(401, "unauthorized")?;
            reply
                .headers
                .push(("www-authenticate".into(), "Bearer".into()));
            return Ok(Some(reply));
        }
        let allowed = match route {
            Route::Submit => "POST",
            Route::Cancel(_) => "DELETE",
            _ => "GET",
        };
        if r.method != allowed {
            let mut reply = error(405, "method_not_allowed")?;
            reply.headers.push(("allow".into(), allowed.into()));
            return Ok(Some(reply));
        }
        if matches!(route, Route::Submit)
            && r.header("content-length")
                .and_then(|v| v.parse::<usize>().ok())
                .is_some_and(|n| n > MAX_REQUEST_BODY)
        {
            return error(400, "request_body_too_large").map(Some);
        }
        Ok(None)
    }
    pub fn handle(&self, r: Request) -> Result<Response, PublicError> {
        if let Some(reply) = self.preflight(&r)? {
            return Ok(reply.into());
        }
        let selected = route(&r.target).ok_or(PublicError::new(404, "not_found"))?;
        match selected {
            Route::Health => Reply::json(200, &self.service.health()?).map(Response::Buffered),
            Route::Qualification | Route::Recovery => {
                let result = if matches!(selected, Route::Qualification) {
                    self.service.qualification()
                } else {
                    self.service.recovery()
                };
                match result {
                    Ok(value) => Reply::json(200, &value),
                    Err(e) => error(503, e.code),
                }
                .map(Response::Buffered)
            }
            Route::Submit => self.submit(&r).map(Response::Buffered),
            Route::Cancel(id) => {
                let result = owner_token(&r).and_then(|token| self.service.cancel(id, token));
                match result {
                    Ok(cancelled)=>Reply::json(if cancelled{202}else{200},&json!({"request_id":id,"status":if cancelled{"cancelling"}else{"terminal"}})),
                    Err(e)=>error(if e.code=="unknown_request"{404}else{409},e.code),
                }.map(Response::Buffered)
            }
            Route::Events(id) => {
                let result = cursor(&r).and_then(|cursor| {
                    owner_token(&r)
                        .and_then(|token| self.service.subscribe(id, cursor, token))
                        .map(|s| (s, cursor))
                });
                let (subscription, resume) = match result {
                    // A resume continues the cursor's sequence under the NEW
                    // subscription's publisher generation (subscribe advances it).
                    Ok((s, resume)) => {
                        let resume =
                            resume.map(|(g, n)| (s.publisher_generation().unwrap_or(g), n));
                        (s, resume)
                    }
                    Err(e) => {
                        return error(
                            if e.code == "unknown_request" {
                                404
                            } else {
                                409
                            },
                            e.code,
                        )
                        .map(Response::Buffered);
                    }
                };
                let mut credentials = Vec::new();
                for name in ["authorization", "x-mycelium-session"] {
                    if let Some(v) = r.header(name) {
                        credentials.push(v.as_bytes().to_vec());
                        if let Some(token) = v.strip_prefix("Bearer ") {
                            credentials.push(token.as_bytes().to_vec());
                        }
                    }
                }
                Ok(Response::Stream {
                    reply: Reply {
                        status: 200,
                        headers: vec![
                            (
                                "content-type".into(),
                                "text/event-stream; charset=utf-8".into(),
                            ),
                            ("cache-control".into(), "no-store".into()),
                            ("x-accel-buffering".into(), "no".into()),
                        ],
                        body: vec![],
                    },
                    body: Box::new(Body {
                        subscription,
                        stream: EventStream::for_request(
                            Validator(self.service.clone()),
                            credentials,
                            id.into(),
                            resume,
                        ),
                    }),
                })
            }
        }
    }
    fn submit(&self, r: &Request) -> Result<Reply, PublicError> {
        if r.body.len() > MAX_REQUEST_BODY {
            return error(400, "request_body_too_large");
        }
        let document: Value = match serde_json::from_slice(&r.body) {
            Ok(v) => v,
            Err(_) => return error(400, "invalid_request_body"),
        };
        if !document.is_object() {
            return error(400, "invalid_request_body");
        }
        match self.service.submit(document) {
            Ok(accepted) => Reply::json(
                202,
                &json!({"request_id":accepted.request_id,"stream_path":format!("/v1/inference/{}/events",accepted.request_id),"cancel_path":format!("/v1/inference/{}",accepted.request_id),"session_token":accepted.session_token}),
            ),
            Err(e) => error(
                match e.code {
                    "gateway_capacity_exhausted" => 503,
                    "deployment_epoch_changed"
                    | "path_changed"
                    | "qualification_mismatch"
                    | "readiness_revoked"
                    | "route_dropped"
                    | "stale_qualification" => 409,
                    _ => 400,
                },
                e.code,
            ),
        }
    }
}
pub(crate) fn valid_owner_token(token: &str) -> bool {
    token.is_ascii() && (32..=256).contains(&token.len())
}
fn owner_token(r: &Request) -> Result<Option<&str>, PublicError> {
    let values = r.values("x-mycelium-session").collect::<Vec<_>>();
    if values.is_empty() {
        return Ok(None);
    }
    if values.len() != 1 || !valid_owner_token(values[0]) {
        return Err(PublicError::new(409, "invalid_session_token"));
    }
    Ok(Some(values[0]))
}
fn cursor(r: &Request) -> Result<Option<(u64, u64)>, PublicError> {
    let invalid = PublicError::new(409, "invalid_last_event_id");
    let values = r.values("last-event-id").collect::<Vec<_>>();
    if values.is_empty() {
        return Ok(None);
    }
    if values.len() != 1 {
        return Err(invalid);
    }
    let (g, s) = values[0].split_once(':').ok_or(invalid)?;
    let parse = |v: &str| -> Result<u64, PublicError> {
        if v.is_empty() || !v.bytes().all(|c| c.is_ascii_digit()) {
            return Err(invalid);
        }
        let n = v.parse::<u64>().map_err(|_| invalid)?;
        if n > i64::MAX as u64 {
            Err(invalid)
        } else {
            Ok(n)
        }
    };
    let g = parse(g)?;
    let s = parse(s)?;
    if g == 0 {
        return Err(invalid);
    }
    Ok(Some((g, s)))
}
struct Validator<S: Service>(Arc<S>);
impl<S: Service> EventValidator for Validator<S> {
    fn validate(&self, event: &Value) -> Result<(), PublicError> {
        // Host validation may add authority checks, never replace the wire schema.
        protocol::request::event(event).map_err(|e| PublicError::new(500, e.0))?;
        self.0.validate_event(event)
    }
}
struct Body<S: Service> {
    subscription: Box<dyn Subscription>,
    stream: EventStream<Validator<S>>,
}
impl<S: Service> StreamingBody for Body<S> {
    fn write(&mut self, writer: &mut dyn Write) -> Result<(), StreamError> {
        while let Some(event) = self.subscription.next()? {
            let sequence = event["sequence"]
                .as_u64()
                .ok_or(PublicError::new(500, "invalid_stream_event"))?;
            let terminal = matches!(
                event["type"].as_str(),
                Some("completed" | "cancelled" | "failed")
            );
            self.stream.write(writer, event)?;
            self.subscription.ack(sequence)?;
            if terminal {
                return Ok(());
            }
        }
        Ok(())
    }
    fn close(&mut self) -> Result<(), PublicError> {
        self.subscription.close()
    }
}
