// SPDX-License-Identifier: AGPL-3.0-only
//! Pure authenticated progress seam; no SQLite/member liveness/replay proof.
use control::{ManagedContext, MemberBinding, authenticate_request, managed_time_limits};
use protocol::{identity::Identity, managed, progress};
use serde_json::{Value, json};
fn fixture() -> Value {
    serde_json::from_str::<Value>(include_str!(
        "../../protocol/fixtures/f4-owner-retirement-vectors.json"
    ))
    .unwrap()[0]["value"]
        .clone()
}
fn envelope(v: &Value, signer: &Identity) -> Value {
    json!({"protocol":managed::SIGNED_ENVELOPE_PROTOCOL,"statement":v,"signature":signer.sign(v).unwrap(),"verification_key":signer.public_key_record()})
}
#[test]
fn progress_retirement_requires_target_authentication_before_claim_binding() {
    let f = fixture();
    let signer = Identity::from_secret("endpoint-entry", &[18; 32]).unwrap();
    let key = signer.public_key_record();
    let b = MemberBinding {
        target_node_id: "node-entry".into(),
        target_endpoint_id: "endpoint-entry".into(),
        target_key_digest: signer.verification_key_digest(),
        membership_generation: "2".into(),
        member_incarnation: "incarnation-2".into(),
        run_id: "run-1".into(),
        deployment_id: "deployment-1".into(),
    };
    let context = ManagedContext {
        protocol: "mycelium.managed_control.token_progress.v1",
        signer_key: &key,
        signer_endpoint: "endpoint-entry",
        swarm_id: "swarm-1",
        owner_node_id: "node-owner",
        binding: &b,
    };
    let mut statement = f["statement"].clone();
    for (k,v) in json!({"protocol":context.protocol,"swarm_id":"swarm-1","sequence":1,"nonce":"nonce-1","issued_at":1000.0,"expires_at":1060.0}).as_object().unwrap() {statement[k]=v.clone();}
    let valid = envelope(&statement, &signer);
    let limits = managed_time_limits().unwrap();
    let authenticated = authenticate_request(&valid, &context, 1000.0, limits).unwrap();
    let command: Value =
        serde_json::from_str(f["claim"]["envelope_json"].as_str().unwrap()).unwrap();
    let c = progress::RetirementContext {
        cancelled: true,
        closed: false,
        command_id: "command-1",
        grant_digest: f["grant_digest"].as_str().unwrap(),
        target_endpoint_id: "endpoint-entry",
        claim: &f["claim"],
        native_command: &command["statement"]["native_command"],
    };
    assert_eq!(
        progress::retirement_subject(&c, authenticated.statement())
            .unwrap()
            .unwrap()["protocol"],
        progress::CANCELLED
    );
    for field in [
        "target_node_id",
        "target_endpoint_id",
        "target_key_digest",
        "membership_generation",
        "member_incarnation",
        "run_id",
        "deployment_id",
        "swarm_id",
        "command_id",
        "claim_token",
    ] {
        let mut bad = statement.clone();
        bad[field] = match field {
            "membership_generation" => json!(3),
            "target_key_digest" => json!(format!("sha256:{}", "f".repeat(64))),
            _ => json!("wrong"),
        };
        let got = authenticate_request(&envelope(&bad, &signer), &context, 1000.0, limits)
            .and_then(|a| progress::retirement_subject(&c, a.statement()));
        assert!(!matches!(got, Ok(Some(_))), "{field}");
    }
    let mut bad = valid.clone();
    bad["signature"]["signature"] = json!("bad");
    assert!(authenticate_request(&bad, &context, 1000.0, limits).is_err());
    assert!(authenticate_request(&valid, &context, 1090.001, limits).is_err());
    for field in ["command_id", "claim_token", "receipt"] {
        let mut bad = statement.clone();
        bad.as_object_mut().unwrap().remove(field);
        assert!(authenticate_request(&envelope(&bad, &signer), &context, 1000.0, limits).is_err());
    }
}
