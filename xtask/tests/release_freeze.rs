// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask release freeze` (CL-L12-01, CL-DOD-08): a candidate manifest is produced only from a
//! clean checkout AT the candidate commit, binds the real exe and bundle digests, is never
//! overwritten, and is what `release candidate` then binds evidence to.
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};
use xtask::release;

static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Sandbox {
    root: PathBuf,
    head: String,
}
fn git_in(repo: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
        .args(args)
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    String::from_utf8(out.stdout).unwrap()
}
impl Sandbox {
    fn new() -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-release-freeze-tests")
            .join(format!(
                "{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        let repo = root.join("repo");
        fs::create_dir_all(&repo).unwrap();
        fs::write(repo.join("README"), "source\n").unwrap();
        let git = |args: &[&str]| git_in(&repo, args);
        git(&["init", "-q"]);
        git(&["add", "README"]);
        git(&["commit", "-q", "-m", "x"]);
        let head = git(&["rev-parse", "HEAD"]).trim().to_string();
        let app = root.join("out/Mycelium.app/Contents/MacOS");
        fs::create_dir_all(&app).unwrap();
        fs::write(app.join("mycelium"), "exe bytes").unwrap();
        fs::write(app.join("../Info.plist"), "<plist/>").unwrap();
        Self { root, head }
    }
    fn app(&self) -> PathBuf {
        self.root.join("out/Mycelium.app")
    }
    fn exe(&self) -> PathBuf {
        self.app().join("Contents/MacOS/mycelium")
    }
    fn freeze(&self, sha: &str, out: &Path) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "freeze", sha, "--repo"])
            .arg(self.root.join("repo"))
            .arg("--exe")
            .arg(self.exe())
            .arg("--bundle")
            .arg(self.app())
            .arg("--out")
            .arg(out)
            .output()
            .unwrap()
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}
fn text(out: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

#[test]
fn freeze_binds_the_commit_the_exe_and_the_bundle_bytes() {
    let sandbox = Sandbox::new();
    let out = sandbox.root.join("candidate.json");
    let frozen = sandbox.freeze(&sandbox.head, &out);
    assert_eq!(frozen.status.code(), Some(0), "{}", text(&frozen));
    let manifest = release::Manifest::load(&out).unwrap();
    assert_eq!(manifest.candidate.source_commit, sandbox.head);
    assert_eq!(
        manifest.candidate.exe_sha256,
        release::file_sha256(&sandbox.exe()).unwrap()
    );
    assert_eq!(
        manifest.candidate.bundle_sha256,
        release::bundle_digest(&sandbox.app()).unwrap()
    );
    assert!(text(&frozen).contains(&manifest.sha256));
    assert_eq!(manifest.verify_bytes(), Ok(()));
    // A later byte change is visible to every consumer of the manifest.
    fs::write(
        sandbox.app().join("Contents/Info.plist"),
        "<plist> </plist>",
    )
    .unwrap();
    assert!(
        manifest
            .verify_bytes()
            .unwrap_err()
            .contains("package_bundle_mismatch")
    );
}

#[test]
fn freeze_binds_a_relative_link_inside_the_bundle_and_refuses_links_out_of_it() {
    // The real bundle has v0.1.4's signable Metal layout (c85059e): the metallib bytes live in
    // `Contents/Resources` and `Contents/Frameworks/mlx.metallib` is the relative link
    // `../Resources/mlx.metallib`. A candidate must be freezable, and the link's target is part
    // of the frozen bytes.
    use std::os::unix::fs::symlink;
    let sandbox = Sandbox::new();
    let contents = sandbox.app().join("Contents");
    fs::create_dir_all(contents.join("Resources")).unwrap();
    fs::create_dir_all(contents.join("Frameworks")).unwrap();
    fs::write(contents.join("Resources/mlx.metallib"), "metal").unwrap();
    fs::write(contents.join("Resources/other.metallib"), "metal").unwrap();
    let link = contents.join("Frameworks/mlx.metallib");
    symlink("../Resources/mlx.metallib", &link).unwrap();
    let out = sandbox.root.join("candidate.json");
    let frozen = sandbox.freeze(&sandbox.head, &out);
    assert_eq!(frozen.status.code(), Some(0), "{}", text(&frozen));
    let manifest = release::Manifest::load(&out).unwrap();
    assert_eq!(manifest.verify_bytes(), Ok(()));
    // Same bytes behind another target: a different candidate.
    fs::remove_file(&link).unwrap();
    symlink("../Resources/other.metallib", &link).unwrap();
    assert!(
        manifest
            .verify_bytes()
            .unwrap_err()
            .contains("package_bundle_mismatch")
    );
    // A link that leaves the bundle is refused, never followed.
    for target in ["/etc/hosts", "../../../outside", "../Resources/../../../x"] {
        fs::remove_file(&link).unwrap();
        symlink(target, &link).unwrap();
        let refused = release::bundle_digest(&sandbox.app()).unwrap_err();
        assert!(
            refused.contains("bundle_link_outside"),
            "{target}: {refused}"
        );
    }
}

#[test]
fn freeze_refuses_another_commit_a_dirty_tree_and_an_existing_manifest() {
    let sandbox = Sandbox::new();
    let out = sandbox.root.join("candidate.json");
    let other = "0".repeat(40);
    let wrong = sandbox.freeze(&other, &out);
    assert_eq!(wrong.status.code(), Some(2), "{}", text(&wrong));
    assert!(
        text(&wrong).contains("freeze_source_mismatch"),
        "{}",
        text(&wrong)
    );
    fs::write(sandbox.root.join("repo/untracked.txt"), "x").unwrap();
    let dirty = sandbox.freeze(&sandbox.head, &out);
    assert!(
        text(&dirty).contains("freeze_tree_dirty"),
        "{}",
        text(&dirty)
    );
    assert!(!out.exists(), "a refused freeze writes nothing");
    fs::remove_file(sandbox.root.join("repo/untracked.txt")).unwrap();
    fs::write(&out, "{}").unwrap();
    let exists = sandbox.freeze(&sandbox.head, &out);
    assert!(
        text(&exists).contains("freeze_out_unwritable"),
        "{}",
        text(&exists)
    );
    assert_eq!(fs::read_to_string(&out).unwrap(), "{}", "never overwritten");
}

/// Lane RELF item 7 (review F5, from its scratch demonstration in
/// `qualification/review-laneREL-bd03439/`): the link check was lexical, so `Contents/esc ->
/// up/../..` with `Contents/up -> ..` passed although it resolves outside the bundle. A link is
/// now resolved through the links it passes (never read through), and must stay inside; a
/// dangling chain that would land outside is refused too. Freeze also refuses an executable that
/// is not the bundle's own `Contents/MacOS/mycelium`.
#[test]
fn freeze_refuses_a_chained_link_that_escapes_the_bundle_and_an_exe_outside_it() {
    use std::os::unix::fs::symlink;
    let sandbox = Sandbox::new();
    let contents = sandbox.app().join("Contents");
    symlink("..", contents.join("up")).unwrap();
    // `up` alone resolves to the bundle root: inside, accepted (its target text is frozen).
    let out = sandbox.root.join("ok.json");
    let frozen = sandbox.freeze(&sandbox.head, &out);
    assert_eq!(frozen.status.code(), Some(0), "{}", text(&frozen));
    for (name, target) in [
        ("esc", "up/../.."),
        ("deep", "up/../../out/Mycelium.app/../../x"),
        ("dangling", "up/../missing-outside"),
    ] {
        let link = contents.join(name);
        symlink(target, &link).unwrap();
        let refused = release::bundle_digest(&sandbox.app()).unwrap_err();
        assert!(
            refused.contains("bundle_link_outside"),
            "{name} -> {target}: {refused}"
        );
        let out = sandbox.root.join(format!("{name}.json"));
        let frozen = sandbox.freeze(&sandbox.head, &out);
        assert_ne!(frozen.status.code(), Some(0), "{name}: {}", text(&frozen));
        assert!(!out.exists());
        fs::remove_file(&link).unwrap();
    }
    // An executable outside the bundle (or another file in it) is not the candidate's exe.
    let stray = sandbox.root.join("stray-exe");
    fs::write(&stray, "exe bytes").unwrap();
    fs::write(sandbox.app().join("Contents/Info2.plist"), "x").unwrap();
    for exe in [stray, sandbox.app().join("Contents/Info2.plist")] {
        let out = sandbox.root.join("stray.json");
        let frozen = Command::new(env!("CARGO_BIN_EXE_xtask"))
            .args(["release", "freeze", &sandbox.head, "--repo"])
            .arg(sandbox.root.join("repo"))
            .arg("--exe")
            .arg(&exe)
            .arg("--bundle")
            .arg(sandbox.app())
            .arg("--out")
            .arg(&out)
            .output()
            .unwrap();
        assert_eq!(frozen.status.code(), Some(2), "{}", text(&frozen));
        assert!(
            text(&frozen).contains("freeze_exe_outside_bundle"),
            "{}",
            text(&frozen)
        );
        assert!(!out.exists());
    }
}
