// SPDX-License-Identifier: AGPL-3.0-only
use desktop::package::*;
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicUsize, Ordering},
};
static SEQ: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l11-package-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Tree {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn fixture(root: &Tree) -> Manifest {
    let entries = [
        (
            "Contents/MacOS/mycelium",
            Kind::Executable,
            b"test exe".as_slice(),
        ),
        (
            "Contents/Frameworks/libmlxc.dylib",
            Kind::Dylib,
            b"test mlxc".as_slice(),
        ),
        (
            "Contents/Frameworks/libmlx.dylib",
            Kind::Dylib,
            b"test mlx".as_slice(),
        ),
        (
            "Contents/Frameworks/mlx.metallib",
            Kind::Metal,
            b"test metal".as_slice(),
        ),
    ]
    .into_iter()
    .map(|(path, kind, bytes)| {
        let p = root.0.join(path);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, bytes).unwrap();
        Entry {
            path: path.into(),
            kind,
            bytes: bytes.len() as u64,
            sha256: format!("{:x}", Sha256::digest(bytes)),
        }
    })
    .collect();
    Manifest { schema: 1, entries }
}
#[test]
fn relocatable_manifest_refuses_missing_and_corrupt_native_files() {
    for target in [
        "Contents/Frameworks/libmlx.dylib",
        "Contents/Frameworks/libmlxc.dylib",
        "Contents/Frameworks/mlx.metallib",
    ] {
        let root = Tree::new();
        let m = fixture(&root);
        assert_eq!(verify(&root.0, &m), Ok(()));
        fs::write(root.0.join(target), b"corrupt").unwrap();
        assert_eq!(verify(&root.0, &m), Err(PackageError::Identity));
        fs::remove_file(root.0.join(target)).unwrap();
        assert_eq!(verify(&root.0, &m), Err(PackageError::Missing));
    }
}
#[test]
fn relocation_and_exclusive_staging_preserve_payload_and_existing_target() {
    let root = Tree::new();
    let m = fixture(&root);
    let output = Tree::new();
    let target = output.0.join("Moved With Spaces.app");
    assemble(&root.0, &target, &m).unwrap();
    assert_eq!(verify(&target, &m), Ok(()));
    assert_eq!(
        assemble(&root.0, &target, &m),
        Err(PackageError::DestinationExists)
    );
    assert_eq!(verify(&target, &m), Ok(()));
}
#[test]
fn package_rejects_traversal_symlink_and_incomplete_manifests() {
    let root = Tree::new();
    let mut m = fixture(&root);
    let good = m.clone();
    m.entries[0].path = "../escape".into();
    assert_eq!(verify(&root.0, &m), Err(PackageError::Layout));
    m = good.clone();
    m.entries.pop();
    assert_eq!(verify(&root.0, &m), Err(PackageError::Layout));
    m = good;
    let path = root.0.join("Contents/Frameworks/mlx.metallib");
    fs::remove_file(&path).unwrap();
    std::os::unix::fs::symlink("../../Frameworks/libmlx.dylib", path).unwrap();
    assert_eq!(verify(&root.0, &m), Err(PackageError::Symlink));
}
#[test]
fn nested_code_signing_is_inside_out_without_deep_signing() {
    let root = Tree::new();
    let m = fixture(&root);
    let plan = signing_plan(&m).unwrap();
    assert_eq!(
        plan,
        vec![
            "Contents/Frameworks/libmlx.dylib",
            "Contents/Frameworks/libmlxc.dylib",
            "Contents/MacOS/mycelium",
            "."
        ]
    );
    assert!(relocatable_link("@rpath/libmlx.dylib"));
    assert!(relocatable_link("@loader_path/libmlx.dylib"));
    assert!(relocatable_link(
        "/System/Library/Frameworks/Metal.framework/Versions/A/Metal"
    ));
    assert!(!relocatable_link("/Users/developer/build/libmlx.dylib"));
    assert!(relocatable_link(
        "@executable_path/../Frameworks/libmlx.dylib"
    ));
    assert!(!relocatable_link("@rpath/../../untrusted.dylib"));
    assert!(!relocatable_link("/opt/homebrew/lib/libmlx.dylib"));
}
#[test]
fn packaged_entry_admission_precedes_any_native_factory() {
    let root = Tree::new();
    let m = fixture(&root);
    let exe = root.0.join("Contents/MacOS/mycelium");
    let admitted = admit_bundle(&exe, &m, "macos", "aarch64", "26.0").unwrap();
    assert_eq!(
        admitted.metal(),
        root.0.join("Contents/Frameworks/mlx.metallib")
    );
    assert!(matches!(
        admit_bundle(&exe, &m, "macos", "aarch64", "25.0"),
        Err(AdmissionError::Platform(_))
    ));
    fs::remove_file(root.0.join("Contents/Frameworks/mlx.metallib")).unwrap();
    assert!(matches!(
        admit_bundle(&exe, &m, "macos", "aarch64", "26.0"),
        Err(AdmissionError::Package(PackageError::Missing))
    ));
}
#[test]
fn cli_stage_verify_and_sign_plan_are_real_and_nonpublishing() {
    let root = Tree::new();
    let m = fixture(&root);
    let output = Tree::new();
    let manifest = output.0.join("manifest.json");
    fs::write(&manifest, serde_json::to_vec(&m).unwrap()).unwrap();
    let dest = output.0.join("Staged.app");
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_desktop-package"))
        .arg("stage")
        .arg(&manifest)
        .arg(&root.0)
        .arg(&dest)
        .output()
        .unwrap();
    assert!(result.status.success(), "{:?}", result.stderr);
    assert_eq!(verify(&dest, &m), Ok(()));
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_desktop-package"))
        .arg("verify")
        .arg(&manifest)
        .arg(&dest)
        .output()
        .unwrap();
    assert!(result.status.success());
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_desktop-package"))
        .arg("sign-plan")
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(result.status.success());
    let steps: Vec<Vec<String>> = String::from_utf8(result.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(steps.len(), 4);
    assert_eq!(
        steps[3],
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            "."
        ]
    );
    assert!(!dest.join("Contents/_CodeSignature").exists());
}
