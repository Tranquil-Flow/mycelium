// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit launch-to-window smoke of a REAL built bundle (never in the default run):
//!
//! ```text
//! MYCELIUM_BUNDLE_APP="/path/Mycelium A13 Demo.app" cargo test --release --offline --locked -j 2 \
//!   -p desktop --features tauri-shell --test bundle_smoke -- --ignored --nocapture --test-threads=1
//! ```
//!
//! Executes ONLY `mycelium` executables of scratch copies (never `open`, never an application
//! name or bundle id, so the installed v0.1.4 app cannot be addressed). The named bundle is
//! first launched bare from where it is (a download: it must refuse, exit 2, no window), then
//! copied (ditto), finalized with a throw-away TEST key and installed into a scratch fake root
//! with the real installer; the INSTALLED executable is launched with `HOME` = the fake home, so
//! a bare launch is judged installed. Frontmost app is observed before/during/after and the app
//! ends with an Apple Event whose target is the exact child pid. The real home, /Applications
//! and ~/Applications are unreachable; nothing real is installed, signed or registered.
#![cfg(all(feature = "tauri-shell", target_os = "macos"))]
mod support;
use budgets::BudgetId;
use std::{
    io::{BufRead, BufReader},
    path::PathBuf,
    process::{Command, Stdio},
    sync::mpsc,
    time::Instant,
};

use support::{
    budget, front_pid, front_restored, frontmost, install_scratch, pressure_level, send_quit,
};

