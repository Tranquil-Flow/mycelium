// SPDX-License-Identifier: AGPL-3.0-only
//! Minimal HTTP/SSE client for the product request gateway: the panicking test helpers
//! [`http`] / [`stream`] and the fallible [`request`] / [`events`] the chat service uses.
use serde_json::Value;
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpStream},
    sync::mpsc,
    time::{Duration, Instant},
};
pub fn http(addr: SocketAddr, method: &str, path: &str, extra: &str, body: &str) -> (u16, Value) {
    let timeout = Duration::from_millis(
        budgets::BudgetId::ModelSelection
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    );
    let mut c = TcpStream::connect(addr).unwrap();
    c.set_read_timeout(Some(timeout)).unwrap();
    write!(c, "{method} {path} HTTP/1.1\r\nHost: {addr}\r\n{extra}Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len()).unwrap();
    c.shutdown(std::net::Shutdown::Write).unwrap();
    let mut response = String::new();
    c.read_to_string(&mut response).unwrap();
    let status = response[9..12].parse().unwrap();
    let body = response.split_once("\r\n\r\n").map(|x| x.1).unwrap_or("");
    (status, serde_json::from_str(body).unwrap_or(Value::Null))
}
/// Product SSE consumer: bounded polls end a stream without a terminal; reconnect
/// with Last-Event-ID and never invent a terminal. Emits (event, arrival) pairs.
/// The write half stays open (a half-close reads as a client disconnect).
pub fn stream(
    addr: SocketAddr,
    path: String,
    auth: String,
    out: mpsc::Sender<(Value, Instant)>,
    allowance: Duration,
) {
    let mut cursor: Option<String> = None;
    let end = Instant::now() + allowance;
    let mut idle = 0u32;
    while Instant::now() < end {
        let mut c = TcpStream::connect(addr).unwrap();
        c.set_read_timeout(Some(allowance)).unwrap();
        let last = cursor
            .as_ref()
            .map(|id| format!("Last-Event-ID: {id}\r\n"))
            .unwrap_or_default();
        write!(
            c,
            "GET {path} HTTP/1.1\r\nHost: {addr}\r\n{auth}{last}Content-Length: 0\r\n\r\n"
        )
        .unwrap();
        let mut reader = BufReader::new(c);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if !line.starts_with("HTTP/1.1 200") {
            let mut rest = String::new();
            if let Err(e) = reader.read_to_string(&mut rest) {
                rest = format!("<unreadable: {e}>");
            }
            panic!("event stream refused: {line}{rest}");
        }
        // Every accepted subscribe advances the publisher generation, even when
        // this connection then delivers no event before its bounded poll ends.
        if let Some(id) = cursor.as_mut()
            && let Some((g, seq)) = id.split_once(':')
        {
            *id = format!("{}:{seq}", g.parse::<u64>().unwrap() + 1);
        }
        let mut delivered = false;
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
            if let Some(id) = line.strip_prefix("id: ") {
                cursor = Some(id.trim().to_owned());
            }
            if let Some(data) = line.strip_prefix("data: ") {
                delivered = true;
                let event: Value = serde_json::from_str(data.trim()).unwrap();
                let terminal = matches!(
                    event["type"].as_str(),
                    Some("completed" | "cancelled" | "failed")
                );
                if out.send((event, Instant::now())).is_err() || terminal {
                    return;
                }
            }
        }
        // Bounded idle polls end streams by design; the overall allowance bounds
        // waiting, and many back-to-back empty streams are recorded, not hidden.
        idle = if delivered { 0 } else { idle + 1 };
        if idle > 0 && idle.is_multiple_of(32) {
            eprintln!("sse: {idle} consecutive empty bounded streams on {path}");
        }
    }
}

