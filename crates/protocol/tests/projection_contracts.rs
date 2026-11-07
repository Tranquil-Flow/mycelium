// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{canonical, projection};
use serde_json::Value;
#[test]
fn projection_positive_bytes_and_negative_corpus() {
    // O:contract:mycelium.evidence_projection.v1.
    support::corpus(
        include_str!("../fixtures/continued-projection-vectors.json"),
        |p, v| protocol::schemas::validate_as(p, v, 0),
    );
}
#[test]
fn historical_records_are_immutable_deduplicated_bounded_sorted_and_filtered() {
    // O:contract:mycelium.evidence_history.v1. Pure derivation, no runtime source.
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continued-history-vectors.json")).unwrap();
    for (i, row) in rows.iter().enumerate() {
        let input = &row["input"];
        let result = projection::history(
            input["records"].as_array().unwrap(),
            input["capability"].as_str(),
        );
        assert_eq!(
            result.is_ok(),
            row["accepted"] == true,
            "case {i}: {row}; {result:?}"
        );
        match result {
            Ok(v) => assert_eq!(
                canonical::to_bytes(&v).unwrap(),
                row["canonical_utf8"].as_str().unwrap().as_bytes()
            ),
            Err(e) => assert_eq!(e.0, row["error"].as_str().unwrap(), "case {i}"),
        }
    }
}
