// SPDX-License-Identifier: AGPL-3.0-only
//! Byte-compatibility with the Python implementations, using fixtures produced by
//! `tests/fixtures/generate_fixtures.py` from the repository's own modules.

use mycelium_mobile_core::{canonical, identity, onboarding};
use serde_json::Value;
use x25519_dalek::StaticSecret;

fn fixture(name: &str) -> Value {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn seed(hex_str: &str) -> [u8; 32] {
    hex::decode(hex_str).unwrap().try_into().unwrap()
}

#[test]
fn canonical_json_matches_python_including_floats_and_escapes() {
    let f = fixture("signature.json");
    let bytes = canonical::to_bytes(&f["statement"]).unwrap();
    assert_eq!(String::from_utf8(bytes).unwrap(), f["canonical_utf8"].as_str().unwrap());
}

#[test]
fn signatures_are_byte_identical_to_python_signer() {
    let f = fixture("signature.json");
    let id = identity::Identity::from_secret(
        f["endpoint_id"].as_str().unwrap(),
        &seed(f["seed_hex"].as_str().unwrap()),
    )
    .unwrap();
    assert_eq!(id.public_key_record(), f["public_key_record"]);
    assert_eq!(id.sign(&f["statement"]).unwrap(), f["signature"]);
    identity::verify(&f["statement"], &f["signature"], &f["public_key_record"]).unwrap();

    let mut tampered = f["statement"].clone();
    tampered["int"] = Value::from(43);
    assert_eq!(
        identity::verify(&tampered, &f["signature"], &f["public_key_record"]),
        Err(identity::IdentityError::Signature)
    );
}

#[test]
fn membership_envelope_signature_matches_python() {
    let f = fixture("join_envelope.json");
    let sig = fixture("signature.json");
    let id = identity::Identity::from_secret(
        "device-endpoint-1",
        &seed(sig["seed_hex"].as_str().unwrap()),
    )
    .unwrap();
    assert_eq!(id.sign(&f["message"]).unwrap(), f["signature"]);
    identity::verify(&f["message"], &f["signature"], &f["verification_key"]).unwrap();
}

#[test]
fn pairing_request_is_byte_identical_and_handoff_decrypts() {
    let f = fixture("onboarding.json");
    let sig = fixture("signature.json");
    let id = identity::Identity::from_secret(
        "device-endpoint-1",
        &seed(sig["seed_hex"].as_str().unwrap()),
    )
    .unwrap();
    let request = &f["request"];
    let recipient = StaticSecret::from(seed(f["recipient_private_hex"].as_str().unwrap()));
    let session = onboarding::PairingSession::with_recipient(
        &id,
        request["device_ref"].as_str().unwrap(),
        request["issued_at"].as_f64().unwrap(),
        300,
        request["pairing_nonce"].as_str().unwrap(),
        recipient,
    )
    .unwrap();
    assert_eq!(&session.request, request);
    assert_eq!(session.request_digest().unwrap(), f["request_digest"].as_str().unwrap());

    let now = f["now"].as_f64().unwrap();
    let payload = session.decrypt(&f["handoff"], now).unwrap();
    assert_eq!(payload, f["payload"]);
    let invite = onboarding::verify_invite_bundle(&payload["invite_bundle"], now).unwrap();
    assert_eq!(invite.seed_url(), "http://127.0.0.1:18765");
    assert_eq!(invite.nonce(), request["pairing_nonce"].as_str().unwrap());

    // QR round trip carries the exact canonical request.
    let qr = session.request_qr().unwrap();
    assert!(qr.starts_with("mycelium:"));
    assert_eq!(&onboarding::decode_qr(&qr).unwrap(), request);
}

#[test]
fn handoff_negative_cases_fail_closed_with_python_codes() {
    use onboarding::OnboardingError as E;
    let f = fixture("onboarding.json");
    let request = &f["request"];
    let handoff = &f["handoff"];
    let recipient = StaticSecret::from(seed(f["recipient_private_hex"].as_str().unwrap()));
    let now = f["now"].as_f64().unwrap();

    let mut ct = handoff.clone();
    let mut raw = base64_decode(ct["ciphertext"].as_str().unwrap());
    raw[0] ^= 1;
    ct["ciphertext"] = Value::from(base64_encode(&raw));
    assert_eq!(onboarding::decrypt_handoff(request, &ct, &recipient, now), Err(E::HandoffInvalid));

    let mut aad = handoff.clone();
    aad["expires_at"] = Value::from(aad["expires_at"].as_f64().unwrap() - 1.0);
    assert_eq!(onboarding::decrypt_handoff(request, &aad, &recipient, now), Err(E::HandoffInvalid));

    let wrong = StaticSecret::from([0x44u8; 32]);
    assert_eq!(onboarding::decrypt_handoff(request, handoff, &wrong, now), Err(E::RecipientMismatch));

    let expires = handoff["expires_at"].as_f64().unwrap();
    assert_eq!(
        onboarding::decrypt_handoff(request, handoff, &recipient, expires + 1.0),
        Err(E::HandoffExpired)
    );

    let mut other = request.clone();
    other["device_ref"] = Value::from("device-ref-2");
    assert_eq!(
        onboarding::decrypt_handoff(&other, handoff, &recipient, now),
        Err(E::RequestSignature)
    );

    let mut termux = request.clone();
    termux["carrier"] = Value::from("termux");
    assert_eq!(onboarding::verify_request(&termux, now), Err(E::RequestSignature));

    let mut extra = handoff.clone();
    extra["device_brand"] = Value::from("x");
    assert_eq!(onboarding::decrypt_handoff(request, &extra, &recipient, now), Err(E::HandoffInvalid));
}

fn base64_decode(s: &str) -> Vec<u8> {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(s).unwrap()
}
fn base64_encode(b: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b)
}

