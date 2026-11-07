// SPDX-License-Identifier: AGPL-3.0-only
//! Oracle-vector tests for the v0.1.4 sidecar local-IPC record layer, handshake and
//! payload shapes, and the sidecar telemetry documents. Every expected value comes
//! from `fixtures/sidecar-local-vectors.json`, produced by an independent
//! stdlib-only generator (`L9-evidence/r8-vector-generator.py`) from the v0.1.4
//! sources; the Rust code never produced an expected value.
use runtime::{
    sidecar_local::{
        ControlCallLimits, LocalProtocolError, RecordKind, SequenceGuard, client_hello,
        decode_record, delivery_from_payload, delivery_payload, derive_session_keys, encode_record,
        error_payload, hmac_sha256, parse_control_call, parse_send, server_hello,
        validate_router_ingress, verify_client_hello, verify_server_hello,
    },
    sidecar_telemetry::{
        ObservationState, TransportEvent, admission_counters_json, document_json, envelope_json,
        update_selected_path,
    },
};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("../fixtures/sidecar-local-vectors.json")).unwrap()
}
fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}
fn arr<const N: usize>(text: &str) -> [u8; N] {
    unhex(text).try_into().unwrap()
}
fn text(value: &Value) -> &str {
    value.as_str().unwrap()
}

#[test]
fn hmac_sha256_matches_the_rfc4231_style_vectors_including_a_key_longer_than_a_block() {
    let v = vectors();
    for case in v["hmac"].as_array().unwrap() {
        let mac = hmac_sha256(
            &unhex(text(&case["key_hex"])),
            &unhex(text(&case["data_hex"])),
        );
        assert_eq!(mac.to_vec(), unhex(text(&case["mac_hex"])));
    }
}

#[test]
fn handshake_proofs_and_session_keys_equal_the_independent_derivation() {
    let v = vectors();
    let h = &v["handshake"];
    let secret: [u8; 32] = arr(text(&h["secret_hex"]));
    let (cn, sn): ([u8; 32], [u8; 32]) = (
        arr(text(&h["client_nonce_hex"])),
        arr(text(&h["server_nonce_hex"])),
    );
    // The client hello a v0.1.4 Python client sends verifies; the nonce comes back.
    let python_hello = text(&h["client_hello_json"]).as_bytes();
    assert_eq!(verify_client_hello(python_hello, &secret).unwrap(), cn);
    // Our own hello carries the same proof (field order is not part of the contract).
    let ours: Value = serde_json::from_slice(&client_hello(&secret, &cn).unwrap()).unwrap();
    assert_eq!(text(&ours["client_proof"]), text(&h["client_proof_hex"]));
    assert_eq!(text(&ours["protocol"]), "mycelium.iroh_sidecar.local.v1");
    // Wrong secret / wrong protocol / unknown field / short nonce are all refused.
    assert_eq!(
        verify_client_hello(python_hello, &[9; 32]),
        Err(LocalProtocolError::InvalidProof)
    );
    let wrong = text(&h["client_hello_json"]).replace("local.v1", "local.v2");
    assert_eq!(
        verify_client_hello(wrong.as_bytes(), &secret),
        Err(LocalProtocolError::UnknownProtocol)
    );
    let extra = text(&h["client_hello_json"]).replacen('{', "{\"x\":1,", 1);
    assert_eq!(
        verify_client_hello(extra.as_bytes(), &secret),
        Err(LocalProtocolError::InvalidHello)
    );
    assert_eq!(
        verify_client_hello(b"not json", &secret),
        Err(LocalProtocolError::InvalidHello)
    );
    // The server hello binds the endpoint id and verifies on the client side.
    let hello = server_hello(&secret, &cn, &sn, text(&h["endpoint_id"])).unwrap();
    let parsed: Value = serde_json::from_slice(&hello).unwrap();
    assert_eq!(text(&parsed["server_proof"]), text(&h["server_proof_hex"]));
    let (nonce, endpoint) = verify_server_hello(&hello, &secret, &cn).unwrap();
    assert_eq!((nonce, endpoint.as_str()), (sn, "public"));
    let swapped = String::from_utf8(hello)
        .unwrap()
        .replace("public", "private");
    assert_eq!(
        verify_server_hello(swapped.as_bytes(), &secret, &cn),
        Err(LocalProtocolError::InvalidProof)
    );
    let keys = derive_session_keys(&secret, &cn, &sn).unwrap();
    assert_eq!(
        keys.client_to_sidecar.to_vec(),
        unhex(text(&h["client_to_sidecar_hex"]))
    );
    assert_eq!(
        keys.sidecar_to_client.to_vec(),
        unhex(text(&h["sidecar_to_client_hex"]))
    );
    assert_eq!(
        derive_session_keys(&secret[..31], &cn, &sn).err(),
        Some(LocalProtocolError::InvalidSecretLength)
    );
}

