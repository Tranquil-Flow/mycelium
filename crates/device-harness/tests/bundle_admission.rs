// SPDX-License-Identifier: AGPL-3.0-only
//! Package admission of a built bundle: file list, sizes and sha256 must equal the genuine
//! manifest + package pins exactly. Fixture bundles live in private scratch roots; the real
//! committed documents are consumed read-only to prove the pins and the manifest agree.
mod support;
use device_harness::{
    bundle_admission::{self, Expected, Statics},
    evidence_inputs::Content,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::{fs, path::Path};
use support::PrivateRoot;

fn content(bytes: &[u8]) -> Content {
    Content {
        size_bytes: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
    }
}
fn body(path: &str) -> Vec<u8> {
    format!("fixture bytes of {path}").into_bytes()
}
const NATIVES: [(&str, &str, &str); 4] = [
    (
        "libmlxc.dylib",
        "Contents/Frameworks/libmlxc.dylib",
        "Dylib",
    ),
    ("libmlx.dylib", "Contents/Frameworks/libmlx.dylib", "Dylib"),
    (
        "libjaccl.dylib",
        "Contents/Frameworks/libjaccl.dylib",
        "Dylib",
    ),
    ("mlx.metallib", "Contents/Frameworks/mlx.metallib", "Metal"),
];
const LICENSES: [&str; 2] = ["LICENSE", "ACKNOWLEDGMENTS.md"];

struct Fixture {
    _root: PrivateRoot,
    app: std::path::PathBuf,
    manifest: Vec<u8>,
    pins: Vec<u8>,
    statics: Statics,
}
impl Fixture {
    fn new(name: &str) -> Self {
        let root = PrivateRoot::new(name);
        let app = root.join("Mycelium A13 Demo.app");
        let reference = |leaf: &str, path: &str| {
            let c = content(&body(path));
            json!({"path": format!("/fixture/{leaf}"), "path_base": "absolute",
                   "size_bytes": c.size_bytes, "sha256": c.sha256})
        };
        let mut natives = vec![];
        let mut pins = vec![];
        for (leaf, destination, kind) in NATIVES {
            natives.push(reference(leaf, destination));
            let c = content(&body(destination));
            pins.push(json!({"path": destination, "kind": kind, "bytes": c.size_bytes, "sha256": c.sha256}));
            write(&app, destination, &body(destination), 0o644);
        }
        let mut notices = vec![];
        for (index, leaf) in LICENSES.iter().enumerate() {
            let destination = format!("Contents/Resources/licenses/{:02}-{leaf}", index + 1);
            notices.push(reference(leaf, &destination));
            write(&app, &destination, &body(&destination), 0o644);
        }
        let statics = Statics {
            executable: content(&body(bundle_admission::EXECUTABLE)),
            info_plist: content(&body(bundle_admission::INFO_PLIST)),
            icon: content(&body(bundle_admission::ICON)),
        };
        write(
            &app,
            bundle_admission::EXECUTABLE,
            &body(bundle_admission::EXECUTABLE),
            0o755,
        );
        write(
            &app,
            bundle_admission::INFO_PLIST,
            &body(bundle_admission::INFO_PLIST),
            0o644,
        );
        write(
            &app,
            bundle_admission::ICON,
            &body(bundle_admission::ICON),
            0o644,
        );
        let manifest = serde_json::to_vec(&json!({
            "schema": bundle_admission::NATIVE_SCHEMA, "native_inputs": natives,
            "license_notices": notices, "unrelated": {"ignored": true}}))
        .unwrap();
        Self {
            _root: root,
            app,
            manifest,
            pins: serde_json::to_vec(&pins).unwrap(),
            statics,
        }
    }
    fn expected(&self) -> device_harness::Result<Expected> {
        Expected::from_documents(
            &self.manifest,
            &content(&self.manifest),
            &self.pins,
            &self.statics,
        )
    }
    fn admit(&self) -> Result<bundle_admission::Receipt, &'static str> {
        bundle_admission::admit(&self.app, &self.expected().unwrap()).map_err(|f| f.code)
    }
}
fn write(app: &Path, relative: &str, bytes: &[u8], mode: u32) {
    let path = app.join(relative);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, bytes).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
}

