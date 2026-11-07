// SPDX-License-Identifier: AGPL-3.0-only
use obligations::{Registry, package, static_source as s};
fn inputs() -> (s::Binding, s::Manifest, Registry, package::PackageCensus) {
    (
        s::checked_in_binding().unwrap(),
        s::accepted_manifest().unwrap(),
        obligations::checked_in().unwrap().0,
        package::checked_in_package().unwrap().1,
    )
}
#[test]
fn accepted_static_source_is_valid_with_preserved_history() {
    let (b, m, h, c) = inputs();
    s::validate_binding(&b, &m, &h, &c).unwrap();
    s::validate_population(&m, &c).unwrap();
    assert_eq!(m.packaged_source_paths.len(), 725);
    assert_eq!(m.resource_paths.len(), 8);
    assert_eq!(b.profiles.len(), h.supported_cells.len());
    assert_eq!(b.retirements, 0);
}
#[test]
fn provisional_source_is_not_accepted() {
    let (mut b, mut m, h, c) = inputs();
    b.source_sha = h.source_sha.clone();
    m.source_sha = h.source_sha.clone();
    assert!(s::validate_binding(&b, &m, &h, &c).is_err());
}
#[test]
fn corrupt_digest_size_path_and_duplicate_source_are_refused() {
    let (b, m, h, c) = inputs();
    for mode in 0..5 {
        let mut broken = m.clone();
        match mode {
            0 => broken.files[0].sha256 = "0".repeat(64),
            1 => broken.files[0].size_bytes += 1,
            2 => broken.files[0].path = "../outside.py".into(),
            3 => broken.files.push(broken.files[0].clone()),
            _ => broken.files[0].path = "post.js".into(),
        }
        assert!(
            s::validate_binding(&b, &broken, &h, &c).is_err(),
            "mode {mode}"
        );
    }
}
#[test]
fn independent_package_population_detects_missing_closure_member() {
    let (_, mut m, _, c) = inputs();
    let removed = m.packaged_source_paths.remove(0);
    m.files.retain(|p| p.path != removed);
    // Does not consult the checked-in successor manifest/binding equality guard.
    assert!(s::validate_population(&m, &c).is_err());
}
#[test]
fn resource_and_profile_member_omission_refuses() {
    let (b, m, h, c) = inputs();
    let mut bad = m.clone();
    bad.resource_paths.pop();
    assert!(s::validate_binding(&b, &bad, &h, &c).is_err());
    let mut bad = b.clone();
    bad.profiles.pop();
    assert!(s::validate_binding(&bad, &m, &h, &c).is_err());
}
#[test]
fn catalogue_source_digest_size_and_missing_member_are_binding() {
    let (b, m, h, c) = inputs();
    for mode in 0..4 {
        let mut bad = c.clone();
        match mode {
            0 => bad.source_sha = h.source_sha.clone(),
            1 => bad.source_objects[0].sha256 = "0".repeat(64),
            2 => bad.source_objects[0].size_bytes += 1,
            _ => {
                bad.source_objects.remove(0);
            }
        }
        assert!(
            s::validate_binding(&b, &m, &h, &bad).is_err(),
            "mode {mode}"
        );
    }
}
#[test]
fn package_member_bytes_are_not_pathset_identity() {
    let (_, m, _, mut c) = inputs();
    let member = c
        .entries
        .iter_mut()
        .find(|e| e.path.contains("/a13-runtime-source/") && e.kind == package::EntryKind::File)
        .unwrap();
    member.sha256 = "0".repeat(64);
    assert!(s::validate_population(&m, &c).is_err());
}
#[test]
fn destinations_obligations_profiles_and_history_are_immutable() {
    let (b, m, h, c) = inputs();
    for key in ["destination", "obligation_id", "proof_state"] {
        let mut bad = b.clone();
        bad.crosswalk[0][key] = serde_json::json!("forged");
        assert!(s::validate_binding(&bad, &m, &h, &c).is_err());
    }
    let mut bad = b.clone();
    bad.profiles[0]["proof_state"] = serde_json::json!("QUALIFIED");
    assert!(s::validate_binding(&bad, &m, &h, &c).is_err());
    let mut bad = h.clone();
    bad.paths.pop();
    assert!(s::validate_binding(&b, &m, &bad, &c).is_err());
}
#[test]
fn unknown_fields_are_not_silently_discarded() {
    let (_, m, _, _) = inputs();
    let mut value = serde_json::to_value(m).unwrap();
    value["forged_authority"] = serde_json::json!(true);
    assert!(serde_json::from_value::<s::Manifest>(value).is_err());
}
