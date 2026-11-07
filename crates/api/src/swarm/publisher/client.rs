// SPDX-License-Identifier: AGPL-3.0-only
//! Credential-free HTTP client using the workspace's existing rustls stack.
use super::{DirectoryPort, Error, seconds};
use crate::swarm::{docs, json::J};
use budgets::BudgetId;
use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};
use rustls_pki_types::ServerName;
use serde_json::Value;
use std::{
    io::{Read, Write},
    net::{TcpStream, ToSocketAddrs},
    sync::Arc,
    time::{Duration, Instant},
};

const RESPONSE_LIMIT: usize = 1 << 20; // swarm_publish.py DirectoryClient._call
const HEADER_LIMIT: usize = 16 * 1024;
fn unreachable() -> Error {
    Error::new("swarm_directory_unreachable")
}

pub struct DirectoryClient {
    origin: String,
    host: String,
    port: u16,
    secure: bool,
    tls: Arc<ClientConfig>,
}
impl DirectoryClient {
    /// Origin only, as in v0.1.4 (the fixed /swarms/api/v1 prefix is appended).
    /// Plain HTTP is permitted ONLY for numeric loopback; never follows redirects.
    pub fn new(origin: &str) -> Result<Self, Error> {
        docs::origin(&J::str(origin), true, false, "directory_origin_invalid")?;
        let secure = origin.starts_with("https://");
        let (host, port) = if secure {
            (origin[8..].to_owned(), 443)
        } else {
            (
                "127.0.0.1".into(),
                origin
                    .rsplit(':')
                    .next()
                    .and_then(|s| s.parse().ok())
                    .ok_or_else(|| Error::new("swarm_directory_origin_invalid"))?,
            )
        };
        let mut roots = RootCertStore::empty();
        roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
        let tls =
            ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .map_err(|_| Error::new("swarm_directory_tls_failed"))?
                .with_root_certificates(roots)
                .with_no_client_auth();
        Ok(Self {
            origin: origin.into(),
            host,
            port,
            secure,
            tls: Arc::new(tls),
        })
    }
    pub fn origin(&self) -> &str {
        &self.origin
    }
}
impl DirectoryPort for DirectoryClient {
    fn call(&self, method: &str, path: &str, body: &[u8]) -> Result<Value, Error> {
        if !matches!(method, "POST" | "GET")
            || !path.starts_with('/')
            || path
                .bytes()
                .any(|c| !(c.is_ascii_alphanumeric() || b"/-_".contains(&c)))
        {
            return Err(Error::new("swarm_directory_route_invalid"));
        }
        let budget =
            seconds(BudgetId::PyMyceliumA13MacosSwarmPublishDirectoryclientInitTimeoutL54)?;
        let deadline = Instant::now() + Duration::from_secs(budget as u64);
        // Resolution is isolated behind the same overall request deadline. No proxy/environment URL.
        let host = self.host.clone();
        let port = self.port;
        let (send, receive) = std::sync::mpsc::sync_channel(1);
        let resolver = std::thread::Builder::new()
            .name("swarm-directory-dns".into())
            .spawn(move || {
                let addresses = (host.as_str(), port)
                    .to_socket_addrs()
                    .map(|a| a.collect::<Vec<_>>());
                if send.send(addresses).is_err() {
                    eprintln!("swarm_directory_dns_receiver_closed");
                }
            })
            .map_err(|_| unreachable())?;
        let addresses = receive
            .recv_timeout(remaining(deadline)?)
            .map_err(|_| unreachable())?
            .map_err(|_| unreachable())?;
        resolver.join().map_err(|_| unreachable())?;
        let mut connected = None;
        for address in addresses {
            match TcpStream::connect_timeout(&address, remaining(deadline)?) {
                Ok(tcp) => {
                    connected = Some(tcp);
                    break;
                }
                Err(_) => continue, // subsequent address or explicit unreachable below
            }
        }
        let tcp = connected.ok_or_else(unreachable)?;
        let authority = self.origin.split_once("://").ok_or_else(unreachable)?.1;
        let head = format!(
            "{method} /swarms/api/v1{path} HTTP/1.1\r\nHost: {authority}\r\nAccept: application/json\r\nAccept-Encoding: identity\r\nUser-Agent: {}\r\nContent-Type: application/json\r\nX-Mycelium-Swarms: 1\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            crate::join_client::USER_AGENT,
            body.len()
        );
        let mut request = head.into_bytes();
        request.extend_from_slice(body);
        let mut stream = if self.secure {
            let name = ServerName::try_from(self.host.clone()).map_err(|_| unreachable())?;
            let connection =
                ClientConnection::new(self.tls.clone(), name).map_err(|_| unreachable())?;
            Stream::Tls(Box::new(StreamOwned::new(connection, tcp)))
        } else {
            Stream::Plain(tcp)
        };
        stream.timeout(deadline)?;
        stream
            .write_all(&request)
            .and_then(|()| stream.flush())
            .map_err(|_| unreachable())?;
        let mut response = Vec::new();
        let mut buf = [0u8; 8192];
        loop {
            stream.timeout(deadline)?;
            let n = stream.read(&mut buf).map_err(|_| unreachable())?;
            if n == 0 {
                break;
            }
            response.extend_from_slice(&buf[..n]);
            if response.len() > RESPONSE_LIMIT + HEADER_LIMIT {
                return Err(Error::new("swarm_directory_response_too_large"));
            }
            if crate::framing::response_complete(&response) {
                break;
            }
        }
        decode(&response)
    }
}
fn remaining(deadline: Instant) -> Result<Duration, Error> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or_else(unreachable)
}
enum Stream {
    Plain(TcpStream),
    Tls(Box<StreamOwned<ClientConnection, TcpStream>>),
}
impl Stream {
    fn timeout(&self, deadline: Instant) -> Result<(), Error> {
        let tcp = match self {
            Self::Plain(tcp) => tcp,
            Self::Tls(tls) => &tls.sock,
        };
        let left = remaining(deadline)?;
        tcp.set_read_timeout(Some(left))
            .and_then(|()| tcp.set_write_timeout(Some(left)))
            .map_err(|_| unreachable())
    }
}
impl Read for Stream {
    fn read(&mut self, b: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Self::Plain(s) => s.read(b),
            Self::Tls(s) => s.read(b),
        }
    }
}
impl Write for Stream {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        match self {
            Self::Plain(s) => s.write(b),
            Self::Tls(s) => s.write(b),
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        match self {
            Self::Plain(s) => s.flush(),
            Self::Tls(s) => s.flush(),
        }
    }
}
fn decode(raw: &[u8]) -> Result<Value, Error> {
    let end = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .filter(|n| *n <= HEADER_LIMIT)
        .ok_or_else(unreachable)?;
    let head = std::str::from_utf8(&raw[..end]).map_err(|_| unreachable())?;
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .and_then(|s| s.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .ok_or_else(unreachable)?;
    let mut length = None;
    let mut chunked = false;
    for line in lines {
        let (key, value) = line.split_once(':').ok_or_else(unreachable)?;
        if key.eq_ignore_ascii_case("content-length") {
            if length.is_some() {
                return Err(unreachable());
            }
            length = Some(value.trim().parse::<usize>().map_err(|_| unreachable())?);
        }
        if key.eq_ignore_ascii_case("transfer-encoding") {
            if chunked || !value.trim().eq_ignore_ascii_case("chunked") {
                return Err(unreachable());
            }
            chunked = true;
        }
    }
    let body = &raw[end + 4..];
    let decoded;
    let body = if chunked {
        if length.is_some() {
            return Err(unreachable());
        }
        decoded = dechunk(body)?;
        decoded.as_slice()
    } else {
        if length.is_some_and(|n| n != body.len()) {
            return Err(unreachable());
        }
        body
    };
    if body.len() > RESPONSE_LIMIT {
        return Err(Error::new("swarm_directory_response_too_large"));
    }
    let parsed = serde_json::from_slice(body).unwrap_or(Value::Null);
    if (200..300).contains(&status) && !parsed.is_object() {
        return Err(unreachable());
    }
    Error::response(status, parsed)
}
fn dechunk(mut rest: &[u8]) -> Result<Vec<u8>, Error> {
    let mut out = Vec::new();
    loop {
        let end = rest
            .windows(2)
            .position(|w| w == b"\r\n")
            .ok_or_else(unreachable)?;
        let size = usize::from_str_radix(
            std::str::from_utf8(&rest[..end])
                .map_err(|_| unreachable())?
                .split(';')
                .next()
                .ok_or_else(unreachable)?,
            16,
        )
        .map_err(|_| unreachable())?;
        rest = &rest[end + 2..];
        if size == 0 {
            return if rest == b"\r\n" {
                Ok(out)
            } else {
                Err(unreachable())
            };
        }
        if size > RESPONSE_LIMIT.saturating_sub(out.len())
            || rest.len() < size + 2
            || &rest[size..size + 2] != b"\r\n"
        {
            return Err(unreachable());
        }
        out.extend_from_slice(&rest[..size]);
        rest = &rest[size + 2..];
    }
}
