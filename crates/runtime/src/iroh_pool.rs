// SPDX-License-Identifier: AGPL-3.0-only
//! QUIC pooled Router driver: applies the deterministic
//! [`sidecar_pool`](crate::sidecar_pool) decisions to real Iroh connections
//! exactly as the shipped v0.1.4 sidecar does (`native/iroh_transport/src/
//! sidecar.rs @ f4e366e9`, cited per item).
//!
//! What this driver owns and the state machine does not: the connect/open/write/
//! read caps, the code-7 stream reset/stop on cancellation (shared connection
//! stays open), the code-8 closes (`retry`, `duplicate`, `identity`,
//! `unconfigured`, `peer_rotated`), the wake-ups (peer change, cancellation,
//! backoff tick, item deadline) and the receive side's 60-second wait for the
//! local dispatch to complete. It adds no recovery the sidecar lacks: one
//! outbound worker, one attempt at a time, no retry-count limit (the
//! enqueue-time lifetime is the only bound), per-connection serial inbound
//! streams, no membership recovery on rotation and no persisted replay.
//!
//! Every bound is caller-supplied ([`DriverLimits`]); native L1 registry IDs for
//! the sidecar bounds are still an open request, so no equal-valued default is
//! invented here. Transport is the host's local-only endpoint: direct loopback
//! addresses, no relay, no address lookup.
//!
//! Fidelity note (resolved in round 8): a connection that completes after its
//! binding was rotated away is parked in the pool under the dead generation key and
//! closed only by the next rotation or shutdown, exactly as v0.1.4 does
//! (`sidecar.rs:2027-2048`). The difference was wire-observable (the peer would see
//! an immediate code-8 `peer_rotated` close instead of an open idle connection), so
//! round 7's early close was removed; see the V03 characterization test.
use crate::sidecar_telemetry::{
    ObservationState, TransportEvent, admission_counters_json, document_json, envelope_json,
    unix_time_ms,
};
use crate::{
    iroh_host::{LoopbackHost, validate_local_peer},
    iroh_wire::classify_response,
    sidecar_pool::{
        AdmissionOutcome, AttemptResult, CANCEL_CODE, ConfigureOutcome, ConfigureRefusal, Digest,
        EndpointKey, EnqueueOutcome, EnqueueRequest, InboundDelivery, InboundHandle,
        InboundTerminal, MessageId, OutboundHandle, OutboundItem, OutboundRun, OutboundTerminal,
        PeerBinding, PoolLimits, PoolRegistration, REASON_DUPLICATE, REASON_IDENTITY,
        REASON_PEER_ROTATED, REASON_RETRY, REJECT_CODE, SidecarState, Step,
    },
};
use budgets::{MonoInstant, Span};
use iroh::{
    Endpoint, EndpointAddr,
    endpoint::{Connection, Incoming, ReadToEndError, RecvStream, SendStream, VarInt},
};
use protocol::{
    remote::{self, RemoteKind},
    router_wire,
};
use std::{
    collections::{BTreeMap, VecDeque},
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex, MutexGuard, PoisonError,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    sync::{Notify, OwnedSemaphorePermit, Semaphore, watch},
    task::{JoinHandle, JoinSet},
    time::{sleep, timeout},
};
use transport::{Channel, Clock, Error};

/// `unconfigured`: a remote connected before any peer table was installed.
pub const REASON_UNCONFIGURED: &[u8] = b"unconfigured";
/// An ALPN this driver does not serve (or the artifact ALPN with no service configured).
pub const REASON_ALPN: &[u8] = b"alpn";
pub const REASON_SHUTDOWN: &[u8] = b"shutdown";

/// Caller-supplied bounds. The shipped values are `connect` 5 s
/// (`PEER_CONNECT_TIMEOUT`, sidecar.rs:54) and `stream_io` 60 s
/// (`STREAM_IO_TIMEOUT`, sidecar.rs:50); neither has a native L1 ID yet.
#[derive(Clone, Copy, Debug)]
pub struct DriverLimits {
    pub pool: PoolLimits,
    /// One outbound dial attempt.
    pub connect: Span,
    /// Each of open / write / read on an outbound stream, each inbound
    /// request read and response write, and the wait for the local dispatch
    /// to complete before an inbound stream is answered.
    pub stream_io: Span,
}

/// Observer for the sidecar's stderr transport events (`log_transport_event`,
/// sidecar.rs:2520): called with the event, the remote endpoint key and generation.
/// Render with [`TransportEvent::line`] for the exact v0.1.4 line.
pub type EventSink = Arc<dyn Fn(TransportEvent, EndpointKey, u64) + Send + Sync>;

/// A non-Router ALPN service the accept loop hands a whole connection to. The
/// artifact source is the only one v0.1.4 serves (sidecar.rs:2731-2744); the hook is
/// the same for any other ALPN the host registers.
pub trait ConnectionService: Send + Sync + 'static {
    /// Serve one accepted connection to completion (the service closes it).
    fn serve(self: Arc<Self>, connection: Connection) -> Pin<Box<dyn Future<Output = ()> + Send>>;
}
/// Optional services and observers; `Services::default()` is the Router-only driver.
#[derive(Clone, Default)]
pub struct Services {
    /// Serves `mycelium.artifact_chunk.v1`. `None` with that ALPN registered on the
    /// endpoint closes such connections with code 8 `alpn` (sidecar.rs:2741).
    pub artifact: Option<Arc<dyn ConnectionService>>,
    /// Serves `mycelium.node_control.v1` (the owner's/member's control receiver).
    pub control: Option<Arc<dyn ConnectionService>>,
    pub events: Option<EventSink>,
}

