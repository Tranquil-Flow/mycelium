// SPDX-License-Identifier: AGPL-3.0-only
//! Lane RELF item 4 (review F2): the release cells a tool can check are produced BY the tool
//! against the frozen candidate, not by a self-asserted receipt. `xtask release check-bundle
//! <check> --manifest M --manifest-sha256 S` re-hashes the frozen exe and bundle, then checks the
//! bundle at the manifest's own `bundle_dir`: `adhoc-signature` (codesign), `bundle-admission`
//! (the source tree's pins + closure), `relocation` (a copy elsewhere still verifies and admits),
//! `release-build` (the recipe's outputs beside the bundle describe exactly these bytes). Such a
//! cell (`argv_is_hint`) is green only on a gate record whose argv IS the cell's command for this
//! manifest and whose log carries the tool's PASS line for this manifest's sha256; `produce` runs
//! only that command. The launch smoke stays an operator receipt, labelled so, and its receipt
//! file must name this candidate (manifest and exe sha256), so a receipt for another candidate
//! never turns it green. Real child processes, real codesign on a scratch bundle.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};

static NEXT: AtomicUsize = AtomicUsize::new(0);
const TOOLS: [&str; 4] = [
    "release-build",
    "adhoc-signature",
    "bundle-admission",
    "relocation",
];

fn text(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}
fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn run(program: &str, args: &[&str]) -> Output {
    let out = Command::new(program).args(args).output().unwrap();
    assert!(out.status.success(), "{program} {args:?}: {}", text(&out));
    out
}

