// SPDX-License-Identifier: AGPL-3.0-only
//! A2-over-iroh: the carrier for phone stage objects on the ALPN
//! `mycelium.artifact_chunk.v1` (authority objects in [`crate::artifact_grant`]).
//!
//! One bidirectional stream per chunk:
//! - the phone writes the canonical JSON of its member-signed chunk request
//!   (which embeds the seed-signed grant) and finishes its side;
//! - the source answers `u32 BE header length | header JSON | bytes`, where the
//!   header is `{"protocol": RESPONSE_PROTOCOL, "status": "ok", "receipt": ..}`
//!   followed by exactly the receipt's `length_bytes`, or
//!   `{"protocol": .., "status": "refused", "code": ..}` with no bytes.
//!
//! The source ([`ArtifactSource`]) admits a request only when the request
//! verifies (`verify_chunk_request`), the authenticated iroh peer is the
//! grant's recipient endpoint, the request id and nonce are new for the grant,
//! the grant's total byte budget is not exceeded, and the object file is the
//! content its name says (re-hashed once, A2 `artifact_source_chunk_unverified`).
//! It paces each grant to `maximum_bytes_per_second` and signs a receipt over
//! the bytes it serves.
//!
//! The replay fence and per-grant byte budget are durable and fail closed:
//! - every admission is appended and fsynced to `<state>/source-ledger.jsonl`
//!   under the same lock as the fence and budget check, before any byte is
//!   read or sent; a request whose bytes then fail to leave stays fenced and
//!   charged;
//! - a failed append poisons the journal: the source refuses everything until
//!   it is rebound (no later line can be glued onto a torn one);
//! - the journal starts with a genesis time; a grant whose window began before
//!   genesis is refused (`artifact_source_ledger_reset`), so a cleared or new
//!   ledger never re-serves a grant it cannot account for;
//! - a missing header, an unreadable file or a malformed line anywhere but a
//!   torn final line fails the bind (`artifact_source_ledger_corrupt` /
//!   `_unavailable`) instead of starting empty;
//! - fence entries live until their request window has passed; at capacity the
//!   source refuses new requests (`artifact_source_busy`) rather than evict.
//!
//! The phone ([`IrohChunkSource`]) checks every receipt
//! (`verify_chunk_receipt`) before the bytes reach the artifact store, which
//! then digest-verifies whole objects before promotion.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use iroh::endpoint::presets;
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMode, SecretKey};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::time::timeout;

use crate::artifact::{ArtifactError, ChunkSource};
use crate::artifact_grant::{self as ag, ARTIFACT_CHUNK_ALPN, Grant};
use crate::canonical;
use crate::identity::Identity;
use crate::transport::Reachability;

pub const RESPONSE_PROTOCOL: &str = "mycelium.mobile_artifact_chunk_response.v1";
const MAX_REQUEST_BYTES: usize = 256 * 1024;
const MAX_HEADER_BYTES: usize = 64 * 1024;
const IO_TIMEOUT: Duration = Duration::from_secs(30);
/// Serving bounds (the sidecar also carries live Router traffic, so a source
/// must never take more than these, and refuses before any heavy work):
/// concurrent artifact connections in total and per peer, one stream at a
/// time per connection, a read deadline per request, an idle deadline per
/// connection, and a small pool for the blocking work (disk reads, hashing,
/// signature checks, journal fsync), which never runs on the async executor.
pub const MAX_ARTIFACT_CONNECTIONS: usize = 16;
pub const MAX_ARTIFACT_CONNECTIONS_PER_PEER: usize = 2;
const MAX_BLOCKING_WORK: usize = 4;
const MAX_PACE: Duration = Duration::from_secs(10);

/// Serving bounds; [`ArtifactLimits::default`] is the product setting.
#[derive(Debug, Clone, Copy)]
pub struct ArtifactLimits {
    pub connections: usize,
    pub connections_per_peer: usize,
    pub read_timeout: Duration,
    pub idle_timeout: Duration,
}

