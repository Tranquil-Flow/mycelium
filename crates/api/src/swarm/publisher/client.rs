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
    net::{SocketAddr, TcpStream, ToSocketAddrs},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

const RESPONSE_LIMIT: usize = 1 << 20; // swarm_publish.py DirectoryClient._call
const HEADER_LIMIT: usize = 16 * 1024;
fn deadline_exceeded() -> Error {
    Error::new("swarm_directory_deadline_exceeded")
}
fn unreachable() -> Error {
    Error::new("swarm_directory_unreachable")
}

type Resolver = dyn Fn(&str, u16) -> std::io::Result<Vec<SocketAddr>> + Send + Sync;

pub struct DirectoryClient {
    origin: String,
    host: String,
    port: u16,
    secure: bool,
    tls: Arc<ClientConfig>,
    resolve: Arc<Resolver>,
    resolver: Mutex<Option<std::thread::JoinHandle<()>>>,
}
impl DirectoryClient {
    /// Origin only, as in v0.1.4 (the fixed /swarms/api/v1 prefix is appended).
    /// Plain HTTP is permitted ONLY for numeric loopback; never follows redirects.
    pub fn new(origin: &str) -> Result<Self, Error> {
        let mut roots = RootCertStore::empty();
        roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
        Self::with_transport(origin, roots, |host, port| {
            (host, port).to_socket_addrs().map(|a| a.collect())
        })
    }

    /// Explicit trust/resolution injection for embedded hosts and isolated loopback tests.
    /// Origin validation, TLS server-name verification and request budgets remain enforced.
    pub fn with_transport(
        origin: &str,
        roots: RootCertStore,
        resolve: impl Fn(&str, u16) -> std::io::Result<Vec<SocketAddr>> + Send + Sync + 'static,
    ) -> Result<Self, Error> {
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
            resolve: Arc::new(resolve),
            resolver: Mutex::new(None),
        })
    }
    pub fn origin(&self) -> &str {
        &self.origin
    }
}
impl DirectoryPort for DirectoryClient {
    fn call(&self, method: &str, path: &str, body: &[u8]) -> Result<Value, Error> {
        self.exchange(method, path, body, None)
    }
    /// The registered request budget, ended earlier by a caller's deadline when that is sooner.
    fn call_until(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        deadline: Instant,
    ) -> Result<Value, Error> {
        self.exchange(method, path, body, Some(deadline))
    }
}
impl DirectoryClient {
    fn exchange(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        cap: Option<Instant>,
    ) -> Result<Value, Error> {
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
        let own = Instant::now() + Duration::from_secs(budget as u64);
        let deadline = cap.map_or(own, |cap| cap.min(own));
        remaining(deadline)?;
        // Timed-out DNS remains owned. A blocked system resolver cannot accumulate
        // one detached thread per retry; concurrent callers fail without waiting.
        let mut slot = self
            .resolver
            .try_lock()
            .map_err(|_| Error::new("swarm_directory_resolution_pending"))?;
        if slot.as_ref().is_some_and(|worker| !worker.is_finished()) {
            return Err(Error::new("swarm_directory_resolution_pending"));
        }
        if let Some(worker) = slot.take() {
            worker
                .join()
                .map_err(|_| Error::new("swarm_directory_resolution_failed"))?;
        }
        let host = self.host.clone();
        let port = self.port;
        let resolve = self.resolve.clone();
        let (send, receive) = std::sync::mpsc::sync_channel(1);
        let resolver = std::thread::Builder::new()
            .name("swarm-directory-dns".into())
            .spawn(move || {
                let addresses = resolve(&host, port);
                if send.send(addresses).is_err() {
                    eprintln!("swarm_directory_dns_receiver_closed");
                }
            })
            .map_err(|_| unreachable())?;
        *slot = Some(resolver);
        drop(slot);
        let addresses = receive
            .recv_timeout(remaining(deadline)?)
            .map_err(|e| match e {
                std::sync::mpsc::RecvTimeoutError::Timeout => deadline_exceeded(),
                std::sync::mpsc::RecvTimeoutError::Disconnected => {
                    Error::new("swarm_directory_resolution_failed")
                }
            })?
            .map_err(|_| unreachable())?;
        remaining(deadline)?;
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
        let tcp = DeadlineTcp::new(connected.ok_or_else(unreachable)?, deadline)?;
        let authority = self.origin.split_once("://").ok_or_else(unreachable)?.1;
        let head = format!(
            "{method} /swarms/api/v1{path} HTTP/1.1\r\nHost: {authority}\r\nAccept: application/json\r\nAccept-Encoding: identity\r\nUser-Agent: Mycelium/{}\r\nContent-Type: application/json\r\nX-Mycelium-Swarms: 1\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            env!("CARGO_PKG_VERSION"),
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
        stream
            .write_all(&request)
            .and_then(|()| stream.flush())
            .map_err(|e| transport_error(deadline, e))?;
        remaining(deadline)?;
        let mut response = Vec::new();
        let mut buf = [0u8; 8192];
        loop {
            remaining(deadline)?;
            let n = stream
                .read(&mut buf)
                .map_err(|e| transport_error(deadline, e))?;
            remaining(deadline)?;
            if n == 0 {
                break;
            }
            response.extend_from_slice(&buf[..n]);
            if response.len() > RESPONSE_LIMIT + HEADER_LIMIT {
                return Err(Error::new("swarm_directory_response_too_large"));
            }
            if crate::framing::response_complete_checked(&response).map_err(Error::new)? {
                break;
            }
        }
        let result = decode(&response);
        remaining(deadline)?;
        result
    }
}
fn remaining(deadline: Instant) -> Result<Duration, Error> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or_else(deadline_exceeded)
}
enum Stream {
    Plain(DeadlineTcp),
    Tls(Box<StreamOwned<ClientConnection, DeadlineTcp>>),
}
fn transport_error(deadline: Instant, error: std::io::Error) -> Error {
    if Instant::now() >= deadline
        || matches!(
            error.kind(),
            std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
        )
    {
        deadline_exceeded()
    } else {
        unreachable()
    }
}

