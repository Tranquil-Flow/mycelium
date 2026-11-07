// SPDX-License-Identifier: AGPL-3.0-only
//! `cargo xtask quick`: affected-crate computation (pure, from a `cargo metadata` fixture), git change
//! collection, and the command line end to end against a throw-away workspace under `target/`.
use std::{
    collections::BTreeSet,
    fs,
    path::PathBuf,
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};
use xtask::quick::{self, Workspace};

/// A miniature of the real layout: `core <- mid <- leaf`, `tools` (test-support) dev-depends on
/// `core`, `core-extra` shares a name prefix with `core`, `solo` stands alone, and `xtask` is last.
/// `leaf` also depends on a registry crate and a crate may name itself; neither is a workspace edge.
fn fixture() -> Workspace {
    let package = |name: &str, dir: &str, deps: &[&str]| {
        let dependencies: Vec<String> = deps
            .iter()
            .map(|dep| format!(r#"{{"name":"{dep}","kind":null,"req":"*"}}"#))
            .collect();
        format!(
            r#"{{"name":"{name}","id":"path+file:///ws/{dir}#{name}@0.2.0-dev","manifest_path":"/ws/{dir}/Cargo.toml","dependencies":[{}],"targets":[]}}"#,
            dependencies.join(",")
        )
    };
    let packages = [
        package("core", "crates/core", &[]),
        package("mid", "crates/mid", &["core"]),
        package("leaf", "crates/leaf", &["mid", "serde", "leaf"]),
        package("tools", "test-support/tools", &["core"]),
        package("core-extra", "crates/core-extra", &[]),
        package("solo", "crates/solo", &[]),
        package("xtask", "xtask", &["serde"]),
    ];
    let json = format!(
        r#"{{"packages":[{}],"workspace_root":"/ws","version":1}}"#,
        packages.join(",")
    );
    Workspace::from_metadata(&json).unwrap()
}

fn files(paths: &[&str]) -> Vec<String> {
    paths.iter().map(|p| (*p).to_string()).collect()
}
fn set(names: &[&str]) -> BTreeSet<String> {
    names.iter().map(|n| (*n).to_string()).collect()
}

#[test]
fn touching_a_leaf_crate_selects_only_that_crate() {
    let affected = fixture().affected(&files(&["crates/leaf/src/lib.rs"]));
    assert_eq!(affected.changed, set(&["leaf"]));
    assert_eq!(affected.crates, set(&["leaf"]));
    assert!(!affected.everything);
}

#[test]
fn touching_a_dependency_adds_every_reverse_dependency_transitively() {
    let affected = fixture().affected(&files(&["crates/core/src/lib.rs"]));
    assert_eq!(affected.changed, set(&["core"]));
    // mid (direct), leaf (through mid), tools (a test-support crate depending on core).
    assert_eq!(affected.crates, set(&["core", "mid", "leaf", "tools"]));
    let middle = fixture().affected(&files(&["crates/mid/tests/t.rs"]));
    assert_eq!(middle.crates, set(&["mid", "leaf"]));
}

#[test]
fn a_shared_name_prefix_does_not_make_two_crates_one() {
    let affected = fixture().affected(&files(&["crates/core-extra/src/lib.rs"]));
    assert_eq!(affected.crates, set(&["core-extra"]));
    assert_eq!(fixture().owner_of("crates/core"), Some("core"));
    assert_eq!(fixture().owner_of("crates/cor"), None);
    // A directory that merely starts with a crate's name is not that crate (and not a crate at all).
    assert_eq!(fixture().owner_of("crates/core-notes/README.md"), None);
    assert!(
        fixture()
            .affected(&files(&["crates/core-notes/README.md"]))
            .crates
            .is_empty()
    );
}

#[test]
fn several_changes_union_and_files_outside_crates_select_nothing() {
    let affected = fixture().affected(&files(&[
        "xtask/src/main.rs",
        "crates/solo/Cargo.toml",
        "AGENTS.md",
        "evidence/l1/receipt.json",
        "runs/x.log",
        ".config/nextest.toml",
    ]));
    assert_eq!(affected.crates, set(&["xtask", "solo"]));
    let none = fixture().affected(&files(&["AGENTS.md", "README.md", "runs/x.log"]));
    assert!(none.crates.is_empty() && none.changed.is_empty());
    assert!(fixture().affected(&[]).crates.is_empty());
}

#[test]
fn a_root_build_file_selects_the_whole_workspace() {
    for global in quick::GLOBAL_FILES {
        let affected = fixture().affected(&files(&[global]));
        assert!(affected.everything, "{global}");
        assert_eq!(affected.crates, fixture().names(), "{global}");
    }
    // The same file name inside a crate is that crate's own file, not a root build file.
    let nested = fixture().affected(&files(&["crates/leaf/Cargo.toml"]));
    assert!(!nested.everything);
    assert_eq!(nested.crates, set(&["leaf"]));
}

#[test]
fn the_longest_directory_owns_a_path_when_a_root_package_exists() {
    let json = r#"{"workspace_root":"/ws","packages":[
        {"name":"top","manifest_path":"/ws/Cargo.toml","dependencies":[]},
        {"name":"sub","manifest_path":"/ws/sub/Cargo.toml","dependencies":[{"name":"top"}]}]}"#;
    let workspace = Workspace::from_metadata(json).unwrap();
    assert_eq!(workspace.owner_of("sub/src/lib.rs"), Some("sub"));
    assert_eq!(workspace.owner_of("src/lib.rs"), Some("top"));
    assert_eq!(
        workspace.affected(&files(&["src/lib.rs"])).crates,
        set(&["top", "sub"])
    );
}