#[test]
fn join_request_builder_reproduces_the_python_signed_envelope() {
    use mycelium_mobile_core::membership::{self, MemberConfig};
    let f = fixture("join_envelope.json");
    let sig = fixture("signature.json");
    let id = identity::Identity::from_secret("device-endpoint-1", &seed(sig["seed_hex"].as_str().unwrap())).unwrap();
    let m = &f["message"];
    let cfg = MemberConfig {
        swarm_id: m["swarm_id"].as_str().unwrap().into(),
        node_id: m["sender_node_id"].as_str().unwrap().into(),
        seed_node_id: m["recipient_node_id"].as_str().unwrap().into(),
        incarnation: m["incarnation"].as_str().unwrap().into(),
        software_version: m["software_version"].as_str().unwrap().into(),
        peer_class: m["peer_class"].as_str().unwrap().into(),
        runtime_capability: m["runtime_capability"].clone(),
        ttl_seconds: 60.0,
    };
    let env = membership::join_request(
        &cfg,
        &id,
        m["message_id"].as_str().unwrap(),
        m["invite_nonce"].as_str().unwrap(),
        &["127.0.0.1:1".to_owned()],
        m["issued_at"].as_f64().unwrap(),
    )
    .unwrap();
    assert_eq!(env, f);
}

#[test]
fn resume_request_requires_a_fresh_incarnation_and_signs_with_the_member_key() {
    use mycelium_mobile_core::membership::{self, MemberConfig};
    let sig = fixture("signature.json");
    let id = identity::Identity::from_secret("device-endpoint-1", &seed(sig["seed_hex"].as_str().unwrap())).unwrap();
    let cfg = MemberConfig {
        swarm_id: "swarm-a".into(),
        node_id: "node-a".into(),
        seed_node_id: "seed-node".into(),
        incarnation: "inc-new".into(),
        software_version: "v".into(),
        peer_class: "android_native_iroh".into(),
        runtime_capability: serde_json::json!({"runtime_backend": "mobile-rust-reference", "transport": "iroh", "activation_protocol": "mycelium.router_wire.v1"}),
        ttl_seconds: 60.0,
    };
    let addrs = ["ip:127.0.0.1:1".to_owned()];
    let env = membership::resume_request(&cfg, &id, "m1", "inc-old", 3, &addrs, 10.0).unwrap();
    assert_eq!(env["message"]["protocol"], membership::RESUME_REQUEST_PROTOCOL);
    assert_eq!(env["message"]["generation"], 3);
    assert_eq!(env["message"]["previous_incarnation"], "inc-old");
    identity::verify(&env["message"], &env["signature"], &env["verification_key"]).unwrap();
    assert_eq!(membership::resume_request(&cfg, &id, "m1", "inc-new", 3, &addrs, 10.0).unwrap_err().0, "membership_resume_incarnation_invalid");
    assert_eq!(membership::resume_request(&cfg, &id, "m1", "inc-old", 0, &addrs, 10.0).unwrap_err().0, "membership_generation_invalid");
}
