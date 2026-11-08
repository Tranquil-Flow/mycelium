// SPDX-License-Identifier: AGPL-3.0-only
//! M16 carrier executor and host for a verified `mycelium.joined_route.v1`.
//!
//! The owner dials each stage member over the Router ALPN (LocalOnly Iroh, exact
//! enrolled carrier endpoint, membership-generation fences) and relays unchanged
//! Router v1 frames: entry HopHeader -> stage 0 -> HopHeader -> stage 1 -> TokenEvent.
//! Members do not dial each other; the owner is the hub. One route epoch serves ONE
//! request (native workers have no KV reset): rotate executor, runtime and route
//! after settlement. Executor callbacks are nonblocking; carrier I/O runs on an
//! owned thread. Cleanup is true only after every stage ACKed its PathCancellation
//! (members ACK after native close/reap) or the host supplied actual death
//! evidence for a stage. Never synthesizes tokens; route_ready stays false.
use crate::{
    app_host,
    joined_shard::route::{self, Path},
    m16_requests::{Dispatch, Execution, Executor, Host, Prepared},
    schema::PublicError,
};
use budgets::{BudgetId as B, Entries, MonoInstant};
use iroh::{EndpointAddr, EndpointId, endpoint::Connection};
use protocol::router_wire;
use runtime::{
    iroh::IrohTransport,
    iroh_host::LoopbackHost,
    iroh_receive::{DispatchRefusal, RouterReceiver},
    m16_coordinator::{SharedClock, SharedIdSource},
    m16_factory::{DeviceFacts, FactoryInputs, LiveM16Runtime, build_live_m16_runtime},
    m16_router_contracts::{DeviceState, IdMap, PyReal, RequestContext, RouterConfig},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    net::SocketAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    time::Instant,
};
use transport::{Channel, Clock, ConnectionFence, Exchange};

