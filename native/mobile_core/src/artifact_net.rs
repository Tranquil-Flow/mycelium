// SPDX-License-Identifier: AGPL-3.0-or-later
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
//! the bytes it serves. The replay fence and per-grant byte budget are durable
//! (A2 keeps both durably): every admission is appended and fsynced to
//! `<state>/source-ledger.jsonl` before any byte is sent, replayed at bind,
//! and compacted to the grants still inside their window (+ skew). The phone ([`IrohChunkSource`]) checks every receipt
//! (`verify_chunk_receipt`) before the bytes reach the artifact store, which
//! then digest-verifies whole objects before promotion.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
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
const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// Replay fence size per source (oldest forgotten first; a forgotten id is
/// also past its 2-minute request window long before).
const MAX_FENCE: usize = 65_536;

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

/// The durable admission journal: one JSON line per admitted request.
struct Journal {
    path: PathBuf,
    file: std::fs::File,
}

impl Journal {
    /// Replay `<dir>/source-ledger.jsonl` into `state`, keeping the entries of
    /// grants not yet expired, and rewrite it compacted (tmp, fsync, rename).
    fn open(dir: &std::path::Path, state: &mut SourceState, now: u64) -> Result<Self, ArtifactError> {
        use std::io::Write;
        let io = |_| ArtifactError("artifact_source_ledger_unavailable".into());
        std::fs::create_dir_all(dir).map_err(io)?;
        let path = dir.join("source-ledger.jsonl");
        let mut kept = Vec::new();
        if let Ok(raw) = std::fs::read_to_string(&path) {
            for line in raw.lines() {
                // A torn final line (crash mid-append) is not an admission.
                let Ok(v) = serde_json::from_str::<Value>(line) else { continue };
                let (Some(g), Some(e), Some(f), Some(n)) = (v["g"].as_str(), v["e"].as_u64(), v["f"].as_str(), v["n"].as_u64()) else { continue };
                if e + ag::CLOCK_SKEW_MS < now {
                    continue;
                }
                state.admit(f.to_owned(), g, n);
                kept.push(line.to_owned());
            }
        }
        let tmp = dir.join("source-ledger.jsonl.tmp");
        let mut f = std::fs::File::create(&tmp).map_err(io)?;
        for line in &kept {
            writeln!(f, "{line}").map_err(io)?;
        }
        f.sync_all().map_err(io)?;
        std::fs::rename(&tmp, &path).map_err(io)?;
        #[cfg(unix)]
        std::fs::File::open(dir).and_then(|d| d.sync_all()).map_err(io)?;
        let file = std::fs::OpenOptions::new().append(true).open(&path).map_err(io)?;
        Ok(Self { path, file })
    }

    fn append(&mut self, grant: &Grant, fence: &str, bytes: u64) -> Result<(), ()> {
        use std::io::Write;
        let line = json!({"g": grant.grant_id, "e": grant.expires_at_unix_ms, "f": fence, "n": bytes});
        writeln!(self.file, "{line}").map_err(|_| ())?;
        self.file.sync_data().map_err(|_| ())
    }
}

#[derive(Default)]
struct SourceState {
    fence: BTreeSet<String>,
    fence_order: VecDeque<String>,
    /// grant id -> (bytes served, first serve instant) for budget and pacing.
    grants: BTreeMap<String, (u64, Instant)>,
    /// object hex -> verified length.
    verified: BTreeMap<String, u64>,
    journal: Option<Journal>,
}

impl SourceState {
    fn admit(&mut self, fence: String, grant_id: &str, bytes: u64) {
        if self.fence.insert(fence.clone()) {
            self.fence_order.push_back(fence);
        }
        while self.fence_order.len() > MAX_FENCE {
            if let Some(old) = self.fence_order.pop_front() {
                self.fence.remove(&old);
            }
        }
        let entry = self.grants.entry(grant_id.to_owned()).or_insert((0, Instant::now()));
        entry.0 += bytes;
    }
}

/// An artifact source serving content-addressed objects (`<root>/<sha256 hex>`).
#[derive(Clone)]
pub struct ArtifactSource {
    endpoint: Endpoint,
    signer: Arc<Identity>,
    seed_key: Arc<Value>,
    root: PathBuf,
    state: Arc<Mutex<SourceState>>,
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
        let mut state = SourceState::default();
        let journal = Journal::open(state_dir, &mut state, now_ms())?;
        state.journal = Some(journal);
        let endpoint = bind(secret, reach, ARTIFACT_CHUNK_ALPN).await?;
        let signer = Identity::from_secret(&endpoint.id().to_string(), signing_secret).map_err(|_| ArtifactError("source_identity_invalid".into()))?;
        Ok(Self { endpoint, signer: Arc::new(signer), seed_key: Arc::new(seed_key), root, state: Arc::new(Mutex::new(state)) })
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
        let pace = {
            let mut st = self.state.lock().unwrap();
            if st.fence.contains(&fence_key) {
                return Err("artifact_chunk_request_replay".into());
            }
            let (served, since) = *st.grants.entry(grant.grant_id.clone()).or_insert((0, Instant::now()));
            if served + request.length_bytes > grant.maximum_total_bytes {
                return Err("artifact_grant_budget_exhausted".into());
            }
            // Durable before any byte leaves: a crash cannot reopen the fence
            // or refund the budget.
            let journaled = st.journal.as_mut().is_some_and(|j| j.append(&grant, &fence_key, request.length_bytes).is_ok());
            if !journaled {
                return Err("artifact_source_ledger_unavailable".into());
            }
            st.admit(fence_key, &grant.grant_id, request.length_bytes);
            // Pacing: the grant's served bytes may not run ahead of its rate.
            let due_ms = (served + request.length_bytes).saturating_mul(1000) / grant.maximum_bytes_per_second;
            due_ms.saturating_sub(since.elapsed().as_millis() as u64)
        };
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
                    let Ok(connection) = incoming.await else { return };
                    let remote = connection.remote_id();
                    while let Ok((mut send, mut recv)) = connection.accept_bi().await {
                        let Ok(Ok(raw)) = timeout(IO_TIMEOUT, recv.read_to_end(MAX_REQUEST_BYTES)).await else { continue };
                        let reply = match source.serve(remote, &raw) {
                            Ok((receipt, data, pace_ms)) => {
                                if pace_ms > 0 {
                                    tokio::time::sleep(Duration::from_millis(pace_ms)).await;
                                }
                                let mut out = frame_header(&json!({"protocol": RESPONSE_PROTOCOL, "status": "ok", "receipt": receipt}));
                                out.extend_from_slice(&data);
                                out
                            }
                            Err(code) => refused(&code),
                        };
                        let _ = timeout(IO_TIMEOUT, send.write_all(&reply)).await;
                        let _ = send.finish();
                    }
                });
            }
        })
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