#[test]
fn malformed_metadata_is_an_error_not_an_empty_selection() {
    assert!(
        Workspace::from_metadata("not json")
            .unwrap_err()
            .starts_with("QUICK: cargo metadata")
    );
    assert!(
        Workspace::from_metadata(r#"{"workspace_root":"/ws","packages":[]}"#)
            .unwrap_err()
            .contains("no workspace crates")
    );
    let outside = r#"{"workspace_root":"/ws","packages":[
        {"name":"x","manifest_path":"/elsewhere/x/Cargo.toml","dependencies":[]}]}"#;
    assert!(
        Workspace::from_metadata(outside)
            .unwrap_err()
            .contains("outside workspace root")
    );
}

#[test]
fn nul_separated_git_output_is_split_and_a_bad_path_is_refused() {
    assert_eq!(
        quick::split_nul(b"a/b.rs\0c d.rs\0").unwrap(),
        files(&["a/b.rs", "c d.rs"])
    );
    assert!(quick::split_nul(b"").unwrap().is_empty());
    assert!(
        quick::split_nul(b"ok.rs\0\xff\xfe.rs\0")
            .unwrap_err()
            .contains("not UTF-8")
    );
}

#[test]
fn the_steps_are_fmt_then_clippy_then_nextest_on_the_selected_crates() {
    let steps = quick::steps(&set(&["mid", "core"]));
    let rendered: Vec<(&str, String)> = steps
        .iter()
        .map(|(label, args)| (*label, args.join(" ")))
        .collect();
    assert_eq!(
        rendered,
        vec![
            ("fmt", "fmt --check".to_string()),
            (
                "clippy",
                "clippy -p core -p mid --all-targets -- -D warnings".to_string()
            ),
            (
                "nextest",
                "nextest run --profile quick --no-tests=warn -p core -p mid".to_string()
            ),
        ]
    );
    // With nothing selected only the format check remains.
    assert_eq!(quick::steps(&BTreeSet::new()).len(), 1);
}

