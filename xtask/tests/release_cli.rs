// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask release` and `xtask gate --candidate` end to end: real child processes, a real git HEAD,
//! evidence records written by the real `xtask gate`, and a dry candidate (fixture bytes) taken
//! from preflight to ACCEPTED, and from a blocked attempt to ACCEPTED by `--resume`.
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

struct Sandbox {
    root: PathBuf,
    head: String,
}
impl Sandbox {
    fn new() -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-release-cli-tests")
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
        let mut manifest: Value =
            serde_json::from_str(&fs::read_to_string(fixtures().join("manifest.json")).unwrap())
                .unwrap();
        manifest["candidate"]["source_commit"] = json!(head);
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
    /// `xtask gate T1 <name> --candidate manifest [--kind k] -- /bin/sh -c <script> <extra...>`
    fn gate(&self, name: &str, kind: &str, script: &str, extra: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["gate", "T1", name, "--registry"])
            .arg(self.registry())
            .arg("--guard")
            .arg(self.root.join("guard-clear.sh"))
            .arg("--candidate")
            .arg(self.root.join("manifest.json"))
            .args(["--kind", kind, "--", "/bin/sh", "-c", script])
            .args(extra)
            .output()
            .unwrap()
    }
    fn log(&self, name: &str) -> String {
        fixtures().join("logs").join(name).display().to_string()
    }
    fn gate_all(&self) {
        let ok = |o: Output, what: &str| assert_eq!(o.status.code(), Some(0), "{what}: {o:?}");
        ok(
            self.gate("fmt", "check", "exit 0", &["cargo", "fmt", "--", "--check"]),
            "fmt",
        );
        ok(
            self.gate(
                "lint",
                "check",
                "exit 0",
                &["cargo", "run", "-p", "xtask", "--", "lint"],
            ),
            "lint",
        );
        ok(
            self.gate("build", "check", "exit 0", &["cargo", "build", "--release"]),
            "build",
        );
        ok(
            self.gate(
                "tests",
                "test",
                "cat \"$0\"",
                &[&self.log("tests-ok.log"), "cargo", "test", "--workspace"],
            ),
            "tests",
        );
        ok(
            self.gate(
                "inventory",
                "test",
                "cat \"$0\"",
                &[
                    &self.log("inventory-ok.log"),
                    "cargo",
                    "test",
                    "-p",
                    "budget-model",
                ],
            ),
            "inventory",
        );
    }
    /// The device runner does not emit receipts yet; the test writes them with the real shape.
    fn receipts(&self, names: &[&str]) {
        let manifest: Value =
            serde_json::from_str(&fs::read_to_string(self.root.join("manifest.json")).unwrap())
                .unwrap();
        let date = String::from_utf8(Command::new("date").arg("+%Y%m%d").output().unwrap().stdout)
            .unwrap()
            .trim()
            .to_string();
        let path = self.registry().join("evidence.jsonl");
        let mut text = fs::read_to_string(&path).unwrap_or_default();
        for name in names {
            let id = format!("EV-{date}-{:04}", text.lines().count() + 1);
            let record = json!({"at": 1793300000.0, "candidate": manifest["candidate"], "exit": 0,
                "failed_tests": [], "id": id, "kind": "device", "lane": "T1", "name": name,
                "total": {"failed": 0, "ignored": 0, "passed": 0}, "verdict": "PASS",
                "log": "logs/check.log"});
            text.push_str(&format!("{record}\n"));
        }
        fs::write(path, text).unwrap();
        fs::create_dir_all(self.registry().join("logs")).unwrap();
        fs::write(self.registry().join("logs/check.log"), "ok\n").unwrap();
    }
    fn release(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "candidate", &self.head])
            .args(args)
            .args(["--manifest"])
            .arg(self.root.join("manifest.json"))
            .arg("--profile")
            .arg(self.root.join("profile.json"))
            .arg("--registry")
            .arg(self.registry())
            .arg("--attempts")
            .arg(self.root.join("attempts"))
            .output()
            .unwrap()
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}
fn text(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

#[test]
fn a_dry_candidate_goes_from_real_gate_records_to_accepted() {
    let sandbox = Sandbox::new();
    sandbox.gate_all();
    sandbox.receipts(&["stranger-install", "relocation-receipt"]);
    let output = sandbox.release(&["--attempt", "ACC1"]);
    assert_eq!(output.status.code(), Some(0), "{}", text(&output));
    let out = text(&output);
    assert!(out.contains("ACCEPTED"), "{out}");
    assert!(out.contains("promotion-receipt.json"), "{out}");
    let receipt: Value = serde_json::from_str(
        &fs::read_to_string(sandbox.root.join("attempts/ACC1/promotion-receipt.json")).unwrap(),
    )
    .unwrap();
    let records = sandbox.records();
    let tests_id = records.iter().find(|r| r["name"] == "tests").unwrap()["id"].clone();
    let cited: Vec<&Value> = receipt["claims"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|c| c["cells"].as_array().unwrap().iter())
        .map(|c| &c["ev"])
        .collect();
    assert!(cited.contains(&&tests_id), "{cited:?}");
    assert_eq!(cited.len(), 7);
    assert_eq!(receipt["candidate"]["source_commit"], sandbox.head.as_str());
}

#[test]
fn a_blocked_attempt_is_resumed_by_name_once_the_missing_receipts_exist() {
    let sandbox = Sandbox::new();
    sandbox.gate_all();
    let blocked = sandbox.release(&["--attempt", "ACC1"]);
    assert_eq!(blocked.status.code(), Some(1), "{}", text(&blocked));
    let out = text(&blocked);
    assert!(out.contains("BLOCKED"), "{out}");
    assert!(out.contains("cell_missing"), "{out}");
    assert!(out.contains("stranger-install"), "{out}");
    assert!(
        !sandbox
            .root
            .join("attempts/ACC1/promotion-receipt.json")
            .exists()
    );
    sandbox.receipts(&["stranger-install", "relocation-receipt"]);
    let resumed = sandbox.release(&["--resume", "ACC1"]);
    assert_eq!(resumed.status.code(), Some(0), "{}", text(&resumed));
    assert!(text(&resumed).contains("ACCEPTED"));
}

#[test]
fn stop_after_runs_through_the_named_state_only() {
    let sandbox = Sandbox::new();
    sandbox.gate_all();
    let output = sandbox.release(&["--attempt", "ACC1", "--stop-after", "package"]);
    assert_eq!(
        output.status.code(),
        Some(1),
        "in-progress is not accepted: {}",
        text(&output)
    );
    assert!(text(&output).contains("IN-PROGRESS"), "{}", text(&output));
    let state: Value = serde_json::from_str(
        &fs::read_to_string(sandbox.root.join("attempts/ACC1/state.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(state["states"]["package"]["status"], "done");
    assert_eq!(state["states"]["cert"]["status"], "pending");
}

#[test]
fn an_unknown_candidate_sha_and_bad_usage_exit_2_and_create_no_attempt() {
    let sandbox = Sandbox::new();
    let run = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release"])
            .args(args)
            .output()
            .unwrap()
    };
    let wrong = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args([
            "release",
            "candidate",
            &"0".repeat(40),
            "--attempt",
            "A",
            "--manifest",
        ])
        .arg(sandbox.root.join("manifest.json"))
        .arg("--profile")
        .arg(sandbox.root.join("profile.json"))
        .arg("--registry")
        .arg(sandbox.registry())
        .arg("--attempts")
        .arg(sandbox.root.join("attempts"))
        .output()
        .unwrap();
    assert_eq!(wrong.status.code(), Some(2), "{}", text(&wrong));
    assert!(
        text(&wrong).contains("candidate_identity_unknown"),
        "{}",
        text(&wrong)
    );
    assert!(!sandbox.root.join("attempts").exists());
    for args in [
        vec![],
        vec!["bogus"],
        vec!["candidate"],
        vec!["candidate", "abc"],
        vec!["candidate", &sandbox.head],
        vec![
            "candidate",
            &sandbox.head,
            "--attempt",
            "A",
            "--resume",
            "A",
        ],
    ] {
        let output = run(&args);
        assert_eq!(output.status.code(), Some(2), "{args:?}: {}", text(&output));
    }
    let missing = sandbox.release(&["--resume", "nope"]);
    assert_eq!(missing.status.code(), Some(2), "{}", text(&missing));
    assert!(text(&missing).contains("attempt_missing"));
}

#[test]
fn the_publish_command_never_publishes_and_exits_3() {
    let sandbox = Sandbox::new();
    sandbox.gate_all();
    sandbox.receipts(&["stranger-install", "relocation-receipt"]);
    assert_eq!(
        sandbox.release(&["--attempt", "ACC1"]).status.code(),
        Some(0)
    );
    let manifest: Value =
        serde_json::from_str(&fs::read_to_string(sandbox.root.join("manifest.json")).unwrap())
            .unwrap();
    let token = sandbox.root.join("token.txt");
    fs::write(
        &token,
        format!(
            "OWNER-PUBLISH-TOKEN v1\naction=publish\nexe_sha256={}\nbundle_sha256={}\n",
            manifest["candidate"]["exe_sha256"].as_str().unwrap(),
            manifest["candidate"]["bundle_sha256"].as_str().unwrap()
        ),
    )
    .unwrap();
    let publish = |with_token: bool| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_xtask"));
        command
            .args([
                "release",
                "publish",
                &sandbox.head,
                "--attempt",
                "ACC1",
                "--manifest",
            ])
            .arg(sandbox.root.join("manifest.json"))
            .arg("--profile")
            .arg(sandbox.root.join("profile.json"))
            .arg("--registry")
            .arg(sandbox.registry())
            .arg("--attempts")
            .arg(sandbox.root.join("attempts"));
        if with_token {
            command.arg("--owner-token").arg(&token);
        }
        command.output().unwrap()
    };
    let without = publish(false);
    assert_eq!(without.status.code(), Some(3), "{}", text(&without));
    assert!(
        text(&without).contains("owner_token_missing"),
        "{}",
        text(&without)
    );
    let with = publish(true);
    assert_eq!(with.status.code(), Some(3), "{}", text(&with));
    assert!(
        text(&with).contains("publication_not_implemented"),
        "{}",
        text(&with)
    );
}

// ------------------------------------------------------------ xtask gate --candidate

#[test]
fn a_candidate_gate_run_adds_exactly_one_extra_key_and_it_equals_the_manifest_candidate() {
    let sandbox = Sandbox::new();
    let output = sandbox.gate("fmt", "check", "exit 0", &["cargo", "fmt"]);
    assert_eq!(output.status.code(), Some(0), "{}", text(&output));
    let records = sandbox.records();
    assert_eq!(records.len(), 1);
    let mut keys: Vec<&str> = records[0]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(keys.len(), 16);
    assert!(keys.contains(&"candidate"));
    let manifest: Value =
        serde_json::from_str(&fs::read_to_string(sandbox.root.join("manifest.json")).unwrap())
            .unwrap();
    assert_eq!(records[0]["candidate"], manifest["candidate"]);
    assert_eq!(records[0]["commit"], sandbox.head.as_str());
}

#[test]
fn a_candidate_gate_run_in_another_tree_is_refused_before_the_command_runs() {
    let sandbox = Sandbox::new();
    let mut manifest: Value =
        serde_json::from_str(&fs::read_to_string(sandbox.root.join("manifest.json")).unwrap())
            .unwrap();
    manifest["candidate"]["source_commit"] = json!("0".repeat(40));
    fs::write(sandbox.root.join("manifest.json"), manifest.to_string()).unwrap();
    let marker = sandbox.root.join("ran");
    let output = sandbox.gate("fmt", "check", "touch \"$0\"", &[marker.to_str().unwrap()]);
    assert_eq!(output.status.code(), Some(2), "{}", text(&output));
    assert!(
        text(&output).contains("candidate_source_mismatch"),
        "{}",
        text(&output)
    );
    assert!(!marker.exists(), "the command must not run");
    assert!(sandbox.records().is_empty());
}

#[test]
fn a_candidate_gate_run_over_other_bytes_is_refused_before_the_command_runs() {
    let sandbox = Sandbox::new();
    let exe = sandbox
        .root
        .join("candidate/bundle/Contents/MacOS/mycelium");
    let mut bytes = fs::read(&exe).unwrap();
    bytes[0] ^= 1;
    fs::write(&exe, bytes).unwrap();
    let marker = sandbox.root.join("ran");
    let output = sandbox.gate("fmt", "check", "touch \"$0\"", &[marker.to_str().unwrap()]);
    assert_eq!(output.status.code(), Some(2), "{}", text(&output));
    assert!(
        text(&output).contains("package_exe_mismatch"),
        "{}",
        text(&output)
    );
    assert!(!marker.exists());
    assert!(sandbox.records().is_empty());
}

#[test]
fn bytes_that_change_while_the_gate_runs_are_never_bound_to_a_record() {
    let sandbox = Sandbox::new();
    let exe = sandbox
        .root
        .join("candidate/bundle/Contents/MacOS/mycelium");
    let output = sandbox.gate(
        "fmt",
        "check",
        "printf x >> \"$0\"",
        &[exe.to_str().unwrap()],
    );
    assert_eq!(output.status.code(), Some(2), "{}", text(&output));
    assert!(
        text(&output).contains("candidate_bytes_changed"),
        "{}",
        text(&output)
    );
    assert!(
        sandbox
            .records()
            .iter()
            .all(|r| r.get("candidate").is_none()),
        "{:?}",
        sandbox.records()
    );
}

#[test]
fn without_the_flag_a_gate_record_is_unchanged_fifteen_keys() {
    let sandbox = Sandbox::new();
    let output = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args(["gate", "T1", "plain", "--registry"])
        .arg(sandbox.registry())
        .arg("--guard")
        .arg(sandbox.root.join("guard-clear.sh"))
        .args(["--kind", "check", "--", "/bin/sh", "-c", "exit 0"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0), "{}", text(&output));
    assert_eq!(sandbox.records()[0].as_object().unwrap().len(), 15);
}

#[test]
fn status_is_read_only_one_line_per_cell_and_exit_codes_follow_the_decision() {
    let sandbox = Sandbox::new();
    sandbox.gate_all();
    let status = |sandbox: &Sandbox| {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "status", &sandbox.head, "--manifest"])
            .arg(sandbox.root.join("manifest.json"))
            .arg("--profile")
            .arg(sandbox.root.join("profile.json"))
            .arg("--registry")
            .arg(sandbox.registry())
            .output()
            .unwrap()
    };
    let open = status(&sandbox);
    assert_eq!(open.status.code(), Some(1), "{}", text(&open));
    let out = text(&open);
    assert!(out.contains("RED   device     stranger-install"), "{out}");
    assert!(out.contains("green cert       tests"), "{out}");
    assert!(
        out.contains("status: 7 cells, 2 red; NOT accepted"),
        "{out}"
    );
    sandbox.receipts(&["stranger-install", "relocation-receipt"]);
    let done = status(&sandbox);
    assert_eq!(done.status.code(), Some(0), "{}", text(&done));
    assert!(text(&done).contains("status: 7 cells, 0 red; every mandatory claim is green"));
    assert!(
        !sandbox.root.join("attempts").exists(),
        "status writes nothing"
    );
}