impl Default for ArtifactLimits {
    fn default() -> Self {
        Self {
            connections: MAX_ARTIFACT_CONNECTIONS,
            connections_per_peer: MAX_ARTIFACT_CONNECTIONS_PER_PEER,
            read_timeout: Duration::from_secs(10),
            idle_timeout: Duration::from_secs(20),
        }
    }
}
const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// Live fence entries per source (requests still inside their window).
const MAX_FENCE: usize = 65_536;
const LEDGER_PROTOCOL: &str = "mycelium.artifact_source_ledger.v1";

fn now_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

async fn bind(secret: &[u8; 32], reach: Reachability, alpn: &[u8]) -> Result<Endpoint, ArtifactError> {
    let builder = match reach {
        Reachability::Production => Endpoint::builder(presets::N0),
        Reachability::LocalOnly => Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .clear_ip_transports()
            .bind_addr(std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0))
            .map_err(|_| ArtifactError("endpoint_bind_failed".into()))?,
    };
    builder
        .secret_key(SecretKey::from_bytes(secret))
        .alpns(vec![alpn.to_vec()])
        .bind()
        .await
        .map_err(|_| ArtifactError("endpoint_bind_failed".into()))
}

/// The durable admission journal: a genesis header, then one JSON line per
/// admitted request.
struct Journal {
    path: PathBuf,
    file: std::fs::File,
}

fn ledger_err(code: &str) -> ArtifactError {
    ArtifactError(code.to_owned())
}

impl Journal {
    /// Replay `<dir>/source-ledger.jsonl` into `state` (a new ledger gets
    /// genesis = now), keep the entries of grants still inside their window,
    /// and rewrite it compacted (tmp, fsync, rename, dir fsync).
    fn open(dir: &std::path::Path, state: &mut SourceState, now: u64) -> Result<Self, ArtifactError> {
        use std::io::Write;
        let unavailable = |_| ledger_err("artifact_source_ledger_unavailable");
        std::fs::create_dir_all(dir).map_err(unavailable)?;
        let path = dir.join("source-ledger.jsonl");
        let mut kept = Vec::new();
        let genesis = match std::fs::symlink_metadata(&path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => now,
            Err(_) => return Err(ledger_err("artifact_source_ledger_unavailable")),
            Ok(meta) if !meta.file_type().is_file() => return Err(ledger_err("artifact_source_ledger_corrupt")),
            Ok(_) => {
                let raw = std::fs::read_to_string(&path).map_err(unavailable)?;
                let complete = raw.ends_with('\n');
                let lines: Vec<&str> = raw.lines().collect();
                let header: Value = lines.first().and_then(|l| serde_json::from_str(l).ok()).ok_or_else(|| ledger_err("artifact_source_ledger_corrupt"))?;
                let genesis = match (header["protocol"].as_str(), header["genesis_unix_ms"].as_u64()) {
                    (Some(LEDGER_PROTOCOL), Some(g)) if lines.len() > 1 || complete => g,
                    _ => return Err(ledger_err("artifact_source_ledger_corrupt")),
                };
                for (i, line) in lines.iter().enumerate().skip(1) {
                    let parsed = serde_json::from_str::<Value>(line).ok().and_then(|v| {
                        Some((v["g"].as_str()?.to_owned(), v["e"].as_u64()?, v["f"].as_str()?.to_owned(), v["n"].as_u64()?, v["x"].as_u64()?))
                    });
                    let Some((g, e, f, n, x)) = parsed else {
                        // Only a torn final line (crash mid-append) is not an admission.
                        if i + 1 == lines.len() && !complete {
                            continue;
                        }
                        return Err(ledger_err("artifact_source_ledger_corrupt"));
                    };
                    if e + ag::CLOCK_SKEW_MS < now {
                        continue; // the grant can no longer be presented
                    }
                    state.admit(f, x, &g, n, now);
                    kept.push((*line).to_owned());
                }
                genesis
            }
        };
        state.genesis_unix_ms = genesis;
        let tmp = dir.join("source-ledger.jsonl.tmp");
        let mut f = std::fs::File::create(&tmp).map_err(unavailable)?;
        writeln!(f, "{}", json!({"protocol": LEDGER_PROTOCOL, "genesis_unix_ms": genesis})).map_err(unavailable)?;
        for line in &kept {
            writeln!(f, "{line}").map_err(unavailable)?;
        }
        f.sync_all().map_err(unavailable)?;
        std::fs::rename(&tmp, &path).map_err(unavailable)?;
        #[cfg(unix)]
        std::fs::File::open(dir).and_then(|d| d.sync_all()).map_err(unavailable)?;
        let file = std::fs::OpenOptions::new().append(true).open(&path).map_err(unavailable)?;
        Ok(Self { path, file })
    }

