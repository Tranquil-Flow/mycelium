// SPDX-License-Identifier: AGPL-3.0-only
//! Native private-file adapter against the REAL filesystem (fixture roots only; no app,
//! device or network). Every refusal the `Files` contract demands is exercised on a real
//! symlink, hard link, FIFO, wrong mode, oversize file and swapped parent.
mod support;
use budgets::Span;
use device_harness::{
    Result,
    native_files::{Admitted, CONTROL, LAUNCH, NativeFiles, SESSION},
    native_io::{FileIdentity, Files, Slot},
    native_os::Probe,
    preflight::ProcessIdentity,
    stranger::Candidate,
};
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::{fs, path::Path};
use support::PrivateRoot;

struct FakeProbe;
impl Probe for FakeProbe {
    fn process(&mut self, pid: u32, _: Span) -> Result<ProcessIdentity> {
        Ok(ProcessIdentity {
            pid,
            birth: "Wed 28 Oct 05:19:01 2026".into(),
            executable: "/fixture/Mycelium.app/Contents/MacOS/mycelium".into(),
        })
    }
    fn listener_port(&mut self, _: u32, _: Span) -> Result<u16> {
        Ok(49152)
    }
}
fn admitted() -> Admitted {
    Admitted {
        candidate: Candidate {
            version: "0.2.0".into(),
            source_sha: "b".repeat(40),
            zip_sha256: "c".repeat(64),
            exe_sha256: "d".repeat(64),
        },
        pid: 4242,
        launch_epoch_ms: 1,
        desktop_manifest_sha256: "e".repeat(64),
        native_inputs_sha256: "f".repeat(64),
    }
}
struct Fixture {
    app: PrivateRoot,
    sess: PrivateRoot,
}
impl Fixture {
    fn new(tag: &str) -> Self {
        Self {
            app: PrivateRoot::new(&format!("nf-app-{tag}")),
            sess: PrivateRoot::new(&format!("nf-sess-{tag}")),
        }
    }
    fn files(&self) -> NativeFiles<FakeProbe> {
        NativeFiles::new(&self.app, &self.sess, self.app.uid(), admitted(), FakeProbe).unwrap()
    }
}
fn put(path: &Path, bytes: &[u8], mode: u32) {
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}
fn span() -> Span {
    Span::from_millis(5_000)
}

