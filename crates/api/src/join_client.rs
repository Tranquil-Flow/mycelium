// SPDX-License-Identifier: AGPL-3.0-only
//! The member's HTTPS client for one owner's pairing window and link probe (`JoinWindowClient`,
//! `mycelium_a13_macos/pairing.py:833-917` @ f4e366e9): canonical `https://` origin, system Web-PKI trust,
//! no redirects, the Mycelium `User-Agent` (Cloudflare's Browser Integrity Check answers 1010 to anything
//! else), the owner's closed error code surfaced as the library code, and the transport failure classes
//! `pairing_window_dns_failed` / `pairing_window_tls_failed` / `pairing_window_timeout`.
//! `with_test_base_url` is the oracle's `_test_base_url` seam (identical requests to a loopback relay).
//! Not ported: the Cloudflare DNS-over-HTTPS fallback of `public_opener` for `*.trycloudflare.com`.
use crate::{
    join_link::canonical_https_origin,
    pairing_windows::{
        JOIN_LINK_SCHEME, PAIRING_WINDOW_PREFIX, POLL_PROTOCOL, SUBMISSION_PROTOCOL,
        error_code_shape,
    },
    schema::{PublicError, Raised},
};
use budgets::{BudgetId, Entries};
use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};
use rustls_pki_types::ServerName;
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpStream, ToSocketAddrs},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

/// `USER_AGENT` of `mycelium_http_identity` (PV18).
pub const USER_AGENT: &str = "Mycelium/0.1.0 (+https://mycelium.now)";
/// `CLOCK_SKEW_WARNING_SECONDS` (pairing.py:553): the advisory threshold (the hard limit is 30 s).
fn clock_skew_warning_seconds() -> i64 {
    BudgetId::PyMyceliumA13MacosPairingClockSkewWarningSecondsL466
        .span(Entries::new(0))
        .map_or(i64::MAX, |s| {
            i64::try_from(s.millis() / 1000).unwrap_or(i64::MAX)
        })
}
/// `JoinWindowClient(timeout=10.0)` (pairing.py:843), from the budget registry.
pub fn default_timeout() -> Duration {
    BudgetId::PyMyceliumA13MacosPairingJoinwindowclientInitTimeoutL843
        .span(Entries::new(0))
        .map_or(Duration::ZERO, |s| Duration::from_millis(s.millis()))
}
/// `response.read(1024 * 1024 + 1)` (pairing.py:884).
const READ_LIMIT: usize = 1024 * 1024 + 1;
/// `HTTPError` bodies are read up to 4096 bytes (pairing.py:890).
const ERROR_BODY_LIMIT: usize = 4096;
/// Distinct remote codes kept alive as `&'static str`; a hostile owner cannot grow it past this.
const CODE_TABLE_LIMIT: usize = 256;

fn fail(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(Raised::Pairing)
}
/// A remote error code as `&'static str` (each distinct code is interned once, bounded).
fn intern(code: &str) -> &'static str {
    use std::collections::HashSet;
    static CODES: Mutex<Option<HashSet<&'static str>>> = Mutex::new(None);
    let mut guard = CODES
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let table = guard.get_or_insert_with(HashSet::new);
    if let Some(known) = table.get(code) {
        return known;
    }
    if table.len() >= CODE_TABLE_LIMIT {
        return "pairing_window_unreachable";
    }
    let leaked: &'static str = Box::leak(code.to_owned().into_boxed_str());
    table.insert(leaked);
    leaked
}

enum Base {
    /// The canonical origin over HTTPS with system roots.
    Public { host: String, port: u16 },
    /// `_test_base_url`: `http://127.0.0.1:<port>`, same requests, no TLS.
    Loopback(SocketAddr),
}
/// `JoinWindowClient`.
pub struct JoinWindowClient {
    origin: String,
    base: Base,
    timeout: Duration,
    tls: Option<Arc<ClientConfig>>,
    clock: Box<dyn Fn() -> f64 + Send + Sync>,
    warnings: Vec<Value>,
}

fn system_tls() -> Arc<ClientConfig> {
    let mut roots = RootCertStore::empty();
    // Roots that fail to parse are skipped, as the system store does.
    roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map(|b| b.with_root_certificates(roots).with_no_client_auth());
    Arc::new(config.unwrap_or_else(|_| {
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_protocol_versions(&[&rustls::version::TLS13])
            .map(|b| {
                b.with_root_certificates(RootCertStore::empty())
                    .with_no_client_auth()
            })
            .expect("tls13 is always available")
    }))
}

