// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask gate` end to end: real child processes, a real registry directory, real
//! git HEAD, and (twice) a real nested `cargo test` on a one-test crate.
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};

static NEXT: AtomicUsize = AtomicUsize::new(0);

/// Keys of a real `tools/v02.py gate` row (registry/evidence.jsonl, EV-20261028-0236).
const PROTOTYPE_KEYS: [&str; 15] = [
    "argv",
    "at",
    "commit",
    "exit",
    "failed_tests",
    "id",
    "kind",
    "lane",
    "load",
    "log",
    "name",
    "targets",
    "total",
    "verdict",
    "wall_s",
];

struct Sandbox {
    root: PathBuf,
}
impl Sandbox {
    fn new() -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-gate-tests")
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
        };
        git(&["init", "-q"]);
        git(&["commit", "-q", "--allow-empty", "-m", "x"]);
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
        fs::write(
            root.join("guard-blocked.sh"),
            "echo BLOCKED: test\nexit 75\n",
        )
        .unwrap();
        Self { root }
    }
    fn registry(&self) -> PathBuf {
        self.root.join("registry")
    }
    fn evidence(&self) -> Vec<Value> {
        let path = self.registry().join("evidence.jsonl");
        fs::read_to_string(path)
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }
    fn head(&self) -> String {
        let out = Command::new("git")
            .args([
                "-C",
                self.root.join("wt").to_str().unwrap(),
                "rev-parse",
                "HEAD",
            ])
            .output()
            .unwrap();
        String::from_utf8(out.stdout).unwrap().trim().to_string()
    }
    fn command(&self, guard: &str, name: &str, kind: Option<&str>, argv: &[&str]) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_xtask"));
        command
            .args(["gate", "T1", name])
            .arg("--registry")
            .arg(self.registry())
            .arg("--guard")
            .arg(self.root.join(guard));
        if let Some(kind) = kind {
            command.args(["--kind", kind]);
        }
        command.arg("--").args(argv);
        command
    }
    fn gate(&self, name: &str, kind: Option<&str>, script: &str) -> Output {
        self.command("guard-clear.sh", name, kind, &["/bin/sh", "-c", script])
            .output()
            .unwrap()
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}

const GREEN: &str = "printf '     Running tests/a.rs (/t/deps/a-0123456789abcdef)\\n\\nrunning 3 tests\\ntest a ... ok\\n\\ntest result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s\\n\\n'";
const ZERO: &str = "printf '     Running tests/a.rs (/t/deps/a-0123456789abcdef)\\n\\nrunning 0 tests\\n\\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\\n\\n'";
const RED: &str = "printf '     Running tests/a.rs (/t/deps/a-0123456789abcdef)\\n\\nrunning 2 tests\\ntest a ... ok\\ntest b ... FAILED\\n\\ntest result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\\n\\nerror: test failed, to rerun pass `--test a`\\n'; exit 101";

#[test]
fn green_run_writes_one_record_with_the_prototype_schema_and_exit_zero() {
    let sandbox = Sandbox::new();
    let output = sandbox.gate("green", Some("test"), GREEN);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let rows = sandbox.evidence();
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    let mut keys: Vec<&str> = row
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(
        keys, PROTOTYPE_KEYS,
        "same fields as tools/v02.py gate rows"
    );
    assert_eq!(row["kind"], "gate");
    assert_eq!(row["lane"], "T1");
    assert_eq!(row["name"], "green");
    assert_eq!(row["verdict"], "PASS");
    assert_eq!(row["exit"], 0);
    assert_eq!(row["commit"], sandbox.head().as_str());
    assert_eq!(row["argv"][0], "/bin/sh");
    assert_eq!(
        row["total"],
        serde_json::json!({"passed": 3, "failed": 0, "ignored": 1})
    );
    assert_eq!(
        row["targets"],
        serde_json::json!([{"target": "tests/a.rs (a)", "ok": true, "passed": 3, "failed": 0, "ignored": 1}])
    );
    assert!(row["id"].as_str().unwrap().starts_with("EV-"));
    assert!(row["id"].as_str().unwrap().ends_with("-0001"));
    assert_eq!(row["id"].as_str().unwrap().len(), "EV-20261028-0001".len());
    assert!(row["wall_s"].is_number());
    assert!(row["at"].as_f64().unwrap() > 1.7e9);
    assert!(!row["load"].as_str().unwrap().is_empty());
    let log = fs::read_to_string(row["log"].as_str().unwrap()).unwrap();
    assert!(log.contains("test result: ok. 3 passed"));
    assert!(
        Path::new(row["log"].as_str().unwrap()).starts_with(sandbox.registry().join("logs/T1"))
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("EV-"));
}

