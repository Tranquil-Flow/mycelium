// SPDX-License-Identifier: AGPL-3.0-only
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, path::Path};
#[test]
fn unchanged_assets_match_frozen_oracle_inventory_and_digests() {
    let provenance: serde_json::Value =
        serde_json::from_str(include_str!("../assets.provenance.json")).unwrap();
    assert_eq!(
        provenance["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let assets = provenance["assets"].as_object().unwrap();
    let actual: BTreeSet<_> = std::fs::read_dir(root.join("assets"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    assert_eq!(actual, assets.keys().cloned().collect());
    for (name, expected) in assets {
        assert_eq!(
            format!(
                "{:x}",
                Sha256::digest(std::fs::read(root.join("assets").join(name)).unwrap())
            ),
            expected.as_str().unwrap(),
            "{name}"
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
