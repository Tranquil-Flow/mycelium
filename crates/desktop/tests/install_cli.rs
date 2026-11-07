// SPDX-License-Identifier: AGPL-3.0-only
//! The `desktop-package` command end to end against a scratch fake install root: finalize (signed),
//! install, upgrade over a v0.1.4-shaped install, status, rollback, uninstall, with a seeded
//! v0.1.4-shaped state tree compared byte for byte after every step. The tool is run as a real
//! subprocess with the host seams (real `sw_vers`, real `xattr`, real file locks). The roots
//! are scratch directories passed explicitly; the real home and Applications are refused.
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};

static SEQ: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        let p = std::env::temp_dir().canonicalize().unwrap().join(format!(
            "v02-l11-cli-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&p).unwrap();
        fs::create_dir(p.join("home")).unwrap();
        Self(p)
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}
impl Drop for Tree {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn tool(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_desktop-package"))
        .args(args)
        .output()
        .unwrap()
}
fn json(output: &Output) -> serde_json::Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}
fn put(path: &Path, bytes: &[u8], mode: u32) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}
fn bundle(tree: &Tree, label: &str, version: &str, exe: &str) -> PathBuf {
    let dir = tree.path(label);
    let app = dir.join("Mycelium A13 Demo.app");
    put(
        &app.join("Contents/MacOS").join(exe),
        format!("exe {version}").as_bytes(),
        0o755,
    );
    put(&app.join("Contents/Info.plist"), b"<plist/>", 0o644);
    put(
        &app.join("Contents/Resources/x.bin"),
        version.as_bytes(),
        0o644,
    );
    symlink("x.bin", app.join("Contents/Resources/link.bin")).unwrap();
    // A throw-away TEST signing key (32 raw bytes, scratch only): never a production key.
    let key = tree.path("test-signing.key");
    fs::write(&key, [0x11u8; 32]).unwrap();
    let out = tool(&[
        "finalize",
        dir.to_str().unwrap(),
        version,
        "0.1.0",
        "0123456789abcdef0123456789abcdef01234567",
        key.to_str().unwrap(),
    ]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    dir
}
fn state_listing(root: &Path) -> String {
    let out = Command::new("/usr/bin/find")
        .arg(root)
        .args([
            "-type",
            "f",
            "-exec",
            "/usr/bin/shasum",
            "-a",
            "256",
            "{}",
            ";",
        ])
        .output()
        .unwrap();
    let mut lines: Vec<_> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(String::from)
        .collect();
    let times = Command::new("/usr/bin/find")
        .arg(root)
        .args(["-exec", "/usr/bin/stat", "-f", "%N %p %m", "{}", ";"])
        .output()
        .unwrap();
    lines.extend(
        String::from_utf8(times.stdout)
            .unwrap()
            .lines()
            .map(String::from),
    );
    lines.sort();
    lines.join("\n")
}

#[test]
fn finalize_install_upgrade_status_rollback_uninstall_keep_user_state_byte_for_byte() {
    let tree = Tree::new();
    let home = tree.path("home");
    let apps = tree.path("apps");
    let (home_arg, apps_arg) = (home.to_str().unwrap(), apps.to_str().unwrap());
    let state = home.join("Library/Application Support/Mycelium A13 macOS Demo");
    for (relative, mode) in [
        ("identity/sidecar.key", 0o600),
        ("authority.json", 0o600),
        ("membership.state.json", 0o600),
        ("run-model.json", 0o600),
        ("fast-mode.json", 0o600),
        ("seed/models/m/weights.bin", 0o644),
    ] {
        put(
            &state.join(relative),
            format!("synthetic {relative}").as_bytes(),
            mode,
        );
    }
    fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap(); // as v0.1.4's app leaves it
    let seeded = state_listing(&state);

    let old = bundle(&tree, "old", "0.1.4", "Mycelium A13 Demo");
    let new = bundle(&tree, "new", "0.2.0", "mycelium");

    let installed = json(&tool(&[
        "install",
        home_arg,
        apps_arg,
        old.to_str().unwrap(),
    ]));
    assert_eq!(
        (installed["action"].as_str(), installed["version"].as_str()),
        (Some("install"), Some("0.1.4"))
    );
    assert_eq!(state_listing(&state), seeded, "fresh install");

    let upgraded = json(&tool(&[
        "install",
        home_arg,
        apps_arg,
        new.to_str().unwrap(),
    ]));
    assert_eq!(upgraded["previous"], "0.1.4");
    assert_eq!(state_listing(&state), seeded, "upgrade");
    let status = json(&tool(&["status", home_arg, apps_arg]));
    assert_eq!(
        (status["current"].as_str(), status["previous"].as_str()),
        (Some("0.2.0"), Some("0.1.4"))
    );
    assert_eq!(
        fs::read_link(apps.join("Mycelium A13.app")).unwrap(),
        home.join("Library/Application Support/Mycelium/programs/0.2.0/Mycelium A13 Demo.app")
    );

    let rolled = json(&tool(&["rollback", home_arg, apps_arg]));
    assert_eq!(rolled["version"], "0.1.4");
    assert_eq!(state_listing(&state), seeded, "rollback");
    assert_eq!(
        json(&tool(&["status", home_arg, apps_arg]))["current"],
        "0.1.4"
    );

    let removed = json(&tool(&["uninstall", home_arg, apps_arg]));
    assert_eq!(removed["state"], "retained");
    assert_eq!(state_listing(&state), seeded, "uninstall");
    assert!(fs::symlink_metadata(apps.join("Mycelium A13.app")).is_err());
    assert_eq!(
        json(&tool(&["status", home_arg, apps_arg]))["installed"],
        false
    );

    let wiped = json(&tool(&[
        "uninstall",
        home_arg,
        apps_arg,
        "--delete-identity-and-state",
    ]));
    assert_eq!(wiped["state"], "deleted");
    assert!(!state.exists());
}

#[test]
fn refusals_are_one_structured_stderr_line_exit_two_and_never_touch_real_roots() {
    let tree = Tree::new();
    let (home, apps) = (tree.path("home"), tree.path("apps"));
    let real_home = std::env::var("HOME").unwrap();
    let before = state_listing(&tree.path("home"));

    let refused = tool(&["status", &real_home, apps.to_str().unwrap()]);
    assert_eq!(refused.status.code(), Some(2));
    let line: serde_json::Value = serde_json::from_slice(&refused.stderr).unwrap();
    assert_eq!(line["refused"], "install_root_is_real_home");
    assert_eq!(line["protocol"], "mycelium.a13_macos_installer.v1");
    assert!(refused.stdout.is_empty());

    let refused = tool(&["status", home.to_str().unwrap(), "/Applications"]);
    assert_eq!(refused.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&refused.stderr).contains("install_root_is_real_applications"));

    // Nothing to roll back to, and a bundle that was never described: refused, nothing written.
    let refused = tool(&["rollback", home.to_str().unwrap(), apps.to_str().unwrap()]);
    assert_eq!(refused.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&refused.stderr).contains("no_previous_version"));
    let bare = tree.path("bare");
    fs::create_dir(&bare).unwrap();
    let refused = tool(&[
        "install",
        home.to_str().unwrap(),
        apps.to_str().unwrap(),
        bare.to_str().unwrap(),
    ]);
    assert_eq!(refused.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&refused.stderr).contains("bundle_incomplete"));
    assert_eq!(state_listing(&tree.path("home")), before);

    // The usage text names every command; nothing defaults to the real home.
    let usage = tool(&["install"]);
    assert!(!usage.status.success());
    assert!(String::from_utf8_lossy(&usage.stderr).contains("usage:"));
}
