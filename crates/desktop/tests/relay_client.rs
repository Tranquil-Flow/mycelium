// SPDX-License-Identifier: AGPL-3.0-only
//! The relay client (`cloudflared`) is a pinned helper of the packaged bundle (lane RELF item 3,
//! review F1). v0.1.4 bundles it (`scripts/build_a13_macos.py:285-288,318,325` @ f4e366e9:
//! `--add-binary <cloudflared>:mycelium_a13_macos/native` + `third_party/cloudflared-LICENSE`) and
//! runs it from there (`public_relay.py:250-255` `installed_relay_binary`, reached from
//! `app.py` `DemoApp` -> `demo_app.action('network_start')` -> `TemporaryHttpsRelay.start`).
//! v0.2 looks for it at `<exe dir>/native/cloudflared` (`api::relay::installed_relay_binary`), so
//! the pinned path is `Contents/MacOS/native/cloudflared`. Startup admission checks it like every
//! other pinned helper BEFORE anything can execute it, and refuses any code file it does not pin
//! beside the executable or in `Frameworks` (a planted `native/cloudflared` was ignored before).
use desktop::{
    bundle,
    package::{self, AdmissionError, Entry, Kind, Manifest, PackageError, RESOURCE_PROBE_PATH},
};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
};

const RELAY: &str = "Contents/MacOS/native/cloudflared";
const RELAY_LICENSE: &str = "Contents/Resources/licenses/cloudflared-LICENSE";
/// v0.1.4's release `APP-FILES.sha256` line 25 (`Contents/Frameworks/mycelium_a13_macos/native/
/// cloudflared`): cloudflared 2026.10.2 arm64, 25972048 bytes.
const V014_RELAY_SHA256: &str = "41423e7c363eed5ec73087cd21b04590e454bd0dc9284f5bd77eada147342c7f";
const V014_RELAY_BYTES: u64 = 25_972_048;
/// v0.1.4's release `APP-FILES.sha256` line 1297 (`Contents/Resources/third_party/cloudflared-LICENSE`).
const V014_RELAY_LICENSE_SHA256: &str =
    "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd";

static SEQ: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-relf-relay-{}-{}",
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
/// A bundle tree laid out like the real one (Metal bytes in Resources, the one relative link).
fn bundle_tree(root: &Path, with_relay: bool) -> Manifest {
    let metal_store = root.join(package::METAL_STORE_PATH);
    fs::create_dir_all(metal_store.parent().unwrap()).unwrap();
    fs::write(&metal_store, b"metal").unwrap();
    fs::create_dir_all(root.join("Contents/Frameworks")).unwrap();
    std::os::unix::fs::symlink(
        package::METAL_LINK_TARGET,
        root.join("Contents/Frameworks/mlx.metallib"),
    )
    .unwrap();
    let mut entries = vec![
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
        Entry {
            path: "Contents/Frameworks/mlx.metallib".into(),
            kind: Kind::Metal,
            bytes: 5,
            sha256: format!("{:x}", Sha256::digest(b"metal")),
        },
        entry(root, RESOURCE_PROBE_PATH, Kind::Helper, b"probe"),
    ];
    if with_relay {
        entries.push(entry(root, RELAY, Kind::Helper, b"relay client"));
    }
    Manifest { schema: 1, entries }
}
fn admit(root: &Path, manifest: &Manifest) -> Result<(), AdmissionError> {
    package::admit_bundle(
        &root.join("Contents/MacOS/mycelium"),
        manifest,
        "macos",
        "aarch64",
        "26.0",
    )
    .map(|_| ())
}

#[test]
fn the_relay_client_is_pinned_at_the_path_the_relay_reads_with_v014s_bytes() {
    let pins = package::helper_pins().unwrap();
    let relay = pins.iter().find(|p| p.path == RELAY);
    let relay = relay.unwrap_or_else(|| panic!("no pinned relay client at {RELAY}: {pins:?}"));
    assert_eq!(relay.kind, Kind::Helper);
    assert_eq!(relay.sha256, V014_RELAY_SHA256);
    assert_eq!(relay.bytes, V014_RELAY_BYTES);
    // The probe pin is unchanged beside it.
    assert!(pins.iter().any(|p| p.path == RESOURCE_PROBE_PATH));
}

#[test]
fn startup_admission_checks_the_pinned_relay_client_and_refuses_a_swapped_or_missing_one() {
    let tree = Tree::new();
    let manifest = bundle_tree(&tree.0, true);
    assert!(
        admit(&tree.0, &manifest).is_ok(),
        "{:?}",
        admit(&tree.0, &manifest)
    );
    let relay = tree.0.join(RELAY);
    fs::write(&relay, b"RELAY CLIENT").unwrap();
    assert!(matches!(
        admit(&tree.0, &manifest),
        Err(AdmissionError::Package(PackageError::Identity))
    ));
    fs::remove_file(&relay).unwrap();
    assert!(matches!(
        admit(&tree.0, &manifest),
        Err(AdmissionError::Package(PackageError::Missing))
    ));
}

