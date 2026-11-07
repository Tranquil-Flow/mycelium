// SPDX-License-Identifier: AGPL-3.0-only
//! Every release cell is producible: `xtask release emit` writes the typed receipt records
//! (release_receipt, device, rehearsal, release_matrix) or an explicit NOT_YET record, and
//! `xtask release produce` runs a gate cell with the candidate binding (CL-L12-01, CL-L12-07).
//! Real child processes, a real git HEAD and the real `xtask gate`; fixture candidate bytes.
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};

static NEXT: AtomicUsize = AtomicUsize::new(0);

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures/release")
}
fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}
fn text(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}
/// The digest from the system tool, independent of the code under test.
fn shasum(path: &Path) -> String {
    let out = Command::new("/usr/bin/shasum")
        .args(["-a", "256"])
        .arg(path)
        .output()
        .unwrap();
    String::from_utf8(out.stdout)
        .unwrap()
        .split_whitespace()
        .next()
        .unwrap()
        .to_string()
}

struct Sandbox {
    root: PathBuf,
    head: String,
}
impl Sandbox {
    fn new() -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-release-emit-tests")
            .join(format!(
                "{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        let worktree = root.join("wt");
        fs::create_dir_all(&worktree).unwrap();
        fs::create_dir_all(root.join("registry")).unwrap();
        let git = |args: &[&str]| {
            let out = Command::new("git")
                .args(["-C", worktree.to_str().unwrap()])
                .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
                .args(args)
                .output()
                .unwrap();
            assert!(out.status.success(), "{out:?}");
            String::from_utf8(out.stdout).unwrap()
        };
        git(&["init", "-q"]);
        git(&["commit", "-q", "--allow-empty", "-m", "x"]);
        let head = git(&["rev-parse", "HEAD"]).trim().to_string();
        fs::write(
            root.join("registry/lanes.tsv"),
            format!(
                "lane\tbranch\tworktree\ttarget\treport\tstatus_json\nT1\tb\t{}\t{}\tr.md\tt.json\n",
                worktree.display(),
                root.join("target").display()
            ),
        )
        .unwrap();
        fs::write(root.join("guard-clear.sh"), "echo CLEAR\nexit 0\n").unwrap();
        copy_tree(&fixtures().join("candidate"), &root.join("candidate"));
        fs::copy(fixtures().join("profile.json"), root.join("profile.json")).unwrap();
        fs::copy(
            fixtures().join("claims.tsv"),
            root.join("registry/CLAIMS.tsv"),
        )
        .unwrap();
        fs::write(root.join("registry/evidence.jsonl"), "").unwrap();
        let mut manifest: Value =
            serde_json::from_str(&fs::read_to_string(fixtures().join("manifest.json")).unwrap())
                .unwrap();
        manifest["candidate"]["source_commit"] = json!(head);
        manifest["frozen_at"] = json!(1.0);
        fs::write(root.join("manifest.json"), manifest.to_string()).unwrap();
        Self { root, head }
    }
    fn registry(&self) -> PathBuf {
        self.root.join("registry")
    }
    fn records(&self) -> Vec<Value> {
        fs::read_to_string(self.registry().join("evidence.jsonl"))
            .unwrap_or_default()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }
    fn manifest(&self) -> Value {
        serde_json::from_str(&fs::read_to_string(self.root.join("manifest.json")).unwrap()).unwrap()
    }
    /// `xtask release emit <kind> <name> ... --registry R --profile P`
    fn emit(&self, kind: &str, name: &str, extra: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "emit", kind, name])
            .args(extra)
            .arg("--manifest")
            .arg(self.root.join("manifest.json"))
            .arg("--profile")
            .arg(self.root.join("profile.json"))
            .arg("--registry")
            .arg(self.registry())
            .output()
            .unwrap()
    }
    fn artifact(&self, name: &str, body: &str) -> PathBuf {
        let path = self.root.join("artifacts").join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, body).unwrap();
        path
    }
    fn status(&self) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "status", &self.head, "--manifest"])
            .arg(self.root.join("manifest.json"))
            .arg("--profile")
            .arg(self.root.join("profile.json"))
            .arg("--registry")
            .arg(self.registry())
            .output()
            .unwrap()
    }
    fn cell_line(&self, cell: &str) -> String {
        let out = text(&self.status());
        out.lines()
            .find(|l| l.split_whitespace().nth(2) == Some(cell))
            .unwrap_or_else(|| panic!("no line for {cell}: {out}"))
            .to_string()
    }
    fn pass(&self, name: &str, artifact: &Path) -> Output {
        self.emit(
            "device",
            name,
            &[
                "--pass",
                "--producer",
                "test-producer",
                "--artifact",
                artifact.to_str().unwrap(),
            ],
        )
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}