#[test]
fn every_record_kind_encodes_to_the_independent_bytes_and_decodes_back() {
    let v = vectors();
    let records = v["records"].as_array().unwrap();
    assert_eq!(records.len(), 38);
    for case in records {
        let kind = RecordKind::try_from(case["kind"].as_u64().unwrap() as u8).unwrap();
        let key: [u8; 32] = arr(text(&case["key_hex"]));
        let (sequence, id): (u64, [u8; 16]) = (
            case["sequence"].as_u64().unwrap(),
            arr(text(&case["message_id_hex"])),
        );
        let payload = unhex(text(&case["payload_hex"]));
        let encoded = encode_record(kind, sequence, id, &payload, &key).unwrap();
        assert_eq!(encoded, unhex(text(&case["encoded_hex"])), "kind {kind:?}");
        let mut guard = SequenceGuard::new();
        // A guard that expects `sequence` is advanced by accepting the earlier ones.
        for earlier in 0..sequence {
            guard.accept(earlier).unwrap();
        }
        let record = decode_record(&encoded, &key, &mut guard).unwrap();
        assert_eq!(
            (
                record.kind,
                record.sequence,
                record.message_id,
                record.payload
            ),
            (kind, sequence, id, payload)
        );
    }
}

#[test]
fn malformed_records_are_refused_and_do_not_advance_the_sequence() {
    let v = vectors();
    let n = &v["record_negative"];
    let key: [u8; 32] = arr(text(&n["key_hex"]));
    let mut guard = SequenceGuard::new();
    let refuse = |name: &str, guard: &mut SequenceGuard| {
        decode_record(&unhex(text(&n[name])), &key, guard).unwrap_err()
    };
    assert_eq!(
        refuse("trailing_byte_hex", &mut guard),
        LocalProtocolError::RecordLengthMismatch
    );
    // v0.1.4 reports a failed tag as `invalid_proof` (local.rs verify_tag); the
    // `invalid_record_tag` code exists but decode never produces it.
    assert_eq!(
        refuse("bad_tag_hex", &mut guard),
        LocalProtocolError::InvalidProof
    );
    assert_eq!(
        refuse("bad_version_hex", &mut guard),
        LocalProtocolError::UnknownRecordVersion
    );
    assert_eq!(
        refuse("unknown_kind_hex", &mut guard),
        LocalProtocolError::UnknownRecordKind
    );
    // The unknown-kind record had a valid tag and DID consume sequence 0 (the
    // sidecar checks the sequence before the kind): a replay of it is a bad sequence.
    assert_eq!(
        refuse("unknown_kind_hex", &mut guard),
        LocalProtocolError::InvalidSequence
    );
    // Too short; and a valid record at sequence 0 fails on a fresh guard only once.
    assert_eq!(
        decode_record(&[0; 10], &key, &mut SequenceGuard::new()).unwrap_err(),
        LocalProtocolError::RecordTooShort
    );
    assert_eq!(
        decode_record(&[0; 62], &[0; 31], &mut SequenceGuard::new()).unwrap_err(),
        LocalProtocolError::InvalidKeyLength
    );
    let ping = encode_record(RecordKind::Ping, 0, [1; 16], b"", &key).unwrap();
    let mut fresh = SequenceGuard::new();
    decode_record(&ping, &key, &mut fresh).unwrap();
    assert_eq!(
        decode_record(&ping, &key, &mut fresh).unwrap_err(),
        LocalProtocolError::InvalidSequence
    );
    assert_eq!(
        RecordKind::try_from(0),
        Err(LocalProtocolError::UnknownRecordKind)
    );
    assert_eq!(
        RecordKind::try_from(20),
        Err(LocalProtocolError::UnknownRecordKind)
    );
    assert_eq!(
        LocalProtocolError::InvalidRecordTag.code(),
        "invalid_record_tag"
    );
}

