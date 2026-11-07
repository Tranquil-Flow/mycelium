// SPDX-License-Identifier: AGPL-3.0-only
use obligations::{Disposition, checked_in, validate};
use serde_json::Value;
use std::collections::BTreeSet;

#[test]
fn complete_provisional_inventory_is_consistent() {
    let (registry, snapshot) = checked_in().unwrap();
    assert_eq!(
        snapshot.source_sha,
        "f5bfb70981569c79cb873afe8e0c5be20342db7e"
    );
    assert_eq!(snapshot.packaged_source_paths.len(), 725); // supplied checkpoint
    assert_eq!(snapshot.a6_paths.len(), 1336); // source manifest at this pin
    assert_eq!(snapshot.contract_protocols.len(), 87); // contract_audit registry
    assert_eq!(registry.test_groups.len(), snapshot.test_paths.len());
    assert_eq!(registry.paths.len(), snapshot.files.len());
    assert_eq!(validate(&registry, &snapshot), Ok(()));
    assert!(
        registry
            .paths
            .iter()
            .all(|p| !matches!(p.disposition, Disposition::Retire { .. }))
    );
    println!(
        "inventory: {} paths, {} packaged sources, {} A6 paths, {} contracts, {} test/support groups, {} escapes; zero unmapped",
        registry.paths.len(),
        snapshot.packaged_source_paths.len(),
        snapshot.a6_paths.len(),
        registry.contracts.len(),
        registry.test_groups.len(),
        registry.escapes.len()
    );
}
#[test]
fn every_baseline_path_is_a_real_obligation_not_a_family_wildcard() {
    let (registry, snapshot) = checked_in().unwrap();
    for file in &snapshot.files {
        let rows: Vec<_> = registry
            .paths
            .iter()
            .filter(|p| p.path == file.path)
            .collect();
        assert_eq!(rows.len(), 1, "{}", file.path);
        assert!(
            registry
                .obligations
                .iter()
                .any(|o| o.id == rows[0].obligation_id)
        );
        assert!(!file.path.contains('*'));
    }
}
#[test]
fn removing_a_real_shipped_path_is_rejected() {
    let (mut registry, snapshot) = checked_in().unwrap();
    registry
        .paths
        .retain(|p| p.path != "physical_inference_node.py");
    let errors = validate(&registry, &snapshot).unwrap_err();
    assert!(
        errors
            .iter()
            .any(|e| e.code == "unmapped_path" && e.subject == "physical_inference_node.py")
    );
}
#[test]
fn contract_crosswalk_preserves_all_original_fixture_and_owner_pins() {
    let old: Value = serde_json::from_str(include_str!("../data/contract-manifest.json")).unwrap();
    let crosswalk: Vec<Value> =
        serde_json::from_str(include_str!("../data/contracts.json")).unwrap();
    let manifest = old["contracts"].as_array().unwrap();
    assert_eq!(crosswalk.len(), manifest.len());
    for contract in manifest {
        let row = crosswalk
            .iter()
            .find(|r| r["source"] == contract["protocol"])
            .unwrap();
        assert_eq!(row["fixture"], contract["fixture"]);
        assert_eq!(row["owner_sources"], contract["owner_sources"]);
    }
}
#[test]
fn a6_is_reconciled_as_changed_source_not_mislabelled_as_shipped_closure() {
    let (_, snapshot) = checked_in().unwrap();
    let a6: Value = serde_json::from_str(include_str!("../data/a6-source-manifest.json")).unwrap();
    let rows = a6["files"].as_array().unwrap();
    let expected: BTreeSet<_> = rows.iter().map(|r| r["path"].as_str().unwrap()).collect();
    let observed: BTreeSet<_> = snapshot.a6_paths.iter().map(String::as_str).collect();
    assert_eq!(observed, expected);
    assert_ne!(snapshot.a6_paths, snapshot.packaged_source_paths);
    for row in rows {
        let pin = snapshot
            .files
            .iter()
            .find(|p| p.path == row["path"].as_str().unwrap())
            .unwrap();
        assert_eq!(
            format!("sha256:{}", pin.sha256),
            row["sha256"].as_str().unwrap(),
            "{}",
            pin.path
        );
        assert_eq!(
            pin.size_bytes,
            row["size_bytes"].as_u64().unwrap(),
            "{}",
            pin.path
        );
    }
}
#[test]
fn incident_inventory_is_not_a_red_or_release_claim() {
    let (registry, _) = checked_in().unwrap();
    let actual: BTreeSet<_> = registry.escapes.iter().map(|e| e.id.as_str()).collect();
    let expected = BTreeSet::from([
        "F1", "F2", "F3", "F4", "F5", "F13", "F14", "F15", "F15b", "F16",
    ]);
    assert_eq!(actual, expected);
    assert!(
        registry
            .escapes
            .iter()
            .all(|e| e.status == "inventory_only")
    );
    let f16 = registry.escapes.iter().find(|e| e.id == "F16").unwrap();
    assert!(f16.historical_target.contains("52d1b69f"));
    assert!(f16.historical_target.contains("accepted L13"));
}
#[test]
fn supported_cells_separate_registered_models_from_reachable_schemas() {
    let (registry, _) = checked_in().unwrap();
    let models: BTreeSet<_> = registry
        .supported_cells
        .iter()
        .filter(|c| c.scope == "registered_macos_model")
        .map(|c| c.values["model_id"].as_str().unwrap())
        .collect();
    assert_eq!(
        models,
        BTreeSet::from([
            "Qwen/Qwen2.5-0.5B-Instruct",
            "Qwen/Qwen2.5-7B-Instruct",
            "Qwen/Qwen3-0.6B",
            "Qwen/Qwen3-8B",
            "Qwen/Qwen3-14B",
        ])
    );
    let os = registry
        .supported_cells
        .iter()
        .find(|c| c.id == "os:macos-arm64")
        .unwrap();
    assert_eq!(os.values["lowest_admitted_macos"], "15.6.1");
    assert_eq!(
        os.values["admitted_versions"],
        serde_json::json!(["15.6.1", "26.5.1"])
    );
    assert!(os.values["final_package_LSMinimumSystemVersion"].is_null());
    assert!(!os.unknowns.is_empty());
    let mode = registry
        .supported_cells
        .iter()
        .find(|c| c.id == "profile:normal-fast")
        .unwrap();
    assert_eq!(mode.values["fresh_preference_enabled"], false);
}
