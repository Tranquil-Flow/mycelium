// SPDX-License-Identifier: AGPL-3.0-only
//! Evidence identity must be checked, not merely shaped like a SHA-256 digest.
use obligations::checked_in;
use obligations::package::{checked_in_package, validate_package};

#[test]
fn evidence_pin_must_match_the_recorded_package_or_source_object() {
    for field in ["sha256", "size_bytes", "path"] {
        let (mut registry, census) = checked_in_package().unwrap();
        let (source, _) = checked_in().unwrap();
        let pin = &mut registry.families[0].evidence[0];
        match field {
            "sha256" => pin.sha256 = "0".repeat(64),
            "size_bytes" => pin.size_bytes += 1,
            "path" => pin.path.push_str("-unreviewed"),
            _ => unreachable!(),
        }
        let errors = validate_package(&registry, &census, &source)
            .expect_err("unverified evidence pin was admitted");
        assert!(errors.iter().any(|e| e.code == "package_evidence_identity"));
    }
}

#[test]
fn embedded_source_hash_must_match_the_accepted_source_object() {
    let (mut registry, census) = checked_in_package().unwrap();
    let (source, _) = checked_in().unwrap();
    let member = registry
        .families
        .iter_mut()
        .flat_map(|f| &mut f.members)
        .find(|m| m.path.contains("!/") && m.source_path.is_some())
        .unwrap();
    member.source_sha256 = Some("0".repeat(64));
    let errors = validate_package(&registry, &census, &source)
        .expect_err("unverified embedded-source identity was admitted");
    assert!(errors.iter().any(|e| e.code == "package_source_crosswalk"));
}
