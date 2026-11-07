// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded HTTP/1.1 over numeric loopback TCP only. No proxies or redirects.
use crate::{Result, duration, io, loopback_origin, parse_sse, require};
use budgets::BudgetId;
use serde_json::Value;
use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    time::Instant,
};
const MAX_BODY: usize = 2 * 1024 * 1024;
const MAX_HEADER: usize = 64 * 1024;

pub struct Response {
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}
impl Response {
    pub fn json(&self) -> Result<Value> {
        serde_json::from_slice(&self.body).map_err(|_| "http:invalid_json".into())
    }
    pub fn text(&self) -> Result<&str> {
        std::str::from_utf8(&self.body).map_err(|_| "http:invalid_utf8".into())
    }
    pub fn events(&self) -> Result<Vec<Value>> {
        require(
            self.headers
                .get("content-type")
                .is_some_and(|v| v.starts_with("text/event-stream")),
            "sse:content_type",
        )?;
        parse_sse(self.text()?)
    }
}

pub struct Client {
    pub origin: String,
    port: u16,
    cookie: Option<String>,
}
impl Client {
    pub fn new(origin: &str) -> Result<Self> {
        Ok(Self {
            origin: origin.to_owned(),
            port: loopback_origin(origin)?,
            cookie: None,
        })
    }
    pub fn request(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        headers: &[(&str, &str)],
        authenticated: bool,
        budget: BudgetId,
    ) -> Result<Response> {
        require(
            matches!(method, "GET" | "POST" | "DELETE")
                && path.starts_with('/')
                && !path.contains(['\r', '\n', ' ']),
            "http:invalid_request_target",
        )?;
        let limit = duration(budget)?;
        let deadline = Instant::now() + limit;
        let address = SocketAddr::from((Ipv4Addr::LOCALHOST, self.port));
        let mut stream =
            TcpStream::connect_timeout(&address, limit).map_err(|e| io("http:connect", e))?;
        stream
            .set_write_timeout(Some(limit))
            .map_err(|e| io("http:write_timeout", e))?;
        let mut wire = format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\nContent-Length: {}\r\n",
            self.port,
            body.len()
        );
        if authenticated {
            let cookie = self.cookie.as_ref().ok_or("http:no_session")?;
            wire.push_str(&format!("Cookie: {cookie}\r\n"));
        }
        for (key, value) in headers {
            require(
                !key.contains(['\r', '\n', ':']) && !value.contains(['\r', '\n']),
                "http:header_injection",
            )?;
            wire.push_str(&format!("{key}: {value}\r\n"));
        }
        wire.push_str("\r\n");
        stream
            .write_all(wire.as_bytes())
            .map_err(|e| io("http:write", e))?;
        stream
            .write_all(body)
            .map_err(|e| io("http:body_write", e))?;
        read_response(stream, deadline)
    }
    pub fn get(&self, path: &str) -> Result<Response> {
        self.request("GET", path, &[], &[], true, BudgetId::ModelSelection)
    }
    pub fn post(&self, path: &str, body: &Value) -> Result<Response> {
        let bytes = serde_json::to_vec(body).map_err(|_| "http:encode")?;
        self.request(
            "POST",
            path,
            &bytes,
            &[
                ("Content-Type", "application/json"),
                ("Origin", &self.origin),
                ("X-Mycelium-Action", "1"),
            ],
            true,
            BudgetId::WorkerStartup,
        )
    }
    /// Consume the app's public one-use browser launch form, never native-control.json.
    pub fn sign_in(&mut self, launch: &str) -> Result<()> {
        require(
            !launch.is_empty()
                && launch
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-'),
            "session:invalid_launch",
        )?;
        let response = self.request(
            "POST",
            "/session",
            format!("launch={launch}").as_bytes(),
            &[
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Origin", "null"),
            ],
            false,
            BudgetId::ModelSelection,
        )?;
        require(response.status == 303, "session:expected_303")?;
        let raw = response
            .headers
            .get("set-cookie")
            .ok_or("session:no_cookie")?;
        require(
            raw.contains("HttpOnly") && raw.contains("SameSite=Strict") && raw.contains("Path=/"),
            "session:cookie_protection",
        )?;
        let cookie = raw.split(';').next().ok_or("session:empty_cookie")?;
        require(cookie.starts_with("a13session="), "session:cookie_name")?;
        self.cookie = Some(cookie.to_owned());
        Ok(())
    }
}
fn arm(reader: &BufReader<TcpStream>, deadline: Instant) -> Result<()> {
    let remaining = deadline
        .checked_duration_since(Instant::now())
        .ok_or("http:deadline")?;
    reader
        .get_ref()
        .set_read_timeout(Some(remaining))
        .map_err(|e| io("http:read_timeout", e))
}
fn line(reader: &mut BufReader<TcpStream>, deadline: Instant) -> Result<String> {
    let mut bytes = Vec::new();
    loop {
        arm(reader, deadline)?;
        let buffer = reader.fill_buf().map_err(|e| io("http:line", e))?;
        require(!buffer.is_empty(), "http:unexpected_eof")?;
        let n = buffer
            .iter()
            .position(|v| *v == b'\n')
            .map_or(buffer.len(), |v| v + 1);
        require(bytes.len() + n <= MAX_HEADER, "http:line_too_large")?;
        let done = buffer[n - 1] == b'\n';
        bytes.extend_from_slice(&buffer[..n]);
        reader.consume(n);
        if done {
            break;
        }
    }
    String::from_utf8(bytes).map_err(|_| "http:header_utf8".into())
}
fn exact(reader: &mut BufReader<TcpStream>, count: usize, deadline: Instant) -> Result<Vec<u8>> {
    require(count <= MAX_BODY, "http:body_too_large")?;
    let mut result = vec![0; count];
    let mut at = 0;
    while at < count {
        arm(reader, deadline)?;
        let n = reader
            .read(&mut result[at..])
            .map_err(|e| io("http:read", e))?;
        require(n > 0, "http:truncated_body")?;
        at += n;
    }
    Ok(result)
}
fn read_response(stream: TcpStream, deadline: Instant) -> Result<Response> {
    let mut reader = BufReader::new(stream);
    let status_line = line(&mut reader, deadline)?;
    let mut parts = status_line.split_whitespace();
    require(
        matches!(parts.next(), Some("HTTP/1.1" | "HTTP/1.0")),
        "http:version",
    )?;
    let status = parts
        .next()
        .ok_or("http:no_status")?
        .parse()
        .map_err(|_| "http:status")?;
    let mut headers: BTreeMap<String, String> = BTreeMap::new();
    let mut header_bytes = status_line.len();
    loop {
        let raw = line(&mut reader, deadline)?;
        header_bytes += raw.len();
        require(header_bytes <= MAX_HEADER, "http:headers_too_large")?;
        if raw == "\r\n" || raw == "\n" {
            break;
        }
        let (key, value) = raw.trim_end().split_once(':').ok_or("http:header")?;
        let key = key.to_ascii_lowercase();
        if let Some(existing) = headers.get_mut(&key) {
            // Framing and routing headers must be unique (request smuggling); Set-Cookie lines stay separate (RFC 6265
            // s4.1) and any other repeated field is a comma list (RFC 9110 s5.3).
            require(
                !matches!(
                    key.as_str(),
                    "content-length" | "transfer-encoding" | "content-type" | "location" | "host"
                ),
                &format!("http:duplicate_header:{key}"),
            )?;
            existing.push_str(if key == "set-cookie" { "\n" } else { ", " });
            existing.push_str(value.trim());
        } else {
            headers.insert(key, value.trim().to_owned());
        }
    }
    let mut body = Vec::new();
    match (
        headers.get("transfer-encoding"),
        headers.get("content-length"),
    ) {
        (Some(_), Some(_)) => return Err("http:ambiguous_framing".into()),
        (Some(encoding), None) => {
            require(encoding == "chunked", "http:transfer_encoding")?;
            loop {
                let size = line(&mut reader, deadline)?;
                let size = usize::from_str_radix(
                    size.trim().split(';').next().ok_or("http:chunk_size")?,
                    16,
                )
                .map_err(|_| "http:chunk_size")?;
                if size == 0 {
                    require(
                        line(&mut reader, deadline)? == "\r\n",
                        "http:trailers_unsupported",
                    )?;
                    break;
                }
                require(size <= MAX_BODY - body.len(), "http:body_too_large")?;
                body.extend(exact(&mut reader, size, deadline)?);
                require(
                    exact(&mut reader, 2, deadline)? == b"\r\n",
                    "http:chunk_delimiter",
                )?;
            }
        }
        (None, Some(length)) => {
            let count = length.parse().map_err(|_| "http:content_length")?;
            body = exact(&mut reader, count, deadline)?;
        }
        (None, None) => loop {
            arm(&reader, deadline)?;
            let mut bytes = [0; 4096];
            let n = reader
                .read(&mut bytes)
                .map_err(|e| io("http:read_to_close", e))?;
            if n == 0 {
                break;
            }
            require(n <= MAX_BODY - body.len(), "http:body_too_large")?;
            body.extend_from_slice(&bytes[..n]);
        },
    }
    Ok(Response {
        status,
        headers,
        body,
    })
}