    /// One whole line per write, then fsync.
    fn append(&mut self, grant: &Grant, fence: &str, request_expires: u64, bytes: u64) -> std::io::Result<()> {
        use std::io::Write;
        let mut line = serde_json::to_vec(&json!({"g": grant.grant_id, "e": grant.expires_at_unix_ms, "f": fence, "n": bytes, "x": request_expires}))?;
        line.push(b'\n');
        self.file.write_all(&line)?;
        self.file.sync_data()
    }
}

struct SourceState {
    /// Fence key -> the request's expiry (kept until it cannot be replayed).
    fence: BTreeMap<String, u64>,
    fence_capacity: usize,
    /// grant id -> (bytes served, first serve instant) for budget and pacing.
    grants: BTreeMap<String, (u64, Instant)>,
    /// object hex -> verified length.
    verified: BTreeMap<String, u64>,
    /// `None` once an append failed: everything is refused until rebind.
    journal: Option<Journal>,
    genesis_unix_ms: u64,
}

impl Default for SourceState {
    fn default() -> Self {
        Self { fence: BTreeMap::new(), fence_capacity: MAX_FENCE, grants: BTreeMap::new(), verified: BTreeMap::new(), journal: None, genesis_unix_ms: u64::MAX }
    }
}

impl SourceState {
    fn admit(&mut self, fence: String, request_expires: u64, grant_id: &str, bytes: u64, now: u64) {
        self.prune(now);
        self.fence.insert(fence, request_expires);
        let entry = self.grants.entry(grant_id.to_owned()).or_insert((0, Instant::now()));
        entry.0 += bytes;
    }

    /// The whole admission decision for one request, under the caller's lock:
    /// refuse, or journal durably and charge; returns the pacing delay (ms).
    fn admit_request(&mut self, grant: &Grant, fence_key: String, request_expires: u64, length: u64, now: u64) -> Result<u64, String> {
        if self.journal.is_none() {
            return Err("artifact_source_ledger_unavailable".into());
        }
        if grant.not_before_unix_ms < self.genesis_unix_ms {
            return Err("artifact_source_ledger_reset".into());
        }
        self.prune(now);
        if self.fence.contains_key(&fence_key) {
            return Err("artifact_chunk_request_replay".into());
        }
        if self.fence.len() >= self.fence_capacity {
            return Err("artifact_source_busy".into());
        }
        let (served, since) = *self.grants.entry(grant.grant_id.clone()).or_insert((0, Instant::now()));
        if served + length > grant.maximum_total_bytes {
            return Err("artifact_grant_budget_exhausted".into());
        }
        if self.journal.as_mut().is_some_and(|j| j.append(grant, &fence_key, request_expires, length).is_err()) {
            // Possibly a partial line on disk: poison the journal so no later
            // admission is glued onto it and lost at replay.
            self.journal = None;
            return Err("artifact_source_ledger_unavailable".into());
        }
        self.admit(fence_key, request_expires, &grant.grant_id, length, now);
        // Pacing: the grant's served bytes may not run ahead of its rate.
        let due_ms = (served + length).saturating_mul(1000) / grant.maximum_bytes_per_second;
        Ok(due_ms.saturating_sub(since.elapsed().as_millis() as u64))
    }

    /// Forget fence entries whose request can no longer be presented.
    fn prune(&mut self, now: u64) {
        self.fence.retain(|_, expires| *expires + ag::CLOCK_SKEW_MS >= now);
    }
}

/// Releases a peer's connection count when its connection ends.
struct PeerSlot {
    peers: Arc<Mutex<BTreeMap<EndpointId, usize>>>,
    id: EndpointId,
}

