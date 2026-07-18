// SPDX-License-Identifier: AGPL-3.0-or-later
//! Small, strict framing carried by one authenticated iroh bidirectional stream.

use std::error::Error;
use std::fmt;

pub const REMOTE_PROTOCOL_VERSION: u8 = 1;
pub const REMOTE_GENERATION_BYTES: usize = 8;
pub const REMOTE_OPERATIONAL_FRAME_BYTES: usize = 16 * 1024 * 1024;
pub const REMOTE_MAX_PAYLOAD_BYTES: usize =
    REMOTE_GENERATION_BYTES + REMOTE_OPERATIONAL_FRAME_BYTES;
pub const REMOTE_HEADER_BYTES: usize = 1 + 1 + 16 + 4;
pub const REMOTE_MAX_FRAME_BYTES: usize = REMOTE_HEADER_BYTES + REMOTE_MAX_PAYLOAD_BYTES;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum RemoteKind {
    Transfer = 1,
    Ack = 2,
    Error = 3,
}

impl TryFrom<u8> for RemoteKind {
    type Error = RemoteProtocolError;

    fn try_from(value: u8) -> Result<Self, RemoteProtocolError> {
        match value {
            1 => Ok(Self::Transfer),
            2 => Ok(Self::Ack),
            3 => Ok(Self::Error),
            _ => Err(RemoteProtocolError::UnknownKind),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RemoteFrame {
    pub kind: RemoteKind,
    pub message_id: [u8; 16],
    pub payload: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RemoteProtocolError {
    PayloadTooLarge,
    FrameTooShort,
    LengthMismatch,
    UnknownVersion,
    UnknownKind,
}

impl RemoteProtocolError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::PayloadTooLarge => "payload_too_large",
            Self::FrameTooShort => "frame_too_short",
            Self::LengthMismatch => "length_mismatch",
            Self::UnknownVersion => "unknown_version",
            Self::UnknownKind => "unknown_kind",
        }
    }
}

impl fmt::Display for RemoteProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl Error for RemoteProtocolError {}

pub fn encode_remote_frame(
    kind: RemoteKind,
    message_id: [u8; 16],
    payload: &[u8],
) -> Result<Vec<u8>, RemoteProtocolError> {
    if payload.len() > REMOTE_MAX_PAYLOAD_BYTES {
        return Err(RemoteProtocolError::PayloadTooLarge);
    }
    let mut encoded = Vec::with_capacity(REMOTE_HEADER_BYTES + payload.len());
    encoded.push(REMOTE_PROTOCOL_VERSION);
    encoded.push(kind as u8);
    encoded.extend_from_slice(&message_id);
    encoded.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    encoded.extend_from_slice(payload);
    Ok(encoded)
}

pub fn decode_remote_frame(encoded: &[u8]) -> Result<RemoteFrame, RemoteProtocolError> {
    if encoded.len() < REMOTE_HEADER_BYTES {
        return Err(RemoteProtocolError::FrameTooShort);
    }
    if encoded.len() > REMOTE_MAX_FRAME_BYTES {
        return Err(RemoteProtocolError::PayloadTooLarge);
    }
    if encoded[0] != REMOTE_PROTOCOL_VERSION {
        return Err(RemoteProtocolError::UnknownVersion);
    }
    let kind = RemoteKind::try_from(encoded[1])?;
    let message_id = encoded[2..18]
        .try_into()
        .map_err(|_| RemoteProtocolError::FrameTooShort)?;
    let payload_len = u32::from_be_bytes(
        encoded[18..22]
            .try_into()
            .map_err(|_| RemoteProtocolError::FrameTooShort)?,
    ) as usize;
    if payload_len > REMOTE_MAX_PAYLOAD_BYTES {
        return Err(RemoteProtocolError::PayloadTooLarge);
    }
    if encoded.len() != REMOTE_HEADER_BYTES + payload_len {
        return Err(RemoteProtocolError::LengthMismatch);
    }
    Ok(RemoteFrame {
        kind,
        message_id,
        payload: encoded[REMOTE_HEADER_BYTES..].to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_remote_frame_round_trip() {
        let encoded = encode_remote_frame(RemoteKind::Transfer, [4; 16], b"frame").unwrap();
        let decoded = decode_remote_frame(&encoded).unwrap();
        assert_eq!(decoded.kind, RemoteKind::Transfer);
        assert_eq!(decoded.message_id, [4; 16]);
        assert_eq!(decoded.payload, b"frame");
        let mut trailing = encoded;
        trailing.push(0);
        assert_eq!(
            decode_remote_frame(&trailing),
            Err(RemoteProtocolError::LengthMismatch)
        );
    }
}
