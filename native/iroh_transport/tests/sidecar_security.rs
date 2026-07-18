// SPDX-License-Identifier: AGPL-3.0-or-later

use mycelium_iroh_transport::local::{
    LOCAL_PROTOCOL, RecordKind, SequenceGuard, client_proof, decode_record, derive_session_keys,
    encode_record, server_proof,
};
use mycelium_iroh_transport::sidecar::{OPERATIONAL_MAX_FRAME_BYTES, validate_router_ingress};

const SECRET: [u8; 32] = [0x11; 32];
const CLIENT_NONCE: [u8; 32] = [0x22; 32];
const SERVER_NONCE: [u8; 32] = [0x33; 32];
const MESSAGE_ID: [u8; 16] = [0x44; 16];

#[test]
fn hmac_handshake_is_domain_separated_and_hkdf_keys_are_directional() {
    assert_eq!(LOCAL_PROTOCOL, "mycelium.iroh_sidecar.local.v1");
    let client = client_proof(&SECRET, &CLIENT_NONCE);
    let server = server_proof(&SECRET, &CLIENT_NONCE, &SERVER_NONCE, "endpoint-public-key");
    assert_ne!(client.as_slice(), server.as_slice());

    let keys = derive_session_keys(&SECRET, &CLIENT_NONCE, &SERVER_NONCE).expect("derive keys");
    assert_ne!(keys.client_to_sidecar, keys.sidecar_to_client);
}

#[test]
fn authenticated_records_reject_tampering_replay_and_sequence_gaps() {
    let keys = derive_session_keys(&SECRET, &CLIENT_NONCE, &SERVER_NONCE).expect("derive keys");
    let encoded = encode_record(
        RecordKind::Ping,
        0,
        MESSAGE_ID,
        b"bounded",
        &keys.client_to_sidecar,
    )
    .expect("encode");

    let mut guard = SequenceGuard::new();
    let decoded = decode_record(&encoded, &keys.client_to_sidecar, &mut guard).expect("decode");
    assert_eq!(decoded.sequence, 0);
    assert_eq!(decoded.message_id, MESSAGE_ID);
    assert_eq!(decoded.payload, b"bounded");

    assert!(decode_record(&encoded, &keys.client_to_sidecar, &mut guard).is_err());

    let skipped = encode_record(
        RecordKind::Ping,
        2,
        MESSAGE_ID,
        b"gap",
        &keys.client_to_sidecar,
    )
    .expect("encode gap");
    let mut fresh_guard = SequenceGuard::new();
    assert!(decode_record(&skipped, &keys.client_to_sidecar, &mut fresh_guard).is_err());

    let mut tampered = encoded;
    tampered[10] ^= 1;
    let mut fresh_guard = SequenceGuard::new();
    assert!(decode_record(&tampered, &keys.client_to_sidecar, &mut fresh_guard).is_err());
}

#[test]
fn router_ingress_uses_canonical_decoder_and_16_mib_operational_cap() {
    assert_eq!(OPERATIONAL_MAX_FRAME_BYTES, 16 * 1024 * 1024);
    assert!(validate_router_ingress(&[]).is_err());
    assert!(validate_router_ingress(&vec![0; OPERATIONAL_MAX_FRAME_BYTES + 1]).is_err());
}