/// [`request`] / [`events`] failure: the I/O error and whether it happened while connecting.
pub type IoFailure = (std::io::Error, bool);
/// One buffered HTTP exchange with the product request gateway (fallible form of [`http`] for
/// product callers). `extra` carries the header lines (each ending `\r\n`); every read waits at
/// most `idle`. Returns the status and the JSON body (`null` when the body is not JSON).
pub fn request(
    addr: SocketAddr,
    method: &str,
    path: &str,
    extra: &str,
    body: &str,
    idle: Duration,
) -> Result<(u16, Value), IoFailure> {
    let mut c = TcpStream::connect_timeout(&addr, idle).map_err(|e| (e, true))?;
    let io = |e: std::io::Error| (e, false);
    c.set_read_timeout(Some(idle)).map_err(io)?;
    c.set_write_timeout(Some(idle)).map_err(io)?;
    write!(c, "{method} {path} HTTP/1.1\r\nHost: {addr}\r\n{extra}Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len()).map_err(io)?;
    c.shutdown(std::net::Shutdown::Write).map_err(io)?;
    let mut response = Vec::new();
    c.read_to_end(&mut response).map_err(io)?;
    let response = String::from_utf8_lossy(&response);
    let status = response
        .get(9..12)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| io(std::io::ErrorKind::InvalidData.into()))?;
    let body = response.split_once("\r\n\r\n").map(|x| x.1).unwrap_or("");
    Ok((status, serde_json::from_str(body).unwrap_or(Value::Null)))
}
/// Why [`events`] ended without an answer from `on_event`.
#[derive(Debug)]
pub enum EventsError {
    /// The gateway refused the subscription: status and JSON body.
    Refused(u16, Value),
    /// An I/O failure, or no event for `idle` (`TimedOut`).
    Io(std::io::Error, bool),
    /// An event's `data:` line is not JSON.
    Invalid,
}
/// The product SSE consumer for product callers (fallible form of [`stream`]): every event goes to
/// `on_event` until it answers `Some`. A bounded poll that ends a connection without a terminal is
/// resumed with `Last-Event-ID`; at most `idle` passes without an event (the shipped client's
/// socket timeout). `Ok(None)`: a terminal event arrived and `on_event` did not answer it.
pub fn events<T>(
    addr: SocketAddr,
    path: &str,
    auth: &str,
    idle: Duration,
    on_event: &mut dyn FnMut(&Value) -> Option<T>,
) -> Result<Option<T>, EventsError> {
    let mut cursor: Option<String> = None;
    let mut progress = Instant::now();
    loop {
        let left = idle.saturating_sub(progress.elapsed());
        if left.is_zero() {
            return Err(EventsError::Io(std::io::ErrorKind::TimedOut.into(), false));
        }
        let mut c =
            TcpStream::connect_timeout(&addr, left).map_err(|e| EventsError::Io(e, true))?;
        let io = |e: std::io::Error| EventsError::Io(e, false);
        c.set_read_timeout(Some(idle)).map_err(io)?;
        let last = cursor
            .as_ref()
            .map(|id| format!("Last-Event-ID: {id}\r\n"))
            .unwrap_or_default();
        write!(
            c,
            "GET {path} HTTP/1.1\r\nHost: {addr}\r\n{auth}{last}Content-Length: 0\r\n\r\n"
        )
        .map_err(io)?;
        let mut reader = BufReader::new(c);
        let mut line = String::new();
        reader.read_line(&mut line).map_err(io)?;
        if !line.starts_with("HTTP/1.1 200") {
            let status = line.get(9..12).and_then(|s| s.parse().ok()).unwrap_or(0);
            let mut rest = String::new();
            reader.read_to_string(&mut rest).map_err(io)?;
            let body = rest.split_once("\r\n\r\n").map(|x| x.1).unwrap_or("");
            return Err(EventsError::Refused(
                status,
                serde_json::from_str(body).unwrap_or(Value::Null),
            ));
        }
        // Every accepted subscribe advances the publisher generation (as in `stream`).
        if let Some(id) = cursor.as_mut()
            && let Some((g, seq)) = id.split_once(':')
            && let Ok(g) = g.parse::<u64>()
        {
            *id = format!("{}:{seq}", g + 1);
        }
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => break,
                Ok(_) => {}
                Err(e) => return Err(io(e)),
            }
            if let Some(id) = line.strip_prefix("id: ") {
                cursor = Some(id.trim().to_owned());
            }
            if let Some(data) = line.strip_prefix("data: ") {
                progress = Instant::now();
                let event: Value =
                    serde_json::from_str(data.trim()).map_err(|_| EventsError::Invalid)?;
                if let Some(answer) = on_event(&event) {
                    return Ok(Some(answer));
                }
                if matches!(
                    event["type"].as_str(),
                    Some("completed" | "cancelled" | "failed")
                ) {
                    return Ok(None);
                }
            }
        }
    }
}
