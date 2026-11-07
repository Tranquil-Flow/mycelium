// SPDX-License-Identifier: AGPL-3.0-only
use obligations::contracts::{self, validate_binding};
use serde_json::json;

#[test]
fn accepted_contracts_preserve_every_protocol_destination_obligation_and_fixture() {
    let binding = contracts::checked_in_binding().unwrap();
    let manifest = contracts::accepted_manifest().unwrap();
    let (history, _) = obligations::checked_in().unwrap();
    assert_eq!(validate_binding(&binding, &manifest, &history), Ok(()));
    assert_eq!(binding.contracts.len(), history.contracts.len());
    assert_ne!(binding.source_sha, history.source_sha);
}

fn corrupt(field: &str) {
    let mut binding = contracts::checked_in_binding().unwrap();
    let manifest = contracts::accepted_manifest().unwrap();
    let (history, _) = obligations::checked_in().unwrap();
    match field {
        "source" => binding.source_sha = history.source_sha.clone(),
        "manifest_sha256" => binding.manifest.sha256 = "0".repeat(64),
        "manifest_size" => binding.manifest.size_bytes += 1,
        "manifest_path" => binding.manifest.path.push_str(".unreviewed"),
        "protocol_missing" => {
            binding.contracts.remove(0);
        }
        "protocol_extra" => {
            let mut r = binding.contracts[0].clone();
            r.source.push_str(".new");
            binding.contracts.push(r);
        }
        "protocol_duplicate" => binding.contracts.push(binding.contracts[0].clone()),
        "obligation" => binding.contracts[0].obligation_id.push_str("-retired"),
        "destination" => binding.contracts[0].destination.push_str("-retired"),
        "fixture" => {
            binding.contracts[0].evidence.get_mut("fixture").unwrap()["sha256"] =
                json!("0".repeat(64));
        }
        "owner_missing" => {
            binding.contracts[0]
                .evidence
                .get_mut("owner_sources")
                .unwrap()
                .as_array_mut()
                .unwrap()
                .remove(0);
        }
        "owner_extra" => {
            let owners = binding.contracts[0]
                .evidence
                .get_mut("owner_sources")
                .unwrap()
                .as_array_mut()
                .unwrap();
            owners.push(owners[0].clone());
        }
        key => {
            let owners = binding.contracts[0]
                .evidence
                .get_mut("owner_sources")
                .unwrap()
                .as_array_mut()
                .unwrap();
            owners[0][key] = match key {
                "sha256" => json!("0".repeat(64)),
                "size_bytes" => json!(0),
                "path" => json!("unreviewed.py"),
                _ => unreachable!(),
            };
        }
    }
    assert!(
        validate_binding(&binding, &manifest, &history).is_err(),
        "admitted {field}"
    );
}
macro_rules! rejects {
    ($($name:ident => $field:literal),* $(,)?) => { $(#[test] fn $name() { corrupt($field); })* };
}
rejects! {
    refuses_provisional_authority => "source",
    refuses_manifest_hash => "manifest_sha256",
    refuses_manifest_size => "manifest_size",
    refuses_manifest_path => "manifest_path",
    refuses_missing_protocol => "protocol_missing",
    refuses_extra_protocol => "protocol_extra",
    refuses_duplicate_protocol => "protocol_duplicate",
    refuses_dropped_obligation => "obligation",
    refuses_changed_destination => "destination",
    refuses_relabelled_fixture => "fixture",
    refuses_missing_owner => "owner_missing",
    refuses_extra_owner => "owner_extra",
    refuses_owner_hash => "sha256",
    refuses_owner_size => "size_bytes",
    refuses_owner_path => "path",
}

#[test]
fn accepted_changed_owner_pins_are_not_the_historical_path_set() {
    let binding = contracts::checked_in_binding().unwrap();
    let old = contracts::historical_manifest().unwrap();
    let errors = contracts::validate_crosswalk(&binding.contracts, &old);
    assert_eq!(errors.len(), 14);
    assert!(
        errors
            .iter()
            .all(|e| e.code == "contract_evidence_identity")
    );
}

#[test]
fn an_authority_owner_without_a_path_obligation_is_unexplained() {
    let mut binding = contracts::checked_in_binding().unwrap();
    let mut manifest = contracts::accepted_manifest().unwrap();
    let (history, _) = obligations::checked_in().unwrap();
    manifest.contracts[0].owner_sources[0].path = "new-owner.py".into();
    binding.contracts[0]
        .evidence
        .get_mut("owner_sources")
        .unwrap()[0]["path"] = json!("new-owner.py");
    assert!(validate_binding(&binding, &manifest, &history).is_err());
}

#[test]
fn conflicting_shared_source_pin_is_rejected() {
    let mut manifest = contracts::accepted_manifest().unwrap();
    let first = manifest.contracts[0].owner_sources[0].clone();
    manifest.contracts[1].owner_sources.push(first);
    manifest.contracts[1]
        .owner_sources
        .last_mut()
        .unwrap()
        .sha256 = "0".repeat(64);
    assert!(contracts::source_objects(&manifest).is_err());
}
