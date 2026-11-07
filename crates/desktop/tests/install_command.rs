// SPDX-License-Identifier: AGPL-3.0-only
//! `Install Mycelium.command`: the one script the user approves in Gatekeeper, ported from
//! v0.1.4 (`_INSTALL_COMMAND` of `scripts/finalize_a13_macos.py`, tested there by
//! `tests/a13_macos/test_install_command.py`, at f4e366e9). Same flow, same refusals, shell only.
//!
//! SAFETY, as in the oracle: no real quarantine attribute is ever set or removed and Gatekeeper is
//! never touched. The marks live in a text file; the script copy under test calls a fake `xattr`
//! (a shell adapter over that file) instead of `/usr/bin/xattr`, and its runner is a stub outside
//! the bundle that only records what it was started with, so nothing in the app is executed.
//! Hash/file validation and the order of checks are the real script's. These tests do NOT qualify
//! Finder or Gatekeeper: those need the owner's isolated-account journey (NOT_RUN, see L11-DESKTOP.md).
use desktop::docs::{self, FinalizeSpec, Signer};
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};

static SEQ: AtomicUsize = AtomicUsize::new(0);
const APP: &str = "Mycelium A13 Demo.app";
const EXE: &str = "Contents/MacOS/mycelium";
const QUARANTINE: &str = "com.apple.quarantine";
const QVALUE: &str = "0083;6700f000;Safari;F00DF00D-0000-0000-0000-000000000000";
const RUNNER_LINE: &str = "RUNNER=\"$EXE\"\n";