#[test]
fn an_untouched_bundle_is_admitted_and_the_receipt_lists_every_file_once() {
    let f = Fixture::new("adm-ok");
    let receipt = f.admit().unwrap();
    assert_eq!(receipt.verdict, "admitted_content_only");
    assert_eq!(receipt.file_count, 4 + LICENSES.len() + 3);
    assert_eq!(receipt.files.len(), receipt.file_count);
    assert_eq!(
        receipt.total_bytes,
        receipt.files.iter().map(|x| x.bytes).sum::<u64>()
    );
    assert!(
        receipt
            .files
            .iter()
            .any(|x| x.path == "Contents/Frameworks/mlx.metallib")
    );
    assert!(
        receipt.not_claimed.contains(&"relocation") && receipt.not_claimed.contains(&"minimum_os")
    );
    assert_eq!(receipt.manifest_sha256, content(&f.manifest).sha256);
    assert_eq!(fs::metadata(&f.app).unwrap().uid(), f._root.uid());
}

#[test]
fn same_size_content_tamper_of_every_class_of_file_is_refused() {
    for target in [
        "Contents/Frameworks/libmlx.dylib",
        "Contents/Frameworks/mlx.metallib",
        "Contents/MacOS/mycelium",
        "Contents/Info.plist",
        "Contents/Resources/icon-windowed.icns",
        "Contents/Resources/licenses/01-LICENSE",
    ] {
        let f = Fixture::new("adm-flip");
        let path = f.app.join(target);
        let mut bytes = fs::read(&path).unwrap();
        bytes[0] ^= 1;
        let mode = fs::metadata(&path).unwrap().permissions().mode();
        fs::write(&path, &bytes).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
        assert_eq!(
            f.admit().unwrap_err(),
            "bundle_file_sha_mismatch",
            "{target}"
        );
    }
}

#[test]
fn size_changes_missing_files_and_extra_files_are_each_refused() {
    let f = Fixture::new("adm-size");
    let p = f.app.join("Contents/Frameworks/libmlxc.dylib");
    fs::write(&p, b"short").unwrap();
    assert_eq!(f.admit().unwrap_err(), "bundle_file_size_mismatch");
    let f = Fixture::new("adm-grow");
    let p = f.app.join("Contents/Frameworks/libmlxc.dylib");
    let mut b = fs::read(&p).unwrap();
    b.push(b'x');
    fs::write(&p, b).unwrap();
    assert_eq!(f.admit().unwrap_err(), "bundle_file_size_mismatch");
    let f = Fixture::new("adm-missing");
    fs::remove_file(f.app.join("Contents/Frameworks/libjaccl.dylib")).unwrap();
    assert_eq!(f.admit().unwrap_err(), "bundle_file_missing");
    let f = Fixture::new("adm-extra");
    write(&f.app, "Contents/Resources/extra.txt", b"x", 0o644);
    assert_eq!(f.admit().unwrap_err(), "bundle_unexpected_entry");
    let f = Fixture::new("adm-extra-root");
    write(&f.app, "stray", b"x", 0o644);
    assert_eq!(f.admit().unwrap_err(), "bundle_unexpected_entry");
    let f = Fixture::new("adm-empty-dir");
    fs::create_dir(f.app.join("Contents/Resources/empty")).unwrap();
    assert_eq!(f.admit().unwrap_err(), "bundle_unexpected_entry");
}

#[test]
fn a_second_metallib_under_the_old_resources_path_cannot_hide_beside_the_real_one() {
    let f = Fixture::new("adm-oldmetal");
    let real = fs::read(f.app.join("Contents/Frameworks/mlx.metallib")).unwrap();
    write(
        &f.app,
        "Contents/Resources/metal/mlx.metallib",
        &real,
        0o644,
    );
    assert_eq!(f.admit().unwrap_err(), "bundle_unexpected_entry");
}

#[test]
fn the_old_layout_alone_is_a_missing_file_not_an_admission() {
    let f = Fixture::new("adm-oldonly");
    let real = fs::read(f.app.join("Contents/Frameworks/mlx.metallib")).unwrap();
    fs::remove_file(f.app.join("Contents/Frameworks/mlx.metallib")).unwrap();
    write(
        &f.app,
        "Contents/Resources/metal/mlx.metallib",
        &real,
        0o644,
    );
    assert_eq!(f.admit().unwrap_err(), "bundle_unexpected_entry");
}

