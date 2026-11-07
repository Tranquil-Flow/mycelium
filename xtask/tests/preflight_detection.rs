// SPDX-License-Identifier: AGPL-3.0-only
use std::{
    collections::BTreeMap,
    ffi::{OsStr, OsString},
    fs,
    path::PathBuf,
};
use xtask::{policy::Date, preflight::Preflight};
fn preflight(detection: serde_json::Value) -> Preflight {
    let manifest = serde_json::json!({"version":1,"tools":[{"name":"input","detect":detection}],"groups":[{"id":"test","kind":"custom","mandatory":true,"needs":["input"],"after":[],"command":["never-execute"]}]});
    Preflight::parse(
        &manifest.to_string(),
        r#"{"version":1,"entries":[]}"#,
        Date::parse("2026-10-26").unwrap(),
    )
    .unwrap()
}
#[test]
fn env_file_is_not_green_when_absent_or_a_directory() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let p = preflight(serde_json::json!({"kind":"env_file","variable":"INPUT"}));
    for environment in [
        BTreeMap::new(),
        BTreeMap::from([("INPUT".into(), root.as_os_str().to_owned())]),
    ] {
        assert!(
            p.evaluate(&root, OsStr::new(""), &environment, &[])
                .unwrap()[0]
                .reason
                .is_some()
        );
    }
    let environment = BTreeMap::from([("INPUT".into(), root.join("Cargo.toml").into_os_string())]);
    assert!(
        p.evaluate(&root, OsStr::new(""), &environment, &[])
            .unwrap()[0]
            .reason
            .is_none()
    );
}
#[test]
#[cfg(unix)]
fn executable_probe_only_inspects_metadata_never_runs_tool() {
    use std::os::unix::fs::PermissionsExt;
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../target")
        .join(format!("xtask-probe-{}", std::process::id()));
    fs::create_dir_all(&root).unwrap();
    let tool = root.join("do-not-execute");
    fs::write(
        &tool,
        "not a runnable binary: presence probe must not execute this",
    )
    .unwrap();
    let p = preflight(serde_json::json!({"kind":"path_executable","program":"do-not-execute"}));
    fs::set_permissions(&tool, fs::Permissions::from_mode(0o600)).unwrap();
    assert!(
        p.evaluate(&root, root.as_os_str(), &BTreeMap::new(), &[])
            .unwrap()[0]
            .reason
            .is_some()
    );
    fs::set_permissions(&tool, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(
        p.evaluate(&root, root.as_os_str(), &BTreeMap::new(), &[])
            .unwrap()[0]
            .reason
            .is_none()
    );
    assert!(
        p.evaluate(
            &root,
            &OsString::from("relative/path"),
            &BTreeMap::new(),
            &[]
        )
        .unwrap()[0]
            .reason
            .is_some()
    );
    fs::remove_dir_all(root).unwrap();
}