impl PeerSlot {
    fn enter(peers: &Arc<Mutex<BTreeMap<EndpointId, usize>>>, id: EndpointId, limit: usize) -> Option<Self> {
        let mut map = peers.lock().unwrap();
        let count = map.entry(id).or_insert(0);
        if *count >= limit {
            return None;
        }
        *count += 1;
        Some(Self { peers: peers.clone(), id })
    }
}

impl Drop for PeerSlot {
    fn drop(&mut self) {
        let mut map = self.peers.lock().unwrap();
        if let Some(count) = map.get_mut(&self.id) {
            *count -= 1;
            if *count == 0 {
                map.remove(&self.id);
            }
        }
    }
}

/// Cheap admission before any signature check or disk IO: the request names
/// a grant whose recipient is the transport-authenticated peer.
fn precheck(remote: EndpointId, raw: &[u8]) -> Result<(), String> {
    let env: Value = serde_json::from_slice(raw).map_err(|_| "artifact_chunk_request_invalid".to_owned())?;
    if env["message"]["grant"]["message"]["recipient_endpoint_id"].as_str() != Some(remote.to_string().as_str()) {
        return Err("artifact_recipient_transport_mismatch".into());
    }
    Ok(())
}

/// An artifact source serving content-addressed objects (`<root>/<sha256 hex>`).
#[derive(Clone)]
pub struct ArtifactSource {
    endpoint: Endpoint,
    signer: Arc<Identity>,
    seed_key: Arc<Value>,
    root: PathBuf,
    state: Arc<Mutex<SourceState>>,
    connections: Arc<tokio::sync::Semaphore>,
    peers: Arc<Mutex<BTreeMap<EndpointId, usize>>>,
    work: Arc<tokio::sync::Semaphore>,
    limits: ArtifactLimits,
}

fn refused(code: &str) -> Vec<u8> {
    frame_header(&json!({"protocol": RESPONSE_PROTOCOL, "status": "refused", "code": code}))
}

fn frame_header(header: &Value) -> Vec<u8> {
    let bytes = canonical::to_bytes(header).unwrap_or_default();
    let mut out = (bytes.len() as u32).to_be_bytes().to_vec();
    out.extend_from_slice(&bytes);
    out
}

impl ArtifactSource {
    /// Bind the source endpoint. Its signing identity is bound to its iroh
    /// endpoint id, which is what grants authorize.
    /// `state_dir` holds the durable admission journal (fence + budgets).
    pub async fn bind(secret: &[u8; 32], signing_secret: &[u8; 32], seed_key: Value, root: PathBuf, state_dir: &std::path::Path, reach: Reachability) -> Result<Self, ArtifactError> {
        let endpoint = bind(secret, reach, ARTIFACT_CHUNK_ALPN).await?;
        Self::with_endpoint(endpoint, signing_secret, seed_key, root, state_dir)
    }

    /// Over an endpoint someone else accepts on (the iroh sidecar): that
    /// endpoint must list [`ARTIFACT_CHUNK_ALPN`] and hand each such
    /// connection to [`serve_connection`](Self::serve_connection).
    pub fn with_endpoint(endpoint: Endpoint, signing_secret: &[u8; 32], seed_key: Value, root: PathBuf, state_dir: &std::path::Path) -> Result<Self, ArtifactError> {
        let mut state = SourceState::default();
        let journal = Journal::open(state_dir, &mut state, now_ms())?;
        state.journal = Some(journal);
        let signer = Identity::from_secret(&endpoint.id().to_string(), signing_secret).map_err(|_| ArtifactError("source_identity_invalid".into()))?;
        Ok(Self {
            endpoint,
            signer: Arc::new(signer),
            seed_key: Arc::new(seed_key),
            root,
            state: Arc::new(Mutex::new(state)),
            connections: Arc::new(tokio::sync::Semaphore::new(MAX_ARTIFACT_CONNECTIONS)),
            peers: Arc::default(),
            work: Arc::new(tokio::sync::Semaphore::new(MAX_BLOCKING_WORK)),
            limits: ArtifactLimits::default(),
        })
    }

