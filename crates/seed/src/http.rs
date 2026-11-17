// SPDX-License-Identifier: AGPL-3.0-only
//! The seed HTTP surface of v0.1.4 (`mycelium_seed/http.py` `_SeedRequestHandler` on stdlib
//! `http.server.BaseHTTPRequestHandler`, `protocol_version = "HTTP/1.1"`), byte for byte: request framing,
//! the error pages of `send_error`, the canonical-JSON ingress and the route table
//! `GET /seed/{identity,rotation}`, `POST /seed/{join,resume,message}`.
//!
//! Transport-agnostic: [`serve`] runs a connection over any reader/writer pair (the tests feed the executed
//! oracle's raw request bytes), [`serve_stream`] over one duplex stream (TCP or TLS). Not ported, by design,
//! because no member client sends them: HTTP/0.9 requests, `Expect: 100-continue`, the public-bootstrap
//! boundary policy, the rotation envelope, route assignment over `/seed/message` (answered
//! `seed_route_assignment_unported`).
use crate::coordinator::{Coordinator, SeedError, canonical_bytes};
use budgets::BudgetId;
use protocol::strict;
use serde_json::{Value, json};
use std::{
    io::{self, BufRead, BufReader, Read, Write},
    time::{SystemTime, UNIX_EPOCH},
};

/// Optional application-owned POST routes. Seed routes retain precedence.
/// Called after bounded canonical JSON ingress, with NO coordinator lock held.
/// `None` preserves the existing 404. Errors use the seed HTTP error envelope/status mapping.
/// An authenticated adapter must use `Coordinator::with_current_member` around its
/// bounded verification/commit, not read `member()` and dispatch later. Slow preparation
/// and external observers belong outside that guard. This trait grants no authority.
pub trait RouteExtension: Send + Sync {
    fn post(
        &self,
        coordinator: &Coordinator,
        path: &str,
        body: &Value,
    ) -> Option<Result<Value, SeedError>>;
    /// Called for a POST to a path the seed does not serve itself, after its request line and
    /// headers and BEFORE its body is read (v0.1.4 `CombinedSeedHandler.do_POST`,
    /// `mycelium_a13_macos/tls.py:438-441`, takes its slot first). `Ok(None)`: nothing to hold.
    /// `Ok(Some(ticket))`: the ticket is held until the answer is built. `Err`: answered at once
    /// with that error, the body unread and the connection closed.
    fn admit(&self, _path: &str) -> Result<Option<Ticket>, SeedError> {
        Ok(None)
    }
}
/// What [`RouteExtension::admit`] holds for one request; dropping it releases it.
pub type Ticket = Box<dyn std::any::Any + Send>;

pub const SEED_JOIN_HTTP_PROTOCOL: &str = "mycelium.seed.join_http.v1";
pub const SEED_RESUME_HTTP_PROTOCOL: &str = "mycelium.seed.resume_http.v1";
pub const SEED_MEMBER_HTTP_PROTOCOL: &str = "mycelium.seed.member_http.v1";
pub const SEED_HTTP_ERROR_PROTOCOL: &str = "mycelium.seed.http_error.v1";
const HEARTBEAT_PROTOCOL: &str = "mycelium.membership.heartbeat.v1";
const ROUTE_ASSIGNMENT_REQUEST_PROTOCOL: &str = "mycelium.route_assignment.request.v1";
/// `http.client._MAXLINE` and `_MAXHEADERS` (stdlib, not v0.1.4 source constants).
const STDLIB_MAX_LINE: usize = 65_536;
const STDLIB_MAX_HEADERS: usize = 100;
const SERVER_VERSION: &str = "MyceliumSeed/1 ";

