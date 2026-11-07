// SPDX-License-Identifier: AGPL-3.0-only
//! Shared helpers of the explicit bundle GUI gates (`bundle_smoke`, `bundle_chat`): budgets,
//! frontmost-app observation, the exact-pid Apple Event quit, and the scratch install.
#![allow(unsafe_code, dead_code)]
use budgets::{BudgetId, Entries};
use objc2::{ClassType, msg_send, rc::Retained};
use objc2_foundation::{NSAppleEventDescriptor, NSError};
use std::{
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};

pub fn budget(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
pub fn frontmost() -> String {
    let output = Command::new("/usr/bin/lsappinfo")
        .arg("front")
        .output()
        .unwrap();
    assert!(output.status.success());
    String::from_utf8(output.stdout).unwrap().trim().to_string()
}
pub fn front_pid(asn: &str) -> String {
    let output = Command::new("/usr/bin/lsappinfo")
        .args(["info", "-only", "pid", asn])
        .output()
        .unwrap();
    // `"pid"=1234` -> `1234`
    String::from_utf8_lossy(&output.stdout)
        .rsplit('=')
        .next()
        .unwrap_or("")
        .trim()
        .to_string()
}
/// Wait (bounded) until the frontmost app is `want` again; returns what was frontmost last.
pub fn front_restored(want: &str) -> String {
    let deadline = Instant::now() + budget(BudgetId::RouterClose);
    loop {
        let now = frontmost();
        if now == want || Instant::now() >= deadline {
            return now;
        }
        std::thread::sleep(budget(BudgetId::OwnerPoll));
    }
}
pub fn send_quit(pid: u32) {
    let pid = i32::try_from(pid).unwrap();
    // SAFETY: NSAppleEventDescriptor APIs with SDK ABI widths. kpid addresses ONLY our
    // unreaped child, never an application name or installed bundle.
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

/// A throw-away TEST-key install of a copy of `app` into a scratch fake root with the real
/// installer; returns the fake root (delete it when done), the fake `HOME`, and the INSTALLED
/// executable. Production signer and keys are never read.
pub fn install_scratch(app: &Path, label: &str) -> (PathBuf, PathBuf, PathBuf) {
    let fake = std::env::temp_dir()
        .canonicalize()
        .unwrap()
        .join(format!("{label}-{}", std::process::id()));
    let (fake_home, fake_apps, staged) =
        (fake.join("home"), fake.join("apps"), fake.join("bundle"));
    for dir in [&fake_home, &fake_apps, &staged] {
        std::fs::create_dir_all(dir).unwrap();
    }
    assert!(
        Command::new("/usr/bin/ditto")
            .arg(app)
            .arg(staged.join("Mycelium A13 Demo.app"))
            .status()
            .unwrap()
            .success()
    );
    let issued = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
        .floor();
    desktop::docs::finalize(
        &staged,
        &desktop::docs::Signer::from_seed([0x66; 32], desktop::docs::SIGNER_ENDPOINT_ID),
        &desktop::docs::FinalizeSpec {
            version: "0.2.0",
            rollback_floor: "0.1.0",
            source_revision: "0123456789abcdef0123456789abcdef01234567",
            issued_at: issued,
            minimum_os: "15.6.1",
        },
    )
    .unwrap();
    let roots = desktop::install::Roots::new(&fake_home, &fake_apps).unwrap();
    desktop::install::install(&staged, &roots, &desktop::install::Seams::host()).unwrap();
    let executable = roots
        .version_dir("0.2.0")
        .join("Mycelium A13 Demo.app/Contents/MacOS/mycelium")
        .canonicalize()
        .unwrap();
    (fake, fake_home, executable)
}
/// `kern.memorystatus_vm_pressure_level` ("1" = normal).
pub fn pressure_level() -> String {
    let output = Command::new("/usr/sbin/sysctl")
        .args(["-n", "kern.memorystatus_vm_pressure_level"])
        .output()
        .unwrap();
    String::from_utf8(output.stdout).unwrap().trim().to_string()
}