/// What the HOST does to the frontmost app when a plain, non-Mycelium regular app shows a
/// window (`tests/fixtures/focus_control.m`, no explicit activation request), launched the same
/// way as the bundle. On a session where the control takes focus too, launch focus is host
/// behaviour, not a property of the bundle, and the bundle is judged against the control.
struct Control {
    front_before: String,
    front_at_window: String,
    front_settled: String,
    front_settled_pid: String,
    control_pid: u32,
    front_after: String,
}
impl Control {
    fn host_activates_new_apps(&self) -> bool {
        self.front_settled != self.front_before
    }
}
fn run_control(scratch: &std::path::Path) -> Control {
    let exe = scratch.join("focus-control");
    assert!(
        Command::new("/usr/bin/clang")
            .args(["-fobjc-arc", "-framework", "Cocoa"])
            .arg(concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/tests/fixtures/focus_control.m"
            ))
            .arg("-o")
            .arg(&exe)
            .status()
            .unwrap()
            .success()
    );
    let front_before = frontmost();
    let mut child = Command::new(&exe)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let control_pid = child.id();
    let mut shown = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut shown)
        .unwrap();
    assert_eq!(shown.trim(), "CONTROL_WINDOW_SHOWN");
    let front_at_window = frontmost();
    std::thread::sleep(budget(BudgetId::OwnerPoll));
    let front_settled = frontmost();
    let front_settled_pid = front_pid(&front_settled);
    // Exact child only: the control is our own unreaped child.
    child.kill().unwrap();
    child.wait().unwrap();
    let front_after = front_restored(&front_before);
    Control {
        front_before,
        front_at_window,
        front_settled,
        front_settled_pid,
        control_pid,
        front_after,
    }
}
#[test]
#[ignore = "explicit GUI gate: executes the named scratch bundle's own executable briefly"]
fn built_bundle_launches_to_a_window_unfocused_and_exits_via_exact_pid_apple_event_quit() {
    let app = PathBuf::from(
        std::env::var("MYCELIUM_BUNDLE_APP").expect("MYCELIUM_BUNDLE_APP must name the built .app"),
    );
    assert!(
        app.starts_with(std::env::var("HOME").unwrap())
            && app.to_string_lossy().contains("/.hermes/cache/scratch/"),
        "only a lane scratch bundle may be launched, never an installed app: {}",
        app.display()
    );
    let downloaded = app.join("Contents/MacOS/mycelium");
    // 1. A bare launch of the download itself refuses (wired launch_location), alert off.
    let refused = Command::new(&downloaded)
        .env("HOME", std::env::temp_dir())
        .env("MYCELIUM_LAUNCH_ALERT", "off")
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(
        refused.status.code(),
        Some(2),
        "{}",
        String::from_utf8_lossy(&refused.stderr)
    );
    assert!(String::from_utf8_lossy(&refused.stderr).contains("\"code\":\"not_installed\""));
    assert!(!String::from_utf8_lossy(&refused.stdout).contains("L11_NATIVE_READY"));
    // 2. A scratch install (real installer, ditto copy, test-key signature) of a finalized copy.
    let (fake, fake_home, executable) = install_scratch(&app, "v02-l11-smoke");
    assert_eq!(pressure_level(), "1", "public demo memory safety gate");

    let control = run_control(&fake);
    assert_eq!(
        control.front_after, control.front_before,
        "the control must hand focus back so the bundle starts from the same frontmost app"
    );
    if control.host_activates_new_apps() {
        assert_eq!(
            control.front_settled_pid,
            control.control_pid.to_string(),
            "the control drifted to something other than itself: indeterminate host session"
        );
    }
    let front_before = frontmost();
    assert_eq!(
        front_before, control.front_before,
        "frontmost app changed under us"
    );
    let started = Instant::now();
    let mut child = Command::new(&executable)
        .env("HOME", &fake_home)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let pid = child.id();
    let (tx, rx) = mpsc::channel();
    let stdout = child.stdout.take().unwrap();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            if tx.send(line.unwrap()).is_err() {
                return;
            }
        }
    });
    let sampler_stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let sampler = {
        let stop = sampler_stop.clone();
        std::thread::spawn(move || {
            let begin = Instant::now();
            let mut changes: Vec<(u128, String)> = Vec::new();
            while !stop.load(std::sync::atomic::Ordering::Acquire) {
                let now = frontmost();
                if changes.last().is_none_or(|(_, last)| *last != now) {
                    changes.push((begin.elapsed().as_millis(), now));
                }
                std::thread::sleep(budget(BudgetId::RegistryPoll));
            }
            changes
        })
    };
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let comm = Command::new("/bin/ps")
            .args(["-p", &pid.to_string(), "-o", "comm="])
            .output()
            .unwrap();
        assert_eq!(
            String::from_utf8(comm.stdout).unwrap().trim(),
            executable.to_string_lossy()
        );
        let mut lines = Vec::new();
        let deadline = Instant::now() + budget(BudgetId::MemberRestartGrace);
        while !([
            "L11_NATIVE_READY",
            "L11_PAGE_LOADED",
            "L11_WINDOW_VISIBLE:true",
        ]
        .iter()
        .all(|r| lines.iter().any(|l| l == r)))
        {
            let line = rx
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .expect("window readiness receipts timed out");
            assert!(!line.contains("FAILED"), "{line}");
            lines.push(line);
        }
        let to_window = started.elapsed();
        let front_at_window = frontmost();
        let front_at_window_pid = front_pid(&front_at_window);
        // Settle one poll interval so a late activation would be visible.
        std::thread::sleep(budget(BudgetId::OwnerPoll));
        let front_settled = frontmost();
        let front_settled_pid = front_pid(&front_settled);
        send_quit(pid);
        let quit_sent = Instant::now();
        let deadline = Instant::now() + budget(BudgetId::CancelAndCleanup);
        let status = loop {
            while let Ok(line) = rx.try_recv() {
                lines.push(line);
            }
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            assert!(
                Instant::now() < deadline,
                "owned process did not exit after the exact-pid Apple Event"
            );
            std::thread::sleep(budget(BudgetId::OwnerPoll));
        };
        let quit_to_exit = quit_sent.elapsed();
        reader.join().unwrap();
        lines.extend(rx.try_iter());
        assert!(status.success(), "{status:?}");
        assert!(
            lines.iter().any(|l| l == "L11_CLEANUP:Verified"),
            "{lines:?}"
        );
        let front_after = front_restored(&front_before);
        sampler_stop.store(true, std::sync::atomic::Ordering::Release);
        let timeline = sampler.join().unwrap();
        println!(
            "BUNDLE_SMOKE {}",
            serde_json::json!({
                "pid": pid, "executable": executable, "to_window_ms": to_window.as_millis(),
                "quit_to_exit_ms": quit_to_exit.as_millis(), "exit_success": status.success(),
                "front_before": front_before, "front_at_window": front_at_window,
                "front_at_window_pid": front_at_window_pid,
                "front_settled": front_settled, "front_settled_pid": front_settled_pid,
                "front_after": front_after, "receipts": lines,
                "front_timeline_ms": timeline,
                "control": {
                    "host_activates_new_apps": control.host_activates_new_apps(),
                    "front_at_window": control.front_at_window,
                    "front_settled": control.front_settled,
                    "front_settled_pid": control.front_settled_pid,
                    "front_after": control.front_after,
                },
            })
        );
        if control.host_activates_new_apps() {
            // Host-session behaviour: even a plain app that never asks for activation becomes
            // frontmost here. The bundle is judged against that control: it may become
            // frontmost ONLY as itself, must not change again (no late activation by anything
            // else), and must hand the user's app back when it quits. Nothing is assumed
            // about the strict case below, which is the original assertion, unchanged.
            assert_eq!(
                front_at_window_pid,
                pid.to_string(),
                "frontmost during launch is neither the user's app nor the bundle itself"
            );
            assert_eq!(front_settled, front_at_window, "no late activation");
            assert_eq!(
                front_after, front_before,
                "the bundle did not hand focus back on quit, unlike the control"
            );
        } else {
            assert_eq!(
                front_at_window, front_before,
                "launch must not take focus from the user's frontmost app"
            );
            assert_eq!(front_settled, front_before, "no late activation");
        }
    }));
    if let Err(panic) = outcome {
        if child.try_wait().unwrap().is_none() {
            child.kill().unwrap();
            child.wait().unwrap();
        }
        sampler_stop.store(true, std::sync::atomic::Ordering::Release);
        if let Err(error) = std::fs::remove_dir_all(&fake) {
            eprintln!("cleanup_failed:{:?}", error.kind());
        }
        std::panic::resume_unwind(panic);
    }
    if let Err(error) = std::fs::remove_dir_all(&fake) {
        eprintln!("cleanup_failed:{:?}", error.kind());
    }
}
