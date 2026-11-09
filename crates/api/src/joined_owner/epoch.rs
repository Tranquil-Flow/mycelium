// SPDX-License-Identifier: AGPL-3.0-only
//! One single-use route epoch on the owner: seed-signed route -> RouteHost ->
//! carrier executor -> M16 runtime -> request service -> loopback HTTP gateway
//! and an event-driven pump. Moved from the owner test driver (B7e/B8) into
//! product code; failures are typed refusals and every thread is owned.
use super::{Ids, Wall, fail, span};
use crate::{
    joined_shard::carrier::VerifiedCarrier,
    m16_requests::{Host as _, M16Requests},
    m16_route::{self, Codec, Currentness, RouteExecutor, RouteHandle, RouteHost, StagePeer},
    registry::Config,
    schema::PublicError,
};
use budgets::BudgetId as B;
use runtime::iroh_host::LanPolicy;
use serde_json::Value;
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
};

/// How the owner reaches the route's stage members.
pub enum Carriers {
    /// LocalOnly loopback carrier (the default; one Mac).
    Loopback(Vec<StagePeer>),
    /// Direct LAN: dial only member-signed, owner-verified advertisements on
    /// exactly `interface`, within `policy`.
    Lan {
        interface: IpAddr,
        policy: LanPolicy,
        verified: Vec<VerifiedCarrier>,
    },
}

/// A live epoch. One request; then it retires (`route_dropped`).
pub struct Epoch {
    service: M16Requests,
    handle: RouteHandle,
    host: Arc<RouteHost>,
    addr: SocketAddr,
    bearer: String,
    stop: Arc<AtomicBool>,
    threads: Vec<JoinHandle<()>>,
    refusals: Arc<Mutex<Vec<String>>>,
}

/// Current member generation from the swarm seed (None = no longer current).
pub fn currentness(coordinator: &Arc<seed::Coordinator>) -> Currentness {
    let coordinator = coordinator.clone();
    Arc::new(move |node: &str| {
        coordinator
            .member(node)
            .ok()
            .and_then(|r| r["generation"].as_u64())
    })
}

