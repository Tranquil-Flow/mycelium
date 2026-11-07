// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{canonical, router_wire};
use serde_json::Value;
fn mutate(base: &Value, case: &Value) -> Value {
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
fn frame(envelope: &Value, payload: &[u8]) -> Vec<u8> {
    let header = canonical::to_ascii_bytes(envelope).unwrap();
    let mut out = (header.len() as u32).to_be_bytes().to_vec();
    out.extend(header);
    out.extend(payload);
    out
}
// O:contract:mycelium.router_wire.v1; O:path:mycelium_router/wire.py
#[test]
fn all_router_messages_match_exact_frames_and_full_negative_corpus() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continued-wire-vectors.json")).unwrap();
    assert_eq!(rows.len(), 18);
    for row in rows {
        let payload = hex::decode(row["payload_hex"].as_str().unwrap()).unwrap();
        let expected = hex::decode(row["encoded_hex"].as_str().unwrap()).unwrap();
        let decoded = router_wire::decode_frame(&expected).unwrap();
        assert_eq!(decoded.payload(), payload);
        assert_eq!(decoded.encode().unwrap(), expected);
        assert_eq!(
            router_wire::encode_frame(
                row["name"].as_str().unwrap(),
                &row["value"]["body"],
                &payload
            )
            .unwrap(),
            expected
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let v = mutate(&row["value"], c);
            let actual = router_wire::decode_frame(&frame(&v, &payload));
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "{} case {i}: {c}; {actual:?}",
                row["name"]
            );
            match actual {
                Ok(d) => {
                    if let Some(expected) = c["encoded_hex"].as_str() {
                        assert_eq!(
                            hex::encode(d.encode().unwrap()),
                            expected,
                            "{} case {i}: {c}",
                            row["name"]
                        );
                    } else {
                        assert!(d.encode().is_err(), "{} case {i}: {c}", row["name"]);
                    }
                }
                Err(e) if c["exception"] == "WireError" => assert_eq!(
                    e.0,
                    c["error"].as_str().unwrap(),
                    "{} case {i}: {c}",
                    row["name"]
                ),
                Err(e) => assert!(!e.0.is_empty()),
            }
        }
    }
}
#[test]
fn binary_framing_refuses_truncation_caps_digest_and_unknown_protocol() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continued-wire-framing.json")).unwrap();
    for c in rows {
        assert_eq!(
            router_wire::decode_frame(&hex::decode(c["frame_hex"].as_str().unwrap()).unwrap())
                .unwrap_err()
                .0,
            c["error"].as_str().unwrap()
        );
    }
}
