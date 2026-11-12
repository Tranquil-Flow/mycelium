// SPDX-License-Identifier: AGPL-3.0-only
//! Reproducible packaged build: the worktree path must not reach the executable. tauri bakes the
//! absolute `CARGO_MANIFEST_DIR` into it as a string literal, so canonical builds go through one
//! fixed symlink (`repro/canonical.sh`). Fast proof with a tiny crate that embeds the same
//! `env!("CARGO_MANIFEST_DIR")`: two trees at different paths and target dirs give byte-identical
//! binaries through the link and different ones without it (the control that makes the check
//! meaningful). The ignored test runs the real thing: the full bundle built twice.
#![cfg(target_os = "macos")]
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};

fn repro_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("repro")
}

fn tiny_tree(base: &Path, rel: &str) -> PathBuf {
    let tree = base.join(rel);
    fs::create_dir_all(tree.join("src")).unwrap();
    fs::write(
        tree.join("Cargo.toml"),
        "[package]\nname = \"canon-probe\"\nversion = \"0.0.0\"\nedition = \"2021\"\n",
    )
    .unwrap();
    fs::write(
        tree.join("src/main.rs"),
        "fn main() { println!(\"{}\", env!(\"CARGO_MANIFEST_DIR\")); }\n",
    )
    .unwrap();
    fs::canonicalize(tree).unwrap()
}

fn cargo_build(tree: &Path, target: &Path, canon: Option<&Path>) -> Vec<u8> {
    let manifest = canon.unwrap_or(tree).join("Cargo.toml");
    let out = Command::new("cargo")
        .args(["build", "--release", "--offline", "--manifest-path"])
        .arg(&manifest)
        .current_dir(tree)
        .env("CARGO_TARGET_DIR", target)
        .env_remove("CARGO_MANIFEST_DIR")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    fs::read(target.join("release/canon-probe")).unwrap()
}

fn through_the_link(canon: &Path, tree: &Path, target: &Path) -> Vec<u8> {
    let build = format!(
        ". '{}/canonical.sh' && canon_acquire '{}' && trap canon_release EXIT && cd '{}' && \
         cargo build --release --offline --manifest-path \"$MYCELIUM_CANON/Cargo.toml\"",
        repro_dir().display(),
        tree.display(),
        tree.display()
    );
    let out = Command::new("bash")
        .args(["-c", &build])
        .env("MYCELIUM_CANONICAL_SRC", canon)
        .env("CARGO_TARGET_DIR", target)
        .env_remove("CARGO_MANIFEST_DIR")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!canon.exists(), "link must be released");
    assert!(!canon.with_extension("lock").exists());
    fs::read(target.join("release/canon-probe")).unwrap()
}

fn contains(hay: &[u8], needle: &str) -> bool {
    hay.windows(needle.len()).any(|w| w == needle.as_bytes())
}

#[test]
fn trees_at_different_paths_build_identical_binaries_through_the_canonical_link_only() {
    let base = std::env::temp_dir().join(format!("v02-l11-canon-{}", std::process::id()));
    let a = tiny_tree(&base, "wt-a/src");
    let b = tiny_tree(&base, "elsewhere/much-deeper/worktree-b/src");
    let canon = base.join("canonical-src");
    let (ta, tb) = (base.join("target-a"), base.join("target-b"));

    // control: plain builds embed the worktree path, so the binaries differ
    let (pa, pb) = (cargo_build(&a, &ta, None), cargo_build(&b, &tb, None));
    assert!(contains(&pa, a.to_str().unwrap()) && contains(&pb, b.to_str().unwrap()));
    assert_ne!(
        pa, pb,
        "control: plain builds from different paths must differ"
    );

    // canonical: identical bytes, only the fixed link path inside, and the link is released
    let (ca, cb) = (
        through_the_link(&canon, &a, &base.join("target-ca")),
        through_the_link(&canon, &b, &base.join("target-cb")),
    );
    assert_eq!(
        ca, cb,
        "canonical builds from different paths must be byte-identical"
    );
    assert!(contains(&ca, canon.to_str().unwrap()));
    assert!(!contains(&ca, a.to_str().unwrap()) && !contains(&cb, b.to_str().unwrap()));
    fs::remove_dir_all(&base).unwrap();
}

