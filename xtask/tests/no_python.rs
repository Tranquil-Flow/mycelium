// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask no-python` (CL-L12-05): reject fixtures (each one Python indicator, built here so the
//! repository itself never holds Python) and accept fixtures (look-alikes that are not Python),
//! in repository and bundle mode, plus the CLI's exit codes.
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::atomic::{AtomicUsize, Ordering},
};
use xtask::no_python::{Mode, audit};

static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-no-python-tests")
            .join(format!(
                "{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        fs::create_dir_all(&root).unwrap();
        Self(root)
    }
    fn file(&self, path: &str, bytes: &[u8]) -> &Self {
        let p = self.0.join(path);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, bytes).unwrap();
        self
    }
}
impl Drop for Tree {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
/// A minimal stored ZIP holding one member `name` (local header + central directory + end record).
fn zip(name: &str) -> Vec<u8> {
    let n = u16::try_from(name.len()).unwrap();
    let mut out = Vec::new();
    out.extend_from_slice(b"PK\x03\x04");
    out.extend_from_slice(&[
        20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ]);
    out.extend_from_slice(&n.to_le_bytes());
    out.extend_from_slice(&[0, 0]);
    out.extend_from_slice(name.as_bytes());
    let central = out.len();
    out.extend_from_slice(b"PK\x01\x02");
    out.extend_from_slice(&[
        20, 0, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ]);
    out.extend_from_slice(&n.to_le_bytes());
    out.extend_from_slice(&[0; 16]);
    out.extend_from_slice(name.as_bytes());
    let size = u32::try_from(out.len() - central).unwrap();
    out.extend_from_slice(b"PK\x05\x06");
    out.extend_from_slice(&[0, 0, 0, 0, 1, 0, 1, 0]);
    out.extend_from_slice(&size.to_le_bytes());
    out.extend_from_slice(&u32::try_from(central).unwrap().to_le_bytes());
    out.extend_from_slice(&[0, 0]);
    out
}
fn codes(root: &Path, mode: Mode) -> Vec<(String, &'static str)> {
    audit(root, mode)
        .unwrap()
        .findings
        .into_iter()
        .map(|f| (f.path, f.code))
        .collect()
}

/// Reject fixtures: (fixture, path, bytes, expected finding code). One indicator each.
fn reject_fixtures() -> Vec<(&'static str, &'static str, Vec<u8>, &'static str)> {
    vec![
        (
            "py-source",
            "tools/build.py",
            b"print(1)\n".to_vec(),
            "NP001",
        ),
        (
            "pyc",
            "lib/__init__.cpython-314.pyc",
            vec![0x0d, 0x0a],
            "NP001",
        ),
        ("pyo", "lib/old.pyo", vec![1], "NP001"),
        ("pycache", "lib/__pycache__/m.bin", vec![1], "NP001"),
        (
            "dist-info",
            "site/x-1.0.dist-info/METADATA",
            b"Name: x\n".to_vec(),
            "NP001",
        ),
        (
            "framework",
            "Contents/Frameworks/Python.framework/Versions/3.14/Resources/Info.plist",
            b"<plist/>".to_vec(),
            "NP001",
        ),
        (
            "libpython",
            "Contents/Frameworks/libpython3.14.dylib",
            vec![0xcf, 0xfa, 0xed, 0xfe],
            "NP001",
        ),
        (
            "interpreter",
            "bin/python3.14",
            vec![0xcf, 0xfa, 0xed, 0xfe],
            "NP001",
        ),
        (
            "env-shebang",
            "scripts/run",
            b"#!/usr/bin/env python3\nimport sys\n".to_vec(),
            "NP002",
        ),
        (
            "abs-shebang",
            "scripts/tool.sh",
            b"#!/usr/bin/python -u\n".to_vec(),
            "NP002",
        ),
        (
            "zip-member",
            "release/app.zip",
            zip("pkg/__main__.py"),
            "NP003",
        ),
        (
            "zipapp",
            "release/tool.pyz.bin",
            [b"#!/bin/sh\n".to_vec(), zip("m.pyc")].concat(),
            "NP003",
        ),
        (
            "pyinstaller",
            "release/frozen.bin",
            [
                b"#!/bin/sh\nexit 0\n".to_vec(),
                b"MEI\x0c\x0b\x0a\x0b\x0e".to_vec(),
            ]
            .concat(),
            "NP003",
        ),
    ]
}

#[test]
fn every_reject_fixture_is_found_in_a_tree_and_in_a_bundle() {
    for (name, path, bytes, code) in reject_fixtures() {
        for mode in [Mode::Repository, Mode::Bundle] {
            let tree = Tree::new();
            tree.file("README.md", b"clean neighbour\n")
                .file(path, &bytes);
            let found = codes(&tree.0, mode);
            assert!(
                found
                    .iter()
                    .any(|(p, c)| path.starts_with(p.as_str()) && *c == code),
                "{name} ({mode:?}): expected {code} at {path}, found {found:?}"
            );
            assert!(
                found.iter().all(|(p, _)| p != "README.md"),
                "{name}: {found:?}"
            );
        }
    }
}

#[test]
fn a_symlink_to_an_interpreter_is_a_finding() {
    let tree = Tree::new();
    tree.file("bin/keep", b"x");
    std::os::unix::fs::symlink("/usr/bin/python3", tree.0.join("bin/py")).unwrap();
    assert_eq!(
        codes(&tree.0, Mode::Bundle),
        [("bin/py".to_string(), "NP001")]
    );
}

#[test]
fn runtime_bytes_inside_a_shipped_binary_are_found_in_bundle_mode_only() {
    let tree = Tree::new();
    tree.file(
        "Contents/MacOS/app",
        &[
            &[0xcf, 0xfa, 0xed, 0xfe][..],
            b"....@rpath/libpython3.14.dylib....Py_Initialize",
        ]
        .concat(),
    );
    let found = codes(&tree.0, Mode::Bundle);
    assert_eq!(
        found,
        [
            ("Contents/MacOS/app".to_string(), "NP004"),
            ("Contents/MacOS/app".to_string(), "NP004")
        ]
    );
    // A repository holds prose and sources that name Python without being Python.
    assert_eq!(codes(&tree.0, Mode::Repository), []);
}

/// Accept fixtures: look-alikes that are not Python.
#[test]
fn accept_fixtures_are_clean_in_both_modes() {
    let tree = Tree::new();
    tree.file(
        "docs/python-free.md",
        b"v0.2 has zero Python; v0.1.4 used python3 and libpython.\n",
    )
    .file(
        "scripts/build.sh",
        b"#!/bin/bash\necho python is not used\n",
    )
    .file(
        "data/paths.json",
        b"{\"oracle\": \"mycelium_a13_macos/app.py:84\"}\n",
    )
    .file("src/pyramid.rs", b"// pyramid\n")
    .file("assets/app.zip", &zip("index.html"))
    .file("crates/x/happy.pyramid", b"x")
    .file("Contents/Resources/typing.pyi.txt", b"x");
    assert_eq!(codes(&tree.0, Mode::Repository), []);
    let bundle = Tree::new();
    bundle
        .file(
            "Contents/MacOS/mycelium",
            &[0xcf, 0xfa, 0xed, 0xfe, 0, 1, 2],
        )
        .file("Contents/Resources/licenses/01-LICENSE", b"MIT\n")
        .file("Contents/Info.plist", b"<plist/>");
    assert_eq!(codes(&bundle.0, Mode::Bundle), []);
}

#[test]
fn repository_mode_skips_only_the_root_git_and_target_directories() {
    let tree = Tree::new();
    tree.file(".git/hooks/pre-commit.py", b"x")
        .file("target/debug/build/x/out/gen.py", b"x")
        .file("crates/a/target/leak.py", b"x");
    assert_eq!(
        codes(&tree.0, Mode::Repository),
        [("crates/a/target/leak.py".to_string(), "NP001")]
    );
}

#[test]
fn the_cli_exits_1_on_findings_0_when_clean_and_2_on_a_missing_tree() {
    let clean = Tree::new();
    clean.file("a.txt", b"x");
    let dirty = Tree::new();
    dirty.file("b/setup.py", b"x");
    let run = |args: &[&Path]| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_xtask"));
        command.arg("no-python").arg("--root").arg(args[0]);
        for bundle in &args[1..] {
            command.arg("--bundle").arg(bundle);
        }
        command.output().unwrap()
    };
    let ok = run(&[&clean.0]);
    assert_eq!(ok.status.code(), Some(0), "{ok:?}");
    assert!(String::from_utf8_lossy(&ok.stdout).contains("0 findings"));
    let bad = run(&[&clean.0, &dirty.0]);
    assert_eq!(bad.status.code(), Some(1), "{bad:?}");
    assert!(String::from_utf8_lossy(&bad.stdout).contains("b/setup.py: NP001"));
    let missing = run(&[&clean.0.join("absent")]);
    assert_eq!(missing.status.code(), Some(2), "{missing:?}");
}