#[test]
fn a_not_yet_record_names_the_reason_and_the_blocker_and_the_cell_says_so() {
    let sandbox = Sandbox::new();
    let out = sandbox.emit(
        "device",
        "stranger-install",
        &[
            "--not-yet",
            "--reason",
            "needs the two-Mac window",
            "--blocker",
            "OQ-002",
            "--blocker",
            "MacBook",
        ],
    );
    assert_eq!(out.status.code(), Some(0), "{}", text(&out));
    let records = sandbox.records();
    assert_eq!(records.len(), 1);
    let record = &records[0];
    assert_eq!(record["kind"], "device");
    assert_eq!(record["name"], "stranger-install");
    assert_eq!(record["verdict"], "NOT_YET");
    assert_eq!(record["lane"], "RELEASE");
    assert_eq!(record["reason"], "needs the two-Mac window");
    assert_eq!(record["blockers"], json!(["OQ-002", "MacBook"]));
    assert!(record["id"].as_str().unwrap().starts_with("EV-"));
    assert!(
        record.get("candidate").is_none(),
        "a statement, not a measurement: {record}"
    );
    let line = sandbox.cell_line("stranger-install");
    assert!(line.starts_with("RED"), "{line}");
    assert!(line.contains("cell_not_yet"), "{line}");
    assert!(line.contains("needs the two-Mac window"), "{line}");
    assert!(line.contains("OQ-002"), "{line}");
    assert!(line.contains(record["id"].as_str().unwrap()), "{line}");
}

#[test]
fn a_not_yet_record_without_a_reason_is_refused_and_writes_nothing() {
    let sandbox = Sandbox::new();
    let out = sandbox.emit("device", "stranger-install", &["--not-yet"]);
    assert_eq!(out.status.code(), Some(2), "{}", text(&out));
    assert!(text(&out).contains("emit_reason_missing"), "{}", text(&out));
    assert!(sandbox.records().is_empty());
}

#[test]
fn a_cell_nobody_declared_and_a_gate_kind_are_refused() {
    let sandbox = Sandbox::new();
    let unknown = sandbox.emit("device", "no-such-cell", &["--not-yet", "--reason", "x"]);
    assert_eq!(unknown.status.code(), Some(2), "{}", text(&unknown));
    assert!(
        text(&unknown).contains("emit_unknown_cell"),
        "{}",
        text(&unknown)
    );
    let gate = sandbox.emit("gate", "fmt", &["--not-yet", "--reason", "x"]);
    assert_eq!(gate.status.code(), Some(2), "{}", text(&gate));
    assert!(
        text(&gate).contains("emit_kind_unsupported"),
        "{}",
        text(&gate)
    );
    let wrong_kind = sandbox.emit(
        "rehearsal",
        "stranger-install",
        &["--not-yet", "--reason", "x"],
    );
    assert!(
        text(&wrong_kind).contains("emit_unknown_cell"),
        "the cell is a device cell: {}",
        text(&wrong_kind)
    );
    assert!(sandbox.records().is_empty());
}