/// Caller-supplied `ControlCall` carrier bounds (v0.1.4: 4 calls at once, 4 MiB response;
/// no native L1 ids yet).
#[derive(Clone, Copy, Debug)]
pub struct ControlCarrierLimits {
    pub concurrency: usize,
    pub max_response_bytes: usize,
}

struct ControlCarrier {
    slots: Arc<Semaphore>,
    max_response: usize,
    /// Calls made through this carrier so far (a failure line names its call number).
    calls: std::sync::atomic::AtomicU64,
}
/// Closes the call's connection on every exit path (sidecar.rs:1428-1436).
struct ControlConnection(Connection);
impl Drop for ControlConnection {
    fn drop(&mut self) {
        self.0
            .close(code(CANCEL_CODE_VAR_VALUE), b"control call ended");
    }
}
const CANCEL_CODE_VAR_VALUE: u32 = crate::sidecar_pool::CANCEL_CODE;

/// Dial counters (the sidecar's `note_attempt` / failure observations, reduced to
/// what the tests and a host diagnostic need).
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct PoolStats {
    /// Outbound dials started (pool miss).
    pub dials: u64,
    /// Dials that errored or exceeded the connect bound.
    pub dial_failures: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PoolError {
    /// Not a direct IPv4 loopback address (no relay, no discovery).
    Address,
    Configure(ConfigureRefusal),
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}
fn tick(sender: &watch::Sender<u64>) {
    sender.send_modify(|value| *value = value.wrapping_add(1));
}
fn duration(span: Span) -> Duration {
    Duration::from_millis(span.millis())
}
/// sha256 of the Router frame (`frame_digest`, sidecar.rs:128).
pub fn frame_digest(frame: &[u8]) -> Digest {
    let text = protocol::canonical::sha256_prefixed(frame);
    let mut out = [0_u8; 32];
    let hex = text.strip_prefix("sha256:").unwrap_or_default().as_bytes();
    for (byte, pair) in out.iter_mut().zip(hex.chunks_exact(2)) {
        let nibble = |c: u8| (c as char).to_digit(16).unwrap_or(0) as u8;
        *byte = (nibble(pair[0]) << 4) | nibble(pair[1]);
    }
    out
}
fn code(value: u32) -> VarInt {
    VarInt::from_u32(value)
}
fn reject(connection: &Connection, reason: &[u8]) {
    connection.close(code(REJECT_CODE), reason);
}
/// sidecar.rs:2708. Resets send and stops receive; the connection stays open.
fn cancel_streams(send: &mut SendStream, receive: &mut RecvStream) {
    if send.reset(code(CANCEL_CODE)).is_err() {
        // Already finished or reset: nothing left to cancel on this half.
    }
    if receive.stop(code(CANCEL_CODE)).is_err() {
        // Already stopped or fully read.
    }
}

/// sidecar.rs:2420-2440. `(class, relay identity, rtt ms)` of the selected path.
fn selected_path_observation(connection: &Connection) -> (String, Option<String>, Option<f64>) {
    let paths = connection.paths();
    let Some(path) = paths.iter().find(|path| path.is_selected()) else {
        return ("unknown".to_owned(), None, None);
    };
    let class = if path.is_ip() {
        "direct"
    } else if path.is_relay() {
        "relay"
    } else {
        "unknown"
    };
    let relay = path.is_relay().then(|| path.remote_addr().to_string());
    (
        class.to_owned(),
        relay,
        Some(path.rtt().as_secs_f64() * 1_000.0),
    )
}

struct Shared<C> {
    clock: Arc<C>,
    services: Services,
    limits: DriverLimits,
    endpoint: Endpoint,
    /// The oracle's `local_only` peer rule (`validate_peer`, `sidecar.rs:1852`) applies.
    local_only: bool,
    state: Mutex<SidecarState>,
    addresses: Mutex<BTreeMap<EndpointKey, EndpointAddr>>,
    connections: Mutex<BTreeMap<(EndpointKey, u64), Connection>>,
    /// `ControlCall` carrier; unset until [`PooledRouter::enable_control_carrier`].
    control: std::sync::OnceLock<ControlCarrier>,
    /// Test seam: runs between the state's rotation and the pool-map close.
    #[cfg(test)]
    rotation_gap: Mutex<Option<Box<dyn FnMut() + Send>>>,
    cancels: Mutex<BTreeMap<OutboundHandle, Arc<Notify>>>,
    queue_ready: Notify,
    /// Published on every effective `configure_peers` (sidecar `peer_changed`
    /// and `peer_generation`).
    peer_changed: watch::Sender<u64>,
    /// Terminal progress: outbound finish/cancel, inbound ack/rotation.
    events: watch::Sender<u64>,
    /// New or redelivered inbound items for local receivers.
    inbound_events: watch::Sender<u64>,
    shutdown: watch::Sender<bool>,
    dials: AtomicU64,
    dial_failures: AtomicU64,
    /// Per `(peer, generation)` transport observations (sidecar.rs:1417).
    observations: Mutex<BTreeMap<(EndpointKey, u64), ObservationState>>,
    inbound_identity_rejections: AtomicU64,
    inbound_frames_admitted: AtomicU64,
    attempt_log: Mutex<VecDeque<AttemptDecision>>,
    inbound_identity_rejections_by_endpoint: Mutex<BTreeMap<EndpointKey, u64>>,
}

/// How many attempt decisions the driver keeps (oldest dropped).
const ATTEMPT_LOG_CAPACITY: usize = 256;
/// The driver's own record, on its injected clock, of one attempt decision: the reading the
/// state machine saw when it chose to attempt, the item's ENQUEUE-time deadline, and the
/// backoff the pool had planned just before it (`wake_at` minus the failure's reading).
/// The state machine only issues an attempt when `deadline.remaining(decided_at)` is `Ok`;
/// recording the decision lets a test check that guarantee exactly through the real driver
/// loop instead of through wall-clock sleeps (an attempt that is already decided still
/// begins, and is counted, when the scheduler delays it past the deadline: v0.1.4's
/// `timeout_at(deadline, send_once)` behaves the same).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AttemptDecision {
    pub decided_at: MonoInstant,
    pub deadline: MonoInstant,
    pub backoff_before: Option<Span>,
}

