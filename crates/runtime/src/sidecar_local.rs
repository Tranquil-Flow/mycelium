// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 sidecar **local IPC** record layer: the authenticated handshake, the
//! HMAC-tagged record framing and the payload shapes a local client (the Python
//! `SidecarClient` in v0.1.4, node-service tooling in v0.2) exchanges with the
//! sidecar. Source: `native/iroh_transport/src/local.rs` and `sidecar.rs:956-1296,
//! 1436-1477 @ f4e366e9`, cross-read against `mycelium_iroh_sidecar/client.py`.
//!
//! Pure and synchronous: no socket, task or clock. HMAC-SHA256 is the admitted `hmac`
//! 0.12.1 + `sha2` 0.10.9 crates (RU-010, CR001; tag comparison is the crate's
//! constant-time `verify_slice`); the single-block HKDF-SHA256 expansion is two HMAC
//! calls over them. Both are pinned to vectors produced by executing the oracle.
//!
//! The in-process node-service path does NOT need this layer (it calls the pooled
//! driver directly); it exists so a local-IPC consumer or a conformance harness can
//! speak the exact v0.1.4 bytes. Process-boundary pieces (Unix-socket / loopback
//! listener security, bootstrap-secret fd, ready line) are not ported (see
//! `IROH-PARITY.md`). Numeric bounds that the sidecar hard-codes are caller-supplied
//! ([`ControlCallLimits`]); wire-format sizes derive from `protocol::remote`.
use hmac::{Hmac, Mac};
use protocol::{remote::REMOTE_OPERATIONAL_FRAME_BYTES, router_wire};
use serde_json::Value;
use sha2::Sha256;
use std::fmt;

pub const LOCAL_PROTOCOL: &str = "mycelium.iroh_sidecar.local.v1";
pub const LOCAL_RECORD_VERSION: u8 = 1;
/// Routed sends carry 32 bytes of destination plus two u64 generations
/// (local.rs:22).
pub const LOCAL_MAX_PAYLOAD_BYTES: usize = REMOTE_OPERATIONAL_FRAME_BYTES + 32 + 16;
pub const LOCAL_RECORD_HEADER_BYTES: usize = 1 + 1 + 8 + 16 + 4;
pub const LOCAL_RECORD_TAG_BYTES: usize = 32;
pub const LOCAL_MAX_RECORD_BYTES: usize =
    LOCAL_RECORD_HEADER_BYTES + LOCAL_MAX_PAYLOAD_BYTES + LOCAL_RECORD_TAG_BYTES;
