// SPDX-License-Identifier: AGPL-3.0-only
//! CL-L12-04: retention keeps every dependency closure (active / native / npm / model / continuity /
//! rollback) and the offline preflight passes before and after a fixture cleanup. Every test builds
//! its own throw-away store under TMPDIR; the fixture holds data only.
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Command,
};
use xtask::retention::{self, Retention};

const EXE: &[u8] = b"active release executable bytes";
const LIB: &[u8] = b"native static library";
const MODEL: &[u8] = b"model weights, 40 bytes of stand-in data";
const PRIOR: &[u8] = b"{\"release\":\"v0.1.4\"}";

/// `(relative path, bytes)` of the store: closures first, then what a cleanup may remove.
fn store_files() -> Vec<(&'static str, Vec<u8>)> {
    vec![
        ("releases/active/Mycelium.app/exe", EXE.to_vec()),
        (".build-native/release/libx.a", LIB.to_vec()),
        ("npm-cache/_cacache/index-v5/aa", b"npm index".to_vec()),
        ("models/qwen/model.bin", MODEL.to_vec()),
        ("receipts/prior/v0.1.4.json", PRIOR.to_vec()),
        ("rollback/v0.1.4/payload.zip", b"rollback payload".to_vec()),
        ("old-shards/shard-1.bin", vec![1; 1000]),
        ("old-shards/shard-2.bin", vec![2; 2000]),
        ("tmp/scratch.txt", vec![3; 30]),
    ]
}
fn sha(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
fn manifest_json() -> String {
    serde_json::json!({
        "schema": 1,
        "preserve": ["active-release", "prior-receipts", "rollback-v014"],
        "closures": [
            {"id": "active-release", "kind": "active",
             "members": [{"path": "releases/active/Mycelium.app/exe", "sha256": sha(EXE)}],
             "requires": ["native-build", "npm-offline", "model-qwen"]},
            {"id": "native-build", "kind": "native", "members": [{"path": ".build-native"}]},
            {"id": "npm-offline", "kind": "npm", "members": [{"path": "npm-cache"}]},
            {"id": "model-qwen", "kind": "model",
             "members": [{"path": "models/qwen/model.bin", "sha256": sha(MODEL)}]},
            {"id": "prior-receipts", "kind": "continuity", "members": [{"path": "receipts/prior"}]},
            {"id": "rollback-v014", "kind": "rollback", "members": [{"path": "rollback/v0.1.4"}]}
        ]
    })
    .to_string()
}
fn edit_manifest(edit: impl FnOnce(&mut serde_json::Value)) -> String {
    let mut value: serde_json::Value = serde_json::from_str(&manifest_json()).unwrap();
    edit(&mut value);
    value.to_string()
}
const PREREQUISITES: &str = r#"{"version":1,
 "tools":[{"name":"native-lib","detect":{"kind":"file","path":".build-native/release/libx.a"}}],
 "groups":[{"id":"unit","kind":"unit","mandatory":true,"needs":["native-lib"],"after":[],"command":["cargo","test"]}]}"#;
const QUARANTINE: &str = r#"{"version":1,"entries":[]}"#;

