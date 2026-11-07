// SPDX-License-Identifier: AGPL-3.0-only
//! CL-L11-03 on the DEVELOPMENT build only (the packaged bundle is not rebuilt or launched this
//! round): the app is started and re-opened through LaunchServices (`/usr/bin/open`, the call
//! behind a Finder double-click and a Dock click), not by exec'ing the binary. What this shows:
//! launch through LaunchServices with Finder-like process conditions (no tty, cwd `/`, stdout
//! and stderr to files, minimal environment) reaches native ready with a visible window;
//! re-opening the running app brings back a hidden window in the SAME process (no second
//! instance); the exact-pid Apple Event Quit then runs the one verified cleanup.
//! What it does not show: Finder itself, Gatekeeper/quarantine, the packaged bundle's
//! `Info.plist`. The dev binary insists on `--isolated-dev`, so the arguments differ from Finder's
//! (none); `open --args` passes them.
#![cfg(all(feature = "dev-shell", target_os = "macos"))]
mod support;
use budgets::BudgetId;
use std::{
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};
use support::{budget, front_pid, pressure_level, send_quit};

struct Launched {
    root: PathBuf,
    app: PathBuf,
    bundle_id: String,
    pid: Option<u32>,
}
impl Drop for Launched {
    fn drop(&mut self) {
        // Exact pid only (found through our unique bundle identifier), and only if still alive.
        if let Some(pid) = self.pid
            && alive(pid)
            && let Err(e) = Command::new("/bin/kill")
                .args(["-KILL", &pid.to_string()])
                .status()
        {
            eprintln!("kill of the launched pid failed: {e}");
        }
        if let Err(e) = std::fs::remove_dir_all(&self.root) {
            eprintln!("scratch cleanup failed: {e}");
        }
    }
}
fn alive(pid: u32) -> bool {
    Command::new("/bin/kill")
        .args(["-0", &pid.to_string()])
        .output()
        .is_ok_and(|o| o.status.success())
}
fn open(args: &[&std::ffi::OsStr]) {
    let output = Command::new("/usr/bin/open").args(args).output().unwrap();
    assert!(
        output.status.success(),
        "open failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}
fn wait_for(file: &Path, line: &str, within: Duration) -> Vec<String> {
    let deadline = Instant::now() + within;
    loop {
        let text = std::fs::read_to_string(file).unwrap_or_default();
        let lines: Vec<String> = text.lines().map(str::to_owned).collect();
        if lines.iter().any(|l| l == line) {
            return lines;
        }
        assert!(
            Instant::now() < deadline,
            "no {line:?} within {within:?}; got {lines:?}"
        );
        std::thread::sleep(budget(BudgetId::OwnerPoll));
    }
}
fn instances(executable: &Path) -> usize {
    let output = Command::new("/bin/ps")
        .args(["-axo", "command="])
        .output()
        .unwrap();
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|l| l.starts_with(&*executable.to_string_lossy()))
        .count()
}

#[test]
#[ignore = "explicit own-development-build GUI gate via LaunchServices; no installed application"]
fn launchservices_launch_reopen_and_quit_of_the_development_build() {
    assert_eq!(pressure_level(), "1", "memory safety gate before launch");
    let root = std::env::temp_dir()
        .canonicalize()
        .unwrap()
        .join(format!("v02-l11-finder-{}", std::process::id()));
    let bundle_id = format!("org.mycelium.l11-development-finder-{}", std::process::id());
    let app = root.join("Relocated L11 Finder.app");
    let executable = app.join("Contents/MacOS/mycelium-desktop-dev");
    std::fs::create_dir_all(app.join("Contents/MacOS")).unwrap();
    std::fs::copy(env!("CARGO_BIN_EXE_mycelium-desktop-dev"), &executable).unwrap();
    std::fs::write(
        app.join("Contents/Info.plist"),
        format!(
            "<?xml version=\"1.0\"?><!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\"><plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>mycelium-desktop-dev</string><key>CFBundleIdentifier</key><string>{bundle_id}</string><key>CFBundleName</key><string>Mycelium L11 Finder</string><key>CFBundlePackageType</key><string>APPL</string><key>LSMinimumSystemVersion</key><string>15.6.1</string></dict></plist>"
        ),
    )
    .unwrap();
    let script = root.join("hide-at-load.js");
    std::fs::write(
        &script,
        "window.addEventListener('load', async () => { await fetch('/l11-window/hide'); });",
    )
    .unwrap();
    let (out, err) = (root.join("stdout.txt"), root.join("stderr.txt"));
    let mut launched = Launched {
        root: root.clone(),
        app: app.clone(),
        bundle_id: bundle_id.clone(),
        pid: None,
    };
    assert!(launched.app.exists() && !launched.bundle_id.is_empty());

    // 1. Launch through LaunchServices, in the background (`-g`): no focus is taken.
    let (stdout_flag, stderr_flag, background, args_flag) = (
        std::ffi::OsStr::new("--stdout"),
        std::ffi::OsStr::new("--stderr"),
        std::ffi::OsStr::new("-g"),
        std::ffi::OsStr::new("--args"),
    );
    open(&[
        background,
        stdout_flag,
        out.as_os_str(),
        stderr_flag,
        err.as_os_str(),
        app.as_os_str(),
        args_flag,
        "--isolated-dev".as_ref(),
        "--probe-script".as_ref(),
        script.as_os_str(),
        "--service-capacity".as_ref(),
        "1".as_ref(),
    ]);
    let lines = wait_for(
        &out,
        "L11_NATIVE_READY",
        budget(BudgetId::MemberRestartGrace),
    );
    assert!(
        lines.iter().any(|l| l == "L11_WINDOW_VISIBLE:true"),
        "{lines:?}"
    );
    let lines = wait_for(
        &out,
        "L11_WINDOW_HIDDEN:visible=false",
        budget(BudgetId::MemberRestartGrace),
    );
    let asn = String::from_utf8(
        Command::new("/usr/bin/lsappinfo")
            .args(["find", &format!("bundleid={bundle_id}")])
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap();
    let pid: u32 = front_pid(asn.trim()).parse().unwrap_or_else(|_| {
        panic!("LaunchServices does not list {bundle_id}: {asn:?} / {lines:?}")
    });
    launched.pid = Some(pid);
    assert_eq!(
        instances(&executable),
        1,
        "exactly one instance after launch"
    );

    // 2. Re-open the running app: LaunchServices sends the reopen Apple Event to the SAME
    //    process, whose hidden window comes back.
    open(&[background, app.as_os_str()]);
    let lines = wait_for(
        &out,
        "L11_REOPEN:had_visible_windows=false:visible=true",
        budget(BudgetId::MemberRestartGrace),
    );
    assert_eq!(
        instances(&executable),
        1,
        "reopen must not start a second instance: {lines:?}"
    );
    assert!(alive(pid), "the reopened process is the launched one");
    // 3. A second re-open with the window already visible keeps it visible, still one process.
    open(&[background, app.as_os_str()]);
    wait_for(
        &out,
        "L11_REOPEN:had_visible_windows=true:visible=true",
        budget(BudgetId::MemberRestartGrace),
    );
    assert_eq!(instances(&executable), 1);
    assert!(alive(pid));

    // 4. Quit through the exact-pid Apple Event: the one cleanup, then the process is gone.
    send_quit(pid);
    let lines = wait_for(&out, "L11_CLEANUP:Verified", budget(BudgetId::StageCleanup));
    let gone = Instant::now() + budget(BudgetId::StageCleanup);
    while alive(pid) && Instant::now() < gone {
        std::thread::sleep(budget(BudgetId::OwnerPoll));
    }
    assert!(
        !alive(pid),
        "process still running after Verified: {lines:?}"
    );
    assert_eq!(instances(&executable), 0);
    assert_eq!(
        std::fs::read_to_string(&err).unwrap_or_default(),
        "",
        "nothing on stderr"
    );
    assert_eq!(
        lines
            .iter()
            .filter(|l| *l == "L11_FAKE_CLEANUP_BEGIN")
            .count(),
        1
    );
}
