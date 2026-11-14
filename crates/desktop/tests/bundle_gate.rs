// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit gate over a REAL built bundle (never counted by the default run):
//!
//! ```text
//! MYCELIUM_BUNDLE_APP="/path/Mycelium.app" cargo test --offline --locked -j 2 \
//!   -p desktop --test bundle_gate -- --ignored --test-threads=1
//! ```
//!
//! Reads and inspects only; it never launches, signs, installs or modifies the app.
#![cfg(target_os = "macos")]
use desktop::{
    bundle,
    package::{self, Manifest},
};
use std::{path::PathBuf, process::Command};

fn app() -> PathBuf {
    PathBuf::from(
        std::env::var("MYCELIUM_BUNDLE_APP")
            .expect("MYCELIUM_BUNDLE_APP must name the built .app (this gate has no default)"),
    )
}
fn tool(program: &str, args: &[&str], file: &std::path::Path) -> String {
    let output = Command::new(program).args(args).arg(file).output().unwrap();
    assert!(
        output.status.success(),
        "{program} {args:?} {}",
        file.display()
    );
    String::from_utf8(output.stdout).unwrap()
}
fn is_macho(path: &std::path::Path) -> bool {
    use std::io::Read;
    let mut magic = [0u8; 4];
    std::fs::File::open(path)
        .and_then(|mut f| f.read_exact(&mut magic))
        .is_ok()
        && matches!(
            magic,
            [0xcf, 0xfa, 0xed, 0xfe] | [0xca, 0xfe, 0xba, 0xbe] | [0xfe, 0xed, 0xfa, 0xcf]
        )
}
fn macho_files(app: &std::path::Path) -> Vec<PathBuf> {
    bundle::census(app)
        .unwrap()
        .files
        .into_iter()
        .map(|(relative, _)| app.join(relative))
        .filter(|path| is_macho(path))
        .collect()
}

#[test]
#[ignore = "explicit gate over a built bundle; set MYCELIUM_BUNDLE_APP"]
fn built_bundle_contains_no_python_by_name_content_shebang_or_load_command() {
    let app = app();
    assert_eq!(
        bundle::audit_no_python(&app).unwrap(),
        vec![],
        "files, names, shebangs, marker bytes"
    );
    let machos = macho_files(&app);
    assert!(
        machos.len() >= 4,
        "executable plus three dylibs: {machos:?}"
    );
    for path in machos {
        let links = tool("/usr/bin/otool", &["-L"], &path).to_ascii_lowercase();
        let commands = tool("/usr/bin/otool", &["-l"], &path).to_ascii_lowercase();
        assert!(!links.contains("python"), "{}: {links}", path.display());
        assert!(!commands.contains("python"), "{}", path.display());
    }
}

#[test]
#[ignore = "explicit gate over a built bundle; set MYCELIUM_BUNDLE_APP"]
fn built_bundle_links_and_rpaths_are_relocatable_with_no_workspace_or_package_manager_path() {
    let app = app();
    for path in macho_files(&app) {
        let links = tool("/usr/bin/otool", &["-L"], &path);
        for line in links.lines().skip(1) {
            let dependency = line.split(" (").next().unwrap().trim();
            assert!(
                package::relocatable_link(dependency),
                "{}: {dependency}",
                path.display()
            );
        }
        let commands = tool("/usr/bin/otool", &["-l"], &path);
        let mut rpaths = 0;
        let mut lines = commands.lines();
        while let Some(line) = lines.next() {
            if line.trim() == "cmd LC_RPATH" {
                let rpath = lines
                    .by_ref()
                    .find(|l| l.trim_start().starts_with("path "))
                    .unwrap();
                let value = rpath
                    .trim_start()
                    .trim_start_matches("path ")
                    .split(" (")
                    .next()
                    .unwrap();
                // An rpath directory is acceptable only as a relocatable prefix.
                assert!(
                    package::relocatable_link(&format!("{value}/x")),
                    "{}: {value}",
                    path.display()
                );
                rpaths += 1;
            }
        }
        // Drop each tool's `file:` header line: it names the bundle location itself.
        let body = |text: &str| text.lines().skip(1).collect::<Vec<_>>().join("\n");
        let (links, commands) = (body(&links), body(&commands));
        for forbidden in [
            "/Users/",
            "/opt/homebrew",
            "/usr/local",
            "/Volumes/",
            "/private/",
        ] {
            assert!(
                !links.contains(forbidden) && !commands.contains(forbidden),
                "{}: {forbidden}",
                path.display()
            );
        }
        eprintln!(
            "MACHO {} rpaths={rpaths}",
            path.strip_prefix(&app).unwrap().display()
        );
    }
}

