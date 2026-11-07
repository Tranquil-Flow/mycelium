// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{canonical::to_bytes as canonical_json_bytes, schemas};
use serde_json::Value;

fn changed(base: &Value, case: &Value) -> Value {
    let path = case["path"].as_array().unwrap();
    if path.is_empty() {
        return case["replacement"].clone();
    }
    let mut value = base.clone();
    let mut at = &mut value;
    for key in &path[..path.len() - 1] {
        at = if let Some(s) = key.as_str() {
            &mut at[s]
        } else {
            &mut at[key.as_u64().unwrap() as usize]
        };
    }
    let last = path.last().unwrap();
    if case["delete"] == true {
        if let Some(s) = last.as_str() {
            at.as_object_mut().unwrap().remove(s).unwrap();
        } else {
            at.as_array_mut()
                .unwrap()
                .remove(last.as_u64().unwrap() as usize);
        }
    } else if let Some(s) = last.as_str() {
        at[s] = case["replacement"].clone();
    } else {
        at[last.as_u64().unwrap() as usize] = case["replacement"].clone();
    }
    value
}

// O:contract:mycelium.product_ui.bootstrap.v1
#[test]
fn bootstrap_matches_primary_schema_bytes_and_full_mutation_corpus() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/expanded-bootstrap-vectors.json")).unwrap();
    for row in rows {
        let base = &row["value"];
        let p = base["protocol"].as_str().unwrap();
        assert_eq!(schemas::validate_as(p, base, 1), Ok(()));
        assert_eq!(
            canonical_json_bytes(base).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let actual = schemas::validate_as(p, &changed(base, c), 1);
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "case {i}: {c}; {actual:?}"
            );
        }
    }
}

// O:contract:mycelium.replica_plan.v1
// O:contract:mycelium.replica_qualification.v1
// O:contract:mycelium.replica_runtime.v1
// O:contract:mycelium.replica_benchmark.v1
// O:contract:mycelium.replica_benchmark.v2
// O:contract:mycelium.deployment_activation.v1
// O:contract:mycelium.product_ui.swarm.v1
#[test]
fn public_shapes_match_pinned_positive_bytes_and_negative_corpus() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/expanded-public-vectors.json")).unwrap();
    let replica: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/expanded-replica-vectors.json")).unwrap();
    for row in rows.into_iter().chain(replica) {
        let base = &row["value"];
        let protocol = base["protocol"].as_str().unwrap();
        assert_eq!(
            canonical_json_bytes(base).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        assert_eq!(
            schemas::validate_as(protocol, base, 1),
            Ok(()),
            "{}",
            row["name"]
        );
        let mut refused = 0;
        for (i, case) in row["cases"].as_array().unwrap().iter().enumerate() {
            let value = changed(base, case);
            let actual = schemas::validate_as(protocol, &value, 1);
            assert_eq!(
                actual.is_ok(),
                case["accepted"] == true,
                "{} case {i}: {case}; actual {actual:?}",
                row["name"]
            );
            if case["accepted"] == false {
                refused += 1;
                // Python's incidental TypeError on unhashable input is a refusal,
                // not a stable protocol error code to export from Rust.
                if case["exception"] != "TypeError" && case["exception"] != "AttributeError" {
                    assert_eq!(
                        actual.unwrap_err().0,
                        case["error"].as_str().unwrap(),
                        "{} case {i}",
                        row["name"]
                    );
                }
            }
        }
        assert!(refused > 0);
    }
}