#[test]
fn a_pass_needs_an_artifact_that_exists_and_must_say_who_produced_it() {
    let sandbox = Sandbox::new();
    let none = sandbox.emit(
        "device",
        "stranger-install",
        &["--pass", "--producer", "device-harness"],
    );
    assert_eq!(none.status.code(), Some(2), "{}", text(&none));
    assert!(
        text(&none).contains("emit_artifact_missing"),
        "{}",
        text(&none)
    );
    let absent = sandbox.emit(
        "device",
        "stranger-install",
        &[
            "--pass",
            "--producer",
            "p",
            "--artifact",
            "/nonexistent/receipt.json",
        ],
    );
    assert_eq!(absent.status.code(), Some(2), "{}", text(&absent));
    assert!(
        text(&absent).contains("emit_artifact_invalid"),
        "{}",
        text(&absent)
    );
    let artifact = sandbox.artifact("r.json", "{}");
    let anonymous = sandbox.emit(
        "device",
        "stranger-install",
        &["--pass", "--artifact", artifact.to_str().unwrap()],
    );
    assert_eq!(anonymous.status.code(), Some(2), "{}", text(&anonymous));
    assert!(
        text(&anonymous).contains("emit_producer_missing"),
        "{}",
        text(&anonymous)
    );
    assert!(sandbox.records().is_empty());
}

#[test]
fn a_symlinked_or_empty_artifact_is_not_evidence() {
    let sandbox = Sandbox::new();
    let real = sandbox.artifact("real.json", "{\"ok\":true}");
    let link = sandbox.root.join("artifacts/link.json");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let empty = sandbox.artifact("empty.json", "");
    for bad in [&link, &empty] {
        let out = sandbox.pass("stranger-install", bad);
        assert_eq!(out.status.code(), Some(2), "{bad:?}: {}", text(&out));
        assert!(
            text(&out).contains("emit_artifact_invalid"),
            "{}",
            text(&out)
        );
    }
    assert!(sandbox.records().is_empty());
}

#[test]
fn a_pass_is_bound_to_the_candidate_bytes_and_the_artifact_digest_and_turns_the_cell_green() {
    let sandbox = Sandbox::new();
    let artifact = sandbox.artifact("stranger.json", "{\"attempt\":\"A1\"}");
    let out = sandbox.pass("stranger-install", &artifact);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out));
    let records = sandbox.records();
    assert_eq!(records.len(), 1);
    let record = &records[0];
    assert_eq!(record["verdict"], "PASS");
    assert_eq!(record["kind"], "device");
    assert_eq!(record["commit"], sandbox.head.as_str());
    assert_eq!(record["candidate"], sandbox.manifest()["candidate"]);
    assert_eq!(record["producer"], "test-producer");
    let listed = record["artifacts"].as_array().unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0]["sha256"], shasum(&artifact).as_str());
    assert_eq!(listed[0]["bytes"], 16);
    assert_eq!(
        Path::new(listed[0]["path"].as_str().unwrap()),
        fs::canonicalize(&artifact).unwrap()
    );
    let line = sandbox.cell_line("stranger-install");
    assert!(line.starts_with("green"), "{line}");
}

#[test]
fn emitting_a_pass_for_changed_candidate_bytes_is_refused() {
    let sandbox = Sandbox::new();
    let exe = sandbox
        .root
        .join("candidate/bundle/Contents/MacOS/mycelium");
    let mut bytes = fs::read(&exe).unwrap();
    bytes.push(0);
    fs::write(&exe, bytes).unwrap();
    let artifact = sandbox.artifact("r.json", "{\"a\":1}");
    let out = sandbox.pass("stranger-install", &artifact);
    assert_eq!(out.status.code(), Some(2), "{}", text(&out));
    assert!(
        text(&out).contains("package_exe_mismatch"),
        "{}",
        text(&out)
    );
    assert!(sandbox.records().is_empty());
}

#[test]
fn a_pass_goes_red_when_its_artifact_changes_or_disappears() {
    let sandbox = Sandbox::new();
    let artifact = sandbox.artifact("r.json", "{\"a\":1}");
    assert_eq!(
        sandbox.pass("stranger-install", &artifact).status.code(),
        Some(0)
    );
    assert!(sandbox.cell_line("stranger-install").starts_with("green"));
    fs::write(&artifact, "{\"a\":2}").unwrap();
    let changed = sandbox.cell_line("stranger-install");
    assert!(changed.contains("artifact_changed"), "{changed}");
    fs::remove_file(&artifact).unwrap();
    let gone = sandbox.cell_line("stranger-install");
    assert!(gone.contains("artifact_missing"), "{gone}");
}

