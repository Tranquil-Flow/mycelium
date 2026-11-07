// SPDX-License-Identifier: AGPL-3.0-only
//! Quit exit status of the real shell, as v0.1.4's launcher:
//! `mycelium_a13_macos/__main__.py:219-223 @ f4e366e9` returns 3 and prints
//! `{"event": "launcher_cleanup_failed", "code": "cleanup_incomplete", "state_retained": true}` on
//! stderr when the app recorded a cleanup error after Quit, and returns 0 otherwise. The executed
//! probes are `SLICE-1-evidence/r8-oracle/launcher-{proven,unproven}.log` (exit 0 / exit 3 with
//! exactly that event line; sha256 in `r8-oracle/SHA256SUMS`).
//!
//! Explicit own-development-build GUI gate (`mycelium-desktop-dev`, a fake core, no product state,
//! no installed application): the exact-pid Apple Event quit is sent to our own unreaped child.
#![cfg(all(feature = "dev-shell", target_os = "macos"))]
mod support;
use budgets::BudgetId;
use std::{
    io::{BufRead, BufReader},
    os::unix::process::CommandExt,
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};
use support::{budget, pressure_level, send_quit};

/// v0.1.4's line, byte for byte (json.dumps default separators).
const V014_EVENT: &str =
    r#"{"event": "launcher_cleanup_failed", "code": "cleanup_incomplete", "state_retained": true}"#;

static NEXT_SCRATCH: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

struct Run {
    /// Time from the exact-pid Apple Event (sent before this clock stopped being read) to exit.
    quit_to_exit: Option<Duration>,
    code: Option<i32>,
    stdout: Vec<String>,
    stderr: Vec<String>,
}

/// What starts the Quit: the exact-pid Apple Event (the Dock / Finder / `osascript` path), or a
/// trusted probe script of the development shell that makes AppKit perform the menu item /
/// Command-Q key equivalent of our own window (`native_probe.rs`).
enum Trigger<'a> {
    AppleEvent,
    ProbeScript(&'a str),
}

fn quit_run(args: &[&str]) -> Run {
    quit_run_within(args, budget(BudgetId::StageCleanup))
}

fn quit_run_within(args: &[&str], exit_budget: Duration) -> Run {
    quit_run_with(args, &Trigger::AppleEvent, exit_budget)
}

