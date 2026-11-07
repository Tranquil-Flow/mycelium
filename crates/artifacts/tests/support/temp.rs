// SPDX-License-Identifier: AGPL-3.0-only
//! Test-only private scratch. Canonicalize the caller's temporary parent, never
//! relax the production cache's no-alias/no-symlink path admission.
use std::{
    fs,
    os::unix::fs::{DirBuilderExt, symlink},
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
pub fn directory(label: &str) -> PathBuf {
    let base = fs::canonicalize(std::env::temp_dir()).unwrap();
    let root = base.join(format!(
        "v02-l7-{label}-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed),
    ));
    fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
    root
}
#[test]
fn symlinked_tmpdir_runs_existing_tests_without_weakening_cache_admission() {
    let root = directory("tmpdir-regression");
    let real = root.join("real");
    fs::DirBuilder::new().mode(0o700).create(&real).unwrap();
    let alias = root.join("alias");
    symlink(&real, &alias).unwrap();
    // Per-child environment avoids unsafe process-global set_var races. Run the
    // existing behavioral tests, excluding only this subprocess trampoline.
    let result = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--skip", "symlinked_tmpdir", "--nocapture"])
        .env("TMPDIR", &alias)
        .output();
    fs::remove_dir_all(&root).unwrap();
    let output = result.unwrap();
    assert!(
        output.status.success(),
        "symlinked TMPDIR child failed:\n{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