/// `JOIN_ROUTE_ERROR_STATUSES` (`http.py:70-110`): the only codes the join route exposes with their status.
const JOIN_ROUTE_ERROR_STATUSES: &[(&str, u16)] = &[
    ("invite_expired", 400),
    ("invite_malformed", 400),
    ("invite_protocol_invalid", 400),
    ("invite_replayed", 409),
    ("invite_signature_invalid", 401),
    ("join_request_protocol_required", 400),
    ("membership_endpoint_addr_invalid", 400),
    ("membership_endpoint_id_mismatch", 400),
    ("membership_envelope_invalid", 400),
    ("membership_field_unusable", 400),
    ("membership_fields_invalid", 400),
    ("membership_generation_invalid", 400),
    ("membership_identifier_invalid", 400),
    ("membership_join_generation_invalid", 400),
    ("membership_key_pin_invalid", 401),
    ("membership_message_expired", 400),
    ("membership_message_from_future", 400),
    ("membership_message_invalid", 400),
    ("membership_contract_upgrade_required", 426),
    ("membership_peer_class_invalid", 400),
    ("membership_peer_profile_binding_mismatch", 400),
    ("membership_peer_profile_invalid", 400),
    ("membership_v2_before_cutover", 400),
    ("membership_v2_peer_profile_invalid", 400),
    ("membership_v2_profile_generation_mismatch", 400),
    ("membership_v2_profile_has_no_consumer_projection", 400),
    ("membership_protocol_invalid", 400),
    ("membership_runtime_capability_invalid", 400),
    ("membership_runtime_capability_mismatch", 400),
    ("membership_signature_invalid", 401),
    ("membership_text_invalid", 400),
    ("membership_time_invalid", 400),
    ("membership_ttl_invalid", 400),
    ("membership_verifier_invalid", 400),
    ("seed_join_key_invalid", 400),
    ("seed_join_mismatch", 400),
    ("seed_join_retry_mismatch", 400),
    ("seed_member_identity_reused", 400),
    ("seed_node_endpoint_conflict", 400),
    ("seed_node_key_conflict", 409),
];

fn join_status(code: &str) -> Option<u16> {
    JOIN_ROUTE_ERROR_STATUSES
        .iter()
        .find(|(c, _)| *c == code)
        .map(|(_, s)| *s)
}

/// `_error_status`.
fn error_status(code: &str) -> u16 {
    if let Some(status) = join_status(code) {
        return status;
    }
    if code == "seed_http_frame_too_large" {
        return 413;
    }
    if matches!(
        code,
        "invite_replayed"
            | "seed_node_key_conflict"
            | "seed_assignment_exists"
            | "seed_message_replayed"
    ) {
        return 409;
    }
    if matches!(code, "seed_member_unknown" | "seed_assignment_unknown") {
        return 404;
    }
    // The combined seed handler's managed-control capacity refusal (v0.1.4
    // `mycelium_a13_macos/tls.py:438-441`; owner ruling 16). Only this code.
    if code == "managed_control_capacity_exhausted" {
        return 503;
    }
    if code.contains("signature") || code.contains("key_pin") {
        return 401;
    }
    400
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Content Too Large",
        426 => "Upgrade Required",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        503 => "Service Unavailable",
        _ => "Unknown",
    }
}

struct Fail {
    code: String,
    status: Option<u16>,
}

impl Fail {
    fn new(code: &str) -> Self {
        Self {
            code: code.to_owned(),
            status: None,
        }
    }
    fn with_status(code: &str, status: u16) -> Self {
        Self {
            code: code.to_owned(),
            status: Some(status),
        }
    }
    /// A coordinator error on a route that answers `getattr(exc, "code", "seed_http_request_invalid")`.
    fn from_seed(error: &SeedError) -> Self {
        Self::new(if error.accident {
            "seed_http_request_invalid"
        } else {
            &error.code
        })
    }
}

fn internal(detail: &str) -> Fail {
    // The wire code for an unlisted failure is deliberately generic, which hides the cause from
    // the peer entirely. Leave the bytes alone and record the fact that this path fired, plus the
    // underlying code, so the owner's log can name what actually went wrong.
    eprintln!("seed_http_internal_fired");
    eprintln!("{}", internal_probe(detail));
    Fail::with_status("seed_http_internal_error", 500)
}

/// The probe line that carries the real cause past a deliberately generic wire code. Kept pure so a
/// test can pin its shape, and free of newlines so it cannot forge a second log line.
pub fn internal_probe(detail: &str) -> String {
    let detail = detail.replace(['\n', '\r'], " ");
    format!("l11_seed_internal_detail:{detail}")
}

/// One response: status, extra-header flavour and body.
struct Reply {
    status: u16,
    phrase: String,
    json: bool,
    close: bool,
    body: Vec<u8>,
}

fn json_reply(status: u16, value: &Value) -> Reply {
    match canonical_bytes(value) {
        Ok(body) => Reply {
            status,
            phrase: reason(status).to_owned(),
            json: true,
            close: false,
            body,
        },
        Err(_) => json_reply(500, &error_body("seed_http_internal_error")),
    }
}

fn error_body(code: &str) -> Value {
    json!({"protocol": SEED_HTTP_ERROR_PROTOCOL, "error": {"code": code}})
}