// ---- lane RELF item 6 (review F3): the blind spots the review planted ------------------------

/// A gzip-compressed tar holding `members` (each a small file), made by the system `tar`.
fn tar_gz(tree: &Tree, name: &str, members: &[&str]) -> Vec<u8> {
    let src = tree.0.join(format!("{name}-src"));
    for member in members {
        let p = src.join(member);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, b"print(1)\n").unwrap();
    }
    let out = tree.0.join(format!("{name}.tmp"));
    let status = Command::new("/usr/bin/tar")
        .arg("-czf")
        .arg(&out)
        .arg("-C")
        .arg(&src)
        .args(members)
        .status()
        .unwrap();
    assert!(status.success());
    let bytes = fs::read(&out).unwrap();
    fs::remove_file(out).unwrap();
    fs::remove_dir_all(src).unwrap();
    bytes
}

#[test]
fn the_reviewers_blind_spots_are_found_in_a_tree_and_in_a_bundle() {
    for mode in [Mode::Repository, Mode::Bundle] {
        let scratch = Tree::new();
        let archive = tar_gz(&scratch, "a", &["pkg/x.py"]);
        let cases: Vec<(&str, Vec<u8>, &str)> = vec![
            (
                "tools/run",
                b"import os\n\ndef main():\n    print(os.getcwd())\n".to_vec(),
                "NP005",
            ),
            (
                "tools/gen",
                b"from pathlib import Path\nPath('x').write_text('y')\n".to_vec(),
                "NP005",
            ),
            (
                "scripts/build.sh",
                b"#!/bin/bash\nset -e\npython3 - <<EOF\nprint(1)\nEOF\n".to_vec(),
                "NP006",
            ),
            (
                "scripts/Install.command",
                b"#!/bin/sh\ncd \"$(dirname \"$0\")\" && exec /usr/bin/env python3 -m pkg\n"
                    .to_vec(),
                "NP006",
            ),
            ("release/src.tar.gz", archive.clone(), "NP003"),
            ("release/src.tgz.bin", archive, "NP003"),
            ("notebooks/a.ipynb", b"{\"cells\": []}\n".to_vec(), "NP001"),
            (
                "pyproject.toml",
                b"[project]\nname = \"x\"\n".to_vec(),
                "NP001",
            ),
            ("requirements.txt", b"mlx==0.29\n".to_vec(), "NP001"),
            (
                "release/prefixed.bin",
                [vec![b'x'; 600], zip("pkg/__main__.py")].concat(),
                "NP003",
            ),
        ];
        for (path, bytes, code) in cases {
            let tree = Tree::new();
            tree.file("README.md", b"clean neighbour\n")
                .file(path, &bytes);
            let found = codes(&tree.0, mode);
            assert!(
                found.iter().any(|(p, c)| p == path && *c == code),
                "{path} ({mode:?}): expected {code}, found {found:?}"
            );
            assert!(found.iter().all(|(p, _)| p != "README.md"), "{found:?}");
        }
    }
}

