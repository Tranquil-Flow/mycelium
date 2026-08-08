// SPDX-License-Identifier: AGPL-3.0-or-later
//! Authenticated protocol used on the local Unix-domain socket.
//!
//! The handshake is a pair of u32-length-prefixed JSON objects.  All following
//! messages are u32-length-prefixed records encoded by [`encode_record`].  Byte
//! strings in handshake JSON are lowercase hexadecimal so the protocol is easy
//! to implement without a binary JSON extension.

use std::error::Error;
use std::fmt;

use hkdf::Hkdf;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use zeroize::{Zeroize, ZeroizeOnDrop};

pub const LOCAL_PROTOCOL: &str = "mycelium.iroh_sidecar.local.v1";
pub const LOCAL_RECORD_VERSION: u8 = 1;
pub const LOCAL_MAX_PAYLOAD_BYTES: usize = 16 * 1024 * 1024 + 16;
pub const LOCAL_RECORD_HEADER_BYTES: usize = 1 + 1 + 8 + 16 + 4;
pub const LOCAL_RECORD_TAG_BYTES: usize = 32;
pub const LOCAL_MAX_RECORD_BYTES: usize =
    LOCAL_RECORD_HEADER_BYTES + LOCAL_MAX_PAYLOAD_BYTES + LOCAL_RECORD_TAG_BYTES;
pub const MAX_HELLO_BYTES: usize = 4 * 1024;

pub const CLIENT_PROOF_DOMAIN: &[u8] = b"mycelium.iroh_sidecar.local.v1/client-proof\0";
pub const SERVER_PROOF_DOMAIN: &[u8] = b"mycelium.iroh_sidecar.local.v1/server-proof\0";
pub const SESSION_KEYS_INFO: &[u8] = b"mycelium.iroh_sidecar.local.v1/session-keys";

const CLIENT_TO_SIDECAR_INFO: &[u8] = b"mycelium.iroh_sidecar.local.v1/client-to-sidecar";
const SIDECAR_TO_CLIENT_INFO: &[u8] = b"mycelium.iroh_sidecar.local.v1/sidecar-to-client";

type HmacSha256 = Hmac<Sha256>;

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
}

impl TryFrom<u8> for RecordKind {
    type Error = LocalProtocolError;