#[test]
fn the_latest_statement_decides_between_not_yet_and_pass() {
    let sandbox = Sandbox::new();
    let artifact = sandbox.artifact("r.json", "{\"a\":1}");
    let nye = |reason: &str| {
        sandbox.emit(
            "device",
            "stranger-install",
            &["--not-yet", "--reason", reason],
        )
    };
    assert_eq!(nye("first").status.code(), Some(0));
    assert!(
        sandbox
            .cell_line("stranger-install")
            .contains("cell_not_yet")
    );
    assert_eq!(
        sandbox.pass("stranger-install", &artifact).status.code(),
        Some(0)
    );
    assert!(
        sandbox.cell_line("stranger-install").starts_with("green"),
        "a later PASS supersedes the NOT_YET"
    );
    assert_eq!(nye("regressed: new build").status.code(), Some(0));
    let line = sandbox.cell_line("stranger-install");
    assert!(
        line.contains("cell_not_yet") && line.contains("regressed: new build"),
        "a later NOT_YET supersedes the PASS: {line}"
    );
}

#[test]
fn a_receipt_record_with_neither_log_nor_artifacts_is_not_green() {
    let sandbox = Sandbox::new();
    let record = json!({"at": 1793300000.0, "candidate": sandbox.manifest()["candidate"],
        "exit": 0, "id": "EV-20261028-0001", "kind": "device", "lane": "T1",
        "name": "stranger-install", "verdict": "PASS"});
    fs::write(
        sandbox.registry().join("evidence.jsonl"),
        format!("{record}\n"),
    )
    .unwrap();
    let line = sandbox.cell_line("stranger-install");
    assert!(line.contains("receipt_no_artifacts"), "{line}");
}

#[test]
fn every_receipt_kind_has_an_emitter_and_a_cell_accepts_only_its_own_kind() {
    let sandbox = Sandbox::new();
    let mut profile: Value =
        serde_json::from_str(&fs::read_to_string(sandbox.root.join("profile.json")).unwrap())
            .unwrap();
    for (kind, name) in [
        ("release_receipt", "bundle-admission"),
        ("rehearsal", "rehearsal-1"),
        ("release_matrix", "f-incidents"),
    ] {
        profile["cells"].as_array_mut().unwrap().push(json!({
            "id": name, "state": "package", "claim": "CL-X-02", "bind": "bytes",
            "verify": {"runs": "receipt", "kind": kind, "name": name},
            "hint": ["xtask", "release", "emit", kind, name]}));
    }
    fs::write(sandbox.root.join("profile.json"), profile.to_string()).unwrap();
    for (kind, name) in [
        ("release_receipt", "bundle-admission"),
        ("rehearsal", "rehearsal-1"),
        ("release_matrix", "f-incidents"),
    ] {
        let artifact = sandbox.artifact(&format!("{name}.json"), "{\"a\":1}");
        let out = sandbox.emit(
            kind,
            name,
            &[
                "--pass",
                "--producer",
                "p",
                "--artifact",
                artifact.to_str().unwrap(),
            ],
        );
        assert_eq!(out.status.code(), Some(0), "{kind}: {}", text(&out));
        assert!(
            sandbox.cell_line(name).starts_with("green"),
            "{}",
            sandbox.cell_line(name)
        );
    }
}

