// SPDX-License-Identifier: AGPL-3.0-only
//! Executed v0.1.4 verifier outcomes, with real signatures from a fresh probe identity.
use api::installation_descriptor::verify_installation_descriptor;
use serde_json::{Value, json};

#[test]
fn signed_descriptors_match_executed_oracle_refusals() {
    let mut rows: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/oracle_descriptor_verification.json")).unwrap();
    rows.extend(
        serde_json::from_str::<Vec<Value>>(include_str!("fixtures/oracle_descriptor_padding.json"))
            .unwrap(),
    );
    for row in rows {
        let got = match verify_installation_descriptor(
            &row["card"],
            &row["member"],
            "swarm-1",
            "run-1",
            "dep-1",
            row["now"].as_f64().unwrap(),
        ) {
            Ok(facts) => json!({"ok":facts}),
            Err(error) => json!({"error":error.code}),
        };
        assert_eq!(got, row["expected"], "{}", row["name"]);
    }
}

#[test]
fn freshly_generated_member_signature_is_required_and_does_not_certify_hardware() {
    use api::installation_descriptor::{Member, Paths, Source, installation_descriptor};
    use protocol::identity::Identity;
    use std::io::Read;
    let mut secret = [0_u8; 32];
    std::fs::File::open("/dev/urandom")
        .unwrap()
        .read_exact(&mut secret)
        .unwrap();
    let signer = Identity::from_secret("endpoint-fresh", &secret).unwrap();
    let member = Member {
        node_id: "member-fresh".into(),
        endpoint_id: signer.endpoint_id().into(),
        verification_key_digest: signer.verification_key_digest(),
        generation: 1,
        incarnation: "inc-fresh".into(),
    };
    let current = json!({"node_id":member.node_id,"endpoint_id":member.endpoint_id,"verification_key_digest":member.verification_key_digest,"generation":1,"incarnation":member.incarnation,"lease_expires_at":2000});
    let mut card = installation_descriptor(
        &signer,
        &member,
        "swarm-1",
        "run-1",
        "dep-1",
        true,
        &Source::Whole {
            source_root: "/scratch/source".into(),
            manifest_digest: format!("sha256:{}", "ab".repeat(32)),
        },
        &Paths {
            state_root: "/scratch/state".into(),
            endpoint_secret_file: "/scratch/key".into(),
            python_executable: "/scratch/host".into(),
            sidecar_binary: "/scratch/sidecar".into(),
        },
        1000.0,
    )
    .unwrap();
    let facts =
        verify_installation_descriptor(&card, &current, "swarm-1", "run-1", "dep-1", 1000.0)
            .unwrap();
    assert_eq!(facts["route_ready"], false);
    assert!(facts.get("host_id").is_none());
    assert!(facts.get("boot_id").is_none());
    let expected: String = signer
        .public_key_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(facts["activation_endpoint_id"], expected);
    card["statement"]["facts"]["source_root"] = json!("/tampered");
    assert_eq!(
        verify_installation_descriptor(&card, &current, "swarm-1", "run-1", "dep-1", 1000.0)
            .unwrap_err()
            .code,
        "installation_descriptor_signature_invalid"
    );
}