struct Store {
    root: PathBuf,
    manifest: PathBuf,
}
impl Store {
    fn new(tag: &str) -> Self {
        Self::with_manifest(tag, &manifest_json())
    }
    fn with_manifest(tag: &str, manifest: &str) -> Self {
        let base = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join("xtask-retention-tests")
            .join(format!("{}-{tag}", std::process::id()));
        match fs::remove_dir_all(&base) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                eprintln!("fixture root absent before setup: {}", base.display());
            }
            Err(e) => panic!("fixture setup cleanup: {e}"),
        }
        let root = base.join("store");
        for (path, bytes) in store_files() {
            let full = root.join(path);
            fs::create_dir_all(full.parent().unwrap()).unwrap();
            fs::write(full, bytes).unwrap();
        }
        fs::create_dir_all(root.join("xtask")).unwrap();
        fs::write(root.join("xtask/prerequisites.json"), PREREQUISITES).unwrap();
        fs::write(root.join("xtask/quarantine.json"), QUARANTINE).unwrap();
        fs::write(
            root.join(retention::FIXTURE_MARKER),
            sha(manifest.as_bytes()),
        )
        .unwrap();
        let manifest_path = base.join("retention.json");
        fs::write(&manifest_path, manifest).unwrap();
        Self {
            root,
            manifest: manifest_path,
        }
    }
    fn retention(&self) -> Retention {
        Retention::parse(&fs::read_to_string(&self.manifest).unwrap()).unwrap()
    }
    fn exists(&self, path: &str) -> bool {
        self.root.join(path).exists()
    }
    /// Every file under the store: path -> sha256 (independent of the tool under test).
    fn digest(&self) -> BTreeMap<String, String> {
        fn walk(dir: &Path, base: &Path, out: &mut BTreeMap<String, String>) {
            for entry in fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    walk(&path, base, out);
                } else {
                    out.insert(
                        path.strip_prefix(base).unwrap().display().to_string(),
                        sha(&fs::read(&path).unwrap()),
                    );
                }
            }
        }
        let mut out = BTreeMap::new();
        walk(&self.root, &self.root, &mut out);
        out
    }
    fn cli(&self, mode: &str, candidates: &[&str], extra: &[&str]) -> std::process::Output {
        let list = self.root.parent().unwrap().join("candidates.txt");
        fs::write(&list, candidates.join("\n")).unwrap();
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .arg("retention")
            .arg(mode)
            .args(["--manifest", &self.manifest.display().to_string()])
            .args(["--root", &self.root.display().to_string()])
            .args(["--candidates", &list.display().to_string()])
            .args(extra)
            .output()
            .unwrap()
    }
}
impl Drop for Store {
    fn drop(&mut self) {
        if let Some(base) = self.root.parent()
            && let Err(e) = fs::remove_dir_all(base)
        {
            eprintln!("fixture cleanup failed for {}: {e}", base.display());
        }
    }
}
fn codes(findings: &[retention::Finding]) -> Vec<&'static str> {
    findings.iter().map(|f| f.code).collect()
}
fn out_text(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stdout).to_string()
}

// ------------------------------------------------------------------------------ the manifest

#[test]
fn the_base_manifest_protects_all_six_kinds() {
    let retention = Retention::parse(&manifest_json()).unwrap();
    assert_eq!(retention.manifest_sha256(), sha(manifest_json().as_bytes()));
    for kind in retention::KINDS {
        assert!(
            retention.protected_kinds().contains(&kind.to_string()),
            "{kind}"
        );
    }
    assert_eq!(retention.protected_kinds().len(), 6);
}

#[test]
fn a_closure_reached_only_through_requires_is_still_protected() {
    // Nothing preserves `native-build` directly: only the active closure requires it.
    let manifest = Retention::parse(&manifest_json()).unwrap();
    assert!(
        manifest
            .protected_ids()
            .contains(&"native-build".to_string())
    );
}

#[test]
fn a_manifest_that_leaves_a_kind_unprotected_is_refused() {
    // The v0.1.1 incident: the native cache was nobody's dependency, so a cleanup took it.
    for kind in ["native", "npm", "model"] {
        let text = edit_manifest(|m| {
            let closures = m["closures"].as_array_mut().unwrap();
            let active = closures
                .iter_mut()
                .find(|c| c["id"] == "active-release")
                .unwrap();
            let id = match kind {
                "native" => "native-build",
                "npm" => "npm-offline",
                _ => "model-qwen",
            };
            active["requires"]
                .as_array_mut()
                .unwrap()
                .retain(|r| r != id);
        });
        let error = Retention::parse(&text).err().unwrap_or_default();
        assert!(error.contains(kind), "{kind}: {error}");
    }
    for (kind, id) in [
        ("continuity", "prior-receipts"),
        ("rollback", "rollback-v014"),
        ("active", "active-release"),
    ] {
        let text = edit_manifest(|m| {
            m["preserve"].as_array_mut().unwrap().retain(|p| p != id);
        });
        let error = Retention::parse(&text).err().unwrap_or_default();
        assert!(error.contains(kind), "{kind}: {error}");
    }
}

