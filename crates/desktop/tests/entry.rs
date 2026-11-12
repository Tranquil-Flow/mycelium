// SPDX-License-Identifier: AGPL-3.0-only
//! Entry routing and the installer command line (`desktop::entry`), ported from
//! `mycelium_a13_macos/__main__.py::main` and `installer_core.cli_parser/emit` (f4e366e9),
//! against scratch roots only (explicit `Roots::new`; never the real home).
use desktop::{
    docs::{self, FinalizeSpec, Signer},
    entry::{self, Route},
    install::{self, Observed, Roots, Seams},
};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
};

static SEQ: AtomicUsize = AtomicUsize::new(0);
fn scratch() -> PathBuf {
    let p = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "v02-l11-entry-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir_all(p.join("home")).unwrap();
    fs::create_dir_all(p.join("apps")).unwrap();
    p
}
fn args(list: &[&str]) -> Vec<String> {
    list.iter().map(|s| (*s).to_string()).collect()
}
fn seams() -> Seams {
    let mut seams = Seams::host();
    seams.observe = Box::new(|| Observed {
        system: "Darwin".into(),
        machine: "arm64".into(),
        macos_version: "26.5.1".into(),
    });
    seams.now = Box::new(|| 1_790_000_100.0);
    seams.clear_quarantine = Box::new(|_| true);
    seams
}
fn bundle(dir: &Path) -> PathBuf {
    let b = dir.join("download/mycelium-0.2.0-macos-arm64");
    let exe = b.join("Mycelium.app/Contents/MacOS/mycelium");
    fs::create_dir_all(exe.parent().unwrap()).unwrap();
    fs::write(&exe, b"inert").unwrap();
    docs::finalize(
        &b,
        &Signer::from_seed([0x55; 32], docs::SIGNER_ENDPOINT_ID),
        &FinalizeSpec {
            version: "0.2.0",
            rollback_floor: "0.1.0",
            source_revision: "0123456789abcdef0123456789abcdef01234567",
            issued_at: 1_790_000_000.0,
            minimum_os: "15.6.1",
        },
    )
    .unwrap();
    b
}
fn installed_exe(home: &Path) -> PathBuf {
    let exe = home.join("Library/Application Support/org.mycelium.app/programs/0.2.0/Mycelium.app/Contents/MacOS/mycelium");
    fs::create_dir_all(exe.parent().unwrap()).unwrap();
    fs::write(&exe, b"inert").unwrap();
    exe
}

#[test]
fn routing_follows_v014_main() {
    let dir = scratch();
    let home = dir.join("home");
    let downloaded = bundle(&dir).join("Mycelium.app/Contents/MacOS/mycelium");
    // Bare (and Finder's -psn) outside the install: refused, whatever else is true.
    for argv in [args(&["mycelium"]), args(&["mycelium", "-psn_0_1"])] {
        let Route::Refuse(refusal) = entry::route(&argv, &downloaded, &home) else {
            panic!("a bare launch from the download must refuse");
        };
        assert_eq!(refusal.code, "not_installed");
        let event: serde_json::Value =
            serde_json::from_str(&entry::refusal_event(&refusal)).unwrap();
        assert_eq!(event["event"], "launcher_refused");
        assert_eq!(event["location"], "elsewhere");
        assert!(
            !event.to_string().contains(&dir.display().to_string()),
            "no path in the event"
        );
    }
    let translocated = PathBuf::from(
        "/private/var/folders/x/AppTranslocation/1/d/Mycelium.app/Contents/MacOS/mycelium",
    );
    let Route::Refuse(refusal) = entry::route(&args(&["mycelium"]), &translocated, &home) else {
        panic!("translocated must refuse");
    };
    assert!(entry::refusal_event(&refusal).contains("\"translocated\""));
    // From the install: starts the app.
    let inside = installed_exe(&home);
    assert!(matches!(
        entry::route(&args(&["mycelium"]), &inside, &home),
        Route::Run
    ));
    // Explicit flags keep their meaning wherever they run from.
    for flag in entry::INSTALLER_ACTIONS {
        assert!(
            matches!(
                entry::route(&args(&["mycelium", flag]), &downloaded, &home),
                Route::Installer
            ),
            "{flag}"
        );
    }
    assert!(matches!(
        entry::route(&args(&["mycelium", "--headless"]), &downloaded, &home),
        Route::Reject
    ));
    assert!(matches!(
        entry::route(&args(&["mycelium", "extra"]), &inside, &home),
        Route::Reject
    ));
}

#[test]
fn the_refusal_names_the_installed_copy_when_there_is_one() {
    let dir = scratch();
    let home = dir.join("home");
    let downloaded = bundle(&dir).join("Mycelium.app/Contents/MacOS/mycelium");
    let inside = installed_exe(&home);
    let apps = home.join("Applications");
    fs::create_dir_all(&apps).unwrap();
    std::os::unix::fs::symlink(
        inside.ancestors().nth(3).unwrap(),
        apps.join("Mycelium.app"),
    )
    .unwrap();
    let Route::Refuse(refusal) = entry::route(&args(&["mycelium"]), &downloaded, &home) else {
        panic!("refuse");
    };
    assert!(
        refusal.message.starts_with("This is the downloaded copy."),
        "{}",
        refusal.message
    );
}

