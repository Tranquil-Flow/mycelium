// SPDX-License-Identifier: AGPL-3.0-only
//! Owner ruling 25 (11 Nov): v0.2 has its OWN identity, so it sits beside v0.1.4 and never
//! touches its files (ruling 20). These pins replace the v0.1.4 identity the packaged app carried
//! before (`Mycelium A13 Demo` / `org.mycelium.a13-macos-demo`, `~/Applications/Mycelium A13.app`,
//! `Application Support/Mycelium/programs`, `Application Support/Mycelium A13 macOS Demo`).
use desktop::{bundle, install, launch_location};
use std::path::PathBuf;

/// Every name v0.1.4 creates under a user's home (installer.py:60-62,117,121; __main__.py:67;
/// finalize_a13_macos.py:135; scripts/a13_operator: `Mycelium A13 Demo/`) @ f4e366e9.
const V014_NAMES: [&str; 5] = [
    "Mycelium A13 Demo.app",
    "Mycelium A13.app",
    "Mycelium A13 macOS Demo",
    "Mycelium A13 Demo",
    "Mycelium",
];

#[test]
fn the_packaged_app_is_named_mycelium_with_bundle_id_org_mycelium_app() {
    assert_eq!(bundle::DISPLAY_NAME, "Mycelium");
    assert_eq!(bundle::BUNDLE_ID, "org.mycelium.app");
    let config: serde_json::Value =
        serde_json::from_str(include_str!("../tauri.packaged.conf.json")).unwrap();
    assert_eq!(config["productName"], "Mycelium");
    assert_eq!(config["identifier"], "org.mycelium.app");
    let plist = bundle::info_plist();
    for line in [
        "<key>CFBundleDisplayName</key>\n\t<string>Mycelium</string>",
        "<key>CFBundleName</key>\n\t<string>Mycelium</string>",
        "<key>CFBundleIdentifier</key>\n\t<string>org.mycelium.app</string>",
    ] {
        assert!(plist.contains(line), "{line}");
    }
    assert!(!plist.contains("A13"), "{plist}");
}

#[test]
fn v02_has_its_own_applications_entry_payload_directory_and_data_folder() {
    assert_eq!(install::APPLICATIONS_ENTRY, "Mycelium.app");
    assert_eq!(install::PAYLOAD_DIRECTORY, "Mycelium.app");
    assert_eq!(install::SUPPORT_FOLDER, "org.mycelium.app");
    // The app's own roots (`$HOME`, read only: asking creates nothing).
    let roots = install::Roots::for_current_user().unwrap();
    let home_buf = PathBuf::from(std::env::var("HOME").unwrap());
    let home = home_buf.as_path();
    let support = home.join("Library/Application Support/org.mycelium.app");
    assert_eq!(roots.programs(), support.join("programs"));
    assert_eq!(roots.state(), support.join("state"));
    assert_eq!(
        launch_location::programs_root(home),
        support.join("programs")
    );
    assert_eq!(
        roots.application_entry(),
        home.join("Applications/Mycelium.app")
    );
    // No v0.2 path component is, or starts like, a name v0.1.4 creates.
    for path in [roots.programs(), roots.state(), roots.application_entry()] {
        for component in path.strip_prefix(home).unwrap().components() {
            let c = component.as_os_str().to_string_lossy();
            assert!(
                !V014_NAMES.iter().any(|v014| c == *v014) && !c.starts_with("Mycelium A13"),
                "{} reuses the v0.1.4 name {c}",
                path.display()
            );
        }
    }
}