fn fail_reply(fail: &Fail) -> Reply {
    json_reply(
        fail.status.unwrap_or_else(|| error_status(&fail.code)),
        &error_body(&fail.code),
    )
}

fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// `BaseHTTPRequestHandler.send_error`: the HTML error page; the connection is closed.
fn error_page(code: u16, message: &str, explain: &str) -> Reply {
    let body = format!(
        "<!DOCTYPE HTML>\n<html lang=\"en\">\n    <head>\n        <meta charset=\"utf-8\">\n        \
         <style type=\"text/css\">\n            :root {{\n                color-scheme: light dark;\n            }}\n        \
         </style>\n        <title>Error response</title>\n    </head>\n    <body>\n        \
         <h1>Error response</h1>\n        <p>Error code: {code}</p>\n        <p>Message: {}.</p>\n        \
         <p>Error code explanation: {code} - {}.</p>\n    </body>\n</html>\n",
        escape(message),
        escape(explain)
    );
    Reply {
        status: code,
        phrase: message.to_owned(),
        json: false,
        close: true,
        body: body.into_bytes(),
    }
}

/// Python `repr(str)` for the printable request lines this is used on.
fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `email.utils.formatdate(usegmt=True)` for the wall clock.
fn http_date() -> String {
    const DAYS: [&str; 7] = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"];
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let (days, rest) = (seconds / 86_400, seconds % 86_400);
    // civil_from_days (Howard Hinnant)
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{}, {day:02} {} {year} {:02}:{:02}:{:02} GMT",
        DAYS[(days % 7) as usize],
        MONTHS[(month - 1) as usize],
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}

fn render(reply: &Reply) -> Vec<u8> {
    let mut head = format!(
        "HTTP/1.1 {} {}\r\nServer: {SERVER_VERSION}\r\nDate: {}\r\n",
        reply.status,
        reply.phrase,
        http_date()
    );
    if reply.json {
        head.push_str("Content-Type: application/json\r\n");
    } else {
        head.push_str("Connection: close\r\nContent-Type: text/html;charset=utf-8\r\n");
    }
    head.push_str(&format!("Content-Length: {}\r\n", reply.body.len()));
    if reply.json {
        head.push_str("Cache-Control: no-store\r\n");
    }
    head.push_str("\r\n");
    let mut out = head.into_bytes();
    out.extend_from_slice(&reply.body);
    out
}

struct Request {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    close: bool,
}

impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
    /// `Message.get_content_type()`: the lower-cased `maintype/subtype` before any parameter, else
    /// `text/plain`.
    fn content_type(&self) -> String {
        let Some(raw) = self.header("Content-Type") else {
            return "text/plain".to_owned();
        };
        let main = raw
            .split(';')
            .next()
            .unwrap_or_default()
            .trim()
            .to_lowercase();
        if main.matches('/').count() == 1 {
            main
        } else {
            "text/plain".to_owned()
        }
    }
}

