// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit GUI qualification; only the freshly built lane development binary.
#![cfg(all(feature = "dev-shell", target_os = "macos"))]
#![allow(unsafe_code)]
use budgets::{BudgetId, Entries};
use objc2::{ClassType, msg_send, rc::Retained};
use objc2_foundation::{NSAppleEventDescriptor, NSError};
use std::{
    io::{BufRead, BufReader},
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};
fn budget(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn assert_memory_pressure_normal() {
    let output = Command::new("/usr/sbin/sysctl")
        .args(["-n", "kern.memorystatus_vm_pressure_level"])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        "1",
        "public demo memory safety gate"
    );
}
fn send_quit(pid: u32) {
    let pid = i32::try_from(pid).unwrap();
    // SAFETY: NSAppleEventDescriptor APIs with SDK ABI widths. kpid addresses
    // ONLY our unreaped child, never an application name or installed bundle.
    unsafe {
        let target: Retained<NSAppleEventDescriptor> = msg_send![NSAppleEventDescriptor::class(),
            descriptorWithDescriptorType: u32::from_be_bytes(*b"kpid"), bytes: (&pid as *const i32).cast::<std::ffi::c_void>(), length: std::mem::size_of::<i32>()];
        let event: Retained<NSAppleEventDescriptor> = msg_send![NSAppleEventDescriptor::class(),
            appleEventWithEventClass: u32::from_be_bytes(*b"aevt"), eventID: u32::from_be_bytes(*b"quit"),
            targetDescriptor: &*target, returnID: -1i16, transactionID: 0i32];
        let mut error: *mut NSError = std::ptr::null_mut();
        let reply: Option<Retained<NSAppleEventDescriptor>> = msg_send![&*event,
            sendEventWithOptions: 3usize, timeout: budget(BudgetId::RouterClose).as_secs_f64(), error: &mut error];
        assert!(
            reply.is_some(),
            "exact-PID AppleEvent failed (no raw NSError logged)"
        );
    }
}
#[test]
#[ignore = "explicit own-development-build GUI gate; no installed application"]
fn c1_native_ready_real_webkit_fetch_then_exact_pid_appleevent_cleanup() {
    native_smoke(false);
}
#[test]
#[ignore = "explicit own-development-build GUI gate; no installed application"]
fn custom_composition_probe_exercises_chat_stop_and_web_quit() {
    native_smoke(true);
}
fn native_smoke(custom: bool) {
    assert_memory_pressure_normal();
    let root = std::env::temp_dir().join(format!(
        "v02-l11-native-smoke-{}-{custom}",
        std::process::id()
    ));
    std::fs::create_dir(&root).unwrap();
    let bundle = root.join("Relocated L11 Development.app");
    std::fs::create_dir_all(bundle.join("Contents/MacOS")).unwrap();
    let executable = bundle.join("Contents/MacOS/mycelium-desktop-dev");
    std::fs::copy(env!("CARGO_BIN_EXE_mycelium-desktop-dev"), &executable).unwrap();
    std::fs::write(bundle.join("Contents/Info.plist"), b"<?xml version=\"1.0\"?><!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\"><plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>mycelium-desktop-dev</string><key>CFBundleIdentifier</key><string>org.mycelium.l11-development</string><key>CFBundleName</key><string>Mycelium L11 Development</string><key>CFBundlePackageType</key><string>APPL</string><key>LSMinimumSystemVersion</key><string>15.6.1</string></dict></plist>").unwrap();
    let mut command = Command::new(&executable);
    if custom {
        let script = root.join("composition-probe.js");
        std::fs::write(&script, include_str!("fixtures/composition_probe.js")).unwrap();
        command
            .args(["--isolated-dev", "--probe-script"])
            .arg(script)
            .args(["--service-capacity", "1"]);
    } else {
        command.args(["--isolated-dev", "--probe", "--service-capacity", "1"]);
    }
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let (tx, rx) = mpsc::channel();
    let stdout = child.stdout.take().unwrap();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let line = line.unwrap();
            if tx.send(line).is_err() {
                return;
            }
        }
    });
    let test = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let mut ready = false;
        let mut fetched = false;
        let mut inactive_policy = false;
        let mut security_receipts = std::collections::BTreeSet::new();
        let mut browser_opens = 0;
        let deadline = Instant::now() + budget(BudgetId::MemberRestartGrace);
        while !(ready && fetched) {
            let line = rx
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .expect("native readiness/fetch timed out");
            println!("{line}");
            ready |= line == "L11_NATIVE_READY";
            fetched |= line == "L11_NATIVE_FETCH_OK";
            inactive_policy |= line == "L11_WEBVIEW_INACTIVE_POLICY:Throttle";
            browser_opens += usize::from(line == "L11_SYSTEM_BROWSER_CAPTURE");
            if line.starts_with("L11_FRAME_")
                || line.starts_with("L11_NAV_")
                || line.starts_with("L11_POPUP_")
                || line.starts_with("L11_CHAT_HEADER_")
                || line == "L11_CUSTOM_PROBE_OK"
                || line == "L11_LOCAL_CHAT_OK"
            {
                security_receipts.insert(line.clone());
            }
            assert!(
                (!line.starts_with("L11_NATIVE_FETCH_") || line == "L11_NATIVE_FETCH_OK")
                    && !line.ends_with("FAILED")
                    && !line.ends_with("EXCEPTION"),
                "{line}"
            );
        }
        assert_eq!(
            security_receipts,
            if custom {
                vec!["L11_CUSTOM_PROBE_OK"]
            } else {
                vec![
                    "L11_FRAME_SAME_PATH_OK",
                    "L11_FRAME_CHAT_OK",
                    "L11_FRAME_DIFFERENT_PATH_OK",
                    "L11_FRAME_REMOTE_OK",
                    "L11_FRAME_SRCDOC_OK",
                    "L11_FRAME_DATA_OK",
                    "L11_FRAME_BLOB_OK",
                    "L11_NAV_DIFFERENT_PATH_OK",
                    "L11_NAV_DATA_OK",
                    "L11_NAV_BLOB_OK",
                    "L11_NAV_EXTERNAL_OK",
                    "L11_FRAME_REDIRECT_OK",
                    "L11_NAV_REDIRECT_OK",
                    "L11_POPUP_EXTERNAL_OK",
                    "L11_POPUP_DATA_OK",
                    "L11_POPUP_BLOB_OK",
                    "L11_POPUP_DIFFERENT_PATH_OK",
                    "L11_POPUP_CHAT_API_OK",
                    "L11_CHAT_HEADER_ROOT_OK",
                    "L11_CHAT_HEADER_CHAT_OK",
                    "L11_LOCAL_CHAT_OK",
                ]
            }
            .into_iter()
            .map(String::from)
            .collect()
        );
        assert_eq!(browser_opens, if custom { 0 } else { 2 });
        // An occluded window must not suspend the page: a held chat request would never be
        // answered. The shell reads the applied policy back from the live web view.
        assert!(
            inactive_policy,
            "web view inactive scheduling policy not Throttle"
        );
        assert_memory_pressure_normal();
        if !custom {
            send_quit(child.id());
        }
        let deadline = Instant::now() + budget(BudgetId::CancelAndCleanup);
        let mut begins = 0;
        let mut verified = false;
        loop {
            while let Ok(line) = rx.try_recv() {
                println!("{line}");
                begins += usize::from(line == "L11_FAKE_CLEANUP_BEGIN");
                verified |= line == "L11_CLEANUP:Verified";
            }
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            assert!(Instant::now() < deadline, "owned process did not exit");
            std::thread::sleep(budget(BudgetId::OwnerPoll));
        }
        reader.join().unwrap();
        for line in rx.try_iter() {
            begins += usize::from(line == "L11_FAKE_CLEANUP_BEGIN");
            verified |= line == "L11_CLEANUP:Verified";
        }
        assert_eq!(begins, 1);
        assert!(verified);
    }));
    if let Err(panic) = test {
        if child.try_wait().unwrap().is_none() {
            child.kill().unwrap();
            child.wait().unwrap();
        }
        use std::io::Read;
        let mut stderr = String::new();
        child
            .stderr
            .take()
            .unwrap()
            .read_to_string(&mut stderr)
            .unwrap();
        eprintln!("owned dev stderr: {stderr}");
        std::fs::remove_dir_all(&root).unwrap();
        std::panic::resume_unwind(panic);
    }
    std::fs::remove_dir_all(&root).unwrap();
}
