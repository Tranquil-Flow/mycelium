// SPDX-License-Identifier: AGPL-3.0-or-later

use std::fs;
use std::path::{Path, PathBuf};

use mycelium_iroh_transport::protocol::{
    MAX_HEADER_BYTES, MAX_PAYLOAD_BYTES, MessageType, WireError, decode_frame, encode_frame,
};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

#[derive(Debug, Deserialize)]
struct GoldenIndex {
    protocol: String,
    frames: Vec<GoldenEntry>,
}

#[derive(Debug, Deserialize)]
struct GoldenEntry {
    byte_length: usize,
    file: String,
    frame_sha256: String,
    message_type: String,
    payload_length: usize,
    payload_sha256: String,
}

fn fixture_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join("contracts/router-wire-golden")
}

fn index() -> GoldenIndex {
    let bytes = fs::read(fixture_dir().join("index.json")).expect("read golden index");
    serde_json::from_slice(&bytes).expect("parse golden index")
}

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn python_golden_frames_decode_and_reencode_byte_identically() {
    let index = index();
    assert_eq!(index.protocol, "mycelium.router_wire.v1");
    assert_eq!(index.frames.len(), 10);

    for entry in index.frames {
        let frame = fs::read(fixture_dir().join(&entry.file)).expect("read golden frame");
        assert_eq!(frame.len(), entry.byte_length, "{}", entry.file);
        assert_eq!(sha256(&frame), entry.frame_sha256, "{}", entry.file);

        let decoded = decode_frame(&frame).expect("decode Python golden frame");
        assert_eq!(decoded.message_type.as_str(), entry.message_type);
        assert_eq!(
            decoded.payload.len(),
            entry.payload_length,
            "{}",
            entry.file
        );
        assert_eq!(
            sha256(&decoded.payload),
            entry.payload_sha256,
            "{}",
            entry.file
        );

        let encoded = encode_frame(&decoded.message_type, &decoded.body, &decoded.payload)
            .expect("reencode Python golden frame");
        assert_eq!(encoded, frame, "{}", entry.file);
    }
}

#[test]
fn rust_constructed_frame_uses_python_float_spelling() {
    let frame = golden_frame("07-reservation-result.bin");
    let mut decoded = decode_frame(&frame).expect("decode valid golden");
    decoded.body.insert("expires_at".into(), json!(1e-7_f64));
    decoded.body.insert(
        "float_cases".into(),
        json!([
            1e-20_f64,
            1e-7_f64,
            1e-6_f64,
            1e-5_f64,
            1e-4_f64,
            1e15_f64,
            1e16_f64,
            1e20_f64,
            -0.0_f64,
            1.0_f64,
            1.2345678901234567_f64
        ]),
    );
    decoded.body.insert("string_case".into(), json!("1e-7"));

    let encoded = encode_frame(&decoded.message_type, &decoded.body, &decoded.payload)
        .expect("encode Rust-created floats");
    let header_length =
        u32::from_be_bytes(encoded[..4].try_into().expect("header prefix")) as usize;
    let header = std::str::from_utf8(&encoded[4..4 + header_length]).expect("UTF-8 header");
    assert!(header.contains("\"expires_at\":1e-07"), "{header}");
    assert!(
        header.contains(
            "\"float_cases\":[1e-20,1e-07,1e-06,1e-05,0.0001,1000000000000000.0,1e+16,1e+20,-0.0,1.0,1.2345678901234567]"
        ),
        "{header}"
    );
    assert!(header.contains("\"string_case\":\"1e-7\""), "{header}");
}

#[test]
fn parsed_numeric_lexemes_are_normalized_to_python_canonical_values() {
    let frame = golden_frame("07-reservation-result.bin");
    let decoded = decode_frame(&frame).expect("decode valid golden");
    for (source, expected) in [
        ("1.2300", "1.23"),
        ("1e0", "1.0"),
        ("1E+0002", "100.0"),
        ("0.0000100", "1e-05"),
        ("1.0000000000000000000000000000000000000001", "1.0"),
        ("-0", "0"),
    ] {
        let mut body = decoded.body.clone();
        let number = serde_json::from_str(source).expect("parse test number");
        body.insert("expires_at".into(), number);
        let encoded = encode_frame(&decoded.message_type, &body, &decoded.payload)
            .expect("encode canonical numeric value");
        let header_length =
            u32::from_be_bytes(encoded[..4].try_into().expect("header prefix")) as usize;
        let header = std::str::from_utf8(&encoded[4..4 + header_length]).expect("UTF-8 header");
        assert!(
            header.contains(&format!("\"expires_at\":{expected}")),
            "source={source}: {header}"
        );
    }

    let mut body = decoded.body;
    body.insert(
        "expires_at".into(),
        serde_json::from_str("1e400").expect("parse overflowing test number"),
    );
    assert_eq!(
        encode_frame(&decoded.message_type, &body, &decoded.payload),
        Err(WireError::InvalidWireMessage)
    );
}