#[test]
fn malformed_manifests_are_refused() {
    let cases: Vec<(&str, String)> = vec![
        ("schema", edit_manifest(|m| m["schema"] = 2.into())),
        (
            "unknown kind",
            edit_manifest(|m| m["closures"][1]["kind"] = "cache".into()),
        ),
        (
            "duplicate id",
            edit_manifest(|m| m["closures"][2]["id"] = "native-build".into()),
        ),
        (
            "unknown requires",
            edit_manifest(|m| m["closures"][0]["requires"][0] = "ghost".into()),
        ),
        (
            "unknown preserve",
            edit_manifest(|m| m["preserve"][0] = "ghost".into()),
        ),
        (
            "empty preserve",
            edit_manifest(|m| m["preserve"] = serde_json::json!([])),
        ),
        (
            "no members",
            edit_manifest(|m| m["closures"][1]["members"] = serde_json::json!([])),
        ),
        (
            "empty id",
            edit_manifest(|m| m["closures"][1]["id"] = "".into()),
        ),
        (
            "dotdot",
            edit_manifest(|m| m["closures"][1]["members"][0]["path"] = "../x".into()),
        ),
        (
            "absolute",
            edit_manifest(|m| m["closures"][1]["members"][0]["path"] = "/etc".into()),
        ),
        (
            "glob",
            edit_manifest(|m| m["closures"][1]["members"][0]["path"] = "a/*".into()),
        ),
        (
            "bad sha",
            edit_manifest(|m| m["closures"][0]["members"][0]["sha256"] = "abc".into()),
        ),
        (
            "upper sha",
            edit_manifest(|m| {
                m["closures"][0]["members"][0]["sha256"] = sha(EXE).to_uppercase().into()
            }),
        ),
        ("unknown field", edit_manifest(|m| m["extra"] = true.into())),
        ("not json", "{".to_string()),
    ];
    for (name, text) in cases {
        assert!(Retention::parse(&text).is_err(), "{name}");
    }
}

// ------------------------------------------------------------------------------ the dry run

#[test]
fn junk_may_go_and_the_plan_reports_exact_bytes_without_touching_anything() {
    let store = Store::new("plan-junk");
    let before = store.digest();
    let planned = retention::plan(
        &store.retention(),
        &store.root,
        &["old-shards".to_string(), "tmp/scratch.txt".to_string()],
    );
    assert_eq!(codes(&planned.findings), Vec::<&str>::new());
    assert_eq!(
        planned.deletable,
        vec![
            ("old-shards".to_string(), 3000),
            ("tmp/scratch.txt".to_string(), 30)
        ]
    );
    assert_eq!(store.digest(), before);
}