#[test]
fn a_run_that_executed_zero_tests_is_recorded_zero_tests_and_fails_the_process() {
    let sandbox = Sandbox::new();
    let output = sandbox.gate("zero", Some("test"), ZERO);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert_eq!(sandbox.evidence()[0]["verdict"], "ZERO-TESTS");
    assert_eq!(
        sandbox.evidence()[0]["exit"],
        0,
        "the child itself exited 0"
    );
}

#[test]
fn a_run_with_no_test_output_is_never_pass() {
    let sandbox = Sandbox::new();
    let output = sandbox.gate("silent", Some("test"), "exit 0");
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert_eq!(sandbox.evidence()[0]["verdict"], "NO-TEST-OUTPUT");
    let output = sandbox.gate("silent-fail", Some("test"), "exit 3");
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(sandbox.evidence()[1]["verdict"], "RAN-NOTHING");
}

#[test]
fn failing_tests_record_fail_with_names_and_counts() {
    let sandbox = Sandbox::new();
    let output = sandbox.gate("red", Some("test"), RED);
    assert_eq!(output.status.code(), Some(1));
    let row = &sandbox.evidence()[0];
    assert_eq!(row["verdict"], "FAIL");
    assert_eq!(row["exit"], 101);
    assert_eq!(row["failed_tests"], serde_json::json!(["b"]));
    assert_eq!(row["total"]["failed"], 1);
}

#[test]
fn check_gates_pass_on_exit_zero_and_fail_otherwise() {
    let sandbox = Sandbox::new();
    assert_eq!(
        sandbox
            .gate("fmt", Some("check"), "echo fine")
            .status
            .code(),
        Some(0)
    );
    assert_eq!(
        sandbox.gate("lint", Some("check"), "exit 1").status.code(),
        Some(1)
    );
    let rows = sandbox.evidence();
    assert_eq!(
        (rows[0]["verdict"].as_str(), rows[1]["verdict"].as_str()),
        (Some("PASS"), Some("FAIL"))
    );
    assert_eq!(
        rows[0]["total"],
        serde_json::json!({"passed": 0, "failed": 0, "ignored": 0})
    );
}

#[test]
fn a_child_killed_by_a_signal_is_killed_with_a_null_exit() {
    let sandbox = Sandbox::new();
    let output = sandbox.gate("kill", Some("check"), "kill -9 $$");
    assert_eq!(output.status.code(), Some(1));
    let row = &sandbox.evidence()[0];
    assert_eq!(row["verdict"], "KILLED");
    assert!(row["exit"].is_null());
}

#[test]
fn the_host_guard_refusal_is_returned_verbatim_and_nothing_runs_or_is_recorded() {
    let sandbox = Sandbox::new();
    let marker = sandbox.root.join("ran");
    let script = format!("touch {}", marker.display());
    let output = sandbox
        .command(
            "guard-blocked.sh",
            "blocked",
            Some("check"),
            &["/bin/sh", "-c", &script],
        )
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(75), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stdout).contains("GUARD 75"));
    assert!(
        !marker.exists(),
        "command must not run when the guard refuses"
    );
    assert!(sandbox.evidence().is_empty());
}

#[test]
fn a_missing_guard_is_a_hard_error_not_a_skipped_guard() {
    let sandbox = Sandbox::new();
    let output = sandbox
        .command(
            "no-such-guard.sh",
            "x",
            Some("check"),
            &["/bin/sh", "-c", "exit 0"],
        )
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(sandbox.evidence().is_empty());
}

