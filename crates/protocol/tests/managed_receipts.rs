// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{ContractError, TimeLimits, canonical, identity::Identity, managed_receipt};
use serde_json::{Value, json};
fn verify(row: &Value, env: &Value) -> Result<Value, ContractError> {
    let c = &row["context"];
    let expected = managed_receipt::ExpectedReceipt {
        kind: match c["operation"].as_str().unwrap() {
            "enqueue" => managed_receipt::ReceiptKind::Enqueue,
            "poll" => managed_receipt::ReceiptKind::Poll,
            "ack" => managed_receipt::ReceiptKind::Ack,
            "result" => managed_receipt::ReceiptKind::Result,
            _ => panic!("bad fixture"),
        },
        owner_key: &c["key"],
        request_id: c["request_id"].as_str().unwrap(),
        swarm_id: c["swarm_id"].as_str().unwrap(),
        owner_node_id: c["owner_node_id"].as_str().unwrap(),
        binding: &c["binding"],
        native_command: &c["command"],
        now: c["now"].as_f64().unwrap(),
    };
    managed_receipt::verify(
        env,
        &expected,
        TimeLimits {
            max_ttl_seconds: 300.0,
            clock_skew_seconds: 30.0,
        },
    )
}
fn envelope(v: &Value) -> Result<Value, ContractError> {
    let who = Identity::from_secret("endpoint-owner", &[17; 32]).unwrap();
    // The oracle producer's dict(statement) maps empty iterable inputs to {}.
    let detached = if v == "" || v.as_array().is_some_and(Vec::is_empty) {
        json!({})
    } else {
        v.clone()
    };
    let v = &detached;
    Ok(
        json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"statement":v,"signature":who.sign(v).map_err(|_|ContractError("invalid_signed_statement"))?,"verification_key":who.public_key_record()}),
    )
}
#[test]
fn signed_managed_receipts_match_oracle_and_refuse_full_mutation_corpus() {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/continuation-managed-receipts.json"
    ))
    .unwrap();
    for r in rows {
        let env = envelope(&r["value"]).unwrap();
        assert_eq!(
            canonical::to_bytes(&env).unwrap(),
            r["envelope_utf8"].as_str().unwrap().as_bytes()
        );
        assert_eq!(verify(&r, &env).unwrap(), r["value"]);
        for case in r["cases"].as_array().unwrap() {
            let got = envelope(&case["value"]).and_then(|e| verify(&r, &e));
            check(&r, case, got);
        }
        for case in r["envelope_cases"].as_array().unwrap() {
            check(&r, case, verify(&r, &case["value"]));
        }
    }
}
fn check(row: &Value, case: &Value, got: Result<Value, ContractError>) {
    assert_eq!(
        got.is_ok(),
        case["accepted"].as_bool().unwrap(),
        "{} / {}: {got:?} oracle={}",
        row["name"],
        case["name"],
        case["error"]
    );
    if let Some(code) = case["error"]
        .as_str()
        .filter(|s| s.starts_with("managed_control_"))
    {
        assert_eq!(
            got.unwrap_err().0,
            code,
            "{} / {}",
            row["name"],
            case["name"]
        );
    }
}
