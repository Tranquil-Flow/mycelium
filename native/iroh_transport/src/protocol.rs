// SPDX-License-Identifier: AGPL-3.0-or-later
//! Canonical `mycelium.router_wire.v1` framing.
//!
//! Message bodies remain JSON objects. This layer enforces known message types and
//! required top-level body keys, but not Python contract value or nested invariants.

use std::error::Error;
use std::fmt::{self, Write as _};

use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};

pub const ROUTER_WIRE_PROTOCOL: &str = "mycelium.router_wire.v1";
pub const MAX_HEADER_BYTES: usize = 1_048_576;
pub const MAX_PAYLOAD_BYTES: usize = 268_435_456;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MessageType {
    HopHeader,
    ManifestDelta,
    ManifestLocked,
    PathCancellation,
    ProgressivePrefillMessage,
    PrefillChunkCompleted,
    ReservationRequest,
    ReservationResult,
    ReservationCommitResult,
    TokenEvent,
    FailureReport,
}

impl MessageType {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::HopHeader => "HopHeader",
            Self::ManifestDelta => "ManifestDelta",
            Self::ManifestLocked => "ManifestLocked",
            Self::PathCancellation => "PathCancellation",
            Self::ProgressivePrefillMessage => "ProgressivePrefillMessage",
            Self::PrefillChunkCompleted => "PrefillChunkCompleted",
            Self::ReservationRequest => "ReservationRequest",
            Self::ReservationResult => "ReservationResult",
            Self::ReservationCommitResult => "ReservationCommitResult",
            Self::TokenEvent => "TokenEvent",
            Self::FailureReport => "FailureReport",
        }
    }

    fn parse(value: &str) -> Result<Self, WireError> {
        match value {
            "HopHeader" => Ok(Self::HopHeader),
            "ManifestDelta" => Ok(Self::ManifestDelta),
            "ManifestLocked" => Ok(Self::ManifestLocked),
            "PathCancellation" => Ok(Self::PathCancellation),
            "ProgressivePrefillMessage" => Ok(Self::ProgressivePrefillMessage),
            "PrefillChunkCompleted" => Ok(Self::PrefillChunkCompleted),
            "ReservationRequest" => Ok(Self::ReservationRequest),
            "ReservationResult" => Ok(Self::ReservationResult),
            "ReservationCommitResult" => Ok(Self::ReservationCommitResult),
            "TokenEvent" => Ok(Self::TokenEvent),
            "FailureReport" => Ok(Self::FailureReport),
            _ => Err(WireError::UnknownMessageType),
        }
    }

    const fn required_body_fields(self) -> &'static [&'static str] {
        match self {
            Self::HopHeader => &[
                "request_id",
                "path_id",
                "path_attempt",
                "phase",
                "token_index",
                "hop_index",
                "source_placement_id",
                "destination_placement_id",
                "topology_version",
                "idempotency_key",
            ],
            Self::ManifestDelta => &["request_id", "path_id", "path_attempt", "hop_index", "hop"],
            Self::ManifestLocked => &["request_id", "path_id", "path_attempt", "manifest", "build"],
            Self::PathCancellation => {
                &["request_id", "path_id", "path_attempt", "topology_version"]
            }
            Self::ProgressivePrefillMessage => &[
                "header",
                "graph",
                "request",
                "ordered_hops",
                "excluded_placements",
                "excluded_edges",
                "excluded_devices",
            ],
            Self::PrefillChunkCompleted => &[
                "request_id",
                "path_id",
                "path_attempt",
                "chunk_index",
                "token_count",
            ],
            Self::ReservationRequest => &[
                "request_id",
                "path_id",
                "path_attempt",
                "placement_id",
                "kv_bytes",
                "deployment_epoch",
                "lease_expires_at",
            ],
            Self::ReservationResult | Self::ReservationCommitResult => &["accepted"],
            Self::TokenEvent => &[
                "request_id",
                "path_id",
                "path_attempt",
                "token_index",
                "token_id",
                "sampling_counter",
            ],
            Self::FailureReport => &[
                "request_id",
                "path_id",
                "path_attempt",
                "token_index",
                "scope",
                "reason",
            ],
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DecodedFrame {
    pub message_type: MessageType,
    pub body: Map<String, Value>,
    pub payload: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WireError {
    TruncatedFrame,
    HeaderTooLarge,
    TruncatedHeader,
    InvalidWireJson,
    InvalidWireEnvelope,
    MissingWireField(String),
    UnknownWireProtocol,
    UnknownMessageType,
    InvalidWireBody,
    InvalidPayloadLength,
    PayloadTooLarge,
    PayloadLengthMismatch,
    PayloadDigestMismatch,
    InvalidWireMessage,
}

impl WireError {
    pub const fn code(&self) -> &'static str {
        match self {
            Self::TruncatedFrame => "truncated_frame",
            Self::HeaderTooLarge => "header_too_large",
            Self::TruncatedHeader => "truncated_header",
            Self::InvalidWireJson => "invalid_wire_json",
            Self::InvalidWireEnvelope => "invalid_wire_envelope",
            Self::MissingWireField(_) => "missing_wire_field",
            Self::UnknownWireProtocol => "unknown_wire_protocol",
            Self::UnknownMessageType => "unknown_message_type",
            Self::InvalidWireBody => "invalid_wire_body",
            Self::InvalidPayloadLength => "invalid_payload_length",
            Self::PayloadTooLarge => "payload_too_large",
            Self::PayloadLengthMismatch => "payload_length_mismatch",
            Self::PayloadDigestMismatch => "payload_digest_mismatch",
            Self::InvalidWireMessage => "invalid_wire_message",
        }
    }
}

impl fmt::Display for WireError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingWireField(field) => write!(formatter, "{}: {field}", self.code()),
            _ => formatter.write_str(self.code()),
        }
    }
}

