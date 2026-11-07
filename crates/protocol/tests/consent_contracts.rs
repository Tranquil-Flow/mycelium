// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{canonical, consent};
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
fn call(kind: &str, v: &Value) -> Result<Value, protocol::ContractError> {
    // Mirror the external recorder's required positional argument wrapper.
    let required = if kind == "caps" {
        vec!["caps", "policy", "device_memory_bytes"]
    } else {
        vec!["grant", "binding", "offer", "resource_violation"]
    };
    if !v
        .as_object()
        .is_some_and(|o| required.iter().all(|k| o.contains_key(*k)))
    {
        return Err(protocol::ContractError("fixture_arguments_missing"));
    }
    if kind == "caps" {
        consent::caps(&v["caps"], &v["policy"], &v["device_memory_bytes"])
    } else {
        consent::standing_admission(
            &v["grant"],
            &v["binding"],
            &v["offer"],
            v["resource_violation"].as_str(),
        )
        .map(|s| s.map(Value::from).unwrap_or(Value::Null))
    }
}
// O:incident:F1; O:incident:F3. Pure predicates, not lifecycle/physical proof.
#[test]
fn standing_caps_and_admission_match_pinned_predicate_oracle() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/expanded-consent-vectors.json")).unwrap();
    for r in rows {
        let kind = r["kind"].as_str().unwrap();
        let base = &r["value"];
        assert_eq!(
            canonical::to_bytes(&call(kind, base).unwrap()).unwrap(),
            r["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in r["cases"].as_array().unwrap().iter().enumerate() {
            let v = changed(base, c);
            let actual = call(kind, &v);
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "{kind} case {i}: {c}; {actual:?}"
            );
            match actual {
                Ok(v) => assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes(),
                    "{kind} case {i}: {c}"
                ),
                Err(e) if c["exception"] == "StandingConsentError" => {
                    assert_eq!(e.0, c["error"].as_str().unwrap(), "case {i}")
                }
                Err(e) => assert!(!e.0.is_empty()),
            }
        }
    }
}