pub const ENDPOINT_ID_BYTES: usize = 32;
pub const GENERATION_BYTES: usize = 8;
const CONFIRMED_GENERATION_BYTES: usize = GENERATION_BYTES * 2;
const CLIENT_PROOF_DOMAIN: &[u8] = b"mycelium.iroh_sidecar.local.v1/client-proof\0";
const SERVER_PROOF_DOMAIN: &[u8] = b"mycelium.iroh_sidecar.local.v1/server-proof\0";
const SESSION_KEYS_INFO: &[u8] = b"mycelium.iroh_sidecar.local.v1/session-keys";
const CLIENT_TO_SIDECAR_INFO: &[u8] = b"mycelium.iroh_sidecar.local.v1/client-to-sidecar";
const SIDECAR_TO_CLIENT_INFO: &[u8] = b"mycelium.iroh_sidecar.local.v1/sidecar-to-client";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum RecordKind {
    Send = 1,
    Receive = 2,
    Delivery = 3,
    Ack = 4,
    Cancel = 5,
    ConfigurePeer = 6,
    Ping = 7,
    Error = 8,
    SendConfirmed = 9,
    ConfigurePeers = 10,
    SendRouted = 11,
    ReceiveFrom = 12,
    DeliveryFrom = 13,
    GetTransportObservations = 14,
    TransportObservations = 15,
    GetAdmissionCounters = 16,
    AdmissionCounters = 17,
    /// Carry one phone control command to a configured peer's control ALPN.
    ControlCall = 18,
    ControlCallResult = 19,
}
impl TryFrom<u8> for RecordKind {
    type Error = LocalProtocolError;
    fn try_from(value: u8) -> Result<Self, LocalProtocolError> {
        Ok(match value {
            1 => Self::Send,
            2 => Self::Receive,
            3 => Self::Delivery,
            4 => Self::Ack,
            5 => Self::Cancel,
            6 => Self::ConfigurePeer,
            7 => Self::Ping,
            8 => Self::Error,
            9 => Self::SendConfirmed,
            10 => Self::ConfigurePeers,
            11 => Self::SendRouted,
            12 => Self::ReceiveFrom,
            13 => Self::DeliveryFrom,
            14 => Self::GetTransportObservations,
            15 => Self::TransportObservations,
            16 => Self::GetAdmissionCounters,
            17 => Self::AdmissionCounters,
            18 => Self::ControlCall,
            19 => Self::ControlCallResult,
            _ => return Err(LocalProtocolError::UnknownRecordKind),
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Record {
    pub kind: RecordKind,
    pub sequence: u64,
    pub message_id: [u8; 16],
    pub payload: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LocalProtocolError {
    InvalidSecretLength,
    InvalidNonceLength,
    InvalidKeyLength,
    InvalidHex,
    InvalidProof,
    PayloadTooLarge,
    RecordTooShort,
    RecordLengthMismatch,
    UnknownRecordVersion,
    UnknownRecordKind,
    InvalidRecordTag,
    InvalidSequence,
    SequenceExhausted,
    InvalidHello,
    UnknownProtocol,
}
impl LocalProtocolError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::InvalidSecretLength => "invalid_secret_length",
            Self::InvalidNonceLength => "invalid_nonce_length",
            Self::InvalidKeyLength => "invalid_key_length",
            Self::InvalidHex => "invalid_hex",
            Self::InvalidProof => "invalid_proof",
            Self::PayloadTooLarge => "payload_too_large",
            Self::RecordTooShort => "record_too_short",
            Self::RecordLengthMismatch => "record_length_mismatch",
            Self::UnknownRecordVersion => "unknown_record_version",
            Self::UnknownRecordKind => "unknown_record_kind",
            Self::InvalidRecordTag => "invalid_record_tag",
            Self::InvalidSequence => "invalid_sequence",
            Self::SequenceExhausted => "sequence_exhausted",
            Self::InvalidHello => "invalid_hello",
            Self::UnknownProtocol => "unknown_protocol",
        }
    }
}
impl fmt::Display for LocalProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}
impl std::error::Error for LocalProtocolError {}

/// Strictly increasing per-direction record counter (local.rs:384-420). It never
/// wraps: after `u64::MAX` is accepted the guard is exhausted.
#[derive(Clone, Debug, Default)]
pub struct SequenceGuard {
    next: u64,
    exhausted: bool,
}
impl SequenceGuard {
    pub const fn new() -> Self {
        Self::at(0)
    }
    /// A guard expecting `next` (a session resumed mid-stream; the exhaustion test).
    pub const fn at(next: u64) -> Self {
        Self {
            next,
            exhausted: false,
        }
    }
    pub const fn next_expected(&self) -> Option<u64> {
        if self.exhausted {
            None
        } else {
            Some(self.next)
        }
    }
    pub fn accept(&mut self, sequence: u64) -> Result<(), LocalProtocolError> {
        if self.exhausted {
            return Err(LocalProtocolError::SequenceExhausted);
        }
        if sequence != self.next {
            return Err(LocalProtocolError::InvalidSequence);
        }
        match self.next.checked_add(1) {
            Some(next) => self.next = next,
            None => self.exhausted = true,
        }
        Ok(())
    }
}

/// HMAC-SHA256 (RFC 2104) by the admitted `hmac` 0.12.1 + `sha2` 0.10.9 crates (RU-010).
/// Any key length is accepted, as before: keys longer than a block are hashed first.
pub fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    // HMAC takes every key length, so `new_from_slice` cannot fail; if it ever did, the
    // all-zero tag is one no verifier built on the same key accepts (fail closed).
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(key) else {
        return [0_u8; 32];
    };
    mac.update(message);
    mac.finalize().into_bytes().into()
}
/// Constant-time tag check by the crate's `verify_slice`; any length other than 32 fails.
fn verify_tag(key: &[u8], message: &[u8], supplied: &[u8]) -> Result<(), LocalProtocolError> {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(key).map_err(|_| LocalProtocolError::InvalidProof)?;
    mac.update(message);
    mac.verify_slice(supplied)
        .map_err(|_| LocalProtocolError::InvalidProof)
}

