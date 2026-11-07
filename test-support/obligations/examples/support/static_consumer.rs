// SPDX-License-Identifier: AGPL-3.0-only
//! Shared by the real CLI and its consumer-ordering tests.
use obligations::{FilePin, Registry, package::PackageCensus, static_source as s};
#[derive(Debug, serde::Serialize)]
pub struct Receipt {
    pub source_sha: String,
    pub source_objects_authenticated: usize,
    pub source_bytes_authenticated: u64,
    pub static_paths: usize,
    pub resource_paths: usize,
    pub profile_contexts: usize,
    pub history_files_authenticated: usize,
    pub retirements: u64,
    pub behavioral_qualification: bool,
    pub native_qualification: bool,
    pub unproven: Vec<String>,
}
/// Preflight all identity/crosswalk/catalogue inputs before calling either reader.
/// Each distinct source object is read/hashed once, overlaps are checked by the
/// structural gate, and unrelated accepted A6/contract objects are never reread.
pub fn authenticate(
    binding: &s::Binding,
    manifest: &s::Manifest,
    history: &Registry,
    census: &PackageCensus,
    mut history_read: impl FnMut(&FilePin) -> Result<Vec<u8>, String>,
    mut source_read: impl FnMut(&str, &str) -> Result<Vec<u8>, String>,
) -> Result<Receipt, String> {
    s::validate_binding(binding, manifest, history, census)?;
    for pin in &binding.history_files {
        super::source_bytes::verify_bytes(pin, &history_read(pin)?)?;
    }
    let objects = s::source_objects(manifest)?;
    let mut total = 0_u64;
    for pin in objects.values() {
        let bytes = source_read(&manifest.source_sha, &pin.path)?;
        super::source_bytes::verify_bytes(pin, &bytes)?;
        total = total
            .checked_add(pin.size_bytes)
            .ok_or("static_bytes_overflow")?;
    }
    Ok(Receipt {
        source_sha: manifest.source_sha.clone(),
        source_objects_authenticated: objects.len(),
        source_bytes_authenticated: total,
        static_paths: manifest.packaged_source_paths.len(),
        resource_paths: manifest.resource_paths.len(),
        profile_contexts: binding.profiles.len(),
        history_files_authenticated: binding.history_files.len(),
        retirements: 0,
        behavioral_qualification: false,
        native_qualification: false,
        unproven: binding.unproven.clone(),
    })
}