impl<C: Clock + Send + Sync + 'static> Shared<C> {
    fn now(&self) -> MonoInstant {
        self.clock.monotonic()
    }
    fn record_decision(&self, decision: AttemptDecision) {
        let mut log = locked(&self.attempt_log);
        if log.len() == ATTEMPT_LOG_CAPACITY {
            log.pop_front();
        }
        log.push_back(decision);
    }
    fn until(&self, at: MonoInstant) -> Duration {
        at.remaining(self.now()).map_or(Duration::ZERO, duration)
    }
    fn emit(&self, event: TransportEvent, peer: PeerBinding) {
        if let Some(sink) = &self.services.events {
            sink(event, peer.endpoint, peer.generation);
        }
    }
    fn observe(&self, peer: PeerBinding, update: impl FnOnce(&mut ObservationState)) {
        update(
            locked(&self.observations)
                .entry((peer.endpoint, peer.generation))
                .or_default(),
        );
    }
    fn cancel_signal(&self, handle: OutboundHandle) -> Arc<Notify> {
        locked(&self.cancels).entry(handle).or_default().clone()
    }
    /// Resolves only when the state machine says this item is cancelled
    /// (rotation also cancels), tolerating spurious wake-ups.
    async fn cancelled(&self, handle: OutboundHandle, signal: &Notify) {
        loop {
            if locked(&self.state).is_cancelled(handle) {
                return;
            }
            signal.notified().await;
        }
    }
    async fn shutdown_requested(&self) {
        let mut rx = self.shutdown.subscribe();
        if rx.wait_for(|stop| *stop).await.is_err() {
            // Sender dropped with the driver: treat as shutdown.
        }
    }
    /// sidecar.rs:1750-1818 plus the dispatcher's carrier / peer lookups (1233-1243).
    async fn control_call(
        &self,
        destination: EndpointKey,
        call_timeout: Duration,
        command: Vec<u8>,
    ) -> Result<Vec<u8>, &'static str> {
        let Some(carrier) = self.control.get() else {
            return Err("carrier_unavailable");
        };
        let mut changed = self.peer_changed.subscribe();
        let peer = locked(&self.state)
            .peer_binding(destination)
            .ok_or("unknown_peer")?;
        let address = locked(&self.addresses)
            .get(&destination)
            .cloned()
            .ok_or("unknown_peer")?;
        let deadline = tokio::time::Instant::now()
            .checked_add(call_timeout)
            .ok_or("control_timeout_invalid")?;
        let still_bound = || locked(&self.state).peer_matches(destination, peer.generation);
        let began = std::time::Instant::now();
        let call = carrier
            .calls
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            + 1;
        let exchange = async {
            let _permit = tokio::time::timeout_at(deadline, carrier.slots.clone().acquire_owned())
                .await
                .map_err(|_| "control_carrier_busy")?
                .map_err(|_| "carrier_unavailable")?;
            if !still_bound() {
                return Err("peer_rotated");
            }
            let connecting = std::time::Instant::now();
            let connection = match tokio::time::timeout_at(
                deadline,
                self.endpoint
                    .connect(address.clone(), Channel::NodeControl.alpn()),
            )
            .await
            {
                Ok(Ok(connection)) => {
                    crate::iroh_control::connect_succeeded(&address);
                    connection
                }
                failed => {
                    let code = if failed.is_err() {
                        "control_connect_timeout"
                    } else {
                        "control_connect_failed"
                    };
                    crate::iroh_control::connect_failure(
                        code,
                        &self.endpoint,
                        &address,
                        call,
                        began.elapsed(),
                        connecting.elapsed(),
                        call_timeout,
                        "",
                    );
                    return Err(code);
                }
            };
            let connection = ControlConnection(connection);
            if connection.0.remote_id().as_bytes() != &destination {
                return Err("control_peer_mismatch");
            }
            if !still_bound() {
                return Err("peer_rotated");
            }
            let (mut send, mut recv) = tokio::time::timeout_at(deadline, connection.0.open_bi())
                .await
                .map_err(|_| "control_open_timeout")?
                .map_err(|_| "control_open_failed")?;
            tokio::time::timeout_at(deadline, send.write_all(&command))
                .await
                .map_err(|_| "control_write_timeout")?
                .map_err(|_| "control_write_failed")?;
            send.finish().map_err(|_| "control_write_failed")?;
            let response =
                match tokio::time::timeout_at(deadline, recv.read_to_end(carrier.max_response))
                    .await
                {
                    Ok(Ok(response)) if response.is_empty() => Err("empty_control_call_result"),
                    Ok(Ok(response)) => Ok(response),
                    Ok(Err(ReadToEndError::TooLong)) => Err("control_response_too_large"),
                    Ok(Err(_)) => Err("control_read_failed"),
                    Err(_) => Err("control_read_timeout"),
                }?;
            // Never admit a result under replaced authority (sidecar.rs:1808-1812).
            if !still_bound() {
                return Err("peer_rotated");
            }
            Ok(response)
        };
        tokio::select! {
            biased;
            _ = changed.changed() => Err("peer_rotated"),
            result = exchange => {
                if changed.has_changed().unwrap_or(true) { Err("peer_rotated") } else { result }
            }
        }
    }
    /// sidecar.rs:1991-2054.
    async fn reusable_connection(&self, peer: PeerBinding) -> Result<Connection, ()> {
        let key = (peer.endpoint, peer.generation);
        if let Some(connection) = locked(&self.connections).get(&key) {
            return Ok(connection.clone());
        }
        let address = locked(&self.addresses)
            .get(&peer.endpoint)
            .cloned()
            .ok_or(())?;
        self.dials.fetch_add(1, Ordering::Relaxed);
        let connection = match timeout(
            duration(self.limits.connect),
            self.endpoint.connect(address, Channel::Router.alpn()),
        )
        .await
        {
            Ok(Ok(connection)) => connection,
            outcome => {
                self.dial_failures.fetch_add(1, Ordering::Relaxed);
                if outcome.is_err() {
                    // sidecar.rs:2008-2020: only the elapsed bound is named.
                    self.emit(TransportEvent::PeerConnectDeadlineExceeded, peer);
                }
                return Err(());
            }
        };
        if connection.remote_id().as_bytes() != &peer.endpoint {
            reject(&connection, REASON_IDENTITY);
            return Err(());
        }
        let mut connections = locked(&self.connections);
        if let Some(existing) = connections.get(&key) {
            reject(&connection, REASON_DUPLICATE);
            return Ok(existing.clone());
        }
        // sidecar.rs:2032: inserted under the dialed generation key even if that
        // binding was rotated away while the dial was pending (v0.1.4 does not
        // re-check); the next rotation or shutdown closes it.
        let mut state = locked(&self.state);
        if state.pool_register(key) == PoolRegistration::Duplicate {
            reject(&connection, REASON_DUPLICATE);
            return Err(());
        }
        connections.insert(key, connection.clone());
        drop(connections);
        drop(state);
        // sidecar.rs:2034-2047.
        let (class, relay, rtt) = selected_path_observation(&connection);
        self.observe(peer, |observation| {
            observation.note_connection_opened(unix_time_ms(), class, relay, rtt);
        });
        self.emit(TransportEvent::ConnectionOpened, peer);
        Ok(connection)
    }
    /// sidecar.rs:2052: close code 8, reason `retry`.
    fn evict_connection(&self, peer: PeerBinding) {
        let key = (peer.endpoint, peer.generation);
        // One critical section (connections, then state): registry and map stay equal.
        let mut connections = locked(&self.connections);
        let removed = connections.remove(&key);
        locked(&self.state).pool_evict(key);
        drop(connections);
        if let Some(connection) = removed {
            reject(&connection, REASON_RETRY);
        }
        // sidecar.rs:2056-2059: every eviction call counts one failure.
        self.observe(peer, |observation| observation.note_failure(unix_time_ms()));
    }
    /// sidecar.rs:2606-2706. One attempt on a fresh bidirectional stream.
    async fn send_once(
        &self,
        peer: PeerBinding,
        item: &OutboundItem,
        signal: &Notify,
    ) -> AttemptResult {
        let stream_io = duration(self.limits.stream_io);
        self.observe(peer, |observation| observation.note_attempt(unix_time_ms()));
        let connection = tokio::select! {
            () = self.cancelled(item.handle, signal) => return AttemptResult::Cancelled,
            result = self.reusable_connection(peer) => match result {
                Ok(connection) => connection,
                Err(()) => {
                    self.evict_connection(peer);
                    return AttemptResult::io_failure();
                }
            }
        };
        let started = tokio::time::Instant::now();
        let (mut send, mut receive) = tokio::select! {
            () = self.cancelled(item.handle, signal) => return AttemptResult::Cancelled,
            result = timeout(stream_io, connection.open_bi()) => match result {
                Ok(Ok(streams)) => streams,
                _ => {
                    self.evict_connection(peer);
                    return AttemptResult::io_failure();
                }
            }
        };
        let source_generation = item.source_generation.unwrap_or(peer.generation);
        let mut transfer = Vec::with_capacity(remote::REMOTE_GENERATION_BYTES + item.payload.len());
        transfer.extend_from_slice(&source_generation.to_be_bytes());
        transfer.extend_from_slice(&item.payload);
        let Ok(encoded) =
            remote::encode_remote_frame(RemoteKind::Transfer, item.message_id, &transfer)
        else {
            return AttemptResult::Cancelled;
        };
        let written = tokio::select! {
            () = self.cancelled(item.handle, signal) => {
                cancel_streams(&mut send, &mut receive);
                return AttemptResult::Cancelled;
            }
            result = timeout(stream_io, send.write_all(&encoded)) => result,
        };
        if !matches!(written, Ok(Ok(()))) || send.finish().is_err() {
            cancel_streams(&mut send, &mut receive);
            self.evict_connection(peer);
            return AttemptResult::io_failure();
        }
        let response = tokio::select! {
            () = self.cancelled(item.handle, signal) => {
                cancel_streams(&mut send, &mut receive);
                return AttemptResult::Cancelled;
            }
            result = timeout(stream_io, receive.read_to_end(remote::REMOTE_MAX_FRAME_BYTES)) => match result {
                Ok(Ok(response)) => response,
                _ => {
                    cancel_streams(&mut send, &mut receive);
                    self.evict_connection(peer);
                    return AttemptResult::io_failure();
                }
            }
        };
        let result = AttemptResult::from(classify_response(&response, item.message_id));
        if matches!(result, AttemptResult::Delivered) {
            // sidecar.rs:2690-2694 (`note_delivered`).
            let (class, relay, rtt) = selected_path_observation(&connection);
            let elapsed = started.elapsed().as_secs_f64() * 1_000.0;
            let bytes = item.payload.len() as u64;
            self.observe(peer, |observation| {
                observation.note_delivered(unix_time_ms(), bytes, elapsed, class, relay, rtt);
            });
        }
        if let AttemptResult::Retry {
            evict_connection: true,
        } = result
        {
            self.evict_connection(peer);
        }
        result
    }
    /// One worker item (sidecar.rs:2530-2594).
    async fn run_item(&self, item: OutboundItem) {
        let handle = item.handle;
        let signal = self.cancel_signal(handle);
        let mut changes = self.peer_changed.subscribe();
        let Some(mut run) = OutboundRun::start(&locked(&self.state), item) else {
            locked(&self.cancels).remove(&handle);
            return;
        };
        let mut decided = self.now();
        let mut failed_at = decided;
        let mut planned_wait: Option<Span> = None;
        let mut step = run.advance(&mut locked(&self.state), decided);
        loop {
            step = match step {
                Step::Finish(_) => break,
                Step::Attempt(peer) => {
                    self.record_decision(AttemptDecision {
                        decided_at: decided,
                        deadline: run.deadline(),
                        backoff_before: planned_wait.take(),
                    });
                    let outcome = timeout(
                        self.until(run.deadline()),
                        self.send_once(peer, run.item(), &signal),
                    )
                    .await;
                    let now = self.now();
                    failed_at = now;
                    let mut state = locked(&self.state);
                    match outcome {
                        Err(_elapsed) => run.attempt_deadline_elapsed(&mut state),
                        Ok(result) => run.attempted(&mut state, now, result),
                    }
                }
                Step::WaitForBinding { wake_at } | Step::Backoff { wake_at } => {
                    planned_wait = wake_at.elapsed_since(failed_at).ok();
                    tokio::select! {
                        () = self.cancelled(handle, &signal) => {}
                        _ = changes.changed() => {}
                        () = sleep(self.until(wake_at)) => {}
                    }
                    decided = self.now();
                    run.advance(&mut locked(&self.state), decided)
                }
            };
        }
        locked(&self.cancels).remove(&handle);
        tick(&self.events);
    }
    async fn outbound_worker(self: Arc<Self>) {
        loop {
            let next = locked(&self.state).next_outbound();
            let Some(item) = next else {
                tokio::select! {
                    () = self.queue_ready.notified() => {}
                    () = self.shutdown_requested() => return,
                }
                continue;
            };
            tokio::select! {
                () = self.run_item(item) => {}
                () = self.shutdown_requested() => return,
            }
        }
    }

    // ---- inbound (sidecar.rs:2710-2897) ---------------------------------
    async fn accept_loop(self: Arc<Self>) {
        let slots = Arc::new(Semaphore::new(self.limits.pool.capacity.max(1)));
        let mut connections = JoinSet::new();
        loop {
            let permit = tokio::select! {
                result = slots.clone().acquire_owned() => match result {
                    Ok(permit) => permit,
                    Err(_) => break,
                },
                () = self.shutdown_requested() => break,
            };
            let incoming = tokio::select! {
                incoming = self.endpoint.accept() => match incoming {
                    Some(incoming) => incoming,
                    None => break,
                },
                () = self.shutdown_requested() => break,
            };
            let shared = self.clone();
            connections.spawn(async move {
                shared.admit_connection(incoming, permit).await;
            });
        }
        connections.abort_all();
        while connections.join_next().await.is_some() {}
    }
    async fn admit_connection(self: Arc<Self>, incoming: Incoming, permit: OwnedSemaphorePermit) {
        let Ok(connection) = incoming.await else {
            return;
        };
        // sidecar.rs:2731-2744: artifact chunks are authorized per request by the
        // seed grant and the recipient endpoint, never by the Router peer table;
        // Router frames are never accepted on that ALPN. The connection gives its
        // Router slot back at once, so artifact load cannot hold the slots Router
        // peers need.
        if connection.alpn() == Channel::ArtifactChunk.alpn() {
            drop(permit);
            match self.services.artifact.clone() {
                Some(service) => service.serve(connection).await,
                None => reject(&connection, REASON_ALPN),
            }
            return;
        }
        // Not a v0.1.4 behaviour (its sidecar never served NodeControl inbound; the endpoint
        // did not even register the ALPN, so the handshake failed). v0.2 runs NodeControl
        // over Iroh, so the host may register it and hand the connection to its control
        // receiver whole: like the artifact ALPN the caller is authorized by the service
        // (signed payloads), not by the Router peer table, and no Router slot is held.
        if connection.alpn() == Channel::NodeControl.alpn() {
            drop(permit);
            match self.services.control.clone() {
                Some(service) => service.serve(connection).await,
                None => reject(&connection, REASON_ALPN),
            }
            return;
        }
        if connection.alpn() != Channel::Router.alpn() {
            reject(&connection, REASON_ALPN);
            return;
        }
        let _permit = permit;
        let expected = {
            let state = locked(&self.state);
            if !state.peers_configured() {
                drop(state);
                reject(&connection, REASON_UNCONFIGURED);
                return;
            }
            let remote: EndpointKey = *connection.remote_id().as_bytes();
            match state.peer_binding(remote) {
                Some(binding) => binding,
                None => {
                    drop(state);
                    // sidecar.rs:2756-2769: counted per remote, then closed.
                    self.inbound_identity_rejections
                        .fetch_add(1, Ordering::AcqRel);
                    let mut by_endpoint = locked(&self.inbound_identity_rejections_by_endpoint);
                    let count = by_endpoint.entry(remote).or_insert(0);
                    *count = count.saturating_add(1);
                    drop(by_endpoint);
                    reject(&connection, REASON_IDENTITY);
                    return;
                }
            }
        };
        self.serve_connection(connection, expected.generation).await;
    }
    /// Strictly serial streams per connection, exactly like the sidecar loop.
    async fn serve_connection(&self, connection: Connection, connection_generation: u64) {
        let stream_io = duration(self.limits.stream_io);
        let remote_id: EndpointKey = *connection.remote_id().as_bytes();
        let mut changes = self.peer_changed.subscribe();
        loop {
            if !locked(&self.state).peer_matches(remote_id, connection_generation) {
                reject(&connection, REASON_PEER_ROTATED);
                return;
            }
            let streams = tokio::select! {
                streams = connection.accept_bi() => streams,
                _ = changes.changed() => {
                    reject(&connection, REASON_PEER_ROTATED);
                    return;
                }
                () = self.shutdown_requested() => return,
            };
            let Ok((mut send, mut receive)) = streams else {
                return;
            };
            let Ok(Ok(request)) = timeout(
                stream_io,
                receive.read_to_end(remote::REMOTE_MAX_FRAME_BYTES),
            )
            .await
            else {
                cancel_streams(&mut send, &mut receive);
                continue;
            };
            let request = match remote::decode_remote_frame(&request) {
                Ok(frame) if frame.kind == RemoteKind::Transfer => frame,
                _ => {
                    cancel_streams(&mut send, &mut receive);
                    continue;
                }
            };
            let Some((prefix, router_frame)) = request
                .payload
                .split_first_chunk::<{ remote::REMOTE_GENERATION_BYTES }>()
            else {
                cancel_streams(&mut send, &mut receive);
                continue;
            };
            let generation = u64::from_be_bytes(*prefix);
            let router_frame = router_frame.to_vec();
            let reply: &[u8] = if generation != connection_generation {
                // sidecar.rs:2840: generation refusal precedes inner validation.
                b"peer_rotated"
            } else if router_frame.len() > remote::REMOTE_OPERATIONAL_FRAME_BYTES
                || router_wire::decode_frame(&router_frame).is_err()
            {
                b"invalid_frame"
            } else {
                let digest = frame_digest(&router_frame);
                let admission = locked(&self.state).admit_inbound(
                    Some((remote_id, connection_generation)),
                    request.message_id,
                    digest,
                    router_frame,
                );
                match admission {
                    AdmissionOutcome::Admitted(handle) => {
                        // sidecar.rs:2859: only a first admission counts.
                        self.inbound_frames_admitted.fetch_add(1, Ordering::AcqRel);
                        tick(&self.inbound_events);
                        match self.dispatch_outcome(handle, stream_io).await {
                            Some(reply) => reply,
                            None => {
                                cancel_streams(&mut send, &mut receive);
                                continue;
                            }
                        }
                    }
                    AdmissionOutcome::PendingDuplicate(handle) => {
                        match self.dispatch_outcome(handle, stream_io).await {
                            Some(reply) => reply,
                            None => {
                                cancel_streams(&mut send, &mut receive);
                                continue;
                            }
                        }
                    }
                    AdmissionOutcome::CompletedDuplicate => b"",
                    AdmissionOutcome::ReplayCollision => b"replay_collision",
                    AdmissionOutcome::PeerRotated => b"peer_rotated",
                    AdmissionOutcome::Full => b"queue_full",
                }
            };
            let kind = if reply.is_empty() {
                RemoteKind::Ack
            } else {
                RemoteKind::Error
            };
            let Ok(response) = remote::encode_remote_frame(kind, request.message_id, reply) else {
                cancel_streams(&mut send, &mut receive);
                continue;
            };
            let written = timeout(stream_io, send.write_all(&response)).await;
            if !matches!(written, Ok(Ok(()))) || send.finish().is_err() {
                cancel_streams(&mut send, &mut receive);
            }
        }
    }
    /// Waits (at most `limit`) for the local dispatch of an admitted item to
    /// finish. `None`: the wait expired; the stream is reset/stopped (code 7)
    /// and nothing is acknowledged (sidecar.rs:2866-2872).
    async fn dispatch_outcome(
        &self,
        handle: InboundHandle,
        limit: Duration,
    ) -> Option<&'static [u8]> {
        let mut changes = self.events.subscribe();
        let wait = async {
            loop {
                if let Some(terminal) = locked(&self.state).inbound_terminal(handle) {
                    return terminal;
                }
                if changes.changed().await.is_err() {
                    return InboundTerminal::PeerRotated;
                }
            }
        };
        match timeout(limit, wait).await {
            Ok(InboundTerminal::Acknowledged) => Some(b""),
            Ok(InboundTerminal::PeerRotated) => Some(b"peer_rotated"),
            Err(_elapsed) => None,
        }
    }
}

