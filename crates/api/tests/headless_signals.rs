// SPDX-License-Identifier: AGPL-3.0-only
//! SIGTERM / SIGINT in the launcher as v0.1.4 (`__main__.py:186-203`): the signal only sets the stop
//! flag the main loop polls; the launcher then closes normally - exit 0, nothing more on stdout or
//! stderr, the transient control files (`native-control.json`, `open-demo.html`) removed. Expected
//! values come from the EXECUTED oracle (`L10-evidence/r11-probe-sigterm.py` -> `oracle_sigterm.json`).
use budgets::{BudgetId, Entries};
use rustix::process::{Pid, Signal, kill_process};
use serde_json::Value;
use std::{
    fs,
    io::{BufRead, BufReader, Read},
    net::{Ipv4Addr, TcpStream},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

fn profile_dir() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    exe.parent().unwrap().parent().unwrap().to_path_buf()
}
fn build() {
    let profile = profile_dir();
    let target = profile.parent().unwrap();
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.toml");
    let mut command = Command::new(std::env::var("CARGO").unwrap_or_else(|_| "cargo".into()));
    command.args(["build", "--offline", "--locked", "-j", "2", "-p", "api"]);
    command.args(["--example", "headless"]);
    command.arg("--manifest-path").arg(manifest);
    command.arg("--target-dir").arg(target);
    if profile.file_name().is_some_and(|name| name == "release") {
        command.arg("--release");
    }
    let output = command.output().expect("cargo is runnable");
    assert!(
        output.status.success(),
        "nested build failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
fn patience() -> Duration {
    let millis = BudgetId::ModelSelection
        .span(Entries::new(0))
        .unwrap()
        .millis();
    Duration::from_millis(millis * 4)
}
fn poll() -> Duration {
    let millis = BudgetId::OwnerPoll.span(Entries::new(0)).unwrap().millis();
    Duration::from_millis(millis)
}
fn names(dir: &Path) -> Vec<String> {
    let mut out: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    out.sort();
    out
}
fn launch(root: &Path) -> Child {
    launch_with(root, None)
}
/// The launcher, optionally under a descriptor limit (`ulimit -n`, then `exec`: the same pid).
fn launch_with(root: &Path, descriptors: Option<u32>) -> Child {
    fs::create_dir_all(root).unwrap();
    fs::set_permissions(root, fs::Permissions::from_mode(0o700)).unwrap();
    let launcher = profile_dir().join("examples/headless");
    let mut command = match descriptors {
        None => Command::new(launcher),
        Some(limit) => {
            let mut shell = Command::new("/bin/sh");
            shell
                .arg("-c")
                .arg(format!("ulimit -n {limit} && exec \"$0\" \"$@\""))
                .arg(launcher);
            shell
        }
    };
    command
        .args(["--headless", "--offline", "--state-root"])
        .arg(root)
        .env("HOME", root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the headless example was built")
}

// A readiness assertion can unwind before the explicit signal/wait path.
// Borrowing the Child keeps its identity/ownership in the test until it is reaped.
struct ReapOnDrop<'a>(&'a mut Child);
impl Drop for ReapOnDrop<'_> {
    fn drop(&mut self) {
        let mut failed = false;
        match self.0.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => {
                if let Err(error) = self.0.kill() {
                    eprintln!("reap_cleanup_failed phase=kill kind={:?}", error.kind());
                    failed = true;
                }
            }
            Err(error) => {
                eprintln!("reap_cleanup_failed phase=try_wait kind={:?}", error.kind());
                failed = true;
                // Wait ownership is unproven: do not signal a possibly reused PID.
            }
        }
        if let Err(error) = self.0.wait() {
            eprintln!("reap_cleanup_failed phase=wait kind={:?}", error.kind());
            failed = true;
        }
        // Report every failed phase, finish cleanup, and preserve an existing panic.
        if failed && !thread::panicking() {
            panic!("owned child cleanup failed");
        }
    }
}