#[test]
fn a_candidate_that_is_inside_equal_to_or_above_a_protected_member_is_refused() {
    let store = Store::new("plan-refuse");
    let retention = store.retention();
    for (candidate, code, closure) in [
        (".build-native", "candidate_contains_member", "native-build"),
        (
            ".build-native/release",
            "candidate_inside_member",
            "native-build",
        ),
        (
            ".build-native/release/libx.a",
            "candidate_inside_member",
            "native-build",
        ),
        (
            "npm-cache/_cacache",
            "candidate_inside_member",
            "npm-offline",
        ),
        ("models", "candidate_contains_member", "model-qwen"),
        (
            "models/qwen/model.bin",
            "candidate_contains_member",
            "model-qwen",
        ),
        ("releases", "candidate_contains_member", "active-release"),
        (
            "receipts/prior/v0.1.4.json",
            "candidate_inside_member",
            "prior-receipts",
        ),
        (
            "rollback/v0.1.4",
            "candidate_contains_member",
            "rollback-v014",
        ),
        ("rollback", "candidate_contains_member", "rollback-v014"),
    ] {
        let planned = retention::plan(&retention, &store.root, &[candidate.to_string()]);
        assert!(planned.deletable.is_empty(), "{candidate}");
        assert_eq!(codes(&planned.findings), vec![code], "{candidate}");
        assert!(
            planned.findings[0].detail.contains(closure),
            "{candidate}: {:?}",
            planned.findings
        );
    }
    // A sibling of a member is not the member.
    let planned = retention::plan(&retention, &store.root, &["models/other".to_string()]);
    assert_eq!(codes(&planned.findings), vec!["candidate_unreadable"]);
    let planned = retention::plan(&retention, &store.root, &["tmp".to_string()]);
    assert_eq!(codes(&planned.findings), Vec::<&str>::new());
    // Path-prefix is not path-ancestry: `.build-native-old` does not contain `.build-native`.
    fs::create_dir_all(store.root.join(".build-native-old")).unwrap();
    let planned = retention::plan(&retention, &store.root, &[".build-native-old".to_string()]);
    assert_eq!(codes(&planned.findings), Vec::<&str>::new());
}

#[test]
fn malformed_duplicate_missing_and_symlinked_candidates_are_findings() {
    let store = Store::new("plan-bad");
    let retention = store.retention();
    for (candidate, code) in [
        ("", "candidate_path_invalid"),
        (".", "candidate_path_invalid"),
        ("../outside", "candidate_path_invalid"),
        ("/etc", "candidate_path_invalid"),
        ("old-shards/*", "candidate_path_invalid"),
        ("not-there", "candidate_unreadable"),
    ] {
        let planned = retention::plan(&retention, &store.root, &[candidate.to_string()]);
        assert_eq!(codes(&planned.findings), vec![code], "{candidate:?}");
        assert!(planned.deletable.is_empty());
    }
    let planned = retention::plan(
        &retention,
        &store.root,
        &["tmp".to_string(), "tmp".to_string()],
    );
    assert_eq!(codes(&planned.findings), vec!["candidate_duplicate"]);
    assert_eq!(planned.deletable.len(), 1);
    std::os::unix::fs::symlink(store.root.join("models"), store.root.join("tmp/link")).unwrap();
    let planned = retention::plan(&retention, &store.root, &["tmp".to_string()]);
    assert_eq!(
        codes(&planned.findings),
        vec!["candidate_unreadable"],
        "a tree holding a symlink"
    );
}

// ------------------------------------------------------------------------------ verify

#[test]
fn a_complete_store_verifies_and_each_defect_is_named() {
    let store = Store::new("verify");
    assert_eq!(
        codes(&retention::verify(&store.retention(), &store.root)),
        Vec::<&str>::new()
    );

    fs::remove_file(store.root.join("models/qwen/model.bin")).unwrap();
    let found = retention::verify(&store.retention(), &store.root);
    assert_eq!(codes(&found), vec!["closure_member_missing"]);
    assert_eq!(found[0].subject, "model-qwen:models/qwen/model.bin");

    let store = Store::new("verify-changed");
    fs::write(
        store.root.join("releases/active/Mycelium.app/exe"),
        b"tampered",
    )
    .unwrap();
    assert_eq!(
        codes(&retention::verify(&store.retention(), &store.root)),
        vec!["closure_member_changed"]
    );

    // A directory member that lost its tree.
    let store = Store::new("verify-dir");
    fs::remove_dir_all(store.root.join("npm-cache")).unwrap();
    assert_eq!(
        codes(&retention::verify(&store.retention(), &store.root)),
        vec!["closure_member_missing"]
    );

    // A member replaced by a symlink is not the member.
    let store = Store::new("verify-link");
    fs::remove_file(store.root.join("models/qwen/model.bin")).unwrap();
    std::os::unix::fs::symlink(
        store.root.join("tmp/scratch.txt"),
        store.root.join("models/qwen/model.bin"),
    )
    .unwrap();
    assert_eq!(
        codes(&retention::verify(&store.retention(), &store.root)),
        vec!["closure_member_missing"]
    );

    // A sha256 pin on a directory is a manifest error found at verify time.
    let text = edit_manifest(|m| m["closures"][4]["members"][0]["sha256"] = sha(b"x").into());
    let store = Store::with_manifest("verify-pin-dir", &text);
    assert_eq!(
        codes(&retention::verify(&store.retention(), &store.root)),
        vec!["closure_member_not_a_file"]
    );
}

