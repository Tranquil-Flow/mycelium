// SPDX-License-Identifier: AGPL-3.0-only
//! The A2-over-iroh authority objects agree with Python byte for byte: a
//! Python seed grant, phone request and source receipt verify in Rust, and
//! Rust signs the identical request and receipt envelopes from the same inputs.

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use mycelium_mobile_core::artifact_grant::{self as ag, ChunkRequest};
use mycelium_mobile_core::identity::Identity;
use serde_json::{Value, json};

fn fixture() -> Value {
    let dir = std::env::var("MYCELIUM_MOBILE_FIXTURES").unwrap_or_else(|_| format!("{}/tests/fixtures", env!("CARGO_MANIFEST_DIR")));
    serde_json::from_str(&std::fs::read_to_string(format!("{dir}/artifact_grant.json")).unwrap()).unwrap()
}

fn member() -> Identity {
    Identity::from_secret(&"a".repeat(64), &[0x11; 32]).unwrap()
}

fn source() -> Identity {
    Identity::from_secret(&"c".repeat(64), &[0x33; 32]).unwrap()
}

const SIZE: u64 = 64 * 4; // first tensor of the synthetic manifest: a [64] f32 vector

fn sizes(digest: &str, f: &Value) -> Option<u64> {
    (digest == f["request"]["message"]["object_digest"].as_str().unwrap()).then_some(SIZE)
}

#[test]
fn python_grant_request_and_receipt_verify_and_rust_signs_identically() {
    let f = fixture();
    let now = f["now_unix_ms"].as_u64().unwrap();
    let grant = ag::verify_grant(&f["grant"], &f["seed_key"], now).unwrap();
    assert_eq!(grant.recipient_endpoint_id, "a".repeat(64));
    assert_eq!(grant.allowed_object_digests.len(), 24);
    grant.check_binding("node-phone-1", &"a".repeat(64), member().verification_key_digest().as_str(), grant.manifest_pack_digest.as_str()).unwrap();
    assert_eq!(grant.check_binding("node-other", &"a".repeat(64), &member().verification_key_digest(), "x").unwrap_err().0, "artifact_grant_recipient_mismatch");

    let src = source();
    let (request, checked) = ag::verify_chunk_request(&f["request"], &f["seed_key"], src.endpoint_id(), &src.verification_key_digest(), &|d| sizes(d, &f), now + 1_000).unwrap();
    assert_eq!(checked, grant);
    assert_eq!((request.offset_bytes, request.length_bytes), (16, 32));
    let ours = ag::sign_chunk_request(&member(), &grant, "chunk-request-1", "nonce-1", src.endpoint_id(), &request.object_digest, 16, 32, now + 1_000).unwrap();
    assert_eq!(ours, f["request"], "Rust signs the same request envelope as Python");

    let data = STANDARD.decode(f["data_b64"].as_str().unwrap()).unwrap();
    ag::verify_chunk_receipt(&f["receipt"], &request, &grant, &data).unwrap();
    assert_eq!(ag::sign_chunk_receipt(&src, &request, &grant, &data, now + 2_000).unwrap(), f["receipt"]);
    let mut flipped = data.clone();
    flipped[0] ^= 1;
    assert_eq!(ag::verify_chunk_receipt(&f["receipt"], &request, &grant, &flipped).unwrap_err().0, "artifact_chunk_receipt_mismatch");
}

#[test]
fn grants_and_requests_fail_closed() {
    let f = fixture();
    let now = f["now_unix_ms"].as_u64().unwrap();
    let seed = &f["seed_key"];
    let other = Identity::from_secret("seed-endpoint-1", &[0x44; 32]).unwrap().public_key_record();
    assert_eq!(ag::verify_grant(&f["grant"], &other, now).unwrap_err().0, "artifact_grant_signer_mismatch");
    let late = now + 900_000 + ag::CLOCK_SKEW_MS + 1;
    assert_eq!(ag::verify_grant(&f["grant"], seed, late).unwrap_err().0, "artifact_grant_ineligible");
    ag::verify_grant(&f["grant"], seed, now - ag::CLOCK_SKEW_MS).unwrap();
    let mut tampered = f["grant"].clone();
    tampered["message"]["maximum_chunk_bytes"] = json!(1);
    assert_eq!(ag::verify_grant(&tampered, seed, now).unwrap_err().0, "artifact_grant_signature_invalid");
    let mut unsorted = f["grant"].clone();
    unsorted["message"]["allowed_object_digests"].as_array_mut().unwrap().swap(0, 1);
    assert_eq!(ag::verify_grant(&unsorted, seed, now).unwrap_err().0, "artifact_grant_invalid");

    let grant = ag::verify_grant(&f["grant"], seed, now).unwrap();
    let src = source();
    let digest = f["request"]["message"]["object_digest"].as_str().unwrap().to_owned();
    let check = |env: &Value, key_digest: &str| {
        ag::verify_chunk_request(env, seed, src.endpoint_id(), key_digest, &|d| (d == digest).then_some(SIZE), now + 1_000).map(|_| ()).unwrap_err().0
    };
    let sign = |who: &Identity, offset: u64, len: u64, obj: &str, at: u64| ag::sign_chunk_request(who, &grant, "r", "n", src.endpoint_id(), obj, offset, len, at).unwrap();
    let key = src.verification_key_digest();
    let impostor = Identity::from_secret(&"a".repeat(64), &[0x55; 32]).unwrap();
    assert_eq!(check(&sign(&impostor, 0, 8, &digest, now), &key), "artifact_chunk_request_unauthorized");
    assert_eq!(check(&sign(&member(), 0, 8, &digest, now), &format!("sha256:{}", "0".repeat(64))), "artifact_chunk_request_unauthorized");
    assert_eq!(check(&sign(&member(), 0, 8, &format!("sha256:{}", "e".repeat(64)), now), &key), "artifact_chunk_request_unauthorized");
    assert_eq!(check(&sign(&member(), SIZE - 4, 8, &digest, now), &key), "artifact_chunk_range_invalid");
    assert_eq!(check(&sign(&member(), 0, 8, &digest, now - 200_000), &key), "artifact_chunk_request_ineligible");
    let mut forged = sign(&member(), 0, 8, &digest, now);
    forged["message"]["length_bytes"] = json!(16);
    assert_eq!(check(&forged, &key), "artifact_chunk_request_signature_invalid");
    // Receipt from a key the grant does not authorize.
    let req = ChunkRequest { request_id: "r".into(), source_endpoint_id: src.endpoint_id().into(), object_digest: digest.clone(), offset_bytes: 0, length_bytes: 8 };
    let rogue = Identity::from_secret(&"c".repeat(64), &[0x77; 32]).unwrap();
    let receipt = ag::sign_chunk_receipt(&rogue, &req, &grant, &[1; 8], now).unwrap();
    assert_eq!(ag::verify_chunk_receipt(&receipt, &req, &grant, &[1; 8]).unwrap_err().0, "artifact_chunk_receipt_signer_mismatch");
}