/// A scratch git repo (the candidate source with its two pin files), a signed `Mycelium.app` laid
/// out like the packaged one (main exe, one pinned helper, Info.plist, a resource), the recipe's
/// outputs beside it, a registry and a frozen manifest.
struct Sandbox {
    root: PathBuf,
    head: String,
}
impl Sandbox {
    fn new() -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-release-tools-tests")
            .join(format!(
                "{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        let repo = root.join("repo");
        fs::create_dir_all(repo.join("crates/desktop")).unwrap();
        fs::create_dir_all(root.join("registry")).unwrap();
        let s = Self {
            root,
            head: String::new(),
        };
        s.build_bundle();
        let helper = fs::read(s.app().join("Contents/MacOS/native/tool")).unwrap();
        fs::write(repo.join("crates/desktop/package-pins.json"), "[]\n").unwrap();
        fs::write(
            repo.join("crates/desktop/helper-pins.json"),
            serde_json::to_string_pretty(&json!([{
                "path": "Contents/MacOS/native/tool", "kind": "Helper",
                "bytes": helper.len(), "sha256": sha(&helper)}]))
            .unwrap(),
        )
        .unwrap();
        let git = |args: &[&str]| {
            let out = Command::new("git")
                .args(["-C", repo.to_str().unwrap()])
                .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
                .args(args)
                .output()
                .unwrap();
            assert!(out.status.success(), "{out:?}");
            String::from_utf8(out.stdout).unwrap()
        };
        git(&["init", "-q"]);
        git(&["add", "-A"]);
        git(&["commit", "-q", "-m", "x"]);
        let head = git(&["rev-parse", "HEAD"]).trim().to_string();
        fs::write(
            s.root.join("registry/lanes.tsv"),
            format!(
                "lane\tbranch\tworktree\ttarget\treport\tstatus_json\nT1\tb\t{}\t{}\tr.md\tt.json\n",
                repo.display(),
                s.root.join("target").display()
            ),
        )
        .unwrap();
        fs::write(
            s.root.join("registry/CLAIMS.tsv"),
            "id\tlane\tclause\tverifier\tstatus\tevidence\tblocked_by\tcritical\tweight\n\
             CL-T-01\tT\tthe bundle is signed\ttool\topen\t-\t-\tyes\t1\n\
             CL-T-02\tT\tthe launch smoke ran\toperator\topen\t-\t-\tyes\t1\n",
        )
        .unwrap();
        fs::write(s.root.join("registry/evidence.jsonl"), "").unwrap();
        fs::write(s.root.join("guard-clear.sh"), "echo CLEAR\nexit 0\n").unwrap();
        let mut s = s;
        s.head = head;
        s.write_profile();
        s.freeze();
        s
    }
    fn app(&self) -> PathBuf {
        self.root.join("out/Mycelium.app")
    }
    fn manifest_path(&self) -> PathBuf {
        self.root.join("manifest.json")
    }
    fn manifest_sha256(&self) -> String {
        sha(&fs::read(self.manifest_path()).unwrap())
    }
    fn build_bundle(&self) {
        let app = self.app();
        fs::create_dir_all(app.join("Contents/MacOS/native")).unwrap();
        fs::create_dir_all(app.join("Contents/Resources")).unwrap();
        fs::copy("/usr/bin/true", app.join("Contents/MacOS/mycelium")).unwrap();
        fs::copy("/usr/bin/true", app.join("Contents/MacOS/native/tool")).unwrap();
        fs::write(
            app.join("Contents/Info.plist"),
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<plist version=\"1.0\"><dict>\
             <key>CFBundleExecutable</key><string>mycelium</string>\
             <key>CFBundleIdentifier</key><string>org.mycelium.app</string>\
             <key>CFBundlePackageType</key><string>APPL</string></dict></plist>\n",
        )
        .unwrap();
        fs::write(app.join("Contents/Resources/notice.txt"), "notice\n").unwrap();
        self.sign();
    }
    fn sign(&self) {
        let app = self.app();
        let exe = app.join("Contents/MacOS/mycelium");
        for target in [exe.as_path(), app.as_path()] {
            run(
                "/usr/bin/codesign",
                &[
                    "--force",
                    "--sign",
                    "-",
                    "--timestamp=none",
                    target.to_str().unwrap(),
                ],
            );
        }
        self.write_outputs();
    }
    /// The recipe's outputs beside the app: `APP-FILES.sha256` (every regular file) and
    /// `SIGN-RECEIPT.json` (the signed executable's sha256).
    fn write_outputs(&self) {
        let app = self.app();
        let mut lines = Vec::new();
        fn walk(root: &Path, dir: &Path, out: &mut Vec<String>) {
            let mut children: Vec<_> = fs::read_dir(dir).unwrap().flatten().collect();
            children.sort_by_key(fs::DirEntry::file_name);
            for child in children {
                let path = child.path();
                let meta = fs::symlink_metadata(&path).unwrap();
                if meta.is_dir() {
                    walk(root, &path, out);
                } else if meta.is_file() {
                    out.push(format!(
                        "{}  {}",
                        sha(&fs::read(&path).unwrap()),
                        path.strip_prefix(root).unwrap().display()
                    ));
                }
            }
        }
        walk(&app, &app, &mut lines);
        lines.sort_by(|a, b| a[66..].cmp(&b[66..]));
        fs::write(
            self.root.join("out/APP-FILES.sha256"),
            lines.join("\n") + "\n",
        )
        .unwrap();
        let exe = fs::read(app.join("Contents/MacOS/mycelium")).unwrap();
        let tool = fs::read(app.join("Contents/MacOS/native/tool")).unwrap();
        fs::write(
            self.root.join("out/CONTENT-MANIFEST.json"),
            json!({"identity": {"bundle_identifier": "org.mycelium.app", "version": "0.2.0"},
            "entries": [
                {"path": "Contents/MacOS/mycelium", "sha256": sha(&exe), "bytes": exe.len()},
                {"path": "Contents/MacOS/native/tool", "sha256": sha(&tool), "bytes": tool.len()},
            ]})
            .to_string(),
        )
        .unwrap();
        fs::write(
            self.root.join("out/SIGN-RECEIPT.json"),
            json!({"executable": {"path": "Contents/MacOS/mycelium",
                "sha256_after": sha(&exe), "bytes_after": exe.len()}})
            .to_string(),
        )
        .unwrap();
    }
    /// (Re)freeze: a fresh manifest of the bundle as it is now.
    fn freeze(&self) {
        if self.manifest_path().exists() {
            fs::remove_file(self.manifest_path()).unwrap();
        }
        let out = Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "freeze", &self.head, "--exe"])
            .arg(self.app().join("Contents/MacOS/mycelium"))
            .arg("--bundle")
            .arg(self.app())
            .arg("--out")
            .arg(self.manifest_path())
            .arg("--repo")
            .arg(self.root.join("repo"))
            .output()
            .unwrap();
        assert!(out.status.success(), "freeze: {}", text(&out));
        // Freeze stamps the current second; records written in this test are later.
        let mut manifest: Value =
            serde_json::from_str(&fs::read_to_string(self.manifest_path()).unwrap()).unwrap();
        manifest["frozen_at"] = json!(1.0);
        fs::write(self.manifest_path(), manifest.to_string()).unwrap();
    }
    fn check(&self, tool: &str) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "check-bundle", tool, "--manifest"])
            .arg(self.manifest_path())
            .args(["--manifest-sha256", &self.manifest_sha256()])
            .arg("--repo")
            .arg(self.root.join("repo"))
            .output()
            .unwrap()
    }
    /// A profile with the four tool cells and the operator launch receipt; the tool hint runs this
    /// test's own xtask binary (the shipped profile runs `cargo run -p xtask`).
    fn write_profile(&self) {
        let repo = self.root.join("repo");
        let mut cells: Vec<Value> = TOOLS
            .iter()
            .map(|tool| {
                json!({"id": tool, "state": "package", "claim": "CL-T-01", "bind": "bytes",
                    "verify": {"runs": "check", "argv_has": ["release", "check-bundle", tool],
                               "argv_is_hint": true},
                    "hint": [env!("CARGO_BIN_EXE_xtask"), "release", "check-bundle", tool,
                             "--manifest", "<manifest>", "--manifest-sha256", "<manifest_sha256>",
                             "--repo", repo.to_str().unwrap()]})
            })
            .collect();
        cells.push(
            json!({"id": "model-free-launch", "state": "cert", "claim": "CL-T-02",
            "bind": "bytes", "verify": {"runs": "receipt", "kind": "release_receipt",
            "name": "model-free-launch-smoke", "operator_receipt": true},
            "hint": ["xtask", "release", "emit", "release_receipt", "model-free-launch-smoke"]}),
        );
        fs::write(
            self.root.join("profile.json"),
            serde_json::to_string_pretty(&json!({"schema": 1, "name": "relf-tools",
                "require_critical_lanes": ["T"], "mandatory_claims": ["CL-T-01", "CL-T-02"],
                "cells": cells}))
            .unwrap(),
        )
        .unwrap();
    }
    fn release(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .arg("release")
            .args(args)
            .arg("--manifest")
            .arg(self.manifest_path())
            .arg("--profile")
            .arg(self.root.join("profile.json"))
            .arg("--registry")
            .arg(self.root.join("registry"))
            .output()
            .unwrap()
    }
    fn produce(&self, cell: &str, tail: &[&str]) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_xtask"));
        command
            .args(["release", "produce", cell, "--lane", "T1", "--guard"])
            .arg(self.root.join("guard-clear.sh"))
            .arg("--manifest")
            .arg(self.manifest_path())
            .arg("--profile")
            .arg(self.root.join("profile.json"))
            .arg("--registry")
            .arg(self.root.join("registry"));
        if !tail.is_empty() {
            command.arg("--").args(tail);
        }
        command.output().unwrap()
    }
    fn cell_line(&self, cell: &str) -> String {
        let out = text(&self.release(&["status", &self.head]));
        out.lines()
            .find(|l| l.split_whitespace().nth(2) == Some(cell))
            .unwrap_or_else(|| panic!("no line for {cell}: {out}"))
            .to_string()
    }
    fn records(&self) -> Vec<Value> {
        fs::read_to_string(self.root.join("registry/evidence.jsonl"))
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }
    fn append(&self, record: &Value) {
        let path = self.root.join("registry/evidence.jsonl");
        let mut text = fs::read_to_string(&path).unwrap();
        text.push_str(&format!("{record}\n"));
        fs::write(path, text).unwrap();
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}
fn pass_line(tool: &str, manifest_sha256: &str) -> String {
    format!("release check-bundle {tool}: PASS manifest_sha256 {manifest_sha256}")
}