// ------------------------------------------------------------------------------ cleanup

#[test]
fn a_fixture_cleanup_removes_junk_keeps_every_closure_and_the_preflight_still_passes() {
    let store = Store::new("cleanup-ok");
    let before = store.digest();
    let report = retention::cleanup(
        &store.retention(),
        &store.root,
        &["old-shards".to_string(), "tmp".to_string()],
        true,
    )
    .unwrap();
    assert_eq!(codes(&report.findings), Vec::<&str>::new());
    assert_eq!(
        report.removed,
        vec![("old-shards".to_string(), 3000), ("tmp".to_string(), 30)]
    );
    assert_eq!(report.preflight_before, vec!["unit: RUN".to_string()]);
    assert_eq!(report.preflight_after, report.preflight_before);
    assert!(!store.exists("old-shards") && !store.exists("tmp"));
    // Every other file is byte-identical to before (independent digest, not the tool's verify).
    let after = store.digest();
    let kept: BTreeMap<_, _> = before
        .iter()
        .filter(|(p, _)| !p.starts_with("old-shards/") && !p.starts_with("tmp/"))
        .map(|(p, h)| (p.clone(), h.clone()))
        .collect();
    assert_eq!(after, kept);
    assert_eq!(
        codes(&retention::verify(&store.retention(), &store.root)),
        Vec::<&str>::new()
    );
}

#[test]
fn a_refused_plan_removes_nothing() {
    let store = Store::new("cleanup-refused");
    let before = store.digest();
    let report = retention::cleanup(
        &store.retention(),
        &store.root,
        &["old-shards".to_string(), ".build-native".to_string()],
        true,
    )
    .unwrap();
    assert_eq!(codes(&report.findings), vec!["candidate_contains_member"]);
    assert!(report.removed.is_empty());
    assert_eq!(
        store.digest(),
        before,
        "not even the harmless candidate went"
    );
}

#[test]
fn rollback_cleanup_refuses_before_deleting_even_unprotected_junk() {
    let store = Store::new("rollback-refusal");
    let before = store.digest();
    let report = retention::cleanup(
        &store.retention(),
        &store.root,
        &["old-shards".into(), "rollback/v0.1.4".into()],
        true,
    )
    .unwrap();
    assert!(
        report.removed.is_empty(),
        "rollback/junk were actually deleted: {:?}",
        report.removed
    );
    assert_eq!(codes(&report.findings), vec!["candidate_contains_member"]);
    assert_eq!(store.digest(), before);
}

#[test]
fn a_store_already_missing_a_member_is_not_cleaned() {
    let store = Store::new("cleanup-incomplete");
    fs::remove_file(store.root.join("rollback/v0.1.4/payload.zip")).unwrap();
    let report =
        retention::cleanup(&store.retention(), &store.root, &["tmp".to_string()], false).unwrap();
    assert_eq!(codes(&report.findings), vec!["closure_member_missing"]);
    assert!(store.exists("tmp"), "nothing was removed");
}

