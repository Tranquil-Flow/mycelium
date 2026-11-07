// SPDX-License-Identifier: AGPL-3.0-only
//! Cargo-level regression for the headless launcher: this test BUILDS the `headless` example
//! (a test host over the real `api` boundary, not a product binary) and the external journey
//! runner, then RUNS the unchanged L3 corpus against the Python golden through the runner's own
//! sandbox (loopback only, scratch-only writes), so the evidence does not live only in an
//! external harness invocation. The corpus, comparator and golden are the L3 lane's, untouched.
//!
//! Host rules: the nested build is `--offline --locked -j 2`; the runner admits a launch only
//! at memory-pressure level 1 and refuses otherwise (a refusal fails this test: it is BLOCKED,
//! never a silent pass).
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};

/// `<target>/<profile>` of the running test binary (`<target>/<profile>/deps/<name>`).
fn profile_dir() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    exe.parent().unwrap().parent().unwrap().to_path_buf()
}
fn build() {
    let profile = profile_dir();
    let target = profile.parent().unwrap();
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.toml");
    // Cargo's --example selector applies across every selected package; listing journey-tests
    // beside it does not build that package's binary. Build each consumed target explicitly.
    for selection in [
        ["-p", "api", "--example", "headless"],
        ["-p", "journey-tests", "--bin", "journey-tests"],
    ] {
        let mut command = Command::new(std::env::var("CARGO").unwrap_or_else(|_| "cargo".into()));
        command.args(["build", "--offline", "--locked", "-j", "2"]);
        command.args(selection);
        command.arg("--manifest-path").arg(&manifest);
        command.arg("--target-dir").arg(target);
        if profile.file_name().is_some_and(|name| name == "release") {
            command.arg("--release");
        }
        let output = command.output().expect("cargo is runnable");
        assert!(
            output.status.success(),
            "nested build {selection:?} failed:\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    assert!(
        profile.join("journey-tests").is_file(),
        "nested build omitted required journey-tests binary: {}",
        profile.join("journey-tests").display()
    );
    assert!(profile.join("examples/headless").is_file());
}
fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}
/// The runner's host guard used to `pgrep -f` a pattern that its own sibling guard processes also
/// carried, so two runner invocations at the same time blocked each other
/// (`BLOCKED:certification_waiter`); the guard now uses the `[P]ARENT-cert-` form. The corpora
/// still run one at a time in one test process so they never contend for the host.
static ONE_AT_A_TIME: std::sync::Mutex<()> = std::sync::Mutex::new(());
/// Runs one unchanged corpus: the receipt of the external runner, parsed.
fn run_corpus(tag: &str, golden: &str, extra: &[&str]) -> (std::process::Output, Value) {
    let _serial = ONE_AT_A_TIME
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    build();
    let profile = profile_dir();
    let work = profile.join(format!("l10-launcher-{tag}-{}", std::process::id()));
    fs::remove_dir_all(&work).unwrap_or(());
    fs::create_dir_all(&work).unwrap();
    // An exact copy: the nested build above can never change the hashed executable mid-run.
    let executable = work.join("headless");
    fs::copy(profile.join("examples/headless"), &executable).unwrap();
    let manifest = work.join("target.json");
    let source_sha = format!("{:040x}", u128::from(std::process::id()));
    fs::write(
        &manifest,
        json!({"implementation": "rust", "source_sha": source_sha,
               "executable": executable, "executable_sha256": sha256(&executable)})
        .to_string(),
    )
    .unwrap();
    let receipt = work.join("receipt.json");
    let fixtures =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../test-support/journeys/fixtures");
    let output = Command::new(profile.join("journey-tests"))
        .arg("--target")
        .arg(&manifest)
        .arg("--receipt")
        .arg(&receipt)
        .arg("--golden")
        .arg(fixtures.join(golden))
        .args(extra)
        .output()
        .expect("the journey runner is runnable");
    let record: Value = fs::read(&receipt)
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or(Value::Null);
    fs::remove_dir_all(&work).unwrap_or(());
    (output, record)
}
fn assert_clean_launches(record: &Value, launches: usize) {
    let list = record["launches"].as_array().expect("launch records");
    assert_eq!(list.len(), launches);
    for launch in list {
        assert_eq!(launch["exit_code"], 0, "{launch}");
        assert_eq!(launch["forced_kill"], false, "{launch}");
        assert_eq!(launch["leftover_pids"], json!([]), "{launch}");
        assert_eq!(launch["listener_closed"], true, "{launch}");
        assert_eq!(launch["stale_control_files"], json!([]), "{launch}");
        assert_eq!(launch["ps_verified_absent"], true, "{launch}");
    }
}

#[test]
fn headless_launcher_passes_the_unchanged_l3_baseline_journeys_against_the_python_golden() {
    let (output, record) = run_corpus("baseline", "python-v014.json", &[]);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "{stdout} {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(record["result"], "PASS", "{}", record["error"]);
    assert_eq!(record["implementation"], "rust");
    assert_eq!(record["observations"].as_array().unwrap().len(), 20);
    assert_clean_launches(&record, 5);
}

#[test]
fn headless_launcher_passes_the_unchanged_l3_boundaries_corpus_against_the_python_golden() {
    let (output, record) = run_corpus(
        "boundaries",
        "python-v014-boundaries.json",
        &["--scenario", "loopback-boundaries"],
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "{stdout} {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(record["result"], "PASS", "{}", record["error"]);
    assert_eq!(record["observations"].as_array().unwrap().len(), 33);
    // Owner, unjoined target, and the restarted target.
    assert_clean_launches(&record, 3);
    // The two-Mac/relay journeys stay BLOCKED (never silently counted as passed).
    assert_eq!(record["exclusions"].as_array().unwrap().len(), 6);
    assert!(
        record["exclusions"]
            .as_array()
            .unwrap()
            .iter()
            .all(|e| e["result"] == "BLOCKED")
    );
}
