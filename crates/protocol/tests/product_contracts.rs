// SPDX-License-Identifier: AGPL-3.0-only
mod support;
// O:contract:mycelium.product_snapshot.v1; O:contract:mycelium.product_event.v1
// O:contract:mycelium.route_explorer.v1
#[test]
fn product_positive_bytes_and_negative_corpus() {
    support::corpus(
        include_str!("../fixtures/continued-product-vectors.json"),
        |p, v| protocol::schemas::validate_as(p, v, 0),
    );
}
#[test]
fn explorer_projection_binds_source_history_and_region_evidence() {
    let rows: Vec<serde_json::Value> = serde_json::from_str(include_str!(
        "../fixtures/continued-explorer-projections.json"
    ))
    .unwrap();
    for row in rows {
        let projected = protocol::explorer::project_from_validated_snapshot(&row["value"]).unwrap();
        assert_eq!(projected, row["output"]);
        assert_eq!(
            protocol::canonical::to_bytes(&projected).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
    }
}
