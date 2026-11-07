// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{canonical, schemas};
use serde_json::Value;
#[test]
fn observatory_positive_bytes_and_negative_corpus() {
    // O:contract:mycelium.observatory.snapshot.v1 and event.v1.
    // Source refusals contain paths/prose; Rust exposes stable non-leaking codes.
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/continued-observatory-vectors.json"
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
            let v = support::mutate(base, c);
            let actual = schemas::validate_as(p, &v, 0);
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "{} case {i}: {c}; {actual:?}",
                row["name"]
            );
            match actual {
                Ok(()) => assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes()
                ),
                Err(e) => assert_eq!(e.0, "observatory_document_invalid"),
            }
        }
        // Independently exercises automatic schema selection as well.
        let mut positive = row.clone();
        positive["cases"] = serde_json::json!([]);
        support::corpus(&serde_json::to_string(&[positive]).unwrap(), |_, v| {
            schemas::validate(v, 0)
        });
    }
}