fn uint(value: &PyReal) -> Option<u64> {
    match value {
        PyReal::Int {
            negative: false,
            magnitude,
        } => magnitude.to_string().parse().ok(),
        _ => None,
    }
}
fn error(code: &'static str) -> PublicError {
    PublicError::new(409, code)
}
fn deadline(clock: &impl Clock, id: B) -> Result<MonoInstant, &'static str> {
    clock
        .monotonic()
        .checked_add(id.span(Entries::ZERO).map_err(|_| "budget_unavailable")?)
        .map_err(|_| "deadline_invalid")
}
/// Product tokenizer seam: the host's selected local model tokenizer.
pub trait Codec: Send + Sync {
    fn encode(&self, text: &str) -> Result<Vec<u32>, PublicError>;
    fn decode(&self, token: u32) -> Result<String, PublicError>;
}
/// Current member generation from the seed (None = no longer current).
pub type Currentness = Arc<dyn Fn(&str) -> Option<u64> + Send + Sync>;
/// Dial target for one stage. The socket is a LocalOnly loopback address.
#[derive(Clone)]
pub struct StagePeer {
    pub node_id: String,
    pub endpoint: EndpointId,
    pub socket: SocketAddr,
    pub generation: u64,
}
/// Per-stage observable teardown outcome, for host status and qualification.
#[derive(Clone, Debug, Default)]
pub struct StageTeardown {
    pub acknowledged_ms: Option<f64>,
    pub failure: Option<String>,
    pub death_evidence: Option<Value>,
}
#[derive(Default)]
pub struct Observed {
    pub ready: bool,
    pub setup_failure: Option<String>,
    pub request_id: Option<String>,
    pub started: Option<Instant>,
    pub token_times_ms: Vec<f64>,
    pub tokens: Vec<u32>,
    pub completed: bool,
    pub failure: Option<&'static str>,
    pub failed_at_ms: Option<f64>,
    pub teardown_requested_ms: Option<f64>,
    pub stages: Vec<StageTeardown>,
    pub teardown_finished: bool,
    pub carrier_closed: bool,
}
/// Observable carrier state plus a change signal for an event-driven host pump.
#[derive(Default)]
pub struct RouteWatch {
    pub state: Mutex<Observed>,
    pub changed: std::sync::Condvar,
}
impl RouteWatch {
    /// Wait (bounded) for a carrier state change; returns immediately on change.
    pub fn wait(&self, seen: usize, timeout: std::time::Duration) -> usize {
        let Ok(guard) = self.state.lock() else {
            return seen;
        };
        let generation = |o: &Observed| {
            o.tokens.len()
                + usize::from(o.completed)
                + usize::from(o.failure.is_some())
                + usize::from(o.teardown_finished) * 2
        };
        if generation(&guard) != seen {
            return generation(&guard);
        }
        match self.changed.wait_timeout(guard, timeout) {
            Ok((g, _)) => generation(&g),
            Err(_) => seen,
        }
    }
}
pub type RouteHandle = Arc<RouteWatch>;
enum Command {
    Start {
        path: Path,
        prompt: Vec<u32>,
        max_new_tokens: u64,
    },
    Teardown(Option<Path>),
}
pub struct RouteExecutor {
    message: Value,
    commands: Option<mpsc::Sender<Command>>,
    observed: RouteHandle,
    pending: VecDeque<u32>,
    taken: usize,
    codec: Arc<dyn Codec>,
    path: Option<Path>,
    teardown_sent: bool,
    thread: Option<std::thread::JoinHandle<()>>,
    cancel: Arc<AtomicBool>,
}
impl RouteExecutor {
    /// Bind the owner carrier with its route-pinned key and dial every stage now.
    pub fn connect(
        message: Value,
        secret: iroh::SecretKey,
        peers: Vec<StagePeer>,
        currentness: Currentness,
        codec: Arc<dyn Codec>,
    ) -> Result<(Self, RouteHandle), PublicError> {
        let stages = message["stages"]
            .as_array()
            .ok_or(error("joined_route_invalid"))?;
        if stages.len() != peers.len()
            || secret.public().to_string() != message["owner_endpoint_id"]
            || stages.iter().zip(&peers).any(|(s, p)| {
                s["member"]["node_id"] != p.node_id
                    || s["carrier_endpoint_id"] != p.endpoint.to_string()
                    || s["member"]["generation"] != p.generation
                    || !p.socket.ip().is_loopback()
            })
        {
            return Err(error("joined_route_peer_mismatch"));
        }
        // The owner fences each carrier on the member generation the route signed.
        for (p, stage) in peers
            .iter()
            .zip(message["stages"].as_array().into_iter().flatten())
        {
            if stage["member"]["generation"].as_u64() != Some(p.generation)
                || currentness(&p.node_id) != Some(p.generation)
            {
                return Err(error("joined_route_member_generation_changed"));
            }
        }
        let observed: RouteHandle = Arc::new(RouteWatch {
            state: Mutex::new(Observed {
                stages: vec![StageTeardown::default(); peers.len()],
                ..Observed::default()
            }),
            changed: std::sync::Condvar::new(),
        });
        let (tx, rx) = mpsc::channel();
        let cancel = Arc::new(AtomicBool::new(false));
        let (shared, flag, route) = (observed.clone(), cancel.clone(), message.clone());
        let thread = std::thread::spawn(move || {
            if let Err(code) = carrier(&route, secret, &peers, &currentness, &rx, &shared, &flag)
                && let Ok(mut o) = shared.state.lock()
            {
                o.setup_failure.get_or_insert(code.to_owned());
            }
        });
        let ready_by = Instant::now()
            + std::time::Duration::from_millis(
                B::WorkerStartup
                    .span(Entries::ZERO)
                    .map_err(|_| error("budget_unavailable"))?
                    .millis(),
            );
        loop {
            {
                let o = observed
                    .state
                    .lock()
                    .map_err(|_| error("joined_route_poisoned"))?;
                if o.ready {
                    break;
                }
                if o.setup_failure.is_some() || Instant::now() >= ready_by {
                    return Err(error("joined_route_connect_failed"));
                }
            }
            std::thread::yield_now();
        }
        Ok((
            Self {
                message,
                commands: Some(tx),
                observed: observed.clone(),
                pending: VecDeque::new(),
                taken: 0,
                codec,
                path: None,
                teardown_sent: false,
                thread: Some(thread),
                cancel,
            },
            observed,
        ))
    }
}
impl Drop for RouteExecutor {
    fn drop(&mut self) {
        // Emergency only: closes connections; never a cleanup receipt.
        self.cancel.store(true, Ordering::SeqCst);
        self.commands.take();
        if let Some(t) = self.thread.take()
            && t.join().is_err()
        {
            eprintln!("joined_route_carrier_panicked");
        }
    }
}
impl Executor for RouteExecutor {
    fn start(&mut self, dispatch: &Dispatch) -> Result<(), PublicError> {
        let hops = dispatch.manifest["ordered_hops"]
            .as_array()
            .ok_or(error("joined_route_manifest_invalid"))?;
        let stages = self.message["stages"]
            .as_array()
            .ok_or(error("joined_route_invalid"))?;
        if hops.len() != stages.len()
            || hops
                .iter()
                .zip(stages)
                .any(|(h, s)| h["placement_id"] != s["placement_id"])
            || dispatch.identity.request_id != dispatch.context.request_id
        {
            return Err(error("joined_route_manifest_mismatch"));
        }
        let prompt = dispatch
            .context
            .prompt_token_ids
            .iter()
            .map(|t| {
                uint(t)
                    .and_then(|v| u32::try_from(v).ok())
                    .ok_or(error("joined_route_tokens_invalid"))
            })
            .collect::<Result<Vec<u32>, _>>()?;
        let max_new_tokens =
            uint(&dispatch.context.max_new_tokens).ok_or(error("joined_route_tokens_invalid"))?;
        let path = Path {
            request_id: dispatch.identity.request_id.clone(),
            path_id: dispatch.identity.path_id.clone(),
            path_attempt: dispatch.identity.path_attempt,
        };
        self.path = Some(path.clone());
        self.commands
            .as_ref()
            .ok_or(error("joined_route_closed"))?
            .send(Command::Start {
                path,
                prompt,
                max_new_tokens,
            })
            .map_err(|_| error("joined_route_carrier_failed"))
    }
    fn poll(&mut self, _: &str) -> Result<Execution, PublicError> {
        let mut o = self
            .observed
            .state
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?;
        let fresh: Vec<u32> = o.tokens[self.taken..].to_vec();
        self.taken = o.tokens.len();
        drop(o);
        self.pending.extend(fresh);
        if let Some(token) = self.pending.pop_front() {
            // Count only tokens actually handed to M16.
            let text = self.codec.decode(token)?;
            return Ok(Execution::Token {
                token_id: token,
                text,
            });
        }
        o = self
            .observed
            .state
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?;
        if let Some(code) = o.failure {
            return Err(error(code));
        }
        // A carrier that exited after readiness without completing is a failure,
        // never an indefinitely pending request.
        if !o.completed
            && (o.setup_failure.is_some() || self.thread.as_ref().is_some_and(|t| t.is_finished()))
        {
            return Err(error("joined_route_carrier_failed"));
        }
        Ok(if o.completed {
            Execution::Completed
        } else {
            Execution::Pending
        })
    }
    fn cleanup(&mut self, _: &str, cancel: bool) -> Result<bool, PublicError> {
        if !self.teardown_sent {
            if cancel {
                self.cancel.store(true, Ordering::SeqCst);
            }
            self.commands
                .as_ref()
                .ok_or(error("joined_route_closed"))?
                .send(Command::Teardown(self.path.clone()))
                .map_err(|_| error("joined_route_carrier_failed"))?;
            self.teardown_sent = true;
            return Ok(false);
        }
        let o = self
            .observed
            .state
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?;
        if !o.teardown_finished {
            return Ok(false);
        }
        if o.stages
            .iter()
            .all(|s| s.acknowledged_ms.is_some() || s.death_evidence.is_some())
        {
            return Ok(true);
        }
        // Retained debt: a stage neither acknowledged nor has death evidence.
        Err(PublicError::new(503, "joined_route_cleanup_unproven"))
    }
}
/// Host-supplied death evidence (exact owned process exit + absent worker PID).
/// Only an unacknowledged stage can be settled this way.
pub fn settle_dead_stage(
    handle: &RouteHandle,
    stage: usize,
    evidence: Value,
) -> Result<(), PublicError> {
    let mut o = handle
        .state
        .lock()
        .map_err(|_| error("joined_route_poisoned"))?;
    let s = o
        .stages
        .get_mut(stage)
        .ok_or(error("joined_route_invalid"))?;
    if s.acknowledged_ms.is_some()
        || evidence["process_exited"] != true
        || evidence["worker_absent"] != true
    {
        return Err(error("joined_route_death_evidence_invalid"));
    }
    s.death_evidence = Some(evidence);
    // Exact-process death evidence fails an in-flight request now, instead of
    // waiting for the hop deadline on a carrier to a peer that cannot answer.
    if !o.completed && !o.teardown_finished && o.failure.is_none() {
        o.failure = Some("joined_route_stage_died");
        o.failed_at_ms = o.started.map(elapsed_ms);
    }
    drop(o);
    handle.changed.notify_all();
    Ok(())
}
fn elapsed_ms(since: Instant) -> f64 {
    since.elapsed().as_secs_f64() * 1000.0
}
type Fence = Box<dyn Fn() -> ConnectionFence + Send + Sync>;
struct Link<'a> {
    send: IrohTransport<'a, app_host_clock::Clock, Fence>,
    receive: RouterReceiver<'a, app_host_clock::Clock, Fence>,
    fence: ConnectionFence,
    sent: u128,
    received: u128,
}
mod app_host_clock {
    pub type Clock = crate::managed_carrier::HostClock;
}
impl Link<'_> {
    /// Teardown exchange: keeps the transport error detail for the evidence.
    fn send_detailed(
        &mut self,
        clock: &app_host_clock::Clock,
        bytes: Vec<u8>,
        id: B,
    ) -> Result<(), String> {
        let request = Exchange {
            peer: self.fence,
            channel: Channel::Router,
            message_id: self.sent.to_be_bytes(),
            payload: bytes,
            deadline: deadline(clock, id)?,
            maximum_response_bytes: protocol::remote::REMOTE_MAX_FRAME_BYTES,
        };
        transport::exchange(&mut self.send, &request, clock)
            .map_err(|e| format!("joined_route_teardown_failed:{e:?}"))?;
        self.sent += 1;
        Ok(())
    }
    fn send(
        &mut self,
        clock: &app_host_clock::Clock,
        bytes: Vec<u8>,
        id: B,
    ) -> Result<(), &'static str> {
        let request = Exchange {
            peer: self.fence,
            channel: Channel::Router,
            message_id: self.sent.to_be_bytes(),
            payload: bytes,
            deadline: deadline(clock, id)?,
            maximum_response_bytes: protocol::remote::REMOTE_MAX_FRAME_BYTES,
        };
        transport::exchange(&mut self.send, &request, clock).map_err(|e| match e {
            transport::Error::Expired => "joined_route_hop_deadline",
            transport::Error::Generation => "joined_route_generation_changed",
            _ => "joined_route_carrier_failed",
        })?;
        self.sent += 1;
        Ok(())
    }
    /// Receive one frame; the caller validates, then this ACKs (single dispatch slot).
    fn receive(
        &mut self,
        clock: &app_host_clock::Clock,
        id: B,
        accept: &dyn Fn(&router_wire::DecodedFrame) -> bool,
    ) -> Result<router_wire::DecodedFrame, &'static str> {
        let delivery = self
            .receive
            .receive(deadline(clock, id)?)
            .map_err(|e| match e {
                transport::Error::Expired => "joined_route_hop_deadline",
                transport::Error::Generation => "joined_route_generation_changed",
                _ => "joined_route_carrier_failed",
            })?;
        if delivery.message_id() != self.received.to_be_bytes() {
            delivery
                .reject(DispatchRefusal::ReplayCollision)
                .map_err(|_| "joined_route_carrier_failed")?;
            return Err("joined_route_frame_sequence");
        }
        let frame = router_wire::decode_frame(delivery.payload())
            .map_err(|_| "joined_route_frame_invalid")?;
        if !accept(&frame) {
            delivery
                .reject(DispatchRefusal::InvalidFrame)
                .map_err(|_| "joined_route_carrier_failed")?;
            return Err("joined_route_frame_binding");
        }
        delivery
            .acknowledge()
            .map_err(|_| "joined_route_carrier_failed")?;
        self.received += 1;
        Ok(frame)
    }
}
#[allow(clippy::too_many_arguments)]
fn carrier(
    message: &Value,
    secret: iroh::SecretKey,
    peers: &[StagePeer],
    currentness: &Currentness,
    commands: &mpsc::Receiver<Command>,
    observed: &RouteHandle,
    cancel: &Arc<AtomicBool>,
) -> Result<(), &'static str> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .map_err(|_| "joined_route_runtime_failed")?;
    let clock = crate::managed_carrier::HostClock::default();
    let mut host =
        LoopbackHost::bind_with_secret(&rt, &clock, deadline(&clock, B::WorkerStartup)?, secret)
            .map_err(|_| "joined_route_bind_failed")?;
    let epoch = message["topology_version"]
        .as_u64()
        .ok_or("joined_route_invalid")?;
    // Per member: data connection (hops/results) THEN an independent control
    // connection (PathCancellation). A failed data exchange closes only its own
    // connection, so Stop/teardown can still reach a slow or resumed member.
    let mut data: Vec<Connection> = Vec::new();
    let mut control: Vec<Connection> = Vec::new();
    for p in peers {
        for set in [&mut data, &mut control] {
            set.push(
                host.connect_channel(
                    &EndpointAddr::new(p.endpoint).with_ip_addr(p.socket),
                    Channel::Router,
                    &clock,
                    deadline(&clock, B::WorkerStartup)?,
                )
                .map_err(|_| "joined_route_connect_failed")?,
            );
        }
    }
    let fence = |p: &StagePeer| -> (ConnectionFence, Fence) {
        let base = ConnectionFence {
            endpoint_id: *p.endpoint.as_bytes(),
            local_generation: epoch,
            remote_generation: p.generation,
        };
        let (node, current) = (p.node_id.clone(), currentness.clone());
        (
            base,
            Box::new(move || ConnectionFence {
                remote_generation: current(&node).unwrap_or(u64::MAX),
                ..base
            }),
        )
    };
    let link = |c: &Connection, p: &StagePeer| -> Result<Link<'_>, &'static str> {
        let (base, f1) = fence(p);
        let (_, f2) = fence(p);
        Ok(Link {
            send: IrohTransport::new(c.clone(), &rt, &clock, base, f1)
                .map_err(|_| "joined_route_generation_changed")?,
            receive: RouterReceiver::new(c.clone(), &rt, &clock, base, f2)
                .map_err(|_| "joined_route_generation_changed")?,
            fence: base,
            sent: 0,
            received: 0,
        })
    };
    observed
        .state
        .lock()
        .map_err(|_| "joined_route_poisoned")?
        .ready = true;
    let result = std::thread::scope(|scope| -> Result<(), &'static str> {
        let mut worker = None;
        for command in commands.iter() {
            match command {
                Command::Start {
                    path,
                    prompt,
                    max_new_tokens,
                } => {
                    if worker.is_some() {
                        return Err("joined_route_single_request");
                    }
                    let mut links: Vec<Link<'_>> = data
                        .iter()
                        .zip(peers)
                        .map(|(c, p)| link(c, p))
                        .collect::<Result<_, _>>()?;
                    let (clock, cancel) = (&clock, cancel.clone());
                    worker = Some(scope.spawn(move || {
                        let began = Instant::now();
                        if let Ok(mut o) = observed.state.lock() {
                            o.request_id = Some(path.request_id.clone());
                            o.started = Some(began);
                        }
                        let outcome = generate(
                            message,
                            &path,
                            prompt,
                            max_new_tokens,
                            &mut links,
                            clock,
                            observed,
                            &cancel,
                            began,
                        );
                        if let Ok(mut o) = observed.state.lock()
                            && !cancel.load(Ordering::SeqCst)
                        {
                            match outcome {
                                Ok(()) => o.completed = true,
                                Err(code) => {
                                    o.failure.get_or_insert(code);
                                    o.failed_at_ms = Some(elapsed_ms(began));
                                }
                            }
                        }
                        observed.changed.notify_all();
                    }));
                }
                Command::Teardown(path) => {
                    let began = Instant::now();
                    cancel.store(true, Ordering::SeqCst);
                    if let Ok(mut o) = observed.state.lock() {
                        o.teardown_requested_ms = o.started.map(elapsed_ms);
                    }
                    let path = path.unwrap_or(Path {
                        request_id: "unstarted".into(),
                        path_id: "unstarted".into(),
                        path_attempt: 0,
                    });
                    let body = route::cancellation(message, &path);
                    let frame = router_wire::encode_frame("PathCancellation", &body, &[])
                        .map_err(|_| "joined_route_frame_invalid")?;
                    // Concurrent per-member control exchanges; a member ACKs only
                    // after its native worker was closed or killed and reaped.
                    let outcomes: Vec<Result<f64, String>> = std::thread::scope(|inner| {
                        let dead: Vec<bool> = observed
                            .state
                            .lock()
                            .map(|o| {
                                o.stages
                                    .iter()
                                    .map(|s| s.death_evidence.is_some())
                                    .collect()
                            })
                            .unwrap_or_default();
                        let handles: Vec<_> = control
                            .iter()
                            .zip(peers)
                            .enumerate()
                            .map(|(i, (c, p))| {
                                let (frame, clock, link) = (frame.clone(), &clock, &link);
                                let dead = dead.get(i).copied().unwrap_or(false);
                                inner.spawn(move || -> Result<f64, String> {
                                    if dead {
                                        // Settled by host death evidence; nothing can ACK.
                                        return Err("joined_route_stage_dead".into());
                                    }
                                    link(c, p)?.send_detailed(clock, frame, B::RouterClose)?;
                                    Ok(elapsed_ms(began))
                                })
                            })
                            .collect();
                        handles
                            .into_iter()
                            .map(|h| {
                                h.join()
                                    .unwrap_or(Err("joined_route_teardown_panicked".into()))
                            })
                            .collect()
                    });
                    for c in data.iter().chain(&control) {
                        c.close(0u32.into(), b"route_retired");
                    }
                    if let Some(w) = worker.take()
                        && w.join().is_err()
                    {
                        return Err("joined_route_worker_panicked");
                    }
                    let mut o = observed.state.lock().map_err(|_| "joined_route_poisoned")?;
                    for (s, outcome) in o.stages.iter_mut().zip(outcomes) {
                        match outcome {
                            Ok(ms) => s.acknowledged_ms = Some(ms),
                            Err(code) => s.failure = Some(code),
                        }
                    }
                    o.teardown_finished = true;
                    drop(o);
                    observed.changed.notify_all();
                    break;
                }
            }
        }
        cancel.store(true, Ordering::SeqCst);
        for c in data.iter().chain(&control) {
            c.close(0u32.into(), b"route_retired");
        }
        if let Some(w) = worker.take()
            && w.join().is_err()
        {
            return Err("joined_route_worker_panicked");
        }
        Ok(())
    });
    drop(data);
    drop(control);
    let closed = host.close(&clock, deadline(&clock, B::RouterClose)?);
    drop(host);
    drop(rt);
    if let Ok(mut o) = observed.state.lock() {
        o.carrier_closed = closed.is_ok();
    }
    result
}
#[allow(clippy::too_many_arguments)]
fn generate(
    message: &Value,
    path: &Path,
    prompt: Vec<u32>,
    max_new_tokens: u64,
    links: &mut [Link<'_>],
    clock: &app_host_clock::Clock,
    observed: &RouteHandle,
    cancel: &AtomicBool,
    began: Instant,
) -> Result<(), &'static str> {
    let last = links.len() - 1;
    let eos = [151_645u32, 151_643];
    let mut input = prompt;
    for t in 0..max_new_tokens {
        if cancel.load(Ordering::SeqCst) {
            return Ok(());
        }
        // Prefill may include the worker's first kernel compilation.
        let budget = if t == 0 {
            B::WorkerStartup
        } else {
            B::PyFinalScriptsRunA11RouteBenchmarkTargetTpotMsL398C12
        };
        let header = route::hop(message, path, t, 0).map_err(|e| e.0)?;
        let frame = router_wire::encode_frame(
            "HopHeader",
            &header,
            &route::entry_payload(&input).map_err(|e| e.0)?,
        )
        .map_err(|_| "joined_route_frame_invalid")?;
        links[0].send(clock, frame, budget)?;
        for i in 0..last {
            let expected = route::hop(message, path, t, i + 1).map_err(|e| e.0)?;
            let frame = links[i].receive(clock, budget, &|f| {
                route::body_matches(f, "HopHeader", &expected)
            })?;
            if cancel.load(Ordering::SeqCst) {
                return Ok(());
            }
            // Relay the exact bytes; the next stage re-validates the binding.
            links[i + 1].send(
                clock,
                frame.encode().map_err(|_| "joined_route_frame_invalid")?,
                budget,
            )?;
        }
        let token = std::cell::Cell::new(None);
        links[last].receive(clock, budget, &|f| {
            let id = f.body()["token_id"]
                .as_u64()
                .and_then(|v| u32::try_from(v).ok());
            let ok = id.is_some_and(|id| {
                route::body_matches(f, "TokenEvent", &route::token_event(path, t, id))
            });
            if ok {
                token.set(id);
            }
            ok
        })?;
        let token = token.get().ok_or("joined_route_frame_binding")?;
        {
            let mut o = observed.state.lock().map_err(|_| "joined_route_poisoned")?;
            o.tokens.push(token);
            o.token_times_ms.push(elapsed_ms(began));
        }
        observed.changed.notify_all();
        if eos.contains(&token) {
            return Ok(());
        }
        input = vec![token];
    }
    Ok(())
}
/// Device facts from the route's member-signed prepare values. Memory and KV
/// capacity are member consent/prepare observations; compute rate is a planning
/// rate from the typed TPOT target row, explicitly NOT a measured throughput.
pub fn runtime(
    message: &Value,
    clock: SharedClock,
    lease_clock: SharedClock,
    ids: SharedIdSource,
) -> Result<LiveM16Runtime, PublicError> {
    let graph = route::graph(message).map_err(|e| error(e.0))?;
    let tpot = B::PyFinalMyceliumRequestGatewayBackendTargetTpotMsL235C12
        .span(Entries::ZERO)
        .map_err(|_| error("budget_unavailable"))?
        .millis();
    let mut devices = BTreeMap::new();
    for s in message["stages"]
        .as_array()
        .ok_or(error("joined_route_invalid"))?
    {
        let layers = s["range"][1].as_u64().unwrap_or(0) - s["range"][0].as_u64().unwrap_or(0);
        let tokens = s["tokens"].as_u64().ok_or(error("joined_route_invalid"))?;
        let memory = s["memory"].as_u64().ok_or(error("joined_route_invalid"))?;
        let node = s["member"]["node_id"].clone();
        devices.insert(
            node.as_str()
                .ok_or(error("joined_route_invalid"))?
                .to_owned(),
            DeviceFacts {
                fast_allocatable_bytes: memory,
                state: DeviceState {
                    node_id: node,
                    state_seq: PyReal::int(1),
                    last_updated: PyReal::Float(lease_clock.now()),
                    availability: "ALIVE".into(),
                    compute_units_per_second: PyReal::from_json(&json!(
                        layers * 1000 / tpot.max(1)
                    ))
                    .ok_or(error("joined_route_invalid"))?,
                    free_compute_fraction: PyReal::int(1),
                    available_kv_bytes: PyReal::from_json(&json!(
                        tokens * layers * 8 * 128 * 2 * 4
                    ))
                    .ok_or(error("joined_route_invalid"))?,
                    pending_hop_queue_depth: PyReal::int(0),
                    neighbor_rtt_ms: IdMap::new(),
                    neighbor_bandwidth_bytes_per_second: IdMap::new(),
                },
            },
        );
    }
    build_live_m16_runtime(FactoryInputs {
        graph,
        devices,
        workload_profiles: BTreeMap::from([("interactive".into(), "interactive".into())]),
        clock,
        lease_clock,
        id_source: ids,
        config: RouterConfig::oracle_default().map_err(|_| error("m16_config_invalid"))?,
        max_concurrent_requests: 1,
    })
    .map_err(|e| PublicError::new(409, Box::leak(e.code().to_owned().into_boxed_str())))
}
/// Owner request host: current route qualification, selected tokenizer, bearer.
pub struct RouteHost {
    coordinator: Arc<seed::Coordinator>,
    route: Value,
    message: Value,
    qualification: Value,
    codec: Arc<dyn Codec>,
    bearer: String,
    next: AtomicU64,
    withdrawn: Mutex<Option<&'static str>>,
}
impl RouteHost {
    pub fn new(
        coordinator: Arc<seed::Coordinator>,
        route: Value,
        codec: Arc<dyn Codec>,
        bearer: String,
    ) -> Result<Self, PublicError> {
        let message =
            route::verify_owner(&coordinator, &route, app_host::now()).map_err(|e| error(e.0))?;
        let qualification = route::qualification(&message).map_err(|e| error(e.0))?;
        Ok(Self {
            coordinator,
            route,
            message,
            qualification,
            codec,
            bearer,
            next: AtomicU64::new(0),
            withdrawn: Mutex::new(None),
        })
    }
    pub fn message(&self) -> &Value {
        &self.message
    }
    /// A verified member-signed withdrawal retires the route (not-ready): the
    /// public refusal is the gateway's existing `readiness_revoked` (409); the
    /// returned notice is the withdrawal evidence.
    pub fn withdraw(&self, offer: &Value, envelope: &Value) -> Result<Value, PublicError> {
        let notice = crate::joined_shard::verify_withdrawal(&self.coordinator, offer, envelope)
            .map_err(|e| error(e.0))?;
        if !self.message["stages"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|s| s["offer_digest"] == notice["offer_digest"])
        {
            return Err(error("joined_route_stage_mismatch"));
        }
        *self
            .withdrawn
            .lock()
            .map_err(|_| error("joined_route_poisoned"))? = Some("readiness_revoked");
        Ok(notice)
    }
}
impl Host for RouteHost {
    fn authorized(&self, request: &crate::http::Request) -> bool {
        request.header("authorization") == Some(&format!("Bearer {}", self.bearer))
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        if let Some(code) = *self
            .withdrawn
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?
        {
            return Err(error(code));
        }
        route::verify_owner(&self.coordinator, &self.route, app_host::now())
            .map_err(|_| error("stale_qualification"))?;
        Ok(self.qualification.clone())
    }
    fn prepare(&self, submission: &Value) -> Result<Prepared, PublicError> {
        let prompt = submission["prompt"]
            .as_str()
            .ok_or(error("invalid_prompt"))?;
        let ids = self.codec.encode(prompt)?;
        let max_new = submission["max_new_tokens"]
            .as_u64()
            .ok_or(error("invalid_max_new_tokens"))?;
        // Every stage worker was prepared for a bounded KV context.
        for s in self.message["stages"].as_array().into_iter().flatten() {
            if s["tokens"]
                .as_u64()
                .is_none_or(|cap| ids.len() as u64 + max_new > cap)
            {
                return Err(error("joined_route_context_exceeds_worker"));
            }
        }
        let latency = |id: B| -> Result<PyReal, PublicError> {
            PyReal::from_json(&json!(
                id.span(Entries::ZERO)
                    .map_err(|_| error("budget_unavailable"))?
                    .millis()
            ))
            .ok_or(error("budget_unavailable"))
        };
        let n = self.next.fetch_add(1, Ordering::SeqCst);
        Ok(Prepared {
            context: RequestContext {
                request_id: format!("route-{}-{n}", self.message["topology_version"]),
                prompt_token_ids: ids.iter().map(|&t| PyReal::int(i64::from(t))).collect(),
                max_new_tokens: PyReal::from_json(&json!(max_new))
                    .ok_or(error("invalid_max_new_tokens"))?,
                expected_new_tokens: PyReal::from_json(&json!(max_new))
                    .ok_or(error("invalid_max_new_tokens"))?,
                qos_class: submission["qos_class"]
                    .as_str()
                    .ok_or(error("invalid_workload_binding"))?
                    .to_owned(),
                admitted_at: PyReal::Float(app_host::now()),
                target_ttft_ms: latency(
                    B::PyFinalMyceliumRequestGatewayBackendTargetTtftMsL234C12,
                )?,
                target_tpot_ms: latency(
                    B::PyFinalMyceliumRequestGatewayBackendTargetTpotMsL235C12,
                )?,
                target_tokens_per_second: PyReal::int(1),
                sampling_seed: PyReal::int(0),
                generation_config_digest: protocol::canonical::digest(
                    &json!({"sampling":"greedy"}),
                )
                .map_err(|_| error("generation_config_invalid"))?,
            },
            owner_token: app_host::hex(&app_host::random(32)?),
            excluded_placements: vec![],
        })
    }
}