    /// Replace the serving bounds (before serving starts).
    pub fn with_limits(mut self, limits: ArtifactLimits) -> Self {
        self.connections = Arc::new(tokio::sync::Semaphore::new(limits.connections.max(1)));
        self.limits = limits;
        self
    }

    /// Bound the live fence (default 65 536); at capacity new requests are refused.
    pub fn set_fence_capacity(&self, capacity: usize) {
        self.state.lock().unwrap().fence_capacity = capacity.max(1);
    }

    /// Where the admission journal lives (for operators and tests).
    pub fn journal_path(&self) -> Option<PathBuf> {
        self.state.lock().unwrap().journal.as_ref().map(|j| j.path.clone())
    }

    pub fn endpoint_id(&self) -> String {
        self.endpoint.id().to_string()
    }

    pub fn key_digest(&self) -> String {
        self.signer.verification_key_digest()
    }

    pub fn addr(&self) -> EndpointAddr {
        self.endpoint.addr()
    }

    pub async fn close(&self) {
        self.endpoint.close().await;
    }

    /// Length of a held object whose content matches its name (hashed once).
    fn object_size(&self, digest: &str) -> Option<u64> {
        let hex = digest.strip_prefix("sha256:")?;
        if let Some(n) = self.state.lock().unwrap().verified.get(hex) {
            return Some(*n);
        }
        let bytes = std::fs::read(self.root.join(hex)).ok()?;
        if hex::encode(Sha256::digest(&bytes)) != hex {
            return None; // A2: artifact_source_chunk_unverified -> not served
        }
        self.state.lock().unwrap().verified.insert(hex.to_owned(), bytes.len() as u64);
        Some(bytes.len() as u64)
    }

    /// Admit one request from `remote`; on success the receipt and the bytes.
    fn serve(&self, remote: EndpointId, raw: &[u8]) -> Result<(Value, Vec<u8>, u64), String> {
        let env: Value = serde_json::from_slice(raw).map_err(|_| "artifact_chunk_request_invalid".to_owned())?;
        let me = self.endpoint_id();
        let (request, grant) = ag::verify_chunk_request(&env, &self.seed_key, &me, &self.key_digest(), &|d| self.object_size(d), now_ms())
            .map_err(|e| e.0.to_owned())?;
        // The transport-authenticated peer must be the grant's recipient.
        if grant.recipient_endpoint_id != remote.to_string() {
            return Err("artifact_recipient_transport_mismatch".into());
        }
        // A2's fence key: sha256(grant_id \0 request_id \0 nonce).
        let fence_key = hex::encode(Sha256::digest(
            format!("{}\0{}\0{}", grant.grant_id, request.request_id, env["message"]["request_nonce"].as_str().unwrap_or("")).as_bytes(),
        ));
        let request_expires = env["message"]["expires_at_unix_ms"].as_u64().unwrap_or(u64::MAX);
        let now = now_ms();
        // Check, journal and charge under one lock: concurrent streams for the
        // same grant serialize here, and nothing is read or sent before the
        // admission is durable.
        let pace = self.state.lock().unwrap().admit_request(&grant, fence_key, request_expires, request.length_bytes, now)?;
        let hex = &request.object_digest[7..];
        let mut file = std::fs::File::open(self.root.join(hex)).map_err(|_| "artifact_object_unavailable".to_owned())?;
        use std::io::{Read, Seek, SeekFrom};
        let mut data = vec![0u8; request.length_bytes as usize];
        file.seek(SeekFrom::Start(request.offset_bytes))
            .and_then(|_| file.read_exact(&mut data))
            .map_err(|_| "artifact_object_unavailable".to_owned())?;
        let receipt = ag::sign_chunk_receipt(&self.signer, &request, &grant, &data, now_ms()).map_err(|e| e.0.to_owned())?;
        Ok((receipt, data, pace))
    }

