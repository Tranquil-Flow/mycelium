// SPDX-License-Identifier: AGPL-3.0-only
//! Router wire framing (`mycelium_router/wire.py`), MYAC/MYTK activation payloads
//! (`mycelium_router/payloads.py`) and the iroh remote frame
//! (`native/iroh_transport/src/remote.rs`), re-implemented without I/O so the
//! same codecs serve Android, iOS and the browser.

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::canonical;

pub const ROUTER_WIRE_PROTOCOL: &str = "mycelium.router_wire.v1";
pub const MAX_HEADER_BYTES: usize = 1024 * 1024;
pub const MAX_PAYLOAD_BYTES: usize = 256 * 1024 * 1024;
pub const MESSAGE_TYPES: [&str; 11] = [
    "HopHeader",
    "ManifestDelta",
    "ManifestLocked",
    "PathCancellation",
    "ProgressivePrefillMessage",
    "PrefillChunkCompleted",
    "ReservationRequest",
    "ReservationResult",
    "ReservationCommitResult",
    "TokenEvent",
    "FailureReport",
];

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct WireError(pub &'static str);

#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub message_type: String,
    pub body: Value,
    pub payload: Vec<u8>,
}

/// `encode_frame`: u32 BE header length + ASCII canonical JSON header + payload.
pub fn encode_frame(message_type: &str, body: &Value, payload: &[u8]) -> Result<Vec<u8>, WireError> {
    if !MESSAGE_TYPES.contains(&message_type) {
        return Err(WireError("unknown_message_type"));
    }
    if payload.len() > MAX_PAYLOAD_BYTES {
        return Err(WireError("payload_too_large"));
    }
    let header = canonical::to_ascii_bytes(&json!({
        "protocol": ROUTER_WIRE_PROTOCOL,
        "message_type": message_type,
        "body": body,
        "payload_length": payload.len(),
        "payload_sha256": hex::encode(Sha256::digest(payload)),
    }))
    .map_err(|_| WireError("invalid_wire_message"))?;
    if header.len() > MAX_HEADER_BYTES {
        return Err(WireError("header_too_large"));
    }
    let mut out = Vec::with_capacity(4 + header.len() + payload.len());
    out.extend_from_slice(&(header.len() as u32).to_be_bytes());
    out.extend_from_slice(&header);
    out.extend_from_slice(payload);
    Ok(out)
}

