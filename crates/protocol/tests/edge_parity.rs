// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{
    TimeLimits,
    artifact_grant::{self, GrantLimits},
    identity,
    membership::{self, Expected},
};
use serde_json::Value;
#[test]
fn identity_base64_and_empty_endpoint_match_python_verifier() {
    let cases: Value =
        serde_json::from_str(include_str!("../fixtures/identity-edge-cases.json")).unwrap();
    let mut differences = Vec::new();
    for c in cases.as_array().unwrap() {
        let actual =
            identity::verify(&c["statement"], &c["signature"], &c["public_key_record"]).is_ok();
        if actual != c["valid"].as_bool().unwrap() {
            differences.push(c["name"].clone());
        }
    }
    assert!(differences.is_empty(), "{differences:?}");
}
#[test]
fn grant_addresses_match_python_not_just_rust_socketaddr() {
    let cases: Value =
        serde_json::from_str(include_str!("../fixtures/grant-address-cases.json")).unwrap();
    let f: Value = serde_json::from_str(include_str!("../fixtures/artifact_grant.json")).unwrap();
    for c in cases.as_array().unwrap() {
        let result = artifact_grant::verify_grant(
            &c["envelope"],
            &f["seed_key"],
            f["now_unix_ms"].as_u64().unwrap(),
            GrantLimits {
                max_grant_ttl_ms: 21600000,
                max_request_ttl_ms: 120000,
                request_ttl_ms: 60000,
                clock_skew_ms: 30000,
            },
        );
        assert_eq!(
            result.err().map(|e| e.0).unwrap_or("ok"),
            c["expected"].as_str().unwrap(),
            "{}",
            c["address"]
        );
    }
}
#[test]
fn membership_verifier_failure_precedence_matches_python() {
    let cases: Value =
        serde_json::from_str(include_str!("../fixtures/membership-verifier-cases.json")).unwrap();
    for case in cases.as_array().unwrap() {
        let c = &case["context"];
        let result = membership::verify(
            &case["envelope"],
            Expected {
                protocol: c["expected_protocol"].as_str().unwrap(),
                key_digest: c["expected_key_digest"].as_str().unwrap(),
                swarm_id: c["expected_swarm_id"].as_str(),
                sender_node_id: c["expected_sender_node_id"].as_str(),
                sender_endpoint_id: c["expected_sender_endpoint_id"].as_str(),
                recipient_node_id: c["expected_recipient_node_id"].as_str(),
            },
            c["now"].as_f64().unwrap(),
            TimeLimits {
                max_ttl_seconds: 3600.0,
                clock_skew_seconds: 30.0,
            },
        );
        assert_eq!(
            result.err().map(|e| e.0).unwrap_or("ok"),
            case["expected"].as_str().unwrap()
        );
    }
}
#[test]
fn registered_signed_message_fixture_verifies() {
    let env: Value = serde_json::from_str(include_str!(
        "../fixtures/contracts/membership-signed-message-v1.json"
    ))
    .unwrap();
    membership::verify(
        &env,
        Expected {
            protocol: "mycelium.membership.join_request.v1",
            key_digest: env["verification_key"]["verification_key_digest"]
                .as_str()
                .unwrap(),
            swarm_id: Some("swarm-fixture"),
            sender_node_id: Some("node-a"),
            sender_endpoint_id: Some("endpoint-node-a"),
            recipient_node_id: Some("seed-node"),
        },
        1000.0,
        TimeLimits {
            max_ttl_seconds: 3600.0,
            clock_skew_seconds: 30.0,
        },
    )
    .unwrap();
}
