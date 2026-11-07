// SPDX-License-Identifier: AGPL-3.0-only
//! Round 7: `planner_assignment.compile_bound_replica_assignments` against actual oracle
//! outputs/exceptions on multi-node (5-6 node) bundles whose real plans carry 2-3 legal tracks
//! (`fixtures/bound-replica.json`, provenance beside it). Round 6 had no replica vectors.
use planner::bound::{BoundInputs, compile_bound_replica_assignments};
use planner::py::{Kind, PyResult};
use protocol::gossip::Limits;
use serde_json::Value;

// Seven-day source hard cap of the gossip record TTL (mycelium_gossip/schema.py:452).
const LIMITS: Limits = Limits {
    max_record_ttl_ms: 604_800_000,
};

fn apply(base: &Value, patch: &Value) -> Value {
    let mut out = base.clone();
    for op in patch.as_array().unwrap() {
        let path = op["path"].as_array().unwrap();
        let mut cursor = &mut out;
        for (i, key) in path.iter().enumerate() {
            let last = i + 1 == path.len();
            let slot = match key {
                Value::String(k) => {
                    if last {
                        if op.get("delete").is_some() {
                            cursor.as_object_mut().unwrap().remove(k);
                            break;
                        }
                        cursor
                            .as_object_mut()
                            .unwrap()
                            .insert(k.clone(), op["value"].clone());
                        break;
                    }
                    &mut cursor[k.as_str()]
                }
                Value::Number(n) => {
                    let index = n.as_u64().unwrap() as usize;
                    if last {
                        cursor[index] = op["value"].clone();
                        break;
                    }
                    &mut cursor[index]
                }
                _ => unreachable!(),
            };
            cursor = slot;
        }
        if path.is_empty() {
            out = op["value"].clone();
        }
    }
    out
}

fn bytes(v: &Value) -> String {
    planner::canonical(v, false).unwrap()
}

/// Oracle success → byte-identical value. Oracle `ValueError` → same refusal text.
/// Oracle ValueError subclasses (evidence/schema owned by the shared L5 boundary) →
/// a refusal, text owned by L5. Any other oracle exception class → a refusal.
fn check(label: &str, oracle: &Value, got: PyResult<Value>) {
    if oracle["ok"] == true {
        let got = got.unwrap_or_else(|e| panic!("{label}: oracle accepted, Rust refused: {e}"));
        assert_eq!(bytes(&oracle["value"]), bytes(&got), "{label}");
        return;
    }
    let err = match got {
        Ok(v) => panic!(
            "{label}: oracle raised {} {:?}; Rust accepted {}",
            oracle["class"],
            oracle["message"],
            bytes(&v)
        ),
        Err(e) => e,
    };
    // `EvidenceBundleError` (a ValueError subclass raised by the gossip evidence contract) carries
    // a per-field text that v0.1.4 callers observe; L5 returns it exactly (99cd3f7).
    if oracle["class"] == "ValueError" || oracle["class"] == "EvidenceBundleError" {
        assert_eq!(err.kind, Kind::Value, "{label}: {err}");
        assert_eq!(oracle["message"].as_str().unwrap(), err.message, "{label}");
    } else if oracle["is_value_error"] == true {
        assert_eq!(err.kind, Kind::Value, "{label}: {err}");
    }
}

#[test]
fn provenance_pins_the_exact_fixture_and_counts() {
    use sha2::{Digest, Sha256};
    let raw = include_str!("fixtures/bound-replica.json");
    let p: Value =
        serde_json::from_str(include_str!("fixtures/bound-replica-provenance.json")).unwrap();
    assert_eq!(
        p["output_sha256"].as_str().unwrap(),
        format!("{:x}", Sha256::digest(raw.as_bytes()))
    );
    assert_eq!(
        p["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let f: Value = serde_json::from_str(raw).unwrap();
    assert_eq!(p["counts"]["cases"], f["cases"].as_array().unwrap().len());
    assert_eq!(
        p["counts"]["scenarios"],
        f["bases"].as_object().unwrap().len()
    );
    for (name, shape) in p["scenarios"].as_object().unwrap() {
        let base = &f["bases"][name.as_str()];
        assert_eq!(
            shape["tracks"],
            base["route_plan"]["legal_tracks"].as_array().unwrap().len()
        );
        assert_eq!(
            shape["placements"],
            base["route_plan"]["placements"].as_array().unwrap().len()
        );
    }
}

#[test]
fn bound_replica_assignments_match_the_oracle_on_multi_node_bundles() {
    let raw = include_str!("fixtures/bound-replica.json");
    let f: Value = serde_json::from_str(raw).unwrap();
    let (mut ok, mut refused) = (0, 0);
    let mut texts = std::collections::BTreeSet::new();
    let mut max_placements = 0;
    for case in f["cases"].as_array().unwrap() {
        let a = apply(&f["bases"][case["base"].as_str().unwrap()], &case["patch"]);
        let got = compile_bound_replica_assignments(&BoundInputs {
            route_plan: &a["route_plan"],
            planner_snapshot: &a["planner_snapshot"],
            evidence_bundle: &a["evidence_bundle"],
            manifest: &a["manifest"],
            deployment_id: &a["deployment_id"],
            deployment_epoch: &a["deployment_epoch"],
            cache_roots: &a["cache_roots"],
            runtime_by_node: &a["runtime_by_node"],
            limits: LIMITS,
        })
        .map(|m| Value::Object(m.into_iter().collect()));
        if case["oracle"]["ok"] == true {
            ok += 1;
            max_placements = max_placements.max(case["oracle"]["value"].as_object().unwrap().len());
        } else {
            refused += 1;
            if case["oracle"]["class"] == "ValueError" {
                texts.insert(case["oracle"]["message"].as_str().unwrap().to_owned());
            }
        }
        check(case["label"].as_str().unwrap(), &case["oracle"], got);
    }
    assert!(
        ok >= 300 && refused >= 2000,
        "{ok} accepted / {refused} refused"
    );
    assert!(
        max_placements >= 6,
        "no vector compiles a 6-placement replica plan"
    );
    // The replica-specific refusals must all be reachable from the vectors.
    for text in [
        "replica assignment requires two or more legal tracks",
        "replica assignment track is invalid",
        "replica track placement_ids must be unique and non-empty",
        "replica track must select one placement per group",
        "replica track contains a layer gap or overlap",
        "replica plan contains an unprovisioned zero-flow placement",
    ] {
        assert!(
            texts.contains(text),
            "no vector reaches {text:?}: {texts:#?}"
        );
    }
}
