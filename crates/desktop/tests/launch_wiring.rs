// SPDX-License-Identifier: AGPL-3.0-only
//! The built shell executable's entry routing, end to end (wiring of `launch_location` and the
//! installer family into `mycelium`, ported from `mycelium_a13_macos/__main__.py::main`).
//! Needs the `tauri-shell` feature: `cargo test -p desktop --features tauri-shell --test launch_wiring`.
//!
//! Every launch runs with `HOME` pointing at a scratch folder and the refusal alert switched off,
//! so the real home, `/Applications`, `~/Applications`, user state and the live v0.1.4 app are
//! unreachable; no window is expected (a window would mean the route was NOT wired). Each child
//! is waited on with a deadline and killed by its exact pid if it is still running.
#![cfg(all(feature = "tauri-shell", target_os = "macos"))]
use budgets::{BudgetId, Entries};
use desktop::docs::{self, FinalizeSpec, Signer};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
fn budget(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}

static SEQ: AtomicUsize = AtomicUsize::new(0);
const EXE: &str = env!("CARGO_BIN_EXE_mycelium");

struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let p = std::env::temp_dir().canonicalize().unwrap().join(format!(
            "v02-l11-wire-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir_all(p.join("home")).unwrap();
        Self(p)
    }
    fn home(&self) -> PathBuf {
        self.0.join("home")
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.0) {
            eprintln!("cleanup_failed:{:?}", error.kind());
        }
    }
}
struct Done {
    code: Option<i32>,
    stdout: String,
    stderr: String,
    killed: bool,
}
fn launch(args: &[&str], home: &Path) -> Done {
    let mut child = Command::new(EXE)
        .args(args)
        .env("HOME", home)
        .env("MYCELIUM_LAUNCH_ALERT", "off")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + budget(BudgetId::MemberRestartGrace);
    let mut killed = false;
    loop {
        if child.try_wait().unwrap().is_some() {
            break;
        }
        if Instant::now() > deadline {
            child.kill().unwrap(); // exact pid: our own child
            killed = true;
            break;
        }
        std::thread::sleep(budget(BudgetId::OwnerPoll));
    }
    let out = child.wait_with_output().unwrap();
    Done {
        code: out.status.code(),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        killed,
    }
}
/// A finalized signed bundle whose app executable is inert data (never run).
fn bundle(scratch: &Scratch, version: &str) -> PathBuf {
    let dir = scratch.0.join(format!("bundle-{version}"));
    let exe = dir.join("Mycelium A13 Demo.app/Contents/MacOS/mycelium");
    fs::create_dir_all(exe.parent().unwrap()).unwrap();
    fs::write(&exe, b"inert stand-in, never executed").unwrap();
    fs::set_permissions(&exe, fs::Permissions::from_mode(0o755)).unwrap();
    let issued = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
        .floor();
    docs::finalize(
        &dir,
        &Signer::from_seed([0x44; 32], docs::SIGNER_ENDPOINT_ID),
        &FinalizeSpec {
            version,
            rollback_floor: "0.1.0",
            source_revision: "0123456789abcdef0123456789abcdef01234567",
            issued_at: issued,
            minimum_os: "15.6.1",
        },
    )
    .unwrap();
    dir
}

#[test]
fn a_bare_launch_outside_the_install_refuses_and_starts_no_window() {
    let scratch = Scratch::new();
    let done = launch(&[], &scratch.home());
    assert!(
        !done.killed,
        "the shell started instead of refusing: {}",
        done.stdout
    );
    assert_eq!(done.code, Some(2), "{}", done.stderr);
    let line: serde_json::Value =
        serde_json::from_str(done.stderr.lines().next().unwrap()).unwrap();
    assert_eq!(line["event"], "launcher_refused");
    assert_eq!(line["code"], "not_installed");
    assert_eq!(line["location"], "elsewhere");
    assert!(
        line["message"]
            .as_str()
            .unwrap()
            .contains("Install Mycelium.command")
    );
    assert!(
        !done.stdout.contains("L11_"),
        "no window receipts: {}",
        done.stdout
    );
    assert!(
        !done.stderr.contains(&scratch.0.display().to_string()),
        "no path in the refusal"
    );
    // Finder's -psn argument is not a request for any mode.
    let done = launch(&["-psn_0_12345"], &scratch.home());
    assert_eq!(done.code, Some(2), "{}", done.stderr);
    // Nothing was written anywhere in the scratch home.
    assert_eq!(fs::read_dir(scratch.home()).unwrap().count(), 0);
}

#[test]
fn unknown_arguments_are_still_rejected() {
    let scratch = Scratch::new();
    let done = launch(&["--bogus"], &scratch.home());
    assert!(!done.killed);
    assert_ne!(done.code, Some(0));
    assert!(
        done.stderr.contains("no_arguments_accepted"),
        "{}",
        done.stderr
    );
}

#[test]
fn the_installer_family_runs_through_the_executable_against_the_given_home_only() {
    let scratch = Scratch::new();
    let home = scratch.home();
    let one = bundle(&scratch, "0.2.0");
    let state = home.join("Library/Application Support/Mycelium A13 macOS Demo");
    let done = launch(&["--install", "--bundle", one.to_str().unwrap()], &home);
    assert!(!done.killed);
    assert_eq!(done.code, Some(0), "{}", done.stderr);
    let report: serde_json::Value = serde_json::from_str(&done.stdout).unwrap();
    assert_eq!(
        (report["action"].as_str(), report["version"].as_str()),
        (Some("install"), Some("0.2.0"))
    );
    assert_eq!(
        fs::read_link(home.join("Applications/Mycelium A13.app")).unwrap(),
        home.join("Library/Application Support/Mycelium/programs/0.2.0/Mycelium A13 Demo.app")
    );
    assert!(state.is_dir());
    let status: serde_json::Value =
        serde_json::from_str(&launch(&["--install-status"], &home).stdout).unwrap();
    assert_eq!(status["current"], "0.2.0");
    // A refused run is one structured stderr line, exit 2.
    let tampered = bundle(&scratch, "0.2.1");
    fs::write(
        tampered.join("Mycelium A13 Demo.app/Contents/MacOS/mycelium"),
        b"x",
    )
    .unwrap();
    let done = launch(
        &["--install", "--bundle", tampered.to_str().unwrap()],
        &home,
    );
    assert_eq!(done.code, Some(2));
    let line: serde_json::Value = serde_json::from_str(&done.stderr).unwrap();
    assert_eq!(line["refused"], "package_digest_mismatch");
    // Uninstall keeps state unless asked.
    let done = launch(&["--uninstall"], &home);
    assert_eq!(done.code, Some(0), "{}", done.stderr);
    assert!(
        state.is_dir()
            && !home
                .join("Library/Application Support/Mycelium/programs")
                .exists()
    );
    let done = launch(&["--uninstall", "--delete-identity-and-state"], &home);
    assert_eq!(done.code, Some(0), "{}", done.stderr);
    assert!(!state.exists());
    // Bad usage: exit 2, no action taken.
    assert_eq!(launch(&["--install", "--rollback"], &home).code, Some(2));
}
