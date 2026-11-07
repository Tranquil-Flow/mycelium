// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{canonical, capacity};
use serde_json::Value;
#[test]
fn capacity_positive_bytes_rederived_claims_and_negative_corpus() {
    // O:contract:mycelium.capacity_profile.v1. Source errors are prose, not codes.
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continued-capacity-vectors.json")).unwrap();
    for row in rows {
        let base = &row["value"];
        let bytes = canonical::to_bytes(base).unwrap();
        assert_eq!(bytes, row["canonical_utf8"].as_str().unwrap().as_bytes());
        assert_eq!(capacity::parse(&bytes).unwrap(), *base);
        for (i, case) in row["cases"].as_array().unwrap().iter().enumerate() {
            let value = support::mutate(base, case);
            let bytes = canonical::to_bytes(&value).unwrap();
            let result = capacity::parse(&bytes);
            assert_eq!(
                result.is_ok(),
                case["accepted"] == true,
                "{} case {i}: {case}; {result:?}",
                row["name"]
            );
            if let Ok(v) = result {
                assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    case["canonical_utf8"].as_str().unwrap().as_bytes()
                );
            } else {
                assert_eq!(result.unwrap_err().0, "capacity_profile_invalid");
            }
        }
    }
}
#[test]
fn capacity_raw_ingress_refuses_noncanonical_and_oversized_bytes() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continued-capacity-ingress.json")).unwrap();
    for row in rows {
        let raw = row["hex"].as_str().unwrap();
        let bytes: Vec<_> = (0..raw.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&raw[i..i + 2], 16).unwrap())
            .collect();
        let actual = capacity::parse(&bytes);
        assert_eq!(
            actual.is_ok(),
            row["accepted"] == true,
            "{}: {actual:?}",
            row["name"]
        );
        if let Ok(v) = actual {
            assert_eq!(
                canonical::to_bytes(&v).unwrap(),
                row["canonical_utf8"].as_str().unwrap().as_bytes()
            );
        } else {
            assert_eq!(actual.unwrap_err().0, "capacity_profile_invalid");
        }
    }
}
// Exercise shared dispatcher too, retaining all common harness checks.
#[test]
fn capacity_dispatch_positive_bytes() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continued-capacity-vectors.json")).unwrap();
    let shared: Vec<_> = rows
        .into_iter()
        .map(|mut row| {
            row["cases"] = serde_json::json!([]);
            row
        })
        .collect();
    support::corpus(&serde_json::to_string(&shared).unwrap(), |p, v| {
        protocol::schemas::validate_as(p, v, 0)
    });
}
