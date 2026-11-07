// SPDX-License-Identifier: AGPL-3.0-only
//! Native implementation of the S14 [`Wire`] seam: one HTTP/1.1 request to
//! `127.0.0.1:<port>` over a fresh `std` TCP connection (`Connection: close`).
//!
//! No proxy, DNS, TLS, redirect following, keep-alive, retry or caller-supplied framing
//! header: `Host`, `Content-Length` and `Connection` are written here and a request that
//! tries to set them (or `Transfer-Encoding`, `Proxy-*`, ...) is refused before any
//! connection exists. The reply is read under one shared deadline and a hard byte cap (an
//! L1 id, see `harness_budgets::LOOPBACK_RESPONSE_BYTES`): ambiguous framing (chunked,
//! duplicate `Content-Length`, short body, extra bytes) is refused rather than guessed.
//! A 3xx is returned to the caller as-is; its `Location` is never requested.
use crate::{
    Fault, FaultKind, Result,
    native_files::Deadline,
    native_io::Wire,
    native_session::{Reply, Request},
};
use budgets::{Bytes, Span};
use serde_json::Value;
use std::io::{ErrorKind, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpStream};

fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}
fn refused() -> Fault {
    fault("native_wire_request_refused")
}
fn invalid() -> Fault {
    fault("native_wire_response_invalid")
}

pub struct NativeWire {
    max_response: u64,
}
impl NativeWire {
    /// `max_response` must come from the L1 registry (`loopback_response_limit`).
    pub fn new(max_response: Bytes) -> Result<Self> {
        if max_response == Bytes::ZERO {
            return Err(fault("native_file_budget_invalid"));
        }
        Ok(Self {
            max_response: max_response.get(),
        })
    }
}

fn token(name: &str) -> bool {
    !name.is_empty()
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
}
/// Framing, routing and hop-by-hop headers belong to this module, never to the caller.
fn reserved(lower: &str) -> bool {
    lower.starts_with("proxy-")
        || matches!(
            lower,
            "host"
                | "content-length"
                | "transfer-encoding"
                | "connection"
                | "keep-alive"
                | "upgrade"
                | "te"
                | "trailer"
                | "expect"
        )
}
fn encode(address: SocketAddrV4, request: &Request) -> Result<Vec<u8>> {
    if *address.ip() != Ipv4Addr::LOCALHOST
        || address.port() == 0
        || address.port() != request.port
        || request.follow_redirects
        || !matches!(request.method, "GET" | "POST")
        || (request.method == "GET" && !request.body.is_empty())
        || !request.path.starts_with('/')
        || !request.path.bytes().all(|b| (0x21..=0x7e).contains(&b))
    {
        return Err(refused());
    }
    let mut head = format!(
        "{} {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n",
        request.method, request.path, request.port
    );
    if request.method == "POST" {
        head += &format!("Content-Length: {}\r\n", request.body.len());
    }
    for (name, value) in &request.headers {
        if !token(name)
            || reserved(&name.to_ascii_lowercase())
            || value.bytes().any(|b| b == 0x7f || (b < 0x20 && b != b'\t'))
        {
            return Err(refused());
        }
        head += &format!("{name}: {value}\r\n");
    }
    head += "\r\n";
    let mut bytes = head.into_bytes();
    bytes.extend_from_slice(&request.body);
    Ok(bytes)
}

