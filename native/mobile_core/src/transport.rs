// SPDX-License-Identifier: AGPL-3.0-only
//! Embedded iroh transport speaking the same ALPN and remote framing as
//! `native/iroh_transport` (`mycelium.iroh.sidecar.v1`), without the sidecar
//! process or Unix socket: the app owns the endpoint directly. Replaces Tailscale:
//! connectivity comes from iroh hole punching with relay fallback.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::Duration;

use iroh::endpoint::presets;
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMode, SecretKey};
use tokio::sync::{Mutex as AsyncMutex, RwLock, mpsc, oneshot};
use tokio::time::timeout;

use crate::node_control::{CONTROL_ALPN, MAX_COMMAND_BYTES};
use crate::wire::{self, Frame, RemoteKind};

pub const IROH_ALPN: &[u8] = b"mycelium.iroh.sidecar.v1";
const STREAM_IO_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_TRANSFER_BYTES: usize = wire::REMOTE_HEADER_BYTES + wire::REMOTE_MAX_PAYLOAD_BYTES;
const REJECT_CODE: u32 = 1;
/// How long a delivery may wait for its dispatch verdict before the sender
/// is told to retry (the stage run happens inside it).
pub const DISPATCH_TIMEOUT: Duration = Duration::from_secs(120);
/// Message ids remembered for duplicate detection (the sidecar's seen table).
const SEEN_LIMIT: usize = 4096;
/// How long one control command may take to answer (`health` must answer
/// while the stage computes: the handler never runs on the compute path).
pub const CONTROL_COMMAND_TIMEOUT: Duration = Duration::from_secs(60);
/// Concurrent control streams per connection (one controller session).
const CONTROL_STREAMS: usize = 4;

/// Live inbound connections per remote endpoint, so withdrawing an endpoint
/// from an allow-list also closes what it already has open.
#[derive(Default)]
struct LiveConnections {
    next: u64,
    by_remote: HashMap<EndpointId, Vec<(u64, iroh::endpoint::Connection)>>,
}

impl LiveConnections {
    fn add(&mut self, remote: EndpointId, connection: iroh::endpoint::Connection) -> u64 {
        self.next += 1;
        self.by_remote.entry(remote).or_default().push((self.next, connection));
        self.next
    }

    fn remove(&mut self, remote: EndpointId, id: u64) {
        if let Some(list) = self.by_remote.get_mut(&remote) {
            list.retain(|(i, _)| *i != id);
            if list.is_empty() {
                self.by_remote.remove(&remote);
            }
        }
    }

    /// Close every live connection whose remote is no longer `allowed`.
    fn close_revoked(&mut self, allowed: impl Fn(&EndpointId) -> bool) {
        self.by_remote.retain(|remote, list| {
            if allowed(remote) {
                return true;
            }
            for (_, connection) in list.drain(..) {
                connection.close(REJECT_CODE.into(), b"revoked");
            }
            false
        });
    }
}

type Live = Arc<std::sync::Mutex<LiveConnections>>;

