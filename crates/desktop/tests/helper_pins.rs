// SPDX-License-Identifier: AGPL-3.0-only
//! The resource probe is a pinned helper of the packaged bundle (lane REL item 4): it is admitted
//! only at the path `api::resources::ProbeObserver::beside_executable` reads, by its pinned bytes;
//! a bundle without it or with other bytes is refused, and assembly copies only the pinned bytes.
use desktop::{
    bundle::{self, BundleError},
    package::{self, Entry, Kind, Manifest, PackageError, RESOURCE_PROBE_PATH},
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
            "v02-rel-helper-{}-{}",
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
fn entry(root: &Path, path: &str, kind: Kind, bytes: &[u8]) -> Entry {
    let file = root.join(path);
    fs::create_dir_all(file.parent().unwrap()).unwrap();
    fs::write(file, bytes).unwrap();
    Entry {
        path: path.into(),
        kind,
        bytes: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
    }
}
fn bundle_with_probe(root: &Path, probe_path: &str) -> Manifest {
    Manifest {
        schema: 1,
        entries: vec![
            entry(root, "Contents/MacOS/mycelium", Kind::Executable, b"exe"),
            entry(
                root,
                "Contents/Frameworks/libmlx.dylib",
                Kind::Dylib,
                b"mlx",
            ),
            entry(
                root,
                "Contents/Frameworks/libmlxc.dylib",
                Kind::Dylib,
                b"mlxc",
            ),
            entry(
                root,
                "Contents/Frameworks/mlx.metallib",
                Kind::Metal,
                b"metal",
            ),
            entry(root, probe_path, Kind::Helper, b"probe"),
        ],
    }
}

#[test]
fn the_shipped_helper_pin_is_the_resource_probe_where_the_observer_looks() {
    let pins = package::helper_pins().unwrap();
    assert_eq!(pins.len(), 1, "{pins:?}");
    assert_eq!(pins[0].kind, Kind::Helper);
    // `ProbeObserver::beside_executable`: `<dir of Contents/MacOS/mycelium>/native/resource_probe`.
    assert_eq!(RESOURCE_PROBE_PATH, "Contents/MacOS/native/resource_probe");
    assert_eq!(pins[0].path, RESOURCE_PROBE_PATH);
    assert!(pins[0].bytes > 0);
    assert_eq!(pins[0].sha256.len(), 64);
}

#[test]
fn admission_refuses_a_bundle_without_the_probe_or_with_other_probe_bytes() {
    let tree = Tree::new();
    let manifest = bundle_with_probe(&tree.0, RESOURCE_PROBE_PATH);
    assert_eq!(package::verify(&tree.0, &manifest), Ok(()));
    let probe = tree.0.join(RESOURCE_PROBE_PATH);
    fs::write(&probe, b"PROBE").unwrap();
    assert_eq!(
        package::verify(&tree.0, &manifest),
        Err(PackageError::Identity),
        "same size, other bytes"
    );
    fs::remove_file(&probe).unwrap();
    assert_eq!(
        package::verify(&tree.0, &manifest),
        Err(PackageError::Missing)
    );
}

#[test]
fn a_helper_is_admitted_only_at_the_observer_path() {
    let tree = Tree::new();
    let elsewhere = bundle_with_probe(&tree.0, "Contents/Frameworks/resource_probe");
    assert_eq!(
        package::verify(&tree.0, &elsewhere),
        Err(PackageError::Layout)
    );
    let mut probe_as_dylib = bundle_with_probe(&tree.0, RESOURCE_PROBE_PATH);
    probe_as_dylib.entries[4].kind = Kind::Dylib;
    assert_eq!(
        package::verify(&tree.0, &probe_as_dylib),
        Err(PackageError::Layout)
    );
}

#[test]
fn the_signing_plan_puts_the_helper_before_the_executable_and_the_bundle() {
    let tree = Tree::new();
    let plan = package::signing_plan(&bundle_with_probe(&tree.0, RESOURCE_PROBE_PATH)).unwrap();
    assert_eq!(
        plan,
        [
            "Contents/Frameworks/libmlx.dylib",
            "Contents/Frameworks/libmlxc.dylib",
            RESOURCE_PROBE_PATH,
            "Contents/MacOS/mycelium",
            "."
        ]
    );
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
            file("libjaccl.dylib", b"j", d), file("mlx.metallib", b"l", "runtime_metal_library"),
        ],
        "license_notices": [file("LICENSE", b"license", "")],
    });
    let manifest = tree.0.join("native-inputs.json");
    fs::write(&manifest, serde_json::to_vec(&inputs).unwrap()).unwrap();
    manifest
}

#[test]
fn assembly_places_only_the_pinned_probe_bytes_beside_the_executable() {
    let tree = Tree::new();
    let native = native_fixture(&tree);
    let icon = Path::new(env!("CARGO_MANIFEST_DIR")).join("icons/icon-windowed.icns");
    let exe = tree.0.join("exe");
    fs::write(&exe, b"not mach-o").unwrap();
    let probe = tree.0.join("resource_probe");
    fs::write(&probe, b"probe bytes").unwrap();
    let pin = Entry {
        path: RESOURCE_PROBE_PATH.into(),
        kind: Kind::Helper,
        bytes: 11,
        sha256: format!("{:x}", Sha256::digest(b"probe bytes")),
    };
    let out = tree.0.join("out");
    fs::create_dir(&out).unwrap();
    let built = bundle::build_with_helpers(
        &bundle::Spec {
            executable: &exe,
            native_inputs: &native,
            icon: &icon,
            out_dir: &out,
            expected_native_inputs_sha256: None,
        },
        &[(&probe, &pin)],
    )
    .unwrap();
    let placed = built.app.join(RESOURCE_PROBE_PATH);
    assert_eq!(fs::read(&placed).unwrap(), b"probe bytes");
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        fs::metadata(&placed).unwrap().permissions().mode() & 0o777,
        0o755
    );
    assert!(
        built
            .manifest
            .entries
            .iter()
            .any(|e| e.path == pin.path && e.kind == Kind::Helper && e.sha256 == pin.sha256)
    );
    assert_eq!(package::verify(&built.app, &built.manifest), Ok(()));

    fs::write(&probe, b"other bytes").unwrap();
    let out2 = tree.0.join("out2");
    fs::create_dir(&out2).unwrap();
    assert_eq!(
        bundle::build_with_helpers(
            &bundle::Spec {
                executable: &exe,
                native_inputs: &native,
                icon: &icon,
                out_dir: &out2,
                expected_native_inputs_sha256: None,
            },
            &[(&probe, &pin)],
        )
        .err(),
        Some(BundleError::SourceIdentity(probe.display().to_string()))
    );
    assert!(
        !out2.join("Mycelium.app").exists(),
        "a refused build leaves no bundle"
    );
}