#[test]
fn the_shipped_profile_has_the_tool_produce_build_signature_admission_and_relocation() {
    let profile: Value = serde_json::from_str(
        &fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("release-profile.json"))
            .unwrap(),
    )
    .unwrap();
    let cell = |id: &str| {
        profile["cells"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["id"] == id)
            .cloned()
            .unwrap_or_else(|| panic!("no cell {id}"))
    };
    for tool in TOOLS {
        let c = cell(tool);
        assert_eq!(c["verify"]["runs"], "check", "{tool}: {c}");
        assert_eq!(c["verify"]["argv_is_hint"], true, "{tool}: {c}");
        let hint: Vec<&str> = c["hint"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| h.as_str().unwrap())
            .collect();
        assert!(
            hint.windows(3)
                .any(|w| w == ["release", "check-bundle", tool]),
            "{tool}: {hint:?}"
        );
        assert!(
            hint.contains(&"<manifest>") && hint.contains(&"<manifest_sha256>"),
            "{tool}: {hint:?}"
        );
    }
    let launch = cell("model-free-launch");
    assert_eq!(launch["verify"]["runs"], "receipt");
    assert_eq!(launch["verify"]["operator_receipt"], true, "{launch}");
}

#[test]
fn every_tool_passes_the_frozen_signed_admitted_bundle_and_names_this_manifest() {
    let sandbox = Sandbox::new();
    for tool in TOOLS {
        let out = sandbox.check(tool);
        assert_eq!(out.status.code(), Some(0), "{tool}: {}", text(&out));
        assert!(
            text(&out).contains(&pass_line(tool, &sandbox.manifest_sha256())),
            "{tool}: {}",
            text(&out)
        );
    }
}

