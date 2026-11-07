// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{canonical, loaded_proof};
use serde_json::Value;
#[test]
fn loader_authentication_matches_exact_bytes_and_independent_binding_refusals() {
    // O:contract:mycelium.layer_load_proof.v1.
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/remaining-load-proof-vectors.json"
    ))
    .unwrap();
    for row in rows {
        let base = &row["value"];
        loaded_proof::authenticate(&base["proof"], &base["binding"]).unwrap();
        let positive = serde_json::json!({"value":base["proof"],"canonical_utf8":row["canonical_utf8"],"cases":[]});
        support::corpus(&serde_json::to_string(&[positive]).unwrap(), |_, p| {
            loaded_proof::authenticate(p, &base["binding"])
        });
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let v = support::mutate(base, c);
            let r = loaded_proof::authenticate(&v["proof"], &v["binding"]);
            assert_eq!(
                r.is_ok(),
                c["accepted"] == true,
                "{} {i} {c}; {r:?}",
                row["name"]
            );
            if let Err(e) = r {
                if c["exception"] == "ValueError" {
                    assert_eq!(e.0, c["error"].as_str().unwrap(), "{} {i}", row["name"]);
                }
            } else {
                assert_eq!(
                    canonical::to_bytes(&v["proof"]).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes()
                );
            }
        }
    }
}