    /// Accept loop (one task per connection, one stream per chunk).
    pub fn spawn_accept(&self) -> tokio::task::JoinHandle<()> {
        let source = self.clone();
        tokio::spawn(async move {
            while let Some(incoming) = source.endpoint.accept().await {
                let source = source.clone();
                tokio::spawn(async move {
                    if let Ok(connection) = incoming.await {
                        source.serve_connection(connection).await;
                    }
                });
            }
        })
    }

    /// Serve one accepted connection: one chunk per bidirectional stream,
    /// until the peer closes it. A connection negotiated for another ALPN is
    /// closed unserved.
    pub async fn serve_connection(&self, connection: iroh::endpoint::Connection) {
        let busy = |c: &iroh::endpoint::Connection| c.close(2u32.into(), b"busy");
        if connection.alpn() != ARTIFACT_CHUNK_ALPN {
            connection.close(1u32.into(), b"alpn");
            return;
        }
        // Bounded before anything is read: total, then per peer.
        let Ok(_slot) = self.connections.clone().try_acquire_owned() else { return busy(&connection) };
        let remote = connection.remote_id();
        let Some(_peer) = PeerSlot::enter(&self.peers, remote, self.limits.connections_per_peer) else { return busy(&connection) };
        // One request stream at a time per connection.
        connection.set_max_concurrent_bi_streams(1u32.into());
        loop {
            let Ok(Ok((mut send, mut recv))) = timeout(self.limits.idle_timeout, connection.accept_bi()).await else { break };
            // A slow or half-open request loses the whole connection.
            let Ok(Ok(raw)) = timeout(self.limits.read_timeout, recv.read_to_end(MAX_REQUEST_BYTES)).await else { break };
            let reply = match precheck(remote, &raw) {
                Err(code) => refused(&code),
                Ok(()) => match self.serve_blocking(remote, raw).await {
                    Ok((receipt, data, pace_ms)) => {
                        // No lock is held here; pacing is capped.
                        let pace = Duration::from_millis(pace_ms).min(MAX_PACE);
                        if !pace.is_zero() {
                            tokio::time::sleep(pace).await;
                        }
                        let mut out = frame_header(&json!({"protocol": RESPONSE_PROTOCOL, "status": "ok", "receipt": receipt}));
                        out.extend_from_slice(&data);
                        out
                    }
                    Err(code) => refused(&code),
                },
            };
            if timeout(IO_TIMEOUT, send.write_all(&reply)).await.is_err() {
                break;
            }
            let _ = send.finish();
        }
        connection.close(0u32.into(), b"done");
    }

    /// Verification, disk reads, hashing and the journal fsync run on the
    /// blocking pool, at most `MAX_BLOCKING_WORK` at once.
    async fn serve_blocking(&self, remote: EndpointId, raw: Vec<u8>) -> Result<(Value, Vec<u8>, u64), String> {
        let permit = self.work.clone().acquire_owned().await.map_err(|_| "artifact_source_closed".to_owned())?;
        let source = self.clone();
        tokio::task::spawn_blocking(move || {
            let _permit = permit;
            source.serve(remote, &raw)
        })
        .await
        .unwrap_or_else(|_| Err("artifact_source_internal".into()))
    }
}

/// The phone's [`ChunkSource`] over iroh: requests are signed with the member
/// identity, carry the seed grant, and every receipt is verified before the
/// bytes are handed to the store. Sources are tried in grant order; a source
/// that refuses or fails is skipped for the rest of the acquisition.
pub struct IrohChunkSource {
    endpoint: Endpoint,
    runtime: tokio::runtime::Handle,
    member: Arc<Identity>,
    grant: Grant,
    sources: Vec<EndpointAddr>,
    connections: BTreeMap<EndpointId, iroh::endpoint::Connection>,
    failed: BTreeSet<EndpointId>,
    pub requests: u64,
}

impl IrohChunkSource {
    /// `sources`: dialable addresses of the grant's authorized sources (with
    /// production reachability an endpoint id alone resolves via discovery).
    pub async fn bind(secret: &[u8; 32], member: Arc<Identity>, grant: Grant, sources: Vec<EndpointAddr>, reach: Reachability) -> Result<Self, ArtifactError> {
        Self::new(bind(secret, reach, ARTIFACT_CHUNK_ALPN).await?, member, grant, sources)
    }