/// Python `int(text)` for decimal text: surrounding whitespace, one optional sign, digits with single
/// underscores between them. `None` when Python would raise `ValueError`.
fn py_int(text: &str) -> Option<i128> {
    let t = text.trim();
    let (negative, digits) = match t.as_bytes().first()? {
        b'-' => (true, &t[1..]),
        b'+' => (false, &t[1..]),
        _ => (false, t),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
        || !digits.bytes().all(|b| b.is_ascii_digit() || b == b'_')
    {
        return None;
    }
    let clean: String = digits.chars().filter(|c| *c != '_').collect();
    let magnitude = clean.parse::<i128>().unwrap_or(i128::MAX);
    Some(if negative { -magnitude } else { magnitude })
}

enum Parsed {
    Request(Request),
    Reply(Reply),
    Silent,
}

fn read_line<R: BufRead>(reader: &mut R) -> io::Result<(Vec<u8>, bool)> {
    let mut line = Vec::new();
    reader
        .by_ref()
        .take(STDLIB_MAX_LINE as u64 + 1)
        .read_until(b'\n', &mut line)?;
    let too_long = line.len() > STDLIB_MAX_LINE;
    Ok((line, too_long))
}

const SYNTAX_EXPLAIN: &str = "Bad request syntax or unsupported method";

/// `handle_one_request` up to and including `parse_request`.
fn parse_request<R: BufRead>(reader: &mut R) -> io::Result<Parsed> {
    let (raw, too_long) = read_line(reader)?;
    if too_long {
        return Ok(Parsed::Reply(error_page(
            414,
            "URI Too Long",
            "URI is too long",
        )));
    }
    if raw.is_empty() {
        return Ok(Parsed::Silent);
    }
    let line: String = raw.iter().map(|b| char::from(*b)).collect();
    let line = line.trim_end_matches(['\r', '\n']).to_owned();
    let words: Vec<&str> = line.split_whitespace().collect();
    if words.is_empty() {
        return Ok(Parsed::Silent);
    }
    let mut close = true;
    if words.len() >= 3 {
        let version = words[words.len() - 1];
        let number = version
            .strip_prefix("HTTP/")
            .map(|v| v.split('.').collect::<Vec<_>>())
            .filter(|parts| {
                parts.len() == 2
                    && parts.iter().all(|p| {
                        !p.is_empty() && p.len() <= 10 && p.bytes().all(|b| b.is_ascii_digit())
                    })
            });
        let Some(parts) = number else {
            return Ok(Parsed::Reply(error_page(
                400,
                &format!("Bad request version ({})", py_repr(version)),
                SYNTAX_EXPLAIN,
            )));
        };
        let (major, minor) = (
            parts[0].parse::<u32>().unwrap_or(u32::MAX),
            parts[1].parse::<u32>().unwrap_or(u32::MAX),
        );
        if (major, minor) >= (1, 1) {
            close = false;
        }
        if major >= 2 {
            return Ok(Parsed::Reply(error_page(
                505,
                &format!("Invalid HTTP version ({}.{})", parts[0], parts[1]),
                "Cannot fulfill request",
            )));
        }
    }
    if !(2..=3).contains(&words.len()) {
        return Ok(Parsed::Reply(error_page(
            400,
            &format!("Bad request syntax ({})", py_repr(&line)),
            SYNTAX_EXPLAIN,
        )));
    }
    if words.len() == 2 {
        return Ok(Parsed::Reply(error_page(
            400,
            &format!("Bad HTTP/0.9 request type ({})", py_repr(words[0])),
            SYNTAX_EXPLAIN,
        )));
    }
    let method = words[0].to_owned();
    let mut path = words[1].to_owned();
    if path.starts_with("//") {
        path = format!("/{}", path.trim_start_matches('/'));
    }
    let mut headers: Vec<(String, String)> = Vec::new();
    let mut count = 0;
    loop {
        let (raw, too_long) = read_line(reader)?;
        if too_long {
            return Ok(Parsed::Reply(error_page(
                431,
                "Line too long",
                &format!("got more than {STDLIB_MAX_LINE} bytes when reading header line"),
            )));
        }
        if raw.is_empty() || raw == b"\r\n" || raw == b"\n" {
            break;
        }
        count += 1;
        if count > STDLIB_MAX_HEADERS {
            return Ok(Parsed::Reply(error_page(
                431,
                "Too many headers",
                &format!("got more than {STDLIB_MAX_HEADERS} headers"),
            )));
        }
        let text: String = raw.iter().map(|b| char::from(*b)).collect();
        let text = text.trim_end_matches(['\r', '\n']);
        if text.starts_with([' ', '\t']) {
            if let Some(last) = headers.last_mut() {
                last.1.push(' ');
                last.1.push_str(text.trim());
            }
        } else if let Some((name, value)) = text.split_once(':') {
            headers.push((name.to_owned(), value.trim().to_owned()));
        }
    }
    let connection = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("Connection"))
        .map_or(String::new(), |(_, v)| v.to_lowercase());
    if connection == "close" {
        close = true;
    } else if connection == "keep-alive" {
        close = false;
    }
    Ok(Parsed::Request(Request {
        method,
        path,
        headers,
        close,
    }))
}

fn frame_limit() -> Result<i128, Fail> {
    BudgetId::PyMyceliumSeedHttpMaxHttpFrameBytesL55
        .bytes()
        .map(|b| i128::from(b.get()))
        .map_err(|_| internal("frame_limit"))
}