/// `xattr` as the script calls it: `-d -r -s ATTR PATH` and `-p ATTR PATH`, over `$FAKE_XATTR_MARKS`
/// (one marked absolute path per line). `-r` takes every path below PATH; links are plain paths.
const FAKE_XATTR: &str = r#"#!/bin/sh
set -u
: "${FAKE_XATTR_MARKS:?}"
case "$1" in
  -d)
    [ "${FAKE_XATTR_FAIL_DELETE:-}" = 1 ] && exit 1
    target="$5"
    tmp="$FAKE_XATTR_MARKS.tmp"
    : > "$tmp"
    while IFS= read -r line; do
      case "$line" in "$target"|"$target"/*) ;; *) printf '%s\n' "$line" >> "$tmp" ;; esac
    done < "$FAKE_XATTR_MARKS"
    mv "$tmp" "$FAKE_XATTR_MARKS"
    ;;
  -p)
    grep -Fxq -- "$3" "$FAKE_XATTR_MARKS" || exit 1
    printf '%s\n' "__QVALUE__"
    ;;
  *) exit 64 ;;
esac
"#;
/// Records what the installer would have been started with, and whether the app folder and a data
/// file inside it were marked at that moment. Lives outside the bundle.
const STUB: &str = r#"#!/bin/sh
BUNDLE="$3"
OUT="$BUNDLE/ran.txt"
{ printf 'args:'; for a in "$@"; do printf ' [%s]' "$a"; done; echo; } > "$OUT"
for item in "$BUNDLE/Mycelium A13 Demo.app" "$BUNDLE/Mycelium A13 Demo.app/Contents/Resources/data.bin"; do
  if "__FAKE_XATTR__" -p __ATTRIBUTE__ "$item" >/dev/null 2>&1; then echo quarantined >> "$OUT"; else echo clean >> "$OUT"; fi
done
echo '{"action": "install", "version": "0.1.0"}'
exit "${FAKE_APP_EXIT:-0}"
"#;

struct Fixture {
    root: PathBuf,
    folder: PathBuf,
    marks: PathBuf,
    fake: PathBuf,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fn unlock(path: &Path) {
            if let Ok(meta) = fs::symlink_metadata(path)
                && meta.is_dir()
            {
                if let Err(error) = fs::set_permissions(path, fs::Permissions::from_mode(0o700)) {
                    eprintln!("unlock_failed:{:?}", error.kind());
                }
                for child in fs::read_dir(path).into_iter().flatten().flatten() {
                    unlock(&child.path());
                }
            }
        }
        unlock(&self.root);
        if let Err(error) = fs::remove_dir_all(&self.root) {
            eprintln!("cleanup_failed:{:?}", error.kind());
        }
    }
}
fn put(path: &Path, bytes: &[u8], mode: u32) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}
fn every_path(root: &Path) -> Vec<PathBuf> {
    let mut out = vec![root.to_path_buf()];
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for child in fs::read_dir(&dir).unwrap().flatten() {
            let path = child.path();
            out.push(path.clone());
            if fs::symlink_metadata(&path).unwrap().is_dir() {
                stack.push(path);
            }
        }
    }
    out
}
fn marked(f: &Fixture, root: &Path) -> Vec<String> {
    let marks = fs::read_to_string(&f.marks).unwrap();
    let set: std::collections::HashSet<_> = marks.lines().collect();
    let mut out: Vec<String> = every_path(root)
        .into_iter()
        .filter(|p| set.contains(p.to_str().unwrap()))
        .map(|p| p.strip_prefix(root).unwrap().display().to_string())
        .collect();
    out.sort();
    out
}

/// A finalized bundle (signed with a throw-away test key), zipped and extracted by `ditto` the way
/// a user's Mac does, then marked in the marks file only.
fn fixture() -> Fixture {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "v02-l11-cmd-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir_all(&root).unwrap();
    let build = root.join("build/mycelium-0.1.0-macos-arm64");
    let app = build.join(APP);
    put(
        &app.join("Contents/Frameworks/libdemo.dylib"),
        b"lib",
        0o644,
    );
    put(&app.join("Contents/Resources/data.bin"), b"data", 0o644);
    symlink(
        "../Frameworks/libdemo.dylib",
        app.join("Contents/Resources/libdemo"),
    )
    .unwrap();
    put(&app.join(EXE), b"stub-executable-never-run", 0o644); // data: nothing here is ever spawned
    docs::finalize(
        &build,
        &Signer::from_seed([0x33; 32], docs::SIGNER_ENDPOINT_ID),
        &FinalizeSpec {
            version: "0.1.0",
            rollback_floor: "0.1.0",
            source_revision: "0123456789abcdef0123456789abcdef01234567",
            issued_at: 1_790_000_000.0,
            minimum_os: "15.6.1",
        },
    )
    .unwrap();
    let ditto = |args: &[&str]| {
        assert!(
            Command::new("/usr/bin/ditto")
                .args(args)
                .status()
                .unwrap()
                .success()
        );
    };
    let zip = root.join("mycelium-0.1.0-macos-arm64.zip");
    ditto(&[
        "-c",
        "-k",
        "--keepParent",
        build.to_str().unwrap(),
        zip.to_str().unwrap(),
    ]);
    let extract = root.join("Down loads"); // a space on purpose
    ditto(&["-x", "-k", zip.to_str().unwrap(), extract.to_str().unwrap()]);
    let folder = extract.join("mycelium-0.1.0-macos-arm64");
    let fake = root.join("tools/xattr");
    put(
        &fake,
        FAKE_XATTR.replace("__QVALUE__", QVALUE).as_bytes(),
        0o755,
    );
    let marks = root.join("tools/marks.txt");
    let stub = root.join("tools/app-stub");
    put(
        &stub,
        STUB.replace("__FAKE_XATTR__", fake.to_str().unwrap())
            .replace("__ATTRIBUTE__", QUARANTINE)
            .as_bytes(),
        0o755,
    );
    // The script under test: the real text, the xattr command and the runner line replaced.
    let text = fs::read_to_string(folder.join(docs::COMMAND_NAME)).unwrap();
    assert_eq!(
        text,
        docs::INSTALL_COMMAND,
        "the zip carries the script byte for byte"
    );
    assert_eq!(text.matches(RUNNER_LINE).count(), 1);
    assert_eq!(text.matches("XATTR=/usr/bin/xattr\n").count(), 1);
    let altered = text
        .replace(
            "XATTR=/usr/bin/xattr\n",
            &format!("XATTR=\"{}\"\n", fake.display()),
        )
        .replace(RUNNER_LINE, &format!("RUNNER=\"{}\"\n", stub.display()));
    fs::write(folder.join("run-under-test.command"), altered).unwrap();
    // The download is marked everywhere (files, folders, links), except the script copy under test.
    let skip = folder.join("run-under-test.command");
    let lines: String = every_path(&folder)
        .into_iter()
        .filter(|p| *p != skip)
        .map(|p| format!("{}\n", p.display()))
        .collect();
    fs::write(&marks, lines).unwrap();
    Fixture {
        root,
        folder,
        marks,
        fake,
    }
}
fn run(f: &Fixture, envs: &[(&str, &str)]) -> Output {
    Command::new("/bin/sh")
        .arg(f.folder.join("run-under-test.command"))
        .env("FAKE_XATTR_MARKS", &f.marks)
        .envs(envs.iter().copied())
        .current_dir("/")
        .output()
        .unwrap()
}
fn ran(f: &Fixture) -> PathBuf {
    f.folder.join("ran.txt")
}

#[test]
fn the_script_clears_quarantine_from_the_app_only_then_runs_the_installer() {
    let f = fixture();
    let app = f.folder.join(APP);
    assert!(!marked(&f, &app).is_empty());
    let done = run(&f, &[]);
    assert!(
        done.status.success(),
        "{}",
        String::from_utf8_lossy(&done.stderr)
    );
    let ran_text = fs::read_to_string(ran(&f)).unwrap();
    let lines: Vec<_> = ran_text.lines().collect();
    assert_eq!(
        lines,
        [
            format!("args: [--install] [--bundle] [{}]", f.folder.display()).as_str(),
            "clean",
            "clean"
        ],
        "app folder and data are clean when the installer starts"
    );
    assert_eq!(marked(&f, &app), Vec::<String>::new());
    let documents = marked(&f, &f.folder);
    for kept in [
        "installation-package-manifest.json",
        "install-source.json",
        "SHA256SUMS",
        "Install Mycelium.command",
    ] {
        assert!(
            documents.iter().any(|d| d == kept),
            "{kept} keeps its mark: {documents:?}"
        );
    }
    let out = String::from_utf8_lossy(&done.stdout).to_lowercase();
    assert!(
        out.contains("installed") && out.contains("applications"),
        "{out}"
    );
}

#[test]
fn a_document_that_does_not_match_the_sums_stops_everything_before_any_trust_is_extended() {
    type Tamper = fn(&Path);
    let cases: Vec<(&str, Tamper)> = vec![
        ("manifest_byte", |d| {
            fs::write(d.join("installation-package-manifest.json"), "{}\n").unwrap()
        }),
        ("install_source_byte", |d| {
            use std::io::Write;
            fs::OpenOptions::new()
                .append(true)
                .open(d.join("install-source.json"))
                .unwrap()
                .write_all(b" ")
                .unwrap();
        }),
        ("command_edit", |d| {
            use std::io::Write;
            fs::OpenOptions::new()
                .append(true)
                .open(d.join(docs::COMMAND_NAME))
                .unwrap()
                .write_all(b"# edited\n")
                .unwrap();
        }),
        ("sums_missing", |d| {
            fs::remove_file(d.join("SHA256SUMS")).unwrap()
        }),
        ("sums_without_manifest", |d| {
            strip(d, "installation-package-manifest.json")
        }),
        ("sums_without_install_source", |d| {
            strip(d, "install-source.json")
        }),
        ("sums_without_command", |d| {
            strip(d, "Install Mycelium.command")
        }),
        ("sums_without_app_files", |d| strip(d, "APP-FILES.sha256")),
        ("sums_empty", |d| {
            fs::write(d.join("SHA256SUMS"), "").unwrap()
        }),
        ("app_file_byte", |d| {
            use std::io::Write;
            fs::OpenOptions::new()
                .append(true)
                .open(d.join(APP).join("Contents/Resources/data.bin"))
                .unwrap()
                .write_all(b"x")
                .unwrap();
        }),
        ("app_file_missing", |d| {
            fs::remove_file(d.join(APP).join("Contents/Resources/data.bin")).unwrap()
        }),
        ("app_extra_file", |d| {
            fs::write(d.join(APP).join("Contents/Resources/extra.bin"), b"extra").unwrap()
        }),
        ("app_extra_link_absolute", |d| {
            symlink("/etc/hosts", d.join(APP).join("Contents/Resources/out")).unwrap()
        }),
        ("app_files_list_edit", |d| {
            use std::io::Write;
            fs::OpenOptions::new()
                .append(true)
                .open(d.join("APP-FILES.sha256"))
                .unwrap()
                .write_all(b"\n")
                .unwrap();
        }),
    ];
    fn strip(folder: &Path, name: &str) {
        let sums = folder.join("SHA256SUMS");
        let kept: String = fs::read_to_string(&sums)
            .unwrap()
            .lines()
            .filter(|l| !l.ends_with(name))
            .map(|l| format!("{l}\n"))
            .collect();
        fs::write(sums, kept).unwrap();
    }
    for (name, tamper) in cases {
        let f = fixture();
        tamper(&f.folder);
        let app = f.folder.join(APP);
        let before = marked(&f, &app);
        assert!(!before.is_empty());
        let done = run(&f, &[]);
        assert_eq!(done.status.code(), Some(2), "{name}");
        assert!(
            String::from_utf8_lossy(&done.stderr)
                .to_lowercase()
                .contains("not installed"),
            "{name}"
        );
        assert!(!ran(&f).exists(), "{name}: the app must never run");
        assert_eq!(
            marked(&f, &app),
            before,
            "{name}: nothing was un-quarantined"
        );
    }
}

#[test]
fn an_app_that_is_not_a_plain_bundle_inside_this_folder_is_refused() {
    for damage in [
        "app_is_a_link",
        "executable_is_a_link",
        "executable_missing",
    ] {
        let f = fixture();
        let app = f.folder.join(APP);
        match damage {
            "app_is_a_link" => {
                let elsewhere = f.root.join("elsewhere.app");
                fs::rename(&app, &elsewhere).unwrap();
                symlink(&elsewhere, &app).unwrap();
            }
            "executable_is_a_link" => {
                let target = f.root.join("other-exe");
                fs::write(&target, "data, never run").unwrap();
                fs::remove_file(app.join(EXE)).unwrap();
                symlink(&target, app.join(EXE)).unwrap();
            }
            _ => fs::remove_file(app.join(EXE)).unwrap(),
        }
        let done = run(&f, &[]);
        assert_eq!(done.status.code(), Some(2), "{damage}");
        assert!(!ran(&f).exists(), "{damage}");
        assert!(
            String::from_utf8_lossy(&done.stderr)
                .to_lowercase()
                .contains("not installed"),
            "{damage}"
        );
    }
}

#[test]
fn the_script_refuses_when_quarantine_removal_fails() {
    let f = fixture();
    let app = f.folder.join(APP);
    let before = marked(&f, &app);
    let done = run(&f, &[("FAKE_XATTR_FAIL_DELETE", "1")]);
    assert_eq!(done.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&done.stderr).contains("download mark could not be removed"));
    assert!(!ran(&f).exists());
    assert_eq!(marked(&f, &app), before);
}

#[test]
fn the_mark_on_a_link_itself_is_cleared_too() {
    let f = fixture();
    let link = f.folder.join(APP).join("Contents/Resources/libdemo");
    assert!(
        marked(&f, &f.folder)
            .iter()
            .any(|p| p.ends_with("Contents/Resources/libdemo"))
    );
    assert!(run(&f, &[]).status.success());
    assert!(
        !marked(&f, &f.folder)
            .iter()
            .any(|p| p.ends_with("Contents/Resources/libdemo"))
    );
    assert!(fs::symlink_metadata(link).unwrap().file_type().is_symlink());
}

#[test]
fn the_installer_refusal_is_passed_through_and_never_reported_as_installed() {
    let f = fixture();
    let done = run(&f, &[("FAKE_APP_EXIT", "2")]);
    assert_eq!(done.status.code(), Some(2));
    assert!(
        !String::from_utf8_lossy(&done.stdout)
            .to_lowercase()
            .contains("is installed")
    );
    assert!(String::from_utf8_lossy(&done.stderr).contains("the installer refused"));
}

#[test]
fn the_script_uses_no_network_no_admin_and_no_global_trust_changes() {
    let text = docs::INSTALL_COMMAND;
    for forbidden in [
        "sudo",
        "curl",
        "wget",
        "nc ",
        "spctl",
        "csrutil",
        "defaults write",
        "chmod -R",
        "rm -r",
    ] {
        assert!(!text.contains(forbidden), "{forbidden}");
    }
    // One targeted removal, of the app only, and the one attribute name.
    assert_eq!(
        text.matches("xattr").count(),
        1,
        "the path of the command, once"
    );
    assert_eq!(
        text.matches("\"$XATTR\"").count(),
        2,
        "one removal and one read-back"
    );
    assert_eq!(text.matches("com.apple.quarantine").count(), 2);
    assert_eq!(
        text.matches("-d -r -s com.apple.quarantine \"$APP\"")
            .count(),
        1
    );
    // The fake `xattr` under test is only ever reached through the script's own XATTR variable.
    assert_eq!(text.matches("XATTR=/usr/bin/xattr\n").count(), 1);
}

#[test]
fn sums_cover_the_documents_the_script_checks_and_every_regular_app_file() {
    let f = fixture();
    let sums = fs::read_to_string(f.folder.join("SHA256SUMS")).unwrap();
    let listed: Vec<_> = sums
        .lines()
        .map(|l| l.split_once("  ").unwrap().1.to_string())
        .collect();
    for name in [
        "installation-package-manifest.json",
        "install-source.json",
        "Install Mycelium.command",
        "APP-FILES.sha256",
    ] {
        assert!(listed.iter().any(|l| l == name), "{name}");
    }
    let app_files = fs::read_to_string(f.folder.join("APP-FILES.sha256")).unwrap();
    let mut files: Vec<_> = app_files
        .lines()
        .map(|l| l.split_once("  ").unwrap().1.to_string())
        .collect();
    files.sort();
    assert_eq!(
        files,
        [
            "Contents/Frameworks/libdemo.dylib",
            "Contents/MacOS/mycelium",
            "Contents/Resources/data.bin"
        ],
        "regular files only; links are covered by the signed digest and refused here when absolute"
    );
    assert!(f.fake.exists());
}