#[test]
fn produce_runs_a_gate_cell_with_the_candidate_binding() {
    let sandbox = Sandbox::new();
    let log = fixtures().join("logs/inventory-ok.log");
    let out = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args(["release", "produce", "inventory", "--lane", "T1"])
        .arg("--manifest")
        .arg(sandbox.root.join("manifest.json"))
        .arg("--profile")
        .arg(sandbox.root.join("profile.json"))
        .arg("--registry")
        .arg(sandbox.registry())
        .arg("--guard")
        .arg(sandbox.root.join("guard-clear.sh"))
        .args(["--", "/bin/sh", "-c", "cat \"$0\""])
        .arg(&log)
        .args(["cargo", "test", "-p", "budget-model"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", text(&out));
    let records = sandbox.records();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["kind"], "gate");
    assert_eq!(records[0]["name"], "inventory");
    assert_eq!(records[0]["lane"], "T1");
    assert_eq!(
        records[0]["candidate"],
        sandbox.manifest()["candidate"],
        "the budget-model run is bound to the candidate bytes"
    );
    assert!(
        sandbox.cell_line("inventory").starts_with("green"),
        "{}",
        sandbox.cell_line("inventory")
    );
}

#[test]
fn produce_refuses_a_command_the_cell_would_not_select_and_a_non_gate_cell() {
    let sandbox = Sandbox::new();
    let produce = |cell: &str, tail: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "produce", cell, "--lane", "T1"])
            .arg("--manifest")
            .arg(sandbox.root.join("manifest.json"))
            .arg("--profile")
            .arg(sandbox.root.join("profile.json"))
            .arg("--registry")
            .arg(sandbox.registry())
            .arg("--guard")
            .arg(sandbox.root.join("guard-clear.sh"))
            .arg("--")
            .args(tail)
            .output()
            .unwrap()
    };
    let wrong = produce("inventory", &["/bin/sh", "-c", "exit 0", "cargo", "test"]);
    assert_eq!(wrong.status.code(), Some(2), "{}", text(&wrong));
    assert!(
        text(&wrong).contains("produce_argv_mismatch"),
        "{}",
        text(&wrong)
    );
    let device = produce("stranger-install", &["/bin/true"]);
    assert_eq!(device.status.code(), Some(2), "{}", text(&device));
    assert!(
        text(&device).contains("produce_not_a_gate_cell"),
        "{}",
        text(&device)
    );
    assert!(text(&device).contains("emit"), "{}", text(&device));
    let unknown = produce("nope", &["/bin/true"]);
    assert!(
        text(&unknown).contains("produce_unknown_cell"),
        "{}",
        text(&unknown)
    );
    assert!(sandbox.records().is_empty());
}

/// The release profile shipped with xtask: every cell has a producer the machine itself offers.
#[test]
fn the_shipped_profile_has_a_producer_for_every_cell() {
    let profile: Value = serde_json::from_str(
        &fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("release-profile.json"))
            .unwrap(),
    )
    .unwrap();
    let mut seen = std::collections::BTreeSet::new();
    for cell in profile["cells"].as_array().unwrap() {
        let id = cell["id"].as_str().unwrap();
        let verify = &cell["verify"];
        let kind = verify["kind"].as_str().unwrap_or("gate");
        let hint: Vec<&str> = cell["hint"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| h.as_str().unwrap())
            .collect();
        if kind == "gate" {
            for token in verify["argv_has"].as_array().unwrap() {
                assert!(
                    hint.contains(&token.as_str().unwrap()),
                    "{id}: the hint command does not select itself (lacks {token}): {hint:?}"
                );
            }
            for token in verify["argv_forbids"].as_array().into_iter().flatten() {
                assert!(!hint.contains(&token.as_str().unwrap()), "{id}: {hint:?}");
            }
            if hint.first() == Some(&"cargo")
                && ["test", "clippy", "build", "run"].contains(&hint[1])
            {
                assert!(
                    hint.contains(&"--offline") && hint.contains(&"--locked"),
                    "{id}: cargo hints are offline and locked: {hint:?}"
                );
            }
        } else {
            assert!(
                ["release_receipt", "device", "rehearsal", "release_matrix"].contains(&kind),
                "{id}: kind {kind} has no emitter"
            );
            let name = verify["name"].as_str().unwrap();
            assert!(
                seen.insert((kind.to_string(), name.to_string())),
                "{id}: two cells share {kind}/{name}"
            );
            assert_eq!(
                &hint[..4],
                ["xtask", "release", "emit", kind],
                "{id}: the hint names the emitter: {hint:?}"
            );
            assert_eq!(hint[4], name, "{id}: {hint:?}");
        }
    }
}