/// Read until the caller's framing decision is complete. Returns the raw response bytes.
fn read_response(stream: &mut TcpStream, max: u64, deadline: &Deadline) -> Result<Vec<u8>> {
    let mut raw = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        if framed(&raw, max)? == Some(true) {
            return Ok(raw);
        }
        stream
            .set_read_timeout(Some(std::time::Duration::from_millis(
                deadline.remaining()?.millis(),
            )))
            .map_err(|_| fault("native_wire_socket_failed"))?;
        match stream.read(&mut buf) {
            Ok(0) => {
                // EOF is a complete body only for a reply with no declared length.
                if framed_at_eof(&raw)? {
                    return Ok(raw);
                }
                return Err(fault("native_wire_response_truncated"));
            }
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                if raw.len() as u64 > max {
                    return Err(fault("native_wire_response_too_large"));
                }
            }
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                return Err(fault("native_io_timeout"));
            }
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(_) => return Err(fault("native_wire_read_failed")),
        }
    }
}
fn split(raw: &[u8]) -> Option<(&[u8], &[u8])> {
    raw.windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|i| (&raw[..i], &raw[i + 4..]))
}
/// `Some(true)`: complete by Content-Length; `Some(false)`/`None`: need more bytes.
fn framed(raw: &[u8], max: u64) -> Result<Option<bool>> {
    let Some((head, body)) = split(raw) else {
        return Ok(None);
    };
    match content_length(head)? {
        Some(n) if n > max => Err(fault("native_wire_response_too_large")),
        Some(n) if body.len() as u64 > n => Err(invalid()),
        Some(n) => Ok(Some(body.len() as u64 == n)),
        None => Ok(Some(false)),
    }
}
fn framed_at_eof(raw: &[u8]) -> Result<bool> {
    let (head, body) = split(raw).ok_or_else(invalid)?;
    Ok(match content_length(head)? {
        Some(n) => body.len() as u64 == n,
        None => true,
    })
}
fn content_length(head: &[u8]) -> Result<Option<u64>> {
    let text = std::str::from_utf8(head).map_err(|_| invalid())?;
    let mut found = None;
    for line in text.split("\r\n").skip(1) {
        let (name, value) = line.split_once(':').ok_or_else(invalid)?;
        let lower = name.to_ascii_lowercase();
        if lower == "transfer-encoding" {
            return Err(invalid());
        }
        if lower == "content-length" {
            let v = value.trim_matches([' ', '\t']);
            if found.is_some() || v.is_empty() || !v.bytes().all(|b| b.is_ascii_digit()) {
                return Err(invalid());
            }
            found = Some(v.parse::<u64>().map_err(|_| invalid())?);
        }
    }
    Ok(found)
}
fn parse(raw: &[u8]) -> Result<Reply> {
    let (head, body) = split(raw).ok_or_else(invalid)?;
    let text = std::str::from_utf8(head).map_err(|_| invalid())?;
    let mut lines = text.split("\r\n");
    let status = lines.next().ok_or_else(invalid)?;
    let code = status
        .strip_prefix("HTTP/1.1 ")
        .or_else(|| status.strip_prefix("HTTP/1.0 "))
        .filter(|rest| rest.len() == 3 || rest.as_bytes().get(3) == Some(&b' '))
        .and_then(|rest| rest.get(..3))
        .filter(|c| c.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|c| c.parse::<u16>().ok())
        .filter(|c| (100..=599).contains(c))
        .ok_or_else(invalid)?;
    let mut headers = Vec::new();
    for line in lines {
        let (name, value) = line.split_once(':').ok_or_else(invalid)?;
        if !token(name) {
            return Err(invalid());
        }
        headers.push((name.to_owned(), value.trim_matches([' ', '\t']).to_owned()));
    }
    let json = if body.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(body).map_err(|_| invalid())?
    };
    Ok(Reply {
        http: code,
        json,
        headers,
    })
}

impl Wire for NativeWire {
    fn send(&mut self, address: SocketAddrV4, request: Request, bound: Span) -> Result<Reply> {
        let deadline = Deadline::new(bound);
        let bytes = encode(address, &request)?;
        let mut stream = TcpStream::connect_timeout(
            &SocketAddr::V4(address),
            std::time::Duration::from_millis(deadline.remaining()?.millis()),
        )
        .map_err(|e| match e.kind() {
            ErrorKind::TimedOut | ErrorKind::WouldBlock => fault("native_io_timeout"),
            _ => fault("native_wire_connect_failed"),
        })?;
        stream
            .set_write_timeout(Some(std::time::Duration::from_millis(
                deadline.remaining()?.millis(),
            )))
            .map_err(|_| fault("native_wire_socket_failed"))?;
        stream.write_all(&bytes).map_err(|e| match e.kind() {
            ErrorKind::TimedOut | ErrorKind::WouldBlock => fault("native_io_timeout"),
            _ => fault("native_wire_write_failed"),
        })?;
        let raw = read_response(&mut stream, self.max_response, &deadline)?;
        deadline.remaining()?;
        parse(&raw)
    }
}
