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
        // v0.1.4 caps ADMITTED requests (request_gateway/service.py:349,458),
        // not connections: submissions and event streams hold one of
        // `max_admitted` slots; Stop, status and health are never refused for
        // capacity. Connections that have not sent a request yet (no bearer
        // checked) are bounded separately, the oldest dropped for a newcomer.
        let max_admitted = usize::try_from(
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
        // F3), so the owned list is bounded by the live connections: at most
        // `max_admitted` holding a slot plus `max_admitted` not yet read.
        let (service, stop) = (epoch.service.clone(), epoch.stop.clone());
        let admitted = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let unread: Arc<Mutex<std::collections::VecDeque<(u64, TcpStream)>>> = Arc::default();
        let gateway = std::thread::Builder::new()
            .name("joined-owner-gateway".into())
            .spawn(move || {
                let mut connections: Vec<std::thread::JoinHandle<()>> = Vec::new();
                let mut next = 0u64;
                for incoming in listener.incoming() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    let stream = match incoming {
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
                    next += 1;
                    let id = next;
                    let Some(oldest) = unread_admit(&unread, id, &stream, max_admitted) else {
                        continue;
                    };
                    if let Some(oldest) = oldest
                        && let Err(e) = oldest.shutdown(std::net::Shutdown::Both)
                    {
                        eprintln!("joined owner gateway drop of a silent connection: {e}");
                    }
                    let (service, own_unread, admitted) =
                        (service.clone(), unread.clone(), admitted.clone());
                    let spawned = std::thread::Builder::new()
                        .name("joined-owner-connection".into())
                        .spawn(move || {
                            let gateway = crate::requests::Gateway::new(service);
                            let slot = Slot::default();
                            let served = crate::http::serve_response_connection(
                                stream,
                                connection_budget,
                                crate::requests::MAX_REQUEST_BODY,
                                |r| {
                                    unread_done(&own_unread, id);
                                    if let Some(reply) = gateway.preflight(r)? {
                                        return Ok(Some(reply));
                                    }
                                    if counted(r) && !slot.take(&admitted, max_admitted) {
                                        return crate::http::Reply::error(PublicError::new(
                                            503,
                                            "gateway_capacity_exhausted",
                                        ))
                                        .map(Some);
                                    }
                                    Ok(None)
                                },
                                |r| gateway.handle(r),
                            );
                            unread_done(&own_unread, id);
                            slot.release(&admitted);
                            if let Err(e) = served {
                                // Bounded polls end streams; recorded, not dropped.
                                eprintln!("joined owner gateway connection ended: {e:?}");
                            }
                        });
                    match spawned {
                        Ok(t) => connections.push(t),
                        Err(e) => {
                            unread_done(&unread, id);
                            eprintln!("joined owner gateway connection thread: {e}");
                        }
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

/// Register a connection that has not sent its request yet. Past `bound`
/// such connections, the oldest is returned to be dropped (it has had its
/// turn; a silent local process cannot starve the owner's chat or Stop).
/// `None` = the connection could not be registered (it is dropped).
fn unread_admit(
    unread: &Mutex<std::collections::VecDeque<(u64, TcpStream)>>,
    id: u64,
    stream: &TcpStream,
    bound: usize,
) -> Option<Option<TcpStream>> {
    let clone = match stream.try_clone() {
        Ok(clone) => clone,
        Err(e) => {
            eprintln!("joined owner gateway connection handle: {e}");
            return None;
        }
    };
    let mut unread = unread.lock().unwrap_or_else(|p| p.into_inner());
    let oldest = if unread.len() >= bound {
        unread.pop_front().map(|(_, s)| s)
    } else {
        None
    };
    unread.push_back((id, clone));
    Some(oldest)
}
/// The connection's request arrived (or the connection ended).
fn unread_done(unread: &Mutex<std::collections::VecDeque<(u64, TcpStream)>>, id: u64) {
    unread
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .retain(|(i, _)| *i != id);
}
/// Requests that hold an admitted slot: submissions and event streams. Stop
/// (`DELETE`), status and health are always served.
fn counted(r: &crate::http::Request) -> bool {
    r.method == "POST"
        || r.target
            .split('?')
            .next()
            .is_some_and(|p| p.ends_with("/events"))
}
/// One connection's admitted slot, released when the connection ends.
#[derive(Default)]
struct Slot(AtomicBool);
impl Slot {
    fn take(&self, admitted: &std::sync::atomic::AtomicUsize, cap: usize) -> bool {
        let taken = admitted
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                (n < cap).then_some(n + 1)
            })
            .is_ok();
        self.0.store(taken, Ordering::SeqCst);
        taken
    }
    fn release(&self, admitted: &std::sync::atomic::AtomicUsize) {
        if self.0.swap(false, Ordering::SeqCst) {
            admitted.fetch_sub(1, Ordering::SeqCst);
        }
    }
}