fn quit_run_with(args: &[&str], trigger: &Trigger, exit_budget: Duration) -> Run {
    assert_eq!(pressure_level(), "1", "memory safety gate before launch");
    let root = std::env::temp_dir().join(format!(
        "v02-l11-quit-codes-{}-{}",
        std::process::id(),
        // One scratch bundle per run: the tests of this file run on parallel threads.
        NEXT_SCRATCH.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
    let bundle = root.join("Relocated L11 Development.app");
    std::fs::create_dir_all(bundle.join("Contents/MacOS")).unwrap();
    let executable = bundle.join("Contents/MacOS/mycelium-desktop-dev");
    std::fs::copy(env!("CARGO_BIN_EXE_mycelium-desktop-dev"), &executable).unwrap();
    std::fs::write(bundle.join("Contents/Info.plist"), b"<?xml version=\"1.0\"?><!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\"><plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>mycelium-desktop-dev</string><key>CFBundleIdentifier</key><string>org.mycelium.l11-development</string><key>CFBundleName</key><string>Mycelium L11 Development</string><key>CFBundlePackageType</key><string>APPL</string><key>LSMinimumSystemVersion</key><string>15.6.1</string></dict></plist>").unwrap();
    let script_path = root.join("quit-variant-probe.js");
    let mut command = Command::new(&executable);
    match trigger {
        Trigger::AppleEvent => command.args(args),
        Trigger::ProbeScript(script) => {
            std::fs::write(&script_path, script).unwrap();
            command
                .args(["--isolated-dev", "--probe-script"])
                .arg(&script_path)
                .args(["--service-capacity", "1"])
        }
    };
    let mut child = command
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let pid = child.id();
    let (tx, rx) = mpsc::channel::<(bool, String)>();
    let out = child.stdout.take().unwrap();
    let out_tx = tx.clone();
    let out_reader = std::thread::spawn(move || {
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            if out_tx.send((true, line)).is_err() {
                return;
            }
        }
    });
    let err = child.stderr.take().unwrap();
    let err_reader = std::thread::spawn(move || {
        for line in BufReader::new(err).lines().map_while(Result::ok) {
            if tx.send((false, line)).is_err() {
                return;
            }
        }
    });
    let kill_group = |child: &mut std::process::Child| {
        if child.try_wait().unwrap().is_none() {
            // The unreaped leader pins this exact group (`process_group(0)`).
            let group = format!("-{}", child.id());
            assert!(
                Command::new("/bin/kill")
                    .args(["-KILL", "--", &group])
                    .status()
                    .unwrap()
                    .success()
            );
            child.wait().unwrap();
        }
    };
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let drain = |stdout: &mut Vec<String>, stderr: &mut Vec<String>| {
        while let Ok((is_out, line)) = rx.try_recv() {
            if is_out {
                stdout.push(line)
            } else {
                stderr.push(line)
            }
        }
    };
    let ready_deadline = Instant::now() + budget(BudgetId::MemberRestartGrace);
    // A probe-script Quit can complete before this thread reads the ready line (the shell
    // exits on its own), so that trigger waits for the exit below and checks the line after.
    while matches!(trigger, Trigger::AppleEvent) && !stdout.iter().any(|l| l == "L11_NATIVE_READY")
    {
        drain(&mut stdout, &mut stderr);
        if child.try_wait().unwrap().is_some() || Instant::now() >= ready_deadline {
            kill_group(&mut child);
            panic!("shell not ready; stdout {stdout:?}; stderr {stderr:?}");
        }
        std::thread::sleep(budget(BudgetId::OwnerPoll));
    }
    let quit_sent = Instant::now();
    if matches!(trigger, Trigger::AppleEvent) {
        send_quit(pid);
    }
    let exit_deadline = quit_sent + exit_budget;
    let mut quit_to_exit = None;
    let status = loop {
        drain(&mut stdout, &mut stderr);
        if let Some(status) = child.try_wait().unwrap() {
            quit_to_exit = Some(quit_sent.elapsed());
            break Some(status);
        }
        if Instant::now() >= exit_deadline {
            kill_group(&mut child);
            break None;
        }
        std::thread::sleep(budget(BudgetId::OwnerPoll));
    };
    out_reader.join().unwrap();
    err_reader.join().unwrap();
    drain(&mut stdout, &mut stderr);
    std::fs::remove_dir_all(&root).unwrap();
    Run {
        quit_to_exit,
        code: status.and_then(|s| s.code()),
        stdout,
        stderr,
    }
}

#[test]
#[ignore = "explicit own-development-build GUI gate; no installed application"]
fn quit_with_unproven_cleanup_exits_3_with_the_v014_event() {
    let run = quit_run(&["--isolated-dev", "--unproven-cleanup"]);
    assert!(
        run.stdout
            .iter()
            .any(|l| l.starts_with("L11_CLEANUP:Failed(")),
        "cleanup must be reported as unproven: {:?} / {:?}",
        run.stdout,
        run.stderr
    );
    assert_eq!(
        run.code,
        Some(3),
        "v0.1.4 exits 3 on Quit with unproven cleanup (None = still running at the exit budget): {:?} / {:?}",
        run.stdout,
        run.stderr
    );
    assert_eq!(
        run.stderr
            .iter()
            .filter(|l| l.contains("launcher_cleanup_failed"))
            .collect::<Vec<_>>(),
        vec![V014_EVENT],
        "{:?}",
        run.stderr
    );
}

#[test]
#[ignore = "explicit own-development-build GUI gate; no installed application"]
fn quit_with_proven_cleanup_exits_0_and_prints_no_failure_event() {
    let run = quit_run(&["--isolated-dev"]);
    assert!(
        run.stdout.iter().any(|l| l == "L11_CLEANUP:Verified"),
        "{:?} / {:?}",
        run.stdout,
        run.stderr
    );
    assert_eq!(run.code, Some(0), "{:?} / {:?}", run.stdout, run.stderr);
    assert!(
        !run.stderr
            .iter()
            .any(|l| l.contains("launcher_cleanup_failed")),
        "{:?}",
        run.stderr
    );
}

/// RU-032 / SE-SLICE1-L11-QUIT-JOIN-60 (strict port, RU-001 option a). v0.1.4's `close()` is
/// `run.stop(); run.join(60)` (`demo_app.py:202-203 @ f4e366e9`): it waits the L1 id
/// `PyMyceliumA13MacosDemoAppCloseRunJoinL203` for the teardown and then carries on and exits 0
/// although the teardown is still running. Executed `SLICE-1-evidence/r10-oracle/` q4: exit 0 at
/// 60.01 s, stderr empty, `run_state_after` "stopping" (teardown held 75 s). A cleanup that never
/// finishes must therefore keep the dev shell up for the whole allowance (the old 4 s / 30 s
/// budgets exited 3 early), then exit 0 with no `launcher_cleanup_failed` line.
#[test]
#[ignore = "explicit own-development-build GUI gate (~65 s); no installed application"]
fn quit_with_a_teardown_that_outlasts_the_join_exits_0_after_the_60s_allowance() {
    let allowance = budget(BudgetId::PyMyceliumA13MacosDemoAppCloseRunJoinL203);
    let run = quit_run_within(
        &["--isolated-dev", "--slow-cleanup"],
        allowance + budget(BudgetId::MemberRestartGrace),
    );
    let elapsed = run.quit_to_exit.unwrap_or_else(|| {
        panic!(
            "never exited within allowance + grace: {:?} / {:?}",
            run.stdout, run.stderr
        )
    });
    println!(
        "quit_to_exit_ms={} allowance_ms={}",
        elapsed.as_millis(),
        allowance.as_millis()
    );
    assert!(
        elapsed >= allowance,
        "Quit left after {elapsed:?}, before the {allowance:?} join allowance: {:?} / {:?}",
        run.stdout,
        run.stderr
    );
    assert_eq!(run.code, Some(0), "{:?} / {:?}", run.stdout, run.stderr);
    assert!(
        run.stdout
            .iter()
            .any(|l| l == "L11_CLEANUP:Failed(Deadline)"),
        "the unfinished teardown is reported, never claimed verified: {:?}",
        run.stdout
    );
    assert!(
        !run.stdout.iter().any(|l| l == "L11_CLEANUP:Verified"),
        "{:?}",
        run.stdout
    );
    assert!(
        run.stderr.is_empty(),
        "v0.1.4 prints nothing on this exit (q4 stderr []): {:?}",
        run.stderr
    );
}

/// CL-L11-04, the variants the exact-pid Apple Event gates do not reach: the "Quit Mycelium" menu
/// item and Command-Q go through the SAME single bounded cleanup (one `begin`, one verified
/// receipt, exit 0, nothing on stderr) as the Apple Event / Dock Quit and the page's own Quit.
/// The dispatch is real AppKit (`performActionForItemAtIndex:` for the menu; a Command-Q key-down
/// `NSEvent` through the main menu's `performKeyEquivalent:`, which only succeeds when the item's
/// accelerator is Command-Q). Own development build only; no installed application.
fn quit_variants(route: &str, expected: &[&str]) -> Run {
    let script = format!(
        "window.addEventListener('load', async () => {{ await fetch('/l11-quit/{route}'); }});"
    );
    let run = quit_run_with(
        &["--isolated-dev"],
        &Trigger::ProbeScript(&script),
        budget(BudgetId::StageCleanup),
    );
    assert!(
        run.stdout.iter().any(|l| l == "L11_NATIVE_READY"),
        "{:?}",
        run.stdout
    );
    for accepted in expected {
        assert!(
            run.stdout.iter().any(|l| l == accepted),
            "AppKit did not take the Quit ({accepted}): {:?} / {:?}",
            run.stdout,
            run.stderr
        );
    }
    let count = |needle: &str| run.stdout.iter().filter(|l| *l == needle).count();
    assert_eq!(
        count("L11_FAKE_CLEANUP_BEGIN"),
        1,
        "one shared cleanup, whichever source asked first or again: {:?} / {:?}",
        run.stdout,
        run.stderr
    );
    assert_eq!(count("L11_CLEANUP:Verified"), 1, "{:?}", run.stdout);
    assert_eq!(run.code, Some(0), "{:?} / {:?}", run.stdout, run.stderr);
    assert!(run.stderr.is_empty(), "{:?}", run.stderr);
    run
}

#[test]
#[ignore = "explicit own-development-build GUI gate; no installed application"]
fn menu_quit_runs_the_one_bounded_cleanup_and_exits_0() {
    quit_variants("menu", &["L11_QUIT_VARIANT:menu:performed"]);
}

#[test]
#[ignore = "explicit own-development-build GUI gate; no installed application"]
fn command_q_runs_the_one_bounded_cleanup_and_exits_0() {
    quit_variants("command-q", &["L11_QUIT_VARIANT:command-q:handled"]);
}

#[test]
#[ignore = "explicit own-development-build GUI gate; no installed application"]
fn menu_then_command_q_share_one_cleanup() {
    quit_variants(
        "menu-then-command-q",
        &[
            "L11_QUIT_VARIANT:menu:performed",
            "L11_QUIT_VARIANT:command-q:handled",
        ],
    );
}
