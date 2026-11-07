// SPDX-License-Identifier: AGPL-3.0-only
//! `SeedHTTPServer` / `LocalTLSSeedServer` (`mycelium_seed/http.py:637-820`, `mycelium_a13_macos/tls.py:378-470`
//! @ f4e366e9): a loopback server on a free high port, TLS around the same request handler, an accept loop
//! that polls every 50 ms, one worker thread per connection with a 2 s socket timeout, and a close that joins
//! every thread (no orphan). The coordinator's seed URL is bound to `public_origin` or to
//! `https://127.0.0.1:<port>` exactly as `LocalTLSSeedServer` does.
use crate::{Coordinator, SeedError, http, tls::Material};
use budgets::{BudgetId, Entries};
use protocol::origin;
use rustls::{ServerConnection, StreamOwned};
use serde_json::Value;
use std::{
    io::{self, Write},
    net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread::{self, JoinHandle},
    time::Duration,
};

fn span(id: BudgetId) -> Result<Duration, SeedError> {
    id.span(Entries::new(0))
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| SeedError::new("seed_budget_unavailable"))
}

/// `49152 <= port <= 65535`: the range `LocalTLSSeedServer` accepts for an explicit port.
const DYNAMIC_PORTS_FIRST: u16 = 49152;

pub struct Server {
    address: SocketAddr,
    base_url: String,
    stop: Arc<AtomicBool>,
    errors: Arc<AtomicU64>,
    accept: Option<JoinHandle<()>>,
    workers: Arc<Mutex<Vec<JoinHandle<()>>>>,
}

type Wrap = Arc<dyn Fn(TcpStream) -> io::Result<()> + Send + Sync>;

impl Server {
    /// TLS around the seed handler with installation-local material (`LocalTLSSeedServer`) on a free port.
    pub fn bind_tls(
        coordinator: Arc<Coordinator>,
        material: &Material,
        public_origin: Option<&str>,
    ) -> Result<Self, SeedError> {
        Self::bind_tls_on(coordinator, material, public_origin, None)
    }

    /// `LocalTLSSeedServer(port=...)` (SE-L10-SEED-PORT): `Some(port)` binds exactly that loopback port, so a host
    /// can rebind the first URL it advertised. Anything outside the dynamic range 49152..=65535 is
    /// `local_tls_port_invalid` (checked after the material and before anything is bound); a port that cannot be
    /// bound is `local_tls_bind_failed`.
    pub fn bind_tls_on(
        coordinator: Arc<Coordinator>,
        material: &Material,
        public_origin: Option<&str>,
        port: Option<i64>,
    ) -> Result<Self, SeedError> {
        Self::bind_tls_routes(coordinator, material, public_origin, port, None)
    }

    fn bind_tls_routes(
        coordinator: Arc<Coordinator>,
        material: &Material,
        public_origin: Option<&str>,
        port: Option<i64>,
        routes: Option<Arc<dyn http::RouteExtension>>,
    ) -> Result<Self, SeedError> {
        let config = crate::tls::server_config(material).map_err(|e| SeedError::new(e.0))?;
        let port = match port {
            None => 0,
            Some(p) => u16::try_from(p)
                .ok()
                .filter(|p| (DYNAMIC_PORTS_FIRST..=u16::MAX).contains(p))
                .ok_or_else(|| SeedError::new("local_tls_port_invalid"))?,
        };
        let handler = coordinator.clone();
        let wrap: Wrap = Arc::new(move |stream: TcpStream| {
            let connection = ServerConnection::new(config.clone())
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
            let mut tls = StreamOwned::new(connection, stream);
            let served = http::serve_stream_with_routes(&handler, &mut tls, routes.as_deref());
            tls.conn.send_close_notify();
            tls.flush().and(served)
        });
        Self::bind(coordinator, "https", public_origin, port, wrap)
    }

    /// The same handler over plain TCP, for tests and for a TLS-terminating proxy on the same host. The
    /// coordinator is bound to `http://127.0.0.1:<port>` unless `public_origin` is given.
    pub fn bind_plain(
        coordinator: Arc<Coordinator>,
        public_origin: Option<&str>,
    ) -> Result<Self, SeedError> {
        Self::bind_plain_routes(coordinator, public_origin, None)
    }

    fn bind_plain_routes(
        coordinator: Arc<Coordinator>,
        public_origin: Option<&str>,
        routes: Option<Arc<dyn http::RouteExtension>>,
    ) -> Result<Self, SeedError> {
        let handler = coordinator.clone();
        let wrap: Wrap = Arc::new(move |mut stream: TcpStream| {
            http::serve_stream_with_routes(&handler, &mut stream, routes.as_deref())
        });
        Self::bind(coordinator, "http", public_origin, 0, wrap)
    }

