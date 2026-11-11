// SPDX-License-Identifier: AGPL-3.0-only
//! A signable bundle layout for the Metal library (lane REL item 2). `codesign` treats every file
//! in `Contents/Frameworks` as nested code and refuses a bundle whose `mlx.metallib` (not Mach-O)
//! sits there ("code object is not signed at all, In subcomponent: …/mlx.metallib"). v0.1.4 keeps
//! the file in `Contents/Resources` and a relative symlink in `Frameworks`
//! (`Frameworks/mlx/lib/mlx.metallib -> ../../../Resources/mlx/lib/mlx.metallib`, package census
//! @ f4e366e9); MLX still finds it beside `libmlx.dylib`. The admission keeps pinning the bytes.
use desktop::{
    bundle,
    package::{self, PackageError},
};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
};

static SEQ: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-rel-metal-{}-{}",
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
fn native_fixture(tree: &Tree) -> PathBuf {
    let file = |name: &str, bytes: &[u8], role: &str| {
        let path = tree.0.join(name);
        fs::write(&path, bytes).unwrap();
        serde_json::json!({
            "path": path, "path_base": "absolute", "size_bytes": bytes.len(),
            "sha256": format!("{:x}", Sha256::digest(bytes)), "role": role,
        })
    };
    let d = "linked_or_transitive_dylib";
    let inputs = serde_json::json!({
        "schema": bundle::NATIVE_INPUT_SCHEMA, "observed_at": "2026-10-28T00:00:00Z",
        "native_inputs": [
            file("libmlxc.dylib", b"c", d), file("libmlx.dylib", b"m", d),
            file("libjaccl.dylib", b"j", d), file("mlx.metallib", b"metal bytes", "runtime_metal_library"),
        ],
        "license_notices": [file("LICENSE", b"license", "")],
    });
    let manifest = tree.0.join("native-inputs.json");
    fs::write(&manifest, serde_json::to_vec(&inputs).unwrap()).unwrap();
    manifest
}
fn built(tree: &Tree) -> bundle::Built {
    let native = native_fixture(tree);
    let icon = Path::new(env!("CARGO_MANIFEST_DIR")).join("icons/icon-windowed.icns");
    let exe = tree.0.join("exe");
    fs::write(&exe, b"not mach-o").unwrap();
    let out = tree.0.join("out");
    fs::create_dir(&out).unwrap();
    bundle::build(&bundle::Spec {
        executable: &exe,
        native_inputs: &native,
        icon: &icon,
        out_dir: &out,
        expected_native_inputs_sha256: None,
    })
    .unwrap()
}

#[test]
fn the_metallib_is_stored_in_resources_and_linked_beside_libmlx_like_v014() {
    let tree = Tree::new();
    let built = built(&tree);
    let link = built.app.join("Contents/Frameworks/mlx.metallib");
    assert!(
        fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink(),
        "Frameworks holds only code and links"
    );
    assert_eq!(
        fs::read_link(&link).unwrap(),
        Path::new("../Resources/mlx.metallib")
    );
    let stored = built.app.join("Contents/Resources/mlx.metallib");
    assert!(fs::symlink_metadata(&stored).unwrap().is_file());
    assert_eq!(
        fs::read(&link).unwrap(),
        b"metal bytes",
        "MLX reads it through the link"
    );
    // The pinned entry is still the path MLX loads, admitted by the stored bytes.
    let metal = built
        .manifest
        .entries
        .iter()
        .find(|e| e.kind == package::Kind::Metal)
        .unwrap();
    assert_eq!(metal.path, "Contents/Frameworks/mlx.metallib");
    assert_eq!(
        metal.sha256,
        format!("{:x}", Sha256::digest(b"metal bytes"))
    );
    assert_eq!(package::verify(&built.app, &built.manifest), Ok(()));
}

#[test]
fn a_metallib_link_elsewhere_or_other_stored_bytes_are_refused() {
    let tree = Tree::new();
    let built = built(&tree);
    let link = built.app.join("Contents/Frameworks/mlx.metallib");
    let stored = built.app.join("Contents/Resources/mlx.metallib");
    fs::write(&stored, b"other bytes").unwrap();
    assert_eq!(
        package::verify(&built.app, &built.manifest),
        Err(PackageError::Identity)
    );
    fs::write(&stored, b"metal bytes").unwrap();
    fs::remove_file(&link).unwrap();
    std::os::unix::fs::symlink(&stored, &link).unwrap();
    assert_eq!(
        package::verify(&built.app, &built.manifest),
        Err(PackageError::Symlink),
        "an absolute link leaves the bundle when it moves"
    );
    fs::remove_file(&link).unwrap();
    assert_eq!(
        package::verify(&built.app, &built.manifest),
        Err(PackageError::Missing)
    );
}