impl Error for WireError {}

pub fn encode_frame(
    message_type: &MessageType,
    body: &Map<String, Value>,
    payload: &[u8],
) -> Result<Vec<u8>, WireError> {
    validate_body_fields(*message_type, body)?;
    if payload.len() > MAX_PAYLOAD_BYTES {
        return Err(WireError::PayloadTooLarge);
    }

    let mut envelope = Map::new();
    envelope.insert(
        "protocol".into(),
        Value::String(ROUTER_WIRE_PROTOCOL.into()),
    );
    envelope.insert(
        "message_type".into(),
        Value::String(message_type.as_str().into()),
    );
    envelope.insert("body".into(), Value::Object(body.clone()));
    envelope.insert(
        "payload_length".into(),
        Value::Number(Number::from(payload.len() as u64)),
    );
    envelope.insert("payload_sha256".into(), Value::String(sha256_hex(payload)));

    let header = canonical_json(&Value::Object(envelope))?;
    if header.len() > MAX_HEADER_BYTES {
        return Err(WireError::HeaderTooLarge);
    }

    let mut frame = Vec::with_capacity(4 + header.len() + payload.len());
    frame.extend_from_slice(&(header.len() as u32).to_be_bytes());
    frame.extend_from_slice(&header);
    frame.extend_from_slice(payload);
    Ok(frame)
}

pub fn decode_frame(frame: &[u8]) -> Result<DecodedFrame, WireError> {
    if frame.len() < 4 {
        return Err(WireError::TruncatedFrame);
    }
    let header_length = u32::from_be_bytes(
        frame[..4]
            .try_into()
            .map_err(|_| WireError::TruncatedFrame)?,
    ) as usize;
    if header_length > MAX_HEADER_BYTES {
        return Err(WireError::HeaderTooLarge);
    }
    let header_end = 4 + header_length;
    if frame.len() < header_end {
        return Err(WireError::TruncatedHeader);
    }

    let envelope: Value =
        serde_json::from_slice(&frame[4..header_end]).map_err(|_| WireError::InvalidWireJson)?;
    let envelope = envelope.as_object().ok_or(WireError::InvalidWireEnvelope)?;

    let protocol = required(envelope, "protocol")?;
    if protocol.as_str() != Some(ROUTER_WIRE_PROTOCOL) {
        return Err(WireError::UnknownWireProtocol);
    }

    let message_type = required(envelope, "message_type")?
        .as_str()
        .ok_or(WireError::UnknownMessageType)
        .and_then(MessageType::parse)?;
    let body = required(envelope, "body")?
        .as_object()
        .ok_or(WireError::InvalidWireBody)?
        .clone();
    validate_body_fields(message_type, &body)?;

    let payload_length_value = required(envelope, "payload_length")?;
    let payload_length_u64 = match payload_length_value.as_u64() {
        Some(length) => length,
        None => {
            let oversized_integer = payload_length_value
                .as_number()
                .is_some_and(|number| number.to_string().bytes().all(|byte| byte.is_ascii_digit()));
            return Err(if oversized_integer {
                WireError::PayloadTooLarge
            } else {
                WireError::InvalidPayloadLength
            });
        }
    };
    if payload_length_u64 > MAX_PAYLOAD_BYTES as u64 {
        return Err(WireError::PayloadTooLarge);
    }
    let payload_length =
        usize::try_from(payload_length_u64).map_err(|_| WireError::InvalidPayloadLength)?;

    let payload = &frame[header_end..];
    if payload.len() != payload_length {
        return Err(WireError::PayloadLengthMismatch);
    }
    let expected_digest = required(envelope, "payload_sha256")?
        .as_str()
        .ok_or(WireError::PayloadDigestMismatch)?;
    if expected_digest != sha256_hex(payload) {
        return Err(WireError::PayloadDigestMismatch);
    }

    Ok(DecodedFrame {
        message_type,
        body,
        payload: payload.to_vec(),
    })
}