#[test]
fn the_sequence_guard_exhausts_instead_of_wrapping() {
    let mut guard = SequenceGuard::new();
    assert_eq!(guard.next_expected(), Some(0));
    assert_eq!(guard.accept(1), Err(LocalProtocolError::InvalidSequence));
    guard.accept(0).unwrap();
    assert_eq!(guard.next_expected(), Some(1));
    let mut last = SequenceGuard::at(u64::MAX);
    last.accept(u64::MAX).unwrap();
    assert_eq!(last.next_expected(), None);
    assert_eq!(
        last.accept(u64::MAX),
        Err(LocalProtocolError::SequenceExhausted)
    );
}

#[test]
fn payload_shapes_for_sends_deliveries_errors_and_control_calls() {
    let v = vectors();
    let dest = [0xAB_u8; 32];
    let frame = b"frame".to_vec();
    // Plain send: the payload is the Router frame.
    let plain = parse_send(RecordKind::Send, frame.clone()).unwrap();
    assert_eq!((plain.destination, plain.expected_generation), (None, None));
    assert_eq!(plain.frame, frame);
    // Confirmed: u64 BE expected generation, u64 BE source generation, then the frame.
    let confirmed = parse_send(
        RecordKind::SendConfirmed,
        unhex(text(&v["records"][16]["payload_hex"])),
    )
    .unwrap();
    assert_eq!(v["records"][16]["kind"], 9);
    assert_eq!(
        (confirmed.expected_generation, confirmed.source_generation),
        (Some(2), Some(3))
    );
    assert_eq!(confirmed.frame, frame);
    // Routed: 32 destination bytes first, then the confirmed layout.
    let routed = parse_send(
        RecordKind::SendRouted,
        unhex(text(&v["records"][20]["payload_hex"])),
    )
    .unwrap();
    assert_eq!(v["records"][20]["kind"], 11);
    assert_eq!(routed.destination, Some(dest));
    assert_eq!(routed.expected_generation, Some(2));
    // Refusals carry the sidecar's codes: short routed prefix, short or zero generations.
    assert_eq!(
        parse_send(RecordKind::SendRouted, vec![0; 31]).unwrap_err(),
        "invalid_peer"
    );
    assert_eq!(
        parse_send(RecordKind::SendConfirmed, vec![0; 15]).unwrap_err(),
        "invalid_generation"
    );
    let mut zero_source = vec![0; 16];
    zero_source[7] = 1;
    assert_eq!(
        parse_send(RecordKind::SendConfirmed, zero_source).unwrap_err(),
        "invalid_generation"
    );
    // Deliveries.
    assert_eq!(
        delivery_payload(3, b"frame"),
        unhex(text(&v["records"][4]["payload_hex"]))
    );
    assert_eq!(
        delivery_from_payload(Some([0xCD; 32]), 4, b"frame"),
        unhex(text(&v["records"][24]["payload_hex"]))
    );
    assert_eq!(
        delivery_from_payload(None, 4, b"x")[..32].to_vec(),
        vec![0; 32],
        "a local (unsourced) delivery names the all-zero source"
    );
    // Error payloads are exactly {"code":...}.
    for (code, expected) in v["error_payloads"].as_object().unwrap() {
        assert_eq!(error_payload(code), text(expected).as_bytes(), "{code}");
    }
    // ControlCall.
    let limits = ControlCallLimits {
        ceiling_millis: 60_000,
        max_command_bytes: 4 * 1024 * 1024,
    };
    let c = &v["control_call"];
    let ok = parse_control_call(&unhex(text(&c["ok"]["payload_hex"])), limits).unwrap();
    assert_eq!(ok.destination, arr::<32>(text(&c["destination_hex"])));
    assert_eq!(ok.millis, c["ok"]["millis"].as_u64().unwrap());
    assert_eq!(ok.command, unhex(text(&c["ok"]["command_hex"])));
    assert!(parse_control_call(&unhex(text(&c["ceiling_ok"]["payload_hex"])), limits).is_ok());
    for (name, code) in [
        ("timeout_exceeded", "control_timeout_exceeded"),
        ("zero_timeout", "invalid_control_call"),
        ("empty_command", "invalid_control_call"),
        ("too_short", "invalid_control_call"),
    ] {
        assert_eq!(
            parse_control_call(&unhex(text(&c[name])), limits).unwrap_err(),
            code,
            "{name}"
        );
    }
    let tight = ControlCallLimits {
        ceiling_millis: 60_000,
        max_command_bytes: 3,
    };
    assert_eq!(
        parse_control_call(&unhex(text(&c["ok"]["payload_hex"])), tight).unwrap_err(),
        "invalid_control_call"
    );
}