#[derive(Clone)]
pub struct SessionKeys {
    pub client_to_sidecar: [u8; 32],
    pub sidecar_to_client: [u8; 32],
}
impl fmt::Debug for SessionKeys {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("SessionKeys([REDACTED])")
    }
}
/// HKDF-SHA256 (salt = client nonce || server nonce, ikm = secret), two single-block
/// expansions (local.rs:264-300).
pub fn derive_session_keys(
    secret: &[u8],
    client_nonce: &[u8; 32],
    server_nonce: &[u8; 32],
) -> Result<SessionKeys, LocalProtocolError> {
    if secret.len() != 32 {
        return Err(LocalProtocolError::InvalidSecretLength);
    }
    let mut salt = [0_u8; 64];
    salt[..32].copy_from_slice(client_nonce);
    salt[32..].copy_from_slice(server_nonce);
    let prk = hmac_sha256(&salt, secret);
    let expand = |direction: &[u8]| {
        let mut info = Vec::with_capacity(SESSION_KEYS_INFO.len() + direction.len() + 1);
        info.extend_from_slice(SESSION_KEYS_INFO);
        info.extend_from_slice(direction);
        info.push(1);
        hmac_sha256(&prk, &info)
    };
    Ok(SessionKeys {
        client_to_sidecar: expand(CLIENT_TO_SIDECAR_INFO),
        sidecar_to_client: expand(SIDECAR_TO_CLIENT_INFO),
    })
}

