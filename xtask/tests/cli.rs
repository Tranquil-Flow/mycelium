// SPDX-License-Identifier: AGPL-3.0-only
use std::{
    fs,
    path::PathBuf,
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Sandbox(PathBuf);
impl Sandbox {
    fn new() -> Self {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-tests")
            .join(format!(
                "{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        fs::create_dir_all(path.join("xtask")).unwrap();
        fs::write(path.join("Cargo.toml"), "[workspace]\n").unwrap();
        fs::write(
            path.join("xtask/allowlist.json"),
            r#"{"version":1,"entries":[]}"#,
        )
        .unwrap();
        fs::write(
            path.join("xtask/quarantine.json"),
            r#"{"version":1,"entries":[]}"#,
        )
        .unwrap();
        Self(path)
    }
    fn write(&self, name: &str, text: &str) {
        let path = self.0.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    fn run(&self, subcommand: &str) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .arg(subcommand)
            .arg("--root")
            .arg(&self.0)
            .output()
            .unwrap()
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
#[test]
fn lint_cli_rejects_negative_control_and_accepts_propagation() {
    let sandbox = Sandbox::new();
    sandbox.write(
        "crates/node/src/lib.rs",
        "fn stop_route() {\n    let _ = stop();\n}\n",
    );
    let rejected = sandbox.run("lint");
    assert_eq!(rejected.status.code(), Some(1));
    assert!(
        String::from_utf8(rejected.stdout)
            .unwrap()
            .contains("crates/node/src/lib.rs:2: SE001")
    );
    sandbox.write(
        "crates/node/src/lib.rs",
        "fn stop_route() -> Result<(), E> { stop()?; Ok(()) }",
    );
    let accepted = sandbox.run("lint");
    assert!(accepted.status.success(), "{:?}", accepted);
    assert!(
        String::from_utf8(accepted.stdout)
            .unwrap()
            .contains("0 violations")
    );
}
#[test]
fn only_own_fixtures_and_build_outputs_are_excluded() {
    let sandbox = Sandbox::new();
    sandbox.write("xtask/fixtures/reject/example.rs", "not Rust");
    sandbox.write("target/generated.rs", "not Rust");
    sandbox.write(
        "crates/node/fixtures/example.rs",
        "fn example() { let _ = work(); }",
    );
    let output = sandbox.run("lint");
    assert!(
        String::from_utf8(output.stdout)
            .unwrap()
            .contains("crates/node/fixtures/example.rs:1: SE001")
    );
}
#[test]
fn malformed_allowlist_and_source_are_hard_failures() {
    let sandbox = Sandbox::new();
    sandbox.write("xtask/allowlist.json", "{}");
    assert!(!sandbox.run("lint").status.success());
    sandbox.write("xtask/allowlist.json", r#"{"version":1,"entries":[]}"#);
    sandbox.write("crates/node/src/lib.rs", "fn {");
    let output = sandbox.run("lint");
    assert!(!output.status.success());
    assert!(String::from_utf8(output.stderr).unwrap().contains("PARSE"));
}
fn manifest(path: &str) -> String {
    serde_json::json!({"version":1,"tools":[{"name":"fixture", "detect":{"kind":"file","path":path}}],"groups":[{"id":"required","kind":"custom","mandatory":true,"needs":["fixture"],"after":[],"command":["not-executed"]}]}).to_string()
}
#[test]
fn preflight_exit_cannot_green_skip_missing_mandatory_input() {
    let sandbox = Sandbox::new();
    sandbox.write("xtask/prerequisites.json", &manifest("missing.bin"));
    let blocked = sandbox.run("preflight");
    assert_eq!(blocked.status.code(), Some(1));
    assert!(
        String::from_utf8(blocked.stdout)
            .unwrap()
            .contains("required: NOT_RUN(BLOCKED: fixture: missing file)")
    );
    sandbox.write("xtask/prerequisites.json", &manifest("Cargo.toml"));
    let ready = sandbox.run("preflight");
    assert!(ready.status.success());
    assert!(
        String::from_utf8(ready.stdout)
            .unwrap()
            .contains("required: RUN")
    );
}
#[test]
fn expired_quarantine_fails_even_with_present_prerequisite() {
    let sandbox = Sandbox::new();
    sandbox.write("xtask/prerequisites.json", &manifest("Cargo.toml"));
    sandbox.write("xtask/quarantine.json", r#"{"version":1,"entries":[{"group":"required","owner":"tests","reason":"known flake","expiry":"2000-01-01","replacement_coverage":["required"]}]}"#);
    let output = sandbox.run("preflight");
    assert!(!output.status.success());
    assert!(
        String::from_utf8(output.stderr)
            .unwrap()
            .contains("expired")
    );
}
#[test]
fn unknown_command_or_option_is_an_error() {
    let sandbox = Sandbox::new();
    assert!(!sandbox.run("typo").status.success());
    let output = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args(["lint", "--typo"])
        .output()
        .unwrap();
    assert!(!output.status.success());
}
