// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `mycelium_seed.authority.derive_product_pseudonym_salt` on cases recorded by executing the
//! oracle (`round9-seed-authority-probe.py`, scratch keys): the 32-byte salt per (seed key, endpoint, swarm id).
//! A Python accident (a lone surrogate cannot be canonicalised) cannot be expressed as a Rust `&str`.
use protocol::identity::Identity;
use protocol::seed_authority::derive_product_pseudonym_salt;
use serde_json::Value;

#[test]
fn product_pseudonym_salts_match_the_executed_oracle() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round9-seed-authority-vectors.json"
    ))
    .unwrap();
    let (mut derived, mut unrepresentable) = (0, 0);
    for case in v["cases"].as_array().unwrap() {
        let Some(swarm_id) = case["swarm_id"].as_str() else {
            assert_eq!(case["swarm_id_escape"], "lone_surrogate_d800");
            assert_eq!(case["accepted"], false);
            unrepresentable += 1;
            continue;
        };
        let identity = Identity::from_secret(
            case["endpoint"].as_str().unwrap(),
            &[u8::try_from(case["secret_byte"].as_u64().unwrap()).unwrap(); 32],
        )
        .unwrap();
        let salt = derive_product_pseudonym_salt(&identity, swarm_id).unwrap();
        assert_eq!(
            hex::encode(salt),
            case["salt_hex"].as_str().unwrap(),
            "{case}"
        );
        derived += 1;
    }
    assert_eq!((derived, unrepresentable), (80, 1));
}
