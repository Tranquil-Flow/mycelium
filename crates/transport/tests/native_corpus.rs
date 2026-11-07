// SPDX-License-Identifier: AGPL-3.0-only
//! Recorded native codec characterization; not real carrier qualification.
use protocol::remote::{RemoteKind, decode_remote_frame, encode_remote_frame};
use transport::qualification::ROUTER_NATIVE_CORPUS_TSV;
fn cases() -> impl Iterator<Item = Vec<&'static str>> {
    ROUTER_NATIVE_CORPUS_TSV
        .lines()
        .filter(|s| !s.is_empty() && !s.starts_with('#'))
        .map(|s| s.split('\t').collect())
}
fn bytes(s: &str) -> Vec<u8> {
    assert_eq!(s.len() % 2, 0);
    s.as_bytes()
        .chunks_exact(2)
        .map(|b| u8::from_str_radix(std::str::from_utf8(b).unwrap(), 16).unwrap())
        .collect()
}
#[test]
fn frozen_native_corpus_matches_the_shared_codec_and_source_ack_policy() {
    let mut names = std::collections::BTreeSet::new();
    for row in cases() {
        assert_eq!(row.len(), 5);
        assert!(names.insert(row[0]));
        let raw = bytes(row[1]);
        let parsed = decode_remote_frame(&raw);
        let attempt = match parsed {
            Err(e) => {
                assert_eq!(e.code(), row[2], "{}", row[0]);
                assert_eq!(row[3], "-");
                "Retry"
            }
            Ok(f) => {
                assert_eq!(row[2], "ok", "{}", row[0]);
                assert_eq!((f.kind as u8).to_string(), row[3]);
                assert_eq!(
                    encode_remote_frame(f.kind, f.message_id, &f.payload).unwrap(),
                    raw,
                    "{}",
                    row[0]
                );
                // Source sidecar.rs:2682-2700. This test-only predicate is not a network adapter.
                if f.message_id != [5; 16] {
                    "Retry"
                } else if f.kind == RemoteKind::Ack && f.payload.is_empty() {
                    "Delivered"
                } else if f.kind == RemoteKind::Error && f.payload == b"peer_rotated" {
                    "PeerRotated"
                } else if f.kind == RemoteKind::Error && f.payload == b"replay_collision" {
                    "ReplayCollision"
                } else {
                    "Retry"
                }
            }
        };
        assert_eq!(attempt, row[4], "{}", row[0]);
    }
    let provenance = transport::qualification::ROUTER_NATIVE_PROVENANCE_JSON;
    let digest = protocol::canonical::sha256_prefixed(ROUTER_NATIVE_CORPUS_TSV.as_bytes());
    assert!(provenance.contains(&format!("\"corpus_sha256\": \"{}\"", &digest[7..])));
    assert!(provenance.contains(&format!("\"rows\": {},", names.len())));
    assert!(
        names.contains("ack_payload")
            && names.contains("generation_transfer")
            && names.contains("wrong_id")
    );
}
#[test]
fn source_generation_is_big_endian_inside_transfer_not_the_message_id() {
    let row = cases().find(|r| r[0] == "generation_transfer").unwrap();
    let frame = decode_remote_frame(&bytes(row[1])).unwrap();
    let mut payload = 3u64.to_be_bytes().to_vec();
    payload.extend_from_slice(b"\x00\xffMYAC");
    assert_eq!(frame.message_id, [5; 16]);
    assert_eq!(frame.kind, RemoteKind::Transfer);
    assert_eq!(frame.payload, payload);
    assert_eq!(
        encode_remote_frame(RemoteKind::Transfer, [5; 16], &payload).unwrap(),
        bytes(row[1])
    );
}
#[test]
fn corrupted_recorded_success_cannot_be_an_acknowledgement() {
    let row = cases().find(|r| r[0] == "ack_empty").unwrap();
    let raw = bytes(row[1]);
    for i in [0, 1, 18] {
        let mut mutant = raw.clone();
        mutant[i] = 255;
        assert!(decode_remote_frame(&mutant).is_err());
    }
    let payload_ack = cases().find(|r| r[0] == "ack_payload").unwrap();
    assert_eq!(payload_ack[2], "ok");
    assert_eq!(payload_ack[4], "Retry");
}
