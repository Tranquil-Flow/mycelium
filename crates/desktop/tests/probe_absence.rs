// SPDX-License-Identifier: AGPL-3.0-only
//! The default bundle must contain no probe code. Probe hooks (attack-target stubs, title
//! receipts, page events, navigation/browser capture, the slice driver script) exist only in
//! builds that name them (`qualification-probe`, or a debug `dev-shell`), never in the shipped
//! bundle. Two explicit checks over REAL built bundles (reads bytes only, never launches):
//!
//! ```text
//! MYCELIUM_BUNDLE_APP="/path/Mycelium.app" \
//! MYCELIUM_PROBE_BUNDLE_APP="/path/probe/Mycelium.app" \
//!   cargo test --offline --locked -j 2 -p desktop --test probe_absence -- --ignored --test-threads=1
//! ```
//!
//! The probe bundle is the positive control: the scanner must see every marker there, so a
//! scanner that cannot see them cannot pass the default bundle.
#![cfg(target_os = "macos")]
use desktop::bundle;
use std::path::{Path, PathBuf};

/// Strings that only the probe hooks and the slice driver script contain.
const PROBE_MARKERS: [&str; 11] = [
    "L11_ATTACK_TARGET_REACHED_FAILED",
    "L11_NAVIGATION_DENIED",
    "L11_SYSTEM_BROWSER_CAPTURE",
    "L11_CHANNEL_REFUSAL",
    "l11-title-recorded",
    "l11-popup-blocked",
    "l11-navigation-blocked",
    "L11_WEBVIEW_INACTIVE_POLICY",
    "L11_WINDOW_OCCLUSION",
    "L11_SLICE_CHAT_DOM_TOKEN",
    "L11_PACKAGED_CHAT_ROUTE_NOT_READY",
];

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}
/// Markers present anywhere in the bundle's regular files (whole files, no read boundary).
fn markers_in(app: &Path) -> Vec<(String, &'static str)> {
    let mut found = Vec::new();
    for (relative, _) in bundle::census(app).unwrap().files {
        let path = app.join(&relative);
        // Large payloads (metallib, dylibs) are scanned too: a probe string can hide anywhere.
        let bytes = std::fs::read(&path).unwrap();
        for marker in PROBE_MARKERS {
            if contains(&bytes, marker.as_bytes()) {
                found.push((relative.clone(), marker));
            }
        }
    }
    found
}
fn app(var: &str) -> PathBuf {
    PathBuf::from(std::env::var(var).unwrap_or_else(|_| panic!("{var} must name a built .app")))
}

#[test]
fn the_scanner_finds_a_marker_split_by_nothing_and_ignores_near_misses() {
    let dir = std::env::temp_dir().join(format!("v02-l11-absence-{}", std::process::id()));
    let app = dir.join("Fake.app");
    std::fs::create_dir_all(app.join("Contents/MacOS")).unwrap();
    std::fs::write(app.join("Contents/MacOS/x"), b"..L11_NAVIGATION_DENIED:..").unwrap();
    std::fs::write(
        app.join("Contents/MacOS/y"),
        b"L11_NAVIGATION_DENIE l11-title",
    )
    .unwrap();
    let found = markers_in(&app);
    std::fs::remove_dir_all(&dir).unwrap();
    assert_eq!(
        found,
        vec![("Contents/MacOS/x".to_string(), "L11_NAVIGATION_DENIED")]
    );
}

#[test]
#[ignore = "explicit gate over a built DEFAULT bundle; set MYCELIUM_BUNDLE_APP"]
fn the_default_bundle_contains_no_probe_code() {
    let found = markers_in(&app("MYCELIUM_BUNDLE_APP"));
    assert!(
        found.is_empty(),
        "probe code ships in the bundle: {found:?}"
    );
}

#[test]
#[ignore = "positive control over a built qualification-probe bundle; set MYCELIUM_PROBE_BUNDLE_APP"]
fn the_scanner_sees_every_marker_in_the_probe_bundle() {
    let found = markers_in(&app("MYCELIUM_PROBE_BUNDLE_APP"));
    for marker in PROBE_MARKERS {
        assert!(
            found.iter().any(|(_, m)| *m == marker),
            "scanner cannot see {marker}: {found:?}"
        );
    }
}