/// Enforce the absolute request budget below rustls, including every read/write
/// within its handshake and partial-record loops. Never reset the deadline.
struct DeadlineTcp {
    tcp: TcpStream,
    deadline: Instant,
}
impl DeadlineTcp {
    fn new(tcp: TcpStream, deadline: Instant) -> Result<Self, Error> {
        let left = remaining(deadline)?;
        tcp.set_read_timeout(Some(left))
            .and_then(|()| tcp.set_write_timeout(Some(left)))
            .map_err(|e| transport_error(deadline, e))?;
        Ok(Self { tcp, deadline })
    }
    fn remaining(&self) -> std::io::Result<Duration> {
        remaining(self.deadline).map_err(|_| {
            std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "swarm_directory_deadline_exceeded",
            )
        })
    }
}
impl Read for DeadlineTcp {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let left = self.remaining()?;
        let result = match self.tcp.set_read_timeout(Some(left)) {
            Ok(()) => self.tcp.read(buf),
            Err(error) if error.kind() == std::io::ErrorKind::InvalidInput => {
                // macOS may reject SO_RCVTIMEO after peer close while an early
                // HTTP refusal is buffered. Drain ONLY nonblocking in this case:
                // falling back to the previous blocking timeout would reopen P2.
                eprintln!("swarm_directory_timeout_update_nonblocking: {error}");
                self.tcp.set_nonblocking(true)?;
                let read = self.tcp.read(buf);
                self.tcp.set_nonblocking(false)?;
                read
            }
            Err(error) => return Err(error),
        };
        self.remaining()?; // A completion after expiry is never admitted.
        result
    }
}
impl Write for DeadlineTcp {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let left = self.remaining()?;
        self.tcp.set_write_timeout(Some(left))?;
        let result = self.tcp.write(buf);
        self.remaining()?;
        result
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.remaining()?;
        let result = self.tcp.flush();
        self.remaining()?;
        result
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
    let raw = crate::framing::final_response(raw)
        .map_err(Error::new)?
        .ok_or_else(unreachable)?;
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
