// SPDX-License-Identifier: AGPL-3.0-only
//! M16 carrier executor and host for a verified `mycelium.joined_route.v1`.
//!
//! The owner dials each stage member over the Router ALPN (LocalOnly Iroh, exact
//! enrolled carrier endpoint, membership-generation fences) and relays unchanged
//! Router v1 frames: entry HopHeader -> stage 0 -> HopHeader -> stage 1 -> TokenEvent.
//! Members do not dial each other; the owner is the hub. A `mycelium.joined_route.v1`
//! epoch serves ONE request: rotate executor, runtime and route after settlement. A
//! `mycelium.joined_route.v2` epoch (lane EP1) serves many requests one at a time
//! over the same connections and message counters: a request ends with a
//! request-scoped PathCancellation that each member ACKs only after its native
//! session was reset (`session_reset_v1`) or its compute closed; the next request
//! starts only on stages proven clean; a carrier-level failure, death evidence, a
//! generation change, expiry or `close` ends the epoch. Executor callbacks are nonblocking; carrier I/O runs on an
//! owned thread. Cleanup is true only after every stage ACKed its PathCancellation
//! (members ACK after native close/reap), the host supplied actual death
//! evidence for a stage, or the seed showed the stage member's lease ran out
//! (`observe_member_leases`, a member on another Mac). Never synthesizes
//! tokens; route_ready stays false.
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
    iroh_host::{LanPolicy, LoopbackHost},
    iroh_receive::{DispatchRefusal, RouterReceiver},
    m16_coordinator::{SharedClock, SharedIdSource},
    m16_factory::{DeviceFacts, FactoryInputs, LiveM16Runtime, build_live_m16_runtime},
    m16_router_contracts::{DeviceState, IdMap, PyReal, RequestContext, RouterConfig},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    net::{IpAddr, SocketAddr},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    time::Instant,
};
use transport::{Channel, Clock, ConnectionFence, Exchange};
#[path = "m16_route_stop.rs"]
mod stop;
pub use stop::{CodecLimit, StopLimit, begin_close_until, close_until};

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
/// Product tokenizer seam. Legacy callbacks must be bounded/nonblocking;
/// Rust cannot preempt arbitrary extension code. Blocking implementations must
/// override the bounded methods and observe `limit` throughout their I/O.
/// OwnerWork retains its exact worker/codec on an unproven bounded teardown.
pub trait Codec: Send + Sync {
    fn encode(&self, text: &str) -> Result<Vec<u32>, PublicError>;
    fn decode(&self, token: u32) -> Result<String, PublicError>;
    fn encode_until(&self, text: &str, limit: &CodecLimit) -> Result<Vec<u32>, PublicError> {
        limit.check()?;
        let result = self.encode(text)?;
        limit.check()?;
        Ok(result)
    }
    fn decode_until(&self, token: u32, limit: &CodecLimit) -> Result<String, PublicError> {
        limit.check()?;
        let result = self.decode(token)?;
        limit.check()?;
        Ok(result)
    }
    /// Settle any interrupted helper under the SAME end; retain custody on Err.
    /// A healthy reusable tokenizer need not be closed when a route ends.
    fn settle_until(&self, _end: Instant) -> Result<(), PublicError> {
        Ok(())
    }
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
/// Carrier network of an executor. Loopback is the default; direct LAN is used
/// only when a caller explicitly configures it with an interface and allow-list.
#[derive(Clone, Debug)]
enum Network {
    Loopback,
    Lan {
        interface: IpAddr,
        policy: LanPolicy,
    },
}
/// Internal dial target: every address comes from a loopback StagePeer or a
/// member-signed, owner-verified carrier advertisement.
#[derive(Clone)]
struct Peer {
    node_id: String,
    endpoint: EndpointId,
    addrs: Vec<SocketAddr>,
    generation: u64,
}
/// Per-stage observable teardown outcome, for host status and qualification.
#[derive(Clone, Debug, Default)]
pub struct StageTeardown {
    pub acknowledged_ms: Option<f64>,
    pub failure: Option<String>,
    pub death_evidence: Option<Value>,
    /// v2: a data frame of the current request was (or may have been) sent to
    /// this stage. A stage the request never touched holds nothing of it.
    pub touched: bool,
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
    /// Teardown deliveries attempted (first teardown + bounded re-dials).
    pub teardown_attempts: u64,
    /// A teardown or re-dial attempt is in flight on the carrier thread.
    pub teardown_busy: bool,
    pub carrier_closed: bool,
    /// The route is `mycelium.joined_route.v2`: ONE epoch serves many requests,
    /// one at a time; the per-request fields above describe the current one.
    pub multi: bool,
    /// Requests started on this epoch.
    pub requests: u64,
    /// Requests whose teardown every stage acknowledged (v2).
    pub settled_requests: u64,
    /// Why a v2 epoch stopped serving when no request carried the code.
    pub epoch_failure: Option<&'static str>,
    /// The v2 carrier ended the epoch and closed its connections.
    pub closed: bool,
    /// Carrier connections the owner dialed (setup and re-dials).
    pub connections: u64,
    /// The current request's carrier worker has exited (v2 quiesce).
    pub worker_done: bool,
}
impl Observed {
    /// Every stage acknowledged or was settled by death evidence (exact host
    /// process exit, or the seed's expired member lease). v2: a stage the
    /// current request never sent a frame to holds nothing of it (its previous
    /// request was proven clean before this one started).
    pub fn settled(&self) -> bool {
        self.stages.iter().all(|s| {
            s.acknowledged_ms.is_some() || s.death_evidence.is_some() || (self.multi && !s.touched)
        })
    }
    /// The epoch can no longer serve. v1 (single-use): a carrier failure, a
    /// setup failure, or a teardown that did not follow a completed request.
    /// v2: a setup or carrier-level failure, death evidence, an idle
    /// retirement, a closed carrier, or a teardown not settled by every
    /// stage; a request stopped or failed by the request service with every
    /// stage's ACK leaves it serving.
    pub fn retired(&self) -> bool {
        if self.multi {
            return self.setup_failure.is_some()
                || self.failure.is_some()
                || self.epoch_failure.is_some()
                || self.closed
                || self.stages.iter().any(|s| s.death_evidence.is_some())
                || (self.teardown_finished && !self.settled());
        }
        self.failure.is_some()
            || self.setup_failure.is_some()
            || (self.teardown_requested_ms.is_some() && !self.completed)
            || (self.teardown_finished && !self.completed)
    }
    /// v2: a request is between its Start and its finished teardown.
    fn in_flight(&self) -> bool {
        self.requests > 0 && !self.teardown_finished
    }
    /// v2: the per-request state of the next request; epoch state is kept.
    fn begin_request(&mut self, request_id: &str) {
        self.request_id = Some(request_id.to_owned());
        self.started = None;
        self.token_times_ms.clear();
        self.tokens.clear();
        self.completed = false;
        self.failure = None;
        self.failed_at_ms = None;
        self.teardown_requested_ms = None;
        for s in &mut self.stages {
            s.acknowledged_ms = None;
            s.failure = None;
            s.touched = false;
        }
        self.teardown_finished = false;
        self.teardown_attempts = 0;
        self.teardown_busy = false;
        self.worker_done = false;
        self.requests += 1;
    }
}
/// Observable carrier state plus a change signal for an event-driven host pump.
#[derive(Default)]
pub struct RouteWatch {
    pub state: Mutex<Observed>,
    pub changed: std::sync::Condvar,
    /// v2: the carrier's command channel for [`close`]; taken by the executor's
    /// drop so the carrier thread always ends with its executor.
    closer: Mutex<Option<mpsc::Sender<Command>>>,
    shutdown: stop::Shutdown,
}
impl RouteWatch {
    /// Wait (bounded) for a carrier state change; returns immediately on change.
    pub fn wait(&self, seen: usize, timeout: std::time::Duration) -> usize {
        let Ok(guard) = self.state.lock() else {
            return seen;
        };
        let generation = |o: &Observed| {
            // v2: the request count keeps the value moving across requests.
            o.tokens.len()
                + usize::from(o.completed)
                + usize::from(o.failure.is_some())
                + usize::from(o.teardown_finished) * 2
                + usize::from(o.closed) * 4
                + usize::try_from(o.requests)
                    .unwrap_or(usize::MAX)
                    .wrapping_shl(32)
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
impl RouteWatch {
    /// Readiness of this epoch as the request host must report it. `spent` =
    /// a submission was already prepared for this epoch (v1 only: a v2 epoch
    /// is not spent by a request). An in-flight failure keeps the carrier's
    /// specific code until teardown finishes, so the request settles under
    /// that code; afterwards (and after Stop, a setup failure or a carrier
    /// exit) a v1 epoch is `route_dropped`. A retired v2 epoch answers its
    /// idle retirement's code (member stale, generation changed), else
    /// `route_dropped`.
    pub fn refusal(&self, spent: bool) -> Result<(), PublicError> {
        let o = self
            .state
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?;
        if let Some(code) = o.failure
            && !o.teardown_finished
        {
            return Err(error(code));
        }
        if o.multi {
            if let Some(code) = o.epoch_failure {
                return Err(error(code));
            }
            // The request whose own teardown retired the epoch (a stage could
            // not reset and was settled by its close) completed on a current
            // route: its qualification stays valid; no new request is prepared.
            let completed_then_retired = !spent
                && o.completed
                && o.failure.is_none()
                && o.settled()
                && o.stages.iter().all(|s| s.death_evidence.is_none());
            return if o.retired() && !completed_then_retired {
                Err(error("route_dropped"))
            } else {
                Ok(())
            };
        }
        if o.retired() || spent {
            return Err(error("route_dropped"));
        }
        Ok(())
    }
}
pub type RouteHandle = Arc<RouteWatch>;
enum Command {
    Start {
        path: Path,
        prompt: Vec<u32>,
        max_new_tokens: u64,
        /// [`Dispatch::product_stop_tokens`] of this request.
        product_stop_tokens: bool,
    },
    Teardown(Option<Path>),
    /// Re-dial each unsettled stage and re-send its PathCancellation.
    Retry,
    /// v2: end the epoch (an unsettled request is torn down as a retirement).
    Close,
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
    cancel: Arc<AtomicBool>,
    /// `mycelium.joined_route.v2`: many requests, one at a time.
    multi: bool,
}
impl RouteExecutor {
    /// Bind the owner carrier with its route-pinned key and dial every stage now
    /// over LocalOnly loopback (the default carrier).
    pub fn connect(
        message: Value,
        secret: iroh::SecretKey,
        peers: Vec<StagePeer>,
        currentness: Currentness,
        codec: Arc<dyn Codec>,
    ) -> Result<(Self, RouteHandle), PublicError> {
        Self::connect_until(message, secret, peers, currentness, codec, None)
    }
    /// The run's existing Stop token and first-Stop allowance, including setup.
    pub fn connect_until(
        message: Value,
        secret: iroh::SecretKey,
        peers: Vec<StagePeer>,
        currentness: Currentness,
        codec: Arc<dyn Codec>,
        stop: Option<StopLimit>,
    ) -> Result<(Self, RouteHandle), PublicError> {
        if peers.iter().any(|p| !p.socket.ip().is_loopback()) {
            return Err(error("joined_route_peer_mismatch"));
        }
        let peers = peers
            .into_iter()
            .map(|p| Peer {
                node_id: p.node_id,
                endpoint: p.endpoint,
                addrs: vec![p.socket],
                generation: p.generation,
            })
            .collect();
        Self::open(
            message,
            secret,
            peers,
            Network::Loopback,
            currentness,
            codec,
            stop,
        )
    }
    /// Explicit direct-LAN carrier: the owner endpoint binds exactly `interface`
    /// (no relay, no discovery) and dials each stage ONLY at the addresses of its
    /// member-signed, owner-verified advertisement, all within `policy`. Iroh
    /// pins each stage's endpoint key, so a different key at an advertised
    /// address fails the handshake. Same deadlines, cancellation and backpressure
    /// as the loopback carrier.
    pub fn connect_lan(
        message: Value,
        secret: iroh::SecretKey,
        carriers: Vec<crate::joined_shard::carrier::VerifiedCarrier>,
        interface: IpAddr,
        policy: LanPolicy,
        currentness: Currentness,
        codec: Arc<dyn Codec>,
    ) -> Result<(Self, RouteHandle), PublicError> {
        Self::connect_lan_until(
            message,
            secret,
            carriers,
            interface,
            policy,
            currentness,
            codec,
            None,
        )
    }
    /// Direct-LAN setup with the same first-Stop limit as the owner work.
    #[allow(clippy::too_many_arguments)]
    pub fn connect_lan_until(
        message: Value,
        secret: iroh::SecretKey,
        carriers: Vec<crate::joined_shard::carrier::VerifiedCarrier>,
        interface: IpAddr,
        policy: LanPolicy,
        currentness: Currentness,
        codec: Arc<dyn Codec>,
        stop: Option<StopLimit>,
    ) -> Result<(Self, RouteHandle), PublicError> {
        if !policy.allows(interface)
            || carriers
                .iter()
                .any(|c| c.addrs().iter().any(|a| !policy.allows(a.ip())))
        {
            return Err(error("joined_route_address_refused"));
        }
        // The owner accepts nothing, so its receive filter needs only the
        // signed carrier hosts: a QUIC Initial from any other host never
        // reaches iroh (REVIEW-laneB9 F1/F2).
        let mut hosts: Vec<IpAddr> = carriers
            .iter()
            .flat_map(|c| c.addrs().iter().map(|a| a.ip().to_canonical()))
            .collect();
        hosts.sort_unstable();
        hosts.dedup();
        let policy = if hosts.is_empty() {
            policy
        } else {
            policy
                .with_peer_hosts(hosts)
                .map_err(|_| error("joined_route_address_refused"))?
        };
        let peers = carriers
            .iter()
            .map(|c| Peer {
                node_id: c.node_id().to_owned(),
                endpoint: c.endpoint(),
                addrs: c.addrs().to_vec(),
                generation: c.generation(),
            })
            .collect();
        Self::open(
            message,
            secret,
            peers,
            Network::Lan { interface, policy },
            currentness,
            codec,
            stop,
        )
    }
    fn open(
        message: Value,
        secret: iroh::SecretKey,
        peers: Vec<Peer>,
        network: Network,
        currentness: Currentness,
        codec: Arc<dyn Codec>,
        stop: Option<StopLimit>,
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
                    || p.addrs.is_empty()
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
        let multi = route::multi_request(&message);
        let (tx, rx) = mpsc::channel();
        let observed: RouteHandle = Arc::new(RouteWatch {
            state: Mutex::new(Observed {
                stages: vec![StageTeardown::default(); peers.len()],
                multi,
                ..Observed::default()
            }),
            changed: std::sync::Condvar::new(),
            closer: Mutex::new(Some(tx.clone())),
            shutdown: stop::Shutdown {
                limit: stop,
                ..Default::default()
            },
        });
        // Request cancellation must not disable cleanup re-dials. Run shutdown
        // has its own flag; both paths wake owned I/O on final executor Drop.
        let cancel = Arc::new(AtomicBool::new(false));
        let (shared, flag, route) = (observed.clone(), cancel.clone(), message.clone());
        let thread = std::thread::spawn(move || {
            let _finished = stop::Finished(shared.clone());
            let served = if multi {
                carrier_epoch(
                    &route,
                    secret,
                    &peers,
                    &network,
                    &currentness,
                    &rx,
                    &shared,
                    &flag,
                )
            } else {
                carrier(
                    &route,
                    secret,
                    &peers,
                    &network,
                    &currentness,
                    &rx,
                    &shared,
                    &flag,
                )
            };
            if let Err(code) = served
                && let Ok(mut o) = shared.state.lock()
            {
                o.setup_failure.get_or_insert(code.to_owned());
            }
        });
        *observed
            .shutdown
            .thread
            .lock()
            .unwrap_or_else(|p| p.into_inner()) = Some(thread);
        let executor = Self {
            message,
            commands: Some(tx),
            observed: observed.clone(),
            pending: VecDeque::new(),
            taken: 0,
            codec,
            path: None,
            teardown_sent: false,
            cancel,
            multi,
        };
        let ready_by = Instant::now()
            + std::time::Duration::from_millis(
                B::WorkerStartup
                    .span(Entries::ZERO)
                    .map_err(|_| error("budget_unavailable"))?
                    .millis(),
            );
        let mut o = observed
            .state
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?;
        while !o.ready {
            let now = Instant::now();
            if o.setup_failure.is_some()
                || observed.shutdown.finished.load(Ordering::SeqCst)
                || observed.shutdown.setup_stopped()
                || now >= ready_by
            {
                return Err(error("joined_route_connect_failed"));
            }
            // Bounded by the owner poll so a finished carrier thread is noticed.
            let wait = (ready_by - now).min(std::time::Duration::from_millis(
                B::OwnerPoll
                    .span(Entries::ZERO)
                    .map_err(|_| error("budget_unavailable"))?
                    .millis(),
            ));
            o = observed
                .changed
                .wait_timeout(o, wait)
                .map_err(|_| error("joined_route_poisoned"))?
                .0;
        }
        drop(o);
        Ok((executor, observed))
    }
}
impl Drop for RouteExecutor {
    fn drop(&mut self) {
        // Emergency only: closes connections; never a cleanup receipt.
        self.cancel.store(true, Ordering::SeqCst);
        // The carrier ends only when every command sender is gone.
        if let Ok(mut closer) = self.observed.closer.lock() {
            closer.take();
        }
        self.commands.take();
        self.observed.shutdown.wake_io();
        self.observed.changed.notify_all();
        if let Err(e) = self.observed.shutdown.join() {
            eprintln!("{}", e.code);
        }
    }
}
impl Executor for RouteExecutor {
    fn start(&mut self, dispatch: &Dispatch) -> Result<(), PublicError> {
        if self.multi {
            // A new request on the same epoch: nothing of the previous one is
            // read again; `path` stays None unless a Start reaches the carrier.
            self.path = None;
            self.pending.clear();
            self.taken = 0;
            self.teardown_sent = false;
        }
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
        if self.multi {
            // Reset the per-request view BEFORE the carrier sees the Start, so
            // `poll` never hands this request the previous request's tokens.
            self.observed
                .state
                .lock()
                .map_err(|_| error("joined_route_poisoned"))?
                .begin_request(&path.request_id);
        }
        self.path = Some(path.clone());
        self.commands
            .as_ref()
            .ok_or(error("joined_route_closed"))?
            .send(Command::Start {
                path,
                prompt,
                max_new_tokens,
                product_stop_tokens: dispatch.product_stop_tokens,
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
            let limit = CodecLimit::for_route(Some(self.observed.clone()))?;
            let text = self.codec.decode_until(token, &limit)?;
            return Ok(Execution::Token {
                token_id: token,
                text,
            });
        }
        #[cfg(test)]
        tests::between_locks(&self.observed);
        o = self
            .observed
            .state
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?;
        // Tokens published after the first read are delivered before any
        // terminal decision: completion never truncates the stream.
        if o.tokens.len() > self.taken {
            return Ok(Execution::Pending);
        }
        if let Some(code) = o.failure {
            return Err(error(code));
        }
        // A carrier that exited after readiness without completing is a failure,
        // never an indefinitely pending request.
        if !o.completed
            && (o.setup_failure.is_some() || self.observed.shutdown.finished.load(Ordering::SeqCst))
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
        if self.multi && self.path.is_none() {
            // v2: the Start never reached the carrier; no member saw anything.
            return Ok(true);
        }
        if !self.teardown_sent {
            // v2: the carrier's request-scoped teardown stops only the request.
            if cancel && !self.multi {
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
        let mut o = self
            .observed
            .state
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?;
        if !o.teardown_finished || o.teardown_busy {
            return Ok(false);
        }
        if o.settled() {
            return Ok(true);
        }
        // A stage did not ACK (stalled past the close budget, or its ACK was
        // lost): re-dial and re-send, a bounded number of times.
        let allowed = B::PyMyceliumA13MacosManagedControlHttpRetryAttemptsL315
            .count()
            .map_err(|_| error("budget_unavailable"))?
            .get();
        let alive =
            self.commands.is_some() && !self.observed.shutdown.finished.load(Ordering::SeqCst);
        if alive && o.teardown_attempts < allowed {
            o.teardown_busy = true;
            drop(o);
            if let Some(tx) = self.commands.as_ref()
                && tx.send(Command::Retry).is_ok()
            {
                return Ok(false);
            }
            if let Ok(mut o) = self.observed.state.lock() {
                o.teardown_busy = false;
            }
        }
        // Retained debt: a stage neither acknowledged nor has death evidence.
        Err(PublicError::new(503, "joined_route_cleanup_unproven"))
    }
}
/// Host-supplied death evidence (exact owned process exit + absent worker PID).
/// Only an unacknowledged stage can be settled this way.
pub fn record_stage_exit(
    handle: &RouteHandle,
    stage: usize,
    evidence: Value,
) -> Result<(), PublicError> {
    let mut o = handle
        .state
        .lock()
        .map_err(|_| error("joined_route_poisoned"))?;
    let multi = o.multi;
    let s = o
        .stages
        .get_mut(stage)
        .ok_or(error("joined_route_invalid"))?;
    // v2: an ACK belongs to a request already settled; the dead stage still
    // ends the epoch.
    if (s.acknowledged_ms.is_some() && !multi)
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
    // v2: a dead stage never serves this epoch again.
    retire_epoch(handle, "joined_route_stage_died")
}
/// The seed's view of one route member for the lease death source: its current
/// member projection and the seed clock (seconds) it was read at. `None` = the
/// seed cannot answer for that node (unknown, or its durable row is unstable).
pub type SeedMembers = Arc<dyn Fn(&str) -> Option<(Value, f64)> + Send + Sync>;

/// Seed lease expiry as stage death evidence across hosts (gap row 16).
///
/// v0.1.4 treats a member whose seed lease ran out as gone: the owner refuses
/// every command and result wait to it (`control_session.py:315-326`,
/// `managed_control_member_stale`), the seed refuses its renewal
/// (`mycelium_seed/coordinator.py:1367-1370`) and the member refuses its own
/// work past its lease (`peer.py:625-640`; here `joined_shard::live`, the stage
/// server's `current()` per step). A stage whose CURRENT seed row is the
/// route's member (same node and generation) and whose lease ended at least the
/// members' clock tolerance ago by the seed clock is therefore settled: nothing
/// it could still hold can be serving this route. Evidence is recorded once;
/// an acknowledged stage is never re-settled; another generation (a resumed
/// member) is not evidence about the route's member. A request still running
/// fails `joined_route_member_stale` (v0.1.4's code for the same situation,
/// `managed_control_member_stale`); an idle epoch retires with it. An earlier
/// failure (normally the hop deadline) keeps its code.
///
/// Returns the stage indices newly settled by this call.
pub fn observe_member_leases(
    handle: &RouteHandle,
    message: &Value,
    seed: &SeedMembers,
) -> Result<Vec<usize>, PublicError> {
    let skew = B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31
        .span(Entries::ZERO)
        .map_err(|_| error("budget_unavailable"))?
        .millis() as f64
        / 1000.0;
    let stages = message["stages"]
        .as_array()
        .ok_or(error("joined_route_invalid"))?;
    // The seed is read first, never under the route lock.
    let mut expired = Vec::new();
    // v2: a stage member whose CURRENT row names another generation (a revoke
    // advances it; a re-join creates a new one) cannot serve this epoch.
    let mut moved = false;
    for (i, stage) in stages.iter().enumerate() {
        let node = stage["member"]["node_id"]
            .as_str()
            .ok_or(error("joined_route_invalid"))?;
        let generation = stage["member"]["generation"]
            .as_u64()
            .ok_or(error("joined_route_invalid"))?;
        let Some((row, now)) = seed(node) else {
            continue;
        };
        if row["node_id"] == node && row["generation"].as_u64().is_some_and(|g| g != generation) {
            moved = true;
        }
        let lease = row["lease_expires_at"].as_f64().filter(|v| v.is_finite());
        if row["node_id"] != node
            || row["generation"].as_u64() != Some(generation)
            || !now.is_finite()
            || lease.is_none_or(|lease| now < lease + skew)
        {
            continue;
        }
        expired.push((
            i,
            json!({"source":"seed_member_lease","node_id":node,"generation":generation,
                "lease_expires_at":row["lease_expires_at"],"observed_at":now,
                "clock_skew_seconds":skew}),
        ));
    }
    let mut o = handle
        .state
        .lock()
        .map_err(|_| error("joined_route_poisoned"))?;
    let mut settled = Vec::new();
    for (i, evidence) in expired {
        let multi = o.multi;
        let s = o.stages.get_mut(i).ok_or(error("joined_route_invalid"))?;
        // v2: an ACK settled an earlier request; the lapsed lease still ends
        // the epoch (no next request may touch this member).
        if (s.acknowledged_ms.is_some() && !multi) || s.death_evidence.is_some() {
            continue;
        }
        s.death_evidence = Some(evidence);
        settled.push(i);
    }
    let multi = o.multi;
    if settled.is_empty() && !(multi && moved) {
        return Ok(settled);
    }
    let code = if settled.is_empty() {
        "joined_route_generation_changed"
    } else {
        "joined_route_member_stale"
    };
    if !o.completed && !o.teardown_finished && o.failure.is_none() {
        o.failure = Some(code);
        o.failed_at_ms = o.started.map(elapsed_ms);
    }
    drop(o);
    handle.changed.notify_all();
    if multi {
        retire_epoch(handle, code)?;
    }
    Ok(settled)
}
/// End a v2 epoch from the owner (Stop of the run, rollover, ceiling, or an
/// idle retirement): an unsettled request is torn down as a retirement, then
/// every carrier connection is closed, so each member ends its serve with its
/// compute closed. A v1 epoch has nothing to close here (no-op).
pub fn close(handle: &RouteHandle) -> Result<(), PublicError> {
    if !handle
        .state
        .lock()
        .map_err(|_| error("joined_route_poisoned"))?
        .multi
        && handle
            .shutdown
            .end
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .is_none()
    {
        return Ok(());
    }
    let closer = handle
        .closer
        .lock()
        .map_err(|_| error("joined_route_poisoned"))?;
    if let Some(tx) = closer.as_ref()
        && tx.send(Command::Close).is_err()
    {
        // The carrier already ended, and closed every connection doing so.
        eprintln!("joined_route_close: carrier already ended");
    }
    Ok(())
}
/// v2: record why an epoch stops serving (first cause kept); when no request
/// is in flight, close it at once so every member releases its compute. A
/// request in flight carries the failure and is retired by its own teardown.
fn retire_epoch(handle: &RouteHandle, code: &'static str) -> Result<(), PublicError> {
    let mut o = handle
        .state
        .lock()
        .map_err(|_| error("joined_route_poisoned"))?;
    if !o.multi {
        return Ok(());
    }
    o.epoch_failure.get_or_insert(code);
    let idle = !o.in_flight();
    drop(o);
    handle.changed.notify_all();
    if idle { close(handle) } else { Ok(()) }
}
fn elapsed_ms(since: Instant) -> f64 {
    since.elapsed().as_secs_f64() * 1000.0
}
type Fence = Box<dyn Fn() -> ConnectionFence + Send + Sync>;
fn peer_addr(p: &Peer) -> EndpointAddr {
    p.addrs
        .iter()
        .fold(EndpointAddr::new(p.endpoint), |a, s| a.with_ip_addr(*s))
}
struct Link<'a> {
    shutdown: &'a stop::Shutdown,
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
            deadline: self.shutdown.deadline(clock, id)?,
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
            deadline: self.shutdown.deadline(clock, id)?,
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
            .receive(self.shutdown.deadline(clock, id)?)
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
    peers: &[Peer],
    network: &Network,
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
    let bound = observed.shutdown.deadline(&clock, B::WorkerStartup)?;
    let lan = match network {
        Network::Loopback => None,
        Network::Lan { interface, policy } => Some((*interface, policy.clone())),
    };
    let inner = LoopbackHost::bind_router_until(&rt, &clock, bound, secret, lan, &|| {
        observed.shutdown.setup_stopped()
    })
    .map_err(|_| "joined_route_bind_failed")?;
    let host = stop::OwnedHost {
        inner,
        observed: observed.clone(),
        clock: &clock,
    };
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
            let connection = host
                .connect_channel_until(
                    &peer_addr(p),
                    Channel::Router,
                    &clock,
                    observed.shutdown.deadline(&clock, B::WorkerStartup)?,
                    &|| observed.shutdown.setup_stopped(),
                )
                .map_err(|_| "joined_route_connect_failed")?;
            observed.shutdown.keep(&connection);
            set.push(connection);
        }
    }
    let fence = |p: &Peer| -> (ConnectionFence, Fence) {
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
    let link = |c: &Connection, p: &Peer| -> Result<Link<'_>, &'static str> {
        let (base, f1) = fence(p);
        let (_, f2) = fence(p);
        Ok(Link {
            shutdown: &observed.shutdown,
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
    let allowed = B::PyMyceliumA13MacosManagedControlHttpRetryAttemptsL315
        .count()
        .map_err(|_| "budget_unavailable")?
        .get();
    // Concurrent per-member control exchanges; a member ACKs only after its
    // native worker was closed or killed and reaped.
    let deliver = |frame: &[u8],
                   targets: &[(usize, &Connection)],
                   began: Instant|
     -> Vec<(usize, Result<f64, String>)> {
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
        std::thread::scope(|inner| {
            let handles: Vec<_> = targets
                .iter()
                .map(|&(i, c)| {
                    let (frame, clock, link) = (frame.to_vec(), &clock, &link);
                    let p = &peers[i];
                    let dead = dead.get(i).copied().unwrap_or(false);
                    (
                        i,
                        inner.spawn(move || -> Result<f64, String> {
                            if dead {
                                // Settled by host death evidence; nothing can ACK.
                                return Err("joined_route_stage_dead".into());
                            }
                            link(c, p)?.send_detailed(clock, frame, B::RouterClose)?;
                            Ok(elapsed_ms(began))
                        }),
                    )
                })
                .collect();
            handles
                .into_iter()
                .map(|(i, h)| {
                    (
                        i,
                        h.join()
                            .unwrap_or(Err("joined_route_teardown_panicked".into())),
                    )
                })
                .collect()
        })
    };
    // Record one delivery attempt; true when the carrier can retire (every stage
    // settled, or the bounded re-dial allowance is spent).
    let record = |outcomes: Vec<(usize, Result<f64, String>)>| -> Result<bool, &'static str> {
        let mut o = observed.state.lock().map_err(|_| "joined_route_poisoned")?;
        for (i, outcome) in outcomes {
            if let Some(s) = o.stages.get_mut(i) {
                match outcome {
                    Ok(ms) => s.acknowledged_ms = Some(ms),
                    Err(code) => s.failure = Some(code),
                }
            }
        }
        o.teardown_attempts += 1;
        o.teardown_busy = false;
        o.teardown_finished = true;
        let done = o.settled() || o.teardown_attempts >= allowed;
        drop(o);
        observed.changed.notify_all();
        Ok(done)
    };
    let mut teardown: Option<(Vec<u8>, Instant)> = None;
    let result = std::thread::scope(|scope| -> Result<(), &'static str> {
        let mut worker = None;
        for command in commands.iter() {
            match command {
                Command::Start {
                    path,
                    prompt,
                    max_new_tokens,
                    product_stop_tokens,
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
                            stop_tokens(product_stop_tokens),
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
                    if teardown.is_some() {
                        continue;
                    }
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
                    let targets: Vec<(usize, &Connection)> = control.iter().enumerate().collect();
                    let outcomes = deliver(&frame, &targets, began);
                    for c in data.iter().chain(&control) {
                        c.close(0u32.into(), b"route_retired");
                    }
                    if let Some(w) = worker.take()
                        && w.join().is_err()
                    {
                        return Err("joined_route_worker_panicked");
                    }
                    teardown = Some((frame, began));
                    if record(outcomes)? {
                        break;
                    }
                }
                // Only a v2 epoch's handle holds a closer (`close`): unreachable here.
                Command::Close => break,
                Command::Retry => {
                    let Some((frame, began)) = teardown.as_ref() else {
                        continue;
                    };
                    // Fresh control connection per unsettled stage: the member keeps
                    // accepting a bounded re-dial after its compute was released.
                    let unsettled: Vec<usize> = observed
                        .state
                        .lock()
                        .map_err(|_| "joined_route_poisoned")?
                        .stages
                        .iter()
                        .enumerate()
                        .filter(|(_, s)| s.acknowledged_ms.is_none() && s.death_evidence.is_none())
                        .map(|(i, _)| i)
                        .collect();
                    let mut dialed = Vec::new();
                    let mut failed = Vec::new();
                    for i in unsettled {
                        match observed
                            .shutdown
                            .deadline(&clock, B::RouterClose)
                            .and_then(|d| {
                                host.connect_channel_until(
                                    &peer_addr(&peers[i]),
                                    Channel::Router,
                                    &clock,
                                    d,
                                    &|| observed.shutdown.setup_stopped(),
                                )
                                .map_err(|_| "joined_route_redial_failed")
                            }) {
                            Ok(c) => {
                                observed.shutdown.keep(&c);
                                dialed.push((i, c));
                            }
                            Err(code) => failed.push((i, Err(code.to_owned()))),
                        }
                    }
                    let targets: Vec<(usize, &Connection)> =
                        dialed.iter().map(|(i, c)| (*i, c)).collect();
                    let mut outcomes = deliver(frame, &targets, *began);
                    for (_, c) in &dialed {
                        c.close(0u32.into(), b"route_retired");
                    }
                    drop(targets);
                    drop(dialed);
                    outcomes.extend(failed);
                    if record(outcomes)? {
                        break;
                    }
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
    drop(host);
    drop(rt);
    result
}
/// v2 (`mycelium.joined_route.v2`, lane EP1) carrier: ONE epoch serves many
/// requests, one at a time, over the same data and control connection per
/// stage. Their Links (message counters) live for the epoch, so each
/// connection's message ids stay monotonic across requests. A request's
/// teardown first lets its in-flight step finish (never interrupted: the
/// worker stops at its next cancellation check), then sends the
/// request-scoped PathCancellation on the epoch's control Links; a member
/// ACKs only after its session was reset or its compute closed. Every stage
/// ACKed: the epoch keeps serving. A carrier-level failure, death evidence,
/// a stage that did not ACK, a refused Start or `Close`: the v1 retirement
/// (PathCancellation, every connection closed, bounded re-dial).
#[allow(clippy::too_many_arguments)]
fn carrier_epoch(
    message: &Value,
    secret: iroh::SecretKey,
    peers: &[Peer],
    network: &Network,
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
    let bound = observed.shutdown.deadline(&clock, B::WorkerStartup)?;
    let lan = match network {
        Network::Loopback => None,
        Network::Lan { interface, policy } => Some((*interface, policy.clone())),
    };
    let inner = LoopbackHost::bind_router_until(&rt, &clock, bound, secret, lan, &|| {
        observed.shutdown.setup_stopped()
    })
    .map_err(|_| "joined_route_bind_failed")?;
    let host = stop::OwnedHost {
        inner,
        observed: observed.clone(),
        clock: &clock,
    };
    let epoch = message["topology_version"]
        .as_u64()
        .ok_or("joined_route_invalid")?;
    let expires_at = message["expires_at"]
        .as_f64()
        .ok_or("joined_route_invalid")?;
    let poll = std::time::Duration::from_millis(
        B::OwnerPoll
            .span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis(),
    );
    let mut data: Vec<Connection> = Vec::new();
    let mut control: Vec<Connection> = Vec::new();
    for p in peers {
        for set in [&mut data, &mut control] {
            let connection = host
                .connect_channel_until(
                    &peer_addr(p),
                    Channel::Router,
                    &clock,
                    observed.shutdown.deadline(&clock, B::WorkerStartup)?,
                    &|| observed.shutdown.setup_stopped(),
                )
                .map_err(|_| "joined_route_connect_failed")?;
            observed.shutdown.keep(&connection);
            set.push(connection);
        }
    }
    let fence = |p: &Peer| -> (ConnectionFence, Fence) {
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
    let link = |c: &Connection, p: &Peer| -> Result<Link<'_>, &'static str> {
        let (base, f1) = fence(p);
        let (_, f2) = fence(p);
        Ok(Link {
            shutdown: &observed.shutdown,
            send: IrohTransport::new(c.clone(), &rt, &clock, base, f1)
                .map_err(|_| "joined_route_generation_changed")?,
            receive: RouterReceiver::new(c.clone(), &rt, &clock, base, f2)
                .map_err(|_| "joined_route_generation_changed")?,
            fence: base,
            sent: 0,
            received: 0,
        })
    };
    // The epoch's Links: built once, message counters kept across requests.
    let mut data_links: Option<Vec<Link<'_>>> = Some(
        data.iter()
            .zip(peers)
            .map(|(c, p)| link(c, p))
            .collect::<Result<_, _>>()?,
    );
    let mut control_links: Vec<Link<'_>> = control
        .iter()
        .zip(peers)
        .map(|(c, p)| link(c, p))
        .collect::<Result<_, _>>()?;
    {
        let mut o = observed.state.lock().map_err(|_| "joined_route_poisoned")?;
        o.connections = (data.len() + control.len()) as u64;
        o.ready = true;
    }
    let dead = || -> Vec<bool> {
        observed
            .state
            .lock()
            .map(|o| {
                o.stages
                    .iter()
                    .map(|s| s.death_evidence.is_some())
                    .collect()
            })
            .unwrap_or_default()
    };
    // Concurrent PathCancellation on the epoch's own control Links.
    let deliver_on = |frame: &[u8],
                      links: &mut [Link<'_>],
                      began: Instant|
     -> Vec<(usize, Result<f64, String>)> {
        let dead = dead();
        std::thread::scope(|inner| {
            let handles: Vec<_> = links
                .iter_mut()
                .enumerate()
                .map(|(i, l)| {
                    let (frame, clock) = (frame.to_vec(), &clock);
                    let dead = dead.get(i).copied().unwrap_or(false);
                    (
                        i,
                        inner.spawn(move || -> Result<f64, String> {
                            if dead {
                                // Settled by death evidence; nothing can ACK.
                                return Err("joined_route_stage_dead".into());
                            }
                            l.send_detailed(clock, frame, B::RouterClose)?;
                            Ok(elapsed_ms(began))
                        }),
                    )
                })
                .collect();
            handles
                .into_iter()
                .map(|(i, h)| {
                    (
                        i,
                        h.join()
                            .unwrap_or(Err("joined_route_teardown_panicked".into())),
                    )
                })
                .collect()
        })
    };
    // A bounded re-dial after a retirement: a fresh connection per stage.
    let deliver_fresh = |frame: &[u8],
                         targets: &[(usize, &Connection)],
                         began: Instant|
     -> Vec<(usize, Result<f64, String>)> {
        let dead = dead();
        std::thread::scope(|inner| {
            let handles: Vec<_> = targets
                .iter()
                .map(|&(i, c)| {
                    let (frame, clock, link) = (frame.to_vec(), &clock, &link);
                    let p = &peers[i];
                    let dead = dead.get(i).copied().unwrap_or(false);
                    (
                        i,
                        inner.spawn(move || -> Result<f64, String> {
                            if dead {
                                return Err("joined_route_stage_dead".into());
                            }
                            link(c, p)?.send_detailed(clock, frame, B::RouterClose)?;
                            Ok(elapsed_ms(began))
                        }),
                    )
                })
                .collect();
            handles
                .into_iter()
                .map(|(i, h)| {
                    (
                        i,
                        h.join()
                            .unwrap_or(Err("joined_route_teardown_panicked".into())),
                    )
                })
                .collect()
        })
    };
    let record = |outcomes: Vec<(usize, Result<f64, String>)>| -> Result<(), &'static str> {
        let mut o = observed.state.lock().map_err(|_| "joined_route_poisoned")?;
        for (i, outcome) in outcomes {
            if let Some(s) = o.stages.get_mut(i) {
                match outcome {
                    Ok(ms) => s.acknowledged_ms = Some(ms),
                    Err(code) => s.failure = Some(code),
                }
            }
        }
        o.teardown_attempts += 1;
        o.teardown_busy = false;
        o.teardown_finished = true;
        drop(o);
        observed.changed.notify_all();
        Ok(())
    };
    // Ends the epoch: every member sees its connections close.
    let close_all = || {
        for c in data.iter().chain(&control) {
            c.close(0u32.into(), b"route_retired");
        }
        if let Ok(mut o) = observed.state.lock() {
            o.closed = true;
        }
        observed.changed.notify_all();
    };
    let cancellation = |path: &Path| {
        router_wire::encode_frame("PathCancellation", &route::cancellation(message, path), &[])
            .map_err(|_| "joined_route_frame_invalid")
    };
    let result = std::thread::scope(|scope| -> Result<(), &'static str> {
        let mut worker: Option<std::thread::ScopedJoinHandle<'_, Vec<Link<'_>>>> = None;
        // The current request's path and its own stop flag (never the epoch's).
        let mut current: Option<(Path, Arc<AtomicBool>)> = None;
        // The current request is torn down (or none was started).
        let mut torn = true;
        let mut open = true;
        let mut retired: Option<(Vec<u8>, Instant)> = None;
        for command in commands.iter() {
            let shutdown = matches!(command, Command::Close)
                && observed
                    .shutdown
                    .end
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .is_some();
            let retire = match command {
                Command::Start {
                    path,
                    prompt,
                    max_new_tokens,
                    product_stop_tokens,
                } => {
                    let refusal = {
                        let o = observed.state.lock().map_err(|_| "joined_route_poisoned")?;
                        if !open || o.retired() {
                            Some(o.epoch_failure.unwrap_or("route_dropped"))
                        } else if !torn || worker.is_some() || data_links.is_none() {
                            // Invariant: the previous request is proven clean first.
                            Some("joined_route_request_unsettled")
                        } else {
                            None
                        }
                    }
                    .or_else(|| {
                        peers
                            .iter()
                            .any(|p| currentness(&p.node_id) != Some(p.generation))
                            .then_some("joined_route_generation_changed")
                    })
                    .or_else(|| (app_host::now() >= expires_at).then_some("joined_route_expired"))
                    .or_else(|| {
                        data.iter()
                            .chain(&control)
                            .any(|c| c.close_reason().is_some())
                            .then_some("joined_route_carrier_failed")
                    });
                    let stop = Arc::new(AtomicBool::new(false));
                    current = Some((path.clone(), stop.clone()));
                    torn = false;
                    if let Some(code) = refusal {
                        // Nothing of this request leaves the owner; the epoch ends.
                        if let Ok(mut o) = observed.state.lock() {
                            o.failure.get_or_insert(code);
                            o.failed_at_ms = Some(0.0);
                            o.epoch_failure.get_or_insert(code);
                        }
                        if open {
                            close_all();
                            open = false;
                        }
                        observed.changed.notify_all();
                    } else {
                        let mut links = data_links.take().ok_or("joined_route_links_missing")?;
                        let clock = &clock;
                        worker = Some(scope.spawn(move || {
                            let began = Instant::now();
                            if let Ok(mut o) = observed.state.lock() {
                                o.started = Some(began);
                            }
                            let outcome = generate(
                                message,
                                &path,
                                prompt,
                                max_new_tokens,
                                stop_tokens(product_stop_tokens),
                                &mut links,
                                clock,
                                observed,
                                &stop,
                                began,
                            );
                            if let Ok(mut o) = observed.state.lock() {
                                match outcome {
                                    Ok(()) if !stop.load(Ordering::SeqCst) => o.completed = true,
                                    Ok(()) => {}
                                    Err(code) => {
                                        // Even after a stop: the in-flight step failed.
                                        if o.failure.is_none() {
                                            o.failure = Some(code);
                                            o.failed_at_ms = Some(elapsed_ms(began));
                                        }
                                    }
                                }
                                o.worker_done = true;
                            }
                            observed.changed.notify_all();
                            links
                        }));
                    }
                    None
                }
                Command::Teardown(_) => match current.clone() {
                    Some((path, stop)) if !torn => {
                        let began = Instant::now();
                        stop.store(true, Ordering::SeqCst);
                        if let Ok(mut o) = observed.state.lock() {
                            o.teardown_requested_ms = o.started.map(elapsed_ms);
                        }
                        if worker.is_none() {
                            // The Start was refused: no frame left the owner.
                            record(Vec::new())?;
                            torn = true;
                            None
                        } else {
                            // Quiesce: the in-flight step finishes; a carrier
                            // failure or death evidence switches to retirement.
                            let quiet = loop {
                                let o =
                                    observed.state.lock().map_err(|_| "joined_route_poisoned")?;
                                if !open
                                    || cancel.load(Ordering::SeqCst)
                                    || o.failure.is_some()
                                    || o.stages.iter().any(|s| s.death_evidence.is_some())
                                {
                                    break false;
                                }
                                if o.worker_done {
                                    break true;
                                }
                                drop(
                                    observed
                                        .changed
                                        .wait_timeout(o, poll)
                                        .map_err(|_| "joined_route_poisoned")?,
                                );
                            };
                            if !quiet {
                                Some(path)
                            } else {
                                data_links = worker
                                    .take()
                                    .map(|w| w.join())
                                    .transpose()
                                    .map_err(|_| "joined_route_worker_panicked")?;
                                if observed
                                    .state
                                    .lock()
                                    .map_err(|_| "joined_route_poisoned")?
                                    .failure
                                    .is_some()
                                {
                                    Some(path)
                                } else {
                                    let frame = cancellation(&path)?;
                                    let outcomes = deliver_on(&frame, &mut control_links, began);
                                    let acked = outcomes.iter().all(|(_, r)| r.is_ok());
                                    record(outcomes)?;
                                    torn = true;
                                    if acked {
                                        if let Ok(mut o) = observed.state.lock() {
                                            o.settled_requests += 1;
                                        }
                                        observed.changed.notify_all();
                                    } else {
                                        // A stage that did not ACK cannot be proven
                                        // clean for a next request: retire the epoch.
                                        close_all();
                                        open = false;
                                        data_links = None;
                                        retired = Some((frame, began));
                                    }
                                    None
                                }
                            }
                        }
                    }
                    _ => None,
                },
                Command::Retry => {
                    match retired.as_ref() {
                        Some((frame, began)) => {
                            let unsettled: Vec<usize> = observed
                                .state
                                .lock()
                                .map_err(|_| "joined_route_poisoned")?
                                .stages
                                .iter()
                                .enumerate()
                                .filter(|(_, s)| {
                                    s.acknowledged_ms.is_none()
                                        && s.death_evidence.is_none()
                                        && s.touched
                                })
                                .map(|(i, _)| i)
                                .collect();
                            let mut dialed = Vec::new();
                            let mut failed = Vec::new();
                            for i in unsettled {
                                match observed.shutdown.deadline(&clock, B::RouterClose).and_then(
                                    |d| {
                                        host.connect_channel_until(
                                            &peer_addr(&peers[i]),
                                            Channel::Router,
                                            &clock,
                                            d,
                                            &|| observed.shutdown.setup_stopped(),
                                        )
                                        .map_err(|_| "joined_route_redial_failed")
                                    },
                                ) {
                                    Ok(c) => {
                                        observed.shutdown.keep(&c);
                                        dialed.push((i, c));
                                    }
                                    Err(code) => failed.push((i, Err(code.to_owned()))),
                                }
                            }
                            if let Ok(mut o) = observed.state.lock() {
                                o.connections += dialed.len() as u64;
                            }
                            let targets: Vec<(usize, &Connection)> =
                                dialed.iter().map(|(i, c)| (*i, c)).collect();
                            let mut outcomes = deliver_fresh(frame, &targets, *began);
                            for (_, c) in &dialed {
                                c.close(0u32.into(), b"route_retired");
                            }
                            drop(targets);
                            drop(dialed);
                            outcomes.extend(failed);
                            record(outcomes)?;
                        }
                        None => {
                            if let Ok(mut o) = observed.state.lock() {
                                o.teardown_busy = false;
                            }
                        }
                    }
                    None
                }
                Command::Close => match current.clone() {
                    Some((path, stop)) if !torn => {
                        // An unsettled request ends with the epoch.
                        stop.store(true, Ordering::SeqCst);
                        if let Ok(mut o) = observed.state.lock()
                            && !o.completed
                        {
                            o.failure.get_or_insert("joined_route_closed");
                        }
                        Some(path)
                    }
                    _ => {
                        if open {
                            close_all();
                            open = false;
                        }
                        None
                    }
                },
            };
            if let Some(path) = retire {
                // Retirement: v1 teardown on the epoch's own control Links.
                let began = Instant::now();
                let frame = cancellation(&path)?;
                let outcomes = if open {
                    deliver_on(&frame, &mut control_links, began)
                } else {
                    (0..peers.len())
                        .map(|i| (i, Err("joined_route_carrier_closed".to_owned())))
                        .collect()
                };
                close_all();
                open = false;
                if let Some(w) = worker.take()
                    && w.join().is_err()
                {
                    return Err("joined_route_worker_panicked");
                }
                data_links = None;
                record(outcomes)?;
                retired = Some((frame, began));
                torn = true;
            }
            if shutdown {
                break;
            }
        }
        // The executor is gone: stop, close, join.
        if let Some((_, stop)) = current.as_ref() {
            stop.store(true, Ordering::SeqCst);
        }
        cancel.store(true, Ordering::SeqCst);
        close_all();
        if let Some(w) = worker.take()
            && w.join().is_err()
        {
            return Err("joined_route_worker_panicked");
        }
        Ok(())
    });
    drop(control_links);
    drop(data_links);
    drop(data);
    drop(control);
    drop(host);
    drop(rt);
    result
}
/// The route's product stop tokens (the browser codec's terminal ids).
const PRODUCT_STOP_TOKENS: [u32; 2] = [151_645, 151_643];
/// The stop tokens of one request: the product's, or none for the route's startup challenge
/// (`_stop_token_ids_override=frozenset()`, route.py:4563-4590 @ f4e366e9).
fn stop_tokens(product: bool) -> &'static [u32] {
    if product { &PRODUCT_STOP_TOKENS } else { &[] }
}
#[allow(clippy::too_many_arguments)]
fn generate(
    message: &Value,
    path: &Path,
    prompt: Vec<u32>,
    max_new_tokens: u64,
    stops: &[u32],
    links: &mut [Link<'_>],
    clock: &app_host_clock::Clock,
    observed: &RouteHandle,
    cancel: &AtomicBool,
    began: Instant,
) -> Result<(), &'static str> {
    let last = links.len() - 1;
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
        touch(observed, 0)?;
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
            touch(observed, i + 1)?;
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
        if stops.contains(&token) {
            return Ok(());
        }
        input = vec![token];
    }
    Ok(())
}
/// From now on a data frame of the current request may reach stage `i`.
fn touch(observed: &RouteHandle, i: usize) -> Result<(), &'static str> {
    let mut o = observed.state.lock().map_err(|_| "joined_route_poisoned")?;
    if let Some(s) = o.stages.get_mut(i) {
        s.touched = true;
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
        // The live coordinator's profiles (`build_live_m16_runtime`, mycelium_m16_runtime.py:1247-1251
        // @ f4e366e9): the chat submits `interactive_chat_v1` (chat.py:124, `chat_service`).
        workload_profiles: BTreeMap::from([
            ("interactive_chat_v1".into(), "interactive".into()),
            ("sustained_batch_v1".into(), "batch".into()),
        ]),
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
    carrier: Mutex<Option<RouteHandle>>,
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
            carrier: Mutex::new(None),
        })
    }
    /// Bind this epoch's carrier: readiness then follows the carrier, so a
    /// stopped, stalled, failed or dead epoch is no longer offered, and the
    /// single-use epoch admits no second request.
    pub fn attach(&self, handle: RouteHandle) -> Result<(), PublicError> {
        *self
            .carrier
            .lock()
            .map_err(|_| error("joined_route_poisoned"))? = Some(handle);
        Ok(())
    }
    fn carrier_refusal(&self, admitting: bool) -> Result<(), PublicError> {
        let carrier = self
            .carrier
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?;
        match carrier.as_ref() {
            Some(handle) => handle.refusal(admitting && self.next.load(Ordering::SeqCst) > 0),
            None => Ok(()),
        }
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
        self.carrier_refusal(false)?;
        route::verify_owner(&self.coordinator, &self.route, app_host::now())
            .map_err(|_| error("stale_qualification"))?;
        Ok(self.qualification.clone())
    }
    fn prepare(&self, submission: &Value) -> Result<Prepared, PublicError> {
        self.carrier_refusal(true)?;
        let prompt = submission["prompt"]
            .as_str()
            .ok_or(error("invalid_prompt"))?;
        let carrier = self
            .carrier
            .lock()
            .map_err(|_| error("joined_route_poisoned"))?
            .clone();
        let limit = CodecLimit::for_route(carrier)?;
        let ids = self.codec.encode_until(prompt, &limit)?;
        let max_new = submission["max_new_tokens"]
            .as_u64()
            .ok_or(error("invalid_max_new_tokens"))?;
        let qos = submission["qos_class"]
            .as_str()
            .ok_or(error("invalid_workload_binding"))?;
        self.prepared(&ids, max_new, qos)
    }
    /// The startup challenge's exact prompt ids: the same carrier and KV-context checks and the
    /// same request context as a product prompt, without the tokenizer.
    fn prepare_tokens(
        &self,
        prompt: &[u32],
        max_new_tokens: u64,
        qos_class: &str,
    ) -> Result<Prepared, PublicError> {
        self.carrier_refusal(true)?;
        self.prepared(prompt, max_new_tokens, qos_class)
    }
}
impl RouteHost {
    /// One request's context over `ids` for `max_new` new tokens (every request on the route).
    fn prepared(&self, ids: &[u32], max_new: u64, qos: &str) -> Result<Prepared, PublicError> {
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
                qos_class: qos.to_owned(),
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
#[cfg(test)]
mod tests {
    //! Deterministic interleavings of the executor's observation of carrier
    //! state. The hook runs at the exact point between the token read and the
    //! terminal read inside `poll`, so no timing or sleep is involved.
    use super::*;
    use std::cell::RefCell;
    type Hook = Box<dyn FnMut(&RouteHandle)>;
    thread_local! {
        static HOOK: RefCell<Option<Hook>> = RefCell::new(None);
    }
    pub(super) fn between_locks(observed: &RouteHandle) {
        HOOK.with(|h| {
            if let Some(hook) = h.borrow_mut().as_mut() {
                hook(observed);
            }
        });
    }
    struct Text;
    impl Codec for Text {
        fn encode(&self, _: &str) -> Result<Vec<u32>, PublicError> {
            Ok(vec![])
        }
        fn decode(&self, token: u32) -> Result<String, PublicError> {
            Ok(format!("t{token}"))
        }
    }
    fn detached(observed: RouteHandle) -> RouteExecutor {
        RouteExecutor {
            message: json!({"stages":[]}),
            commands: None,
            observed,
            pending: VecDeque::new(),
            taken: 0,
            codec: Arc::new(Text),
            path: None,
            teardown_sent: false,
            cancel: Arc::new(AtomicBool::new(false)),
            multi: false,
        }
    }
    /// N1: the carrier publishes the final token and then completion between the
    /// executor's two observations. The stream must still deliver that token.
    #[test]
    fn final_token_published_between_observations_is_never_lost() {
        let observed: RouteHandle = Arc::new(RouteWatch::default());
        let mut executor = detached(observed.clone());
        let mut fired = false;
        HOOK.with(|h| {
            *h.borrow_mut() = Some(Box::new(move |o: &RouteHandle| {
                if !fired {
                    fired = true;
                    let mut s = o.state.lock().unwrap();
                    s.tokens.push(7);
                    s.token_times_ms.push(0.0);
                    drop(s);
                    o.state.lock().unwrap().completed = true;
                }
            }));
        });
        let mut delivered = Vec::new();
        for _ in 0..4 {
            match executor.poll("r").unwrap() {
                Execution::Token { token_id, .. } => delivered.push(token_id),
                Execution::Pending => {}
                Execution::Completed => break,
            }
        }
        HOOK.with(|h| h.borrow_mut().take());
        assert_eq!(delivered, vec![7], "completed before every token was taken");
    }
}
