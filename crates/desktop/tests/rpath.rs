// SPDX-License-Identifier: AGPL-3.0-only
//! Relocatable executable rpath (`@executable_path/../Frameworks`) for the MLX-linked
//! executable, checked with `otool` on real Mach-O files compiled here (tiny C probes, no
//! network, nothing installed) and proven with the dynamic loader by moving the bundle.
#![cfg(target_os = "macos")]
use desktop::{
    bundle::{self, BundleError},
    package,
};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::atomic::{AtomicUsize, Ordering},
};

static SEQ: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l11-rpath-{}-{}",
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
fn clang(args: &[&str]) {
    let out = Command::new("/usr/bin/clang").args(args).output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
/// `<root>/Probe.app/Contents/{MacOS/probe, Frameworks/libprobe.dylib}`; the executable links
/// `@rpath/libprobe.dylib` and carries exactly the rpaths given.
fn probe_app(root: &Path, rpaths: &[&str]) -> PathBuf {
    let app = root.join("Probe.app");
    fs::create_dir_all(app.join("Contents/MacOS")).unwrap();
    fs::create_dir_all(app.join("Contents/Frameworks")).unwrap();
    fs::write(root.join("lib.c"), "int probe_value(void){return 7;}\n").unwrap();
    fs::write(
        root.join("main.c"),
        "int probe_value(void);\nint main(void){return probe_value()==7?0:3;}\n",
    )
    .unwrap();
    let lib = app.join("Contents/Frameworks/libprobe.dylib");
    clang(&[
        "-dynamiclib",
        "-install_name",
        "@rpath/libprobe.dylib",
        root.join("lib.c").to_str().unwrap(),
        "-o",
        lib.to_str().unwrap(),
    ]);
    let exe = app.join("Contents/MacOS/probe");
    let mut args: Vec<String> = vec![
        root.join("main.c").to_str().unwrap().into(),
        lib.to_str().unwrap().into(),
        "-o".into(),
        exe.to_str().unwrap().into(),
    ];
    for rpath in rpaths {
        args.push(format!("-Wl,-rpath,{rpath}"));
    }
    clang(&args.iter().map(String::as_str).collect::<Vec<_>>());
    app
}
fn exe(app: &Path) -> PathBuf {
    app.join("Contents/MacOS/probe")
}
fn run(app: &Path) -> Option<i32> {
    Command::new(exe(app)).status().unwrap().code()
}

#[test]
fn otool_output_parsing_is_exact_about_rpaths_and_dependencies() {
    let l = "x:\nLoad command 9\n          cmd LC_RPATH\n      cmdsize 48\n         path @executable_path/../Frameworks (offset 12)\nLoad command 10\n          cmd LC_RPATH\n      cmdsize 32\n         path /Users/x/lib (offset 12)\nLoad command 11\n          cmd LC_LOAD_DYLIB\n         name /not/an/rpath (offset 24)\n";
    assert_eq!(
        bundle::parse_rpaths(l),
        vec!["@executable_path/../Frameworks", "/Users/x/lib"]
    );
    let big_l = "x:\n\t@rpath/libmlx.dylib (compatibility version 0.0.0, current version 0.0.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)\n";
    assert_eq!(
        bundle::parse_dependencies(big_l),
        vec!["@rpath/libmlx.dylib", "/usr/lib/libSystem.B.dylib"]
    );
}

#[test]
fn rpath_policy_accepts_only_bundle_relative_directories() {
    for good in [
        bundle::FRAMEWORKS_RPATH,
        "@executable_path/../Frameworks/sub",
        "@loader_path",
        "@loader_path/sub",
    ] {
        assert!(package::relocatable_rpath(good), "{good}");
    }
    for bad in [
        "",
        "/Users/someone/mycelium-rs/target/release/build/mlx",
        "/opt/homebrew/lib",
        "/usr/local/lib",
        "@executable_path/../../elsewhere",
        "@loader_path/../Frameworks",
        "@loader_path/../../x",
        "@executable_path/../Frameworks/../x",
        "@executable_path/../Frameworks2",
        "@executable_path",
        "../Frameworks",
        "@rpath/x",
    ] {
        assert!(!package::relocatable_rpath(bad), "{bad:?}");
    }
}

#[test]
fn an_executable_with_the_frameworks_rpath_passes_the_link_check() {
    let tree = Tree::new();
    let app = probe_app(&tree.0, &[bundle::FRAMEWORKS_RPATH]);
    assert_eq!(
        bundle::executable_rpaths(&exe(&app)).unwrap(),
        vec![bundle::FRAMEWORKS_RPATH]
    );
    assert_eq!(bundle::check_executable_links(&exe(&app)), Ok(()));
}

#[test]
fn an_absolute_rpath_is_refused_as_is_a_missing_one_for_an_rpath_dependency() {
    let tree = Tree::new();
    let absolute = probe_app(&tree.0, &["/Users/builder/target/release/build/mlx/lib"]);
    assert_eq!(
        bundle::check_executable_links(&exe(&absolute)),
        Err(BundleError::Rpath(
            "/Users/builder/target/release/build/mlx/lib".into()
        ))
    );
    let other = Tree::new();
    let none = probe_app(&other.0, &[]);
    assert_eq!(
        bundle::check_executable_links(&exe(&none)),
        Err(BundleError::Rpath("missing_frameworks_rpath".into()))
    );
}

#[test]
fn a_non_mach_o_file_has_no_links_to_check() {
    let tree = Tree::new();
    let file = tree.path("plain");
    fs::write(&file, b"shell executable bytes").unwrap();
    assert_eq!(bundle::check_executable_links(&file), Ok(()));
    assert!(bundle::executable_rpaths(&file).unwrap().is_empty());
}

#[test]
fn the_relocatable_bundle_runs_after_being_moved_and_the_absolute_one_does_not() {
    let tree = Tree::new();
    let good = probe_app(&tree.path("a"), &[bundle::FRAMEWORKS_RPATH]);
    assert_eq!(run(&good), Some(0), "runs where it was built");
    let moved_root = tree.path("elsewhere/deeper");
    fs::create_dir_all(&moved_root).unwrap();
    desktop::install::ditto_copy(&good, &moved_root.join("Probe.app")).unwrap();
    fs::remove_dir_all(tree.path("a")).unwrap();
    assert_eq!(
        run(&moved_root.join("Probe.app")),
        Some(0),
        "runs after the move"
    );

    // Control: the same probe with an absolute rpath into its build directory breaks when moved.
    let built = tree.path("b");
    let bad = probe_app(
        &built,
        &[built
            .join("Probe.app/Contents/Frameworks")
            .to_str()
            .unwrap()],
    );
    assert_eq!(run(&bad), Some(0), "control runs where it was built");
    let relocated = tree.path("c");
    fs::create_dir_all(&relocated).unwrap();
    desktop::install::ditto_copy(&bad, &relocated.join("Probe.app")).unwrap();
    fs::remove_dir_all(&built).unwrap();
    assert_ne!(
        run(&relocated.join("Probe.app")),
        Some(0),
        "the absolute rpath must fail once the build directory is gone"
    );
}

// ---- bundle assembly uses the same check --------------------------------------------------

fn native_fixture(tree: &Tree) -> PathBuf {
    let dylib = |name: &str, bytes: &[u8], role: &str| {
        let path = tree.path(name);
        fs::write(&path, bytes).unwrap();
        use sha2::{Digest, Sha256};
        serde_json::json!({
            "path": path, "path_base": "absolute", "size_bytes": bytes.len(),
            "sha256": format!("{:x}", Sha256::digest(bytes)), "role": role,
        })
    };
    let d = "linked_or_transitive_dylib";
    let inputs = serde_json::json!({
        "schema": bundle::NATIVE_INPUT_SCHEMA, "observed_at": "2026-10-28T00:00:00Z",
        "native_inputs": [
            dylib("libmlxc.dylib", b"c", d), dylib("libmlx.dylib", b"m", d),
            dylib("libjaccl.dylib", b"j", d), dylib("mlx.metallib", b"l", "runtime_metal_library"),
        ],
        "license_notices": [dylib("LICENSE", b"license", "")],
    });
    let manifest = tree.path("native-inputs.json");
    fs::write(&manifest, serde_json::to_vec(&inputs).unwrap()).unwrap();
    manifest
}

#[test]
fn bundle_assembly_refuses_an_executable_with_an_absolute_rpath_and_builds_with_the_relocatable_one()
 {
    let tree = Tree::new();
    let manifest = native_fixture(&tree);
    let icon = Path::new(env!("CARGO_MANIFEST_DIR")).join("icons/icon-windowed.icns");
    let out = tree.path("out");
    fs::create_dir(&out).unwrap();

    let bad = Tree::new();
    let bad_app = probe_app(&bad.0, &["/Users/builder/mlx/lib"]);
    let refused = bundle::build(&bundle::Spec {
        executable: &exe(&bad_app),
        native_inputs: &manifest,
        icon: &icon,
        out_dir: &out,
        expected_native_inputs_sha256: None,
    });
    assert_eq!(
        refused.err(),
        Some(BundleError::Rpath("/Users/builder/mlx/lib".into()))
    );
    assert!(
        !out.join("Mycelium A13 Demo.app").exists(),
        "a refused build leaves no bundle"
    );

    let good = Tree::new();
    let good_app = probe_app(&good.0, &[bundle::FRAMEWORKS_RPATH]);
    let built = bundle::build(&bundle::Spec {
        executable: &exe(&good_app),
        native_inputs: &manifest,
        icon: &icon,
        out_dir: &out,
        expected_native_inputs_sha256: None,
    })
    .unwrap();
    assert_eq!(
        bundle::executable_rpaths(&built.app.join("Contents/MacOS/mycelium")).unwrap(),
        vec![bundle::FRAMEWORKS_RPATH]
    );
}
