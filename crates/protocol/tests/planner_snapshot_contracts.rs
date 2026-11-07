// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{canonical, gossip, planner_snapshot};
use serde_json::Value;
const LIMITS: gossip::Limits = gossip::Limits {
    max_record_ttl_ms: 604_800_000,
};
fn run(name: &str, v: &Value) -> Result<Value, protocol::ContractError> {
    if name == "binding" {
        planner_snapshot::validate_binding(&v["snapshot"], &v["bundle"], LIMITS)?;
        Ok(v["snapshot"].clone())
    } else {
        planner_snapshot::derive(
            &v["bundle"],
            &v["model"],
            &v["workload"],
            &v["policy"],
            LIMITS,
        )
    }
}
#[test]
fn planner_snapshot_reconstructs_every_field_from_independent_gossip_evidence() {
    // O:contract:mycelium.layer_planner_snapshot.v1.
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/remaining-snapshot-vectors.json")).unwrap();
    for row in rows {
        let name = row["name"].as_str().unwrap();
        let base = &row["value"];
        assert_eq!(
            canonical::to_bytes(&run(name, base).unwrap()).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        let positive = serde_json::json!({"value":serde_json::from_str::<Value>(row["canonical_utf8"].as_str().unwrap()).unwrap(),"canonical_utf8":row["canonical_utf8"],"cases":[]});
        support::corpus(&serde_json::to_string(&[positive]).unwrap(), |_, v| {
            planner_snapshot::validate_binding(v, &base["bundle"], LIMITS)
        });
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let mut v = support::mutate(base, c);
            if c["rebuild_bundle"] == true {
                let b = &mut v["bundle"];
                for r in b["records"].as_array_mut().unwrap() {
                    r["payload_hash"] = canonical::digest(&r["payload"])
                        .unwrap()
                        .trim_start_matches("sha256:")
                        .into();
                }
                for f in ["router_view", "allocator_view", "evidence_bundle_digest"] {
                    b.as_object_mut().unwrap().remove(f);
                }
                *b = gossip::build_bundle(b, LIMITS).unwrap();
            }
            let r = run(name, &v);
            assert_eq!(r.is_ok(), c["accepted"] == true, "{name} {i} {c}; {r:?}");
            if let Ok(v) = r {
                assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes(),
                    "{name} {i}"
                );
            }
        }
    }
}