impl JoinWindowClient {
    /// `JoinWindowClient(origin, timeout=...)`: the canonical-origin check is `pairing_window_origin_invalid`.
    pub fn new(origin: &str, timeout: Duration) -> Result<Self, PublicError> {
        let origin = canonical_https_origin(&Value::String(origin.to_owned()))
            .ok()
            .filter(|c| c == origin)
            .ok_or_else(|| fail("pairing_window_origin_invalid"))?;
        let netloc = origin.trim_start_matches("https://");
        let (host, port) = match netloc.rsplit_once(':') {
            Some((h, p)) => (h.to_owned(), p.parse().unwrap_or(443)),
            None => (netloc.to_owned(), 443),
        };
        Ok(Self {
            origin,
            base: Base::Public { host, port },
            timeout,
            tls: None,
            clock: Box::new(|| {
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_or(0.0, |d| d.as_secs_f64())
            }),
            warnings: Vec::new(),
        })
    }
    /// `_test_base_url`: `http://127.0.0.1:<port>` only, else `ValueError` ("test base url is invalid").
    pub fn with_test_base_url(mut self, base_url: &str) -> Result<Self, PublicError> {
        let port = base_url
            .strip_prefix("http://127.0.0.1:")
            .filter(|p| (1..=5).contains(&p.len()) && p.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|p| p.parse::<u16>().ok())
            .ok_or_else(|| {
                PublicError::new(409, "action_not_completed")
                    .raised_by(Raised::Builtin("ValueError"))
            })?;
        self.base = Base::Loopback(SocketAddr::from(([127, 0, 0, 1], port)));
        Ok(self)
    }
    /// Trust these PEM roots instead of the system store (a test relay with its own CA; v0.1.4 has no such
    /// switch, the product always uses the system roots).
    #[must_use]
    pub fn with_tls_roots_pem(mut self, ca_pem: &[u8]) -> Self {
        use rustls_pki_types::{CertificateDer, pem::PemObject};
        let mut roots = RootCertStore::empty();
        roots.add_parsable_certificates(
            CertificateDer::pem_slice_iter(ca_pem).filter_map(Result::ok),
        );
        self.tls =
            ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .ok()
                .map(|b| Arc::new(b.with_root_certificates(roots).with_no_client_auth()));
        self
    }
    /// The clock the `Date` skew advisory compares against (`_clock`).
    #[must_use]
    pub fn with_clock(mut self, clock: Box<dyn Fn() -> f64 + Send + Sync>) -> Self {
        self.clock = clock;
        self
    }
    pub fn origin(&self) -> &str {
        &self.origin
    }
    /// Advisory only (never blocks a join): `[{"code": "clock_skew", "skew_seconds": n}]`.
    pub fn warnings(&self) -> &[Value] {
        &self.warnings
    }

    /// `_note_clock`: the HTTPS `Date` against this Mac's clock.
    fn note_clock(&mut self, date: Option<&str>) {
        let Some(date) = date.and_then(parse_http_date) else {
            return;
        };
        let skew = (date - (self.clock)()).round_ties_even() as i64;
        self.warnings = if skew.abs() > clock_skew_warning_seconds() {
            vec![json!({"code": "clock_skew", "skew_seconds": skew})]
        } else {
            Vec::new()
        };
    }

    /// One POST; the owner's body, or the closed code the owner refused with.
    pub fn post(
        &mut self,
        path: &str,
        body: &[u8],
        content_type: &str,
    ) -> Result<Vec<u8>, PublicError> {
        let (status, date, raw) = self.exchange(path, body, content_type)?;
        self.note_clock(date.as_deref());
        if (200..300).contains(&status) {
            return Ok(raw);
        }
        // `HTTPError`: the owner's `error` (a string, or a `{"code": ...}` object) is the code.
        let parsed = serde_json::from_slice::<Value>(&raw[..raw.len().min(ERROR_BODY_LIMIT)]).ok();
        let code = parsed.and_then(|v| match v.get("error") {
            Some(Value::String(s)) => Some(s.clone()),
            Some(Value::Object(o)) if o.len() == 1 => {
                o.get("code").and_then(Value::as_str).map(str::to_owned)
            }
            _ => None,
        });
        Err(fail(match code {
            Some(c) if error_code_shape(&c) => intern(&c),
            _ => "pairing_window_unreachable",
        }))
    }

