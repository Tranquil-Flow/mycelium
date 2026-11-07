// SPDX-License-Identifier: AGPL-3.0-only
//! O:contract:mycelium.transport_path_observation.v1
//! O:contract:mycelium.coarse_region_evidence.v1
use protocol::{ContractError, canonical, evidence};
use serde_json::Value;

fn validate(v: &Value, context: &Value, transport: bool) -> Result<(), ContractError> {
    if transport {
        evidence::transport_path(
            v,
            context.get("now_unix_ms").filter(|v| !v.is_null()),
            context["require_resolved"].as_bool().unwrap(),
        )
    } else {
        evidence::coarse_region(v)
    }
}

#[test]
fn evidence_shapes_digests_freshness_and_reuse_match_pinned_oracle() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/resume-evidence-vectors.json")).unwrap();
    for row in rows {
        let transport = row["value"]["protocol"] == "mycelium.transport_path_observation.v1";
        let context = &row["context"];
        validate(&row["value"], context, transport).unwrap();
        assert_eq!(
            canonical::to_ascii_bytes(&row["value"]).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for case in row["cases"].as_array().unwrap() {
            let got = validate(&case["value"], context, transport);
            assert_eq!(
                got.is_ok(),
                case["accepted"].as_bool().unwrap(),
                "{} / {}: {got:?}, oracle={}",
                row["name"],
                case["name"],
                case["error"]
            );
            if case["exception"] == "ValueError" {
                assert_eq!(
                    got.unwrap_err().0,
                    case["error"].as_str().unwrap(),
                    "{} / {}",
                    row["name"],
                    case["name"]
                );
            }
        }
        if let Some(cases) = row["context_cases"].as_array() {
            for case in cases {
                let got = validate(&row["value"], &case["context"], transport);
                assert_eq!(
                    got.is_ok(),
                    case["accepted"].as_bool().unwrap(),
                    "{case}: {got:?}"
                );
                if let Err(error) = got {
                    assert_eq!(error.0, case["error"].as_str().unwrap());
                }
            }
        }
    }
}
