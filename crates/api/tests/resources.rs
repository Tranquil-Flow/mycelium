// SPDX-License-Identifier: AGPL-3.0-only
//! `api::resources` equals the executed oracle (`resources.policy_violation`, `_parse_probe_output`, f4e366e9):
//! `fixtures/oracle_resources.json` from `L10-evidence/r18-probe-resources.py`
//! (sha256 a866484bb3e0d8b5f1e1cbdbb1aac6ace3819fd4e88e728a0cbb374c090d37a5; fixture sha256
//! 2ef47f4014e51b85d11d13c50ffdaed11618828ff45e5893cabdbb0a71d8bdb4): 780 policy x observation cases and 14
//! helper outputs. Observations the app never hands to `policy_violation` (not a dict of three strings:
//! `_resource_check` replaces them with the unknown observation first) are compared through that
//! normalisation.
use api::resources::{Observation, observation_from_value, parse_probe_output, policy_violation};
use serde_json::Value;

const FIXTURE: &str = include_str!("fixtures/oracle_resources.json");

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}

#[test]
fn the_policy_gate_equals_the_executed_oracle() {
    let doc: Value = serde_json::from_str(FIXTURE).unwrap();
    let cases = doc["policy_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 780);
    let mut reachable = 0;
    for case in cases {
        let raw = &case["observation"];
        let reaches_policy = raw.as_object().is_some_and(|m| {
            m.len() == 3
                && ["power", "thermal", "network"]
                    .iter()
                    .all(|k| m.get(*k).is_some_and(Value::is_string))
        });
        if !reaches_policy {
            // The oracle's own answer is `resource_observation_invalid`; the app never asks it.
            assert_eq!(observation_from_value(raw), Observation::unknown(), "{raw}");
            continue;
        }
        reachable += 1;
        let got = policy_violation(&case["policy"], &observation_from_value(raw));
        assert_eq!(
            got.map(str::to_owned)
                .map(Value::from)
                .unwrap_or(Value::Null),
            case["result"],
            "{case}"
        );
    }
    assert!(reachable > 700, "{reachable}");
}

#[test]
fn the_helper_output_schema_equals_the_executed_oracle() {
    let doc: Value = serde_json::from_str(FIXTURE).unwrap();
    for case in doc["parse_cases"].as_array().unwrap() {
        let bytes = unhex(case["hex"].as_str().unwrap());
        assert_eq!(bytes.len() as u64, case["len"].as_u64().unwrap());
        assert_eq!(
            parse_probe_output(&bytes).to_value(),
            case["result"],
            "{case}"
        );
    }
}
