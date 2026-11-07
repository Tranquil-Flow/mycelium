// SPDX-License-Identifier: AGPL-3.0-only
//! Contract inventory authority only; never runtime or replacement qualification.
use crate::{Crosswalk, FilePin, Registry, Violation, canonical_path, issue};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const HISTORICAL_SOURCE: &str = "f5bfb70981569c79cb873afe8e0c5be20342db7e";
pub const ACCEPTED_SOURCE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Contract {
    pub protocol: String,
    pub fixture: FilePin,
    pub owner_sources: Vec<FilePin>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Manifest {
    pub protocol: String,
    pub claim_boundary: String,
    pub contracts: Vec<Contract>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub source_sha: String,
    pub manifest: FilePin,
    pub contracts: Vec<Crosswalk>,
}

pub fn historical_manifest() -> Result<Manifest, serde_json::Error> {
    serde_json::from_str(include_str!("../data/contract-manifest.json"))
}
pub fn accepted_manifest() -> Result<Manifest, serde_json::Error> {
    serde_json::from_str(include_str!("../data/accepted-contract-manifest.json"))
}
pub fn checked_in_binding() -> Result<Binding, serde_json::Error> {
    serde_json::from_str(include_str!("../data/accepted-contract-binding.json"))
}

/// Compare all protocol/fixture/owner identities, including absent and extra members.
/// `manifest` must be authenticated separately before this structural check is credited.
pub fn validate_crosswalk(rows: &[Crosswalk], manifest: &Manifest) -> Vec<Violation> {
    let mut errors = Vec::new();
    let mut expected = BTreeMap::new();
    if manifest.protocol != "mycelium.contract_manifest.v1" || manifest.contracts.is_empty() {
        issue(&mut errors, "contract_manifest_schema", &manifest.protocol);
    }
    for contract in &manifest.contracts {
        if expected
            .insert(contract.protocol.as_str(), contract)
            .is_some()
        {
            issue(
                &mut errors,
                "duplicate_contract_authority",
                &contract.protocol,
            );
        }
        let mut owners = BTreeSet::new();
        if contract.owner_sources.is_empty() {
            issue(&mut errors, "contract_owner_missing", &contract.protocol);
        }
        for pin in std::iter::once(&contract.fixture).chain(&contract.owner_sources) {
            if !canonical_path(&pin.path) || !crate::hex(&pin.sha256, 64) {
                issue(&mut errors, "contract_pin_invalid", &pin.path);
            }
        }
        for pin in &contract.owner_sources {
            if !owners.insert(&pin.path) {
                issue(&mut errors, "duplicate_contract_owner", &pin.path);
            }
        }
    }
    let mut seen = BTreeSet::new();
    for row in rows {
        if !seen.insert(row.source.as_str()) {
            issue(&mut errors, "duplicate_contract_crosswalk", &row.source);
        }
        let Some(contract) = expected.get(row.source.as_str()) else {
            issue(&mut errors, "unlisted_contract_crosswalk", &row.source);
            continue;
        };
        // JSON equality preserves exact path, size, digest and complete membership.
        if row.evidence.get("fixture") != serde_json::to_value(&contract.fixture).ok().as_ref()
            || row.evidence.get("owner_sources")
                != serde_json::to_value(&contract.owner_sources).ok().as_ref()
        {
            issue(&mut errors, "contract_evidence_identity", &row.source);
        }
    }
    for protocol in expected.keys() {
        if !seen.contains(protocol) {
            issue(&mut errors, "unmapped_contract_authority", protocol);
        }
    }
    errors
}

/// Rebind only the contract subpopulation. All other historical populations stay historical.
/// The original destination/obligation and fixture identity cannot be dropped or relabelled.
pub fn validate_binding(
    binding: &Binding,
    manifest: &Manifest,
    history: &Registry,
) -> Result<(), Vec<Violation>> {
    let mut errors = validate_crosswalk(&binding.contracts, manifest);
    if binding.source_sha != ACCEPTED_SOURCE
        || binding.manifest.path != "contracts/contract-manifest.v1.json"
        || binding.manifest.sha256
            != "dde648d2af8cc77e0a697cac820a3ef7ea2a043537ca542106038467cc19aa4e"
        || binding.manifest.size_bytes != 71574
        || history.source_sha != HISTORICAL_SOURCE
    {
        issue(
            &mut errors,
            "contract_authority_identity",
            &binding.source_sha,
        );
    }
    let old: BTreeMap<_, _> = history
        .contracts
        .iter()
        .map(|r| (r.source.as_str(), r))
        .collect();
    let new: BTreeSet<_> = binding
        .contracts
        .iter()
        .map(|r| r.source.as_str())
        .collect();
    for row in &binding.contracts {
        match old.get(row.source.as_str()) {
            Some(prior)
                if row.id == prior.id
                    && row.destination == prior.destination
                    && row.obligation_id == prior.obligation_id
                    && row.evidence.get("fixture") == prior.evidence.get("fixture")
                    && row.citation.contains(ACCEPTED_SOURCE) => {}
            _ => issue(&mut errors, "contract_history_not_preserved", &row.source),
        }
    }
    for protocol in old.keys() {
        if !new.contains(protocol) {
            issue(&mut errors, "contract_history_missing", protocol);
        }
    }
    match source_objects(manifest) {
        Ok(objects) => {
            let mapped: BTreeSet<_> = history.paths.iter().map(|p| p.path.as_str()).collect();
            for path in objects.keys() {
                if !mapped.contains(path.as_str()) {
                    issue(&mut errors, "unmapped_contract_source_path", path);
                }
            }
        }
        Err(conflicts) => errors.extend(conflicts),
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

/// Exact union, rejecting conflicting pins shared by different contract owners.
pub fn source_objects(manifest: &Manifest) -> Result<BTreeMap<String, FilePin>, Vec<Violation>> {
    let mut objects: BTreeMap<String, FilePin> = BTreeMap::new();
    let mut errors = Vec::new();
    for pin in manifest
        .contracts
        .iter()
        .flat_map(|c| std::iter::once(&c.fixture).chain(&c.owner_sources))
    {
        if let Some(prior) = objects.insert(pin.path.clone(), pin.clone())
            && (prior.sha256 != pin.sha256 || prior.size_bytes != pin.size_bytes)
        {
            issue(&mut errors, "contract_conflicting_source_pin", &pin.path);
        }
    }
    if errors.is_empty() {
        Ok(objects)
    } else {
        Err(errors)
    }
}
