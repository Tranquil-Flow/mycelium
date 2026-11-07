// SPDX-License-Identifier: AGPL-3.0-only
//! Canonical prerequisite regressions; no native/oracle evidence is fabricated.
use std::{collections::BTreeMap, ffi::OsStr, path::Path};
use xtask::{policy::Date, preflight::Preflight};

#[test]
fn journey_detector_names_the_real_manifest_not_the_package_name() {
    let manifest: serde_json::Value =
        serde_json::from_str(include_str!("../prerequisites.json")).unwrap();
    let tool = manifest["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["name"] == "journey-crate")
        .unwrap();
    assert_eq!(
        tool["detect"],
        serde_json::json!({"kind":"file","path":"test-support/journeys/Cargo.toml"})
    );
    let group = manifest["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|g| g["id"] == "oracle-journeys")
        .unwrap();
    assert!(group["mandatory"].as_bool().unwrap());
    assert!(
        group["needs"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("oracle-manifest"))
    );
}

#[test]
fn canonical_native_and_oracle_groups_refuse_unset_empty_missing_or_directory_evidence() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    assert!(root.join("test-support/journeys/Cargo.toml").is_file());
    let p = Preflight::parse(
        include_str!("../prerequisites.json"),
        include_str!("../quarantine.json"),
        Date::parse("2026-10-27").unwrap(),
    )
    .unwrap();
    for value in [
        None,
        Some(""),
        Some("absent-l12-evidence-manifest.json"),
        Some(root.to_str().unwrap()),
    ] {
        let mut env = BTreeMap::new();
        for name in ["MYCELIUM_NATIVE_INPUT_MANIFEST", "MYCELIUM_ORACLE_MANIFEST"] {
            if let Some(value) = value {
                env.insert(name.into(), value.into());
            }
        }
        let statuses = p
            .evaluate(
                root,
                OsStr::new(""),
                &env,
                &["native-adapters".into(), "oracle-journeys".into()],
            )
            .unwrap();
        for (group, tool, variable) in [
            (
                "native-adapters",
                "native-input-manifest",
                "MYCELIUM_NATIVE_INPUT_MANIFEST",
            ),
            (
                "oracle-journeys",
                "oracle-manifest",
                "MYCELIUM_ORACLE_MANIFEST",
            ),
        ] {
            let status = statuses.iter().find(|s| s.group == group).unwrap();
            assert!(status.mandatory);
            let expected = match value {
                None | Some("") => format!("{tool}: environment {variable} is unset"),
                Some(v) if v == root.to_str().unwrap() => format!("{tool}: not a regular file"),
                Some(_) => format!("{tool}: missing file"),
            };
            assert!(
                status.reason.as_ref().unwrap().contains(&expected),
                "{}",
                status.render()
            );
            assert!(status.render().contains("NOT_RUN(BLOCKED:"));
            assert!(!status.render().contains("journey-crate: missing file"));
        }
    }
}

#[test]
fn cli_cannot_turn_missing_evidence_into_a_successful_preflight() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args([
            "preflight",
            "--group",
            "native-adapters",
            "--group",
            "oracle-journeys",
        ])
        .env_remove("MYCELIUM_NATIVE_INPUT_MANIFEST")
        .env_remove("MYCELIUM_ORACLE_MANIFEST")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    let stdout = String::from_utf8(output.stdout).unwrap();
    for name in ["MYCELIUM_NATIVE_INPUT_MANIFEST", "MYCELIUM_ORACLE_MANIFEST"] {
        assert!(
            stdout.contains(&format!("environment {name} is unset")),
            "{stdout}"
        );
    }
    for group in ["native-adapters", "oracle-journeys"] {
        assert!(
            stdout.contains(&format!("{group}: NOT_RUN(BLOCKED:")),
            "{stdout}"
        );
    }
}