#[test]
fn child_environment_is_the_lane_environment_at_lowest_priority() {
    let sandbox = Sandbox::new();
    let mut command = sandbox.command(
        "guard-clear.sh",
        "env",
        Some("check"),
        &["/bin/sh", "-c", "echo target=$CARGO_TARGET_DIR inc=$CARGO_INCREMENTAL dbg=$CARGO_PROFILE_DEV_DEBUG,$CARGO_PROFILE_TEST_DEBUG py=[$PYTHONPATH]; ps -o ni= -p $$"],
    );
    command.env("PYTHONPATH", "/hermes");
    assert_eq!(command.output().unwrap().status.code(), Some(0));
    let log = fs::read_to_string(sandbox.evidence()[0]["log"].as_str().unwrap()).unwrap();
    assert!(
        log.contains(&format!("target={}", sandbox.root.join("target").display())),
        "{log}"
    );
    assert!(log.contains("inc=0 dbg=0,0 py=[]"), "{log}");
    assert!(
        log.lines()
            .any(|l| l.trim().parse::<u32>().is_ok_and(|n| n >= 19)),
        "nice -n 19 (or more if the caller is already niced): {log}"
    );
}

#[test]
fn ids_continue_the_file_and_logs_are_never_overwritten() {
    let sandbox = Sandbox::new();
    fs::write(
        sandbox.registry().join("evidence.jsonl"),
        "{\"id\": \"EV-20000101-0001\"}\n{\"id\": \"EV-20000101-0002\"}\n{\"id\": \"EV-20000101-0003\"}\n",
    )
    .unwrap();
    assert_eq!(
        sandbox
            .gate("same", Some("check"), "echo one")
            .status
            .code(),
        Some(0)
    );
    assert_eq!(
        sandbox
            .gate("same", Some("check"), "echo two")
            .status
            .code(),
        Some(0)
    );
    let rows = sandbox.evidence();
    assert_eq!(rows.len(), 5);
    assert!(rows[3]["id"].as_str().unwrap().ends_with("-0004"));
    assert!(rows[4]["id"].as_str().unwrap().ends_with("-0005"));
    let (a, b) = (
        rows[3]["log"].as_str().unwrap(),
        rows[4]["log"].as_str().unwrap(),
    );
    assert_ne!(a, b, "same lane+name in the same second must not clobber");
    assert!(fs::read_to_string(a).unwrap().contains("one"));
    assert!(fs::read_to_string(b).unwrap().contains("two"));
}

#[test]
fn concurrent_gates_append_whole_lines_with_unique_sequential_ids() {
    let sandbox = Sandbox::new();
    let children: Vec<_> = (0..8)
        .map(|i| {
            sandbox
                .command(
                    "guard-clear.sh",
                    &format!("c{i}"),
                    Some("check"),
                    &["/bin/sh", "-c", "echo hi"],
                )
                .spawn()
                .unwrap()
        })
        .collect();
    for mut child in children {
        assert!(child.wait().unwrap().success());
    }
    let rows = sandbox.evidence();
    assert_eq!(rows.len(), 8);
    let mut seqs: Vec<u32> = rows
        .iter()
        .map(|r| r["id"].as_str().unwrap()[12..].parse().unwrap())
        .collect();
    seqs.sort_unstable();
    assert_eq!(seqs, (1..=8).collect::<Vec<_>>());
}

#[test]
fn bad_lane_name_or_missing_command_is_a_usage_error_and_records_nothing() {
    let sandbox = Sandbox::new();
    let cases: [&[&str]; 6] = [
        &["gate", "NOPE", "x", "--", "/bin/true"],
        &["gate", "T1", "../escape", "--", "/bin/true"],
        &["gate", "T1", "x"],
        &["gate", "T1", "x", "--"],
        &["gate", "T1", "x", "--kind", "weird", "--", "/bin/true"],
        &["gate", "parse", "x", "--", "/bin/true"],
    ];
    for args in cases {
        let mut command = Command::new(env!("CARGO_BIN_EXE_xtask"));
        // `--registry` must precede the child argv; insert it before any `--`.
        let split = args.iter().position(|a| *a == "--").unwrap_or(args.len());
        command
            .args(&args[..split])
            .arg("--registry")
            .arg(sandbox.registry())
            .args(&args[split..]);
        let output = command.output().unwrap();
        assert_eq!(output.status.code(), Some(2), "{args:?}: {output:?}");
    }
    assert!(sandbox.evidence().is_empty());
}

