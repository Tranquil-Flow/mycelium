// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{
    ContractError, canonical,
    identity::Identity,
    observation::{self, ExpectedObservation},
};
use serde_json::{Value, json};
fn signed(v: &Value) -> Result<Value, ContractError> {
    let id = Identity::from_secret("endpoint-phone", &[9; 32]).unwrap();
    Ok(
        json!({"ok":true,"result":{"observation":v,"signature":id.sign(v).map_err(|_|ContractError("sign_invalid"))?,"verification_key":id.public_key_record()}}),
    )
}
#[test]
fn signed_cancellation_receipt_matches_oracle_bytes_and_all_mutations() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continuation-cancel-vectors.json")).unwrap();
    for r in rows {
        let c = &r["context"];
        let expected = ExpectedObservation {
            event: c["event"].as_str().unwrap(),
            node_id: c["node_id"].as_str().unwrap(),
            host_id: c["host_id"].as_str().unwrap(),
            process_id: &c["process_id"],
            run_id: c["run_id"].as_str().unwrap(),
            deployment_id: c["deployment_id"].as_str().unwrap(),
            endpoint_id: c["endpoint_id"].as_str().unwrap(),
            key: &c["key"],
        };
        let validate = |v: &Value| -> Result<(), ContractError> {
            let env = if r["kind"] == "observation" {
                signed(v)?
            } else {
                v.clone()
            };
            observation::verify(&env, &expected)?.require_cancel_receipt(&c["subject"])
        };
        validate(&r["value"]).unwrap();
        let mut env = if r["kind"] == "observation" {
            signed(&r["value"]).unwrap()
        } else {
            r["value"].clone()
        };
        let proof = observation::verify(&env, &expected)
            .unwrap()
            .cancel_receipt(&c["subject"])
            .unwrap();
        assert_eq!(proof.subject(), &c["subject"]);
        assert_eq!(
            proof.signed_observation_bytes(),
            canonical::to_bytes(&env["result"]).unwrap()
        );
        assert_eq!(
            proof.observation_digest(),
            env["result"]["signature"]["signed_statement_digest"]
                .as_str()
                .unwrap()
        );
        // Downstream evidence cannot accidentally seal a mutated caller envelope.
        env["result"]["observation"]["details"]["request_cleanup"]["complete"] = json!(false);
        assert_ne!(
            proof.signed_observation_bytes(),
            canonical::to_bytes(&env["result"]).unwrap()
        );
        assert_eq!(proof.subject(), &c["subject"]);
        if r["kind"] == "observation" {
            assert_eq!(
                canonical::to_bytes(&signed(&r["value"]).unwrap()).unwrap(),
                r["envelope_utf8"].as_str().unwrap().as_bytes()
            );
        } else {
            assert_eq!(
                canonical::to_bytes(&r["value"]).unwrap(),
                r["canonical_utf8"].as_str().unwrap().as_bytes()
            );
        }
        for case in r["cases"].as_array().unwrap() {
            let got = validate(&case["value"]);
            assert_eq!(
                got.is_ok(),
                case["accepted"].as_bool().unwrap(),
                "{} / {}: {got:?} oracle={}",
                r["name"],
                case["name"],
                case["error"]
            );
            if case["exception"] == "ControllerError" {
                assert_eq!(
                    got.unwrap_err().0,
                    case["error"].as_str().unwrap(),
                    "{} / {}",
                    r["name"],
                    case["name"]
                );
            }
        }
    }
}