impl Epoch {
    /// Attach the owner side of an already-issued, already-delivered route
    /// envelope: verify it against the seed, dial every stage, build the M16
    /// runtime and serve the request gateway on a fresh loopback port.
    pub fn open(
        coordinator: Arc<seed::Coordinator>,
        envelope: Value,
        owner: iroh::SecretKey,
        carriers: Carriers,
        codec: Arc<dyn Codec>,
    ) -> Result<Self, PublicError> {
        let poll = span(B::OwnerPoll)?;
        let connection_budget = span(B::ModelSelection)?;
        // Each request holds one connection; more open connections than the
        // gateway admits pending requests are answered busy, never held.
        let max_connections = usize::try_from(
            B::PyFinalMyceliumRequestGatewayServiceMaxPendingRequestsL310C36
                .count()
                .map_err(|_| fail("budget_unavailable"))?
                .get(),
        )
        .map_err(|_| fail("budget_unavailable"))?;
        let bearer = crate::app_host::hex(&crate::app_host::random(32)?);
        let host = Arc::new(RouteHost::new(
            coordinator.clone(),
            envelope,
            codec.clone(),
            bearer.clone(),
        )?);
        let message = host.message().clone();
        let current = currentness(&coordinator);
        let (executor, handle) = match carriers {
            Carriers::Loopback(peers) => {
                RouteExecutor::connect(message.clone(), owner, peers, current, codec)
            }
            Carriers::Lan {
                interface,
                policy,
                verified,
            } => RouteExecutor::connect_lan(
                message.clone(),
                owner,
                verified,
                interface,
                policy,
                current,
                codec,
            ),
        }?;
        host.attach(handle.clone())?;
        let runtime = m16_route::runtime(
            &message,
            Arc::new(Wall),
            Arc::new(Wall),
            Arc::new(Ids::default()),
        )?;
        let host_dyn: Arc<dyn crate::m16_requests::Host> = host.clone();
        let config = Config::new(64, 4, 0.0).map_err(|_| fail("registry_config_invalid"))?;
        let service = M16Requests::new(runtime, host_dyn, Box::new(executor), config)?;
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .map_err(|_| fail("joined_owner_gateway_bind_failed"))?;
        let addr = listener
            .local_addr()
            .map_err(|_| fail("joined_owner_gateway_bind_failed"))?;
        let stop = Arc::new(AtomicBool::new(false));
        let refusals = Arc::new(Mutex::new(Vec::new()));
        let mut epoch = Self {
            service,
            handle,
            host,
            addr,
            bearer,
            stop,
            threads: Vec::new(),
            refusals,
        };
        // Gateway: one owned thread per connection, all joined on close.
        // Finished connections are joined as new ones arrive (REVIEW-laneB16
        // F3), so the owned list is bounded by the live connections.
        let (service, stop) = (epoch.service.clone(), epoch.stop.clone());
        let gateway = std::thread::Builder::new()
            .name("joined-owner-gateway".into())
            .spawn(move || {
                let mut connections: Vec<std::thread::JoinHandle<()>> = Vec::new();
                for incoming in listener.incoming() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    let mut stream = match incoming {
                        Ok(stream) => stream,
                        Err(e) => {
                            eprintln!("joined owner gateway accept: {e}");
                            continue;
                        }
                    };
                    let (finished, live): (Vec<_>, Vec<_>) =
                        connections.drain(..).partition(|c| c.is_finished());
                    connections = live;
                    for c in finished {
                        if c.join().is_err() {
                            eprintln!("joined owner gateway connection panicked");
                        }
                    }
                    if connections.len() >= max_connections {
                        busy(&mut stream);
                        continue;
                    }
                    let service = service.clone();
                    let spawned = std::thread::Builder::new()
                        .name("joined-owner-connection".into())
                        .spawn(move || {
                            let gateway = crate::requests::Gateway::new(service);
                            if let Err(e) = crate::http::serve_response_connection(
                                stream,
                                connection_budget,
                                crate::requests::MAX_REQUEST_BODY,
                                |r| gateway.preflight(r),
                                |r| gateway.handle(r),
                            ) {
                                // Bounded polls end streams; recorded, not dropped.
                                eprintln!("joined owner gateway connection ended: {e:?}");
                            }
                        });
                    match spawned {
                        Ok(t) => connections.push(t),
                        Err(e) => eprintln!("joined owner gateway connection thread: {e}"),
                    }
                }
                for c in connections {
                    if c.join().is_err() {
                        eprintln!("joined owner gateway connection panicked");
                    }
                }
            })
            .map_err(|_| fail("joined_owner_thread_failed"))?;
        epoch.threads.push(gateway);
        // Pump: event-driven by carrier changes, bounded by the owner poll.
        let (service, stop, handle, refusals) = (
            epoch.service.clone(),
            epoch.stop.clone(),
            epoch.handle.clone(),
            epoch.refusals.clone(),
        );
        let pump = std::thread::Builder::new()
            .name("joined-owner-pump".into())
            .spawn(move || {
                let mut seen = 0;
                while !stop.load(Ordering::SeqCst) {
                    if let Err(e) = service.tick() {
                        // Distinct closed codes in first-seen order: bounded by
                        // the code set, however long the epoch runs.
                        let mut r = refusals.lock().unwrap_or_else(|p| p.into_inner());
                        if !r.iter().any(|seen| seen == e.code) {
                            r.push(e.code.to_owned());
                        }
                    }
                    seen = handle.wait(seen, poll);
                }
            });
        match pump {
            Ok(t) => epoch.threads.push(t),
            Err(_) => {
                // Drop joins the gateway thread already started.
                return Err(fail("joined_owner_thread_failed"));
            }
        }
        Ok(epoch)
    }
    /// The loopback request gateway (`POST /v1/inference`, SSE, `DELETE` Stop).
    pub fn addr(&self) -> SocketAddr {
        self.addr
    }
    /// Bearer the gateway requires (private to the owner operator).
    pub fn bearer(&self) -> &str {
        &self.bearer
    }
    pub fn handle(&self) -> &RouteHandle {
        &self.handle
    }
    pub fn host(&self) -> &Arc<RouteHost> {
        &self.host
    }
    pub fn service(&self) -> &M16Requests {
        &self.service
    }
    /// Member worker pids named by the seed-signed route (each member's own
    /// signed resident proof); probes only, never kill targets.
    pub fn worker_pids(&self) -> Vec<i32> {
        self.host.message()["stages"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|s| s["worker_pid"].as_i64())
            .filter_map(|p| i32::try_from(p).ok())
            .collect()
    }
    /// The current qualification (refused once the epoch is spent/retired).
    pub fn qualification(&self) -> Result<Value, PublicError> {
        self.host.qualification()
    }
    /// Refusals the pump recorded so far (distinct codes, first-seen order).
    pub fn refusals(&self) -> Vec<String> {
        self.refusals
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .clone()
    }
    fn shutdown(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        // Wake the accept loop; a refused connect means it already exited.
        if let Err(e) = TcpStream::connect(self.addr) {
            eprintln!("joined owner gateway wake: {e}");
        }
        self.handle.changed.notify_all();
        for t in self.threads.drain(..) {
            if t.join().is_err() {
                eprintln!("joined owner epoch thread panicked");
            }
        }
    }
    /// Stop the gateway and pump, join every owned thread (freeing the port),
    /// and return the pump's recorded refusals.
    pub fn close(mut self) -> Vec<String> {
        self.shutdown();
        self.refusals()
    }
}
impl Drop for Epoch {
    fn drop(&mut self) {
        if !self.threads.is_empty() {
            self.shutdown();
        }
    }
}

/// Answer a connection past the gateway's bound at once (typed 503, no
/// request read) and close it; nothing is owned for it.
fn busy(stream: &mut TcpStream) {
    use std::io::Write;
    let body = r#"{"error":"joined_owner_gateway_busy"}"#;
    let reply = format!(
        "HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    // A fresh socket's send buffer takes these few bytes without blocking.
    if let Err(e) = stream
        .set_nonblocking(true)
        .and_then(|()| stream.write_all(reply.as_bytes()))
        .and_then(|()| stream.shutdown(std::net::Shutdown::Write))
    {
        eprintln!("joined owner gateway busy reply: {e}");
    }
}