#[test]
fn parse_subcommand_judges_an_existing_log_without_recording() {
    let sandbox = Sandbox::new();
    let logs = Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/gate-logs");
    let run = |file: &str, exit: &str| {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["gate", "parse"])
            .arg(logs.join(file))
            .args(["--exit", exit])
            .output()
            .unwrap()
    };
    let pass = run("pass-multi-target.log", "0");
    assert_eq!(pass.status.code(), Some(0));
    assert!(
        String::from_utf8_lossy(&pass.stdout)
            .starts_with("PASS: 109 passed / 0 failed / 0 ignored over 54 targets")
    );
    let zero = run("zero-tests-excerpt.log", "0");
    assert_eq!(zero.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&zero.stdout).starts_with("ZERO-TESTS: 0 passed"));
    assert!(sandbox.evidence().is_empty());
}

fn tiny_crate(root: &Path) -> PathBuf {
    let dir = root.join("tiny");
    fs::create_dir_all(dir.join("tests")).unwrap();
    fs::write(
        dir.join("Cargo.toml"),
        "[package]\nname = \"tiny\"\nversion = \"0.0.0\"\nedition = \"2021\"\n\n[workspace]\n",
    )
    .unwrap();
    fs::create_dir_all(dir.join("src")).unwrap();
    fs::write(dir.join("src/lib.rs"), "").unwrap();
    fs::write(
        dir.join("tests/one.rs"),
        "#[test]\nfn only() { assert_eq!(1 + 1, 2); }\n",
    )
    .unwrap();
    dir
}

#[test]
fn a_real_cargo_test_gate_counts_per_target_and_a_filter_matching_nothing_is_zero_tests() {
    let sandbox = Sandbox::new();
    let tiny = tiny_crate(&sandbox.root);
    // Point the lane worktree at the tiny crate (a git repo itself is not needed for cargo).
    let lanes = fs::read_to_string(sandbox.registry().join("lanes.tsv")).unwrap();
    fs::write(
        sandbox.registry().join("lanes.tsv"),
        lanes.replace(
            &sandbox.root.join("wt").display().to_string(),
            &tiny.display().to_string(),
        ),
    )
    .unwrap();
    let ok = sandbox
        .command(
            "guard-clear.sh",
            "real-pass",
            None,
            &["cargo", "test", "--offline", "-j", "1"],
        )
        .output()
        .unwrap();
    assert_eq!(ok.status.code(), Some(0), "{ok:?}");
    let none = sandbox
        .command(
            "guard-clear.sh",
            "real-zero",
            None,
            &["cargo", "test", "--offline", "-j", "1", "no_such_test_name"],
        )
        .output()
        .unwrap();
    assert_eq!(none.status.code(), Some(1), "{none:?}");
    let rows = sandbox.evidence();
    assert_eq!(
        rows[0]["verdict"], "PASS",
        "kind inferred from `cargo test`"
    );
    assert_eq!(
        rows[0]["total"],
        serde_json::json!({"passed": 1, "failed": 0, "ignored": 0})
    );
    let labels: Vec<&str> = rows[0]["targets"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["target"].as_str().unwrap())
        .collect();
    assert!(labels.contains(&"tests/one.rs (one)"), "{labels:?}");
    assert!(
        labels.contains(&"unittests src/lib.rs (tiny)"),
        "{labels:?}"
    );
    assert!(labels.contains(&"doc:tiny"), "{labels:?}");
    assert_eq!(rows[1]["verdict"], "ZERO-TESTS");
    assert_eq!(rows[1]["exit"], 0, "cargo itself exited 0");
}

#[test]
fn a_taken_log_name_is_never_truncated_whatever_the_clock_says() {
    let sandbox = Sandbox::new();
    let directory = sandbox.root.join("logs-direct");
    let mut paths = Vec::new();
    for round in 0..3 {
        let (mut file, path) =
            xtask::gate::create_log(&directory, "20261028T120000", "same").unwrap();
        std::io::Write::write_all(&mut file, format!("round {round}").as_bytes()).unwrap();
        paths.push(path);
    }
    let names: Vec<_> = paths
        .iter()
        .map(|p| p.file_name().unwrap().to_str().unwrap().to_string())
        .collect();
    assert_eq!(
        names,
        [
            "20261028T120000-same.log",
            "20261028T120000-same-2.log",
            "20261028T120000-same-3.log"
        ]
    );
    for (round, path) in paths.iter().enumerate() {
        assert_eq!(fs::read_to_string(path).unwrap(), format!("round {round}"));
    }
}