#[test]
fn installer_flags_run_install_status_uninstall_with_v014_output_and_defaults() {
    let dir = scratch();
    let roots = Roots::new(&dir.join("home"), &dir.join("apps")).unwrap();
    let b = bundle(&dir);
    // `--install` with no --bundle: the folder above the app, as `Path(sys.executable).parents[3]`.
    let exe = b.join("Mycelium.app/Contents/MacOS/mycelium");
    let done = entry::run_installer(&args(&["mycelium", "--install"]), &exe, &roots, &seams());
    assert_eq!(done.code, 0, "{}", done.stderr);
    let report: serde_json::Value = serde_json::from_str(&done.stdout).unwrap();
    assert_eq!(report["action"], "install");
    assert_eq!(report["version"], "0.2.0");
    let status = entry::run_installer(
        &args(&["mycelium", "--install-status"]),
        &exe,
        &roots,
        &seams(),
    );
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&status.stdout).unwrap()["current"],
        "0.2.0"
    );
    let gone = entry::run_installer(&args(&["mycelium", "--uninstall"]), &exe, &roots, &seams());
    assert_eq!(gone.code, 0, "{}", gone.stderr);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&gone.stdout).unwrap()["state"],
        "retained"
    );
    assert!(roots.state().is_dir());
    let wiped = entry::run_installer(
        &args(&["mycelium", "--uninstall", "--delete-identity-and-state"]),
        &exe,
        &roots,
        &seams(),
    );
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&wiped.stdout).unwrap()["state"],
        "deleted"
    );
}

#[test]
fn refusals_are_one_structured_line_with_the_plain_language_message_where_v014_has_one() {
    let dir = scratch();
    let roots = Roots::new(&dir.join("home"), &dir.join("apps")).unwrap();
    let exe = dir.join("not-in-a-bundle/mycelium");
    // Rollback with nothing installed: code only (v0.1.4 has no message for it).
    let done = entry::run_installer(&args(&["mycelium", "--rollback"]), &exe, &roots, &seams());
    assert_eq!((done.code, done.stdout.as_str()), (2, ""));
    let line: serde_json::Value = serde_json::from_str(&done.stderr).unwrap();
    assert_eq!(
        line,
        serde_json::json!({"protocol": install::PROTOCOL, "refused": "no_previous_version"})
    );
    assert_eq!(done.stderr.lines().count(), 1);
    // A damaged record: code plus the message.
    let programs = roots.programs();
    fs::create_dir_all(&programs).unwrap();
    fs::set_permissions(
        &programs,
        std::os::unix::fs::PermissionsExt::from_mode(0o700),
    )
    .unwrap();
    fs::write(programs.join("versions.json"), b"{{").unwrap();
    fs::set_permissions(
        programs.join("versions.json"),
        std::os::unix::fs::PermissionsExt::from_mode(0o600),
    )
    .unwrap();
    let done = entry::run_installer(
        &args(&["mycelium", "--install-status"]),
        &exe,
        &roots,
        &seams(),
    );
    let line: serde_json::Value = serde_json::from_str(&done.stderr).unwrap();
    assert_eq!(line["refused"], "installation_record_invalid");
    assert_eq!(
        line["message"],
        "The record of installed versions is damaged. Uninstall, then install again."
    );
}

#[test]
fn bad_command_lines_exit_two_and_change_nothing() {
    let dir = scratch();
    let roots = Roots::new(&dir.join("home"), &dir.join("apps")).unwrap();
    let exe = dir.join("x/mycelium");
    for argv in [
        args(&["mycelium", "--install", "--rollback"]),
        args(&["mycelium", "--bundle"]),
        args(&["mycelium", "--delete-identity-and-state"]),
        args(&["mycelium", "--install-status", "--nope"]),
    ] {
        let before: Vec<_> = fs::read_dir(dir.join("home"))
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .collect();
        let done = entry::run_installer(&argv, &exe, &roots, &seams());
        assert_eq!(done.code, 2, "{argv:?}");
        assert!(
            done.stdout.is_empty() && done.stderr.contains("usage:"),
            "{argv:?}"
        );
        let after: Vec<_> = fs::read_dir(dir.join("home"))
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .collect();
        assert_eq!(before, after);
    }
}

#[test]
fn the_product_roots_are_home_and_home_applications() {
    // `for_current_user` is the app's own entry point (HOME); it must exist and be absolute.
    let roots = Roots::for_current_user().unwrap();
    let home = PathBuf::from(std::env::var("HOME").unwrap());
    assert_eq!(
        roots.programs(),
        home.join("Library/Application Support/org.mycelium.app/programs")
    );
    assert_eq!(
        roots.application_entry(),
        home.join("Applications/Mycelium.app")
    );
    // Read only: nothing is created or changed by asking.
}
