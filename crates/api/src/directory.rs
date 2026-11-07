// SPDX-License-Identifier: AGPL-3.0-only
//! Directory HTTP boundary at f4e366e9 server.py:144-224/directory.py:83-119.
//! Signature verification, time, replay, rate limits and durable storage remain
//! service-owned. No fake success/grant or alternative authority is implemented.
use crate::{
    http::{Reply, Request},
    schema::{PublicError, unique_value},
};
use serde_json::{Map, Value};
use std::net::IpAddr;
pub const PREFIX: &str = "/swarms/api/v1";
pub const SIGNED_ROUTES: &[&str] = &[
    "/records",
    "/delete",
    "/heartbeat",
    "/access-requests",
    "/owner-inbox",
    "/grants",
    "/deny",
    "/my-grants",
    "/entry-offers",
];
pub trait Service: Send + Sync {
    /// Return the exact public projection, including its original signed record.
    /// /config and /swarms have distinct schemas; never expose database rows.
    fn get(&self, path: &str) -> Result<Value, PublicError>;
    /// MUST verify protocol, signature, freshness, ownership and replay/rate
    /// authority before mutation. Parsing here is NOT signature acceptance.
    fn signed(&self, path: &str, document: &Map<String, Value>) -> Result<Value, PublicError>;
    /// Host-composed services override these to apply the original per-client
    /// read/write/query budgets. Legacy callbacks are transport-free seams only.
    fn get_for_client(&self, path: &str, _client: &str) -> Result<Value, PublicError> {
        self.get(path)
    }
    fn signed_for_client(
        &self,
        path: &str,
        document: &Map<String, Value>,
        _client: &str,
    ) -> Result<Value, PublicError> {
        self.signed(path, document)
    }
    /// Lookup the original signed owner's key and render docs.swarm_link, not a
    /// browser-supplied URL. Missing store rows return 404 swarm_unknown.
    fn link_svg(&self, _swarm_id: &str, _client: &str) -> Result<Vec<u8>, PublicError> {
        Err(PublicError::new(501, "directory_qr_not_integrated"))
    }
    /// Exact response bytes for GET routes. Services that must preserve the
    /// source's key order (serde maps sort keys) override this.
    fn get_body_for_client(&self, path: &str, client: &str) -> Result<Vec<u8>, PublicError> {
        encode(&self.get_for_client(path, client)?)
    }
    /// Exact response bytes for a framed POST. The default keeps the transport
    /// order (strict parse, then `signed_for_client`); a concrete authority
    /// overrides it to own the rate-limit-before-parse order of directory.py:108-119.
    fn post_for_client(
        &self,
        path: &str,
        body: &[u8],
        client: &str,
    ) -> Result<Vec<u8>, PublicError> {
        let document = directory_object(body)?;
        encode(&self.signed_for_client(path, &document, client)?)
    }
    /// server.py csp(allow_loopback): `http://127.0.0.1:*` joins connect-src.
    fn allow_loopback(&self) -> bool {
        false
    }
}
fn encode(value: &Value) -> Result<Vec<u8>, PublicError> {
    serde_json::to_vec(value).map_err(|_| PublicError::new(500, "response_encoding_failed"))
}
pub struct Directory<S: Service> {
    service: S,
}
impl<S: Service> Directory<S> {
    pub fn new(service: S) -> Self {
        Self { service }
    }
    pub fn service(&self) -> &S {
        &self.service
    }
    pub fn handle(&self, request: Request) -> Result<Reply, PublicError> {
        self.handle_client(request, "ip:unknown")
    }
    /// Proxy trust is listener configuration, not a caller-controlled header.
    pub fn handle_from(
        &self,
        request: Request,
        peer: IpAddr,
        trusted_proxy: bool,
    ) -> Result<Reply, PublicError> {
        let client = client_key(&request, peer, trusted_proxy);
        self.handle_client(request, &client)
    }
    pub fn preflight(&self, request: &Request) -> Result<Option<Reply>, PublicError> {
        match guard(request) {
            Ok(()) => Ok(None),
            Err(e) => self
                .decorate(Reply::error(e)?, request.method == "HEAD")
                .map(Some),
        }
    }
    fn handle_client(&self, request: Request, client: &str) -> Result<Reply, PublicError> {
        let head = request.method == "HEAD";
        let reply = match self.dispatch(request, client) {
            Ok(v) => v,
            Err(e) if e.is_dropped() => return Err(e),
            Err(e) => Reply::error(e)?,
        };
        self.decorate(reply, head)
    }
    fn decorate(&self, mut reply: Reply, head: bool) -> Result<Reply, PublicError> {
        reply
            .headers
            .push(("cross-origin-opener-policy".into(), "same-origin".into()));
        let connect = if self.service.allow_loopback() {
            "'self' https: http://127.0.0.1:*"
        } else {
            "'self' https:"
        };
        reply.headers.push((
            "content-security-policy".into(),
            format!("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src {connect}; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"),
        ));
        if head {
            reply
                .headers
                .push(("content-length".into(), reply.body.len().to_string()));
            reply.body.clear();
        }
        Ok(reply)
    }
    fn dispatch(&self, r: Request, client: &str) -> Result<Reply, PublicError> {
        guard(&r)?;

        if matches!(r.method.as_str(), "GET" | "HEAD")
            && let Some(reply) = crate::assets::directory(&r.target)
        {
            return Ok(reply);
        }
        let path = r
            .target
            .strip_prefix(PREFIX)
            .filter(|p| p.starts_with('/'))
            .ok_or(PublicError::new(404, "route_unknown"))?;
        if matches!(r.method.as_str(), "GET" | "HEAD") {
            if let Some(id) = path
                .strip_prefix("/swarms/")
                .and_then(|tail| tail.strip_suffix("/link.svg"))
                .filter(|id| swarm_id(id))
            {
                let mut reply = Reply::json(200, &Value::Null)?;
                reply.headers.retain(|(key, _)| key != "content-type");
                reply
                    .headers
                    .push(("content-type".into(), "image/svg+xml".into()));
                reply.body = self.service.link_svg(id, client)?;
                return Ok(reply);
            }
            if SIGNED_ROUTES.contains(&path) {
                return Err(PublicError::new(405, "method_not_allowed"));
            }
            if path == "/config"
                || path == "/swarms"
                || path.strip_prefix("/swarms/").is_some_and(swarm_id)
            {
                return Ok(Reply::json_bytes(
                    200,
                    self.service.get_body_for_client(path, client)?,
                ));
            }
            return Err(PublicError::new(404, "route_unknown"));
        }
        if r.method != "POST" {
            return Err(PublicError::new(501, "unsupported_method"));
        }

        // Native chunked framing remains a host adapter seam. At this transport-
        // free boundary body is already decoded; contradictory framing refuses.
        let lengths: Vec<_> = r.values("content-length").collect();
        let codings: Vec<_> = r.values("transfer-encoding").collect();
        if !codings.is_empty() {
            if codings.len() != 1
                || !codings[0].trim().eq_ignore_ascii_case("chunked")
                || !lengths.is_empty()
            {
                return Err(PublicError::new(400, "frame_invalid"));
            }
        } else if lengths.len() != 1
            || !lengths[0].bytes().all(|b| b.is_ascii_digit())
            || lengths[0].parse::<usize>() != Ok(r.body.len())
        {
            return Err(PublicError::new(400, "frame_invalid"));
        }
        if r.body.len() > 8192 {
            return Err(PublicError::new(413, "body_too_large"));
        }
        if !SIGNED_ROUTES.contains(&path) {
            return Err(PublicError::new(404, "route_unknown"));
        }
        Ok(Reply::json_bytes(
            200,
            self.service.post_for_client(path, &r.body, client)?,
        ))
    }
}
pub fn client_key(r: &Request, peer: IpAddr, trusted_proxy: bool) -> String {
    if !trusted_proxy {
        return format!("ip:{peer}");
    }
    let raw = r.header("cf-connecting-ip").unwrap_or("");
    if (2..=45).contains(&raw.len())
        && raw
            .bytes()
            .all(|b| b.is_ascii_hexdigit() || b == b'.' || b == b':')
    {
        format!("ip:{raw}")
    } else {
        "ip:unknown".into()
    }
}
fn guard(r: &Request) -> Result<(), PublicError> {
    if matches!(r.method.as_str(), "PUT" | "DELETE" | "PATCH" | "OPTIONS") {
        return Err(PublicError::new(405, "method_not_allowed"));
    }
    if r.target.contains("..")
        || r.target.contains(['%', '?', '#', '\\'])
        || (r.method == "POST" && !r.target.starts_with("/swarms/api/v1/"))
    {
        return Err(PublicError::new(404, "route_unknown"));
    }
    if r.method == "POST" {
        if r.header("x-mycelium-swarms") != Some("1")
            || r.header("content-type").is_none_or(|v| {
                v.split(';')
                    .next()
                    .is_none_or(|v| v.trim() != "application/json")
            })
        {
            return Err(PublicError::new(403, "header_required"));
        }
        if r.header("cookie").is_some() || r.header("authorization").is_some() {
            return Err(PublicError::new(400, "credentials_refused"));
        }
    }
    Ok(())
}
fn swarm_id(id: &str) -> bool {
    id.len() == 23
        && id.starts_with("sw-")
        && id[3..]
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
}
fn directory_object(raw: &[u8]) -> Result<Map<String, Value>, PublicError> {
    let value = unique_value(raw).map_err(|e| {
        let code = if e.to_string().starts_with("duplicate_key") {
            "json_duplicate_key"
        } else {
            "json_invalid"
        };
        PublicError::new(400, code)
    })?;
    validate_value(&value, 0)?;
    match value {
        Value::Object(v) => Ok(v),
        _ => Err(PublicError::new(400, "json_invalid")),
    }
}
fn validate_value(v: &Value, depth: usize) -> Result<(), PublicError> {
    let refusal = |code| PublicError::new(400, code);
    if depth > 6 {
        return Err(refusal("document_too_deep"));
    }
    match v {
        Value::Number(n) => {
            if n.is_f64() {
                return Err(refusal("json_float_refused"));
            }
            if !n
                .as_i64()
                .is_some_and(|n| (-9_007_199_254_740_991..=9_007_199_254_740_991).contains(&n))
            {
                return Err(refusal("integer_out_of_range"));
            }
        }
        Value::String(s) if s.chars().any(|c| c.is_control()) => {
            return Err(refusal("text_invalid"));
        }
        Value::Array(a) => {
            for v in a {
                validate_value(v, depth + 1)?;
            }
        }
        Value::Object(o) => {
            for (k, v) in o {
                if k.is_empty()
                    || k.len() > 40
                    || !k.as_bytes()[0].is_ascii_lowercase()
                    || !k
                        .bytes()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
                {
                    return Err(refusal("key_invalid"));
                }
                validate_value(v, depth + 1)?;
            }
        }
        _ => {}
    }
    Ok(())
}