#[test]
fn a_tool_refuses_bytes_other_than_the_frozen_ones_and_another_manifest() {
    let sandbox = Sandbox::new();
    fs::write(
        sandbox.app().join("Contents/Resources/notice.txt"),
        "edit\n",
    )
    .unwrap();
    for tool in TOOLS {
        let out = sandbox.check(tool);
        assert_eq!(out.status.code(), Some(1), "{tool}: {}", text(&out));
        assert!(
            text(&out).contains("package_bundle_mismatch"),
            "{tool}: {}",
            text(&out)
        );
    }
    let other = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args(["release", "check-bundle", "adhoc-signature", "--manifest"])
        .arg(sandbox.manifest_path())
        .args(["--manifest-sha256", &"0".repeat(64)])
        .output()
        .unwrap();
    assert_eq!(other.status.code(), Some(2), "{}", text(&other));
    assert!(
        text(&other).contains("check_manifest_mismatch"),
        "{}",
        text(&other)
    );
}

#[test]
fn the_tools_find_a_broken_seal_an_unpinned_helper_a_swapped_pin_and_stale_build_outputs() {
    // A resource changed after signing, frozen as it is: the seal no longer verifies.
    let sandbox = Sandbox::new();
    fs::write(
        sandbox.app().join("Contents/Resources/notice.txt"),
        "edit\n",
    )
    .unwrap();
    sandbox.write_outputs();
    sandbox.freeze();
    let out = sandbox.check("adhoc-signature");
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
    let out = sandbox.check("relocation");
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));

    // An unpinned helper beside the executable (signed into the bundle, frozen).
    let sandbox = Sandbox::new();
    fs::copy(
        "/usr/bin/true",
        sandbox.app().join("Contents/MacOS/native/cloudflared"),
    )
    .unwrap();
    sandbox.sign();
    sandbox.freeze();
    assert_eq!(sandbox.check("adhoc-signature").status.code(), Some(0));
    let out = sandbox.check("bundle-admission");
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
    assert!(
        text(&out).contains("unpinned") && text(&out).contains("native/cloudflared"),
        "{}",
        text(&out)
    );

    // The pinned helper with other bytes.
    let sandbox = Sandbox::new();
    fs::copy(
        "/usr/bin/false",
        sandbox.app().join("Contents/MacOS/native/tool"),
    )
    .unwrap();
    sandbox.sign();
    sandbox.freeze();
    let out = sandbox.check("bundle-admission");
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
    assert!(text(&out).contains("native/tool"), "{}", text(&out));

    // The recipe's outputs describe other bytes than the frozen bundle.
    let sandbox = Sandbox::new();
    let listing = sandbox.root.join("out/APP-FILES.sha256");
    let edited = fs::read_to_string(&listing)
        .unwrap()
        .lines()
        .filter(|l| !l.ends_with("notice.txt"))
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    fs::write(&listing, edited).unwrap();
    let out = sandbox.check("release-build");
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
    assert!(text(&out).contains("notice.txt"), "{}", text(&out));
}

