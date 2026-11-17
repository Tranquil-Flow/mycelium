// SPDX-License-Identifier: AGPL-3.0-only
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, path::Path};

/// The v0.1.4 oracle commit. Changing the app's page assets does not change which upstream the
/// carried-over files came from, so this stays pinned.
const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";

fn digest(root: &Path, name: &str) -> String {
    format!(
        "{:x}",
        Sha256::digest(std::fs::read(root.join("assets").join(name)).unwrap())
    )
}

/// v0.2 no longer ships every page asset byte-identically: the theme round replaced the window-confirm
/// gates and recoloured the page, and added the website's living-background renderer. The parity claim
/// that survives is narrower, so it is asserted narrowly instead of being dropped:
///
/// - every file in `assets/` is accounted for exactly once, as unchanged / diverged / added;
/// - a file listed `unchanged` still hashes to its v0.1.4 oracle digest;
/// - a file listed `diverged` or `added` hashes to the digest recorded beside it, and every
///   divergence states why, so the difference stays evidence rather than drift.
#[test]
fn carried_over_assets_keep_their_oracle_digests_and_divergences_are_recorded() {
    let provenance: serde_json::Value =
        serde_json::from_str(include_str!("../assets.provenance.json")).unwrap();
    assert_eq!(provenance["oracle"], ORACLE);
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));

    let section = |key: &str| provenance[key].as_object().unwrap().clone();
    let unchanged = section("unchanged");
    let diverged = section("diverged");
    let added = section("added");

    let actual: BTreeSet<String> = std::fs::read_dir(root.join("assets"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    let mut recorded: Vec<String> = Vec::new();
    recorded.extend(unchanged.keys().cloned());
    recorded.extend(diverged.keys().cloned());
    recorded.extend(added.keys().cloned());
    let recorded_set: BTreeSet<String> = recorded.iter().cloned().collect();
    assert_eq!(
        recorded.len(),
        recorded_set.len(),
        "a file is recorded in more than one section"
    );
    assert_eq!(
        actual, recorded_set,
        "assets/ and the provenance record disagree"
    );

    for (name, expected) in &unchanged {
        assert_eq!(
            digest(root, name),
            expected.as_str().unwrap(),
            "{name} is listed as unchanged from {ORACLE} but its bytes moved",
        );
    }
    for name in diverged.keys().chain(added.keys()) {
        let entry = &provenance["diverged"][name];
        let entry = if entry.is_null() {
            &provenance["added"][name]
        } else {
            entry
        };
        assert_eq!(
            digest(root, name),
            entry["sha256"].as_str().unwrap(),
            "{name}: recorded digest does not match the shipped bytes",
        );
    }
    for name in diverged.keys() {
        let reason = provenance["diverged"][name]["reason"].as_str().unwrap();
        assert!(
            reason.len() > 40,
            "{name} diverges from {ORACLE} without a real reason recorded",
        );
    }
    assert_eq!(
        format!(
            "{:x}",
            Sha256::digest(std::fs::read(root.join("icons/icon.png")).unwrap())
        ),
        provenance["icon"]["sha256"].as_str().unwrap()
    );
}
#[test]
fn tauri_config_grants_no_native_ipc_and_never_bundles_a_release_by_default() {
    let config: serde_json::Value =
        serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
    assert_eq!(
        config["app"]["security"]["capabilities"],
        serde_json::json!([])
    );
    assert_eq!(config["app"]["withGlobalTauri"], false);
    assert_eq!(config["app"]["windows"], serde_json::json!([]));
    assert_eq!(config["bundle"]["active"], false);
    assert_eq!(config["bundle"]["macOS"]["minimumSystemVersion"], "15.6.1");
    assert_eq!(config["bundle"]["macOS"]["signingIdentity"], "-");
}
