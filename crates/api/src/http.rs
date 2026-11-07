// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded HTTP/1.1 adapter over an already accepted loopback stream. Listener,
//! concurrency admission and shutdown ownership belong to the embedding host.
//! No redirect, proxy, TLS, background task, or hidden listener is created here.
use crate::{
    schema::PublicError,
    wire::{Json, Text},
};
use serde_json::Value;
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpStream,
    time::{Duration, Instant},
};
pub const MAX_BODY: usize = 65_536; // app.py:31; wire frame bound, not a duration.
const MAX_HEADERS: usize = 65_536;
#[derive(Clone, Debug)]
pub struct Request {
    pub method: String,
    pub target: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}
impl Request {
    pub fn values<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a str> {
        self.headers
            .iter()
            .filter(move |(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}
#[derive(Clone, Debug)]
pub struct Reply {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}
/// Source owns bounded polling/cancellation; close runs on every post-dispatch
/// outcome, including HEAD, invalid headers and failed writes.
pub trait StreamingBody: Send {
    fn write(&mut self, writer: &mut dyn Write) -> Result<(), crate::sse::StreamError>;
    fn close(&mut self) -> Result<(), PublicError>;
}
pub enum Response {
    Buffered(Reply),
    Stream {
        reply: Reply,
        body: Box<dyn StreamingBody>,
    },
}
impl From<Reply> for Response {
    fn from(reply: Reply) -> Self {
        Self::Buffered(reply)
    }
}
impl Reply {
    /// Sorted keys, raw UTF-8 text: `json.dumps(v, sort_keys=True, separators=(',',':'),
    /// ensure_ascii=False)` - the request gateway (asgi.py:23) and directory bodies of
    /// services that sort. Handlers that must keep insertion order use [`Reply::ordered`].
    pub fn json(status: u16, value: &Value) -> Result<Self, PublicError> {
        Self::ordered(status, &Json::from(value), Text::Utf8)
    }
    /// `json.dumps(v, separators=(',',':'))` with the given `ensure_ascii` and the key order the
    /// caller built (the desktop app: insertion order, ASCII escapes).
    pub fn ordered(status: u16, value: &Json, text: Text) -> Result<Self, PublicError> {
        Ok(Self::json_bytes(status, value.dumps(text)))
    }
    /// Sorted keys with `\uXXXX` escapes (the default `ensure_ascii`), e.g. the chat page.
    pub fn json_ascii(status: u16, value: &Value) -> Result<Self, PublicError> {
        Self::ordered(status, &Json::from(value), Text::Ascii)
    }
    /// Already-encoded JSON, e.g. from a service that preserves key order.
    pub fn json_bytes(status: u16, body: Vec<u8>) -> Self {
        Self {
            status,
            headers: vec![
                ("content-type".into(), "application/json".into()),
                ("cache-control".into(), "no-store".into()),
                ("referrer-policy".into(), "no-referrer".into()),
                ("x-content-type-options".into(), "nosniff".into()),
            ],
            body,
        }
    }
    pub fn error(error: PublicError) -> Result<Self, PublicError> {
        if error.is_dropped() {
            return Err(error);
        }
        Self::json(error.status, &error.document())
    }
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}
#[derive(Debug)]
pub enum TransportError {
    Io {
        phase: &'static str,
        kind: std::io::ErrorKind,
    },
    Boundary(PublicError),
    Deadline,
    Stream(crate::sse::StreamError),
    Cleanup {
        primary: Option<Box<TransportError>>,
        cleanup: PublicError,
    },
}
fn io(phase: &'static str, error: std::io::Error) -> TransportError {
    TransportError::Io {
        phase,
        kind: error.kind(),
    }
}
fn arm(stream: &TcpStream, deadline: Instant) -> Result<(), TransportError> {
    let remaining = deadline
        .checked_duration_since(Instant::now())
        .ok_or(TransportError::Deadline)?;
    stream
        .set_read_timeout(Some(remaining))
        .map_err(|e| io("read_timeout", e))?;
    stream
        .set_write_timeout(Some(remaining))
        .map_err(|e| io("write_timeout", e))
}
fn line(reader: &mut BufReader<TcpStream>, deadline: Instant) -> Result<Vec<u8>, TransportError> {
    let mut result = Vec::new();
    loop {
        arm(reader.get_ref(), deadline)?;
        let buf = reader.fill_buf().map_err(|e| io("headers", e))?;
        if buf.is_empty() {
            return Err(TransportError::Boundary(PublicError::new(
                400,
                "incomplete_body",
            )));
        }
        let count = buf
            .iter()
            .position(|b| *b == b'\n')
            .map_or(buf.len(), |p| p + 1);
        if result.len() + count > MAX_HEADERS {
            return Err(TransportError::Boundary(PublicError::new(
                431,
                "headers_too_large",
            )));
        }
        let done = buf[count - 1] == b'\n';
        result.extend_from_slice(&buf[..count]);
        reader.consume(count);
        if done {
            return Ok(result);
        }
    }
}
/// `preflight` runs after headers and BEFORE reading a potentially slow body,
/// preserving Host/auth/Origin refusal precedence. Duration is injected: L1 must
/// register the desktop socket/read budget before a default host is composed.
pub fn serve_connection(
    stream: TcpStream,
    budget: Duration,
    preflight: impl Fn(&Request) -> Result<Option<Reply>, PublicError>,
    handle: impl FnOnce(Request) -> Result<Reply, PublicError>,
) -> Result<(), TransportError> {
    serve_response_connection(stream, budget, MAX_BODY, preflight, |r| {
        handle(r).map(Response::Buffered)
    })
}
/// Close-delimited streaming; caller supplies the surface's pinned body cap.
/// This function spawns no tasks. Subscription lifetime/admission is host-owned.
pub fn serve_response_connection(
    stream: TcpStream,
    budget: Duration,
    max_body: usize,
    preflight: impl Fn(&Request) -> Result<Option<Reply>, PublicError>,
    handle: impl FnOnce(Request) -> Result<Response, PublicError>,
) -> Result<(), TransportError> {
    let deadline = Instant::now()
        .checked_add(budget)
        .ok_or(TransportError::Deadline)?;
    let mut reader = BufReader::new(stream);
    let raw = line(&mut reader, deadline)?;
    let start = std::str::from_utf8(&raw)
        .map_err(|_| TransportError::Boundary(PublicError::new(400, "framing_rejected")))?;
    let fields: Vec<_> = start.split_whitespace().collect();
    if fields.len() != 3 || !matches!(fields[2], "HTTP/1.1" | "HTTP/1.0") {
        return Err(TransportError::Boundary(PublicError::new(
            400,
            "framing_rejected",
        )));
    }
    let mut request = Request {
        method: fields[0].into(),
        target: fields[1].into(),
        headers: Vec::new(),
        body: Vec::new(),
    };
    let mut total = raw.len();
    loop {
        let raw = line(&mut reader, deadline)?;
        total += raw.len();
        if total > MAX_HEADERS {
            return Err(TransportError::Boundary(PublicError::new(
                431,
                "headers_too_large",
            )));
        }
        if raw == b"\r\n" {
            break;
        }
        let text = std::str::from_utf8(&raw)
            .map_err(|_| TransportError::Boundary(PublicError::new(400, "framing_rejected")))?;
        let (name, value) =
            text.trim_end_matches(['\r', '\n'])
                .split_once(':')
                .ok_or(TransportError::Boundary(PublicError::new(
                    400,
                    "framing_rejected",
                )))?;
        if name.is_empty() || !name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-') {
            return Err(TransportError::Boundary(PublicError::new(
                400,
                "framing_rejected",
            )));
        }
        request
            .headers
            .push((name.to_ascii_lowercase(), value.trim().into()));
    }
    let head = request.method == "HEAD";
    let response = if let Some(reply) = preflight(&request).map_err(TransportError::Boundary)? {
        Response::Buffered(reply)
    } else {
        let lengths: Vec<_> = request.values("content-length").collect();
        let length = if matches!(request.method.as_str(), "GET" | "HEAD")
            || (request.method == "DELETE" && lengths.is_empty())
        {
            Some(0)
        } else if lengths.len() == 1 && request.header("transfer-encoding").is_none() {
            lengths[0].parse::<usize>().ok()
        } else {
            None
        };
        match length {
            None => Reply::error(PublicError::new(400, "framing_rejected"))
                .map_err(TransportError::Boundary)?
                .into(),
            Some(n) if n > max_body => Reply::error(PublicError::new(413, "body_too_large"))
                .map_err(TransportError::Boundary)?
                .into(),
            Some(n) => {
                request.body.resize(n, 0);
                let mut at = 0;
                while at < n {
                    arm(reader.get_ref(), deadline)?;
                    let count = reader
                        .read(&mut request.body[at..])
                        .map_err(|e| io("body", e))?;
                    if count == 0 {
                        break;
                    }
                    at += count;
                }
                if at != n {
                    Reply::error(PublicError::new(400, "incomplete_body"))
                        .map_err(TransportError::Boundary)?
                        .into()
                } else {
                    handle(request).map_err(TransportError::Boundary)?
                }
            }
        }
    };
    let (reply, mut body) = match response {
        Response::Buffered(reply) => (reply, None),
        Response::Stream { reply, body } => (reply, Some(body)),
    };
    let outcome = (|| {
        // No deadline is silently extended by a slow backend; host service timeouts
        // are a distinct L6/L9 seam. Allow its completed reply a supplied I/O budget.
        let write_deadline = Instant::now()
            .checked_add(budget)
            .ok_or(TransportError::Deadline)?;
        arm(reader.get_ref(), write_deadline)?;
        let mut stream = reader.into_inner();
        let reason = match reply.status {
            200 => "OK",
            202 => "Accepted",
            303 => "See Other",
            400 => "Bad Request",
            401 => "Unauthorized",
            403 => "Forbidden",
            404 => "Not Found",
            405 => "Method Not Allowed",
            409 => "Conflict",
            413 => "Content Too Large",
            415 => "Unsupported Media Type",
            503 => "Service Unavailable",
            _ => "Response",
        };
        let advertised = match reply.header("content-length") {
            Some(value) => value.parse::<usize>().map_err(|_| {
                TransportError::Boundary(PublicError::new(500, "response_header_invalid"))
            })?,
            None => reply.body.len(),
        };
        if (body.is_some() && (reply.header("content-length").is_some() || !reply.body.is_empty()))
            || (!head && advertised != reply.body.len())
        {
            return Err(TransportError::Boundary(PublicError::new(
                500,
                "response_header_invalid",
            )));
        }
        let mut wire = format!(
            "HTTP/1.1 {} {}\r\nConnection: close\r\n",
            reply.status, reason
        );
        if body.is_none() {
            wire.push_str(&format!("Content-Length: {advertised}\r\n"));
        }
        for (name, value) in &reply.headers {
            if name.contains(['\r', '\n', ':']) || value.contains(['\r', '\n']) {
                return Err(TransportError::Boundary(PublicError::new(
                    500,
                    "response_header_invalid",
                )));
            }
            if name.eq_ignore_ascii_case("content-length")
                || name.eq_ignore_ascii_case("connection")
            {
                continue;
            }
            wire.push_str(&format!("{name}: {value}\r\n"));
        }
        wire.push_str("\r\n");
        stream
            .write_all(wire.as_bytes())
            .map_err(|e| io("response_headers", e))?;
        if !head {
            if let Some(source) = body.as_mut() {
                stream
                    .flush()
                    .map_err(|e| io("response_headers_flush", e))?;
                source
                    .write(&mut BoundedWriter {
                        stream: &mut stream,
                        budget,
                    })
                    .map_err(TransportError::Stream)?;
            } else {
                stream
                    .write_all(&reply.body)
                    .map_err(|e| io("response_body", e))?;
            }
        }
        stream.flush().map_err(|e| io("response_flush", e))?;
        // Stream lifetime is independent of per-write/drain I/O budgets.
        let write_deadline = Instant::now()
            .checked_add(budget)
            .ok_or(TransportError::Deadline)?;
        // An auth refusal precedes reading the body. Send FIN before draining so an
        // unread posted body cannot turn the already-written refusal into TCP RST.
        // The drain has the same bounded I/O deadline and never delays the response.
        stream
            .shutdown(std::net::Shutdown::Write)
            .map_err(|e| io("response_shutdown", e))?;
        let mut drained = 0usize;
        let mut buffer = [0u8; 4096];
        while drained < max_body {
            // Darwin may reject setsockopt after both peers half-close. Observe EOF
            // before re-arming; peek retains the already-installed bounded timeout.
            if Instant::now() >= write_deadline {
                return Err(TransportError::Deadline);
            }
            if stream
                .peek(&mut buffer[..1])
                .map_err(|e| io("response_drain_peek", e))?
                == 0
            {
                return Ok(());
            }
            arm(&stream, write_deadline)?;
            let count = stream
                .read(&mut buffer)
                .map_err(|e| io("response_drain", e))?;
            if count == 0 {
                return Ok(());
            }
            drained += count;
        }
        Err(TransportError::Boundary(PublicError::new(
            413,
            "body_too_large",
        )))
    })();
    if let Some(source) = body.as_mut()
        && let Err(cleanup) = source.close()
    {
        return Err(TransportError::Cleanup {
            primary: outcome.err().map(Box::new),
            cleanup,
        });
    }
    outcome
}

struct BoundedWriter<'a> {
    stream: &'a mut TcpStream,
    budget: Duration,
}
impl Write for BoundedWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.stream.set_write_timeout(Some(self.budget))?;
        self.stream.write(bytes)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.stream.flush()
    }
}
