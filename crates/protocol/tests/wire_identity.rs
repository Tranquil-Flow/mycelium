// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{
    canonical,
    identity::{self, Identity},
    strict,
};
use serde_json::Value;

#[test]
fn oracle_signature_and_canonical_bytes_are_exact() {
    let f: Value = serde_json::from_str(include_str!("../fixtures/signature.json")).unwrap();
    let id = Identity::from_secret(f["endpoint_id"].as_str().unwrap(), &[0x11; 32]).unwrap();
    assert_eq!(
        canonical::to_bytes(&f["statement"]).unwrap(),
        f["canonical_utf8"].as_str().unwrap().as_bytes()
    );
    assert_eq!(id.public_key_record(), f["public_key_record"]);
    assert_eq!(id.sign(&f["statement"]).unwrap(), f["signature"]);
    identity::verify(&f["statement"], &f["signature"], &f["public_key_record"]).unwrap();
    let mut wrong = f["statement"].clone();
    wrong["int"] = 43.into();
    assert!(identity::verify(&wrong, &f["signature"], &f["public_key_record"]).is_err());
}

#[test]
fn strict_ingress_rejects_nested_duplicates_even_escaped_keys() {
    for raw in [
        br#"{"x":1,"x":2}"#.as_slice(),
        br#"{"x":{"a":1,"\u0061":1}}"#,
        br#"{"x":[{"a":1,"a":1}]}"#,
    ] {
        assert!(
            strict::object(raw, 4096).is_err(),
            "accepted duplicate {:?}",
            raw
        );
    }
}

#[test]
fn canonical_ingress_rejects_noncanonical_and_nonobject() {
    for raw in [
        b"[]".as_slice(),
        b"{} ",
        b"{\"b\":1,\"a\":2}",
        b"{\"x\":NaN}",
        b"{\"x\":1e999}",
    ] {
        assert!(
            strict::canonical_object(raw, 4096).is_err(),
            "accepted {:?}",
            raw
        );
    }
    assert_eq!(
        strict::canonical_object(b"{}", 2).unwrap(),
        serde_json::json!({})
    );
    assert!(strict::canonical_object(b"{}", 1).is_err());
}

#[test]
fn inventory_has_all_87_unique_hash_pinned_fixtures() {
    use sha2::{Digest, Sha256};
    let f: Value = serde_json::from_str(include_str!("../fixtures/inventory.json")).unwrap();
    let rows = f["contracts"].as_array().unwrap();
    assert_eq!(rows.len(), 87);
    let mut ids = std::collections::BTreeSet::new();
    for row in rows {
        assert!(ids.insert(row["protocol"].as_str().unwrap()));
        let name = std::path::Path::new(row["fixture"]["path"].as_str().unwrap())
            .file_name()
            .unwrap();
        let raw = std::fs::read(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("fixtures/contracts")
                .join(name),
        )
        .unwrap();
        assert_eq!(
            raw.len() as u64,
            row["fixture"]["size_bytes"].as_u64().unwrap()
        );
        assert_eq!(
            hex::encode(Sha256::digest(&raw)),
            row["fixture"]["sha256"].as_str().unwrap()
        );
    }
}
