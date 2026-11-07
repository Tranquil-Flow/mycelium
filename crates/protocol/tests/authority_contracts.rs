// SPDX-License-Identifier: AGPL-3.0-only
// O:contract:mycelium.seed_key_transition.v1; O:contract:mycelium.path_manifest.v1
use protocol::{ContractError, canonical, path_manifest, seed_transition};
use serde_json::{Value, json};
#[test]
fn seed_transition_dual_signature_pin_and_coercion_corpus() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/f4-seed-vectors.json")).unwrap();
    for row in rows {
        let now = &row["context"]["now"];
        let pin = row["context"]["pin"].as_str();
        assert_eq!(
            seed_transition::verify(&row["value"], now, pin).unwrap(),
            row["value"]["transition"]
        );
        assert_eq!(
            canonical::to_bytes(&row["value"]).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for case in row["cases"].as_array().unwrap() {
            let got = seed_transition::verify(&case["value"], now, pin);
            check(case, got);
        }
        for case in row["context_cases"].as_array().unwrap() {
            check(
                case,
                seed_transition::verify(&row["value"], &case["now"], case["pin"].as_str()),
            );
        }
    }
    let frozen: Value = serde_json::from_str(include_str!(
        "../fixtures/contracts/seed-key-transition-v1.json"
    ))
    .unwrap();
    assert!(
        seed_transition::verify(
            &frozen,
            &json!(1700000000),
            frozen["transition"]["old_seed_key_digest"].as_str()
        )
        .is_ok()
    );
}
fn check(case: &Value, got: Result<Value, ContractError>) {
    assert_eq!(
        got.is_ok(),
        case["accepted"].as_bool().unwrap(),
        "{}: {got:?}, oracle {}",
        case["name"],
        case["error"]
    );
    if case["accepted"] == true {
        assert_eq!(got.unwrap(), case["output"]);
    } else if case["exception"] == "SeedOperatorError" || case["exception"] == "ContractError" {
        assert_eq!(
            got.unwrap_err().0,
            case["error"].as_str().unwrap(),
            "{}",
            case["name"]
        );
    }
}
#[test]
fn path_manifest_closed_shape_and_independent_graph_binding_corpus() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/f4-manifest-vectors.json")).unwrap();
    for row in rows {
        for case in row["cases"].as_array().unwrap() {
            let got = path_manifest::parse(&case["value"]).and_then(|m| {
                if row["name"] == "graph-bound" {
                    m.bind_to_validated_graph(&row["context"]["graph"])?;
                }
                Ok(m.into_value())
            });
            check(case, got);
        }
        let m = path_manifest::parse(&row["value"]).unwrap();
        m.bind_to_validated_graph(&row["context"]["graph"]).unwrap();
    }
}
#[test]
fn seed_numeric_compatibility_uses_oracle_unicode_decimal_set() {
    let data: Value =
        serde_json::from_str(include_str!("../fixtures/f4-decimal-zeroes.json")).unwrap();
    assert_eq!(
        seed_transition::DECIMAL_ZEROES,
        data["zeroes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| n.as_u64().unwrap() as u32)
            .collect::<Vec<_>>()
    );
}