#[test]
fn reads_report_real_identity_owner_mode_links_and_mtime() {
    let f = Fixture::new("read");
    put(&f.app.join(CONTROL), br#"{"port":1}"#, 0o600);
    let file = f
        .files()
        .read(Slot::Control, 4096, span())
        .unwrap()
        .unwrap();
    let m = fs::metadata(f.app.join(CONTROL)).unwrap();
    assert_eq!(
        file.identity,
        FileIdentity {
            device: m.dev(),
            inode: m.ino()
        }
    );
    assert_eq!(
        (file.uid, file.mode, file.links, file.regular, file.symlink),
        (f.app.uid(), 0o600, 1, true, false)
    );
    assert_eq!(file.bytes, br#"{"port":1}"#);
    assert_eq!(
        file.modified_ms,
        u64::try_from(m.mtime()).unwrap() * 1000
            + u64::try_from(m.mtime_nsec()).unwrap() / 1_000_000
    );
}

#[test]
fn absent_file_is_none_and_launch_and_control_live_in_the_app_root_session_in_its_own() {
    let f = Fixture::new("absent");
    let mut files = f.files();
    for slot in [Slot::Control, Slot::Launch, Slot::Session] {
        assert!(files.read(slot, 4096, span()).unwrap().is_none());
    }
    put(&f.app.join(LAUNCH), b"x", 0o600);
    put(&f.sess.join(SESSION), b"y", 0o600);
    assert_eq!(
        files
            .read(Slot::Launch, 4096, span())
            .unwrap()
            .unwrap()
            .bytes,
        b"x"
    );
    assert_eq!(
        files
            .read(Slot::Session, 4096, span())
            .unwrap()
            .unwrap()
            .bytes,
        b"y"
    );
}

#[test]
fn symlink_is_flagged_and_its_target_is_never_read() {
    let f = Fixture::new("link");
    let secret = f.app.join("secret");
    put(&secret, b"TOP-SECRET-TARGET", 0o600);
    symlink(&secret, f.app.join(CONTROL)).unwrap();
    let file = f
        .files()
        .read(Slot::Control, 4096, span())
        .unwrap()
        .unwrap();
    assert!(file.symlink && !file.regular);
    assert!(file.bytes.is_empty());
}

#[test]
fn wrong_mode_hard_link_and_fifo_are_reported_not_normalised() {
    let f = Fixture::new("shape");
    put(&f.app.join(CONTROL), b"{}", 0o644);
    let c = f
        .files()
        .read(Slot::Control, 4096, span())
        .unwrap()
        .unwrap();
    assert_eq!(c.mode, 0o644);
    put(&f.app.join(LAUNCH), b"<form>", 0o600);
    fs::hard_link(f.app.join(LAUNCH), f.app.join("alias")).unwrap();
    let l = f.files().read(Slot::Launch, 4096, span()).unwrap().unwrap();
    assert_eq!(l.links, 2);
    // A FIFO must neither block the harness nor be read.
    let fifo = f.sess.join(SESSION);
    let status = std::process::Command::new("/usr/bin/mkfifo")
        .arg(&fifo)
        .status()
        .unwrap();
    assert!(status.success());
    let started = std::time::Instant::now();
    let s = f
        .files()
        .read(Slot::Session, 4096, span())
        .unwrap()
        .unwrap();
    assert!(!s.regular && s.bytes.is_empty());
    // It returned within its own bound instead of blocking on the writer-less FIFO.
    assert!(started.elapsed().as_millis() < u128::from(span().millis()));
}

#[test]
fn read_is_bounded_by_the_cap_not_the_file_size() {
    let f = Fixture::new("big");
    put(&f.app.join(CONTROL), &vec![b'a'; 1 << 20], 0o600);
    let file = f.files().read(Slot::Control, 16, span()).unwrap().unwrap();
    assert_eq!(
        file.bytes.len(),
        17,
        "cap plus one so the caller can refuse"
    );
}

#[test]
fn roots_must_be_canonical_private_owned_directories_and_stay_so() {
    let f = Fixture::new("root");
    let uid = f.app.uid();
    let open = f.app.join("open");
    fs::create_dir(&open).unwrap();
    fs::set_permissions(&open, fs::Permissions::from_mode(0o755)).unwrap();
    let new = |app: &Path, sess: &Path, uid: u32| {
        NativeFiles::new(app, sess, uid, admitted(), FakeProbe)
            .err()
            .map(|e| e.code)
    };
    assert_eq!(new(&open, &f.sess, uid), Some("native_root_invalid"));
    let link = f.app.join("link");
    symlink(&*f.sess, &link).unwrap();
    assert_eq!(new(&link, &f.sess, uid), Some("native_root_invalid"));
    assert_eq!(
        new(&f.app, &f.sess, uid.wrapping_add(1)),
        Some("native_root_invalid")
    );
    assert_eq!(
        new(Path::new("relative"), &f.sess, uid),
        Some("native_root_invalid")
    );
    assert_eq!(new(&f.app, &f.app, uid), Some("native_root_invalid"));
    assert_eq!(new(&f.app, &f.sess, uid), None);
    // Swap the app root for a different private directory after construction.
    let mut files = f.files();
    let moved = f.app.with_file_name(format!(
        "{}-moved",
        f.app.file_name().unwrap().to_str().unwrap()
    ));
    fs::rename(&*f.app, &moved).unwrap();
    fs::create_dir(&*f.app).unwrap();
    fs::set_permissions(&*f.app, fs::Permissions::from_mode(0o700)).unwrap();
    let err = files.read(Slot::Control, 4096, span()).err().unwrap();
    assert_eq!(err.code, "native_root_changed");
    fs::remove_dir(&*f.app).unwrap();
    fs::rename(&moved, &*f.app).unwrap();
}

#[test]
fn replace_session_creates_then_replaces_only_the_expected_identity() {
    let f = Fixture::new("replace");
    let mut files = f.files();
    let target = f.sess.join(SESSION);
    files.replace_session(None, b"first", span()).unwrap();
    let first = fs::metadata(&target).unwrap();
    assert_eq!(fs::read(&target).unwrap(), b"first");
    assert_eq!(
        (
            first.permissions().mode() & 0o777,
            first.nlink(),
            first.uid()
        ),
        (0o600, 1, f.sess.uid())
    );
    let old = FileIdentity {
        device: first.dev(),
        inode: first.ino(),
    };
    files.replace_session(Some(old), b"second", span()).unwrap();
    let second = fs::metadata(&target).unwrap();
    assert_eq!(fs::read(&target).unwrap(), b"second");
    assert_ne!(
        second.ino(),
        first.ino(),
        "atomic replacement, not in-place write"
    );
    assert_eq!(second.permissions().mode() & 0o777, 0o600);
    // No temp residue.
    let names: Vec<_> = fs::read_dir(&*f.sess)
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    assert_eq!(names, vec![std::ffi::OsString::from(SESSION)]);
}

#[test]
fn stale_or_mismatched_expectations_leave_the_existing_session_untouched() {
    let f = Fixture::new("stale");
    let mut files = f.files();
    let target = f.sess.join(SESSION);
    files.replace_session(None, b"first", span()).unwrap();
    let stale = FileIdentity {
        device: 1,
        inode: 2,
    };
    for old in [None, Some(stale)] {
        let err = files.replace_session(old, b"evil", span()).err().unwrap();
        assert_eq!(err.code, "native_session_identity_changed");
        assert_eq!(fs::read(&target).unwrap(), b"first");
    }
    fs::remove_file(&target).unwrap();
    let err = files
        .replace_session(Some(stale), b"evil", span())
        .err()
        .unwrap();
    assert_eq!(err.code, "native_session_identity_changed");
    assert!(!target.exists());
    // A planted symlink is never followed or replaced through.
    let victim = f.sess.join("victim");
    put(&victim, b"keep", 0o600);
    symlink(&victim, &target).unwrap();
    assert!(files.replace_session(None, b"evil", span()).is_err());
    assert_eq!(fs::read(&victim).unwrap(), b"keep");
    let entries = fs::read_dir(&*f.sess).unwrap().count();
    assert_eq!(entries, 2, "victim and the planted link only, no temp file");
}

#[test]
fn observe_binds_probe_identity_listener_port_and_control_mtime() {
    let f = Fixture::new("observe");
    let mut files = f.files();
    assert_eq!(
        files.observe(span()).err().unwrap().code,
        "native_control_missing"
    );
    put(&f.app.join(CONTROL), br#"{"port":49152}"#, 0o600);
    let o = files.observe(span()).unwrap();
    let m = fs::metadata(f.app.join(CONTROL)).unwrap();
    assert_eq!(o.port, 49152);
    assert_eq!(
        o.control_modified_ms,
        u64::try_from(m.mtime()).unwrap() * 1000
            + u64::try_from(m.mtime_nsec()).unwrap() / 1_000_000
    );
    assert_eq!(o.binding.process.pid, 4242);
    assert_eq!(o.binding.candidate, admitted().candidate);
    assert_eq!(o.binding.native_inputs_sha256, "f".repeat(64));
}

#[test]
fn probe_returning_a_different_pid_is_refused() {
    struct Wrong;
    impl Probe for Wrong {
        fn process(&mut self, pid: u32, _: Span) -> Result<ProcessIdentity> {
            Ok(ProcessIdentity {
                pid: pid + 1,
                birth: "b".into(),
                executable: "/x".into(),
            })
        }
        fn listener_port(&mut self, _: u32, _: Span) -> Result<u16> {
            Ok(1)
        }
    }
    let f = Fixture::new("wrongpid");
    put(&f.app.join(CONTROL), b"{}", 0o600);
    let mut files = NativeFiles::new(&f.app, &f.sess, f.app.uid(), admitted(), Wrong).unwrap();
    assert_eq!(
        files.observe(span()).err().unwrap().code,
        "native_process_identity_mismatch"
    );
}

#[test]
fn source_unbounded_read_returns_the_whole_file_and_a_bounded_read_still_refuses_via_the_caller() {
    let f = Fixture::new("unbounded");
    let big = vec![b'x'; 300_000];
    put(&f.app.join(LAUNCH), &big, 0o600);
    let all = f
        .files()
        .read(
            Slot::Launch,
            device_harness::harness_budgets::SOURCE_UNBOUNDED_READ.get(),
            span(),
        )
        .unwrap()
        .unwrap();
    assert_eq!(
        all.bytes.len(),
        big.len(),
        "no ceiling of our own (RU-014, V03-L12-1)"
    );
    // A bounded cap still reads cap+1 bytes only, so the caller can refuse it.
    let capped = f.files().read(Slot::Launch, 4096, span()).unwrap().unwrap();
    assert_eq!(capped.bytes.len(), 4097);
}
