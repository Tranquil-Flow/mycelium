// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{canonical, provisioning};
use serde_json::Value;
fn changed(base: &Value, case: &Value) -> Value {
    let mut value = base.clone();
    let path = case["path"].as_array().unwrap();
    if path.is_empty() {
        return case["replacement"].clone();
    }
    let mut at = &mut value;
    for key in &path[..path.len() - 1] {
        at = if let Some(s) = key.as_str() {
            &mut at[s]
        } else {
            &mut at[key.as_u64().unwrap() as usize]
        };
    }
    let key = path.last().unwrap();
    if case["delete"] == true {
        if let Some(s) = key.as_str() {
            at.as_object_mut().unwrap().remove(s).unwrap();
        } else {
            at.as_array_mut()
                .unwrap()
                .remove(key.as_u64().unwrap() as usize);
        }
    } else if let Some(s) = key.as_str() {
        at[s] = case["replacement"].clone();
    } else {
        at[key.as_u64().unwrap() as usize] = case["replacement"].clone();
    }
    value
}
// O:contract:mycelium.manual_provisioning_route.v1
#[test]
fn manual_routes_match_positive_bytes_and_full_mutation_corpus() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/expanded-manual-vectors.json")).unwrap();
    for r in rows {
        provisioning::manual_route(&r["value"]).unwrap();
        assert_eq!(
            canonical::to_bytes(&r["value"]).unwrap(),
            r["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in r["cases"].as_array().unwrap().iter().enumerate() {
            let v = changed(&r["value"], c);
            let actual = provisioning::manual_route(&v);
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "case {i}: {c}; {actual:?}"
            );
        }
    }
}
// O:contract:mycelium.model_preparation_authorization.v2
#[test]
fn preparation_authorization_matches_derived_bytes_and_refusals() {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/expanded-preparation-vectors.json"
    ))
    .unwrap();
    for r in rows {
        let v = &r["input"];
        let out = provisioning::authorization(&v["operation"], &v["decision"], &v["now_unix_ms"])
            .unwrap();
        assert_eq!(
            canonical::to_bytes(&out).unwrap(),
            r["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in r["cases"].as_array().unwrap().iter().enumerate() {
            let v = changed(v, c);
            let actual =
                provisioning::authorization(&v["operation"], &v["decision"], &v["now_unix_ms"]);
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "{} case {i}: {c}; {actual:?}",
                r["name"]
            );
            match actual {
                Ok(out) => assert_eq!(
                    canonical::to_bytes(&out).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes(),
                    "case {i}: {c}"
                ),
                Err(e) if c["exception"] == "ModelPreparationError" => {
                    assert_eq!(e.0, c["error"].as_str().unwrap(), "case {i}: {c}")
                }
                Err(e) => assert!(!e.0.is_empty()),
            }
        }
    }
}
