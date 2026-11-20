// SPDX-License-Identifier: AGPL-3.0-only
//! Signed artifact service over the native artifact ALPN. Disk/journal ownership
//! is injected, not recreated as an in-memory replay approximation.
use super::iroh_artifact::{ArtifactError, ArtifactWireLimits};
use budgets::{Entries, Span};
use iroh::endpoint::Connection;
use protocol::{
    artifact_grant::{ChunkRequest, Grant, GrantLimits},
    identity::Identity,
};
use std::sync::Arc;

pub struct AdmittedChunk {
    pub bytes: Vec<u8>,
    pub pace: Span,
}
/// Implement with the source's content-verified object store and durable journal.
/// Admission MUST atomically check/fence/charge and fsync BEFORE reading bytes.
/// Read/send failures remain charged; live entries never evict at capacity.
pub trait ArtifactObjects: Send + Sync + 'static {
    fn verified_size(&self, digest: &str) -> Option<u64>;
    fn admit_and_read(
        &self,
        grant: &Grant,
        request: &ChunkRequest,
        nonce: &str,
        request_expiry: u64,
        now: u64,
    ) -> Result<AdmittedChunk, ArtifactError>;
}
#[derive(Clone, Copy)]
pub struct ArtifactServeLimits {
    pub wire: ArtifactWireLimits,
    pub connections: Entries,
    pub per_peer: Entries,
    pub blocking: Entries,
    pub read: Span,
    pub idle: Span,
    pub maximum_pace: Span,
}
pub struct ArtifactService<S: ArtifactObjects> {
    signer: Arc<Identity>,
    seed: Arc<serde_json::Value>,
    grant_limits: GrantLimits,
    objects: Arc<S>,
    limits: ArtifactServeLimits,
    now: Arc<dyn Fn() -> u64 + Send + Sync>,
    connections: Arc<tokio::sync::Semaphore>,
    work: Arc<tokio::sync::Semaphore>,
    peers: Arc<std::sync::Mutex<std::collections::BTreeMap<iroh::EndpointId, usize>>>,
}
impl<S: ArtifactObjects> ArtifactService<S> {
    pub fn new(
        signer: Identity,
        seed: serde_json::Value,
        grant_limits: GrantLimits,
        objects: Arc<S>,
        limits: ArtifactServeLimits,
        now: Arc<dyn Fn() -> u64 + Send + Sync>,
    ) -> Result<Self, ArtifactError> {
        let connections = capacity(limits.connections)?;
        let blocking = capacity(limits.blocking)?;
        capacity(limits.per_peer)?;
        Ok(Self {
            signer: Arc::new(signer),
            seed: Arc::new(seed),
            grant_limits,
            objects,
            limits,
            now,
            connections: Arc::new(tokio::sync::Semaphore::new(connections)),
            work: Arc::new(tokio::sync::Semaphore::new(blocking)),
            peers: Default::default(),
        })
    }
    pub async fn serve_connection(&self, connection: Connection) -> Result<(), ArtifactError> {
        if connection.alpn() != transport::Channel::ArtifactChunk.alpn() {
            connection.close(1u32.into(), b"wrong alpn");
            return Err(ArtifactError("artifact_source_alpn".into()));
        }
        let Ok(_connection) = self.connections.clone().try_acquire_owned() else {
            connection.close(2u32.into(), b"source busy");
            return Err(ArtifactError("artifact_source_busy".into()));
        };
        let Some(_peer) = PeerSlot::enter(
            &self.peers,
            connection.remote_id(),
            self.limits.per_peer.get() as usize,
        ) else {
            connection.close(2u32.into(), b"source busy");
            return Err(ArtifactError("artifact_source_busy".into()));
        };
        connection.set_max_concurrent_bi_streams(1u32.into());
        let outcome = self.serve_streams(&connection).await;
        connection.close(0u32.into(), b"artifact source closed");
        outcome
    }
    async fn serve_streams(&self, connection: &Connection) -> Result<(), ArtifactError> {
        use crate::iroh_artifact::{RESPONSE_PROTOCOL, frame_response};
        use std::time::Duration;
        use tokio::time::timeout;
        loop {
            let (mut send, mut recv) = match timeout(
                Duration::from_millis(self.limits.idle.millis()),
                connection.accept_bi(),
            )
            .await
            {
                Ok(Ok(streams)) => streams,
                _ => return Ok(()),
            };
            let raw = match timeout(
                Duration::from_millis(self.limits.read.millis()),
                recv.read_to_end(self.limits.wire.request_bytes.get() as usize),
            )
            .await
            {
                Ok(Ok(raw)) => raw,
                _ => return Ok(()),
            };
            let serving = std::time::Instant::now();
            let precheck = precheck(connection.remote_id(), &raw);
            let (header, data, pace) = match precheck {
                Err(error) => (
                    serde_json::json!({"protocol":RESPONSE_PROTOCOL,"status":"refused","code":error.0}),
                    Vec::new(),
                    Span::ZERO,
                ),
                Ok(envelope) => match self.work.clone().try_acquire_owned() {
                    Err(_) => (
                        serde_json::json!({"protocol":RESPONSE_PROTOCOL,"status":"refused","code":"artifact_source_busy"}),
                        Vec::new(),
                        Span::ZERO,
                    ),
                    Ok(permit) => {
                        let signer = self.signer.clone();
                        let seed = self.seed.clone();
                        let limits = self.grant_limits;
                        let objects = self.objects.clone();
                        let now = self.now.clone();
                        let result = tokio::task::spawn_blocking(move || {
                            // The permit stays with non-cancellable blocking work,
                            // even when this connection task is cancelled.
                            let _permit = permit;
                            let (request, grant) = protocol::artifact_grant::verify_chunk_request(
                                &envelope,
                                &seed,
                                signer.endpoint_id(),
                                &signer.verification_key_digest(),
                                &|digest| objects.verified_size(digest),
                                now(),
                                limits,
                            )
                            .map_err(|e| ArtifactError(e.0.into()))?;
                            let message = &envelope["message"];
                            let nonce = message["request_nonce"].as_str().ok_or_else(|| {
                                ArtifactError("artifact_chunk_request_invalid".into())
                            })?;
                            let expiry =
                                message["expires_at_unix_ms"].as_u64().ok_or_else(|| {
                                    ArtifactError("artifact_chunk_request_invalid".into())
                                })?;
                            let admitted =
                                objects.admit_and_read(&grant, &request, nonce, expiry, now())?;
                            let receipt = protocol::artifact_grant::sign_chunk_receipt(
                                &signer,
                                &request,
                                &grant,
                                &admitted.bytes,
                                now(),
                            )
                            .map_err(|e| ArtifactError(e.0.into()))?;
                            Ok::<_, ArtifactError>((receipt, admitted))
                        })
                        .await;
                        match result {
                            Ok(Ok((receipt, admitted))) => (
                                serde_json::json!({"protocol":RESPONSE_PROTOCOL,"status":"ok","receipt":receipt}),
                                admitted.bytes,
                                admitted.pace,
                            ),
                            Ok(Err(error)) => (
                                serde_json::json!({"protocol":RESPONSE_PROTOCOL,"status":"refused","code":error.0}),
                                Vec::new(),
                                Span::ZERO,
                            ),
                            Err(_) => (
                                serde_json::json!({"protocol":RESPONSE_PROTOCOL,"status":"refused","code":"artifact_source_internal_error"}),
                                Vec::new(),
                                Span::ZERO,
                            ),
                        }
                    }
                },
            };
            let pace = pace.millis().min(self.limits.maximum_pace.millis());
            let prepared = serving.elapsed();
            if pace > 0 {
                tokio::time::sleep(Duration::from_millis(pace)).await;
            }
            let response = frame_response(&header, &data, self.limits.wire.header_bytes)?;
            if !matches!(
                timeout(
                    Duration::from_millis(self.limits.wire.io.millis()),
                    send.write_all(&response)
                )
                .await,
                Ok(Ok(()))
            ) {
                return Ok(());
            }
            if send.finish().is_err() {
                // Baseline continues accepting streams after FIN failure. Keep
                // that behavior, but expose the failure without payload data.
                eprintln!("artifact_source_stream_finish_failed");
            }
            // Lane XC diagnostic: verify + read + sign, pacing, and the write into the stream.
            eprintln!(
                "artifact_source_chunk bytes={} prepare_ms={} pace_ms={pace} total_ms={}",
                data.len(),
                prepared.as_millis(),
                serving.elapsed().as_millis()
            );
        }
    }
}
fn capacity(value: Entries) -> Result<usize, ArtifactError> {
    let n = usize::try_from(value.get())
        .map_err(|_| ArtifactError("artifact_source_limits_invalid".into()))?;
    if n == 0 || n > tokio::sync::Semaphore::MAX_PERMITS {
        return Err(ArtifactError("artifact_source_limits_invalid".into()));
    }
    Ok(n)
}
fn precheck(remote: iroh::EndpointId, raw: &[u8]) -> Result<serde_json::Value, ArtifactError> {
    let envelope: serde_json::Value = serde_json::from_slice(raw)
        .map_err(|_| ArtifactError("artifact_chunk_request_invalid".into()))?;
    if envelope["message"]["grant"]["message"]["recipient_endpoint_id"].as_str()
        != Some(remote.to_string().as_str())
    {
        return Err(ArtifactError(
            "artifact_recipient_transport_mismatch".into(),
        ));
    }
    Ok(envelope)
}
struct PeerSlot {
    peers: Arc<std::sync::Mutex<std::collections::BTreeMap<iroh::EndpointId, usize>>>,
    id: iroh::EndpointId,
}
impl PeerSlot {
    fn enter(
        peers: &Arc<std::sync::Mutex<std::collections::BTreeMap<iroh::EndpointId, usize>>>,
        id: iroh::EndpointId,
        limit: usize,
    ) -> Option<Self> {
        let mut map = peers.lock().unwrap();
        let count = map.entry(id).or_insert(0);
        if *count >= limit {
            return None;
        }
        *count += 1;
        Some(Self {
            peers: peers.clone(),
            id,
        })
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

impl<S: ArtifactObjects> crate::iroh_pool::ConnectionService for ArtifactService<S> {
    fn serve(
        self: Arc<Self>,
        connection: Connection,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>> {
        Box::pin(async move {
            // The service closes the connection itself; its refusal is already on the
            // wire as a close code, so there is nothing further for the accept loop.
            if self.serve_connection(connection).await.is_err() {
                // Refused or ended by the peer: recorded by the close code.
            }
        })
    }
}