/// One control command from an allow-listed controller (plan §4.2): the
/// command document and where to send the canonical response document.
#[derive(Debug)]
pub struct ControlRequest {
    pub from: EndpointId,
    pub command: Vec<u8>,
    pub reply: oneshot::Sender<Vec<u8>>,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct TransportError(pub String);

fn err(code: &str) -> TransportError {
    TransportError(code.to_owned())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reachability {
    /// n0 production relays plus direct paths (the product default).
    Production,
    /// Loopback only, no relays: deterministic in-process tests.
    LocalOnly,
}

/// Local, revocable authority. Clones retain the exact installed authority;
/// replacing a peer/session never revives a previously admitted frame. A peer
/// has its own revocation bit and the lifecycle authority as its parent.
#[derive(Debug, Clone, Default)]
pub struct Admission(Arc<AdmissionState>);

#[derive(Debug, Default)]
struct AdmissionState {
    revoked: std::sync::atomic::AtomicBool,
    parent: Option<Admission>,
}

impl Admission {
    fn child(&self) -> Self {
        Self(Arc::new(AdmissionState { revoked: false.into(), parent: Some(self.clone()) }))
    }
    pub(crate) fn revoke(&self) {
        self.0.revoked.store(true, std::sync::atomic::Ordering::Release);
    }
    pub(crate) fn revoked(&self) -> bool {
        self.0.revoked.load(std::sync::atomic::Ordering::Acquire)
            || self.0.parent.as_ref().is_some_and(Self::revoked)
    }
}

#[derive(Clone)]
struct InstalledPeer {
    binding: PeerBinding,
    admission: Admission,
}

impl std::ops::Deref for InstalledPeer {
    type Target = PeerBinding;
    fn deref(&self) -> &Self::Target { &self.binding }
}

/// One admitted inbound Router frame.
#[derive(Debug)]
pub struct Inbound {
    pub admission: Admission,
    pub from: EndpointId,
    /// The Router node id the peer is configured as (its endpoint id when
    /// configured without one).
    pub from_node: String,
    pub generation: u64,
    pub frame: Frame,
    /// With [`NodeTransport::spawn_accept_dispatch`]: the dispatcher's verdict,
    /// `Ok(())` to acknowledge or `Err(code)` to refuse (the sender retries).
    pub reply: Option<oneshot::Sender<Result<(), String>>>,
}

/// One allow-listed peer: its route generation and Router node id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PeerBinding {
    pub generation: u64,
    pub node_id: String,
    pub addr: EndpointAddr,
}

/// A dispatched message's frame digest and verdict.
type SeenEntry = ([u8; 32], Result<(), String>);

/// Remote message ids already dispatched: id -> (frame digest, verdict).
#[derive(Default)]
struct Seen {
    done: HashMap<[u8; 16], SeenEntry>,
    order: VecDeque<[u8; 16]>,
    inflight: HashMap<[u8; 16], [u8; 32]>,
}

/// Which path a connection used: evidence for the `network` gate family.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct PathReport {
    pub selected: &'static str,
    pub rtt_ms: u64,
    pub path_count: usize,
}

#[derive(Clone)]
pub struct NodeTransport {
    endpoint: Endpoint,
    peers: Arc<RwLock<HashMap<EndpointId, InstalledPeer>>>,
    seen: Arc<AsyncMutex<Seen>>,
    /// Endpoints allowed to open the control ALPN, and where commands go.
    controllers: Arc<RwLock<std::collections::HashSet<EndpointId>>>,
    control: Arc<RwLock<Option<mpsc::Sender<ControlRequest>>>>,
    /// Live data-plane and control connections (closed on revocation).
    peer_live: Live,
    control_live: Live,
    /// Bumped on every controller-set change: the node reconciles the
    /// control session and what it installed against the new set.
    controller_epoch: Arc<tokio::sync::watch::Sender<u64>>,
    primary_peer: Arc<std::sync::Mutex<Option<String>>>,
    frames_sent: Arc<std::sync::atomic::AtomicU64>,
    frames_received: Arc<std::sync::atomic::AtomicU64>,
    fatal: Arc<std::sync::Mutex<Option<String>>>,
}