/// The bytes the client proof is the MAC of (local.rs, domain || client nonce).
fn client_proof_input(
    secret: &[u8],
    client_nonce: &[u8; 32],
) -> Result<Vec<u8>, LocalProtocolError> {
    if secret.len() != 32 {
        return Err(LocalProtocolError::InvalidSecretLength);
    }
    let mut input = Vec::with_capacity(CLIENT_PROOF_DOMAIN.len() + 32);
    input.extend_from_slice(CLIENT_PROOF_DOMAIN);
    input.extend_from_slice(client_nonce);
    Ok(input)
}
fn client_proof(secret: &[u8], client_nonce: &[u8; 32]) -> Result<[u8; 32], LocalProtocolError> {
    Ok(hmac_sha256(
        secret,
        &client_proof_input(secret, client_nonce)?,
    ))
}
/// The bytes the server proof is the MAC of (domain || both nonces || len || endpoint id).
fn server_proof_input(
    secret: &[u8],
    client_nonce: &[u8; 32],
    server_nonce: &[u8; 32],
    endpoint_id: &str,
) -> Result<Vec<u8>, LocalProtocolError> {
    if secret.len() != 32 {
        return Err(LocalProtocolError::InvalidSecretLength);
    }
    let length = u32::try_from(endpoint_id.len()).map_err(|_| LocalProtocolError::InvalidProof)?;
    let mut input = Vec::with_capacity(SERVER_PROOF_DOMAIN.len() + 64 + 4 + endpoint_id.len());
    input.extend_from_slice(SERVER_PROOF_DOMAIN);
    input.extend_from_slice(client_nonce);
    input.extend_from_slice(server_nonce);
    input.extend_from_slice(&length.to_be_bytes());
    input.extend_from_slice(endpoint_id.as_bytes());
    Ok(input)
}
fn server_proof(
    secret: &[u8],
    client_nonce: &[u8; 32],
    server_nonce: &[u8; 32],
    endpoint_id: &str,
) -> Result<[u8; 32], LocalProtocolError> {
    Ok(hmac_sha256(
        secret,
        &server_proof_input(secret, client_nonce, server_nonce, endpoint_id)?,
    ))
}
fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
fn hex_decode_array(text: &str) -> Result<[u8; 32], LocalProtocolError> {
    if text.len() != 64 || !text.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(LocalProtocolError::InvalidHex);
    }
    let mut out = [0_u8; 32];
    for (index, byte) in out.iter_mut().enumerate() {
        let pair = text
            .get(index * 2..index * 2 + 2)
            .ok_or(LocalProtocolError::InvalidHex)?;
        *byte = u8::from_str_radix(pair, 16).map_err(|_| LocalProtocolError::InvalidHex)?;
    }
    Ok(out)
}
fn hello_object(bytes: &[u8]) -> Result<serde_json::Map<String, Value>, LocalProtocolError> {
    match serde_json::from_slice::<Value>(bytes) {
        Ok(Value::Object(object)) => Ok(object),
        _ => Err(LocalProtocolError::InvalidHello),
    }
}
/// Strict hello fields (local.rs `deny_unknown_fields` + serde aliases): `protocol`
/// plus each `(name, alias)` pair exactly once under either spelling, strings only.
fn hello_fields<'a>(
    object: &'a serde_json::Map<String, Value>,
    pairs: &[(&str, &str)],
    extra: &[&str],
) -> Result<(&'a str, Vec<&'a str>), LocalProtocolError> {
    let known = |key: &str| {
        key == "protocol"
            || extra.contains(&key)
            || pairs
                .iter()
                .any(|(name, alias)| key == *name || key == *alias)
    };
    if object.keys().any(|key| !known(key)) {
        return Err(LocalProtocolError::InvalidHello);
    }
    let string = |key: &str| object.get(key).map(|value| value.as_str());
    let protocol = string("protocol")
        .flatten()
        .ok_or(LocalProtocolError::InvalidHello)?;
    let mut values = Vec::with_capacity(pairs.len());
    for (name, alias) in pairs {
        let value = match (string(name), string(alias)) {
            (Some(value), None) | (None, Some(value)) => value,
            _ => return Err(LocalProtocolError::InvalidHello),
        };
        values.push(value.ok_or(LocalProtocolError::InvalidHello)?);
    }
    Ok((protocol, values))
}
/// The client's opening object (JSON, hex nonce and proof).
pub fn client_hello(secret: &[u8], client_nonce: &[u8; 32]) -> Result<Vec<u8>, LocalProtocolError> {
    let proof = client_proof(secret, client_nonce)?;
    Ok(format!(
        "{{\"protocol\":\"{LOCAL_PROTOCOL}\",\"client_nonce\":\"{}\",\"client_proof\":\"{}\"}}",
        hex_encode(client_nonce),
        hex_encode(&proof)
    )
    .into_bytes())
}
/// Sidecar side: verifies a client hello and returns the client nonce.
pub fn verify_client_hello(bytes: &[u8], secret: &[u8]) -> Result<[u8; 32], LocalProtocolError> {
    let object = hello_object(bytes)?;
    let (protocol, values) = hello_fields(
        &object,
        &[("client_nonce", "nonce"), ("client_proof", "proof")],
        &[],
    )?;
    if protocol != LOCAL_PROTOCOL {
        return Err(LocalProtocolError::UnknownProtocol);
    }
    let nonce = hex_decode_array(values[0])?;
    let proof = hex_decode_array(values[1])?;
    verify_tag(secret, &client_proof_input(secret, &nonce)?, &proof)?;
    Ok(nonce)
}
/// Sidecar side: the answering object, binding the public endpoint id.
pub fn server_hello(
    secret: &[u8],
    client_nonce: &[u8; 32],
    server_nonce: &[u8; 32],
    endpoint_id: &str,
) -> Result<Vec<u8>, LocalProtocolError> {
    let proof = server_proof(secret, client_nonce, server_nonce, endpoint_id)?;
    let endpoint =
        serde_json::to_string(endpoint_id).map_err(|_| LocalProtocolError::InvalidHello)?;
    Ok(format!(
        "{{\"protocol\":\"{LOCAL_PROTOCOL}\",\"server_nonce\":\"{}\",\"endpoint_id\":{endpoint},\"server_proof\":\"{}\"}}",
        hex_encode(server_nonce),
        hex_encode(&proof)
    )
    .into_bytes())
}
/// Client side: verifies the sidecar hello, returning its nonce and endpoint id.
pub fn verify_server_hello(
    bytes: &[u8],
    secret: &[u8],
    client_nonce: &[u8; 32],
) -> Result<([u8; 32], String), LocalProtocolError> {
    let object = hello_object(bytes)?;
    let (protocol, values) = hello_fields(
        &object,
        &[("server_nonce", "nonce"), ("server_proof", "proof")],
        &["endpoint_id"],
    )?;
    let endpoint = object
        .get("endpoint_id")
        .and_then(Value::as_str)
        .ok_or(LocalProtocolError::InvalidHello)?;
    if protocol != LOCAL_PROTOCOL {
        return Err(LocalProtocolError::UnknownProtocol);
    }
    let nonce = hex_decode_array(values[0])?;
    let proof = hex_decode_array(values[1])?;
    verify_tag(
        secret,
        &server_proof_input(secret, client_nonce, &nonce, endpoint)?,
        &proof,
    )?;
    Ok((nonce, endpoint.to_owned()))
}