#[test]
fn an_empty_artifacts_list_is_not_evidence_even_though_the_key_exists() {
    let sandbox = Sandbox::new();
    let record = json!({"at": 1793300000.0, "candidate": sandbox.manifest()["candidate"],
        "exit": 0, "id": "EV-20261028-0001", "kind": "device", "lane": "RELEASE",
        "name": "stranger-install", "verdict": "PASS", "artifacts": []});
    fs::write(
        sandbox.registry().join("evidence.jsonl"),
        format!("{record}\n"),
    )
    .unwrap();
    let line = sandbox.cell_line("stranger-install");
    assert!(line.contains("receipt_no_artifacts"), "{line}");
}

#[test]
fn a_record_is_exactly_one_of_pass_and_not_yet_and_the_flags_do_not_mix() {
    let sandbox = Sandbox::new();
    let artifact = sandbox.artifact("r.json", "{\"a\":1}");
    let neither = sandbox.emit("device", "stranger-install", &[]);
    assert_eq!(neither.status.code(), Some(2), "{}", text(&neither));
    assert!(
        text(&neither).contains("exactly one of --pass and --not-yet"),
        "{}",
        text(&neither)
    );
    let both = sandbox.emit(
        "device",
        "stranger-install",
        &["--pass", "--not-yet", "--reason", "x"],
    );
    assert_eq!(both.status.code(), Some(2), "{}", text(&both));
    for mixed in [&["--reason", "late"][..], &["--blocker", "OQ-1"][..]] {
        let mut args = vec![
            "--pass",
            "--producer",
            "p",
            "--artifact",
            artifact.to_str().unwrap(),
        ];
        args.extend_from_slice(mixed);
        let out = sandbox.emit("device", "stranger-install", &args);
        assert_eq!(out.status.code(), Some(2), "{mixed:?}: {}", text(&out));
        assert!(text(&out).contains("belong to --not-yet"), "{}", text(&out));
    }
    let with_artifact = sandbox.emit(
        "device",
        "stranger-install",
        &[
            "--not-yet",
            "--reason",
            "x",
            "--artifact",
            artifact.to_str().unwrap(),
        ],
    );
    assert_eq!(with_artifact.status.code(), Some(2));
    assert!(sandbox.records().is_empty());
}

#[test]
fn a_blank_reason_is_no_reason() {
    let sandbox = Sandbox::new();
    let out = sandbox.emit(
        "device",
        "stranger-install",
        &["--not-yet", "--reason", "   "],
    );
    assert_eq!(out.status.code(), Some(2), "{}", text(&out));
    assert!(text(&out).contains("emit_reason_missing"), "{}", text(&out));
    assert!(sandbox.records().is_empty());
}

#[test]
fn produce_refuses_a_command_that_carries_a_token_the_cell_forbids() {
    let sandbox = Sandbox::new();
    let mut profile: Value =
        serde_json::from_str(&fs::read_to_string(sandbox.root.join("profile.json")).unwrap())
            .unwrap();
    for cell in profile["cells"].as_array_mut().unwrap() {
        if cell["id"] == "inventory" {
            cell["verify"]["argv_forbids"] = json!(["--structure-only"]);
        }
    }
    fs::write(sandbox.root.join("profile.json"), profile.to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args(["release", "produce", "inventory", "--lane", "T1"])
        .arg("--manifest")
        .arg(sandbox.root.join("manifest.json"))
        .arg("--profile")
        .arg(sandbox.root.join("profile.json"))
        .arg("--registry")
        .arg(sandbox.registry())
        .arg("--guard")
        .arg(sandbox.root.join("guard-clear.sh"))
        .args([
            "--",
            "/bin/true",
            "cargo",
            "test",
            "-p",
            "budget-model",
            "--structure-only",
        ])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2), "{}", text(&out));
    assert!(
        text(&out).contains("produce_argv_mismatch") && text(&out).contains("--structure-only"),
        "{}",
        text(&out)
    );
    assert!(sandbox.records().is_empty());
}