#[test]
fn router_ingress_applies_the_operational_cap_before_the_wire_decoder() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continued-wire-vectors.json"
    ))
    .unwrap();
    let frame = unhex(text(&fixture[0]["encoded_hex"]));
    assert!(validate_router_ingress(&frame).is_ok());
    assert_eq!(
        validate_router_ingress(b"not-a-router-frame")
            .unwrap_err()
            .code(),
        "invalid_frame"
    );
    assert_eq!(
        validate_router_ingress(&vec![0; 16 * 1024 * 1024 + 1])
            .unwrap_err()
            .code(),
        "frame_too_large"
    );
}

fn state_from(value: &Value) -> ObservationState {
    ObservationState {
        path_class: text(&value["path"]).to_owned(),
        relay_identity: value["relay"].as_str().map(str::to_owned),
        cold_rtt_ms: value["cold"].as_f64(),
        rtt_samples_ms: value["rtt"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect(),
        acknowledged_bytes: value["acked_bytes"].as_u64().unwrap(),
        acknowledged_elapsed_ms: value["acked_ms"].as_f64().unwrap(),
        attempts: value["attempts"].as_u64().unwrap(),
        failures: value["failures"].as_u64().unwrap(),
        connections_opened: value["opened"].as_u64().unwrap(),
        frames_sent: value["frames"].as_u64().unwrap(),
        reconnect_count: value["reconnects"].as_u64().unwrap(),
        selected_path_changes: value["changes"].as_u64().unwrap(),
        measured_at_unix_ms: value["at"].as_u64().unwrap(),
    }
}

#[test]
fn observation_documents_match_the_independent_arithmetic_byte_for_byte() {
    let v = vectors();
    let mut documents = Vec::new();
    for case in v["observations"].as_array().unwrap() {
        let state = state_from(&case["state"]);
        let json = document_json(
            text(&case["remote_endpoint_id"]),
            case["generation"].as_u64().unwrap(),
            Some(&state),
        );
        assert_eq!(
            json,
            text(&case["document_json"]),
            "{}",
            text(&case["name"])
        );
        documents.push(json);
    }
    // A peer that was configured but never used reports zeros and `unknown`.
    let never = &v["observations"][1];
    assert_eq!(
        document_json(text(&never["remote_endpoint_id"]), 1, None),
        text(&never["document_json"])
    );
    assert_eq!(
        envelope_json(&[documents[0].clone(), documents[1].clone()]),
        text(&v["envelope_json"])
    );
    assert_eq!(
        admission_counters_json(5, 7, 2, 1_700_000_000_999),
        text(&v["admission_counters_json"])
    );
}

#[test]
fn the_selected_path_change_counter_follows_the_sidecar_rule_and_rtt_history_is_bounded() {
    let v = vectors();
    for case in v["path_change_rule"].as_array().unwrap() {
        let mut state = ObservationState {
            path_class: text(&case["prior"]).to_owned(),
            ..ObservationState::default()
        };
        update_selected_path(&mut state, text(&case["new"]).to_owned(), None, None);
        assert_eq!(
            state.selected_path_changes,
            case["delta"].as_u64().unwrap(),
            "{case}"
        );
        assert_eq!(state.path_class, text(&case["new"]));
    }
    let mut state = ObservationState::default();
    for sample in 0..70 {
        update_selected_path(
            &mut state,
            "direct".to_owned(),
            None,
            Some(f64::from(sample)),
        );
    }
    assert_eq!(
        state.rtt_samples_ms.len(),
        64,
        "the window keeps 64 samples"
    );
    assert_eq!(state.rtt_samples_ms.front().copied(), Some(6.0));
    assert_eq!(state.cold_rtt_ms, Some(0.0), "cold rtt is the first sample");
    // Negative and non-finite samples are ignored.
    update_selected_path(&mut state, "direct".to_owned(), None, Some(-1.0));
    update_selected_path(&mut state, "direct".to_owned(), None, Some(f64::NAN));
    assert_eq!(state.rtt_samples_ms.len(), 64);
    assert_eq!(state.rtt_samples_ms.back().copied(), Some(69.0));
}

#[test]
fn transport_events_render_the_sidecar_stderr_lines() {
    let v = vectors();
    let remote: [u8; 32] = arr(&"ab".repeat(32));
    for case in v["events"].as_array().unwrap() {
        let event = match text(&case["event"]) {
            "transport_connection_opened" => TransportEvent::ConnectionOpened,
            "peer_connect_deadline_exceeded" => TransportEvent::PeerConnectDeadlineExceeded,
            other => panic!("unexpected event {other}"),
        };
        assert_eq!(event.name(), text(&case["event"]));
        assert_eq!(
            event.line(
                &hex_id(&remote),
                case["connection_generation"].as_u64().unwrap()
            ),
            text(&case["line"])
        );
    }
}
fn hex_id(bytes: &[u8; 32]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

// RU-010 / CR001: the record layer's MAC is the admitted `hmac` + `sha2` crates, not a
// local primitive. The vector tests above pin the bytes; these pin WHERE they come from.
const PRODUCT_SOURCE: &str = include_str!("../src/sidecar_local.rs");

#[test]
fn the_record_layer_mac_is_the_admitted_crates_and_not_a_local_hmac() {
    // The HMAC pad pair (RFC 2104 ipad 0x36 / opad 0x5c) and a block-size constant are the
    // marks of a hand-written HMAC; none may appear in product code.
    for pad in ["0x36", "0x5c", "0x5C", "SHA256_BLOCK", "tags_equal"] {
        assert!(
            !PRODUCT_SOURCE.contains(pad),
            "sidecar_local.rs still carries the hand-written HMAC mark {pad:?}"
        );
    }
    assert!(
        PRODUCT_SOURCE.contains("hmac::{Hmac, Mac}") || PRODUCT_SOURCE.contains("use hmac::"),
        "sidecar_local.rs must use the admitted hmac crate"
    );
    assert!(
        PRODUCT_SOURCE.contains("verify_slice"),
        "tag comparison must be the crate's constant-time verify_slice"
    );
}

#[test]
fn the_declared_dependencies_are_the_admitted_versions() {
    let manifest = include_str!("../Cargo.toml");
    assert!(
        manifest.contains("hmac = { version = \"=0.12.1\""),
        "{manifest}"
    );
    assert!(
        manifest.contains("sha2 = { version = \"=0.10.9\""),
        "{manifest}"
    );
}

#[test]
fn tag_verification_rejects_every_flipped_tag_bit_and_every_wrong_length() {
    let key = [7_u8; 32];
    let encoded = encode_record(RecordKind::Ping, 0, [3_u8; 16], b"x", &key).unwrap();
    assert!(decode_record(&encoded, &key, &mut SequenceGuard::new()).is_ok());
    for bit in 0..(32 * 8) {
        let mut bad = encoded.clone();
        let at = bad.len() - 32 + bit / 8;
        bad[at] ^= 1 << (bit % 8);
        assert_eq!(
            decode_record(&bad, &key, &mut SequenceGuard::new()).unwrap_err(),
            LocalProtocolError::InvalidProof,
            "tag bit {bit}"
        );
    }
    // A tag of any other length is refused by the declared-length check before the MAC.
    for cut in [1_usize, 16, 31] {
        let mut short = encoded.clone();
        short.truncate(short.len() - cut);
        assert!(decode_record(&short, &key, &mut SequenceGuard::new()).is_err());
    }
    let mut long = encoded.clone();
    long.push(0);
    assert_eq!(
        decode_record(&long, &key, &mut SequenceGuard::new()).unwrap_err(),
        LocalProtocolError::RecordLengthMismatch
    );
}