#[test]
fn rust_reencoded_frames_are_emitted_for_python() {
    let output = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/router-wire-rust-output");
    if output.exists() {
        fs::remove_dir_all(&output).expect("remove stale Rust output");
    }
    fs::create_dir_all(&output).expect("create Rust output");

    for entry in index().frames {
        let frame = fs::read(fixture_dir().join(&entry.file)).expect("read golden frame");
        let decoded = decode_frame(&frame).expect("decode Python golden frame");
        let encoded = encode_frame(&decoded.message_type, &decoded.body, &decoded.payload)
            .expect("encode Rust compatibility frame");
        fs::write(output.join(entry.file), encoded).expect("write Rust compatibility frame");
    }

    let body = json!({
        "accepted": true,
        "deployment_epoch": 11,
        "expires_at": 1e-7_f64,
        "reason": "",
        "reservation_id": "rust-created-réservation-🌙"
    })
    .as_object()
    .expect("Rust-created body object")
    .clone();
    let payload = b"\x00rust-produced\xff";
    let encoded = encode_frame(&MessageType::ReservationResult, &body, payload)
        .expect("encode independently Rust-created frame");
    fs::write(output.join("rust-created-reservation-result.bin"), encoded)
        .expect("write independently Rust-created frame");
}

fn golden_frame(filename: &str) -> Vec<u8> {
    fs::read(fixture_dir().join(filename)).expect("read golden frame")
}

fn raw_frame(envelope: &Value, payload: &[u8]) -> Vec<u8> {
    let header = serde_json::to_vec(envelope).expect("encode test envelope");
    let mut frame = Vec::with_capacity(4 + header.len() + payload.len());
    frame.extend_from_slice(&(header.len() as u32).to_be_bytes());
    frame.extend_from_slice(&header);
    frame.extend_from_slice(payload);
    frame
}

fn mutate_header(frame: &[u8], mutate: impl FnOnce(&mut Map<String, Value>)) -> Vec<u8> {
    let header_length = u32::from_be_bytes(frame[..4].try_into().expect("header prefix")) as usize;
    let mut envelope: Value =
        serde_json::from_slice(&frame[4..4 + header_length]).expect("parse golden header");
    mutate(envelope.as_object_mut().expect("object envelope"));
    raw_frame(&envelope, &frame[4 + header_length..])
}

#[test]
fn malformed_prefix_header_and_json_fail_closed() {
    assert_eq!(decode_frame(&[]), Err(WireError::TruncatedFrame));
    assert_eq!(decode_frame(&[0, 0, 0]), Err(WireError::TruncatedFrame));
    assert_eq!(
        decode_frame(&[0, 0, 0, 2, b'{']),
        Err(WireError::TruncatedHeader)
    );
    assert_eq!(
        decode_frame(&[0, 0, 0, 1, 0xff]),
        Err(WireError::InvalidWireJson)
    );
    assert_eq!(
        decode_frame(&[0, 0, 0, 1, b'{']),
        Err(WireError::InvalidWireJson)
    );
    assert_eq!(
        decode_frame(&raw_frame(&json!([]), &[])),
        Err(WireError::InvalidWireEnvelope)
    );
}

#[test]
fn envelope_protocol_type_and_fields_fail_closed() {
    let frame = golden_frame("01-hop-header.bin");
    for field in [
        "protocol",
        "message_type",
        "body",
        "payload_length",
        "payload_sha256",
    ] {
        let changed = mutate_header(&frame, |envelope| {
            envelope.remove(field);
        });
        assert_eq!(
            decode_frame(&changed),
            Err(WireError::MissingWireField(field.into())),
            "{field}"
        );
    }

    let changed = mutate_header(&frame, |envelope| {
        envelope.insert("protocol".into(), json!("mycelium.router_wire.v2"));
    });
    assert_eq!(decode_frame(&changed), Err(WireError::UnknownWireProtocol));

    let changed = mutate_header(&frame, |envelope| {
        envelope.insert("message_type".into(), json!("UnknownMessage"));
    });
    assert_eq!(decode_frame(&changed), Err(WireError::UnknownMessageType));

    let changed = mutate_header(&frame, |envelope| {
        envelope.insert("body".into(), json!([]));
    });
    assert_eq!(decode_frame(&changed), Err(WireError::InvalidWireBody));
}

