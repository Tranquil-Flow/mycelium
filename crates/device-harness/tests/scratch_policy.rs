// SPDX-License-Identifier: AGPL-3.0-only
//! The fixture/scratch-root policy, exercised with explicit injected paths only.
//! Nothing here reads the ambient `TMPDIR` for an assertion. RED controls: a
//! non-private, symlinked, foreign-owned, relative or mislocated root is refused,
//! by the fixture policy and by the harness's own production directory checks.
mod support;
use budgets::Span;
use device_harness::{
    attempt::{Attempt, Parallel},
    effects::{Command, CommandKind, Transport},
    native::NativeTransport,
    preflight::ProcessIdentity,
};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::{fs, path::Path};
use support::{PrivateRoot, verify_private_root};

fn mode(path: &Path, bits: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(bits)).unwrap();
}
fn identity() -> ProcessIdentity {
    ProcessIdentity {
        pid: std::process::id(),
        birth: "birth".into(),
        executable: "exact harness".into(),
    }
}
const HOST_SCRATCH: &str = ".hermes/cache/scratch";

#[test]
fn fresh_root_is_canonical_private_owned_and_accepted() {
    let root = PrivateRoot::new("policy-ok");
    assert_eq!(verify_private_root(&root, root.uid(), None).unwrap(), *root);
    assert_eq!(fs::canonicalize(&*root).unwrap(), *root);
    assert_eq!(
        fs::metadata(&*root).unwrap().permissions().mode() & 0o777,
        0o700
    );
}

#[test]
fn non_private_symlinked_foreign_and_relative_roots_are_refused() {
    let root = PrivateRoot::new("policy-red");
    let uid = root.uid();
    let real = root.join("real");
    fs::create_dir(&real).unwrap();
    mode(&real, 0o700);
    assert_eq!(verify_private_root(&real, uid, None).unwrap(), real);
    for bits in [0o750, 0o755, 0o770, 0o707, 0o777, 0o500] {
        mode(&real, bits);
        assert_eq!(
            verify_private_root(&real, uid, None),
            Err("root_not_private"),
            "{bits:o}"
        );
    }
    mode(&real, 0o700);
    let link = root.join("link");
    symlink(&real, &link).unwrap();
    assert_eq!(
        verify_private_root(&link, uid, None),
        Err("root_is_symlink")
    );
    // A symlinked ancestor is a non-canonical root even though the leaf is real.
    let via_link = link.join("inner");
    fs::create_dir(&via_link).unwrap();
    mode(&via_link, 0o700);
    assert_eq!(
        verify_private_root(&via_link, uid, None),
        Err("root_not_canonical")
    );
    assert_eq!(
        verify_private_root(&real.join("..").join("real"), uid, None),
        Err("root_not_canonical")
    );
    assert_eq!(
        verify_private_root(&real, uid.wrapping_add(1), None),
        Err("root_wrong_owner")
    );
    assert_eq!(
        verify_private_root(Path::new("relative/root"), uid, None),
        Err("root_not_absolute")
    );
    let file = root.join("file");
    fs::write(&file, b"x").unwrap();
    assert_eq!(
        verify_private_root(&file, uid, None),
        Err("root_not_directory")
    );
    assert_eq!(
        verify_private_root(&root.join("absent"), uid, None),
        Err("root_unobserved")
    );
}

#[test]
fn explicit_location_marker_is_enforced_on_injected_paths_not_ambient_env() {
    let root = PrivateRoot::new("policy-marker");
    let uid = root.uid();
    // The marker rule is a pure function of the injected path text.
    for outside in [
        "/var/folders/zz/abcdef/T/v02-l12-x",
        "/private/var/folders/zz/T/x",
        "/tmp/x",
        "/Users/someone/.hermes/cache/other/x",
    ] {
        assert_eq!(
            verify_private_root(Path::new(outside), uid, Some(HOST_SCRATCH)),
            Err("root_outside_required_location"),
            "{outside}"
        );
    }
    // Inside the marker the remaining checks still apply, so a mere substring match
    // of a non-private or absent directory is not admitted.
    let inside = root.join(".hermes/cache/scratch/leaf");
    fs::create_dir_all(&inside).unwrap();
    mode(&inside, 0o755);
    assert_eq!(
        verify_private_root(&inside, uid, Some(HOST_SCRATCH)),
        Err("root_not_private")
    );
    mode(&inside, 0o700);
    assert_eq!(
        verify_private_root(&inside, uid, Some(HOST_SCRATCH)).unwrap(),
        inside
    );
    assert_eq!(
        verify_private_root(
            &root.join(".hermes/cache/scratch/absent"),
            uid,
            Some(HOST_SCRATCH)
        ),
        Err("root_unobserved")
    );
}

#[test]
fn attempt_open_refuses_symlinked_or_foreign_roots() {
    let root = PrivateRoot::new("policy-attempt");
    let uid = root.uid();
    let real = root.join("real-out");
    fs::create_dir(&real).unwrap();
    let link = root.join("link-out");
    symlink(&real, &link).unwrap();
    let markers = root.join("markers");
    let refused = Attempt::open(&link, &markers, "soak", identity(), Parallel::Quiet, uid)
        .err()
        .unwrap();
    assert_eq!(refused.code, "attempt_directory_invalid");
    assert!(!markers.join("DEVICE.busy").exists());
    let refused = Attempt::open(
        &root.join("foreign-out"),
        &markers,
        "soak",
        identity(),
        Parallel::Quiet,
        uid.wrapping_add(1),
    )
    .err()
    .unwrap();
    assert_eq!(refused.code, "attempt_directory_invalid");
    assert!(!markers.join("DEVICE.busy").exists());
}

#[test]
fn native_capture_directory_must_be_private_owned_and_not_a_link() {
    let root = PrivateRoot::new("policy-capture");
    let uid = root.uid();
    let command = Command {
        kind: CommandKind::Local,
        program: "/usr/bin/id".into(),
        args: vec!["-u".into()],
    };
    let transport = |scratch: &Path, uid: u32| NativeTransport {
        scratch: scratch.to_path_buf(),
        uid,
        poll: Span::from_millis(50),
        max_output_bytes: 4096,
        diagnostics: vec![],
    };
    let open = root.join("open");
    fs::create_dir(&open).unwrap();
    mode(&open, 0o755);
    let link = root.join("link");
    symlink(&*root, &link).unwrap();
    let cases: [(&Path, u32); 3] = [(&open, uid), (&link, uid), (&*root, uid.wrapping_add(1))];
    for (scratch, uid) in cases {
        let err = transport(scratch, uid)
            .execute(&command, Span::from_millis(1000))
            .err()
            .unwrap();
        assert_eq!(err.code, "capture_directory_not_private", "{scratch:?}");
    }
    let err = transport(&root.join("absent"), uid)
        .execute(&command, Span::from_millis(1000))
        .err()
        .unwrap();
    assert_eq!(err.code, "capture_directory_unavailable");
    // Control: the same transport admits a private owned capture directory.
    let ok = transport(&root, uid)
        .execute(&command, Span::from_millis(5000))
        .unwrap();
    assert_eq!(ok.rc, 0);
    assert_eq!(ok.stdout.trim().parse::<u32>().ok(), Some(uid));
}