#[test]
#[ignore = "explicit gate over a built bundle; set MYCELIUM_BUNDLE_APP"]
fn built_bundle_matches_its_content_manifest_and_info_plist_identity() {
    let app = app();
    let out = app.parent().unwrap();
    let content: serde_json::Value =
        serde_json::from_slice(&std::fs::read(out.join("CONTENT-MANIFEST.json")).unwrap()).unwrap();
    let manifest = Manifest {
        schema: 1,
        entries: content["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| package::Entry {
                path: e["path"].as_str().unwrap().into(),
                kind: match e["kind"].as_str().unwrap() {
                    "Executable" => package::Kind::Executable,
                    "Dylib" => package::Kind::Dylib,
                    "Metal" => package::Kind::Metal,
                    _ => package::Kind::Resource,
                },
                bytes: e["bytes"].as_u64().unwrap(),
                sha256: e["sha256"].as_str().unwrap().into(),
            })
            .collect(),
    };
    assert_eq!(package::verify(&app, &manifest), Ok(()));
    let census = bundle::census(&app).unwrap();
    assert!(census.symlinks.is_empty());
    assert_eq!(
        census.files.len(),
        manifest.entries.len(),
        "no unlisted file in the bundle"
    );
    assert_eq!(
        census.files.iter().map(|f| f.1).sum::<u64>(),
        content["totals"]["content_bytes"].as_u64().unwrap()
    );
    let listing = std::fs::read_to_string(out.join("APP-FILES.sha256")).unwrap();
    assert_eq!(listing, bundle::app_files_listing(&app).unwrap());
    // Native payload identities come from the genuine manifest, not from the bundle.
    let pins: Vec<package::Entry> =
        serde_json::from_str(include_str!("../package-pins.json")).unwrap();
    for pin in pins {
        let entry = manifest
            .entries
            .iter()
            .find(|e| e.path == pin.path)
            .unwrap();
        assert_eq!(
            (entry.bytes, &entry.sha256),
            (pin.bytes, &pin.sha256),
            "{}",
            pin.path
        );
    }
    let plist = app.join("Contents/Info.plist");
    assert!(tool("/usr/bin/plutil", &["-lint"], &plist).contains("OK"));
    let json: serde_json::Value = serde_json::from_str(&tool(
        "/usr/bin/plutil",
        &["-convert", "json", "-o", "-"],
        &plist,
    ))
    .unwrap();
    assert_eq!(json["CFBundleIdentifier"], bundle::BUNDLE_ID);
    assert_eq!(json["CFBundleName"], bundle::DISPLAY_NAME);
    assert_eq!(json["CFBundleDisplayName"], bundle::DISPLAY_NAME);
    assert_eq!(json["CFBundleExecutable"], bundle::EXECUTABLE);
    assert_eq!(json["CFBundleIconFile"], bundle::ICON_FILE);
    assert_eq!(json["LSMinimumSystemVersion"], bundle::MINIMUM_OS);
    assert_eq!(json["NSHighResolutionCapable"], true);
    assert_eq!(
        json["NSLocalNetworkUsageDescription"],
        bundle::LOCAL_NETWORK_USAGE
    );
    // Unsigned local build: there must be no signature or CodeResources to mistake for a seal.
    assert!(!app.join("Contents/_CodeSignature").exists());
}

#[test]
#[ignore = "explicit gate over a built bundle; set MYCELIUM_BUNDLE_APP"]
fn built_executable_carries_exactly_the_relocatable_frameworks_rpath_and_every_rpath_dependency_resolves()
 {
    let app = app();
    let exe = app.join("Contents/MacOS/mycelium");
    // Prepared for the MLX-linked executable: dyld finds `@rpath/libmlx.dylib` next to the
    // executable, wherever the bundle lives, and nothing else is on the search path.
    assert_eq!(
        bundle::executable_rpaths(&exe).unwrap(),
        vec![bundle::FRAMEWORKS_RPATH],
        "the executable must carry exactly @executable_path/../Frameworks"
    );
    assert_eq!(bundle::check_executable_links(&exe), Ok(()));
    let commands = tool("/usr/bin/otool", &["-l"], &exe);
    // The tool's first line names the file (and so the bundle location itself).
    let commands: String = commands.lines().skip(1).collect::<Vec<_>>().join("\n");
    for forbidden in [
        "/Users/",
        "/opt/homebrew",
        "/usr/local",
        "/Volumes/",
        "/private/",
    ] {
        assert!(!commands.contains(forbidden), "{forbidden}");
    }
    // Every @rpath dependency (libmlxc, libmlx once the executable links MLX) resolves to a bundled file.
    for dependency in bundle::parse_dependencies(&tool("/usr/bin/otool", &["-L"], &exe)) {
        if let Some(name) = dependency.strip_prefix("@rpath/") {
            assert!(
                app.join("Contents/Frameworks").join(name).is_file(),
                "{dependency} does not resolve inside Contents/Frameworks"
            );
        }
    }
}

#[test]
#[ignore = "explicit gate over a built bundle; set MYCELIUM_BUNDLE_APP"]
fn the_packaged_executable_really_links_the_bundled_mlx_libraries_and_nothing_foreign() {
    let exe = app().join("Contents/MacOS/mycelium");
    let dependencies = bundle::parse_dependencies(&tool("/usr/bin/otool", &["-L"], &exe));
    // A shell-only executable would pass every other gate; this one proves the core is linked.
    assert!(
        dependencies.iter().any(|d| d == "@rpath/libmlxc.dylib"),
        "{dependencies:?}"
    );
    // libmlx is a dependency of libmlxc, not of the executable; its install name resolves from
    // the same Frameworks rpath.
    let mlxc = app().join("Contents/Frameworks/libmlxc.dylib");
    let inner = bundle::parse_dependencies(&tool("/usr/bin/otool", &["-L"], &mlxc));
    assert!(
        inner.iter().any(|d| d == "@rpath/libmlx.dylib"),
        "{inner:?}"
    );
    // No dependency outside the bundle, the system, or Apple frameworks.
    for dependency in &dependencies {
        assert!(
            dependency.starts_with("@rpath/")
                || dependency.starts_with("/usr/lib/")
                || dependency.starts_with("/System/Library/"),
            "foreign dependency {dependency}"
        );
    }
}