#[test]
fn payload_lengths_bounds_and_digest_fail_closed() {
    let frame = golden_frame("01-hop-header.bin");
    for invalid in [json!(-1), json!(true), json!(1.5), json!("12")] {
        let changed = mutate_header(&frame, |envelope| {
            envelope.insert("payload_length".into(), invalid);
        });
        assert_eq!(decode_frame(&changed), Err(WireError::InvalidPayloadLength));
    }

    let too_large = mutate_header(&frame, |envelope| {
        envelope.insert("payload_length".into(), json!(MAX_PAYLOAD_BYTES + 1));
    });
    assert_eq!(decode_frame(&too_large), Err(WireError::PayloadTooLarge));

    let beyond_u64 = mutate_header(&frame, |envelope| {
        envelope.insert(
            "payload_length".into(),
            serde_json::from_str("18446744073709551616").expect("parse oversized integer"),
        );
    });
    assert_eq!(decode_frame(&beyond_u64), Err(WireError::PayloadTooLarge));

    let decoded = decode_frame(&frame).expect("decode valid golden");
    let oversized_payload = vec![0; MAX_PAYLOAD_BYTES + 1];
    assert_eq!(
        encode_frame(&decoded.message_type, &decoded.body, &oversized_payload),
        Err(WireError::PayloadTooLarge)
    );

    let at_limit_without_payload =
        mutate_header(&golden_frame("02-manifest-delta.bin"), |envelope| {
            envelope.insert("payload_length".into(), json!(MAX_PAYLOAD_BYTES));
        });
    assert_eq!(
        decode_frame(&at_limit_without_payload),
        Err(WireError::PayloadLengthMismatch)
    );

    let mut header_too_large = Vec::from(((MAX_HEADER_BYTES + 1) as u32).to_be_bytes());
    header_too_large.extend_from_slice(b"{}");
    assert_eq!(
        decode_frame(&header_too_large),
        Err(WireError::HeaderTooLarge)
    );

    let mut trailing = frame.clone();
    trailing.push(0);
    assert_eq!(
        decode_frame(&trailing),
        Err(WireError::PayloadLengthMismatch)
    );

    let mut short = frame.clone();
    short.pop();
    assert_eq!(decode_frame(&short), Err(WireError::PayloadLengthMismatch));

    let wrong_digest = mutate_header(&frame, |envelope| {
        envelope.insert("payload_sha256".into(), json!("00".repeat(32)));
    });
    assert_eq!(
        decode_frame(&wrong_digest),
        Err(WireError::PayloadDigestMismatch)
    );
}

fn required_body_fields(message_type: MessageType) -> &'static [&'static str] {
    match message_type {
        MessageType::HopHeader => &[
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
        MessageType::ManifestDelta => {
            &["request_id", "path_id", "path_attempt", "hop_index", "hop"]
        }
        MessageType::ManifestLocked => {
            &["request_id", "path_id", "path_attempt", "manifest", "build"]
        }
        MessageType::ProgressivePrefillMessage => &[
            "header",
            "graph",
            "request",
            "ordered_hops",
            "excluded_placements",
            "excluded_edges",
            "excluded_devices",
        ],
        MessageType::PrefillChunkCompleted => &[
            "request_id",
            "path_id",
            "path_attempt",
            "chunk_index",
            "token_count",
        ],
        MessageType::ReservationRequest => &[
            "request_id",
            "path_id",
            "path_attempt",
            "placement_id",
            "kv_bytes",
            "deployment_epoch",
            "lease_expires_at",
        ],
        MessageType::ReservationResult | MessageType::ReservationCommitResult => &["accepted"],
        MessageType::TokenEvent => &[
            "request_id",
            "path_id",
            "path_attempt",
            "token_index",
            "token_id",
            "sampling_counter",
        ],
        MessageType::FailureReport => &[
            "request_id",
            "path_id",
            "path_attempt",
            "token_index",
            "scope",
            "reason",
        ],
    }
}

#[test]
fn missing_required_body_fields_fail_closed_for_every_message_type() {
    for entry in index().frames {
        let frame = golden_frame(&entry.file);
        let decoded = decode_frame(&frame).expect("decode valid golden");
        for field in required_body_fields(decoded.message_type) {
            let changed = mutate_header(&frame, |envelope| {
                envelope
                    .get_mut("body")
                    .and_then(Value::as_object_mut)
                    .expect("object body")
                    .remove(*field);
            });
            assert_eq!(
                decode_frame(&changed),
                Err(WireError::MissingWireField((*field).into())),
                "{}.{field}",
                entry.message_type
            );
        }
    }
}

#[test]
fn encoder_accepts_exact_header_limit_and_rejects_one_byte_more() {
    let frame = golden_frame("01-hop-header.bin");
    let mut decoded = decode_frame(&frame).expect("decode valid golden");
    decoded.body.insert("padding".into(), json!(""));
    let base = encode_frame(&decoded.message_type, &decoded.body, &[]).expect("encode base");
    let base_header_length =
        u32::from_be_bytes(base[..4].try_into().expect("header prefix")) as usize;
    let padding_length = MAX_HEADER_BYTES - base_header_length;

    decoded
        .body
        .insert("padding".into(), json!("x".repeat(padding_length)));
    let at_limit =
        encode_frame(&decoded.message_type, &decoded.body, &[]).expect("encode at header limit");
    assert_eq!(
        u32::from_be_bytes(at_limit[..4].try_into().expect("header prefix")) as usize,
        MAX_HEADER_BYTES
    );
    let decoded_at_limit = decode_frame(&at_limit).expect("decode frame at header limit");
    assert_eq!(decoded_at_limit.message_type, decoded.message_type);
    assert_eq!(decoded_at_limit.body, decoded.body);
    assert!(decoded_at_limit.payload.is_empty());

    decoded
        .body
        .insert("padding".into(), json!("x".repeat(padding_length + 1)));
    assert_eq!(
        encode_frame(&decoded.message_type, &decoded.body, &[]),
        Err(WireError::HeaderTooLarge)
    );
}