#[test]
fn only_a_marked_fixture_root_is_ever_cleaned() {
    let store = Store::new("cleanup-marker");
    fs::remove_file(store.root.join(retention::FIXTURE_MARKER)).unwrap();
    let error = retention::cleanup(&store.retention(), &store.root, &["tmp".to_string()], false)
        .err()
        .unwrap_or_default();
    assert!(error.contains("marker"), "{error}");
    assert!(store.exists("tmp"));
    // A marker for another manifest is not this manifest's fixture.
    fs::write(
        store.root.join(retention::FIXTURE_MARKER),
        sha(b"another manifest"),
    )
    .unwrap();
    let error = retention::cleanup(&store.retention(), &store.root, &["tmp".to_string()], false)
        .err()
        .unwrap_or_default();
    assert!(error.contains("sha256"), "{error}");
    assert!(store.exists("tmp"));
}

#[test]
fn the_after_check_catches_a_cleanup_that_broke_the_preflight_the_manifest_did_not_know_about() {
    // The v0.1.1 replay: the native closure names only `.build-native/keep`, so the plan lets
    // `.build-native/release` go although the preflight needs `.build-native/release/libx.a`.
    let text =
        edit_manifest(|m| m["closures"][1]["members"][0]["path"] = ".build-native/keep".into());
    let make = |tag: &str| {
        let store = Store::with_manifest(tag, &text);
        fs::write(store.root.join(".build-native/keep"), b"keep").unwrap();
        store
    };
    let store = make("cleanup-replay");
    let report = retention::cleanup(
        &store.retention(),
        &store.root,
        &[".build-native/release".to_string()],
        true,
    )
    .unwrap();
    assert_eq!(codes(&report.findings), vec!["preflight_changed"]);
    assert_eq!(report.preflight_before, vec!["unit: RUN".to_string()]);
    assert!(
        report.preflight_after[0].starts_with("unit: NOT_RUN(BLOCKED:"),
        "{:?}",
        report.preflight_after
    );
    // Without the preflight comparison the same cleanup looks clean: the comparison is what saw it.
    let store = make("cleanup-replay-nopf");
    let report = retention::cleanup(
        &store.retention(),
        &store.root,
        &[".build-native/release".to_string()],
        false,
    )
    .unwrap();
    assert_eq!(codes(&report.findings), Vec::<&str>::new());
}

#[test]
fn overlapping_candidates_and_control_files_refuse_before_removing_anything() {
    let store = Store::new("cleanup-overlap");
    let before = store.digest();
    for candidates in [
        vec!["old-shards", "old-shards/shard-1.bin"],
        vec!["old-shards/shard-1.bin", "old-shards"],
        vec!["tmp", retention::FIXTURE_MARKER],
        vec!["tmp", "xtask"],
    ] {
        let report = retention::cleanup(
            &store.retention(),
            &store.root,
            &candidates.iter().map(|p| p.to_string()).collect::<Vec<_>>(),
            true,
        )
        .unwrap();
        assert!(!report.findings.is_empty());
        assert!(report.removed.is_empty());
        assert_eq!(store.digest(), before);
    }
}

#[test]
fn an_already_blocked_preflight_is_not_a_successful_before_after_comparison() {
    let store = Store::new("cleanup-blocked-before");
    fs::write(
        store.root.join("xtask/prerequisites.json"),
        PREREQUISITES.replace(".build-native/release/libx.a", "absent-prerequisite"),
    )
    .unwrap();
    let before = store.digest();
    let report =
        retention::cleanup(&store.retention(), &store.root, &["tmp".into()], true).unwrap();
    assert_eq!(codes(&report.findings), vec!["preflight_blocked"]);
    assert!(report.removed.is_empty());
    assert_eq!(store.digest(), before);
}

