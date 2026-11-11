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