    /// Opt-in application routes over the existing plain server.
    pub fn bind_plain_with_routes(
        coordinator: Arc<Coordinator>,
        public_origin: Option<&str>,
        routes: Arc<dyn http::RouteExtension>,
    ) -> Result<Self, SeedError> {
        Self::bind_plain_routes(coordinator, public_origin, Some(routes))
    }

    /// Opt-in application routes over the existing TLS server.
    pub fn bind_tls_on_with_routes(
        coordinator: Arc<Coordinator>,
        material: &Material,
        public_origin: Option<&str>,
        port: Option<i64>,
        routes: Arc<dyn http::RouteExtension>,
    ) -> Result<Self, SeedError> {
        Self::bind_tls_routes(coordinator, material, public_origin, port, Some(routes))
    }

    fn bind(
        coordinator: Arc<Coordinator>,
        scheme: &str,
        public_origin: Option<&str>,
        port: u16,
        wrap: Wrap,
    ) -> Result<Self, SeedError> {
        // `SeedHTTPServer.__init__`: `canonical_https_origin(public_seed_url)` raises `ValueError("origin is invalid")`
        // before anything is bound; the coordinator stays unbound.
        if let Some(origin) = public_origin {
            origin::canonical_https_origin(&Value::from(origin))
                .map_err(|_| SeedError::accident("origin is invalid"))?;
        }
        let socket_timeout =
            span(BudgetId::PyMyceliumSeedHttpSeedrequesthandlerSetupSelfConnectionSettimeoutL375)?;
        let poll = span(BudgetId::PyFinalMyceliumSeedHttpSelfServerTimeoutL676C8)?;
        let listener = TcpListener::bind(SocketAddr::from((Ipv4Addr::LOCALHOST, port)))
            .map_err(|_| SeedError::new("local_tls_bind_failed"))?;
        listener
            .set_nonblocking(true)
            .map_err(|_| SeedError::new("local_tls_bind_failed"))?;
        let address = listener
            .local_addr()
            .map_err(|_| SeedError::new("local_tls_bind_failed"))?;
        let base_url = format!("{scheme}://127.0.0.1:{}", address.port());
        coordinator.bind_seed_url(public_origin.unwrap_or(&base_url))?;
        let stop = Arc::new(AtomicBool::new(false));
        let errors = Arc::new(AtomicU64::new(0));
        let workers: Arc<Mutex<Vec<JoinHandle<()>>>> = Arc::new(Mutex::new(Vec::new()));
        let accept = {
            let (stop, errors, workers) = (stop.clone(), errors.clone(), workers.clone());
            thread::Builder::new()
                .name("mycelium-seed-http".into())
                .spawn(move || {
                    while !stop.load(Ordering::SeqCst) {
                        match listener.accept() {
                            Ok((stream, _)) => {
                                let prepared = stream
                                    .set_nonblocking(false)
                                    .and_then(|()| stream.set_read_timeout(Some(socket_timeout)))
                                    .and_then(|()| stream.set_write_timeout(Some(socket_timeout)));
                                if prepared.is_err() {
                                    errors.fetch_add(1, Ordering::SeqCst);
                                    continue;
                                }
                                let (wrap, errors) = (wrap.clone(), errors.clone());
                                let worker = thread::spawn(move || {
                                    if wrap(stream).is_err() {
                                        errors.fetch_add(1, Ordering::SeqCst);
                                    }
                                });
                                match workers.lock() {
                                    Ok(mut list) => list.push(worker),
                                    Err(poisoned) => poisoned.into_inner().push(worker),
                                }
                            }
                            Err(e) if e.kind() == io::ErrorKind::WouldBlock => thread::sleep(poll),
                            Err(_) => {
                                errors.fetch_add(1, Ordering::SeqCst);
                                thread::sleep(poll);
                            }
                        }
                    }
                })
                .map_err(|_| SeedError::new("local_tls_bind_failed"))?
        };
        Ok(Self {
            address,
            base_url,
            stop,
            errors,
            accept: Some(accept),
            workers,
        })
    }

    /// `https://127.0.0.1:<port>` (`http://` for a plain server): what the coordinator is bound to unless a
    /// public origin was given.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub fn address(&self) -> SocketAddr {
        self.address
    }

    /// Connections that ended in an I/O or TLS error (a bad handshake, a timeout).
    pub fn connection_errors(&self) -> u64 {
        self.errors.load(Ordering::SeqCst)
    }

    /// Stop accepting and join the accept thread and every connection thread.
    pub fn close(mut self) {
        self.shutdown();
    }

    fn shutdown(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(accept) = self.accept.take()
            && accept.join().is_err()
        {
            self.errors.fetch_add(1, Ordering::SeqCst);
        }
        let workers = match self.workers.lock() {
            Ok(mut list) => std::mem::take(&mut *list),
            Err(poisoned) => std::mem::take(&mut *poisoned.into_inner()),
        };
        for worker in workers {
            if worker.join().is_err() {
                self.errors.fetch_add(1, Ordering::SeqCst);
            }
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.shutdown();
    }
}
