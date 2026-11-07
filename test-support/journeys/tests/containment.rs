// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::process::{Scratch, audit_tree, kernel_path, sandbox_profile, scratch_base};
use std::{
    env, fs,
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::Command,
};
/// Parent shapes of `<home>/.hermes/cache/scratch` a host can have: no Hermes tree at all,
/// ordinary directories, or a symlink at some level (this host links `~/.hermes`).
const SAFE_SHAPES: [&str; 5] = [
    "absent",
    "ordinary",
    "symlinked-hermes",
    "symlinked-scratch",
    "symlinked-home",
];
/// Unsafe existing levels and their exact refusal; none is created through or repaired.
const UNSAFE_SHAPES: [(&str, &str); 6] = [
    ("world-writable-scratch", "scratch:base_writable_by_others"),
    ("group-writable-cache", "scratch:base_writable_by_others"),
    ("file-hermes", "scratch:base_not_directory"),
    ("dangling-hermes", "scratch:base_inspect:io:NotFound"),
    ("foreign-scratch", "scratch:base_not_owned"),
    ("scratch-is-home", "scratch:base_contains_home"),
];
/// Builds `shape` under `root` (owned by this process) and returns the HOME to pass
/// explicitly. The ambient HOME is never mutated.
fn home_shape(root: &Path, shape: &str) -> PathBuf {
    let case = root.join(shape);
    let (home, elsewhere) = (case.join("home"), case.join("elsewhere"));
    fs::create_dir_all(&elsewhere).unwrap();
    if shape == "symlinked-home" {
        fs::create_dir_all(elsewhere.join(".hermes/cache/scratch")).unwrap();
        symlink(&elsewhere, &home).unwrap();
        return home;
    }
    fs::create_dir(&home).unwrap();
    let hermes = home.join(".hermes");
    let mode = |path: PathBuf, bits| fs::set_permissions(path, fs::Permissions::from_mode(bits));
    match shape {
        "absent" => {}
        "ordinary" => fs::create_dir_all(hermes.join("cache/scratch")).unwrap(),
        "symlinked-hermes" => symlink(&elsewhere, &hermes).unwrap(),
        "symlinked-scratch" | "foreign-scratch" | "scratch-is-home" => {
            fs::create_dir_all(hermes.join("cache")).unwrap();
            let target = match shape {
                "foreign-scratch" => Path::new("/usr"),
                "scratch-is-home" => home.as_path(),
                _ => elsewhere.as_path(),
            };
            symlink(target, hermes.join("cache/scratch")).unwrap();
        }
        "world-writable-scratch" => {
            fs::create_dir_all(hermes.join("cache/scratch")).unwrap();
            mode(hermes.join("cache/scratch"), 0o777).unwrap();
        }
        "group-writable-cache" => {
            fs::create_dir_all(hermes.join("cache")).unwrap();
            mode(hermes.join("cache"), 0o775).unwrap();
        }
        "file-hermes" => fs::write(&hermes, b"not a directory").unwrap(),
        "dangling-hermes" => symlink(case.join("missing"), &hermes).unwrap(),
        _ => unreachable!("{shape}"),
    }
    home
}
/// Every entry under `path` with its unfollowed mode, so a refusal provably changed nothing.
fn tree(path: &Path) -> Vec<(PathBuf, u32)> {
    let meta = fs::symlink_metadata(path).unwrap();
    let mut out = vec![(path.to_path_buf(), meta.mode())];
    if meta.is_dir() {
        let mut entries = fs::read_dir(path)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect::<Vec<_>>();
        entries.sort();
        out.extend(entries.iter().flat_map(|entry| tree(entry)));
    }
    out
}
#[test]
fn audit_never_follows_links_and_rejects_model_payloads() {
    let home = PathBuf::from(env::var_os("HOME").unwrap());
    let mut scratch = Scratch::new(&home, None).unwrap();
    fs::write(scratch.root.join("small.json"), b"{}").unwrap();
    assert_eq!(audit_tree(&scratch.root).unwrap(), 2);
    fs::write(scratch.root.join("weights.safetensors"), b"bad").unwrap();
    assert_eq!(
        audit_tree(&scratch.root).unwrap_err(),
        "scratch:model_payload_forbidden"
    );
    fs::remove_file(scratch.root.join("weights.safetensors")).unwrap();
    std::os::unix::fs::symlink("/never-read", scratch.root.join("escape")).unwrap();
    assert_eq!(
        audit_tree(&scratch.root).unwrap_err(),
        "scratch:symlink_forbidden"
    );
    scratch.remove().unwrap();
    assert!(!scratch.root.exists());
}
#[test]
fn sandbox_enforcement_has_real_positive_and_negative_controls() {
    // The ambient HOME first, then every safe parent shape as the probe child's own HOME.
    let host = PathBuf::from(env::var_os("HOME").unwrap());
    let mut fixture = Scratch::new(&host, None).unwrap();
    let shapes = SAFE_SHAPES.map(|shape| Some(home_shape(&fixture.root, shape)));
    for home in [None].into_iter().chain(shapes) {
        let mut probe = std::process::Command::new(env!("CARGO_BIN_EXE_journey-tests"));
        if let Some(home) = &home {
            probe.env("HOME", home);
        }
        let result = probe.arg("--test-isolation").output().unwrap();
        assert!(
            result.status.success(),
            "{home:?}: {}",
            String::from_utf8_lossy(&result.stderr)
        );
        let value: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(value["nonloopback_connect"], "PermissionDenied", "{home:?}");
        assert_eq!(value["protected_file_read"], "PermissionDenied", "{home:?}");
        assert_eq!(value["outside_file_write"], "PermissionDenied", "{home:?}");
        assert_eq!(value["loopback_connect"], "PASS", "{home:?}");
    }
    fixture.remove().unwrap();
}
#[test]
fn sandbox_profile_binds_the_path_the_kernel_resolves_for_every_scratch_parent_shape() {
    // Seatbelt matches the resolved vnode path, never a symlinked spelling, and a fresh host
    // has no Hermes tree. Build every shape on every host so neither a spelling-only profile
    // nor a pre-existing `~/.hermes` can pass unnoticed.
    let host = PathBuf::from(env::var_os("HOME").unwrap());
    let mut fixture = Scratch::new(&host, None).unwrap();
    let uid = fs::metadata(&fixture.root).unwrap().uid();
    assert_ne!(fs::metadata("/usr").unwrap().uid(), uid, "run unprivileged");
    for shape in SAFE_SHAPES {
        let home = home_shape(&fixture.root, shape);
        let mut scratch = Scratch::new(&home, None).unwrap_or_else(|e| panic!("{shape}: {e}"));
        let spelled = home.join(".hermes/cache/scratch");
        let base = scratch_base(&home).unwrap();
        assert_eq!(base, kernel_path(&spelled).unwrap(), "{shape}");
        assert_eq!(scratch.root.parent(), Some(base.as_path()), "{shape}");
        for level in [".hermes", ".hermes/cache", ".hermes/cache/scratch"] {
            let meta = fs::metadata(home.join(level)).unwrap();
            assert!(
                meta.is_dir() && meta.uid() == uid && meta.mode() & 0o022 == 0,
                "{shape}:{level}"
            );
        }
        fs::create_dir(home.join("Library")).unwrap();
        fs::write(home.join("Library/secret"), b"secret").unwrap();
        let profile = sandbox_profile(&home, &scratch.root).unwrap();
        // The only write grant is the resolved per-run directory: never the base, HOME or
        // an ancestor, whichever spelling is offered.
        let grant = format!(
            "(allow file-write* (subpath \"{}\") (literal \"/dev/null\"))",
            scratch.root.display()
        );
        assert_eq!(profile.matches("(allow file-write*").count(), 1, "{shape}");
        assert!(profile.contains(&grant), "{shape}: {profile}");
        for wider in [
            &spelled,
            &base,
            &home,
            &kernel_path(&home).unwrap(),
            &fixture.root,
        ] {
            assert_eq!(
                sandbox_profile(&home, wider).err().as_deref(),
                Some("sandbox:scratch_not_owned"),
                "{shape}: {wider:?}"
            );
        }
        let sandboxed = |args: &[&Path]| {
            Command::new("/usr/bin/sandbox-exec")
                .arg("-p")
                .arg(&profile)
                .args(args)
                .env_clear()
                .output()
                .unwrap()
        };
        let secret = home.join("Library/secret");
        assert_eq!(fs::read(&secret).unwrap(), b"secret");
        let read = sandboxed(&["/bin/cat".as_ref(), &secret]);
        assert!(
            !read.status.success()
                && String::from_utf8_lossy(&read.stderr).contains("Operation not permitted"),
            "{shape}: protected read was not denied: {read:?}"
        );
        // Written through the HOME spelling, landed at the resolved per-run path.
        let run = spelled.join(scratch.root.file_name().unwrap());
        let write = sandboxed(&["/usr/bin/touch".as_ref(), &run.join("written")]);
        assert!(
            write.status.success(),
            "{shape}: scratch write denied: {write:?}"
        );
        assert!(scratch.root.join("written").is_file(), "{shape}");
        for outside in [home.join("outside"), spelled.join("outside")] {
            let denied = sandboxed(&["/usr/bin/touch".as_ref(), &outside]);
            assert!(
                !denied.status.success(),
                "{shape}: {outside:?} write allowed"
            );
            assert!(!outside.exists(), "{shape}: {outside:?}");
        }
        scratch.remove().unwrap();
    }
    for (shape, refused) in UNSAFE_SHAPES {
        let home = home_shape(&fixture.root, shape);
        let before = tree(&fixture.root.join(shape));
        assert_eq!(
            Scratch::new(&home, None).err().as_deref(),
            Some(refused),
            "{shape}"
        );
        assert_eq!(tree(&fixture.root.join(shape)), before, "{shape}: changed");
    }
    fixture.remove().unwrap();
}