#[test]
fn startup_admission_refuses_unpinned_code_beside_the_executable_or_in_frameworks() {
    for (plant, what) in [
        (
            RELAY,
            "an unpinned native/cloudflared (the relay would run it)",
        ),
        (
            "Contents/MacOS/native/other_helper",
            "another native helper",
        ),
        ("Contents/MacOS/other", "a second executable"),
        ("Contents/Frameworks/libextra.dylib", "an extra dylib"),
        ("Contents/Frameworks/sub/libnested.dylib", "a nested dylib"),
    ] {
        let tree = Tree::new();
        let manifest = bundle_tree(&tree.0, false);
        assert!(
            admit(&tree.0, &manifest).is_ok(),
            "{what}: control {:?}",
            admit(&tree.0, &manifest)
        );
        let path = tree.0.join(plant);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, b"planted").unwrap();
        let refused = admit(&tree.0, &manifest);
        assert!(
            format!("{refused:?}").contains("Unpinned"),
            "{what} at {plant} was admitted: {refused:?}"
        );
    }
    // A link planted beside the executable is refused as well (only the metallib link exists).
    let tree = Tree::new();
    let manifest = bundle_tree(&tree.0, false);
    std::os::unix::fs::symlink("/usr/bin/true", tree.0.join(RELAY)).unwrap();
    let refused = admit(&tree.0, &manifest);
    assert!(
        format!("{refused:?}").contains("Unpinned"),
        "planted link admitted: {refused:?}"
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
fn assembly_places_the_pinned_relay_client_and_its_licence_and_records_their_provenance() {
    let tree = Tree::new();
    let native = native_fixture(&tree);
    let icon = Path::new(env!("CARGO_MANIFEST_DIR")).join("icons/icon-windowed.icns");
    let exe = tree.0.join("exe");
    fs::write(&exe, b"not mach-o").unwrap();
    let pin = |path: &str, kind: Kind, bytes: &[u8]| Entry {
        path: path.into(),
        kind,
        bytes: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
    };
    let (probe, relay, licence) = (
        tree.0.join("resource_probe"),
        tree.0.join("cloudflared"),
        tree.0.join("cloudflared-LICENSE"),
    );
    fs::write(&probe, b"probe bytes").unwrap();
    fs::write(&relay, b"relay bytes").unwrap();
    fs::write(&licence, b"Apache License 2.0").unwrap();
    let probe_pin = pin(RESOURCE_PROBE_PATH, Kind::Helper, b"probe bytes");
    let relay_pin = pin(RELAY, Kind::Helper, b"relay bytes");
    let licence_pin = pin(RELAY_LICENSE, Kind::Resource, b"Apache License 2.0");
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
        &[
            (&probe, &probe_pin),
            (&relay, &relay_pin),
            (&licence, &licence_pin),
        ],
    );
    let built = built.unwrap_or_else(|e| panic!("relay client and licence refused: {e:?}"));
    use std::os::unix::fs::PermissionsExt;
    for (relative, bytes, mode) in [
        (RELAY, &b"relay bytes"[..], 0o755),
        (RELAY_LICENSE, &b"Apache License 2.0"[..], 0o644),
    ] {
        let placed = built.app.join(relative);
        assert_eq!(fs::read(&placed).unwrap(), bytes, "{relative}");
        assert_eq!(
            fs::metadata(&placed).unwrap().permissions().mode() & 0o777,
            mode,
            "{relative}"
        );
    }
    assert_eq!(package::verify(&built.app, &built.manifest), Ok(()));
    let content = bundle::content_manifest(&built).unwrap();
    let row = |path: &str| {
        content["entries"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["path"] == path)
            .cloned()
            .unwrap_or_else(|| panic!("{path} not in the content manifest"))
    };
    let relay_row = row(RELAY);
    assert_eq!(relay_row["kind"], "Helper");
    let source = relay_row["source"].as_str().unwrap();
    assert!(
        source.contains("cloudflared 2026.10.2") && !source.contains(&*tree.0.to_string_lossy()),
        "{source}"
    );
    // v0.1.4's binary records minos 26.0, above LSMinimumSystemVersion 15.6.1: said, not hidden.
    let findings = content["native_inputs"]["minimum_os_findings"].to_string();
    assert!(
        findings.contains(RELAY) && findings.contains("26.0"),
        "{findings}"
    );
    assert_eq!(row(RELAY_LICENSE)["kind"], "Resource");
}