    fn try_from(value: u8) -> Result<Self, LocalProtocolError> {
        match value {
            1 => Ok(Self::Send),
            2 => Ok(Self::Receive),
            3 => Ok(Self::Delivery),
            4 => Ok(Self::Ack),
            5 => Ok(Self::Cancel),
            6 => Ok(Self::ConfigurePeer),
            7 => Ok(Self::Ping),
            8 => Ok(Self::Error),
            9 => Ok(Self::SendConfirmed),
            10 => Ok(Self::ConfigurePeers),
            11 => Ok(Self::SendRouted),
            _ => Err(LocalProtocolError::UnknownRecordKind),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Record {
    pub kind: RecordKind,
    pub sequence: u64,
    pub message_id: [u8; 16],
    pub payload: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LocalProtocolError {
    InvalidSecretLength,
    InvalidNonceLength,
    InvalidKeyLength,
    InvalidHex,
    InvalidProof,
    KeyDerivation,
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
    pub const fn code(&self) -> &'static str {
        match self {
            Self::InvalidSecretLength => "invalid_secret_length",
            Self::InvalidNonceLength => "invalid_nonce_length",
            Self::InvalidKeyLength => "invalid_key_length",
            Self::InvalidHex => "invalid_hex",
            Self::InvalidProof => "invalid_proof",
            Self::KeyDerivation => "key_derivation_failed",
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

impl Error for LocalProtocolError {}

#[derive(Clone, Zeroize, ZeroizeOnDrop)]
pub struct SessionKeys {
    pub client_to_sidecar: [u8; 32],
    pub sidecar_to_client: [u8; 32],
}

impl fmt::Debug for SessionKeys {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("SessionKeys([REDACTED])")
    }
}

/// Client handshake object.  Nonces and proofs are lowercase hex strings.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClientHello {
    pub protocol: String,
    #[serde(alias = "nonce")]
    pub client_nonce: String,
    #[serde(alias = "proof")]
    pub client_proof: String,
}

/// Server handshake object.  Nonces and proofs are lowercase hex strings.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerHello {
    pub protocol: String,
    #[serde(alias = "nonce")]
    pub server_nonce: String,
    pub endpoint_id: String,
    #[serde(alias = "proof")]
    pub server_proof: String,
}

impl ClientHello {
    pub fn new(secret: &[u8], client_nonce: &[u8; 32]) -> Result<Self, LocalProtocolError> {
        Ok(Self {
            protocol: LOCAL_PROTOCOL.to_owned(),
            client_nonce: hex::encode(client_nonce),
            client_proof: hex::encode(client_proof_checked(secret, client_nonce)?),
        })
    }

    pub fn verify(&self, secret: &[u8]) -> Result<[u8; 32], LocalProtocolError> {
        if self.protocol != LOCAL_PROTOCOL {
            return Err(LocalProtocolError::UnknownProtocol);
        }
        let nonce = decode_hex_array::<32>(&self.client_nonce)?;
        let proof = decode_hex_array::<32>(&self.client_proof)?;
        let expected = client_proof_checked(secret, &nonce)?;
        verify_tag(&expected, &proof)?;
        Ok(nonce)
    }
}

impl ServerHello {
    pub fn new(
        secret: &[u8],
        client_nonce: &[u8; 32],
        server_nonce: &[u8; 32],
        endpoint_id: &str,
    ) -> Result<Self, LocalProtocolError> {
        Ok(Self {
            protocol: LOCAL_PROTOCOL.to_owned(),
            server_nonce: hex::encode(server_nonce),
            endpoint_id: endpoint_id.to_owned(),
            server_proof: hex::encode(server_proof_checked(
                secret,
                client_nonce,
                server_nonce,
                endpoint_id,
            )?),
        })
    }

    pub fn verify(
        &self,
        secret: &[u8],
        client_nonce: &[u8; 32],
    ) -> Result<[u8; 32], LocalProtocolError> {
        if self.protocol != LOCAL_PROTOCOL {
            return Err(LocalProtocolError::UnknownProtocol);
        }
        let nonce = decode_hex_array::<32>(&self.server_nonce)?;
        let proof = decode_hex_array::<32>(&self.server_proof)?;
        let expected = server_proof_checked(secret, client_nonce, &nonce, &self.endpoint_id)?;
        verify_tag(&expected, &proof)?;
        Ok(nonce)
    }
}

/// HMAC proving possession of the bootstrap secret by the client.
///
/// This compatibility helper accepts a fixed 32-byte secret.  Runtime code uses
/// the checked slice variant so malformed bootstrap input fails explicitly.
pub fn client_proof(secret: &[u8; 32], client_nonce: &[u8; 32]) -> [u8; 32] {
    client_proof_checked(secret, client_nonce).expect("32-byte HMAC keys are valid")
}

/// HMAC proving possession by the server and binding its public endpoint key.
pub fn server_proof(
    secret: &[u8; 32],
    client_nonce: &[u8; 32],
    server_nonce: &[u8; 32],
    endpoint_id: &str,
) -> [u8; 32] {
    server_proof_checked(secret, client_nonce, server_nonce, endpoint_id)
        .expect("32-byte HMAC keys are valid")
}

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
    let hkdf = Hkdf::<Sha256>::new(Some(&salt), secret);
    salt.zeroize();

    let mut client_to_sidecar = [0_u8; 32];
    let mut sidecar_to_client = [0_u8; 32];
    let mut client_info =
        Vec::with_capacity(SESSION_KEYS_INFO.len() + CLIENT_TO_SIDECAR_INFO.len());
    client_info.extend_from_slice(SESSION_KEYS_INFO);
    client_info.extend_from_slice(CLIENT_TO_SIDECAR_INFO);
    let mut server_info =
        Vec::with_capacity(SESSION_KEYS_INFO.len() + SIDECAR_TO_CLIENT_INFO.len());
    server_info.extend_from_slice(SESSION_KEYS_INFO);
    server_info.extend_from_slice(SIDECAR_TO_CLIENT_INFO);
    hkdf.expand(&client_info, &mut client_to_sidecar)
        .map_err(|_| LocalProtocolError::KeyDerivation)?;
    hkdf.expand(&server_info, &mut sidecar_to_client)
        .map_err(|_| LocalProtocolError::KeyDerivation)?;
    client_info.zeroize();
    server_info.zeroize();

    Ok(SessionKeys {
        client_to_sidecar,
        sidecar_to_client,
    })
}

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

    let mut encoded = Vec::with_capacity(LOCAL_RECORD_HEADER_BYTES + payload.len() + 32);
    encoded.push(LOCAL_RECORD_VERSION);
    encoded.push(kind as u8);
    encoded.extend_from_slice(&sequence.to_be_bytes());
    encoded.extend_from_slice(&message_id);
    encoded.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    encoded.extend_from_slice(payload);
    let tag = hmac(key, &encoded)?;
    encoded.extend_from_slice(&tag);
    Ok(encoded)
}

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

