// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{
    TimeLimits, canonical,
    identity::Identity,
    managed,
    membership::{self, Expected},
    strict,
};
use serde_json::Value;

fn member_check(env: &Value, c: &Value) -> Result<Value, protocol::ContractError> {
    membership::verify(
        env,
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
    )
}
fn managed_check(env: &Value, c: &Value) -> Result<Value, protocol::ContractError> {
    managed::verify(
        env,
        c["expected_protocol"].as_str().unwrap(),
        &c["expected_key_record"],
        c["expected_endpoint_id"].as_str().unwrap(),
        c["now"].as_f64().unwrap(),
        TimeLimits {
            max_ttl_seconds: 300.0,
            clock_skew_seconds: 30.0,
        },
    )
}

#[test]
fn all_contract_canonical_bytes_match_external_oracle() {
    let rows: Value =
        serde_json::from_str(include_str!("../fixtures/canonical-contracts.json")).unwrap();
    assert_eq!(rows.as_array().unwrap().len(), 87);
    for row in rows.as_array().unwrap() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("fixtures/contracts")
            .join(row["path"].as_str().unwrap());
        let raw = std::fs::read(path).unwrap();
        let value = strict::object(&raw, raw.len()).unwrap();
        assert_eq!(
            canonical::to_bytes(&value).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes(),
            "{}",
            row["path"]
        );
    }
}

#[test]
fn membership_positive_bytes_and_96_oracle_refusals() {
    let rows: Value =
        serde_json::from_str(include_str!("../fixtures/membership-vectors.json")).unwrap();
    assert_eq!(rows.as_array().unwrap().len(), 12);
    for row in rows.as_array().unwrap() {
        let raw = row["canonical_utf8"].as_str().unwrap().as_bytes();
        let env = strict::canonical_object(raw, raw.len()).unwrap();
        let source_name = std::path::Path::new(row["source"].as_str().unwrap())
            .file_name()
            .unwrap();
        let original: Value = serde_json::from_slice(
            &std::fs::read(
                std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("fixtures/contracts")
                    .join(source_name),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(env["message"], original);
        assert_eq!(canonical::to_bytes(&env).unwrap(), raw);
        let id = Identity::from_secret(
            env["message"]["sender_endpoint_id"].as_str().unwrap(),
            &[0x11; 32],
        )
        .unwrap();
        assert_eq!(id.sign(&env["message"]).unwrap(), env["signature"]);
        assert_eq!(member_check(&env, &row["context"]).unwrap(), env["message"]);
        assert_eq!(row["negative"].as_array().unwrap().len(), 8);
        for case in row["negative"].as_array().unwrap() {
            let result = member_check(&case["envelope"], &case["context"]);
            let code = result.err().map(|e| e.0).unwrap_or("ok");
            assert_eq!(
                code,
                case["expected"].as_str().unwrap(),
                "{} {}",
                row["source"],
                case["name"]
            );
        }
    }
}

#[test]
fn managed_verified_statement_obeys_oracle_frame_cap() {
    let rows: Value =
        serde_json::from_str(include_str!("../fixtures/managed-vectors.json")).unwrap();
    let mut env = rows[0]["envelope"].clone();
    env["statement"]["padding"] = Value::from("x".repeat(managed::MAX_FRAME_BYTES));
    let id = Identity::from_secret("endpoint-owner", &[0x11; 32]).unwrap();
    env["signature"] = id.sign(&env["statement"]).unwrap();
    assert_eq!(
        managed_check(&env, &rows[0]["context"]).err().map(|e| e.0),
        Some("managed_control_frame_too_large")
    );
}

#[test]
fn managed_control_and_provisioning_bytes_and_refusals() {
    let rows: Value =
        serde_json::from_str(include_str!("../fixtures/managed-vectors.json")).unwrap();
    assert_eq!(rows.as_array().unwrap().len(), 2);
    for row in rows.as_array().unwrap() {
        let env = &row["envelope"];
        assert_eq!(
            canonical::to_bytes(env).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        let id = Identity::from_secret("endpoint-owner", &[0x11; 32]).unwrap();
        assert_eq!(id.sign(&env["statement"]).unwrap(), env["signature"]);
        assert_eq!(
            managed_check(env, &row["context"]).unwrap(),
            env["statement"]
        );
        for case in row["negative"].as_array().unwrap() {
            let result = managed_check(&case["envelope"], &case["context"]);
            let code = result.err().map(|e| e.0).unwrap_or("ok");
            assert_eq!(code, case["expected"].as_str().unwrap(), "{}", case["name"]);
        }
    }
}