impl NodeTransport {
    pub async fn bind(secret: &[u8; 32], reach: Reachability) -> Result<Self, TransportError> {
        let builder = match reach {
            Reachability::Production => Endpoint::builder(presets::N0),
            Reachability::LocalOnly => Endpoint::builder(presets::Minimal)
                .relay_mode(RelayMode::Disabled)
                .clear_ip_transports()
                .bind_addr(std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0))
                .map_err(|_| err("endpoint_bind_failed"))?,
        };
        let endpoint = builder
            .secret_key(SecretKey::from_bytes(secret))
            .alpns(vec![IROH_ALPN.to_vec(), CONTROL_ALPN.to_vec()])
            .bind()
            .await
            .map_err(|_| err("endpoint_bind_failed"))?;
        Ok(Self { endpoint, peers: Arc::new(RwLock::new(HashMap::new())), seen: Arc::default(),
            controllers: Arc::default(),
            control: Arc::default(),
            peer_live: Live::default(),
            control_live: Live::default(),
            controller_epoch: Arc::new(tokio::sync::watch::channel(0).0),
            primary_peer: Arc::default(), frames_sent: Arc::default(), frames_received: Arc::default(), fatal: Arc::default(),
        })
    }

    pub(crate) fn mark_fatal(&self, code: &str) {
        *self.fatal.lock().unwrap() = Some(code.to_owned());
    }

    pub async fn snapshot(&self) -> serde_json::Value {
        let peers = self.peers.read().await;
        let mut ids: Vec<&str> = peers.values().map(|p| p.node_id.as_str()).collect();
        ids.sort_unstable();
        serde_json::json!({
            "peer_node_id": *self.primary_peer.lock().unwrap(), "peer_node_ids": ids,
            "remote_frames_sent": self.frames_sent.load(std::sync::atomic::Ordering::Relaxed),
            "remote_frames_received": self.frames_received.load(std::sync::atomic::Ordering::Relaxed),
            "fatal_error": *self.fatal.lock().unwrap(),
            "counter_scope": "transport_lifetime_written_and_admitted_frames",
        })
    }

    pub fn endpoint_id(&self) -> String {
        self.endpoint.id().to_string()
    }

    /// The node's endpoint (artifact acquisition dials sources with it, so the
    /// source authenticates the same iroh identity the grant names).
    pub fn endpoint(&self) -> Endpoint {
        self.endpoint.clone()
    }

    pub fn addr(&self) -> EndpointAddr {
        self.endpoint.addr()
    }

    /// Wait until a relay (home) connection is up, bounded.
    pub async fn online(&self, within: Duration) -> bool {
        timeout(within, self.endpoint.online()).await.is_ok()
    }

    /// Advertised transport addresses (`endpoint_addr.addrs` in the join request).
    pub fn advertised_addrs(&self) -> Vec<String> {
        let addr = self.endpoint.addr();
        let mut out: Vec<String> = addr.addrs.iter().map(|a| a.to_string()).collect();
        out.sort();
        out.truncate(16);
        if out.is_empty() {
            out.push(format!("iroh:{}", self.endpoint.id()));
        }
        out
    }

    /// Allow-list one peer at an exact generation (ConfigurePeer semantics).
    pub async fn configure_peer(&self, peer: EndpointId, generation: u64) {
        self.configure_peer_authorized(peer, generation, Admission::default()).await;
    }

    pub(crate) async fn configure_peer_authorized(&self, peer: EndpointId, generation: u64, admission: Admission) {
        let mut peers = self.peers.write().await;
        let binding = PeerBinding { generation, node_id: peer.to_string(), addr: EndpointAddr::from(peer) };
        if let Some(old) = peers.insert(peer, InstalledPeer { binding, admission: admission.child() }) {
            old.admission.revoke();
        }
        if peers.len() == 1 { *self.primary_peer.lock().unwrap() = Some(peer.to_string()); }
    }

    /// Replace the routed peer set (ConfigurePeers semantics): each Router
    /// node id with its endpoint (and address hints) at its route generation.
    pub async fn configure_route_peers(&self, peers: Vec<PeerBinding>) {
        self.configure_route_peers_authorized(peers, Admission::default()).await;
    }

    /// Install peer authority rooted in the node's current lifecycle. The root
    /// is captured under its state lock, never refreshed when dequeuing work.
    pub(crate) async fn configure_route_peers_authorized(&self, peers: Vec<PeerBinding>, admission: Admission) {
        let mut table = self.peers.write().await;
        for peer in table.values() { peer.admission.revoke(); }
        table.clear();
        // ConfigurePeers carries the control peer first, then its optional
        // forward peer. Never guess this role by sorting node identifiers.
        *self.primary_peer.lock().unwrap() = peers.first().map(|p| p.node_id.clone());
        for binding in peers {
            table.insert(binding.addr.id, InstalledPeer { binding, admission: admission.child() });
        }
        // A peer that left the route keeps no live connection.
        self.peer_live.lock().unwrap().close_revoked(|remote| table.contains_key(remote));
    }

    /// The configured peer bound to a Router node id.
    pub async fn peer_for_node(&self, node_id: &str) -> Option<PeerBinding> {
        self.peers.read().await.values().find(|b| b.node_id == node_id && !b.admission.revoked()).map(|b| b.binding.clone())
    }

    /// Withdraw a peer from the allow-list (its next connection is refused).
    pub async fn remove_peer(&self, peer: EndpointId) {
        let table = {
            let mut table = self.peers.write().await;
            if let Some(removed) = table.remove(&peer) {
                removed.admission.revoke();
                let mut primary = self.primary_peer.lock().unwrap();
                if primary.as_deref() == Some(&removed.node_id) { *primary = None; }
            }
            table.keys().copied().collect::<std::collections::HashSet<_>>()
        };
        self.peer_live.lock().unwrap().close_revoked(|remote| table.contains(remote));
    }

    pub async fn close(&self) {
        *self.fatal.lock().unwrap() = Some("transport_closed".into());
        self.endpoint.close().await;
    }

    /// Accept loop: admitted frames go to `inbox`; each is acknowledged once
    /// enqueued. Unknown peers and stale generations are refused with the same
    /// codes the sidecar uses.
    pub fn spawn_accept(&self, inbox: mpsc::Sender<Inbound>) -> tokio::task::JoinHandle<()> {
        self.accept_loop(inbox, false)
    }

    /// Accept loop with the Python transport's delivery semantics
    /// (`transports/iroh.py`): a frame is acknowledged only after the
    /// dispatcher's verdict (`Inbound::reply`), a refused or timed-out
    /// dispatch is answered with an error so the sender retries, and a
    /// repeated message id is answered from the seen table without being
    /// dispatched again (another frame under the same id: `replay_collision`).
    pub fn spawn_accept_dispatch(&self, inbox: mpsc::Sender<Inbound>) -> tokio::task::JoinHandle<()> {
        self.accept_loop(inbox, true)
    }

    /// Replace the controllers allowed to open the control ALPN.
    /// A controller withdrawn here loses its live control connections too.
    pub async fn configure_controllers(&self, controllers: impl IntoIterator<Item = EndpointId>) {
        let allowed: std::collections::HashSet<EndpointId> = controllers.into_iter().collect();
        *self.controllers.write().await = allowed.clone();
        self.control_live.lock().unwrap().close_revoked(|remote| allowed.contains(remote));
        self.controller_epoch.send_modify(|epoch| *epoch += 1);
    }

    /// Whether `controller` is in the (single, transport-owned) controller set.
    pub async fn is_controller(&self, controller: &EndpointId) -> bool {
        self.controllers.read().await.contains(controller)
    }

    /// Notified on every controller-set change.
    pub fn subscribe_controller_changes(&self) -> tokio::sync::watch::Receiver<u64> {
        self.controller_epoch.subscribe()
    }

    /// Route control commands to `handler` (none: control connections refused).
    pub async fn set_control_handler(&self, handler: Option<mpsc::Sender<ControlRequest>>) {
        *self.control.write().await = handler;
    }

    /// Controller side: send one control command document, return the response.
    pub async fn control_call(&self, peer: EndpointAddr, command: &[u8]) -> Result<Vec<u8>, TransportError> {
        let connection = timeout(Duration::from_secs(30), self.endpoint.connect(peer, CONTROL_ALPN))
            .await
            .map_err(|_| err("connect_timeout"))?
            .map_err(|e| TransportError(format!("connect_failed:{e}")))?;
        let (mut send, mut recv) = timeout(STREAM_IO_TIMEOUT, connection.open_bi()).await.map_err(|_| err("open_timeout"))?.map_err(|_| err("open_failed"))?;
        timeout(STREAM_IO_TIMEOUT, send.write_all(command)).await.map_err(|_| err("write_timeout"))?.map_err(|_| err("write_failed"))?;
        send.finish().map_err(|_| err("write_failed"))?;
        timeout(CONTROL_COMMAND_TIMEOUT + STREAM_IO_TIMEOUT, recv.read_to_end(MAX_COMMAND_BYTES))
            .await
            .map_err(|_| err("read_timeout"))?
            .map_err(|_| err("read_failed"))
    }

    fn accept_loop(&self, inbox: mpsc::Sender<Inbound>, dispatch: bool) -> tokio::task::JoinHandle<()> {
        let endpoint = self.endpoint.clone();
        let frames_received = self.frames_received.clone();
        let fatal = self.fatal.clone();
        let peers = self.peers.clone();
        let seen = self.seen.clone();
        let controllers = self.controllers.clone();
        let control = self.control.clone();
        let (peer_live, control_live) = (self.peer_live.clone(), self.control_live.clone());
        tokio::spawn(async move {
            while let Some(incoming) = endpoint.accept().await {
                let peers = peers.clone();
                let frames_received = frames_received.clone();
                let inbox = inbox.clone();
                let seen = seen.clone();
                let (controllers, control) = (controllers.clone(), control.clone());
                let (peer_live, control_live) = (peer_live.clone(), control_live.clone());
                tokio::spawn(async move {
                    let Ok(connection) = incoming.await else { return };
                    let remote = connection.remote_id();
                    if connection.alpn() == CONTROL_ALPN {
                        serve_control(connection, remote, controllers, control, control_live).await;
                        return;
                    }
                    let Some(binding) = peers.read().await.get(&remote).cloned() else {
                        connection.close(REJECT_CODE.into(), b"identity");
                        return;
                    };
                    // Registered before the re-check, so a removal racing this
                    // accept either sees the connection or is seen by it.
                    let live_id = peer_live.lock().unwrap().add(remote, connection.clone());
                    if binding.admission.revoked() || !peers.read().await.contains_key(&remote) {
                        connection.close(REJECT_CODE.into(), b"revoked");
                        peer_live.lock().unwrap().remove(remote, live_id);
                        return;
                    }
                    let _live = LiveGuard { live: peer_live.clone(), remote, id: live_id };
                    while let Ok((mut send, mut recv)) = connection.accept_bi().await {
                        // Re-checked for every stream: a peer removed from the
                        // route loses this connection (its generation check
                        // below would also refuse the frame).
                        if binding.admission.revoked() || !peers.read().await.contains_key(&remote) {
                            connection.close(REJECT_CODE.into(), b"revoked");
                            return;
                        }
                        let Ok(Ok(bytes)) = timeout(STREAM_IO_TIMEOUT, recv.read_to_end(MAX_TRANSFER_BYTES)).await
                        else {
                            continue;
                        };
                        let Ok(request) = wire::decode_remote(&bytes) else { continue };
                        if request.kind != RemoteKind::Transfer {
                            continue;
                        }
                        // The binding may have rotated since the connection opened.
                        let current = peers.read().await.get(&remote).map(|b| b.generation);
                        let verdict: Result<(), String> = match wire::split_transfer(&request.payload) {
                            Ok(_) if binding.admission.revoked() => Err("peer_rotated".into()),
                            Ok((generation, _)) if Some(generation) != current || generation != binding.generation => Err("peer_rotated".into()),
                            Ok((generation, raw)) => match wire::decode_frame(raw) {
                                Ok(frame) if !dispatch => match inbox.try_send(Inbound { admission: binding.admission.clone(), from: remote, from_node: binding.node_id.clone(), generation, frame, reply: None }) {
                                    Ok(()) => Ok(()),
                                    Err(_) => Err("queue_full".into()),
                                },
                                Ok(frame) => {
                                    deliver(&seen, &inbox, request.message_id, raw, Inbound { admission: binding.admission.clone(), from: remote, from_node: binding.node_id.clone(), generation, frame, reply: None }).await
                                }
                                Err(_) => Err("invalid_frame".into()),
                            },
                            Err(_) => Err("invalid_frame".into()),
                        };
                        if verdict.is_ok() { frames_received.fetch_add(1, std::sync::atomic::Ordering::Relaxed); }
                        let (kind, payload) = match &verdict {
                            Ok(()) => (RemoteKind::Ack, Vec::new()),
                            Err(code) => (RemoteKind::Error, code.as_bytes().to_vec()),
                        };
                        if let Ok(response) = wire::encode_remote(kind, request.message_id, &payload) {
                            let _ = timeout(STREAM_IO_TIMEOUT, send.write_all(&response)).await;
                            let _ = send.finish();
                        }
                    }
                });
            }
            *fatal.lock().unwrap() = Some("transport_accept_stopped".into());
        })
    }

    /// Send one Router frame to a peer and wait for its Ack.
    pub async fn send(
        &self,
        peer: EndpointAddr,
        generation: u64,
        router_frame: &[u8],
    ) -> Result<PathReport, TransportError> {
        self.send_cancellable(peer, generation, router_frame, std::future::pending()).await
    }

    /// Request-scoped cancellation keeps the normal data deadlines unchanged.
    /// A cancelled exchange explicitly resets/stops its owned streams before
    /// returning; dropping a send future alone could gracefully flush bytes.
    pub(crate) async fn send_cancellable(
        &self,
        peer: EndpointAddr,
        generation: u64,
        router_frame: &[u8],
        cancel: impl std::future::Future<Output = ()>,
    ) -> Result<PathReport, TransportError> {
        tokio::pin!(cancel);
        let connection = tokio::select! {
            biased;
            _ = &mut cancel => return Err(err("request_cancelled_or_expired")),
            result = timeout(Duration::from_secs(30), self.endpoint.connect(peer, IROH_ALPN)) =>
                result.map_err(|_| err("connect_timeout"))?.map_err(|e| TransportError(format!("connect_failed:{e}")))?,
        };
        let (mut send, mut recv) = tokio::select! {
            biased;
            _ = &mut cancel => return Err(err("request_cancelled_or_expired")),
            result = timeout(STREAM_IO_TIMEOUT, connection.open_bi()) =>
                result.map_err(|_| err("open_timeout"))?.map_err(|_| err("open_failed"))?,
        };
        let mut message_id = [0u8; 16];
        getrandom::getrandom(&mut message_id).map_err(|_| err("randomness_unavailable"))?;
        let encoded = wire::encode_remote(RemoteKind::Transfer, message_id, &wire::transfer_payload(generation, router_frame))
            .map_err(|e| TransportError(e.0.into()))?;
        let outcome = tokio::select! {
            biased;
            _ = &mut cancel => Err(err("request_cancelled_or_expired")),
            result = async {
                timeout(STREAM_IO_TIMEOUT, send.write_all(&encoded)).await
                    .map_err(|_| err("write_timeout"))?.map_err(|_| err("write_failed"))?;
                send.finish().map_err(|_| err("write_failed"))?;
                self.frames_sent.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                let response = timeout(STREAM_IO_TIMEOUT, recv.read_to_end(MAX_TRANSFER_BYTES)).await
                    .map_err(|_| err("read_timeout"))?.map_err(|_| err("read_failed"))?;
                let response = wire::decode_remote(&response).map_err(|e| TransportError(e.0.into()))?;
                if response.message_id != message_id { return Err(err("response_id_mismatch")); }
                match response.kind {
                    RemoteKind::Ack => Ok(path_report(&connection)),
                    RemoteKind::Error => Err(TransportError(String::from_utf8_lossy(&response.payload).into_owned())),
                    RemoteKind::Transfer => Err(err("unexpected_transfer")),
                }
            } => result,
        };
        if outcome.is_err() {
            let _ = send.reset(REJECT_CODE.into());
            let _ = recv.stop(REJECT_CODE.into());
        }
        outcome
    }
}