    fn exchange(
        &mut self,
        path: &str,
        body: &[u8],
        content_type: &str,
    ) -> Result<(u16, Option<String>, Vec<u8>), PublicError> {
        let deadline = Instant::now() + self.timeout;
        let (address, authority, secure) = match &self.base {
            Base::Loopback(address) => (vec![*address], address.to_string(), false),
            Base::Public { host, port } => {
                let resolved = (host.as_str(), *port)
                    .to_socket_addrs()
                    .map_err(|_| fail("pairing_window_dns_failed"))?
                    .collect::<Vec<_>>();
                if resolved.is_empty() {
                    return Err(fail("pairing_window_dns_failed"));
                }
                let authority = if *port == 443 {
                    host.clone()
                } else {
                    format!("{host}:{port}")
                };
                (resolved, authority, true)
            }
        };
        let timeout = || fail("pairing_window_timeout");
        let mut last = None;
        let mut tcp = None;
        for candidate in &address {
            match TcpStream::connect_timeout(candidate, self.timeout) {
                Ok(stream) => {
                    tcp = Some(stream);
                    break;
                }
                Err(e) => last = Some(e),
            }
        }
        let Some(tcp) = tcp else {
            return Err(match last.map(|e| e.kind()) {
                Some(std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock) => timeout(),
                _ => fail("pairing_window_unreachable"),
            });
        };
        tcp.set_read_timeout(Some(self.timeout))
            .and_then(|()| tcp.set_write_timeout(Some(self.timeout)))
            .map_err(|_| fail("pairing_window_unreachable"))?;
        let head = format!(
            "POST {path} HTTP/1.1\r\nHost: {authority}\r\nAccept-Encoding: identity\r\nUser-Agent: {USER_AGENT}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        );
        let mut wire = head.into_bytes();
        wire.extend_from_slice(body);
        let reply = if secure {
            let Base::Public { host, .. } = &self.base else {
                return Err(fail("pairing_window_unreachable"));
            };
            let tls = self.tls.get_or_insert_with(system_tls).clone();
            let name = ServerName::try_from(host.clone())
                .map_err(|_| fail("pairing_window_tls_failed"))?;
            let connection =
                ClientConnection::new(tls, name).map_err(|_| fail("pairing_window_tls_failed"))?;
            let mut stream = StreamOwned::new(connection, tcp);
            roundtrip(&mut stream, &wire, deadline, true)?
        } else {
            let mut stream = tcp;
            roundtrip(&mut stream, &wire, deadline, false)?
        };
        parse_response(&reply)
    }

    fn json(&mut self, path: &str, document: &Value) -> Result<Value, PublicError> {
        let raw = self.post(path, &protocol_compact(document), "application/json")?;
        match serde_json::from_slice::<Value>(&raw) {
            Ok(value) if value.is_object() => Ok(value),
            _ => Err(fail("pairing_window_response_invalid")),
        }
    }
    /// `submit(window_id, code=, request=, report=, quota=)`.
    pub fn submit(
        &mut self,
        window_id: &str,
        code: &str,
        request: &Value,
        report: &Value,
        quota: &Value,
    ) -> Result<Value, PublicError> {
        self.json(
            &format!("{PAIRING_WINDOW_PREFIX}{window_id}/request"),
            &json!({
                "protocol": SUBMISSION_PROTOCOL, "code": code, "request": request,
                "report": report, "quota": quota,
            }),
        )
    }
    /// `poll(window_id, code=, digest=)`.
    pub fn poll(
        &mut self,
        window_id: &str,
        code: &str,
        digest: &str,
    ) -> Result<Value, PublicError> {
        self.json(
            &format!("{PAIRING_WINDOW_PREFIX}{window_id}/poll"),
            &json!({"protocol": POLL_PROTOCOL, "code": code, "request_digest": digest}),
        )
    }
    /// `probe(download_kib, upload)`: the owner's `/link-probe/v1/<kib>` answer, raw.
    pub fn probe(&mut self, download_kib: u64, upload: &[u8]) -> Result<Vec<u8>, PublicError> {
        self.post(
            &format!("{}/{}", crate::capability::LINK_PROBE_PATH, download_kib),
            upload,
            "application/octet-stream",
        )
    }
    /// `member_report(report)`.
    pub fn member_report(&mut self, report: &Value) -> Result<Value, PublicError> {
        self.json(crate::capability::MEMBER_REPORT_PATH, report)
    }
}

/// `json.dumps(document, separators=(",", ":"))`, keys in the document's own order.
fn protocol_compact(document: &Value) -> Vec<u8> {
    crate::wire::Json::from(document).dumps(crate::wire::Text::Ascii)
}

fn roundtrip<S: Read + Write>(
    stream: &mut S,
    wire: &[u8],
    deadline: Instant,
    secure: bool,
) -> Result<Vec<u8>, PublicError> {
    let class = |e: &std::io::Error| -> PublicError {
        match e.kind() {
            std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock => {
                fail("pairing_window_timeout")
            }
            std::io::ErrorKind::InvalidData if secure => fail("pairing_window_tls_failed"),
            _ if secure && e.get_ref().is_some_and(|inner| inner.is::<rustls::Error>()) => {
                fail("pairing_window_tls_failed")
            }
            _ => fail("pairing_window_unreachable"),
        }
    };
    stream
        .write_all(wire)
        .and_then(|()| stream.flush())
        .map_err(|e| class(&e))?;
    let mut reply = Vec::new();
    let mut chunk = [0u8; 16 * 1024];
    let cap = READ_LIMIT + 16 * 1024;
    loop {
        if Instant::now() > deadline {
            return Err(fail("pairing_window_timeout"));
        }
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                reply.extend_from_slice(&chunk[..n]);
                // `http.client` returns on the framing, not on the hang-up.
                if reply.len() > cap || crate::framing::response_complete(&reply) {
                    break;
                }
            }
            // A TLS close without close_notify after a full body is still a full body.
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof && !reply.is_empty() => break,
            Err(e) => return Err(class(&e)),
        }
    }
    Ok(reply)
}