/// `version | kind | sequence u64 BE | message id | payload len u32 BE | payload |
/// HMAC-SHA256(key, everything before)` (local.rs:302-326).
pub fn encode_record(
    kind: RecordKind,
    sequence: u64,
    message_id: [u8; 16],
    payload: &[u8],
    key: &[u8],
) -> Result<Vec<u8>, LocalProtocolError> {
    if payload.len() > LOCAL_MAX_PAYLOAD_BYTES {
        return Err(LocalProtocolError::PayloadTooLarge);
    }
    if key.len() != 32 {
        return Err(LocalProtocolError::InvalidKeyLength);
    }
    let length = u32::try_from(payload.len()).map_err(|_| LocalProtocolError::PayloadTooLarge)?;
    let mut encoded = Vec::with_capacity(LOCAL_RECORD_HEADER_BYTES + payload.len() + 32);
    encoded.push(LOCAL_RECORD_VERSION);
    encoded.push(kind as u8);
    encoded.extend_from_slice(&sequence.to_be_bytes());
    encoded.extend_from_slice(&message_id);
    encoded.extend_from_slice(&length.to_be_bytes());
    encoded.extend_from_slice(payload);
    let tag = hmac_sha256(key, &encoded);
    encoded.extend_from_slice(&tag);
    Ok(encoded)
}
/// Check order is the sidecar's (local.rs:328-382): key, length bounds, version,
/// declared length, tag, **then** the sequence (consumed even when the kind is
/// unknown), then the kind. A record refused before the sequence step does not
/// advance the guard.
pub fn decode_record(
    encoded: &[u8],
    key: &[u8],
    sequence: &mut SequenceGuard,
) -> Result<Record, LocalProtocolError> {
    if key.len() != 32 {
        return Err(LocalProtocolError::InvalidKeyLength);
    }
    if encoded.len() < LOCAL_RECORD_HEADER_BYTES + LOCAL_RECORD_TAG_BYTES {
        return Err(LocalProtocolError::RecordTooShort);
    }
    if encoded.len() > LOCAL_MAX_RECORD_BYTES {
        return Err(LocalProtocolError::PayloadTooLarge);
    }
    if encoded[0] != LOCAL_RECORD_VERSION {
        return Err(LocalProtocolError::UnknownRecordVersion);
    }
    let field = |range: std::ops::Range<usize>| {
        encoded.get(range).ok_or(LocalProtocolError::RecordTooShort)
    };
    let record_sequence = u64::from_be_bytes(
        field(2..10)?
            .try_into()
            .map_err(|_| LocalProtocolError::RecordTooShort)?,
    );
    let message_id: [u8; 16] = field(10..26)?
        .try_into()
        .map_err(|_| LocalProtocolError::RecordTooShort)?;
    let payload_length = u32::from_be_bytes(
        field(26..30)?
            .try_into()
            .map_err(|_| LocalProtocolError::RecordTooShort)?,
    ) as usize;
    if payload_length > LOCAL_MAX_PAYLOAD_BYTES {
        return Err(LocalProtocolError::PayloadTooLarge);
    }
    let expected_length = LOCAL_RECORD_HEADER_BYTES
        .checked_add(payload_length)
        .and_then(|length| length.checked_add(LOCAL_RECORD_TAG_BYTES))
        .ok_or(LocalProtocolError::PayloadTooLarge)?;
    if encoded.len() != expected_length {
        return Err(LocalProtocolError::RecordLengthMismatch);
    }
    let signed_end = expected_length - LOCAL_RECORD_TAG_BYTES;
    verify_tag(key, &encoded[..signed_end], &encoded[signed_end..])?;
    sequence.accept(record_sequence)?;
    let kind = RecordKind::try_from(encoded[1])?;
    Ok(Record {
        kind,
        sequence: record_sequence,
        message_id,
        payload: encoded[LOCAL_RECORD_HEADER_BYTES..signed_end].to_vec(),
    })
}

