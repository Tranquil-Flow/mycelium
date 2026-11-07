// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{canonical, promotion};
use serde_json::Value;
#[test]
fn promotion_reconstructs_exact_positive_bytes_and_rejects_forged_claims() {
    // O:contract:mycelium.candidate_promotion_report.v1.
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continued-promotion-vectors.json")).unwrap();
    for row in rows {
        let base = &row["value"];
        let p = base["protocol"].as_str().unwrap();
        assert_eq!(
            canonical::to_bytes(&promotion::parse(base).unwrap()).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let value = support::mutate(base, c);
            let result = promotion::parse(&value);
            assert_eq!(
                result.is_ok(),
                c["accepted"] == true,
                "{} case {i}: {c}; {result:?}",
                row["name"]
            );
            assert_eq!(
                protocol::schemas::validate_as(p, &value, 0).is_ok(),
                result.is_ok()
            );
            match result {
                Ok(v) => assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes()
                ),
                Err(e) => assert_eq!(e.0, "candidate_promotion_report_invalid"),
            }
        }
        let mut positive = row.clone();
        positive["cases"] = serde_json::json!([]);
        support::corpus(&serde_json::to_string(&[positive]).unwrap(), |p, v| {
            protocol::schemas::validate_as(p, v, 0)
        });
    }
}
