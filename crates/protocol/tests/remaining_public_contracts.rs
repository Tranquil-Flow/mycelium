// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{canonical, schemas};
use serde_json::Value;
#[test]
fn remaining_public_shapes_match_positive_bytes_and_all_negative_vectors() {
    // O:contract:mycelium.assignment_artifact_cache.v1; O:contract:mycelium.assignment_materialization.v1;
    // O:contract:mycelium.concurrent_request_runtime.v1; O:contract:mycelium.m23_heterogeneous_kv_gate.v1;
    // O:contract:mycelium.m13_placement_projection.v1; O:contract:mycelium.m14_topology_projection.v1; O:contract:mycelium.m15_plan_comparison.v1.
    let rows: Vec<Value> = serde_json::from_str(concat!(
        include_str!("../fixtures/remaining-public-vectors.json.part-00"),
        include_str!("../fixtures/remaining-public-vectors.json.part-01"),
        include_str!("../fixtures/remaining-public-vectors.json.part-02"),
        include_str!("../fixtures/remaining-public-vectors.json.part-03"),
        include_str!("../fixtures/remaining-public-vectors.json.part-04")
    ))
    .unwrap();
    for row in rows {
        let base = &row["value"];
        let p = base["protocol"].as_str().unwrap();
        schemas::validate_as(p, base, 0).unwrap();
        assert_eq!(
            canonical::to_bytes(base).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let mut v = support::mutate(base, c);
            if let Some(d) = c.get("evidence_digest_override") {
                v["evidence_digest"] = d.clone();
            }
            let r = schemas::validate_as(p, &v, 0);
            assert_eq!(
                r.is_ok(),
                c["accepted"] == true,
                "{} {i}: {c}; {r:?}",
                row["name"]
            );
            if r.is_ok() {
                assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes()
                );
            }
        }
        let mut positive = row.clone();
        positive["cases"] = serde_json::json!([]);
        support::corpus(&serde_json::to_string(&[positive]).unwrap(), |p, v| {
            schemas::validate_as(p, v, 0)
        });
    }
}