/// `decode_frame` (framing and integrity; per-type body validation stays with
/// the Router that owns each message contract).
pub fn decode_frame(frame: &[u8]) -> Result<Frame, WireError> {
    if frame.len() < 4 {
        return Err(WireError("truncated_frame"));
    }
    let header_len = u32::from_be_bytes(frame[..4].try_into().unwrap()) as usize;
    if header_len > MAX_HEADER_BYTES {
        return Err(WireError("header_too_large"));
    }
    let end = 4 + header_len;
    if frame.len() < end {
        return Err(WireError("truncated_frame"));
    }
    let header: Value =
        serde_json::from_slice(&frame[4..end]).map_err(|_| WireError("invalid_wire_header"))?;
    let map = header.as_object().ok_or(WireError("invalid_wire_header"))?;
    let fields = ["protocol", "message_type", "body", "payload_length", "payload_sha256"];
    if map.len() != fields.len() || !fields.iter().all(|f| map.contains_key(*f)) {
        return Err(WireError("invalid_wire_header"));
    }
    if map["protocol"] != ROUTER_WIRE_PROTOCOL {
        return Err(WireError("unknown_wire_protocol"));
    }
    let message_type = map["message_type"].as_str().ok_or(WireError("unknown_message_type"))?;
    if !MESSAGE_TYPES.contains(&message_type) {
        return Err(WireError("unknown_message_type"));
    }
    let payload = &frame[end..];
    if map["payload_length"].as_u64() != Some(payload.len() as u64) {
        return Err(WireError("payload_length_mismatch"));
    }
    if map["payload_sha256"] != hex::encode(Sha256::digest(payload)).as_str() {
        return Err(WireError("payload_digest_mismatch"));
    }
    Ok(Frame { message_type: message_type.to_owned(), body: map["body"].clone(), payload: payload.to_vec() })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DType {
    F16 = 1,
    BF16 = 2,
    F32 = 3,
}

impl DType {
    fn from_code(code: u8) -> Result<Self, WireError> {
        match code {
            1 => Ok(Self::F16),
            2 => Ok(Self::BF16),
            3 => Ok(Self::F32),
            _ => Err(WireError("unknown_activation_dtype_code")),
        }
    }
    pub fn bytes(self) -> usize {
        if self == Self::F32 { 4 } else { 2 }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Activation {
    pub dtype: DType,
    pub shape: Vec<u32>,
    pub data: Vec<u8>,
}

const MYAC_HEADER: usize = 4 + 1 + 1 + 1 + 1 + 8;

impl Activation {
    /// `(1, seq, hidden)` float32 rows, as the pixel runtime requires.
    pub fn from_rows_f32(rows: &[Vec<f64>]) -> Self {
        let hidden = rows.first().map_or(0, Vec::len);
        let data = rows.iter().flatten().flat_map(|v| (*v as f32).to_le_bytes()).collect();
        Self { dtype: DType::F32, shape: vec![1, rows.len() as u32, hidden as u32], data }
    }

    pub fn to_rows_f64(&self) -> Result<Vec<Vec<f64>>, WireError> {
        if self.dtype != DType::F32 {
            return Err(WireError("activation_dtype_mismatch"));
        }
        if self.shape.len() != 3 || self.shape[0] != 1 {
            return Err(WireError("activation_rank_mismatch"));
        }
        let hidden = self.shape[2] as usize;
        let values: Vec<f64> = self
            .data
            .chunks_exact(4)
            .map(|c| f64::from(f32::from_le_bytes(c.try_into().unwrap())))
            .collect();
        Ok(values.chunks(hidden.max(1)).map(<[f64]>::to_vec).collect())
    }

    /// `(1, seq, hidden)` float32 from flat row-major values.
    pub fn from_flat_f32(sequence: usize, hidden: usize, values: &[f32]) -> Self {
        let data = values.iter().flat_map(|v| v.to_le_bytes()).collect();
        Self { dtype: DType::F32, shape: vec![1, sequence as u32, hidden as u32], data }
    }

    /// `(seq, hidden, flat row-major values)` of a `(1, seq, hidden)` float32 payload.
    pub fn to_flat_f32(&self) -> Result<(usize, usize, Vec<f32>), WireError> {
        if self.dtype != DType::F32 {
            return Err(WireError("activation_dtype_mismatch"));
        }
        if self.shape.len() != 3 || self.shape[0] != 1 {
            return Err(WireError("activation_rank_mismatch"));
        }
        let (seq, hidden) = (self.shape[1] as usize, self.shape[2] as usize);
        if seq.checked_mul(hidden).and_then(|n| n.checked_mul(4)) != Some(self.data.len()) {
            return Err(WireError("activation_data_length_mismatch"));
        }
        let values = self.data.chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap())).collect();
        Ok((seq, hidden, values))
    }

    pub fn encode(&self) -> Result<Vec<u8>, WireError> {
        self.validate()?;
        let mut out = Vec::with_capacity(MYAC_HEADER + 4 * self.shape.len() + self.data.len());
        out.extend_from_slice(b"MYAC");
        out.extend_from_slice(&[1, self.dtype as u8, self.shape.len() as u8, 1]);
        out.extend_from_slice(&(self.data.len() as u64).to_be_bytes());
        for d in &self.shape {
            out.extend_from_slice(&d.to_be_bytes());
        }
        out.extend_from_slice(&self.data);
        Ok(out)
    }

    pub fn decode(payload: &[u8]) -> Result<Self, WireError> {
        if payload.len() < MYAC_HEADER {
            return Err(WireError("truncated_activation_payload"));
        }
        if &payload[..4] != b"MYAC" {
            return Err(WireError("invalid_activation_payload_magic"));
        }
        if payload[4] != 1 {
            return Err(WireError("unknown_activation_payload_version"));
        }
        let dtype = DType::from_code(payload[5])?;
        let rank = payload[6] as usize;
        if payload[7] != 1 {
            return Err(WireError("unsupported_activation_byte_order"));
        }
        if rank == 0 || rank > 8 {
            return Err(WireError("invalid_activation_rank"));
        }
        let data_len = u64::from_be_bytes(payload[8..16].try_into().unwrap()) as usize;
        let meta = MYAC_HEADER + 4 * rank;
        if payload.len() < meta {
            return Err(WireError("truncated_activation_shape"));
        }
        if data_len > MAX_PAYLOAD_BYTES {
            return Err(WireError("activation_payload_too_large"));
        }
        if payload.len() != meta + data_len {
            return Err(WireError("activation_payload_length_mismatch"));
        }
        let shape = (0..rank)
            .map(|i| u32::from_be_bytes(payload[MYAC_HEADER + 4 * i..MYAC_HEADER + 4 * i + 4].try_into().unwrap()))
            .collect();
        let act = Self { dtype, shape, data: payload[meta..].to_vec() };
        act.validate()?;
        Ok(act)
    }

    fn validate(&self) -> Result<(), WireError> {
        if self.shape.is_empty() || self.shape.len() > 8 || self.shape.contains(&0) {
            return Err(WireError("invalid_activation_shape"));
        }
        let elements = self.shape.iter().try_fold(1usize, |a, d| a.checked_mul(*d as usize));
        match elements.and_then(|e| e.checked_mul(self.dtype.bytes())) {
            Some(n) if n == self.data.len() => Ok(()),
            _ => Err(WireError("activation_data_length_mismatch")),
        }
    }
}

// ---- iroh remote frame ------------------------------------------------------

pub const REMOTE_VERSION: u8 = 1;
pub const REMOTE_HEADER_BYTES: usize = 1 + 1 + 16 + 4;
pub const REMOTE_MAX_PAYLOAD_BYTES: usize = 8 + 16 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RemoteKind {
    Transfer = 1,
    Ack = 2,
    Error = 3,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemoteFrame {
    pub kind: RemoteKind,
    pub message_id: [u8; 16],
    pub payload: Vec<u8>,
}

pub fn encode_remote(kind: RemoteKind, message_id: [u8; 16], payload: &[u8]) -> Result<Vec<u8>, WireError> {
    if payload.len() > REMOTE_MAX_PAYLOAD_BYTES {
        return Err(WireError("payload_too_large"));
    }
    let mut out = Vec::with_capacity(REMOTE_HEADER_BYTES + payload.len());
    out.push(REMOTE_VERSION);
    out.push(kind as u8);
    out.extend_from_slice(&message_id);
    out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    out.extend_from_slice(payload);
    Ok(out)
}

pub fn decode_remote(encoded: &[u8]) -> Result<RemoteFrame, WireError> {
    if encoded.len() < REMOTE_HEADER_BYTES {
        return Err(WireError("frame_too_short"));
    }
    if encoded[0] != REMOTE_VERSION {
        return Err(WireError("unknown_version"));
    }
    let kind = match encoded[1] {
        1 => RemoteKind::Transfer,
        2 => RemoteKind::Ack,
        3 => RemoteKind::Error,
        _ => return Err(WireError("unknown_kind")),
    };
    let len = u32::from_be_bytes(encoded[18..22].try_into().unwrap()) as usize;
    if len > REMOTE_MAX_PAYLOAD_BYTES {
        return Err(WireError("payload_too_large"));
    }
    if encoded.len() != REMOTE_HEADER_BYTES + len {
        return Err(WireError("length_mismatch"));
    }
    Ok(RemoteFrame { kind, message_id: encoded[2..18].try_into().unwrap(), payload: encoded[22..].to_vec() })
}

/// Transfer payload: 8-byte BE sender generation + Router frame.
pub fn transfer_payload(generation: u64, router_frame: &[u8]) -> Vec<u8> {
    let mut out = generation.to_be_bytes().to_vec();
    out.extend_from_slice(router_frame);
    out
}

pub fn split_transfer(payload: &[u8]) -> Result<(u64, &[u8]), WireError> {
    if payload.len() < 8 {
        return Err(WireError("frame_too_short"));
    }
    Ok((u64::from_be_bytes(payload[..8].try_into().unwrap()), &payload[8..]))
}
