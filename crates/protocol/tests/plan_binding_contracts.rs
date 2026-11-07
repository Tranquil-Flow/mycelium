// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `bind_operator_plan_document` on cases recorded by executing the oracle
//! (`round8-plan-binding-probe.py`, scratch keys): the bound plan byte for byte, or the exact code
//! (the plan-binding code, or the membership-contract code raised through it).
use protocol::TimeLimits;
use protocol::canonical;
use protocol::identity::Identity;
use protocol::plan_binding::{BindingSeed, bind_operator_plan_document};
use serde_json::Value;

#[test]
fn plan_binding_matches_the_executed_oracle() {
    let v: Value =
        serde_json::from_str(include_str!("../fixtures/round8-plan-binding-vectors.json")).unwrap();
    let cases = v["cases"].as_array().unwrap();
    let limits = TimeLimits {
        max_ttl_seconds: 3_600.0,
        clock_skew_seconds: 0.0,
    };
    let (mut accepted, mut refused, mut accidents) = (0, 0, 0);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let signer = Identity::from_secret(
            case["signer"]["endpoint"].as_str().unwrap(),
            &[u8::try_from(case["signer"]["byte"].as_u64().unwrap()).unwrap(); 32],
        )
        .unwrap();
        let now = match case["now_special"].as_str() {
            Some("nan") => f64::NAN,
            Some("inf") => f64::INFINITY,
            Some("-inf") => f64::NEG_INFINITY,
            _ => case["now"].as_f64().unwrap(),
        };
        let boot = case["observed_boot_ids_given"]
            .as_bool()
            .unwrap()
            .then_some(&case["observed_boot_ids"]);
        let seed = BindingSeed {
            signer: &signer,
            swarm_id: case["swarm_id"].as_str().unwrap(),
            seed_node_id: case["seed_node_id"].as_str().unwrap(),
            limits,
        };
        let got = bind_operator_plan_document(
            &case["plan"],
            &seed,
            case["members"].as_array().unwrap(),
            now,
            boot,
        );
        if case["accepted"] == true {
            let got = got.unwrap_or_else(|e| panic!("{name}: refused with {e}"));
            assert_eq!(
                String::from_utf8(canonical::to_bytes(&got).unwrap()).unwrap(),
                String::from_utf8(canonical::to_bytes(&case["result"]).unwrap()).unwrap(),
                "{name}"
            );
            accepted += 1;
        } else if let Some(code) = case.get("foreign_code") {
            assert_eq!(
                got.map(|_| ()).map_err(|e| e.0),
                Err(code.as_str().unwrap()),
                "{name}"
            );
            refused += 1;
        } else if case.get("python_exception").is_some() {
            assert!(got.is_err(), "{name}: oracle crashed; v0.2 must refuse");
            accidents += 1;
        } else {
            assert_eq!(
                got.map(|_| ()).map_err(|e| e.0),
                Err(case["code"].as_str().unwrap()),
                "{name}"
            );
            refused += 1;
        }
    }
    assert_eq!(
        (cases.len(), accepted, refused, accidents),
        (116, 37, 79, 0)
    );
}
