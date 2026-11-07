// SPDX-License-Identifier: AGPL-3.0-only
//! Additive final A6 source identities, never behavioral or retirement evidence.
use crate::{Crosswalk, FilePin, Obligation, PathDisposition, Registry, Violation};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub base_commit: String,
    pub files: Vec<FilePin>,
    pub protocol: String,
    pub source_digest: String,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub source_sha: String,
    pub manifest: FilePin,
    pub history_files: Vec<FilePin>,
    pub paths: Vec<PathDisposition>,
    pub obligations: Vec<Obligation>,
    pub test_groups: Vec<Crosswalk>,
    pub body_rows: Vec<serde_json::Value>,
}
pub fn checked_in_binding() -> Result<Binding, serde_json::Error> {
    serde_json::from_str(include_str!("../data/accepted-a6-binding.json"))
}
pub fn accepted_manifest() -> Result<Manifest, serde_json::Error> {
    serde_json::from_str(include_str!("../data/accepted-a6-source-manifest.json"))
}
/// A6 uses the literal `sha256:` namespace; refuse other encodings, never repair them.
pub fn file_pin(pin: &FilePin) -> Result<FilePin, String> {
    let digest = pin
        .sha256
        .strip_prefix("sha256:")
        .ok_or("a6_digest_prefix")?;
    if !crate::hex(digest, 64) || !crate::canonical_path(&pin.path) {
        return Err("a6_pin_invalid".into());
    }
    Ok(FilePin {
        path: pin.path.clone(),
        sha256: digest.into(),
        size_bytes: pin.size_bytes,
    })
}
/// Structural preflight; byte authentication is a separate required CLI boundary.
pub fn validate_binding(
    binding: &Binding,
    manifest: &Manifest,
    history: &Registry,
) -> Result<(), Vec<Violation>> {
    use std::collections::BTreeSet;
    let mut errors = Vec::new();
    let (original, mut snapshot) = crate::checked_in().expect("embedded history");
    let authority = accepted_manifest().expect("embedded final A6");
    let expected = checked_in_binding().expect("embedded crosswalk");
    if serde_json::to_value(history).ok() != serde_json::to_value(&original).ok() {
        crate::issue(&mut errors, "a6_history_not_preserved", "registry");
    }
    if binding.source_sha != crate::contracts::ACCEPTED_SOURCE
        || serde_json::to_value(&binding.manifest).ok()
            != serde_json::to_value(&expected.manifest).ok()
        || serde_json::to_value(&binding.history_files).ok()
            != serde_json::to_value(&expected.history_files).ok()
    {
        crate::issue(&mut errors, "a6_authority_identity", &binding.source_sha);
    }
    if serde_json::to_value(manifest).ok() != serde_json::to_value(&authority).ok() {
        crate::issue(&mut errors, "a6_manifest_identity", "final A6");
    }
    // The reviewed crosswalk is an immutable successor, not authority to rewrite history.
    // Exact membership binds AST-derived body spans and existing TB group identities.
    for (name, actual, wanted) in [
        (
            "paths",
            serde_json::to_value(&binding.paths),
            serde_json::to_value(&expected.paths),
        ),
        (
            "obligations",
            serde_json::to_value(&binding.obligations),
            serde_json::to_value(&expected.obligations),
        ),
        (
            "test_groups",
            serde_json::to_value(&binding.test_groups),
            serde_json::to_value(&expected.test_groups),
        ),
        (
            "body_rows",
            serde_json::to_value(&binding.body_rows),
            serde_json::to_value(&expected.body_rows),
        ),
    ] {
        if actual.ok() != wanted.ok() {
            crate::issue(&mut errors, "a6_crosswalk_identity", name);
        }
    }
    let mapped: BTreeSet<_> = original.paths.iter().map(|r| r.path.as_str()).collect();
    let final_paths: BTreeSet<_> = manifest.files.iter().map(|p| p.path.as_str()).collect();
    let added: BTreeSet<_> = binding.paths.iter().map(|p| p.path.as_str()).collect();
    let missing: BTreeSet<_> = final_paths.difference(&mapped).copied().collect();
    if added != missing
        || added.len() != binding.paths.len()
        || final_paths.len() != manifest.files.len()
    {
        crate::issue(&mut errors, "a6_population_union", "paths");
    }
    let old_a6: Manifest =
        serde_json::from_str(include_str!("../data/a6-source-manifest.json")).expect("old A6");
    if old_a6
        .files
        .iter()
        .any(|p| !final_paths.contains(p.path.as_str()))
    {
        crate::issue(&mut errors, "a6_unexplained_deletion", "old A6");
    }
    // A temporary structural union leaves the published historical snapshot untouched.
    let mut overlay = original.clone();
    overlay.paths.extend(binding.paths.clone());
    overlay.obligations.extend(binding.obligations.clone());
    overlay.test_groups.extend(binding.test_groups.clone());
    for pin in &manifest.files {
        match file_pin(pin) {
            Ok(pin) if added.contains(pin.path.as_str()) => {
                snapshot.test_paths.push(pin.path.clone());
                snapshot.a6_paths.push(pin.path.clone());
                snapshot.files.push(pin);
            }
            Ok(_) => {}
            Err(_) => crate::issue(&mut errors, "a6_pin_invalid", &pin.path),
        }
    }
    if let Err(e) = crate::validate(&overlay, &snapshot) {
        errors.extend(e);
    }
    // Bind the already qualified contract successor structurally. The CLI reads
    // the deduplicated source union once; negative controls never reread 204 objects.
    if let Err(e) = crate::contracts::validate_binding(
        &crate::contracts::checked_in_binding().expect("contract binding"),
        &crate::contracts::accepted_manifest().expect("contract manifest"),
        history,
    ) {
        errors.extend(e);
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}