fn run(label: &str, signal: Signal) {
    build();
    let oracle: Value = serde_json::from_str(include_str!("fixtures/oracle_sigterm.json")).unwrap();
    let want = oracle["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["label"] == label)
        .unwrap();
    // `_private_root` refuses a non-canonical state root (macOS `TMPDIR` is under the `/var` symlink).
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("l10r11-{label}-{}", std::process::id()));
    fs::remove_dir_all(&root).unwrap_or(());
    let mut child = launch(&root);
    let guard = ReapOnDrop(&mut child);
    let child = &mut *guard.0;
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let (send, receive) = mpsc::channel();
    let (ready_send, ready) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut first = String::new();
        stdout.read_line(&mut first).unwrap_or(0);
        ready_send.send(first.clone()).unwrap_or(());
        let mut rest = String::new();
        stdout.read_to_string(&mut rest).unwrap_or(0);
        send.send((first, rest)).unwrap_or(());
    });
    // The launch event is the launcher's readiness; the control files exist from then on.
    let announced = ready.recv_timeout(patience()).unwrap_or_default();
    assert!(
        announced.contains("launcher_started"),
        "{label}: no launcher_started within {:?}; child_pid={}; child_state={:?}; stdout={announced:?}",
        patience(),
        child.id(),
        child.try_wait()
    );
    let before = names(&root);
    for file in ["native-control.json", "open-demo.html"] {
        assert!(
            before.iter().any(|n| n == file),
            "{file} missing: {before:?}"
        );
    }
    let pid = Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap();
    kill_process(pid, signal).unwrap(); // the exact pid
    let waited = Instant::now() + patience();
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() > waited {
            child.kill().unwrap_or(());
            child.wait().unwrap_or_default();
            panic!("{label}: the launcher ignored the signal (killed after the patience)");
        }
        thread::sleep(poll());
    };
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap_or(0);
    reader.join().unwrap_or(());
    let (first, rest) = receive.recv_timeout(patience()).unwrap_or_default();
    let after = names(&root);
    assert!(first.contains("launcher_started"), "{first}");
    assert_eq!(
        status.code(),
        want["exit_code"].as_i64().map(|c| c as i32),
        "{status:?}"
    );
    assert_eq!(stderr.trim(), "", "{stderr}");
    assert!(rest.trim().is_empty(), "stdout after the signal: {rest}");
    // The oracle removes exactly its transient control files; so must the port.
    let removed_by_oracle: Vec<&str> = ["native-control.json", "open-demo.html"].to_vec();
    for file in &removed_by_oracle {
        assert!(
            !after.iter().any(|n| n == file),
            "{file} left behind: {after:?}"
        );
        assert!(
            want["files_before"]
                .as_array()
                .unwrap()
                .iter()
                .any(|n| n == file)
                && !want["files_after"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|n| n == file),
            "the oracle fixture does not remove {file}"
        );
    }
    fs::remove_dir_all(&root).unwrap_or(());
}

#[test]
fn readiness_panic_reaps_the_owned_child_before_harness_rescue() {
    let mut child = Command::new("/bin/sleep").arg("60").spawn().unwrap();
    let pid = Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap();
    let unwind = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _guard = ReapOnDrop(&mut child);
        panic!("controlled readiness assertion");
    }));
    assert!(unwind.is_err());
    let reaped_by_guard = child.try_wait().unwrap().is_some();
    // Preserve the failing control without leaving the deliberately held child.
    if !reaped_by_guard {
        child.kill().unwrap();
        child.wait().unwrap();
    }
    assert!(
        reaped_by_guard,
        "panic guard must reap before harness rescue"
    );
    assert_eq!(
        rustix::process::test_kill_process(pid),
        Err(rustix::io::Errno::SRCH)
    );
}