/// `_read_body`.
fn read_body<R: BufRead>(
    request: &Request,
    reader: &mut R,
    close: &mut bool,
) -> Result<Value, Fail> {
    if request.header("Transfer-Encoding").is_some() {
        return Err(Fail::new("seed_http_transfer_encoding_unsupported"));
    }
    if request.content_type() != "application/json" {
        return Err(Fail::new("seed_http_content_type_invalid"));
    }
    let Some(raw_length) = request.header("Content-Length") else {
        return Err(Fail::new("seed_http_content_length_required"));
    };
    let Some(length) = py_int(raw_length) else {
        return Err(Fail::new("seed_http_content_length_invalid"));
    };
    if length < 0 {
        return Err(Fail::new("seed_http_content_length_invalid"));
    }
    if length > frame_limit()? {
        *close = true;
        return Err(Fail::new("seed_http_frame_too_large"));
    }
    let wanted = usize::try_from(length).map_err(|_| internal("content_length"))?;
    let mut raw = Vec::with_capacity(wanted);
    reader
        .by_ref()
        .take(wanted as u64)
        .read_to_end(&mut raw)
        .map_err(|_| internal("body_read"))?;
    if raw.len() != wanted {
        return Err(Fail::new("seed_http_body_truncated"));
    }
    let value = strict::canonical_value(&raw, wanted.max(1)).map_err(|e| match e.0 {
        "frame_noncanonical" => Fail::new("seed_http_noncanonical"),
        _ => Fail::new("seed_http_json_invalid"),
    })?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(Fail::new("seed_http_body_invalid"))
    }
}

fn has_exactly(body: &Value, keys: &[&str]) -> bool {
    body.as_object()
        .is_some_and(|m| m.len() == keys.len() && keys.iter().all(|k| m.contains_key(*k)))
}

fn post_route(
    coordinator: &Coordinator,
    routes: Option<&dyn RouteExtension>,
    request: &Request,
    body: &Value,
) -> Result<Value, Fail> {
    match request.path.as_str() {
        "/seed/join" => {
            if !has_exactly(body, &["protocol", "invite_token", "join_envelope"]) {
                return Err(Fail::new("seed_http_body_invalid"));
            }
            if body["protocol"] != SEED_JOIN_HTTP_PROTOCOL {
                return Err(Fail::new("seed_http_protocol_invalid"));
            }
            coordinator
                .accept_join_value(&body["invite_token"], &body["join_envelope"])
                .map_err(|e| {
                    // `accident` means the coordinator hit an internal fault; the wire must stay
                    // generic, but throwing away `e.code` here is what made step 7 unreachable:
                    // the owner's log said only `managed_control_internal_error`.
                    if e.accident {
                        return internal(&e.code);
                    }
                    match join_status(&e.code) {
                        Some(status) => Fail::with_status(&e.code, status),
                        None => internal(&e.code),
                    }
                })
        }
        "/seed/resume" => {
            if !has_exactly(body, &["protocol", "resume_envelope"]) {
                return Err(Fail::new("seed_http_body_invalid"));
            }
            if body["protocol"] != SEED_RESUME_HTTP_PROTOCOL {
                return Err(Fail::new("seed_http_protocol_invalid"));
            }
            coordinator
                .accept_resume(&body["resume_envelope"])
                .map_err(|e| Fail::from_seed(&e))
        }
        "/seed/message" => {
            if !has_exactly(body, &["protocol", "envelope"]) {
                return Err(Fail::new("seed_http_body_invalid"));
            }
            if body["protocol"] != SEED_MEMBER_HTTP_PROTOCOL {
                return Err(Fail::new("seed_http_protocol_invalid"));
            }
            let envelope = &body["envelope"];
            let Some(expected) = envelope
                .as_object()
                .and_then(|e| e.get("message"))
                .and_then(Value::as_object)
                .and_then(|m| m.get("protocol"))
            else {
                return Err(Fail::new("seed_http_body_invalid"));
            };
            let expected = expected.as_str().unwrap_or_default();
            if expected == ROUTE_ASSIGNMENT_REQUEST_PROTOCOL {
                return Err(Fail::with_status("seed_route_assignment_unported", 501));
            }
            let message = coordinator
                .receive_member_message(envelope, expected)
                .map_err(|e| Fail::from_seed(&e))?;
            let message_id = message["message_id"].as_str().unwrap_or_default();
            if expected == HEARTBEAT_PROTOCOL {
                coordinator
                    .lease_renewal(
                        message["sender_node_id"].as_str().unwrap_or_default(),
                        message_id,
                    )
                    .map_err(|e| Fail::from_seed(&e))
            } else {
                coordinator
                    .receipt_envelope(message_id)
                    .map_err(|e| Fail::from_seed(&e))
            }
        }
        _ => routes
            .and_then(|r| r.post(coordinator, &request.path, body))
            .map(|result| result.map_err(|e| Fail::from_seed(&e)))
            .unwrap_or_else(|| Err(Fail::with_status("seed_http_route_unknown", 404))),
    }
}