/// One controller connection: only an allow-listed controller, a bounded
/// command per stream, a bounded number of streams in flight, and every
/// answer within [`CONTROL_COMMAND_TIMEOUT`].
async fn serve_control(
    connection: iroh::endpoint::Connection,
    remote: EndpointId,
    controllers: Arc<RwLock<std::collections::HashSet<EndpointId>>>,
    control: Arc<RwLock<Option<mpsc::Sender<ControlRequest>>>>,
    live: Live,
) {
    let handler = control.read().await.clone();
    let (true, Some(handler)) = (controllers.read().await.contains(&remote), handler) else {
        connection.close(REJECT_CODE.into(), b"identity");
        return;
    };
    let live_id = live.lock().unwrap().add(remote, connection.clone());
    let _live = LiveGuard { live: live.clone(), remote, id: live_id };
    let slots = Arc::new(tokio::sync::Semaphore::new(CONTROL_STREAMS));
    while let Ok((mut send, mut recv)) = connection.accept_bi().await {
        // Membership is re-checked for every stream: a withdrawn controller's
        // connection is closed, never served.
        if !controllers.read().await.contains(&remote) {
            connection.close(REJECT_CODE.into(), b"revoked");
            return;
        }
        let Ok(permit) = slots.clone().try_acquire_owned() else {
            let _ = send.finish();
            continue;
        };
        let (handler, controllers, connection) = (handler.clone(), controllers.clone(), connection.clone());
        tokio::spawn(async move {
            let _permit = permit;
            let Ok(Ok(command)) = timeout(STREAM_IO_TIMEOUT, recv.read_to_end(MAX_COMMAND_BYTES)).await else { return };
            // And again right before the command reaches the node.
            if !controllers.read().await.contains(&remote) {
                connection.close(REJECT_CODE.into(), b"revoked");
                return;
            }
            let (reply, answer) = oneshot::channel();
            if handler.send(ControlRequest { from: remote, command, reply }).await.is_err() {
                return;
            }
            if let Ok(Ok(response)) = timeout(CONTROL_COMMAND_TIMEOUT, answer).await {
                let _ = timeout(STREAM_IO_TIMEOUT, send.write_all(&response)).await;
                let _ = send.finish();
            }
        });
    }
}