/// The pooled Router carrier over one local-only endpoint.
pub struct PooledRouter<C: Clock + Send + Sync + 'static> {
    shared: Arc<Shared<C>>,
    tasks: Mutex<Vec<JoinHandle<()>>>,
}
impl<C: Clock + Send + Sync + 'static> PooledRouter<C> {
    /// Starts the outbound worker and the inbound accept loop on the host's
    /// runtime. The host's endpoint must have the Router ALPN (`bind` default).
    /// Tasks progress only while that runtime is being driven.
    pub fn start(
        host: &LoopbackHost<'_>,
        clock: Arc<C>,
        limits: DriverLimits,
    ) -> Result<Self, Error> {
        Self::start_with(host, clock, limits, Services::default())
    }
    /// [`Self::start`] with the artifact-ALPN service and/or an event observer.
    /// Once started, this driver owns the endpoint's accept loop: do not also call
    /// `LoopbackHost::accept` for the same endpoint.
    pub fn start_with(
        host: &LoopbackHost<'_>,
        clock: Arc<C>,
        limits: DriverLimits,
        services: Services,
    ) -> Result<Self, Error> {
        let endpoint = host.endpoint()?.clone();
        let (peer_changed, _) = watch::channel(0);
        let (events, _) = watch::channel(0);
        let (inbound_events, _) = watch::channel(0);
        let (shutdown, _) = watch::channel(false);
        let shared = Arc::new(Shared {
            clock,
            services,
            limits,
            endpoint,
            local_only: host.mode().local_only(),
            state: Mutex::new(SidecarState::new(limits.pool)),
            addresses: Mutex::new(BTreeMap::new()),
            connections: Mutex::new(BTreeMap::new()),
            control: std::sync::OnceLock::new(),
            #[cfg(test)]
            rotation_gap: Mutex::new(None),
            cancels: Mutex::new(BTreeMap::new()),
            queue_ready: Notify::new(),
            peer_changed,
            events,
            inbound_events,
            shutdown,
            dials: AtomicU64::new(0),
            dial_failures: AtomicU64::new(0),
            observations: Mutex::new(BTreeMap::new()),
            inbound_identity_rejections: AtomicU64::new(0),
            inbound_frames_admitted: AtomicU64::new(0),
            attempt_log: Mutex::new(VecDeque::new()),
            inbound_identity_rejections_by_endpoint: Mutex::new(BTreeMap::new()),
        });
        let runtime = host.runtime();
        let tasks = vec![
            runtime.spawn(shared.clone().outbound_worker()),
            runtime.spawn(shared.clone().accept_loop()),
        ];
        Ok(Self {
            shared,
            tasks: Mutex::new(tasks),
        })
    }

    /// sidecar.rs:1897-1940 plus the rotation closes of the pooled connections.
    pub fn configure_peers(
        &self,
        ordered: Vec<(EndpointAddr, u64)>,
    ) -> Result<ConfigureOutcome, PoolError> {
        for (address, _) in &ordered {
            if self.shared.local_only {
                validate_local_peer(address).map_err(|_| PoolError::Address)?;
            }
        }
        let bindings = ordered
            .iter()
            .map(|(address, generation)| PeerBinding {
                endpoint: *address.id.as_bytes(),
                generation: *generation,
            })
            .collect();
        // Lock order is connections, then state (as in `reusable_connection`): the state's
        // rotation and the close of the whole pool map are one critical section, so a dial
        // that finishes meanwhile registers either before both or after both and the state's
        // pool registry always holds exactly the keys of the connection map (M23x).
        let mut connections = locked(&self.shared.connections);
        let outcome = locked(&self.shared.state)
            .configure_peers(bindings)
            .map_err(PoolError::Configure)?;
        #[cfg(test)]
        if let Some(gap) = locked(&self.shared.rotation_gap).as_mut() {
            gap();
        }
        *locked(&self.shared.addresses) = ordered
            .into_iter()
            .map(|(address, _)| (*address.id.as_bytes(), address))
            .collect();
        if outcome.changed {
            // sidecar.rs:2109-2114 takes the WHOLE pool map: that includes a
            // connection parked under a dead key by a late dial.
            let pooled = std::mem::take(&mut *connections);
            drop(connections);
            for connection in pooled.into_values() {
                reject(&connection, REASON_PEER_ROTATED);
            }
            for signal in locked(&self.shared.cancels).values() {
                signal.notify_one();
            }
            tick(&self.shared.peer_changed);
            tick(&self.shared.events);
            tick(&self.shared.inbound_events);
        }
        Ok(outcome)
    }

    // ---- outbound ---------------------------------------------------------
    pub fn enqueue(&self, request: EnqueueRequest) -> EnqueueOutcome {
        let outcome = locked(&self.shared.state).enqueue(request, self.shared.now());
        if let EnqueueOutcome::Queued(handle) = outcome {
            self.shared.cancel_signal(handle);
            self.shared.queue_ready.notify_one();
        }
        outcome
    }
    /// Removes the exact token/queued item, signals the worker attempt and
    /// releases the capacity permit immediately (sidecar.rs:2196-2218).
    pub fn cancel(&self, message_id: MessageId) -> bool {
        let handle = locked(&self.shared.state).cancel_handle(message_id);
        let Some(handle) = handle else {
            return false;
        };
        self.shared.cancel_signal(handle).notify_one();
        tick(&self.shared.events);
        true
    }
    pub fn outbound_terminal(&self, handle: OutboundHandle) -> Option<OutboundTerminal> {
        locked(&self.shared.state).terminal(handle)
    }
    /// The driver's recorded attempt decisions, oldest first (bounded to the last 256).
    pub fn attempt_decisions(&self) -> Vec<AttemptDecision> {
        locked(&self.shared.attempt_log).iter().copied().collect()
    }
    pub fn outbound_free(&self) -> usize {
        locked(&self.shared.state).outbound_free()
    }
    /// Waits for the first-wins terminal of an outbound item. Unbounded by
    /// itself: the item's enqueue-time lifetime always ends it.
    pub async fn wait_terminal(&self, handle: OutboundHandle) -> OutboundTerminal {
        let mut changes = self.shared.events.subscribe();
        loop {
            if let Some(terminal) = self.outbound_terminal(handle) {
                return terminal;
            }
            if changes.changed().await.is_err() {
                return OutboundTerminal::Cancelled;
            }
        }
    }
    pub fn stats(&self) -> PoolStats {
        PoolStats {
            dials: self.shared.dials.load(Ordering::Relaxed),
            dial_failures: self.shared.dial_failures.load(Ordering::Relaxed),
        }
    }
    /// `mycelium.iroh_sidecar.transport_observations.v1` for every configured peer,
    /// sorted by remote endpoint id (sidecar.rs:2090-2107). A configured peer that was
    /// never used reports zeros and `unknown`.
    pub fn transport_observations_json(&self) -> String {
        let bindings: Vec<(String, PeerBinding)> = locked(&self.shared.addresses)
            .iter()
            .filter_map(|(endpoint, address)| {
                locked(&self.shared.state)
                    .peer_binding(*endpoint)
                    .map(|binding| (address.id.to_string(), binding))
            })
            .collect();
        let observations = locked(&self.shared.observations);
        let mut documents: Vec<(String, String)> = bindings
            .into_iter()
            .map(|(id, binding)| {
                let state = observations.get(&(binding.endpoint, binding.generation));
                let document = document_json(&id, binding.generation, state);
                (id, document)
            })
            .collect();
        documents.sort_by(|left, right| left.0.cmp(&right.0));
        let documents: Vec<String> = documents
            .into_iter()
            .map(|(_, document)| document)
            .collect();
        envelope_json(&documents)
    }
    /// `mycelium.iroh_sidecar.inbound_admission.v1` scoped to one candidate remote
    /// (sidecar.rs:1820-1838).
    pub fn admission_counters_json(&self, candidate: EndpointKey) -> String {
        let candidate_rejections = locked(&self.shared.inbound_identity_rejections_by_endpoint)
            .get(&candidate)
            .copied()
            .unwrap_or(0);
        admission_counters_json(
            self.shared
                .inbound_identity_rejections
                .load(Ordering::Acquire),
            self.shared.inbound_frames_admitted.load(Ordering::Acquire),
            candidate_rejections,
            unix_time_ms(),
        )
    }
    /// Turns the NodeControl `ControlCall` carrier on, once (sidecar.rs:1701: the carrier
    /// exists when the runtime has a bound endpoint; before this, `control_call` answers
    /// `carrier_unavailable`). Returns whether this call enabled it.
    pub fn enable_control_carrier(&self, limits: ControlCarrierLimits) -> bool {
        // `false` when the carrier already exists: the sidecar creates it once, a second
        // call keeps the first.
        self.shared
            .control
            .set(ControlCarrier {
                slots: Arc::new(Semaphore::new(limits.concurrency)),
                max_response: limits.max_response_bytes,
                calls: std::sync::atomic::AtomicU64::new(0),
            })
            .is_ok()
    }
    /// The primary binding and all bindings of the installed peer table.
    pub fn peer_snapshot(&self) -> (Option<PeerBinding>, Vec<PeerBinding>) {
        locked(&self.shared.state).peer_snapshot()
    }
    /// One `ControlCall` (sidecar.rs:1750-1818): carry `command` to the configured peer's
    /// NodeControl ALPN and return its exact response bytes. Every wait shares one absolute
    /// deadline; a peer change at any point ends the call as `peer_rotated`.
    pub async fn control_call(
        &self,
        destination: EndpointKey,
        timeout: Duration,
        command: Vec<u8>,
    ) -> Result<Vec<u8>, &'static str> {
        self.shared
            .control_call(destination, timeout, command)
            .await
    }
    /// Test seam: admits an inbound frame as the driver's accept path would.
    #[cfg(test)]
    pub(crate) fn admit_for_test(
        &self,
        origin: Option<(EndpointKey, u64)>,
        message_id: MessageId,
        frame: Vec<u8>,
    ) {
        let digest = frame_digest(&frame);
        locked(&self.shared.state).admit_inbound(origin, message_id, digest, frame);
        tick(&self.shared.inbound_events);
    }
    /// Live pooled outbound connections (identity + generation keyed).
    pub fn pooled_connections(&self) -> usize {
        locked(&self.shared.connections).len()
    }

    // ---- local receive side ----------------------------------------------
    pub fn try_receive(&self, session: u64) -> Option<InboundDelivery> {
        locked(&self.shared.state).receive(session)
    }
    /// Waits up to `wait` for a pending inbound item (the local IPC poll).
    pub async fn receive(&self, session: u64, wait: Span) -> Option<InboundDelivery> {
        let mut changes = self.shared.inbound_events.subscribe();
        let limit = sleep(duration(wait));
        tokio::pin!(limit);
        loop {
            if let Some(delivery) = self.try_receive(session) {
                return Some(delivery);
            }
            tokio::select! {
                changed = changes.changed() => {
                    if changed.is_err() {
                        return None;
                    }
                }
                () = &mut limit => return None,
            }
        }
    }
    /// Only the session holding the id inflight may acknowledge; a completed
    /// dispatch lets the waiting remote stream receive its ACK.
    pub fn ack(&self, session: u64, message_id: MessageId) -> bool {
        let acknowledged = locked(&self.shared.state).ack(session, message_id);
        if acknowledged {
            tick(&self.shared.events);
        }
        acknowledged
    }
    /// Local hangup: inflight items return to the front in original order.
    pub fn redeliver_session(&self, session: u64) {
        locked(&self.shared.state).redeliver_session(session);
        tick(&self.shared.inbound_events);
    }
    pub fn inbound_free(&self) -> usize {
        locked(&self.shared.state).inbound_free()
    }

    /// Stops both tasks and closes every pooled connection. Must complete
    /// (and the router be dropped) before `LoopbackHost::close`.
    pub async fn shutdown(&self) {
        self.shared.shutdown.send_replace(true);
        let tasks = std::mem::take(&mut *locked(&self.tasks));
        for task in tasks {
            if task.await.is_err() {
                // A panicked driver task is surfaced by the test harness.
            }
        }
        let pooled = std::mem::take(&mut *locked(&self.shared.connections));
        for connection in pooled.into_values() {
            connection.close(code(0), REASON_SHUTDOWN);
        }
    }
}

#[cfg(test)]
#[path = "iroh_pool_tests.rs"]
mod tests;