/// A parsed `Send` / `SendConfirmed` / `SendRouted` payload (sidecar.rs:1055-1100).
/// A routed destination is returned raw: whether those 32 bytes are a valid endpoint
/// id is the dispatcher's `invalid_peer` check.
#[derive(Debug, Eq, PartialEq)]
pub struct SendPayload {
    pub destination: Option<[u8; ENDPOINT_ID_BYTES]>,
    pub expected_generation: Option<u64>,
    pub source_generation: Option<u64>,
    pub frame: Vec<u8>,
}
/// Refusal codes are the sidecar's error strings (`invalid_peer`,
/// `invalid_generation`).
pub fn parse_send(kind: RecordKind, payload: Vec<u8>) -> Result<SendPayload, &'static str> {
    let routed = kind == RecordKind::SendRouted;
    let confirmed = routed || kind == RecordKind::SendConfirmed;
    let (destination, body) = if routed {
        let Some((head, rest)) = payload.split_first_chunk::<ENDPOINT_ID_BYTES>() else {
            return Err("invalid_peer");
        };
        (Some(*head), rest.to_vec())
    } else {
        (None, payload)
    };
    if !confirmed {
        return Ok(SendPayload {
            destination,
            expected_generation: None,
            source_generation: None,
            frame: body,
        });
    }
    if body.len() < CONFIRMED_GENERATION_BYTES {
        return Err("invalid_generation");
    }
    let generation = |range: std::ops::Range<usize>| {
        body.get(range)
            .and_then(|bytes| <[u8; GENERATION_BYTES]>::try_from(bytes).ok())
            .map(u64::from_be_bytes)
            .ok_or("invalid_generation")
    };
    let expected = generation(0..GENERATION_BYTES)?;
    let source = generation(GENERATION_BYTES..CONFIRMED_GENERATION_BYTES)?;
    if expected == 0 || source == 0 {
        return Err("invalid_generation");
    }
    Ok(SendPayload {
        destination,
        expected_generation: Some(expected),
        source_generation: Some(source),
        frame: body[CONFIRMED_GENERATION_BYTES..].to_vec(),
    })
}
/// `Delivery` payload: generation u64 BE then the Router frame (sidecar.rs:1143).
pub fn delivery_payload(generation: u64, frame: &[u8]) -> Vec<u8> {
    [generation.to_be_bytes().as_slice(), frame].concat()
}
/// `DeliveryFrom` payload: 32-byte source (all zero for a local delivery),
/// generation u64 BE, frame (sidecar.rs:1149-1160).
pub fn delivery_from_payload(
    source: Option<[u8; ENDPOINT_ID_BYTES]>,
    generation: u64,
    frame: &[u8],
) -> Vec<u8> {
    [
        source.unwrap_or([0; ENDPOINT_ID_BYTES]).as_slice(),
        generation.to_be_bytes().as_slice(),
        frame,
    ]
    .concat()
}
/// `{"code":"..."}` (sidecar.rs:1270).
pub fn error_payload(code: &str) -> Vec<u8> {
    let quoted = serde_json::to_string(code).unwrap_or_else(|_| "\"\"".to_owned());
    format!("{{\"code\":{quoted}}}").into_bytes()
}

