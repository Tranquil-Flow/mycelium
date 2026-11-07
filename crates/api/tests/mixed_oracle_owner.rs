// SPDX-License-Identifier: AGPL-3.0-only
//! Mixed run: the Rust member's join-window client against the ORACLE's owner (the certified v0.1.4
//! executable behind its external relay's loopback proxy). It is `#[ignore]`d because it needs the
//! oracle running: `L10-evidence/r15-mixed.py` starts the owner in its sandbox, opens the window, runs
//! this test with `L10_MIXED_BASE` (the proxy `http://127.0.0.1:<port>`) and `L10_MIXED_LINK` (the
//! `mycelium-join:` link) and approves the claim through the owner's own `/api/pair/window/approve`.
//! It proves, against the real v0.1.4 owner: the link parses, the link probe round trips, the
//! signed join report is accepted by `verify_capability_link_report`, the pairing request is accepted
//! by `verify_pairing_request`, and the oracle's encrypted handoff decrypts, verifies and is pinned to
//! the seed key the link carried. The seed leg (TLS to the public origin) is not reachable on a loopback proxy.
use api::{
    capability, handoff, join_client::JoinWindowClient, join_link::parse_join_text,
    persistent::identity_fingerprint,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use protocol::identity::Identity;
use serde_json::json;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}
fn ms(millis: u64) -> Duration {
    Duration::from_millis(millis)
}
fn secs(seconds: u64) -> Duration {
    Duration::from_secs(seconds)
}

#[test]
#[ignore = "needs the oracle owner: run L10-evidence/r15-mixed.py"]
fn the_rust_member_joins_the_oracle_owner_window_up_to_the_decrypted_handoff() {
    let base = std::env::var("L10_MIXED_BASE").expect("L10_MIXED_BASE");
    let text = std::env::var("L10_MIXED_LINK").expect("L10_MIXED_LINK");
    let out = std::env::var("L10_MIXED_OUT").expect("L10_MIXED_OUT");
    let (link, _) = parse_join_text(&json!(text), &json!(now())).expect("the oracle's link parses");
    let signer = Identity::from_secret("endpoint-rustmember", &[21u8; 32]).expect("key");
    let secret = [22u8; 32];
    let issued = now();
    let statement = json!({
        "protocol": "mycelium.pairing_request.v1", "node_id": "node-rustmember",
        "identity_endpoint_id": signer.endpoint_id(), "identity_key_digest": signer.verification_key_digest(),
        "identity_verification_key": signer.public_key_record(), "peer_class": "mac_mlx_iroh",
        "invite_protocol": "mycelium.invite_bundle.v1",
        "recipient_encryption_key": URL_SAFE_NO_PAD.encode(handoff::crypto::x25519_public(&secret)),
        "pairing_nonce": "pairing-rust-mixed-0001", "issued_at": issued, "expires_at": issued + 300.0,
    });
    let mut request = statement.clone();
    request["signature"] = signer.sign(&statement).expect("sign");
    let mut client = JoinWindowClient::new(&link.origin, api::join_client::default_timeout())
        .expect("client")
        .with_test_base_url(&base)
        .expect("test base");
    let started = std::time::Instant::now();
    let mut clock = move || started.elapsed().as_secs_f64();
    let measured = capability::measure_link(
        &mut |kib, up| client.probe(kib, up),
        3,
        256 * 1024,
        &mut clock,
    )
    .expect("the oracle owner answers the link probe");
    let device = json!({
        "chip": "Apple M4 Pro", "memory_bytes": 51_539_607_552u64, "available_storage_bytes": 400_000_000_000u64,
        "os_version": "26.5.1", "mlx": {"available": false, "device": null},
        "power": "unknown", "thermal": "unknown", "network": "unknown",
    });
    let report = capability::signed_report(
        &signer,
        "node-rustmember",
        "join",
        None,
        &device,
        &measured,
        now(),
        capability::report_ttl_seconds(),
        None,
    )
    .expect("report");
    let quota = json!({"storage_bytes": 2_000_000_000u64, "network_bytes_per_day": 2_000_000_000u64, "background_allowed": false});
    let answer = client
        .submit(&link.window_id, &link.code, &request, &report, &quota)
        .expect("the oracle's window accepts the Rust member's claim");
    let fingerprint = identity_fingerprint(&request["identity_key_digest"]).expect("fingerprint");
    assert_eq!(answer["state"], "waiting_for_owner", "{answer}");
    assert_eq!(answer["fingerprint"], fingerprint.as_str(), "{answer}");
    let digest = protocol::canonical::digest(&request).expect("digest");
    assert_eq!(
        answer["request_digest"],
        digest.as_str(),
        "the oracle computes the same request digest"
    );
    // the oracle's owner approves from the outside (r15-mixed.py); poll like the app does
    let deadline = std::time::Instant::now() + secs(90);
    let approved = loop {
        let polled = client
            .poll(&link.window_id, &link.code, &digest)
            .expect("poll answers");
        if polled["state"] == "approved" {
            break polled;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "the oracle owner never approved"
        );
        std::thread::sleep(ms(300));
    };
    // decrypt + verify every binding: request digest, recipient key, invite signature, seed pin, nonce
    let payload = handoff::decrypt_invitation_handoff(
        &request,
        &approved["handoff"],
        Some(&secret),
        &json!(now()),
    )
    .expect("the oracle's handoff decrypts and verifies in Rust");
    assert_eq!(payload["node_id"], "node-rustmember");
    assert_eq!(payload["quota"]["storage_bytes"], 2_000_000_000u64);
    assert_eq!(
        payload["seed_key_digest"].as_str(),
        link.seed_key_digest.as_deref(),
        "the handoff is pinned to the seed key the link carried"
    );
    let bundle = &payload["invite_bundle"];
    let verified = handoff::verify_invite_bundle(bundle, &json!(now())).expect("invite verifies");
    std::fs::write(
        out,
        json!({
            "submit": answer, "fingerprint": fingerprint, "seed_url": verified.payload["seed_url"],
            "swarm_id": verified.payload["swarm_id"], "pinned": link.seed_key_digest.is_some(),
            "warnings": client.warnings(),
        })
        .to_string(),
    )
    .expect("result written");
}