fn validate_body_fields(
    message_type: MessageType,
    body: &Map<String, Value>,
) -> Result<(), WireError> {
    for field in message_type.required_body_fields() {
        if !body.contains_key(*field) {
            return Err(WireError::MissingWireField((*field).into()));
        }
    }
    Ok(())
}

fn required<'a>(envelope: &'a Map<String, Value>, field: &str) -> Result<&'a Value, WireError> {
    envelope
        .get(field)
        .ok_or_else(|| WireError::MissingWireField(field.into()))
}

fn canonical_json(value: &Value) -> Result<Vec<u8>, WireError> {
    let encoded = serde_json::to_string(value).map_err(|_| WireError::InvalidWireMessage)?;
    let encoded = normalize_python_numbers(&encoded)?;
    let mut ascii = String::with_capacity(encoded.len());
    for character in encoded.chars() {
        if character.is_ascii() {
            ascii.push(character);
            continue;
        }
        let codepoint = character as u32;
        if codepoint <= 0xffff {
            write!(&mut ascii, "\\u{codepoint:04x}").map_err(|_| WireError::InvalidWireMessage)?;
        } else {
            let supplementary = codepoint - 0x1_0000;
            let high = 0xd800 + (supplementary >> 10);
            let low = 0xdc00 + (supplementary & 0x3ff);
            write!(&mut ascii, "\\u{high:04x}\\u{low:04x}")
                .map_err(|_| WireError::InvalidWireMessage)?;
        }
    }
    Ok(ascii.into_bytes())
}

fn normalize_python_numbers(encoded: &str) -> Result<String, WireError> {
    let bytes = encoded.as_bytes();
    let mut normalized = String::with_capacity(bytes.len());
    let mut in_string = false;
    let mut escaped = false;
    let mut index = 0;

    while index < bytes.len() {
        let byte = bytes[index];
        if in_string {
            if !byte.is_ascii() {
                let character = encoded[index..]
                    .chars()
                    .next()
                    .ok_or(WireError::InvalidWireMessage)?;
                normalized.push(character);
                index += character.len_utf8();
                continue;
            }
            normalized.push(byte as char);
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
            index += 1;
            continue;
        }
        if byte == b'"' {
            in_string = true;
            normalized.push('"');
            index += 1;
            continue;
        }
        if byte != b'-' && !byte.is_ascii_digit() {
            normalized.push(byte as char);
            index += 1;
            continue;
        }

        let number_start = index;
        index += 1;
        while index < bytes.len()
            && matches!(bytes[index], b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-')
        {
            index += 1;
        }
        normalize_python_number_token(&encoded[number_start..index], &mut normalized)?;
    }

    Ok(normalized)
}

fn normalize_python_number_token(token: &str, normalized: &mut String) -> Result<(), WireError> {
    if token.contains(['.', 'e', 'E']) {
        let value = token
            .parse::<f64>()
            .map_err(|_| WireError::InvalidWireMessage)?;
        if !value.is_finite() {
            return Err(WireError::InvalidWireMessage);
        }
        let finite = serde_json::to_string(&value).map_err(|_| WireError::InvalidWireMessage)?;
        normalize_finite_float_token(&finite, normalized);
    } else if token == "-0" {
        normalized.push('0');
    } else {
        normalized.push_str(token);
    }
    Ok(())
}

fn normalize_finite_float_token(token: &str, normalized: &mut String) {
    if let Some(exponent_index) = token.find(['e', 'E']) {
        normalized.push_str(&token[..exponent_index]);
        normalized.push('e');
        let exponent = &token[exponent_index + 1..];
        let (sign, digits) = match exponent.as_bytes().first() {
            Some(b'+' | b'-') => (&exponent[..1], &exponent[1..]),
            _ => ("", exponent),
        };
        normalized.push_str(sign);
        if digits.len() == 1 {
            normalized.push('0');
        }
        normalized.push_str(digits);
        return;
    }

    let unsigned = token.strip_prefix('-').unwrap_or(token);
    let Some(fraction) = unsigned.strip_prefix("0.") else {
        normalized.push_str(token);
        return;
    };
    let leading_zeros = fraction.bytes().take_while(|byte| *byte == b'0').count();
    if leading_zeros < 4 || leading_zeros == fraction.len() {
        normalized.push_str(token);
        return;
    }

    if token.starts_with('-') {
        normalized.push('-');
    }
    let significant = &fraction[leading_zeros..];
    normalized.push_str(&significant[..1]);
    if significant.len() > 1 {
        normalized.push('.');
        normalized.push_str(&significant[1..]);
    }
    write!(normalized, "e-{:02}", leading_zeros + 1)
        .expect("formatting a number into String cannot fail");
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