// Reap outside Child to exercise real OS errors, not injected production fields.
// The subprocess makes Drop's stderr observable even during an actual unwind.
#[test]
fn cleanup_errors_report_phase_and_kind_without_double_panic() {
    const MODE: &str = "L10_REAP_DIAGNOSTIC_CONTROL";
    if let Ok(mode) = std::env::var(MODE) {
        let mut child = Command::new("/bin/sleep").arg("60").spawn().unwrap();
        let pid = Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap();
        child.kill().unwrap();
        rustix::process::waitpid(Some(pid), rustix::process::WaitOptions::empty()).unwrap();
        assert_eq!(
            rustix::process::test_kill_process(pid),
            Err(rustix::io::Errno::SRCH)
        );
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = ReapOnDrop(&mut child);
            if mode == "unwind" {
                panic!("controlled readiness assertion");
            }
        }));
        assert!(result.is_err(), "cleanup failure must not pass silently");
        if mode == "unwind" {
            assert_eq!(
                result.unwrap_err().downcast_ref::<&str>(),
                Some(&"controlled readiness assertion"),
                "cleanup must preserve the original panic"
            );
        }
        return;
    }
    for mode in ["normal", "unwind"] {
        let output = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "cleanup_errors_report_phase_and_kind_without_double_panic",
                "--nocapture",
            ])
            .env(MODE, mode)
            .output()
            .unwrap();
        let stderr = String::from_utf8(output.stderr).unwrap();
        for (phase, errno) in [
            ("try_wait", rustix::io::Errno::CHILD),
            ("wait", rustix::io::Errno::CHILD),
        ] {
            let kind = std::io::Error::from_raw_os_error(errno.raw_os_error()).kind();
            assert!(
                stderr.contains(&format!("reap_cleanup_failed phase={phase} kind={kind:?}")),
                "missing phase/ErrorKind cleanup diagnostic: {phase} ({mode})"
            );
        }
        assert!(
            !stderr.contains("phase=kill"),
            "lost wait ownership must not signal a PID"
        );
        assert!(
            output.status.success(),
            "diagnostic subprocess must survive"
        );
    }
}

#[test]
fn sigterm_closes_the_launcher_as_v014() {
    run("sigterm", Signal::TERM);
}
#[test]
fn sigint_closes_the_launcher_as_v014() {
    run("sigint", Signal::INT);
}

/// B15f (REVIEW-laneB15 F2): a listener that ends without a quit closes the launcher as a quit
/// does: the helper loops end, the transient control files go away and the exit reports the
/// listener's failure (exit 3). Here `accept` fails (EMFILE): the launcher runs under a small
/// descriptor limit and the test holds connections until the listener is gone. Before, nothing
/// closed the core when the listener ended without panicking, and the process waited for a
/// signal.
#[test]
fn a_listener_that_ends_without_a_quit_closes_the_launcher() {
    build();
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("b15f-listener-{}", std::process::id()));
    fs::remove_dir_all(&root).unwrap_or(());
    let mut child = launch_with(&root, Some(64));
    let guard = ReapOnDrop(&mut child);
    let child = &mut *guard.0;
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let (ready_send, ready) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut first = String::new();
        stdout.read_line(&mut first).unwrap_or(0);
        ready_send.send(first).unwrap_or(());
        let mut rest = String::new();
        stdout.read_to_string(&mut rest).unwrap_or(0);
    });
    let announced = ready.recv_timeout(patience()).unwrap_or_default();
    assert!(announced.contains("launcher_started"), "{announced:?}");
    let control: Value =
        serde_json::from_str(&fs::read_to_string(root.join("native-control.json")).unwrap())
            .unwrap();
    let port = u16::try_from(control["port"].as_u64().unwrap()).unwrap();
    // Held, idle connections use up the launcher's descriptors until `accept` fails.
    let mut held = Vec::new();
    while let Ok(stream) = TcpStream::connect((Ipv4Addr::LOCALHOST, port)) {
        held.push(stream);
        assert!(
            held.len() < 1024,
            "the listener never ended under its descriptor limit"
        );
    }
    drop(held);
    let waited = Instant::now() + patience();
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        assert!(
            Instant::now() < waited,
            "the launcher kept running after its listener ended (it waits for a signal)"
        );
        thread::sleep(poll());
    };
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap_or(0);
    reader.join().unwrap_or(());
    assert_eq!(status.code(), Some(3), "{status:?} {stderr}");
    assert!(
        stderr.contains("launcher_failed") && stderr.contains("listener_accept_failed"),
        "the exit does not report the listener's failure: {stderr}"
    );
    let after = names(&root);
    for file in ["native-control.json", "open-demo.html"] {
        assert!(
            !after.iter().any(|n| n == file),
            "{file} left behind: {after:?}"
        );
    }
    fs::remove_dir_all(&root).unwrap_or(());
}