/// Unregisters a live connection when its serving task ends.
struct LiveGuard {
    live: Live,
    remote: EndpointId,
    id: u64,
}

impl Drop for LiveGuard {
    fn drop(&mut self) {
        if let Ok(mut live) = self.live.lock() {
            live.remove(self.remote, self.id);
        }
    }
}

fn path_report(connection: &iroh::endpoint::Connection) -> PathReport {
    let paths = connection.paths();
    let mut selected = "unknown";
    let mut rtt_ms = 0;
    for p in paths.iter() {
        if p.is_selected() {
            selected = if p.is_relay() { "relay" } else if p.is_ip() { "direct" } else { "other" };
            rtt_ms = p.rtt().as_millis() as u64;
        }
    }
    PathReport { selected, rtt_ms, path_count: paths.len() }
}

/// Dispatch one delivery and wait for its verdict, deduplicating by message id.
async fn deliver(
    seen: &Arc<AsyncMutex<Seen>>,
    inbox: &mpsc::Sender<Inbound>,
    message_id: [u8; 16],
    raw: &[u8],
    mut inbound: Inbound,
) -> Result<(), String> {
    use sha2::{Digest, Sha256};
    let digest: [u8; 32] = Sha256::digest(raw).into();
    let admission = inbound.admission.clone();
    {
        let mut table = seen.lock().await;
        if admission.revoked() { return Err("peer_rotated".into()); }
        if let Some((done, verdict)) = table.done.get(&message_id) {
            return if *done == digest { verdict.clone() } else { Err("replay_collision".into()) };
        }
        if let Some(pending) = table.inflight.get(&message_id) {
            return if *pending == digest { Err("duplicate_inflight".into()) } else { Err("replay_collision".into()) };
        }
        table.inflight.insert(message_id, digest);
    }
    let (tx, rx) = oneshot::channel();
    inbound.reply = Some(tx);
    let verdict = match inbox.try_send(inbound) {
        Err(_) => Err("queue_full".to_owned()),
        Ok(()) => match timeout(DISPATCH_TIMEOUT, rx).await {
            Ok(Ok(verdict)) => verdict,
            Ok(Err(_)) => Err("dispatch_dropped".to_owned()),
            Err(_) => Err("dispatch_timeout".to_owned()),
        },
    };
    let mut table = seen.lock().await;
    table.inflight.remove(&message_id);
    if admission.revoked() { return Err("peer_rotated".into()); }
    // Only an acknowledged delivery is final: a refused one may be retried.
    if verdict.is_ok() {
        table.done.insert(message_id, (digest, verdict.clone()));
        table.order.push_back(message_id);
        while table.order.len() > SEEN_LIMIT {
            if let Some(old) = table.order.pop_front() {
                table.done.remove(&old);
            }
        }
    }
    verdict
}

pub fn parse_endpoint_id(text: &str) -> Result<EndpointId, TransportError> {
    text.parse().map_err(|_| err("endpoint_id_invalid"))
}