    let record_sequence = u64::from_be_bytes(
        encoded[2..10]
            .try_into()
            .map_err(|_| LocalProtocolError::RecordTooShort)?,
    );
    let message_id = encoded[10..26]
        .try_into()
        .map_err(|_| LocalProtocolError::RecordTooShort)?;
    let payload_len = u32::from_be_bytes(
        encoded[26..30]
            .try_into()
            .map_err(|_| LocalProtocolError::RecordTooShort)?,
    ) as usize;
    if payload_len > LOCAL_MAX_PAYLOAD_BYTES {
        return Err(LocalProtocolError::PayloadTooLarge);
    }
    let expected_len = LOCAL_RECORD_HEADER_BYTES
        .checked_add(payload_len)
        .and_then(|length| length.checked_add(LOCAL_RECORD_TAG_BYTES))
        .ok_or(LocalProtocolError::PayloadTooLarge)?;
    if encoded.len() != expected_len {
        return Err(LocalProtocolError::RecordLengthMismatch);
    }

    let signed_end = expected_len - LOCAL_RECORD_TAG_BYTES;
    let expected_tag = hmac(key, &encoded[..signed_end])?;
    verify_tag(&expected_tag, &encoded[signed_end..])?;
    sequence.accept(record_sequence)?;
    let kind = RecordKind::try_from(encoded[1])?;

    Ok(Record {
        kind,
        sequence: record_sequence,
        message_id,
        payload: encoded[LOCAL_RECORD_HEADER_BYTES..signed_end].to_vec(),
    })
}

#[derive(Clone, Debug, Default)]
pub struct SequenceGuard {
    next: u64,
    exhausted: bool,
}

