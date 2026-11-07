// SPDX-License-Identifier: AGPL-3.0-only
//! External oracle differential corpus; no Python execution in this test.
use protocol::{ContractError, canonical, native, schemas};
use serde_json::Value;

fn validate(row: &Value, value: &Value) -> Result<(), ContractError> {
    match row["kind"].as_str().unwrap() {
        "registered" => {
            schemas::validate_as(row["value"]["protocol"].as_str().unwrap(), value, 8_000)
        }
        "native_command" => native::validate_command(value),
        "native_response" => native::validate_response(
            value,
            row["context"]["command_id"].as_str().unwrap(),
            row["context"]["node_id"].as_str().unwrap(),
            row["context"]["disposition"].as_str().unwrap(),
        ),
        "progress" => native::validate_progress(value, row["context"]["node_id"].as_str().unwrap()),
        "policy_payload" => native::validate_policy_payload(value),
        other => panic!("unmapped corpus {other}"),
    }
}
fn corpus(raw: &str) {
    let rows: Vec<Value> = serde_json::from_str(raw).unwrap();
    for row in &rows {
        assert_eq!(
            validate(row, &row["value"]),
            Ok(()),
            "positive {}",
            row["name"]
        );
        assert_eq!(
            canonical::to_bytes(&row["value"]).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for case in row["cases"].as_array().unwrap() {
            let got = validate(row, &case["value"]);
            assert_eq!(
                got.is_ok(),
                case["accepted"].as_bool().unwrap(),
                "{} / {}: {got:?}; oracle={}",
                row["name"],
                case["name"],
                case["error"]
            );
            // Incidental Python TypeError/KeyError paths have no stable public code.
            // Still require a refusal, never bless an exception as acceptance.
            if !case["accepted"].as_bool().unwrap()
                && !matches!(
                    case["exception"].as_str(),
                    Some("TypeError" | "KeyError" | "AttributeError")
                )
                && case["error"] != "false"
            {
                assert_eq!(
                    got.unwrap_err().0,
                    case["error"].as_str().unwrap(),
                    "{} / {}",
                    row["name"],
                    case["name"]
                );
            }
        }
    }
}
#[test]
fn registered_cancellation_liveness_and_internet_schema_corpus() {
    corpus(include_str!("../fixtures/continuation-schema-vectors.json"));
}
#[test]
fn managed_native_provisioning_and_progress_corpus() {
    corpus(include_str!("../fixtures/continuation-native-vectors.json"));
}
