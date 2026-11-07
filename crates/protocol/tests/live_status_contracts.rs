// SPDX-License-Identifier: AGPL-3.0-only
#[allow(dead_code)]
mod support;
use protocol::{
    canonical,
    live_status::{self, Limits},
};
use serde_json::Value;
// Exact source limits, recorded by the external oracle; production caller supplies L1 policy.
fn limits() -> Limits {
    Limits {
        fatal_code_chars: 128,
        cooperative_work_ms: 2000.0,
        cleanup_ms: serde_json::from_str::<Value>(include_str!(
            "../fixtures/round4-live-provenance.json"
        ))
        .unwrap()["cleanup_max_ms"]
            .as_u64()
            .unwrap(),
        maximum_concurrent_requests: 4,
        a10_intervals: 256,
        a10_observations: 64,
    }
}
// O:contract:mycelium.live_route_status.v1
#[test]
fn complete_live_projection_matches_pinned_bytes_and_refusals() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/round4-live-vectors.json")).unwrap();
    for row in rows {
        assert_eq!(
            canonical::to_bytes(&live_status::project(&row["value"], &limits()).unwrap()).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes(),
            "{}",
            row["name"]
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let result = live_status::project(&support::mutate(&row["value"], c), &limits());
            assert_eq!(
                result.is_ok(),
                c["accepted"] == true,
                "{} #{i} path={} replacement={} delete={} result={:?}",
                row["name"],
                c["path"],
                c["replacement"],
                c["delete"],
                result.as_ref().err()
            );
            if let Ok(out) = result {
                assert_eq!(
                    canonical::to_bytes(&out).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes(),
                    "{} #{i} {c}",
                    row["name"]
                );
            }
        }
    }
}