    /// Over an existing endpoint (the node's own: its iroh id is what the
    /// grant names). Dialing needs no local ALPN registration. Must be called
    /// inside the tokio runtime the fetches will be driven on.
    pub fn new(endpoint: Endpoint, member: Arc<Identity>, grant: Grant, sources: Vec<EndpointAddr>) -> Result<Self, ArtifactError> {
        if sources.is_empty() || sources.iter().any(|a| !grant.sources.contains_key(&a.id.to_string())) {
            return Err(ArtifactError("artifact_source_not_authorized".into()));
        }
        Ok(Self { endpoint, runtime: tokio::runtime::Handle::current(), member, grant, sources, connections: BTreeMap::new(), failed: BTreeSet::new(), requests: 0 })
    }

    async fn fetch_from(&mut self, addr: EndpointAddr, digest: &str, offset: u64, len: u64) -> Result<Vec<u8>, ArtifactError> {
        let fail = |code: &str| ArtifactError(code.to_owned());
        let id = addr.id;
        let connection = match self.connections.get(&id) {
            Some(c) if c.close_reason().is_none() => c.clone(),
            _ => {
                let c = timeout(CONNECT_TIMEOUT, self.endpoint.connect(addr, ARTIFACT_CHUNK_ALPN))
                    .await
                    .map_err(|_| fail("artifact_source_connect_timeout"))?
                    .map_err(|_| fail("artifact_source_unreachable"))?;
                self.connections.insert(id, c.clone());
                c
            }
        };
        let mut nonce = [0u8; 12];
        getrandom::getrandom(&mut nonce).map_err(|_| fail("randomness_unavailable"))?;
        self.requests += 1;
        let request_id = format!("chunk-{}-{}", self.requests, hex::encode(&nonce[..4]));
        let env = ag::sign_chunk_request(&self.member, &self.grant, &request_id, &hex::encode(nonce), &id.to_string(), digest, offset, len, now_ms())
            .map_err(|e| fail(e.0))?;
        let (mut send, mut recv) = timeout(IO_TIMEOUT, connection.open_bi()).await.map_err(|_| fail("artifact_source_timeout"))?.map_err(|_| fail("artifact_source_unreachable"))?;
        let body = canonical::to_bytes(&env).map_err(|_| fail("artifact_chunk_request_invalid"))?;
        timeout(IO_TIMEOUT, send.write_all(&body)).await.map_err(|_| fail("artifact_source_timeout"))?.map_err(|_| fail("artifact_source_unreachable"))?;
        send.finish().map_err(|_| fail("artifact_source_unreachable"))?;
        let raw = timeout(IO_TIMEOUT, recv.read_to_end(4 + MAX_HEADER_BYTES + len as usize))
            .await
            .map_err(|_| fail("artifact_source_timeout"))?
            .map_err(|_| fail("artifact_source_response_invalid"))?;
        if raw.len() < 4 {
            return Err(fail("artifact_source_response_invalid"));
        }
        let header_len = u32::from_be_bytes([raw[0], raw[1], raw[2], raw[3]]) as usize;
        if header_len > MAX_HEADER_BYTES || raw.len() < 4 + header_len {
            return Err(fail("artifact_source_response_invalid"));
        }
        let header: Value = serde_json::from_slice(&raw[4..4 + header_len]).map_err(|_| fail("artifact_source_response_invalid"))?;
        if header["protocol"] != RESPONSE_PROTOCOL {
            return Err(fail("artifact_source_response_invalid"));
        }
        if header["status"] != "ok" {
            return Err(ArtifactError(format!("artifact_source_refused:{}", header["code"].as_str().unwrap_or("unknown"))));
        }
        let data = raw[4 + header_len..].to_vec();
        let request = ag::ChunkRequest { request_id, source_endpoint_id: id.to_string(), object_digest: digest.to_owned(), offset_bytes: offset, length_bytes: len };
        ag::verify_chunk_receipt(&header["receipt"], &request, &self.grant, &data).map_err(|e| fail(e.0))?;
        Ok(data)
    }
}