#[test]
fn symlinks_hardlinks_modes_and_a_symlinked_root_are_refused() {
    let f = Fixture::new("adm-link");
    let p = f.app.join("Contents/Frameworks/libmlx.dylib");
    let copy = f.app.join("Contents/Resources/licenses/01-LICENSE");
    fs::remove_file(&p).unwrap();
    symlink(&copy, &p).unwrap();
    assert_eq!(f.admit().unwrap_err(), "bundle_symlink_refused");
    let f = Fixture::new("adm-hard");
    let p = f.app.join("Contents/Frameworks/libmlx.dylib");
    fs::hard_link(&p, f.app.join("Contents/Frameworks/other-name")).unwrap();
    assert!(matches!(
        f.admit().unwrap_err(),
        "bundle_entry_not_regular" | "bundle_unexpected_entry"
    ));
    let f = Fixture::new("adm-hard2");
    let q = f.app.join("Contents/Frameworks/libmlx.dylib");
    let outside = f.app.parent().unwrap().join("outside-link");
    fs::hard_link(&q, &outside).unwrap();
    assert_eq!(f.admit().unwrap_err(), "bundle_entry_not_regular");
    let f = Fixture::new("adm-gw");
    fs::set_permissions(
        f.app.join("Contents/Frameworks/libmlx.dylib"),
        fs::Permissions::from_mode(0o664),
    )
    .unwrap();
    assert_eq!(f.admit().unwrap_err(), "bundle_file_mode");
    let f = Fixture::new("adm-noexec");
    fs::set_permissions(
        f.app.join(bundle_admission::EXECUTABLE),
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    assert_eq!(f.admit().unwrap_err(), "bundle_file_mode");
    let f = Fixture::new("adm-rootlink");
    let alias = f.app.parent().unwrap().join("alias.app");
    symlink(&f.app, &alias).unwrap();
    let expected = f.expected().unwrap();
    assert_eq!(
        bundle_admission::admit(&alias, &expected).unwrap_err().code,
        "bundle_root_invalid"
    );
    assert_eq!(
        bundle_admission::admit(&f.app.join("nope"), &expected)
            .unwrap_err()
            .code,
        "bundle_root_unreadable"
    );
}

fn with(f: &Fixture, edit: impl FnOnce(&mut Value)) -> device_harness::Result<Expected> {
    let mut v: Value = serde_json::from_slice(&f.pins).unwrap();
    edit(&mut v);
    Expected::from_documents(
        &f.manifest,
        &content(&f.manifest),
        &serde_json::to_vec(&v).unwrap(),
        &f.statics,
    )
}

#[test]
fn package_pins_and_manifest_must_agree_byte_for_byte() {
    let f = Fixture::new("adm-pins");
    let code = |r: device_harness::Result<Expected>| r.unwrap_err().code;
    assert_eq!(
        code(with(&f, |v| v[0]["sha256"] = json!("0".repeat(64)))),
        "bundle_pins_disagree"
    );
    assert_eq!(
        code(with(&f, |v| v[1]["bytes"] = json!(1))),
        "bundle_pins_disagree"
    );
    assert_eq!(
        code(with(&f, |v| {
            v.as_array_mut().unwrap().pop();
        })),
        "bundle_pins_disagree"
    );
    assert_eq!(
        code(with(&f, |v| {
            let d = v[0].clone();
            v.as_array_mut().unwrap()[1] = d;
        })),
        "bundle_pins_disagree"
    );
    assert_eq!(
        code(with(&f, |v| v[3]["path"] =
            json!("Contents/Resources/metal/mlx.metallib"))),
        "bundle_pins_disagree"
    );
    assert_eq!(
        code(with(&f, |v| v[3]["kind"] = json!("Dylib"))),
        "bundle_pins_disagree"
    );
    assert_eq!(
        code(with(&f, |v| v[0]["extra"] = json!(1))),
        "bundle_pins_invalid"
    );
    // The same native path listed twice (all four natives still present) is refused.
    assert_eq!(
        code(with(&f, |v| {
            let d = v[0].clone();
            v.as_array_mut().unwrap().push(d);
        })),
        "bundle_pins_disagree"
    );
    // Pin file that agrees but with the metallib moved to the old path is the pre-fix layout.
    let moved = with(&f, |v| {
        v[3]["path"] = json!("Contents/Frameworks/mlx.metallib")
    });
    assert!(moved.is_ok());
}

#[test]
fn the_manifest_must_match_its_independent_pin_and_have_the_closed_native_set() {
    let f = Fixture::new("adm-manifest");
    let mut wrong = content(&f.manifest);
    wrong.sha256 = "1".repeat(64);
    assert_eq!(
        Expected::from_documents(&f.manifest, &wrong, &f.pins, &f.statics)
            .unwrap_err()
            .code,
        "evidence_content_mismatch"
    );
    for (edit, name) in [
        ("schema", "bundle_manifest_invalid"),
        ("drop", "bundle_manifest_invalid"),
        ("dup", "bundle_manifest_invalid"),
    ] {
        let mut v: Value = serde_json::from_slice(&f.manifest).unwrap();
        match edit {
            "schema" => v["schema"] = json!("other"),
            "drop" => {
                v["native_inputs"].as_array_mut().unwrap().remove(0);
            }
            _ => v["native_inputs"][0] = v["native_inputs"][1].clone(),
        }
        let bytes = serde_json::to_vec(&v).unwrap();
        assert_eq!(
            Expected::from_documents(&bytes, &content(&bytes), &f.pins, &f.statics)
                .unwrap_err()
                .code,
            name,
            "{edit}"
        );
    }
}

fn repo(relative: &str) -> Vec<u8> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    fs::read(root.join(relative)).unwrap()
}

