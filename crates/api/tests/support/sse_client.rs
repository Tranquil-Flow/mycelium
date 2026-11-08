// SPDX-License-Identifier: AGPL-3.0-only
//! Minimal HTTP/SSE client for the product request gateway (tests only).
use serde_json::Value;
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpStream},
    sync::mpsc,
    time::{Duration, Instant},
};
pub fn http(addr: SocketAddr, method: &str, path: &str, extra: &str, body: &str) -> (u16, Value) {
    let timeout = Duration::from_secs(5);
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
