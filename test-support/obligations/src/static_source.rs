// SPDX-License-Identifier: AGPL-3.0-only
//! Accepted static-source identity overlay; never runtime/retirement authority.
use crate::{FilePin, Registry, package::PackageCensus};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema: String,
    pub source_sha: String,
    pub packaged_source_paths: Vec<String>,
    pub resource_paths: Vec<String>,
    pub profile_source_paths: Vec<String>,
    pub derivation_inputs: Vec<String>,
    pub files: Vec<FilePin>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub schema: String,
    pub source_sha: String,
    pub history_files: Vec<FilePin>,
    pub crosswalk: Vec<serde_json::Value>,
    pub profiles: Vec<serde_json::Value>,
    pub unproven: Vec<String>,
    pub retirements: u64,
}
pub fn accepted_manifest() -> Result<Manifest, serde_json::Error> {
    serde_json::from_str(include_str!("../data/accepted-static-source-manifest.json"))
}
pub fn checked_in_binding() -> Result<Binding, serde_json::Error> {
    serde_json::from_str(include_str!("../data/accepted-static-binding.json"))
}
fn same<T: Serialize>(a: &T, b: &T) -> Result<bool, String> {
    Ok(serde_json::to_value(a).map_err(|e| e.to_string())?
        == serde_json::to_value(b).map_err(|e| e.to_string())?)
}
fn require(ok: bool, code: &str) -> Result<(), String> {
    if ok { Ok(()) } else { Err(code.into()) }
}
/// Authenticate the structural input identities before any bulk source reads.
/// Actual byte authentication remains a required, separate consumer boundary.
pub fn validate_binding(
    binding: &Binding,
    manifest: &Manifest,
    history: &Registry,
    census: &PackageCensus,
) -> Result<(), String> {
    require(
        binding.source_sha == crate::contracts::ACCEPTED_SOURCE
            && manifest.source_sha == crate::contracts::ACCEPTED_SOURCE,
        "static_source_authority",
    )?;
    require(
        same(binding, &checked_in_binding().map_err(|e| e.to_string())?)?,
        "static_binding_identity",
    )?;
    require(
        same(manifest, &accepted_manifest().map_err(|e| e.to_string())?)?,
        "static_manifest_identity",
    )?;
    require(
        same(history, &crate::checked_in().map_err(|e| e.to_string())?.0)?,
        "static_history_identity",
    )?;
    require(
        same(
            census,
            &crate::package::checked_in_package()
                .map_err(|e| e.to_string())?
                .1,
        )?,
        "static_catalogue_identity",
    )?;
    validate_population(manifest, census)?;
    let pins = source_objects(manifest)?;
    let a6 = crate::a6::accepted_manifest().map_err(|e| e.to_string())?;
    let contracts = crate::contracts::accepted_manifest().map_err(|e| e.to_string())?;
    let contract_pins = crate::contracts::source_objects(&contracts)
        .map_err(|e| format!("static_contracts: {e:?}"))?;
    // Reuse accepted overlaps only by exact path/hash/size; unrelated populations
    // are not reread or requalified by this bounded successor.
    for pin in a6.files.iter().map(crate::a6::file_pin) {
        check_overlap(&pins, &pin?)?;
    }
    for pin in contract_pins.values().chain(census.source_objects.iter()) {
        check_overlap(&pins, pin)?;
    }
    let a6_binding = crate::a6::checked_in_binding().map_err(|e| e.to_string())?;
    let contract_binding = crate::contracts::checked_in_binding().map_err(|e| e.to_string())?;
    for pin in [&a6_binding.manifest, &contract_binding.manifest] {
        require(
            pins.contains_key(pin.path.as_str()),
            "static_manifest_missing",
        )?;
        check_overlap(&pins, pin)?;
    }
    let mapped: std::collections::BTreeSet<_> = manifest
        .packaged_source_paths
        .iter()
        .chain(&manifest.resource_paths)
        .chain(&manifest.profile_source_paths)
        .collect();
    require(
        binding.crosswalk.len() == mapped.len(),
        "static_crosswalk_population",
    )?;
    for path in mapped {
        let original = history
            .paths
            .iter()
            .find(|p| &p.path == path)
            .ok_or_else(|| format!("static_unmapped: {path}"))?;
        let obligation = history
            .obligations
            .iter()
            .find(|o| o.id == original.obligation_id)
            .ok_or("static_obligation_missing")?;
        let row = binding
            .crosswalk
            .iter()
            .find(|r| r["path"].as_str() == Some(path))
            .ok_or("static_crosswalk_missing")?;
        require(
            row["path_id"] == original.id
                && row["obligation_id"] == original.obligation_id
                && row["destination"] == obligation.destination
                && row["proof_state"] == "UNPROVEN"
                && row["disposition"]
                    == serde_json::to_value(&original.disposition).map_err(|e| e.to_string())?,
            "static_crosswalk_changed",
        )?;
    }
    require(
        binding.profiles.len() == history.supported_cells.len(),
        "static_profile_population",
    )?;
    for cell in &history.supported_cells {
        let row = binding
            .profiles
            .iter()
            .find(|r| r["id"] == cell.id)
            .ok_or("static_profile_missing")?;
        require(row["proof_state"] == "UNPROVEN", "static_profile_claim")?;
    }
    Ok(())
}
fn check_overlap(
    pins: &std::collections::BTreeMap<&str, &FilePin>,
    pin: &FilePin,
) -> Result<(), String> {
    if let Some(other) = pins.get(pin.path.as_str()) {
        require(
            same(*other, pin)?,
            &format!("static_pin_conflict: {}", pin.path),
        )?;
    }
    Ok(())
}
/// Unique canonical source objects, not arbitrary filesystem paths or claims.
pub fn source_objects(
    manifest: &Manifest,
) -> Result<std::collections::BTreeMap<&str, &FilePin>, String> {
    let mut pins = std::collections::BTreeMap::new();
    for pin in &manifest.files {
        require(
            crate::canonical_path(&pin.path)
                && crate::hex(&pin.sha256, 64)
                && !pin.path.split('/').any(|part| part == "post.js"),
            "static_pin_invalid",
        )?;
        require(
            pins.insert(pin.path.as_str(), pin).is_none(),
            "static_pin_duplicate",
        )?;
    }
    Ok(pins)
}
/// Independently check closure membership AND bytes against the released package
/// catalogue, without consulting the embedded static successor's membership.
pub fn validate_population(manifest: &Manifest, census: &PackageCensus) -> Result<(), String> {
    use std::collections::BTreeSet;
    require(
        manifest.source_sha == census.source_sha,
        "static_catalogue_source",
    )?;
    let pins = source_objects(manifest)?;
    let mut expected = BTreeSet::new();
    let mut resources = BTreeSet::new();
    for entry in &census.entries {
        if entry.kind != crate::package::EntryKind::File {
            continue;
        }
        let source = if let Some((_, path)) = entry
            .path
            .split_once("/Contents/Resources/a13-runtime-source/")
        {
            require(expected.insert(path), "static_package_duplicate")?;
            Some(path)
        } else if let Some((_, path)) = entry.path.split_once("/Contents/Resources/") {
            if path.starts_with("mycelium_a13_macos/web/") {
                require(resources.insert(path), "static_resource_duplicate")?;
                Some(path)
            } else {
                None
            }
        } else {
            None
        };
        if let Some(path) = source {
            let pin = pins
                .get(path)
                .ok_or_else(|| format!("static_package_member_missing: {path}"))?;
            require(
                pin.sha256 == entry.sha256 && pin.size_bytes == entry.size_bytes,
                &format!("static_package_bytes: {path}"),
            )?;
        }
    }
    let closure: BTreeSet<_> = manifest
        .packaged_source_paths
        .iter()
        .map(String::as_str)
        .collect();
    let actual_resources: BTreeSet<_> =
        manifest.resource_paths.iter().map(String::as_str).collect();
    require(
        !expected.is_empty()
            && closure == expected
            && closure.len() == manifest.packaged_source_paths.len(),
        "static_package_population",
    )?;
    require(
        actual_resources == resources && actual_resources.len() == manifest.resource_paths.len(),
        "static_resource_population",
    )?;
    let all: BTreeSet<_> = closure
        .into_iter()
        .chain(actual_resources)
        .chain(manifest.profile_source_paths.iter().map(String::as_str))
        .chain(manifest.derivation_inputs.iter().map(String::as_str))
        .collect();
    require(
        all == pins.keys().copied().collect(),
        "static_source_population",
    )
}