impl SequenceGuard {
    pub const fn new() -> Self {
        Self {
            next: 0,
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

fn client_proof_checked(
    secret: &[u8],
    client_nonce: &[u8; 32],
) -> Result<[u8; 32], LocalProtocolError> {
    if secret.len() != 32 {
        return Err(LocalProtocolError::InvalidSecretLength);
    }
    let mut input = Vec::with_capacity(CLIENT_PROOF_DOMAIN.len() + client_nonce.len());
    input.extend_from_slice(CLIENT_PROOF_DOMAIN);
    input.extend_from_slice(client_nonce);
    hmac(secret, &input)
}

fn server_proof_checked(
    secret: &[u8],
    client_nonce: &[u8; 32],
    server_nonce: &[u8; 32],
    endpoint_id: &str,
) -> Result<[u8; 32], LocalProtocolError> {
    if secret.len() != 32 {
        return Err(LocalProtocolError::InvalidSecretLength);
    }
    let endpoint_len =
        u32::try_from(endpoint_id.len()).map_err(|_| LocalProtocolError::InvalidProof)?;
    let mut input = Vec::with_capacity(
        SERVER_PROOF_DOMAIN.len() + client_nonce.len() + server_nonce.len() + 4 + endpoint_id.len(),
    );
    input.extend_from_slice(SERVER_PROOF_DOMAIN);
    input.extend_from_slice(client_nonce);
    input.extend_from_slice(server_nonce);
    input.extend_from_slice(&endpoint_len.to_be_bytes());
    input.extend_from_slice(endpoint_id.as_bytes());
    hmac(secret, &input)
}

fn hmac(key: &[u8], input: &[u8]) -> Result<[u8; 32], LocalProtocolError> {
    let mut mac = <HmacSha256 as Mac>::new_from_slice(key)
        .map_err(|_| LocalProtocolError::InvalidKeyLength)?;
    mac.update(input);
    Ok(mac.finalize().into_bytes().into())
}

fn verify_tag(expected: &[u8; 32], supplied: &[u8]) -> Result<(), LocalProtocolError> {
    let mut mac = <HmacSha256 as Mac>::new_from_slice(expected)
        .map_err(|_| LocalProtocolError::InvalidKeyLength)?;
    mac.update(b"constant-time-comparison");
    let expected_comparison = mac.clone().finalize().into_bytes();

    let supplied_array: [u8; 32] = supplied
        .try_into()
        .map_err(|_| LocalProtocolError::InvalidProof)?;
    let mut supplied_mac = <HmacSha256 as Mac>::new_from_slice(&supplied_array)
        .map_err(|_| LocalProtocolError::InvalidKeyLength)?;
    supplied_mac.update(b"constant-time-comparison");
    supplied_mac
        .verify_slice(&expected_comparison)
        .map_err(|_| LocalProtocolError::InvalidProof)
}

fn decode_hex_array<const N: usize>(encoded: &str) -> Result<[u8; N], LocalProtocolError> {
    if encoded.len() != N * 2 || !encoded.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(LocalProtocolError::InvalidHex);
    }
    let decoded = hex::decode(encoded).map_err(|_| LocalProtocolError::InvalidHex)?;
    decoded
        .try_into()
        .map_err(|_| LocalProtocolError::InvalidHex)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hello_round_trip_and_wrong_secret_rejected() {
        let secret = [1_u8; 32];
        let client_nonce = [2_u8; 32];
        let server_nonce = [3_u8; 32];
        let client = ClientHello::new(&secret, &client_nonce).unwrap();
        assert_eq!(client.verify(&secret).unwrap(), client_nonce);
        assert!(client.verify(&[9_u8; 32]).is_err());

        let server = ServerHello::new(&secret, &client_nonce, &server_nonce, "public").unwrap();
        assert_eq!(server.verify(&secret, &client_nonce).unwrap(), server_nonce);
        assert!(server.verify(&[9_u8; 32], &client_nonce).is_err());
    }

    #[test]
    fn configure_peers_is_a_distinct_record_kind_and_unknown_kinds_stay_closed() {
        assert_eq!(RecordKind::ConfigurePeers as u8, 10);
        assert_eq!(
            RecordKind::try_from(10_u8).unwrap(),
            RecordKind::ConfigurePeers
        );
        assert_ne!(RecordKind::ConfigurePeers, RecordKind::ConfigurePeer);
        assert_eq!(RecordKind::SendRouted as u8, 11);
        assert_eq!(RecordKind::try_from(11_u8).unwrap(), RecordKind::SendRouted);
        assert_eq!(
            RecordKind::try_from(12_u8),
            Err(LocalProtocolError::UnknownRecordKind)
        );

        let key = [7_u8; 32];
        let encoded = encode_record(
            RecordKind::ConfigurePeers,
            0,
            [8_u8; 16],
            br#"{"peers":[]}"#,
            &key,
        )
        .unwrap();
        let mut guard = SequenceGuard::new();
        let decoded = decode_record(&encoded, &key, &mut guard).unwrap();
        assert_eq!(decoded.kind, RecordKind::ConfigurePeers);
        assert_eq!(decoded.payload, br#"{"peers":[]}"#);
    }

    #[test]
    fn record_parser_rejects_trailing_bytes_without_advancing_sequence() {
        let key = [7_u8; 32];
        let record = encode_record(RecordKind::Ping, 0, [8_u8; 16], b"", &key).unwrap();
        let mut with_trailing = record.clone();
        with_trailing.push(0);
        let mut guard = SequenceGuard::new();
        assert!(decode_record(&with_trailing, &key, &mut guard).is_err());
        assert!(decode_record(&record, &key, &mut guard).is_ok());
    }
}
