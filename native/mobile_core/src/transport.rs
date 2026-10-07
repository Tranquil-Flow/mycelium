// SPDX-License-Identifier: AGPL-3.0-or-later
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

/// One admitted inbound Router frame.
#[derive(Debug)]
pub struct Inbound {
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
    peers: Arc<RwLock<HashMap<EndpointId, PeerBinding>>>,
    seen: Arc<AsyncMutex<Seen>>,
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
            .alpns(vec![IROH_ALPN.to_vec()])
            .bind()
            .await
            .map_err(|_| err("endpoint_bind_failed"))?;
        Ok(Self { endpoint, peers: Arc::new(RwLock::new(HashMap::new())), seen: Arc::default() })
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
        self.peers.write().await.insert(peer, PeerBinding { generation, node_id: peer.to_string(), addr: EndpointAddr::from(peer) });
    }

    /// Replace the routed peer set (ConfigurePeers semantics): each Router
    /// node id with its endpoint (and address hints) at its route generation.
    pub async fn configure_route_peers(&self, peers: Vec<PeerBinding>) {
        let mut table = self.peers.write().await;
        table.clear();
        for binding in peers {
            table.insert(binding.addr.id, binding);
        }
    }

    /// The configured peer bound to a Router node id.
    pub async fn peer_for_node(&self, node_id: &str) -> Option<PeerBinding> {
        self.peers.read().await.values().find(|b| b.node_id == node_id).cloned()
    }

    /// Withdraw a peer from the allow-list (its next connection is refused).
    pub async fn remove_peer(&self, peer: EndpointId) {
        self.peers.write().await.remove(&peer);
    }

    pub async fn close(&self) {
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

    fn accept_loop(&self, inbox: mpsc::Sender<Inbound>, dispatch: bool) -> tokio::task::JoinHandle<()> {
        let endpoint = self.endpoint.clone();
        let peers = self.peers.clone();
        let seen = self.seen.clone();
        tokio::spawn(async move {
            while let Some(incoming) = endpoint.accept().await {
                let peers = peers.clone();
                let inbox = inbox.clone();
                let seen = seen.clone();
                tokio::spawn(async move {
                    let Ok(connection) = incoming.await else { return };
                    let remote = connection.remote_id();
                    let Some(binding) = peers.read().await.get(&remote).cloned() else {
                        connection.close(REJECT_CODE.into(), b"identity");
                        return;
                    };
                    while let Ok((mut send, mut recv)) = connection.accept_bi().await {
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
                            Ok((generation, _)) if Some(generation) != current || generation != binding.generation => Err("peer_rotated".into()),
                            Ok((generation, raw)) => match wire::decode_frame(raw) {
                                Ok(frame) if !dispatch => match inbox.try_send(Inbound { from: remote, from_node: binding.node_id.clone(), generation, frame, reply: None }) {
                                    Ok(()) => Ok(()),
                                    Err(_) => Err("queue_full".into()),
                                },
                                Ok(frame) => {
                                    deliver(&seen, &inbox, request.message_id, raw, Inbound { from: remote, from_node: binding.node_id.clone(), generation, frame, reply: None }).await
                                }
                                Err(_) => Err("invalid_frame".into()),
                            },
                            Err(_) => Err("invalid_frame".into()),
                        };
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
        })
    }

    /// Send one Router frame to a peer and wait for its Ack.
    pub async fn send(
        &self,
        peer: EndpointAddr,
        generation: u64,
        router_frame: &[u8],
    ) -> Result<PathReport, TransportError> {
        let connection = timeout(Duration::from_secs(30), self.endpoint.connect(peer, IROH_ALPN))
            .await
            .map_err(|_| err("connect_timeout"))?
            .map_err(|e| TransportError(format!("connect_failed:{e}")))?;
        let (mut send, mut recv) = timeout(STREAM_IO_TIMEOUT, connection.open_bi())
            .await
            .map_err(|_| err("open_timeout"))?
            .map_err(|_| err("open_failed"))?;
        let mut message_id = [0u8; 16];
        getrandom::getrandom(&mut message_id).map_err(|_| err("randomness_unavailable"))?;
        let encoded = wire::encode_remote(RemoteKind::Transfer, message_id, &wire::transfer_payload(generation, router_frame))
            .map_err(|e| TransportError(e.0.into()))?;
        timeout(STREAM_IO_TIMEOUT, send.write_all(&encoded))
            .await
            .map_err(|_| err("write_timeout"))?
            .map_err(|_| err("write_failed"))?;
        send.finish().map_err(|_| err("write_failed"))?;
        let response = timeout(STREAM_IO_TIMEOUT, recv.read_to_end(MAX_TRANSFER_BYTES))
            .await
            .map_err(|_| err("read_timeout"))?
            .map_err(|_| err("read_failed"))?;
        let response = wire::decode_remote(&response).map_err(|e| TransportError(e.0.into()))?;
        if response.message_id != message_id {
            return Err(err("response_id_mismatch"));
        }
        let report = path_report(&connection);
        match response.kind {
            RemoteKind::Ack => Ok(report),
            RemoteKind::Error => Err(TransportError(String::from_utf8_lossy(&response.payload).into_owned())),
            RemoteKind::Transfer => Err(err("unexpected_transfer")),
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
    {
        let mut table = seen.lock().await;
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
