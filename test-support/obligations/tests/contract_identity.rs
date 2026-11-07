// SPDX-License-Identifier: AGPL-3.0-only
use obligations::{checked_in, validate};
use serde_json::json;

#[test]
fn contract_owner_hash_size_path_and_missing_members_are_not_advisory() {
    for field in ["sha256", "size_bytes", "path", "missing"] {
        let (mut registry, snapshot) = checked_in().unwrap();
        let owners = registry.contracts[0].evidence["owner_sources"]
            .as_array()
            .unwrap()
            .clone();
        let mut changed = owners;
        match field {
            "sha256" => changed[0][field] = json!("0".repeat(64)),
            "size_bytes" => changed[0][field] = json!(0),
            "path" => changed[0][field] = json!("unreviewed.py"),
            "missing" => {
                changed.remove(0);
            }
            _ => unreachable!(),
        }
        registry.contracts[0]
            .evidence
            .insert("owner_sources".into(), json!(changed));
        assert!(
            validate(&registry, &snapshot).is_err(),
            "accepted corrupted contract {field}"
        );
    }
}

#[test]
fn original_contract_binding_is_a_positive_control() {
    let (registry, snapshot) = checked_in().unwrap();
    assert_eq!(validate(&registry, &snapshot), Ok(()));
}
