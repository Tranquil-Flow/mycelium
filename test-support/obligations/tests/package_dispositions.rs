// SPDX-License-Identifier: AGPL-3.0-only
//! Final ZIP coverage is independent of the provisional source census.
use obligations::package::{
    EntryKind, PackageCensus, PackageDisposition, PackageRegistry, checked_in_package,
    validate_package,
};
use obligations::{Registry, Violation, checked_in};

fn data() -> (PackageRegistry, PackageCensus, Registry) {
    let (registry, census) = checked_in_package().unwrap();
    let (source, _) = checked_in().unwrap();
    (registry, census, source)
}
fn has(errors: &[Violation], code: &str, subject: &str) {
    assert!(
        errors
            .iter()
            .any(|e| e.code == code && e.subject == subject),
        "missing {code} for {subject}"
    );
}
fn mutate(
    f: impl FnOnce(&mut PackageRegistry, &mut PackageCensus, &mut Registry),
) -> Vec<Violation> {
    let (mut registry, mut census, mut source) = data();
    f(&mut registry, &mut census, &mut source);
    validate_package(&registry, &census, &source).expect_err("invalid package was admitted")
}

#[test]
fn released_zip_has_zero_unexplained_paths() {
    let (registry, census, source) = data();
    let coverage = validate_package(&registry, &census, &source).unwrap();
    // Independently measured ZIP and archive tables, not implementation outputs.
    assert_eq!(coverage.zip_entries, 1552);
    assert_eq!(coverage.directories, 182);
    assert_eq!(coverage.dispositioned_zip_entries, 1370);
    assert_eq!(coverage.embedded_entries, 2631);
    assert_eq!(coverage.unexplained_paths, 0);
}

