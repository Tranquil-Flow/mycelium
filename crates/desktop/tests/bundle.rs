// SPDX-License-Identifier: AGPL-3.0-only
//! Local unsigned bundle assembly, identity and the no-Python audit.
use desktop::{
    bundle::{self, BundleError, Spec},
    package::{self, Kind, PackageError},
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
            "v02-l11-bundle-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}
impl Drop for Tree {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn crate_file(relative: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(relative)
}
fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn source(tree: &Tree, name: &str, bytes: &[u8], role: &str) -> serde_json::Value {
    let path = tree.path(name);
    fs::write(&path, bytes).unwrap();
    serde_json::json!({
        "path": path, "path_base": "absolute", "size_bytes": bytes.len(),
        "sha256": sha(bytes), "role": role,
        "macho_build_version": (role == "linked_or_transitive_dylib")
            .then_some("cmd LC_BUILD_VERSION\n  minos 26.0\n"),
    })
}
/// Synthetic inputs in the genuine manifest's envelope. Returns (manifest path, exe path).
fn fixture(tree: &Tree) -> (PathBuf, PathBuf) {
    let dylib = "linked_or_transitive_dylib";
    let inputs = serde_json::json!({
        "schema": bundle::NATIVE_INPUT_SCHEMA,
        "observed_at": "2026-10-27T22:14:11Z",
        "ignored_future_field": [1, 2, 3],
        "native_inputs": [
            source(tree, "libmlxc.dylib", b"mlxc bytes", dylib),
            source(tree, "libmlx.dylib", b"mlx bytes", dylib),
            source(tree, "libjaccl.dylib", b"jaccl bytes", dylib),
            source(tree, "mlx.metallib", b"metal bytes", "runtime_metal_library"),
        ],
        "license_notices": [
            source(tree, "LICENSE", b"license one", ""),
            source(tree, "ACKNOWLEDGMENTS.md", b"ack", ""),
        ],
    });
    let manifest = tree.path("native-inputs.json");
    fs::write(&manifest, serde_json::to_vec(&inputs).unwrap()).unwrap();
    let exe = tree.path("mycelium-exe");
    fs::write(&exe, b"shell executable bytes").unwrap();
    (manifest, exe)
}
fn out(tree: &Tree) -> PathBuf {
    let out = tree.path("out");
    fs::create_dir(&out).unwrap();
    out
}
fn build(tree: &Tree, manifest: &Path, exe: &Path) -> Result<bundle::Built, BundleError> {
    bundle::build(&Spec {
        executable: exe,
        native_inputs: manifest,
        icon: &crate_file("icons/icon-windowed.icns"),
        out_dir: &out_dir(tree),
        expected_native_inputs_sha256: None,
    })
}
fn out_dir(tree: &Tree) -> PathBuf {
    tree.path("out")
}

// ---- identity -------------------------------------------------------------

#[test]
fn info_plist_has_the_released_v014_key_set_and_values_except_two_named_differences() {
    // Certified v0.1.4 `Contents/Info.plist` (sha256 e1220422…), read-only oracle.
    let v014 = [
        ("CFBundleDisplayName", "Mycelium A13 Demo"),
        ("CFBundleExecutable", "Mycelium A13 Demo"),
        ("CFBundleIconFile", "icon-windowed.icns"),
        ("CFBundleIdentifier", "org.mycelium.a13-macos-demo"),
        ("CFBundleInfoDictionaryVersion", "6.0"),
        ("CFBundleName", "Mycelium A13 Demo"),
        ("CFBundlePackageType", "APPL"),
        ("CFBundleShortVersionString", "0.0.0"),
        ("LSMinimumSystemVersion", "15.6.1"),
    ];
    let plist = bundle::info_plist();
    assert!(plist.contains("<key>NSHighResolutionCapable</key>\n\t<true/>"));
    let mut keys = vec!["NSHighResolutionCapable".to_string()];
    for (key, old) in v014 {
        let pattern = format!("<key>{key}</key>\n\t<string>");
        let value = plist
            .split_once(&pattern)
            .and_then(|(_, rest)| rest.split_once("</string>"))
            .map(|(value, _)| value)
            .unwrap_or_else(|| panic!("{key} missing"));
        keys.push(key.to_string());
        match key {
            // The only intentional differences: v0.2 executable and version.
            "CFBundleExecutable" => assert_eq!((value, old), ("mycelium", "Mycelium A13 Demo")),
            "CFBundleShortVersionString" => assert_eq!(value, "0.2.0"),
            _ => assert_eq!(value, old, "{key}"),
        }
    }
    assert_eq!(plist.matches("<key>").count(), keys.len(), "no extra keys");
    assert!(plist.starts_with("<?xml"));
}
#[test]
fn identity_constants_icon_and_tauri_packaged_config_agree() {
    let provenance: serde_json::Value =
        serde_json::from_str(include_str!("../assets.provenance.json")).unwrap();
    assert_eq!(provenance["app_icon"]["sha256"], bundle::ICON_SHA256);
    assert_eq!(
        sha(&fs::read(crate_file("icons/icon-windowed.icns")).unwrap()),
        bundle::ICON_SHA256
    );
    // The packaged build overrides only name, version and identifier.
    let config: serde_json::Value =
        serde_json::from_str(include_str!("../tauri.packaged.conf.json")).unwrap();
    assert_eq!(config["productName"], bundle::DISPLAY_NAME);
    assert_eq!(config["identifier"], bundle::BUNDLE_ID);
    assert_eq!(config["version"], bundle::VERSION);
    assert_eq!(config.as_object().unwrap().len(), 3);
    assert_eq!(bundle::MINIMUM_OS, "15.6.1");
}
#[test]
fn embedded_pins_equal_the_genuine_native_input_manifest_and_the_backend_build_pins() {
    let genuine = fs::read(crate_file(
        "../../evidence/foundation-exit/native-inputs.json",
    ))
    .unwrap();
    let inputs = bundle::parse_native_inputs(&genuine).unwrap();
    let pins = bundle::native_pins(&inputs).unwrap();
    let embedded: Vec<package::Entry> =
        serde_json::from_str(include_str!("../package-pins.json")).unwrap();
    assert_eq!(
        serde_json::to_value(&pins).unwrap(),
        serde_json::to_value(&embedded).unwrap()
    );
    let kinds: Vec<_> = embedded.iter().map(|e| e.kind.clone()).collect();
    assert_eq!(kinds, [Kind::Dylib, Kind::Dylib, Kind::Dylib, Kind::Metal]);
    // The independent L8 pin table (`pins.rs`, included by `build.rs`) pins the very same four identities.
    // Only the production table, not the refused-digest constants of that file's own tests.
    let pin_table = include_str!("../../backend-mlx/src/pins.rs")
        .split("#[cfg(test)]")
        .next()
        .unwrap();
    for entry in &embedded {
        assert!(pin_table.contains(&entry.sha256), "{}", entry.path);
    }
    assert_eq!(inputs.license_notices.len(), 7);
}

// ---- assembly -------------------------------------------------------------

#[test]
fn bundle_has_the_documented_layout_modes_and_verifies_against_its_own_manifest() {
    use std::os::unix::fs::PermissionsExt;
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let built = build(&tree, &manifest, &exe).unwrap();
    assert_eq!(built.app, out_dir(&tree).join("Mycelium A13 Demo.app"));
    let paths: Vec<_> = built
        .manifest
        .entries
        .iter()
        .map(|e| e.path.as_str())
        .collect();
    assert_eq!(
        paths,
        [
            "Contents/Frameworks/libjaccl.dylib",
            "Contents/Frameworks/libmlx.dylib",
            "Contents/Frameworks/libmlxc.dylib",
            "Contents/Frameworks/mlx.metallib",
            "Contents/Info.plist",
            "Contents/MacOS/mycelium",
            "Contents/Resources/icon-windowed.icns",
            "Contents/Resources/licenses/01-LICENSE",
            "Contents/Resources/licenses/02-ACKNOWLEDGMENTS.md",
        ]
    );
    // MLX loads its default library from beside libmlx.dylib and from nowhere under Resources/:
    // the first real worker trapped in `metal::Device::Device()` with the older layout.
    let dir = |p: &str| std::path::Path::new(p).parent().unwrap().to_path_buf();
    assert_eq!(
        dir("Contents/Frameworks/mlx.metallib"),
        dir("Contents/Frameworks/libmlx.dylib")
    );
    assert!(built.app.join("Contents/Frameworks/mlx.metallib").is_file());
    assert!(!built.app.join("Contents/Resources/metal").exists());
    assert_eq!(package::verify(&built.app, &built.manifest), Ok(()));
    let mode = |p: &str| {
        fs::metadata(built.app.join(p))
            .unwrap()
            .permissions()
            .mode()
            & 0o777
    };
    assert_eq!(mode("Contents/MacOS/mycelium"), 0o755);
    assert_eq!(mode("Contents/Frameworks/libmlx.dylib"), 0o755);
    assert_eq!(mode("Contents/Frameworks/mlx.metallib"), 0o644);
    let census = bundle::census(&built.app).unwrap();
    assert!(census.symlinks.is_empty());
    assert_eq!(
        census.files.len(),
        built.manifest.entries.len(),
        "no unlisted file"
    );
    assert_eq!(
        fs::read_to_string(built.app.join("Contents/Info.plist")).unwrap(),
        bundle::info_plist()
    );
    // Byte-identical to the genuine sources, not regenerated.
    assert_eq!(
        fs::read(built.app.join("Contents/Frameworks/libmlxc.dylib")).unwrap(),
        b"mlxc bytes"
    );
    // Re-running into a populated output is refused; the existing app is untouched.
    assert_eq!(
        build(&tree, &manifest, &exe).unwrap_err(),
        BundleError::DestinationExists
    );
    assert_eq!(package::verify(&built.app, &built.manifest), Ok(()));
}
#[test]
fn content_manifest_reports_sizes_sources_trust_level_and_unqualified_minimum_os() {
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let built = build(&tree, &manifest, &exe).unwrap();
    let content = bundle::content_manifest(&built).unwrap();
    assert_eq!(content["identity"]["bundle_identifier"], bundle::BUNDLE_ID);
    assert_eq!(content["trust_level"]["signed"], false);
    assert_eq!(content["trust_level"]["notarized"], false);
    assert_eq!(content["native_inputs"]["minimum_os_qualified"], false);
    let findings = content["native_inputs"]["minimum_os_findings"]
        .as_array()
        .unwrap();
    assert_eq!(
        findings.len(),
        3,
        "three dylibs record minos 26.0 > 15.6.1: {findings:?}"
    );
    assert!(findings.iter().all(|f| {
        f.as_str()
            .unwrap()
            .contains("exceeds LSMinimumSystemVersion 15.6.1")
    }));
    assert_eq!(content["totals"]["regular_files"], 9);
    assert_eq!(content["totals"]["symlinks"], 0);
    let total: u64 = built.manifest.entries.iter().map(|e| e.bytes).sum();
    assert_eq!(content["totals"]["content_bytes"], total);
    let mlx = content["entries"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["path"] == "Contents/Frameworks/libmlx.dylib")
        .unwrap();
    assert_eq!(mlx["sha256"], sha(b"mlx bytes"));
    assert_eq!(mlx["source_role"], "linked_or_transitive_dylib");
    assert!(mlx["source"].as_str().unwrap().ends_with("libmlx.dylib"));
    let listing = bundle::app_files_listing(&built.app).unwrap();
    assert!(listing.contains(&format!(
        "{}  Contents/Frameworks/libmlx.dylib\n",
        sha(b"mlx bytes")
    )));
    assert_eq!(listing.lines().count(), 9);
}
#[test]
fn every_native_source_is_identity_checked_and_a_failed_build_leaves_nothing() {
    for victim in ["libmlxc.dylib", "mlx.metallib", "LICENSE"] {
        let tree = Tree::new();
        let (manifest, exe) = fixture(&tree);
        out(&tree);
        fs::write(tree.path(victim), b"tampered but different").unwrap();
        assert!(matches!(
            build(&tree, &manifest, &exe),
            Err(BundleError::SourceIdentity(path)) if path.ends_with(victim)
        ));
        assert!(!out_dir(&tree).join("Mycelium A13 Demo.app").exists());
        fs::remove_file(tree.path(victim)).unwrap();
        assert!(matches!(
            build(&tree, &manifest, &exe),
            Err(BundleError::Io(_))
        ));
        assert!(!out_dir(&tree).join("Mycelium A13 Demo.app").exists());
    }
}
#[test]
fn icon_must_be_the_certified_v014_icon() {
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let fake = tree.path("fake.icns");
    fs::write(&fake, b"not the icon").unwrap();
    let result = bundle::build(&Spec {
        executable: &exe,
        native_inputs: &manifest,
        icon: &fake,
        out_dir: &out_dir(&tree),
        expected_native_inputs_sha256: None,
    });
    assert!(matches!(result, Err(BundleError::SourceIdentity(_))));
    assert!(!out_dir(&tree).join("Mycelium A13 Demo.app").exists());
}
#[test]
fn native_input_manifest_refusals() {
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let good: serde_json::Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    type Mutation = fn(&mut serde_json::Value);
    let mutations: [(&str, Mutation); 5] = [
        ("native_inputs_schema", |v| v["schema"] = "other".into()),
        ("native_inputs_entry", |v| {
            v["native_inputs"][0]["path_base"] = "repository".into()
        }),
        ("native_inputs_entry", |v| {
            v["native_inputs"][0]["path"] = "relative/libmlx.dylib".into()
        }),
        ("native_inputs_role", |v| {
            v["native_inputs"][0]["role"] = "extra".into()
        }),
        ("native_inputs_name", |v| {
            v["native_inputs"][3]["path"] = "/".into()
        }),
    ];
    for (expected, mutate) in mutations {
        let mut bad = good.clone();
        mutate(&mut bad);
        fs::write(&manifest, serde_json::to_vec(&bad).unwrap()).unwrap();
        assert_eq!(
            build(&tree, &manifest, &exe).unwrap_err(),
            BundleError::NativeInputs(expected)
        );
        assert!(!out_dir(&tree).join("Mycelium A13 Demo.app").exists());
    }
    fs::write(&manifest, b"{}").unwrap();
    assert_eq!(
        build(&tree, &manifest, &exe).unwrap_err(),
        BundleError::NativeInputs("native_inputs_unparseable")
    );
}
#[test]
fn packaged_manifest_is_pins_plus_the_measured_executable_and_still_refuses_corruption() {
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let built = build(&tree, &manifest, &exe).unwrap();
    let inputs = bundle::parse_native_inputs(&fs::read(&manifest).unwrap()).unwrap();
    let pins = bundle::native_pins(&inputs).unwrap();
    let measured =
        package::with_measured_executable(pins, &built.app.join("Contents/MacOS/mycelium"))
            .unwrap();
    assert_eq!(package::verify(&built.app, &measured), Ok(()));
    for victim in [
        "Contents/Frameworks/libjaccl.dylib",
        "Contents/Frameworks/mlx.metallib",
    ] {
        let original = fs::read(built.app.join(victim)).unwrap();
        fs::write(built.app.join(victim), b"corrupt").unwrap();
        assert_eq!(
            package::verify(&built.app, &measured),
            Err(PackageError::Identity)
        );
        fs::write(built.app.join(victim), original).unwrap();
    }
    assert_eq!(package::verify(&built.app, &measured), Ok(()));
}
#[test]
fn layout_admits_only_the_named_icon_and_licenses_as_resources() {
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let mut built = build(&tree, &manifest, &exe).unwrap();
    assert_eq!(package::verify(&built.app, &built.manifest), Ok(()));
    let icon = built
        .manifest
        .entries
        .iter_mut()
        .find(|e| e.path.ends_with("icon-windowed.icns"))
        .unwrap();
    icon.path = "Contents/Resources/other.icns".into();
    assert_eq!(
        package::verify(&built.app, &built.manifest),
        Err(PackageError::Layout)
    );
}

// ---- no Python anywhere in the bundle -------------------------------------

fn write(root: &Path, relative: &str, bytes: &[u8]) {
    let path = root.join(relative);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, bytes).unwrap();
}
fn flagged(root: &Path) -> Vec<String> {
    bundle::audit_no_python(root)
        .unwrap()
        .into_iter()
        .map(|f| f.0)
        .collect()
}
#[test]
fn clean_bundle_has_no_python_indicators() {
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let built = build(&tree, &manifest, &exe).unwrap();
    assert_eq!(bundle::audit_no_python(&built.app).unwrap(), vec![]);
}
#[test]
fn every_python_indicator_kind_is_found() {
    let cases: [(&str, &[u8]); 15] = [
        ("Contents/Resources/mod.py", b"x"),
        ("Contents/Resources/mod.pyc", b"x"),
        ("Contents/Resources/__pycache__/m.cpython-314.pyc", b"x"),
        ("Contents/Resources/a.dist-info/RECORD", b"x"),
        ("Contents/Resources/site-packages/m", b"x"),
        (
            "Contents/Frameworks/Python.framework/Versions/3.14/Python",
            b"x",
        ),
        ("Contents/Frameworks/Python", b"x"),
        ("Contents/Frameworks/libpython3.14.dylib", b"x"),
        ("Contents/Resources/base_library.zip", b"x"),
        (
            "Contents/Resources/_cffi_backend.cpython-314-darwin.so",
            b"x",
        ),
        ("Contents/Resources/pyvenv.cfg", b"x"),
        (
            "Contents/MacOS/launcher",
            b"#!/usr/bin/env python3\nprint(1)\n",
        ),
        (
            "Contents/MacOS/mycelium",
            b"\xcf\xfa\xed\xfe....libpython3.14.dylib....",
        ),
        (
            "Contents/MacOS/other",
            b"....@rpath/Python.framework/Versions/3.14/Python....",
        ),
        ("Contents/MacOS/third", b"....Py_Initialize...."),
    ];
    for (path, bytes) in cases {
        let tree = Tree::new();
        write(&tree.0, "Contents/Info.plist", b"clean");
        write(&tree.0, path, bytes);
        let found = flagged(&tree.0);
        assert!(
            found
                .iter()
                .any(|f| path == f || path.starts_with(&format!("{f}/"))),
            "{path}: {found:?}"
        );
    }
}
#[test]
fn marker_bytes_straddling_every_read_boundary_are_found() {
    let overlap = 16usize;
    for needle in [&b"libpython"[..], b"Python.framework", b"PYTHONHOME"] {
        for offset in ((1usize << 20) + overlap - needle.len() - 1)..=((1 << 20) + overlap + 1) {
            let tree = Tree::new();
            let mut bytes = vec![0u8; offset + needle.len() + 8];
            bytes[offset..offset + needle.len()].copy_from_slice(needle);
            write(&tree.0, "Contents/Resources/blob.bin", &bytes);
            assert_eq!(
                flagged(&tree.0),
                ["Contents/Resources/blob.bin"],
                "{needle:?}@{offset}"
            );
        }
    }
}
#[test]
fn a_v014_shaped_tree_is_rejected_and_a_symlinked_python_is_named() {
    let tree = Tree::new();
    for path in [
        "Contents/MacOS/Mycelium A13 Demo",
        "Contents/Frameworks/Python.framework/Versions/3.14/Python",
        "Contents/Resources/python3.14/lib/os.py",
        "Contents/Resources/aiohttp/__init__.py",
        "Contents/Resources/mlx_metal-0.31.1.dist-info/RECORD",
    ] {
        write(&tree.0, path, b"plain");
    }
    std::os::unix::fs::symlink(
        "Python.framework/Versions/3.14/Python",
        tree.path("Contents/Frameworks/Python"),
    )
    .unwrap();
    let found = flagged(&tree.0);
    assert!(found.contains(&"Contents/Frameworks/Python".to_string()));
    assert!(found.contains(&"Contents/Resources/python3.14".to_string()));
    assert!(found.contains(&"Contents/Resources/aiohttp/__init__.py".to_string()));
    assert!(
        !found
            .iter()
            .any(|f| f == "Contents/MacOS/Mycelium A13 Demo")
    );
}

// ---- candidate native-inputs manifest: consumed only when its sha256 is pinned and verified ----

fn build_pinned(
    tree: &Tree,
    manifest: &Path,
    exe: &Path,
    pin: Option<&str>,
) -> Result<bundle::Built, BundleError> {
    bundle::build(&Spec {
        executable: exe,
        native_inputs: manifest,
        icon: &crate_file("icons/icon-windowed.icns"),
        out_dir: &out_dir(tree),
        expected_native_inputs_sha256: pin,
    })
}
#[test]
fn a_pinned_native_inputs_manifest_is_used_only_when_its_sha256_matches() {
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let actual = sha(&fs::read(&manifest).unwrap());
    let wrong = "0".repeat(64);
    assert_eq!(
        build_pinned(&tree, &manifest, &exe, Some(&wrong)).err(),
        Some(BundleError::NativeInputs("native_inputs_sha256_mismatch"))
    );
    assert!(
        !out_dir(&tree).join("Mycelium A13 Demo.app").exists(),
        "nothing assembled on a pin mismatch"
    );
    for malformed in ["", "ABC", &actual.to_uppercase(), &format!("{actual}0")] {
        assert_eq!(
            build_pinned(&tree, &manifest, &exe, Some(malformed)).err(),
            Some(BundleError::NativeInputs("native_inputs_sha256_mismatch")),
            "{malformed:?}"
        );
    }
    let built = build_pinned(&tree, &manifest, &exe, Some(&actual)).unwrap();
    assert_eq!(built.native_inputs_sha256, actual);
}
fn candidate_fixture(tree: &Tree) -> (PathBuf, PathBuf) {
    // L8's `mycelium.l8.native-inputs-candidate.v1` shape: no path_base, roles linked_dylib /
    // transitive_dylib / runtime_metal_library, a `minos` per dylib instead of a build dump.
    let entry = |name: &str, bytes: &[u8], role: &str, minos: Option<&str>| {
        let path = tree.path(name);
        fs::write(&path, bytes).unwrap();
        serde_json::json!({"path": path, "role": role, "minos": minos,
            "size_bytes": bytes.len(), "sha256": sha(bytes), "lc_rpath": []})
    };
    let notice = |name: &str, bytes: &[u8]| {
        let path = tree.path(name);
        fs::write(&path, bytes).unwrap();
        serde_json::json!({"path": path, "size_bytes": bytes.len(), "sha256": sha(bytes)})
    };
    let value = serde_json::json!({
        "schema": "mycelium.l8.native-inputs-candidate.v1",
        "observed_at": "2026-10-28T05:00:00Z", "status": "PROPOSAL",
        "native_inputs": [
            entry("libmlxc.dylib", b"c14", "linked_dylib", Some("14.0")),
            entry("libmlx.dylib", b"m14", "transitive_dylib", Some("14.0")),
            entry("libjaccl.dylib", b"j14", "transitive_dylib", Some("15.7")),
            entry("mlx.metallib", b"l14", "runtime_metal_library", None),
        ],
        "license_notices": [notice("LICENSE", b"license")],
    });
    let manifest = tree.path("candidate.json");
    fs::write(&manifest, serde_json::to_vec(&value).unwrap()).unwrap();
    (manifest, tree.path("mycelium-exe"))
}
#[test]
fn a_candidate_manifest_is_consumed_only_with_a_verified_pin_and_its_sources_are_verified() {
    let tree = Tree::new();
    let (manifest, exe) = candidate_fixture(&tree);
    fs::write(&exe, b"shell executable bytes").unwrap();
    out(&tree);
    assert_eq!(
        build_pinned(&tree, &manifest, &exe, None).err(),
        Some(BundleError::NativeInputs(
            "native_inputs_candidate_requires_pin"
        )),
        "an unpinned candidate is never consumed"
    );
    let wrong = "1".repeat(64);
    assert_eq!(
        build_pinned(&tree, &manifest, &exe, Some(&wrong)).err(),
        Some(BundleError::NativeInputs("native_inputs_sha256_mismatch"))
    );
    let pin = sha(&fs::read(&manifest).unwrap());
    let built = build_pinned(&tree, &manifest, &exe, Some(&pin)).unwrap();
    assert_eq!(built.native_inputs_sha256, pin);
    let paths: Vec<_> = built
        .manifest
        .entries
        .iter()
        .map(|e| e.path.as_str())
        .collect();
    for expected in [
        "Contents/Frameworks/libmlx.dylib",
        "Contents/Frameworks/libmlxc.dylib",
        "Contents/Frameworks/libjaccl.dylib",
        "Contents/Frameworks/mlx.metallib",
    ] {
        assert!(paths.contains(&expected), "{expected}");
    }
    // The recorded minos feeds the minimum-OS findings: 15.7 exceeds 15.6.1, 14.0 does not.
    let content = bundle::content_manifest(&built).unwrap();
    let findings = content["native_inputs"]["minimum_os_findings"]
        .as_array()
        .unwrap();
    assert_eq!(findings.len(), 1, "{findings:?}");
    assert!(findings[0].as_str().unwrap().contains("libjaccl"));
}
#[test]
fn a_candidate_source_that_does_not_match_its_recorded_identity_refuses_assembly() {
    let tree = Tree::new();
    let (manifest, exe) = candidate_fixture(&tree);
    fs::write(&exe, b"shell executable bytes").unwrap();
    out(&tree);
    fs::write(tree.path("libmlx.dylib"), b"swapped after the proposal").unwrap();
    let pin = sha(&fs::read(&manifest).unwrap());
    assert!(matches!(
        build_pinned(&tree, &manifest, &exe, Some(&pin)),
        Err(BundleError::SourceIdentity(_))
    ));
    assert!(!out_dir(&tree).join("Mycelium A13 Demo.app").exists());
}
#[test]
fn an_unknown_schema_is_still_refused_even_with_a_pin() {
    let tree = Tree::new();
    let (manifest, exe) = fixture(&tree);
    out(&tree);
    let mut value: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    value["schema"] = "mycelium.something.else.v1".into();
    let other = tree.path("other.json");
    fs::write(&other, serde_json::to_vec(&value).unwrap()).unwrap();
    let pin = sha(&fs::read(&other).unwrap());
    assert_eq!(
        build_pinned(&tree, &other, &exe, Some(&pin)).err(),
        Some(BundleError::NativeInputs("native_inputs_schema"))
    );
}