#[test]
fn look_alikes_of_the_new_indicators_are_clean() {
    let tree = Tree::new();
    tree.file("NOTES", b"important: import the key\nclass notes follow\n")
        .file("LICENSE", b"Permission is hereby granted, from the authors\n")
        .file(
            "scripts/check.sh",
            b"#!/bin/bash\n# no python3 here (v0.1.4 used python3)\necho \"python is gone\"\npythonic=1\n",
        )
        .file(
            "assets/app.js",
            b"import { a } from './b.js';\nexport default a;\n",
        )
        .file("src/main.rs", b"use std::fs;\nfn main() {}\n")
        .file("data/plain.tar.gz", &tar_gz(&tree, "p", &["index.html"]));
    assert_eq!(codes(&tree.0, Mode::Repository), []);
    assert_eq!(codes(&tree.0, Mode::Bundle), []);
}

#[test]
fn an_empty_tree_or_bundle_is_refused_not_passed() {
    let empty = Tree::new();
    let error = audit(&empty.0, Mode::Bundle).unwrap_err();
    assert!(error.contains("no files"), "{error}");
    let error = audit(&empty.0, Mode::Repository).unwrap_err();
    assert!(error.contains("no files"), "{error}");
    let clean = Tree::new();
    clean.file("a.txt", b"x");
    let out = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .arg("no-python")
        .arg("--root")
        .arg(&clean.0)
        .arg("--bundle")
        .arg(&empty.0)
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2), "{out:?}");
}

#[test]
fn the_shipped_no_python_cell_runs_on_the_frozen_bundle_only() {
    let profile: serde_json::Value = serde_json::from_str(
        &fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("release-profile.json"))
            .unwrap(),
    )
    .unwrap();
    let cell = profile["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == "no-python")
        .cloned()
        .unwrap();
    assert_eq!(cell["verify"]["argv_is_hint"], true, "{cell}");
    let hint: Vec<&str> = cell["hint"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| h.as_str().unwrap())
        .collect();
    assert!(
        hint.windows(2).any(|w| w == ["--bundle", "<bundle>"]),
        "{hint:?}"
    );
}