/// Status, `Date` header and the (identity or chunked) body of one HTTP/1.x response.
fn parse_response(reply: &[u8]) -> Result<(u16, Option<String>, Vec<u8>), PublicError> {
    let unreachable = || fail("pairing_window_unreachable");
    let split = reply
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(unreachable)?;
    let head = std::str::from_utf8(&reply[..split]).map_err(|_| unreachable())?;
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .and_then(|l| l.split(' ').nth(1))
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(unreachable)?;
    let (mut date, mut chunked, mut length) = (None, false, None);
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            let value = value.trim();
            match name.trim().to_ascii_lowercase().as_str() {
                "date" => date = Some(value.to_owned()),
                "transfer-encoding" => chunked = value.eq_ignore_ascii_case("chunked"),
                "content-length" => length = value.parse::<usize>().ok(),
                _ => {}
            }
        }
    }
    let mut body = reply[split + 4..].to_vec();
    if chunked {
        body = dechunk(&body).ok_or_else(unreachable)?;
    } else if let Some(length) = length {
        body.truncate(length);
    }
    body.truncate(READ_LIMIT);
    Ok((status, date, body))
}
fn dechunk(raw: &[u8]) -> Option<Vec<u8>> {
    let (mut out, mut rest) = (Vec::new(), raw);
    loop {
        let line_end = rest.windows(2).position(|w| w == b"\r\n")?;
        let size = usize::from_str_radix(
            std::str::from_utf8(&rest[..line_end])
                .ok()?
                .split(';')
                .next()?
                .trim(),
            16,
        )
        .ok()?;
        rest = &rest[line_end + 2..];
        if size == 0 {
            return Some(out);
        }
        if rest.len() < size + 2 {
            return None;
        }
        out.extend_from_slice(&rest[..size]);
        rest = &rest[size + 2..];
    }
}
/// RFC 7231 `Date: Wed, 28 Oct 2026 12:00:00 GMT` as epoch seconds (`parsedate_to_datetime`).
fn parse_http_date(text: &str) -> Option<f64> {
    let parts: Vec<&str> = text.split_whitespace().collect();
    let [_, day, month, year, clock, zone] = parts.as_slice() else {
        return None;
    };
    if *zone != "GMT" && *zone != "UTC" {
        return None;
    }
    let months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let month = months.iter().position(|m| m == month)? as i64 + 1;
    let (day, year): (i64, i64) = (day.parse().ok()?, year.parse().ok()?);
    let mut hms = clock.split(':').map(|p| p.parse::<i64>().ok());
    let (h, m, s) = (hms.next()??, hms.next()??, hms.next()??);
    // days from civil (Howard Hinnant)
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Some((days * 86_400 + h * 3600 + m * 60 + s) as f64)
}

/// `JOIN_LINK_SCHEME`, re-exported for hosts that print links.
pub const LINK_SCHEME: &str = JOIN_LINK_SCHEME;