#[test]
fn original_637_gaps_are_exactly_dispositioned_and_each_is_required() {
    let prior: serde_json::Value = serde_json::from_str(include_str!(
        "../../../evidence/foundation-exit/package-reconciliation.json"
    ))
    .unwrap();
    let original = prior["unexplained_paths"].as_array().unwrap();
    assert_eq!(original.len(), 637);
    let (registry, census, source) = data();
    let prior_paths: std::collections::BTreeSet<_> = prior["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["path"].as_str().unwrap())
        .collect();
    let actual_paths = census
        .entries
        .iter()
        .filter(|e| e.scope == "zip")
        .map(|e| e.path.as_str())
        .collect();
    assert_eq!(prior_paths, actual_paths);
    for value in original {
        let path = value.as_str().unwrap();
        let mut changed = registry.clone();
        let before: usize = changed.families.iter().map(|f| f.members.len()).sum();
        for family in &mut changed.families {
            family.members.retain(|m| m.path != path);
        }
        let after: usize = changed.families.iter().map(|f| f.members.len()).sum();
        assert_eq!(
            before - after,
            1,
            "each exact path must have one disposition"
        );
        has(
            &validate_package(&changed, &census, &source).unwrap_err(),
            "unexplained_package_path",
            path,
        );
    }
}

#[test]
fn new_same_shape_header_does_not_inherit_a_family_disposition() {
    let errors = mutate(|r, c, _| {
        let family = r
            .families
            .iter()
            .find(|f| f.id == "mlx-wheel-sdk-headers")
            .unwrap();
        let mut entry = c
            .entries
            .iter()
            .find(|e| e.path == family.members[0].path)
            .unwrap()
            .clone();
        entry.path = format!("{}NEW-UNREVIEWED.h", family.rule.prefix);
        assert!(
            family.rule.matches(&entry),
            "exercise the tempting wildcard case"
        );
        c.entries.push(entry);
    });
    assert!(
        errors.iter().any(
            |e| e.code == "unexplained_package_path" && e.subject.ends_with("NEW-UNREVIEWED.h")
        )
    );
}

#[test]
fn new_embedded_dependency_does_not_inherit_a_retirement() {
    let errors = mutate(|r, c, _| {
        let family = r
            .families
            .iter()
            .find(|f| f.id == "embedded-dependency:numpy")
            .unwrap();
        let mut entry = c
            .entries
            .iter()
            .find(|e| e.path == family.members[0].path)
            .unwrap()
            .clone();
        entry.path = format!("{}.NEW_UNREVIEWED", family.rule.prefix);
        assert!(family.rule.matches(&entry));
        c.entries.push(entry);
    });
    assert!(
        errors
            .iter()
            .any(|e| e.code == "unexplained_package_path" && e.subject.ends_with("NEW_UNREVIEWED"))
    );
}

#[test]
fn a_new_zip_native_binary_still_fails() {
    let errors = mutate(|_, c, _| {
        let mut entry = c
            .entries
            .iter()
            .find(|e| e.path.ends_with("/native/cloudflared"))
            .unwrap()
            .clone();
        entry.path.push_str("-unreviewed");
        c.entries.push(entry);
    });
    assert!(
        errors.iter().any(|e| e.code == "unexplained_package_path"
            && e.subject.ends_with("cloudflared-unreviewed"))
    );
}

#[test]
fn package_hash_and_size_are_independently_bound() {
    for change_hash in [true, false] {
        let errors = mutate(|r, _, _| {
            let member = &mut r.families[0].members[0];
            if change_hash {
                member.sha256 = "0".repeat(64);
            } else {
                member.size_bytes += 1;
            }
        });
        assert!(errors.iter().any(|e| e.code == "package_pin_mismatch"));
    }
}

#[test]
fn native_runtime_cannot_be_swallowed_by_sdk_header_family() {
    let errors = mutate(|r, _, _| {
        let native = r
            .families
            .iter_mut()
            .find(|f| f.id == "mlx-native-runtime")
            .unwrap()
            .members
            .pop()
            .unwrap();
        let headers = r
            .families
            .iter_mut()
            .find(|f| f.id == "mlx-wheel-sdk-headers")
            .unwrap();
        headers.members.push(native);
    });
    assert!(errors.iter().any(|e| e.code == "package_family_mismatch"));
}

#[test]
fn every_review_field_and_provenance_is_required() {
    for field in ["component", "reason", "reviewer", "citation", "evidence"] {
        let errors = mutate(|r, _, _| {
            let family = r
                .families
                .iter_mut()
                .find(|f| f.disposition == PackageDisposition::Retire)
                .unwrap();
            match field {
                "component" => family.component.clear(),
                "reason" => family.reason.clear(),
                "reviewer" => family.reviewer.clear(),
                "citation" => family.citation.clear(),
                "evidence" => family.evidence.clear(),
                _ => unreachable!(),
            }
        });
        assert!(errors.iter().any(|e| e.code == "package_review_missing"));
    }
}

#[test]
fn dangling_destination_is_not_a_disposition() {
    let errors = mutate(|r, _, _| r.families[0].destination = "invented::module".into());
    assert!(
        errors
            .iter()
            .any(|e| e.code == "unknown_package_destination")
    );
}

#[test]
fn source_crosswalk_cannot_silently_change_destination() {
    let errors = mutate(|r, _, _| {
        let family = r
            .families
            .iter_mut()
            .find(|f| f.members[0].source_path.is_some())
            .unwrap();
        family.destination = if family.destination == "desktop::quit" {
            "model::shape"
        } else {
            "desktop::quit"
        }
        .into();
    });
    assert!(errors.iter().any(|e| e.code == "package_source_crosswalk"));
}

#[test]
fn loose_source_and_web_pin_the_accepted_bytes() {
    let errors = mutate(|r, _, _| {
        let family = r
            .families
            .iter_mut()
            .find(|f| f.disposition == PackageDisposition::WebRetained)
            .unwrap();
        family.members[0].source_sha256 = Some("0".repeat(64));
    });
    assert!(errors.iter().any(|e| e.code == "package_source_crosswalk"));
}

#[test]
fn duplicate_family_and_member_are_rejected() {
    let errors = mutate(|r, _, _| r.families.push(r.families[0].clone()));
    assert!(errors.iter().any(|e| e.code == "duplicate_package_family"));
    assert!(
        errors
            .iter()
            .any(|e| e.code == "duplicate_package_disposition")
    );
}

#[test]
fn duplicate_census_and_unlisted_member_are_rejected() {
    let errors = mutate(|_, c, _| c.entries.push(c.entries[0].clone()));
    assert!(errors.iter().any(|e| e.code == "duplicate_package_path"));
    let errors = mutate(|r, _, _| r.families[0].members[0].path.push_str("-not-in-zip"));
    assert!(
        errors
            .iter()
            .any(|e| e.code == "unlisted_package_disposition")
    );
    assert!(errors.iter().any(|e| e.code == "unexplained_package_path"));
}

#[test]
fn archive_and_accepted_source_identity_cannot_drift() {
    for source in [true, false] {
        let errors = mutate(|r, _, _| {
            if source {
                r.source_sha = "0".repeat(40);
            } else {
                r.archive_sha256 = "0".repeat(64);
            }
        });
        has(&errors, "package_identity", "package");
    }
}

#[test]
fn empty_census_cannot_claim_zero_unexplained() {
    let errors = mutate(|r, c, _| {
        r.families.clear();
        c.entries.clear();
    });
    has(&errors, "empty_package_census", "package");
}

#[test]
fn container_disposition_never_covers_omitted_members() {
    let errors = mutate(|r, _, _| {
        for f in &mut r.families {
            f.members.retain(|m| !m.path.contains("!/"));
        }
    });
    assert_eq!(
        errors
            .iter()
            .filter(|e| e.code == "unexplained_package_path")
            .count(),
        2631
    );
}

#[test]
fn embedded_member_requires_a_real_container() {
    let errors = mutate(|_, c, _| {
        c.entries
            .iter_mut()
            .find(|e| e.scope == "pyz")
            .unwrap()
            .container = Some("missing-archive".into());
    });
    assert!(errors.iter().any(|e| e.code == "invalid_package_container"));
}

#[test]
fn symlink_identity_is_not_dereferenced_content() {
    let (r, c, s) = data();
    assert_eq!(
        c.entries
            .iter()
            .filter(|e| e.kind == EntryKind::Symlink)
            .count(),
        55
    );
    assert!(validate_package(&r, &c, &s).is_ok());
    let errors = mutate(|_, c, _| {
        let link = c
            .entries
            .iter_mut()
            .find(|e| e.kind == EntryKind::Symlink)
            .unwrap();
        link.link_target = Some("/outside-package/secret".into());
    });
    assert!(errors.iter().any(|e| e.code == "invalid_package_link"));
    assert!(errors.iter().any(|e| e.code == "package_pin_mismatch"));
}

#[test]
fn directories_do_not_hide_files_or_grant_prefix_coverage() {
    let errors = mutate(|_, c, _| {
        let directory = c
            .entries
            .iter_mut()
            .find(|e| e.kind == EntryKind::Directory)
            .unwrap();
        directory.size_bytes = 1;
    });
    assert!(
        errors
            .iter()
            .any(|e| e.code == "nonempty_package_directory")
    );
}

#[test]
fn wildcard_rule_and_unknown_summary_fields_are_rejected() {
    let errors = mutate(|r, _, _| r.families[0].rule.prefix = "*".into());
    assert!(
        errors
            .iter()
            .any(|e| e.code == "invalid_package_family_rule")
    );
    let (r, _, _) = data();
    let mut value = serde_json::to_value(r).unwrap();
    value["unexplained_paths"] = 0.into();
    assert!(serde_json::from_value::<PackageRegistry>(value).is_err());
}
