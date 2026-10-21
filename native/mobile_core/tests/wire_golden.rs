// SPDX-License-Identifier: AGPL-3.0-only
//! Router wire golden frames (`contracts/router-wire-golden`), shared with the
//! Python and native iroh implementations.

use mycelium_mobile_core::wire::{self, Activation, RemoteKind};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn golden_dir() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contracts/router-wire-golden")
}

#[test]
fn every_golden_frame_decodes_and_reencodes_byte_identically() {
    let index: Value =
        serde_json::from_str(&std::fs::read_to_string(golden_dir().join("index.json")).unwrap()).unwrap();
    let frames = index["frames"].as_array().unwrap();
    assert_eq!(frames.len(), 11);
    for entry in frames {
        let bytes = std::fs::read(golden_dir().join(entry["file"].as_str().unwrap())).unwrap();
        assert_eq!(hex::encode(Sha256::digest(&bytes)), entry["frame_sha256"].as_str().unwrap());
        let frame = wire::decode_frame(&bytes).unwrap();
        assert_eq!(frame.message_type, entry["message_type"].as_str().unwrap());
        assert_eq!(frame.payload.len() as u64, entry["payload_length"].as_u64().unwrap());
        let again = wire::encode_frame(&frame.message_type, &frame.body, &frame.payload).unwrap();
        assert_eq!(again, bytes, "{}", entry["file"]);
    }
}

#[test]
fn tampered_frames_fail_closed() {
    let bytes = std::fs::read(golden_dir().join("01-hop-header.bin")).unwrap();
    let mut payload_flip = bytes.clone();
    *payload_flip.last_mut().unwrap() ^= 1;
    assert_eq!(wire::decode_frame(&payload_flip).unwrap_err().0, "payload_digest_mismatch");
    assert_eq!(wire::decode_frame(&bytes[..bytes.len() - 1]).unwrap_err().0, "payload_length_mismatch");
    assert_eq!(wire::decode_frame(&bytes[..3]).unwrap_err().0, "truncated_frame");
    assert_eq!(wire::encode_frame("Bogus", &Value::Null, b"").unwrap_err().0, "unknown_message_type");
}

#[test]
fn activation_and_remote_frames_round_trip_and_reject_corruption() {
    let rows = vec![vec![0.5, -1.25, 3.0], vec![1.0, 2.0, -0.0]];
    let act = Activation::from_rows_f32(&rows);
    assert_eq!(act.shape, vec![1, 2, 3]);
    let enc = act.encode().unwrap();
    assert_eq!(&enc[..8], b"MYAC\x01\x03\x03\x01");
    let dec = Activation::decode(&enc).unwrap();
    assert_eq!(dec.to_rows_f64().unwrap(), rows);
    assert_eq!(Activation::decode(&enc[..enc.len() - 1]).unwrap_err().0, "activation_payload_length_mismatch");
    let mut bad = enc.clone();
    bad[5] = 9;
    assert_eq!(Activation::decode(&bad).unwrap_err().0, "unknown_activation_dtype_code");

    let transfer = wire::transfer_payload(7, b"frame");
    let remote = wire::encode_remote(RemoteKind::Transfer, [9; 16], &transfer).unwrap();
    let back = wire::decode_remote(&remote).unwrap();
    assert_eq!(back.kind, RemoteKind::Transfer);
    assert_eq!(wire::split_transfer(&back.payload).unwrap(), (7, &b"frame"[..]));
    assert_eq!(wire::decode_remote(&remote[..remote.len() - 1]).unwrap_err().0, "length_mismatch");
}
