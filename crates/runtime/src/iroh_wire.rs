// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned sidecar Router carrier bytes (not the NodeControl/artifact protocols).
//! native/iroh_transport/src/sidecar.rs:2644-2699 @ f4e366e9.
use protocol::remote::{self, RemoteKind};
use transport::{Channel, Error, Exchange};
pub fn transfer(request: &Exchange) -> Result<Vec<u8>, Error> {
    if request.channel != Channel::Router {
        return Err(Error::Channel);
    }
    let size = request
        .payload
        .len()
        .checked_add(remote::REMOTE_GENERATION_BYTES)
        .ok_or(Error::FrameTooLarge)?;
    if size > remote::REMOTE_MAX_PAYLOAD_BYTES {
        return Err(Error::FrameTooLarge);
    }
    let mut payload = Vec::with_capacity(size);
    payload.extend_from_slice(&request.peer.local_generation.to_be_bytes());
    payload.extend_from_slice(&request.payload);
    remote::encode_remote_frame(RemoteKind::Transfer, request.message_id, &payload)
        .map_err(|_| Error::FrameTooLarge)
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SendAttempt {
    Delivered,
    PeerRotated,
    ReplayCollision,
    Retry { evict_connection: bool },
}
/// Source send-attempt classification, distinct from retry scheduling.
pub fn classify_response(bytes: &[u8], id: [u8; 16]) -> SendAttempt {
    let Ok(frame) = remote::decode_remote_frame(bytes) else {
        return SendAttempt::Retry {
            evict_connection: true,
        };
    };
    if frame.message_id != id {
        return SendAttempt::Retry {
            evict_connection: true,
        };
    }
    if frame.kind == RemoteKind::Ack && frame.payload.is_empty() {
        SendAttempt::Delivered
    } else if frame.kind == RemoteKind::Error && frame.payload == b"peer_rotated" {
        SendAttempt::PeerRotated
    } else if frame.kind == RemoteKind::Error && frame.payload == b"replay_collision" {
        SendAttempt::ReplayCollision
    } else {
        SendAttempt::Retry {
            evict_connection: false,
        }
    }
}
pub fn acknowledgement(bytes: &[u8], request: &Exchange) -> Result<(), Error> {
    let frame = remote::decode_remote_frame(bytes).map_err(|_| Error::Unavailable)?;
    if frame.message_id != request.message_id {
        return Err(Error::Correlation);
    }
    match classify_response(bytes, request.message_id) {
        SendAttempt::Delivered => Ok(()),
        SendAttempt::PeerRotated => Err(Error::Generation),
        SendAttempt::ReplayCollision | SendAttempt::Retry { .. } => Err(Error::Unavailable),
    }
}