#[test]
fn symlinked_ancestors_cannot_make_members_or_candidates_escape_the_store() {
    let store = Store::new("ancestor-link");
    std::os::unix::fs::symlink(store.root.join("old-shards"), store.root.join("alias")).unwrap();
    let planned = retention::plan(
        &store.retention(),
        &store.root,
        &["alias/shard-1.bin".into()],
    );
    assert_eq!(codes(&planned.findings), vec!["candidate_unreadable"]);
    let alias = store.root.parent().unwrap().join("root-alias");
    std::os::unix::fs::symlink(&store.root, &alias).unwrap();
    assert!(retention::cleanup(&store.retention(), &alias, &["tmp".into()], false).is_err());
    assert!(store.exists("tmp"));
}

#[test]
fn requires_cycles_terminate_and_still_preserve_the_entire_reachable_graph() {
    let text =
        edit_manifest(|m| m["closures"][1]["requires"] = serde_json::json!(["active-release"]));
    let manifest = Retention::parse(&text).unwrap();
    assert_eq!(manifest.protected_ids().len(), 6);
}

// ------------------------------------------------------------------------------ the command

#[test]
fn the_plan_command_is_a_dry_run_with_exit_codes_0_1_2() {
    let store = Store::new("cli-plan");
    let before = store.digest();
    let output = store.cli("plan", &["old-shards", "# a comment", "", "tmp"], &[]);
    let text = out_text(&output);
    assert_eq!(output.status.code(), Some(0), "{text}");
    assert!(
        text.contains("DELETE-CANDIDATE old-shards (3000 B)"),
        "{text}"
    );
    assert!(text.contains("2 may go, 0 refused"), "{text}");
    assert!(text.contains("dry run, nothing touched"), "{text}");
    let output = store.cli("plan", &["old-shards", "models"], &[]);
    let text = out_text(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(
        text.contains("FINDING candidate_contains_member models"),
        "{text}"
    );
    assert_eq!(store.digest(), before);
    // Unreadable manifest, unknown mode and repeated options are input errors.
    let output = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args([
            "retention",
            "plan",
            "--manifest",
            "/nonexistent/m.json",
            "--root",
            "/tmp",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let output = store.cli("explode", &[], &[]);
    assert_eq!(output.status.code(), Some(2));
    let output = store.cli("plan", &["tmp"], &["--root", "/tmp"]);
    assert_eq!(output.status.code(), Some(2));
}

#[test]
fn the_verify_and_cleanup_commands_report_and_exit() {
    let store = Store::new("cli-verify");
    let output = store.cli("verify", &[], &[]);
    assert_eq!(output.status.code(), Some(0), "{}", out_text(&output));
    assert!(out_text(&output).contains("6 protected closures, 0 findings"));
    let output = store.cli("cleanup", &["old-shards", "tmp"], &["--preflight"]);
    let text = out_text(&output);
    assert_eq!(output.status.code(), Some(0), "{text}");
    assert!(text.contains("REMOVED old-shards (3000 B)"), "{text}");
    assert!(text.contains("preflight compared before/after"), "{text}");
    assert!(!store.exists("old-shards"));
    fs::remove_file(store.root.join("models/qwen/model.bin")).unwrap();
    let output = store.cli("verify", &[], &[]);
    assert_eq!(output.status.code(), Some(1));
    assert!(
        out_text(&output)
            .contains("FINDING closure_member_missing model-qwen:models/qwen/model.bin")
    );
    // A cleanup on an unmarked root is an input error, nothing removed.
    let store = Store::new("cli-unmarked");
    fs::remove_file(store.root.join(retention::FIXTURE_MARKER)).unwrap();
    let output = store.cli("cleanup", &["tmp"], &[]);
    assert_eq!(output.status.code(), Some(2));
    assert!(store.exists("tmp"));
}

#[test]
fn the_retention_code_has_no_network_or_process_launching() {
    let source =
        fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/retention.rs")).unwrap();
    let code: String = source
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    for forbidden in [
        "std::process",
        "Command::new",
        "TcpStream",
        "UdpSocket",
        "std::net",
        "reqwest",
        "ureq",
        "curl",
        "git ",
    ] {
        assert!(
            !code.contains(forbidden),
            "retention.rs mentions {forbidden}"
        );
    }
}