#[test]
fn a_stale_lock_of_a_dead_owner_is_broken_and_a_real_directory_is_never_replaced() {
    let base = std::env::temp_dir().join(format!("v02-l11-canon-lock-{}", std::process::id()));
    fs::create_dir_all(&base).unwrap();
    let tree = tiny_tree(&base, "t");
    let canon = base.join("canonical-src");
    let lock = base.join("canonical-src.lock");
    fs::create_dir(&lock).unwrap();
    fs::write(lock.join("pid"), "999999").unwrap(); // not a live pid on this host
    let script = format!(
        ". '{}/canonical.sh' && canon_acquire '{}' && test -L \"$MYCELIUM_CANON\" && canon_release && echo broken-and-released",
        repro_dir().display(),
        tree.display()
    );
    let run = |canon: &Path| {
        Command::new("bash")
            .args(["-c", &script])
            .env("MYCELIUM_CANONICAL_SRC", canon)
            .output()
            .unwrap()
    };
    let out = run(&canon);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!canon.exists() && !lock.exists());
    // a real directory at the canonical path is refused, untouched
    fs::create_dir(&canon).unwrap();
    let out = run(&canon);
    assert!(!out.status.success());
    assert!(canon.is_dir() && !canon.is_symlink());
    fs::remove_dir_all(&base).unwrap();
}

/// Lane RELF item 5 (review F4, REL-R1): every builder-specific path must stay out of the build's
/// inputs. Sourcing the recipe's environment for two builders (different HOME, different-length
/// target dirs, different cargo homes) must give the same canonical path and the same RUSTFLAGS
/// (RUSTFLAGS text changes the output), and neither may name a HOME or a target dir. The flags
/// keep LC_UUID (`-no_uuid` is not used) and drop the linker debug map (`-Wl,-S`), whose object
/// paths made the UUID depend on where objects were built.
#[test]
fn the_recipe_environment_is_the_same_for_two_builders() {
    let show = |home: &str, target: &str, cargo_home: &str| -> String {
        let script = format!(
            ". '{}/env.sh' && printf 'CANON=%s\\nRUSTFLAGS=%s\\n' \"$MYCELIUM_CANON\" \"$RUSTFLAGS\"",
            repro_dir().display()
        );
        let out = Command::new("bash")
            .args(["-c", &script])
            .env("HOME", home)
            .env("CARGO_TARGET_DIR", target)
            .env("CARGO_HOME", cargo_home)
            .env("MLXC_INCLUDE_DIR", "/pinned/include")
            .env("MLXC_LIB_DIR", "/pinned/lib")
            .env_remove("MYCELIUM_CANONICAL_SRC")
            .env_remove("RUSTFLAGS")
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap()
    };
    let a = show("/Users/ab", "/Users/ab/t", "/Users/ab/.cargo");
    let b = show(
        "/Users/another-builder",
        "/Volumes/build/much/longer/target-dir",
        "/opt/cargo-home",
    );
    assert_eq!(a, b, "the recipe environment depends on the builder");
    for needle in [
        "/Users/ab",
        "another-builder",
        "/t\n",
        "target-dir",
        "cargo-home\n",
    ] {
        assert!(!a.contains(needle), "{needle} in {a}");
    }
    assert!(a.contains("-Wl,-S") && !a.contains("no_uuid"), "{a}");
}

/// Heavy: two full release builds of the committed HEAD from two copies at different paths with
/// different target dirs, sequentially. `MYCELIUM_REPRO_SCRATCH=<dir under ~/.hermes/cache/scratch>`.
#[test]
#[ignore = "two release builds; set MYCELIUM_REPRO_SCRATCH"]
fn two_path_builds_of_the_same_commit_are_byte_identical() {
    let scratch = std::env::var("MYCELIUM_REPRO_SCRATCH").expect("MYCELIUM_REPRO_SCRATCH");
    let mut cmd = Command::new(repro_dir().join("verify-two-paths.sh"));
    cmd.arg(scratch);
    let out = cmd.output().unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success(),
        "{text}\n{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(text.contains("REPRODUCIBLE:"), "{text}");
}