static NEXT: AtomicUsize = AtomicUsize::new(0);
/// A throw-away git workspace with real `Cargo.toml` files so `cargo metadata` can read it.
struct Repo(PathBuf);
impl Repo {
    fn new() -> Self {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-tests")
            .join(format!(
                "quick-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        fs::create_dir_all(&path).unwrap();
        let repo = Self(path);
        repo.write(
            "Cargo.toml",
            "[workspace]\nresolver = \"3\"\nmembers = [\"crates/*\"]\n",
        );
        for (name, deps) in [
            ("base", ""),
            ("user", "base = { path = \"../base\" }\n"),
            ("other", ""),
        ] {
            repo.write(
                &format!("crates/{name}/Cargo.toml"),
                &format!(
                    "[package]\nname = \"{name}\"\nversion = \"0.0.0\"\nedition = \"2024\"\n\n[dependencies]\n{deps}"
                ),
            );
            repo.write(&format!("crates/{name}/src/lib.rs"), "pub fn f() {}\n");
        }
        repo.write("AGENTS.md", "notes\n");
        repo.write(".gitignore", "/target\n");
        repo.git(&["init", "-q", "-b", "main"]);
        // A tracked lockfile, as in the real workspace; otherwise `cargo metadata` would create an
        // untracked one and `quick` would rightly select everything.
        let lock = Command::new(env!("CARGO"))
            .current_dir(&repo.0)
            .args(["generate-lockfile", "--offline"])
            .output()
            .unwrap();
        assert!(
            lock.status.success(),
            "{}",
            String::from_utf8_lossy(&lock.stderr)
        );
        repo.commit("base");
        repo
    }
    fn write(&self, name: &str, text: &str) {
        let path = self.0.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    fn git(&self, args: &[&str]) -> Output {
        let output = Command::new("git")
            .arg("-C")
            .arg(&self.0)
            .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
            .args([
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
            ])
            .args(args)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        output
    }
    fn commit(&self, message: &str) {
        self.git(&["add", "-A"]);
        self.git(&["commit", "-q", "-m", message]);
    }
    fn quick(&self, extra: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .arg("quick")
            .arg("--root")
            .arg(&self.0)
            .args(extra)
            .output()
            .unwrap()
    }
}
impl Drop for Repo {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

#[test]
fn changed_files_cover_commits_staged_unstaged_untracked_and_deleted() {
    let repo = Repo::new();
    repo.git(&["checkout", "-q", "-b", "work"]);
    repo.write("crates/base/src/lib.rs", "pub fn f() { /* committed */ }\n");
    repo.commit("committed change");
    repo.write("crates/other/src/extra.rs", "pub fn g() {}\n");
    repo.git(&["add", "crates/other/src/extra.rs"]);
    repo.write("crates/user/src/lib.rs", "pub fn f() { /* unstaged */ }\n");
    repo.write("crates/new/src/lib.rs", "pub fn n() {}\n");
    repo.write("target/ignored.rs", "// ignored by .gitignore\n");
    fs::remove_file(repo.0.join("AGENTS.md")).unwrap();
    let (merge_base, changed) = quick::changed_files(&repo.0, "main").unwrap();
    assert_eq!(merge_base.len(), 40);
    assert_eq!(
        changed,
        files(&[
            "AGENTS.md",
            "crates/base/src/lib.rs",
            "crates/new/src/lib.rs",
            "crates/other/src/extra.rs",
            "crates/user/src/lib.rs",
        ])
    );
}

#[test]
fn an_unknown_base_is_an_error_not_an_empty_diff() {
    let repo = Repo::new();
    let error = quick::changed_files(&repo.0, "no-such-branch").unwrap_err();
    assert!(error.starts_with("QUICK: git merge-base"), "{error}");
}

#[test]
fn list_selects_a_dependency_and_its_dependents_from_real_git_and_cargo() {
    let repo = Repo::new();
    repo.git(&["checkout", "-q", "-b", "work"]);
    repo.write("crates/base/src/lib.rs", "pub fn f() { /* edited */ }\n");
    let output = repo.quick(&["--list"]);
    assert_eq!(output.status.code(), Some(0), "{}", stdout(&output));
    let text = stdout(&output);
    assert!(text.contains("2 of 3 crates selected"), "{text}");
    assert!(text.contains("quick:   base (changed)"), "{text}");
    assert!(
        text.contains("quick:   user (depends on a changed crate)"),
        "{text}"
    );
    assert!(!text.contains("other"), "{text}");
    // `--base HEAD` ignores the committed history and sees only the uncommitted edit.
    repo.commit("edit");
    repo.write("crates/other/src/lib.rs", "pub fn f() { /* later */ }\n");
    let later = stdout(&repo.quick(&["--list", "--base", "HEAD"]));
    assert!(later.contains("1 of 3 crates selected"), "{later}");
    assert!(later.contains("quick:   other (changed)"), "{later}");
}

#[test]
fn usage_errors_exit_two_and_name_the_option() {
    let repo = Repo::new();
    for bad in [&["--bogus"][..], &["--base"], &["--list", "--list"]] {
        let output = repo.quick(bad);
        assert_eq!(output.status.code(), Some(2), "{bad:?}");
        let message = String::from_utf8_lossy(&output.stderr).into_owned();
        assert!(message.contains("USAGE: cargo xtask quick"), "{message}");
    }
    let unknown = repo.quick(&["--list", "--base", "no-such-branch"]);
    assert_eq!(unknown.status.code(), Some(2));
}