impl ChunkSource for IrohChunkSource {
    /// Blocking: call from a blocking context (`spawn_blocking`), never from an
    /// async task, because it drives the iroh I/O on the captured runtime.
    fn fetch(&mut self, sha256: &str, offset: u64, max_len: usize) -> Result<Vec<u8>, ArtifactError> {
        let len = (max_len as u64).min(self.grant.maximum_chunk_bytes);
        let mut last = ArtifactError("artifact_no_source_available".into());
        for addr in self.sources.clone() {
            if self.failed.contains(&addr.id) {
                continue;
            }
            let runtime = self.runtime.clone();
            match runtime.block_on(self.fetch_from(addr.clone(), sha256, offset, len)) {
                Ok(data) => return Ok(data),
                Err(e) => {
                    self.failed.insert(addr.id);
                    self.connections.remove(&addr.id);
                    last = e;
                }
            }
        }
        Err(last)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grant(not_before: u64) -> Grant {
        Grant {
            grant_id: "grant-1".into(),
            recipient_node_id: "node".into(),
            recipient_endpoint_id: "a".repeat(64),
            recipient_key_digest: format!("sha256:{}", "1".repeat(64)),
            assignment_id: "route-1".into(),
            manifest_pack_digest: format!("sha256:{}", "2".repeat(64)),
            allowed_object_digests: vec![],
            sources: BTreeMap::new(),
            source_addrs: BTreeMap::new(),
            maximum_total_bytes: 100,
            maximum_bytes_per_second: 1 << 30,
            maximum_chunk_bytes: 64,
            not_before_unix_ms: not_before,
            expires_at_unix_ms: not_before + 900_000,
            envelope: Value::Null,
        }
    }

    fn dir(tag: &str) -> PathBuf {
        let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        std::env::temp_dir().join(format!("mycelium-ledger-{tag}-{}-{n}", std::process::id()))
    }

    #[test]
    fn a_failed_append_poisons_the_journal_and_refuses_everything_after() {
        let d = dir("poison");
        let mut st = SourceState::default();
        st.journal = Some(Journal::open(&d, &mut st, 1_000).unwrap());
        let g = grant(1_000);
        assert!(st.admit_request(&g, "f1".into(), 60_000, 10, 2_000).is_ok());
        // The append handle can no longer write (as on a full or failing disk).
        let path = st.journal.as_ref().unwrap().path.clone();
        st.journal.as_mut().unwrap().file = std::fs::File::open(&path).unwrap();
        assert_eq!(st.admit_request(&g, "f2".into(), 60_000, 10, 2_000).unwrap_err(), "artifact_source_ledger_unavailable");
        assert!(st.journal.is_none());
        assert_eq!(st.admit_request(&g, "f3".into(), 60_000, 10, 2_000).unwrap_err(), "artifact_source_ledger_unavailable");
        assert!(!st.fence.contains_key("f2") && st.grants["grant-1"].0 == 10, "a refused request is neither fenced nor charged");
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn fence_entries_outlive_their_window_only_and_never_evict_live_ones() {
        let d = dir("fence");
        let mut st = SourceState::default();
        st.journal = Some(Journal::open(&d, &mut st, 1_000).unwrap());
        st.fence_capacity = 1;
        let g = grant(1_000);
        st.admit_request(&g, "f1".into(), 10_000, 1, 2_000).unwrap();
        assert_eq!(st.admit_request(&g, "f2".into(), 10_000, 1, 2_000).unwrap_err(), "artifact_source_busy");
        // Once f1's request window (+ skew) has passed it is pruned and a new
        // request fits; f1 itself can no longer be presented by then.
        st.admit_request(&g, "f2".into(), 200_000, 1, 10_000 + ag::CLOCK_SKEW_MS + 1).unwrap();
        assert!(!st.fence.contains_key("f1"));
        // A grant from before genesis is refused.
        assert_eq!(st.admit_request(&grant(999), "f9".into(), 200_000, 1, 2_000).unwrap_err(), "artifact_source_ledger_reset");
        std::fs::remove_dir_all(d).unwrap();
    }
}