fn handle<R: BufRead>(
    coordinator: &Coordinator,
    routes: Option<&dyn RouteExtension>,
    request: &Request,
    reader: &mut R,
    close: &mut bool,
) -> Reply {
    match request.method.as_str() {
        "GET" => match request.path.as_str() {
            "/seed/rotation" => match coordinator.rotation_envelope() {
                Some(envelope) => json_reply(200, &envelope),
                None => fail_reply(&Fail::with_status("seed_rotation_absent", 404)),
            },
            "/seed/identity" => match coordinator.identity_envelope() {
                Ok(envelope) => json_reply(200, &envelope),
                Err(e) => fail_reply(&Fail::from_seed(&e)),
            },
            _ => fail_reply(&Fail::with_status("seed_http_route_unknown", 404)),
        },
        "POST" => {
            let seed_route = matches!(
                request.path.as_str(),
                "/seed/join" | "/seed/resume" | "/seed/message"
            );
            let _ticket = match routes.filter(|_| !seed_route) {
                Some(routes) => match routes.admit(&request.path) {
                    Ok(ticket) => ticket,
                    Err(error) => {
                        // The body was never read: nothing else can follow on this connection.
                        *close = true;
                        return fail_reply(&Fail::from_seed(&error));
                    }
                },
                None => None,
            };
            let outcome = read_body(request, reader, close)
                .and_then(|body| post_route(coordinator, routes, request, &body));
            match outcome {
                Ok(document) => json_reply(200, &document),
                Err(fail) => fail_reply(&fail),
            }
        }
        other => error_page(
            501,
            &format!("Unsupported method ({})", py_repr(other)),
            "Server does not support this operation",
        ),
    }
}

/// One request/response exchange; `None` when the peer closed (or sent only blank lines).
fn exchange<R: BufRead>(
    coordinator: &Coordinator,
    routes: Option<&dyn RouteExtension>,
    reader: &mut R,
) -> io::Result<Option<(Vec<u8>, bool)>> {
    match parse_request(reader)? {
        Parsed::Silent => Ok(None),
        Parsed::Reply(reply) => Ok(Some((render(&reply), true))),
        Parsed::Request(request) => {
            let mut close = request.close;
            let reply = handle(coordinator, routes, &request, reader, &mut close);
            let close = close || reply.close;
            Ok(Some((render(&reply), close)))
        }
    }
}

/// Serve one connection from a reader/writer pair until the peer closes or a response closes it.
pub fn serve<R: Read, W: Write>(
    coordinator: &Coordinator,
    reader: R,
    mut writer: W,
) -> io::Result<()> {
    let mut reader = BufReader::new(reader);
    while let Some((bytes, close)) = exchange(coordinator, None, &mut reader)? {
        writer.write_all(&bytes)?;
        writer.flush()?;
        if close {
            break;
        }
    }
    Ok(())
}

/// Serve one connection over a duplex stream (TCP, TLS).
pub fn serve_stream<S: Read + Write>(coordinator: &Coordinator, stream: &mut S) -> io::Result<()> {
    serve_stream_with_routes(coordinator, stream, None)
}

/// Existing duplex transport with an optional application extension. Socket I/O
/// and response serialization happen outside any authority guard held by the adapter.
pub fn serve_stream_with_routes<S: Read + Write>(
    coordinator: &Coordinator,
    stream: &mut S,
    routes: Option<&dyn RouteExtension>,
) -> io::Result<()> {
    let mut reader = BufReader::new(&mut *stream);
    while let Some((bytes, close)) = exchange(coordinator, routes, &mut reader)? {
        reader.get_mut().write_all(&bytes)?;
        reader.get_mut().flush()?;
        if close {
            break;
        }
    }
    Ok(())
}

#[cfg(test)]
mod internal_probe_tests {
    use super::internal_probe;

    #[test]
    fn the_probe_names_the_underlying_code() {
        let line = internal_probe("seed_invite_expired");
        assert_eq!(line, "l11_seed_internal_detail:seed_invite_expired");
    }

    #[test]
    fn the_probe_cannot_forge_a_second_log_line() {
        let line = internal_probe("boom\nseed_http_internal_fired");
        assert!(
            !line.contains('\n'),
            "a detail must not break the log line: {line}"
        );
    }
}
