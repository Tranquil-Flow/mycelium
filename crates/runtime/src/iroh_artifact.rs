// SPDX-License-Identifier: AGPL-3.0-only
//! Artifact ALPN is distinct from Router and NodeControl. One canonical signed
//! request per stream, u32-BE JSON header followed by receipt-bound bytes.
//! The caller supplies reviewed native bounds; no unrelated equal-valued budget
//! is substituted for missing source-specific registry entries.
use budgets::{Bytes, MonoInstant, Span};
use iroh::{EndpointId, endpoint::Connection};
use protocol::artifact_grant::{ChunkRequest, Grant};
use transport::Clock;
#[derive(Clone, Copy, Debug)]
pub struct ArtifactWireLimits {
    pub request_bytes: Bytes,
    pub header_bytes: Bytes,
    pub io: Span,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArtifactError(pub String);
impl std::fmt::Display for ArtifactError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for ArtifactError {}
pub const RESPONSE_PROTOCOL: &str = "mycelium.mobile_artifact_chunk_response.v1";
/// Reuses one source connection within an acquisition. A failed source stays
/// excluded; this is not Router retry/reconnect and never replays a command.
pub struct ChunkSession<'a, C: Clock> {
    connection: Option<Connection>,
    runtime: &'a tokio::runtime::Runtime,
    clock: &'a C,
    local: EndpointId,
    limits: ArtifactWireLimits,
}
impl<'a, C: Clock> ChunkSession<'a, C> {
    pub fn new(
        connection: Connection,
        runtime: &'a tokio::runtime::Runtime,
        clock: &'a C,
        local: EndpointId,
        limits: ArtifactWireLimits,
    ) -> Result<Self, ArtifactError> {
        if connection.alpn() != transport::Channel::ArtifactChunk.alpn() {
            return Err(ArtifactError("artifact_source_alpn".into()));
        }
        Ok(Self {
            connection: Some(connection),
            runtime,
            clock,
            local,
            limits,
        })
    }
    pub fn fetch(
        &mut self,
        envelope: &serde_json::Value,
        request: &ChunkRequest,
        grant: &Grant,
        deadline: MonoInstant,
    ) -> Result<Vec<u8>, ArtifactError> {
        let result = self.fetch_once(envelope, request, grant, deadline);
        if result.is_err() {
            self.connection.take();
        }
        result
    }
    fn fetch_once(
        &self,
        envelope: &serde_json::Value,
        request: &ChunkRequest,
        grant: &Grant,
        deadline: MonoInstant,
    ) -> Result<Vec<u8>, ArtifactError> {
        use crate::iroh_host::outside_runtime;
        let fail = |s: &str| ArtifactError(s.into());
        outside_runtime().map_err(|_| fail("artifact_source_unreachable"))?;
        let connection = self
            .connection
            .as_ref()
            .ok_or_else(|| fail("artifact_no_source_available"))?;
        let peer = connection.remote_id().to_string();
        if grant.recipient_endpoint_id != self.local.to_string()
            || request.source_endpoint_id != peer
            || !grant.sources.contains_key(&peer)
        {
            return Err(fail("artifact_source_not_authorized"));
        }
        let body = protocol::canonical::to_bytes(envelope)
            .map_err(|_| fail("artifact_chunk_request_invalid"))?;
        if body.len() as u64 > self.limits.request_bytes.get() {
            return Err(fail("artifact_chunk_request_invalid"));
        }
        let maximum = usize::try_from(self.limits.header_bytes.get())
            .ok()
            .and_then(|n| n.checked_add(std::mem::size_of::<u32>()))
            .and_then(|n| {
                usize::try_from(request.length_bytes)
                    .ok()
                    .and_then(|b| n.checked_add(b))
            })
            .ok_or_else(|| fail("artifact_source_response_invalid"))?;
        let raw = self.runtime.block_on(async {
            let wait = || self.limits.wait(self.clock, deadline);
            let (mut send, mut recv) = tokio::time::timeout(wait()?, connection.open_bi())
                .await
                .map_err(|_| fail("artifact_source_timeout"))?
                .map_err(|_| fail("artifact_source_unreachable"))?;
            tokio::time::timeout(wait()?, send.write_all(&body))
                .await
                .map_err(|_| fail("artifact_source_timeout"))?
                .map_err(|_| fail("artifact_source_unreachable"))?;
            send.finish()
                .map_err(|_| fail("artifact_source_unreachable"))?;
            tokio::time::timeout(wait()?, recv.read_to_end(maximum))
                .await
                .map_err(|_| fail("artifact_source_timeout"))?
                .map_err(|_| fail("artifact_source_response_invalid"))
        })?;
        self.limits.wait(self.clock, deadline)?;
        let (header, data) = decode_response(&raw, self.limits.header_bytes)?;
        if header["status"] != "ok" {
            return Err(fail(&format!(
                "artifact_source_refused:{}",
                header["code"].as_str().unwrap_or("unknown")
            )));
        }
        protocol::artifact_grant::verify_chunk_receipt(&header["receipt"], request, grant, data)
            .map_err(|e| fail(e.0))?;
        Ok(data.to_vec())
    }
}
impl ArtifactWireLimits {
    pub(crate) fn wait(
        self,
        clock: &impl Clock,
        deadline: MonoInstant,
    ) -> Result<std::time::Duration, ArtifactError> {
        let left = crate::iroh_host::allowance(clock, deadline)
            .map_err(|_| ArtifactError("artifact_source_timeout".into()))?;
        Ok(left.min(std::time::Duration::from_millis(self.io.millis())))
    }
}
pub fn decode_response(
    raw: &[u8],
    maximum: Bytes,
) -> Result<(serde_json::Value, &[u8]), ArtifactError> {
    let invalid = || ArtifactError("artifact_source_response_invalid".into());
    let prefix = std::mem::size_of::<u32>();
    let length = u32::from_be_bytes(
        raw.get(..prefix)
            .ok_or_else(invalid)?
            .try_into()
            .map_err(|_| invalid())?,
    ) as usize;
    let end = prefix.checked_add(length).ok_or_else(invalid)?;
    if length as u64 > maximum.get() {
        return Err(invalid());
    }
    let header: serde_json::Value =
        serde_json::from_slice(raw.get(prefix..end).ok_or_else(invalid)?).map_err(|_| invalid())?;
    if header["protocol"] != RESPONSE_PROTOCOL {
        return Err(invalid());
    }
    Ok((header, &raw[end..]))
}
pub fn frame_response(
    header: &serde_json::Value,
    data: &[u8],
    maximum: Bytes,
) -> Result<Vec<u8>, ArtifactError> {
    let invalid = || ArtifactError("artifact_source_response_invalid".into());
    let bytes = protocol::canonical::to_bytes(header).map_err(|_| invalid())?;
    if bytes.len() as u64 > maximum.get() {
        return Err(invalid());
    }
    let length: u32 = bytes.len().try_into().map_err(|_| invalid())?;
    let mut out = length.to_be_bytes().to_vec();
    out.extend_from_slice(&bytes);
    out.extend_from_slice(data);
    Ok(out)
}
