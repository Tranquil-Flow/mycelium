// SPDX-License-Identifier: AGPL-3.0-only
//! Shared chat HTTP boundary at f4e366e9 chat.py:532-613. The browser polls
//! JSON events; this is NOT the request gateway's SSE endpoint. Backend owns
//! admission/rate limits, relay accounting, history, cancellation and generation.
use crate::{
    desktop,
    http::{Reply, Request},
    schema::PublicError,
    wire::{Json, Text},
};
use serde_json::{Map, Value};
use std::{
    net::{IpAddr, Ipv6Addr},
    time::Duration,
};
const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'";
#[derive(Debug)]
pub struct ChatError {
    pub error: PublicError,
    pub retry_after: Option<Duration>,
}
impl ChatError {
    pub const fn new(status: u16, code: &'static str) -> Self {
        Self {
            error: PublicError::new(status, code),
            retry_after: None,
        }
    }
    fn reply(self) -> Result<Reply, PublicError> {
        let mut body = self.error.document();
        if let Some(delay) = self.retry_after {
            body["retry_after_s"] = delay.as_secs().into();
        }
        Reply::json_ascii(self.error.status, &body)
    }
}
pub trait Service: Send + Sync {
    fn is_owner(&self) -> bool;
    fn state(&self) -> Result<Value, ChatError>;
    /// Preserve the raw document until the relay entry guard runs. The service
    /// must validate prompt/max_new_tokens before admitting any generation.
    fn send(&self, client: &str, body: Value, via_relay: bool) -> Result<Value, ChatError>;
    fn events(&self, body: Value) -> Result<Value, ChatError>;
    /// Key order matters on the wire: v0.1.4 `chat._json` does not sort (chat.py:612-613), so a
    /// service that builds its documents in the source's insertion order overrides these three; the
    /// defaults are the sorted `Value` entry points above.
    fn state_json(&self) -> Result<Json, ChatError> {
        self.state().map(Json::from)
    }
    fn send_json(&self, client: &str, body: Value, via_relay: bool) -> Result<Json, ChatError> {
        self.send(client, body, via_relay).map(Json::from)
    }
    fn events_json(&self, body: Value) -> Result<Json, ChatError> {
        self.events(body).map(Json::from)
    }
}
pub struct Chat<S: Service> {
    service: S,
}
impl<S: Service> Chat<S> {
    pub fn new(service: S) -> Self {
        Self { service }
    }
    pub fn service(&self) -> &S {
        &self.service
    }
    /// `via_relay` is trusted listener configuration, NEVER a request header.
    pub fn handle(&self, r: Request, via_relay: bool) -> Result<Reply, PublicError> {
        let mut reply = match self.dispatch(&r, via_relay) {
            Ok(r) => r,
            Err(e) => e.reply()?,
        };
        reply
            .headers
            .push(("content-security-policy".into(), CSP.into()));
        Ok(reply)
    }
    fn dispatch(&self, r: &Request, via_relay: bool) -> Result<Reply, ChatError> {
        if !self.service.is_owner() {
            return Err(ChatError::new(404, "not_found"));
        }
        if r.method == "GET"
            && let Some(asset) = crate::assets::chat(&r.target)
        {
            return Ok(asset);
        }
        if r.method != "POST"
            || !matches!(
                r.target.as_str(),
                "/chat/api/send" | "/chat/api/events" | "/chat/api/state"
            )
        {
            return Err(ChatError::new(404, "not_found"));
        }
        if r.header("x-mycelium-chat") != Some("1")
            || r.header("content-type") != Some("application/json")
        {
            return Err(ChatError::new(403, "chat_header_required"));
        }
        if r.body.len() > 16384 {
            return Err(ChatError::new(413, "chat_body_too_large"));
        }
        // Oracle chat json.loads intentionally differs from strict desktop
        // consent parsing: empty body is {}, duplicate keys keep the last value,
        // and /state accepts any valid JSON value. Never use this for signatures.
        let body: Value = serde_json::from_slice(if r.body.is_empty() { b"{}" } else { &r.body })
            .map_err(|_| ChatError::new(400, "chat_request_invalid"))?;
        let value = match r.target.as_str() {
            "/chat/api/state" => self.service.state_json()?,
            "/chat/api/send" => {
                self.service
                    .send_json(&client_key(r, via_relay), body, via_relay)?
            }
            _ => self.service.events_json(body)?,
        };
        Reply::ordered(200, &value, Text::Ascii).map_err(|error| ChatError {
            error,
            retry_after: None,
        })
    }
}
pub fn client_key(r: &Request, via_relay: bool) -> String {
    if !via_relay {
        return "local".into();
    }
    let raw = r.header("cf-connecting-ip").unwrap_or("");
    if !(2..=45).contains(&raw.len())
        || !raw
            .bytes()
            .all(|b| b.is_ascii_hexdigit() || b == b'.' || b == b':')
    {
        return "relay:unknown".into();
    }
    match raw.parse::<IpAddr>() {
        Ok(IpAddr::V4(ip)) => format!("relay:{ip}"),
        Ok(IpAddr::V6(ip)) => {
            if let Some(v4) = ip.to_ipv4_mapped() {
                return format!("relay:{v4}");
            }
            let mut bytes = ip.octets();
            bytes[8..].fill(0);
            format!("relay:{}/64", Ipv6Addr::from(bytes))
        }
        Err(_) => "relay:unknown".into(),
    }
}
/// Optional composition of private desktop actions and public local chat. Relay
/// listeners compose Chat directly and must enforce their own ingress boundary.
pub struct WithChat<A, C: Service> {
    actions: A,
    chat: Chat<C>,
}
impl<A, C: Service> WithChat<A, C> {
    pub fn new(actions: A, chat: Chat<C>) -> Self {
        Self { actions, chat }
    }
}
impl<A: desktop::Service, C: Service> desktop::Service for WithChat<A, C> {
    fn action(&self, path: &str, body: &Map<String, Value>) -> Result<Value, PublicError> {
        self.actions.action(path, body)
    }
    fn asset(&self, path: &str) -> Result<Option<Reply>, PublicError> {
        self.actions.asset(path)
    }
    fn chat(&self, r: Request) -> Result<Reply, PublicError> {
        self.chat.handle(r, false)
    }
}