/// Caller-supplied `ControlCall` bounds: the sidecar hard-codes 60 s
/// (`CONTROL_CALL_TIMEOUT_CEILING`, sidecar.rs:81) and 4 MiB (`CONTROL_CALL_MAX_BYTES`,
/// sidecar.rs:80); neither has a native L1 id, so no default exists here.
#[derive(Clone, Copy, Debug)]
pub struct ControlCallLimits {
    pub ceiling_millis: u64,
    pub max_command_bytes: usize,
}
/// One parsed `ControlCall` payload: 32 raw destination bytes, u64 BE timeout in
/// milliseconds, then the command bytes.
#[derive(Debug, Eq, PartialEq)]
pub struct ControlCall {
    pub destination: [u8; ENDPOINT_ID_BYTES],
    pub millis: u64,
    pub command: Vec<u8>,
}
const CONTROL_CALL_PREFIX_BYTES: usize = 8;
/// [`parse_control_call_with`] accepting any 32 bytes as a destination.
pub fn parse_control_call(
    payload: &[u8],
    limits: ControlCallLimits,
) -> Result<ControlCall, &'static str> {
    parse_control_call_with(payload, limits, |_| true)
}
/// sidecar.rs:1451-1477, in the sidecar's check order: length, destination validity
/// (`invalid_control_destination`), the timeout ceiling, then zero timeout / empty or
/// oversized command.
pub fn parse_control_call_with(
    payload: &[u8],
    limits: ControlCallLimits,
    valid_destination: impl Fn(&[u8; ENDPOINT_ID_BYTES]) -> bool,
) -> Result<ControlCall, &'static str> {
    let prefix = ENDPOINT_ID_BYTES + CONTROL_CALL_PREFIX_BYTES;
    if payload.len() < prefix {
        return Err("invalid_control_call");
    }
    let destination: [u8; ENDPOINT_ID_BYTES] = payload[..ENDPOINT_ID_BYTES]
        .try_into()
        .map_err(|_| "invalid_control_call")?;
    if !valid_destination(&destination) {
        return Err("invalid_control_destination");
    }
    let millis = u64::from_be_bytes(
        payload[ENDPOINT_ID_BYTES..prefix]
            .try_into()
            .map_err(|_| "invalid_control_call")?,
    );
    if millis > limits.ceiling_millis {
        return Err("control_timeout_exceeded");
    }
    let command = &payload[prefix..];
    if millis == 0 || command.is_empty() || command.len() > limits.max_command_bytes {
        return Err("invalid_control_call");
    }
    Ok(ControlCall {
        destination,
        millis,
        command: command.to_vec(),
    })
}

#[derive(Debug, Eq, PartialEq)]
pub enum IngressError {
    FrameTooLarge,
    InvalidFrame,
}
impl IngressError {
    pub const fn code(&self) -> &'static str {
        match self {
            Self::FrameTooLarge => "frame_too_large",
            Self::InvalidFrame => "invalid_frame",
        }
    }
}
impl fmt::Display for IngressError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}
impl std::error::Error for IngressError {}
/// Applies the operational cap before the canonical Router wire decoder
/// (sidecar.rs:144-151).
pub fn validate_router_ingress(frame: &[u8]) -> Result<(), IngressError> {
    if frame.len() > REMOTE_OPERATIONAL_FRAME_BYTES {
        return Err(IngressError::FrameTooLarge);
    }
    router_wire::decode_frame(frame)
        .map(|_| ())
        .map_err(|_| IngressError::InvalidFrame)
}
