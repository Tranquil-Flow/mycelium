// SPDX-License-Identifier: AGPL-3.0-only
//! Ad-hoc signing at the v0.1.4 trust level (CL-L11-06, lane REL item 2) on real Mach-O files
//! compiled here with clang (no network, no keychain, nothing installed): pinned code keeps its
//! bytes and its verified ad-hoc linker signature, the executable and the bundle are signed
//! ad-hoc, and `codesign --verify --deep --strict` passes on the result.
#![cfg(target_os = "macos")]
use desktop::{
    package::{CODE_RESOURCES_PATH, Entry, Kind, RESOURCE_PROBE_PATH},
    sign,
};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::atomic::{AtomicUsize, Ordering},
};

static SEQ: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-rel-sign-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Tree {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn clang(args: &[&str]) {
    let out = Command::new("/usr/bin/clang").args(args).output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
fn pin(app: &Path, path: &str, kind: Kind) -> Entry {
    let bytes = fs::read(app.join(path)).unwrap();
    Entry {
        path: path.into(),
        kind,
        bytes: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(&bytes)),
    }
}
/// `<root>/T.app` with a linker-signed dylib, a linker-signed helper and an executable.
fn app(root: &Path) -> (PathBuf, Vec<Entry>) {
    let app = root.join("T.app");
    for dir in ["Contents/MacOS/native", "Contents/Frameworks"] {
        fs::create_dir_all(app.join(dir)).unwrap();
    }
    let source = root.join("m.c");
    fs::write(&source, "int main(void){return 0;}\n").unwrap();
    let lib = root.join("l.c");
    fs::write(&lib, "int value(void){return 7;}\n").unwrap();
    let path = |p: &str| app.join(p).display().to_string();
    clang(&[
        "-o",
        &path("Contents/MacOS/mycelium"),
        source.to_str().unwrap(),
    ]);
    clang(&["-o", &path(RESOURCE_PROBE_PATH), source.to_str().unwrap()]);
    clang(&[
        "-dynamiclib",
        "-install_name",
        "@rpath/libmlxc.dylib",
        "-o",
        &path("Contents/Frameworks/libmlxc.dylib"),
        lib.to_str().unwrap(),
    ]);
    fs::write(
        app.join("Contents/Info.plist"),
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>mycelium</string><key>CFBundleIdentifier</key><string>org.example.rel-sign-test</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>\n",
    )
    .unwrap();
    let pins = vec![
        pin(&app, "Contents/Frameworks/libmlxc.dylib", Kind::Dylib),
        pin(&app, RESOURCE_PROBE_PATH, Kind::Helper),
    ];
    (app, pins)
}
fn codesign(args: &[&str], target: &Path) -> std::process::Output {
    Command::new(sign::CODESIGN)
        .args(args)
        .arg(target)
        .output()
        .unwrap()
}

#[test]
fn adhoc_signing_keeps_pinned_code_bytes_signs_the_executable_and_seals_the_bundle() {
    let tree = Tree::new();
    let (app, pins) = app(&tree.0);
    let exe_before = fs::read(app.join("Contents/MacOS/mycelium")).unwrap();
    let receipt = sign::adhoc(&app, &pins);
    assert!(receipt.is_ok(), "{receipt:?}");
    let receipt = receipt.unwrap();
    for entry in &pins {
        assert_eq!(
            pin(&app, &entry.path, entry.kind.clone()).sha256,
            entry.sha256,
            "{}",
            entry.path
        );
    }
    assert_ne!(
        fs::read(app.join("Contents/MacOS/mycelium")).unwrap(),
        exe_before
    );
    assert!(app.join(CODE_RESOURCES_PATH).is_file());
    // Independently of the receipt: the whole bundle verifies, strictly and deeply, and is ad-hoc.
    let verify = codesign(&["--verify", "--deep", "--strict", "-vvv"], &app);
    assert!(verify.status.success(), "{verify:?}");
    let display = codesign(&["-d", "-v"], &app);
    assert!(
        sign::is_adhoc_display(&String::from_utf8_lossy(&display.stderr)),
        "{display:?}"
    );
    assert_eq!(receipt["verify"]["exit"], 0);
    // codesign validates nested code concurrently, so its `--prepared:`/`--validated:` lines come
    // in another order on other runs (lane RELF: two builds of one commit wrote two different
    // SIGN-RECEIPT.json); the receipt records the transcript in one canonical (sorted) order.
    let lines: Vec<&str> = receipt["verify"]["stderr"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| l.as_str().unwrap())
        .collect();
    let mut sorted = lines.clone();
    sorted.sort_unstable();
    assert_eq!(lines, sorted, "verify transcript in canonical order");
    assert!(
        lines.iter().any(|l| l.ends_with(": valid on disk")),
        "{lines:?}"
    );
    assert_eq!(receipt["trust_level"], sign::TRUST_LEVEL);
    let text = receipt.to_string();
    assert!(
        !text.contains(&app.display().to_string()),
        "receipt paths are bundle-relative"
    );
    // No identity, timestamp or notarization appears in any command.
    for step in receipt["steps"].as_array().unwrap() {
        let argv: Vec<&str> = step["argv"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a.as_str().unwrap())
            .collect();
        assert!(
            !argv.iter().any(|a| a.contains("Developer ID")
                || a.starts_with("--keychain")
                || *a == "--timestamp"
                || a.contains("notar")),
            "{argv:?}"
        );
        if argv.contains(&"--sign") {
            assert_eq!(
                argv[argv.iter().position(|a| *a == "--sign").unwrap() + 1],
                "-"
            );
            assert!(argv.contains(&"--timestamp=none"));
            assert!(!argv.contains(&"--deep"), "no --deep signing");
        }
    }
}

#[test]
fn pinned_code_without_a_valid_adhoc_signature_is_refused_before_anything_is_signed() {
    let tree = Tree::new();
    let (app, _) = app(&tree.0);
    let dylib = app.join("Contents/Frameworks/libmlxc.dylib");
    let removed = codesign(&["--remove-signature"], &dylib);
    assert!(removed.status.success(), "{removed:?}");
    let pins = vec![
        pin(&app, "Contents/Frameworks/libmlxc.dylib", Kind::Dylib),
        pin(&app, RESOURCE_PROBE_PATH, Kind::Helper),
    ];
    let exe_before = fs::read(app.join("Contents/MacOS/mycelium")).unwrap();
    let refused = sign::adhoc(&app, &pins);
    assert!(
        refused
            .as_ref()
            .is_err_and(|e| e.0.starts_with("pinned_code_not_adhoc_signed")),
        "{refused:?}"
    );
    assert_eq!(
        fs::read(app.join("Contents/MacOS/mycelium")).unwrap(),
        exe_before
    );
    assert!(!app.join(CODE_RESOURCES_PATH).exists());
}

#[test]
fn pinned_code_with_other_bytes_is_refused() {
    let tree = Tree::new();
    let (app, mut pins) = app(&tree.0);
    pins[1].sha256 = "0".repeat(64);
    let refused = sign::adhoc(&app, &pins);
    assert!(
        refused
            .as_ref()
            .is_err_and(|e| e.0.starts_with("sign_pinned_bytes_changed")),
        "{refused:?}"
    );
    assert!(!app.join(CODE_RESOURCES_PATH).exists());
}
