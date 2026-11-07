// SPDX-License-Identifier: AGPL-3.0-only
//! The persisted `diagnostic=` string (and `remote_code`) v0.1.4's `_verified_observation`
//! raises, on 1,352 cases recorded by executing the oracle (`round7-observation-diagnostic-probe.py`,
//! scratch keys). `diagnostic` is persisted user-visible state in the qualification serve
//! diagnostic, so it is asserted byte-exact, not by label family.
use protocol::observation::{self, ExpectedObservation};
use serde_json::Value;

#[test]
fn refusals_carry_the_oracle_code_remote_code_and_diagnostic_string() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round7-observation-diagnostic-vectors.json"
    ))
    .unwrap();
    let key = &v["key"];
    let cases = v["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 1352);
    let (mut accepted, mut diagnostics) = (0, 0);
    for case in cases {
        let c = &v["contexts"][case["context"].as_str().unwrap()];
        let expected = ExpectedObservation {
            event: c["event"].as_str().unwrap(),
            node_id: c["node_id"].as_str().unwrap(),
            host_id: c["host_id"].as_str().unwrap(),
            process_id: &c["process_id"],
            run_id: c["run_id"].as_str().unwrap(),
            deployment_id: c["deployment_id"].as_str().unwrap(),
            endpoint_id: c["endpoint_id"].as_str().unwrap(),
            key,
        };
        let name = case["name"].as_str().unwrap();
        let got = observation::verify_detailed(&case["response"], &expected);
        if case["accepted"] == true {
            assert!(
                got.is_ok(),
                "{name}: oracle accepted, v0.2 refused {:?}",
                got.err()
            );
            // the plain verifier agrees on acceptance
            assert!(
                observation::verify(&case["response"], &expected).is_ok(),
                "{name}"
            );
            accepted += 1;
            continue;
        }
        let r = got
            .err()
            .unwrap_or_else(|| panic!("{name}: oracle refused, v0.2 accepted"));
        assert_eq!(Some(r.code), case["code"].as_str(), "{name}: code");
        assert_eq!(
            r.remote_code.as_deref(),
            case["remote_code"].as_str(),
            "{name}: remote_code"
        );
        assert_eq!(
            r.diagnostic.as_deref(),
            case["diagnostic"].as_str(),
            "{name}: diagnostic"
        );
        // the plain verifier keeps its contract: the code only
        assert_eq!(
            observation::verify(&case["response"], &expected)
                .err()
                .map(|e| e.0),
            case["code"].as_str(),
            "{name}"
        );
        diagnostics += usize::from(r.diagnostic.is_some());
    }
    assert_eq!((accepted, diagnostics), (41, 1255));
}