#[test]
fn the_committed_genuine_manifest_and_package_pins_describe_one_bundle_layout() {
    let manifest = repo("evidence/foundation-exit/native-inputs.json");
    let pins = repo("crates/desktop/package-pins.json");
    let stub = |name: &str| content(name.as_bytes());
    let statics = Statics {
        executable: stub("exe"),
        info_plist: stub("plist"),
        icon: stub("icon"),
    };
    // The manifest digest is pinned here independently of the manifest bytes.
    let pin = Content {
        size_bytes: 44299,
        sha256: "acae88fb9dc9cf5aa7d0d05a3c69ccfcbc72cac6ee69af60864333fa07c41544".into(),
    };
    let expected = Expected::from_documents(&manifest, &pin, &pins, &statics).unwrap();
    let paths: Vec<_> = expected.files().keys().map(String::as_str).collect();
    assert_eq!(paths.len(), 4 + 7 + 3);
    assert!(paths.contains(&"Contents/Frameworks/mlx.metallib"));
    assert!(paths.iter().all(|p| !p.contains("Resources/metal")));
    assert_eq!(
        paths
            .iter()
            .filter(|p| p.starts_with("Contents/Resources/licenses/"))
            .count(),
        7
    );
}

#[test]
fn a_recorded_size_that_differs_from_the_file_is_refused_even_when_the_digest_matches() {
    // The digest field is the file's real digest in both cases; only the recorded size is wrong,
    // in either direction.
    for delta in [1i64, -1] {
        let f = Fixture::new("adm-size-record");
        let mut v: Value = serde_json::from_slice(&f.manifest).unwrap();
        let n = v["native_inputs"][1]["size_bytes"].as_u64().unwrap() as i64 + delta;
        v["native_inputs"][1]["size_bytes"] = json!(n);
        let manifest = serde_json::to_vec(&v).unwrap();
        let mut pins: Value = serde_json::from_slice(&f.pins).unwrap();
        let pin = pins
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|p| p["path"] == "Contents/Frameworks/libmlx.dylib")
            .unwrap();
        pin["bytes"] = json!(n);
        let expected = Expected::from_documents(
            &manifest,
            &content(&manifest),
            &serde_json::to_vec(&pins).unwrap(),
            &f.statics,
        )
        .unwrap();
        assert_eq!(
            bundle_admission::admit(&f.app, &expected).unwrap_err().code,
            "bundle_file_size_mismatch",
            "{delta}"
        );
    }
}
