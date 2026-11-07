// SPDX-License-Identifier: AGPL-3.0-only
//! Synchronous deterministic transport seam. Concrete Iroh/Tokio ownership is L9.
//! Authenticated identity is supplied by the adapter, never by a payload claim.
use budgets::MonoInstant;

/// Frozen, independently recorded wire inputs for simulated and real carrier tests.
/// Records describe the native Router ALPN only, not NodeControl/ArtifactChunk.
/// See the TSV header and provenance for the distinction between codec verdicts
/// and the source sidecar's send-attempt state; neither implies QUIC qualification.
pub mod qualification {
    pub const ROUTER_NATIVE_CORPUS_TSV: &str = include_str!("../fixtures/router-native.tsv");
    pub const ROUTER_NATIVE_PROVENANCE_JSON: &str =
        include_str!("../fixtures/router-native-provenance.json");
}

pub const ROUTER_ALPN: &[u8] = b"mycelium.iroh.sidecar.v1";
pub const NODE_CONTROL_ALPN: &[u8] = b"mycelium.node_control.v1";
pub use protocol::artifact_grant::ARTIFACT_CHUNK_ALPN;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ConnectionFence {
    pub endpoint_id: [u8; 32],
    pub local_generation: u64,
    pub remote_generation: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Channel {
    Router,
    NodeControl,
    ArtifactChunk,
}
impl Channel {
    pub fn alpn(self) -> &'static [u8] {
        match self {
            Self::Router => ROUTER_ALPN,
            Self::NodeControl => NODE_CONTROL_ALPN,
            Self::ArtifactChunk => ARTIFACT_CHUNK_ALPN,
        }
    }
}
#[derive(Clone, Debug)]
pub struct Exchange {
    pub peer: ConnectionFence,
    pub channel: Channel,
    pub message_id: [u8; 16],
    pub payload: Vec<u8>,
    pub deadline: MonoInstant,
    pub maximum_response_bytes: usize,
}
#[derive(Clone, Debug)]
pub struct Response {
    pub authenticated_peer: ConnectionFence,
    pub channel: Channel,
    pub message_id: [u8; 16],
    pub payload: Vec<u8>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Authentication,
    Generation,
    Channel,
    Correlation,
    FrameTooLarge,
    Expired,
    ClockDomain,
    Cancelled,
    Unavailable,
    CleanupUnproven,
}

/// Fake and real adapters must implement the same bounded exchange/cancel/close
/// contract. Exchange reports completion (not merely queued work); cancel/close
/// may not return success while ownership/cleanup remains unproven.
pub trait Transport {
    fn exchange(&mut self, request: &Exchange) -> Result<Response, Error>;
    fn cancel(&mut self, message_id: [u8; 16], deadline: MonoInstant) -> Result<(), Error>;
    fn close(&mut self, deadline: MonoInstant) -> Result<(), Error>;
}

pub trait Clock {
    fn monotonic(&self) -> MonoInstant;
    fn wall_seconds(&self) -> f64;
}

pub fn exchange<T: Transport>(
    transport: &mut T,
    request: &Exchange,
    clock: &impl Clock,
) -> Result<Vec<u8>, Error> {
    check_deadline(request.deadline, clock.monotonic())?;
    let response = transport.exchange(request)?;
    check_deadline(request.deadline, clock.monotonic())?;
    if response.authenticated_peer.endpoint_id != request.peer.endpoint_id {
        return Err(Error::Authentication);
    }
    if response.authenticated_peer.local_generation != request.peer.local_generation
        || response.authenticated_peer.remote_generation != request.peer.remote_generation
    {
        return Err(Error::Generation);
    }
    if response.channel != request.channel {
        return Err(Error::Channel);
    }
    if response.message_id != request.message_id {
        return Err(Error::Correlation);
    }
    if response.payload.len() > request.maximum_response_bytes {
        return Err(Error::FrameTooLarge);
    }
    Ok(response.payload)
}

/// Cancel completion is still owned by the adapter; timeout is never success.
pub fn cancel<T: Transport>(
    transport: &mut T,
    message_id: [u8; 16],
    deadline: MonoInstant,
    clock: &impl Clock,
) -> Result<(), Error> {
    check_deadline(deadline, clock.monotonic())?;
    transport.cancel(message_id, deadline)?;
    check_deadline(deadline, clock.monotonic())
}
/// Close must prove cleanup, not merely enqueue it.
pub fn close<T: Transport>(
    transport: &mut T,
    deadline: MonoInstant,
    clock: &impl Clock,
) -> Result<(), Error> {
    check_deadline(deadline, clock.monotonic())?;
    transport.close(deadline)?;
    check_deadline(deadline, clock.monotonic())
}

fn check_deadline(deadline: MonoInstant, now: MonoInstant) -> Result<(), Error> {
    deadline.same_domain(now).map_err(|_| Error::ClockDomain)?;
    if now.ticks() >= deadline.ticks() {
        Err(Error::Expired)
    } else {
        Ok(())
    }
}
