// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{ContractError, canonical};
use serde_json::Value;
pub fn mutate(base: &Value, case: &Value) -> Value {
    let mut v = base.clone();
    let path = case["path"].as_array().unwrap();
    if path.is_empty() {
        return case["replacement"].clone();
    }
    let mut at = &mut v;
    for p in &path[..path.len() - 1] {
        at = if let Some(s) = p.as_str() {
            &mut at[s]
        } else {
            &mut at[p.as_u64().unwrap() as usize]
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
    v
}
pub fn corpus(raw: &str, validate: impl Fn(&str, &Value) -> Result<(), ContractError>) {
    let rows: Vec<Value> = serde_json::from_str(raw).unwrap();
    assert!(!rows.is_empty());
    for row in rows {
        let base = &row["value"];
        let protocol = base["protocol"].as_str().unwrap();
        validate(protocol, base).unwrap();
        assert_eq!(
            canonical::to_bytes(base).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let mut v = mutate(base, c);
            if let Some(digest) = c.get("digest_override") {
                v["digest"] = digest.clone();
            }
            let actual = validate(protocol, &v);
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "{} case {i}: {c}; {actual:?}",
                row["name"]
            );
            if let Err(e) = actual {
                if c["exception"] == "TypeError" || c["exception"] == "OverflowError" {
                    // Python runtime exceptions have no stable protocol code.
                    assert!(!e.0.is_empty());
                    continue;
                }
                assert_eq!(
                    e.0,
                    c["error"].as_str().unwrap(),
                    "{} case {i}: {c}",
                    row["name"]
                );
            } else if let Some(bytes) = c["canonical_utf8"].as_str() {
                assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    bytes.as_bytes(),
                    "{} case {i}",
                    row["name"]
                );
            }
        }
    }
}