#[test]
fn produce_runs_only_the_tool_and_the_cell_is_green_only_on_this_manifests_tool_run() {
    let sandbox = Sandbox::new();
    // A hand-made command that prints the PASS line is not the tool: refused, nothing written.
    let fake = format!(
        "echo '{}'",
        pass_line("adhoc-signature", &sandbox.manifest_sha256())
    );
    let refused = sandbox.produce(
        "adhoc-signature",
        &[
            "/bin/sh",
            "-c",
            &fake,
            "release",
            "check-bundle",
            "adhoc-signature",
        ],
    );
    assert_eq!(refused.status.code(), Some(2), "{}", text(&refused));
    assert!(sandbox.records().is_empty());

    let out = sandbox.produce("adhoc-signature", &[]);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out));
    let records = sandbox.records();
    assert_eq!(records.len(), 1, "{records:?}");
    let argv: Vec<String> = serde_json::from_value(records[0]["argv"].clone()).unwrap();
    assert!(
        argv.contains(&sandbox.manifest_sha256()),
        "the tool ran for this manifest: {argv:?}"
    );
    let line = sandbox.cell_line("adhoc-signature");
    assert!(line.starts_with("green"), "{line}");

    // The same tool record shape, hand-appended with a log of another manifest's run: RED.
    let mut forged = records[0].clone();
    forged["id"] = json!("EV-20990101-0001");
    forged["at"] = json!(records[0]["at"].as_f64().unwrap() + 1.0);
    let log = sandbox.root.join("registry/forged.log");
    fs::write(
        &log,
        format!("{}\n", pass_line("adhoc-signature", &"1".repeat(64))),
    )
    .unwrap();
    forged["log"] = json!(log.display().to_string());
    sandbox.append(&forged);
    let line = sandbox.cell_line("adhoc-signature");
    assert!(
        line.starts_with("RED") && line.contains("tool_log_unbound"),
        "{line}"
    );

    // A record whose argv is not the cell's command (a shell that echoes the line): RED.
    let mut other = records[0].clone();
    other["id"] = json!("EV-20990101-0002");
    other["at"] = json!(records[0]["at"].as_f64().unwrap() + 2.0);
    other["argv"] = json!([
        "/bin/sh",
        "-c",
        fake,
        "release",
        "check-bundle",
        "adhoc-signature"
    ]);
    sandbox.append(&other);
    let line = sandbox.cell_line("adhoc-signature");
    assert!(
        line.starts_with("RED") && line.contains("tool_argv_not_hint"),
        "{line}"
    );
}

#[test]
fn the_launch_smoke_is_an_operator_receipt_that_must_name_this_candidate() {
    let sandbox = Sandbox::new();
    let manifest: Value =
        serde_json::from_str(&fs::read_to_string(sandbox.manifest_path()).unwrap()).unwrap();
    let exe = manifest["candidate"]["exe_sha256"].as_str().unwrap();
    let receipt = |name: &str, body: String| {
        let path = sandbox.root.join(name);
        fs::write(&path, body).unwrap();
        path
    };
    let emit = |artifact: &Path| {
        sandbox.release(&[
            "emit",
            "release_receipt",
            "model-free-launch-smoke",
            "--pass",
            "--producer",
            "operator: smoke3.sh",
            "--artifact",
            artifact.to_str().unwrap(),
        ])
    };
    // A receipt that does not name this candidate is refused at emit.
    let other = receipt(
        "other.txt",
        format!("manifest_sha256 {}\nexe_sha256 {exe}\n", "2".repeat(64)),
    );
    let out = emit(&other);
    assert_eq!(out.status.code(), Some(2), "{}", text(&out));
    assert!(
        text(&out).contains("operator_receipt_unbound"),
        "{}",
        text(&out)
    );
    assert!(sandbox.records().is_empty());
    // Hand-appended for this candidate's bytes but resting on that receipt: RED.
    let artifact_entry = |path: &Path| {
        let bytes = fs::read(path).unwrap();
        json!({"path": path.display().to_string(), "sha256": sha(&bytes), "bytes": bytes.len()})
    };
    sandbox.append(
        &json!({"at": 2.0, "id": "EV-20990101-0003", "kind": "release_receipt",
        "lane": "RELEASE", "name": "model-free-launch-smoke", "producer": "hand",
        "artifacts": [artifact_entry(&other)], "candidate": manifest["candidate"],
        "commit": sandbox.head, "exit": 0, "verdict": "PASS"}),
    );
    let line = sandbox.cell_line("model-free-launch");
    assert!(
        line.starts_with("RED") && line.contains("operator_receipt_unbound"),
        "{line}"
    );
    // The operator's receipt for THIS candidate: green, and labelled an operator receipt.
    let mine = receipt(
        "mine.txt",
        format!(
            "operator receipt\nmanifest_sha256 {}\nexe_sha256 {exe}\n",
            sandbox.manifest_sha256()
        ),
    );
    let out = emit(&mine);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out));
    let line = sandbox.cell_line("model-free-launch");
    assert!(line.starts_with("green"), "{line}");
    assert!(line.contains("operator receipt"), "{line}");
}
