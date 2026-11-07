// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticate final contract/owner bytes without executing the accepted application.
#[path = "support/source_bytes.rs"]
mod source_bytes;
use obligations::contracts::{self, Binding, Manifest};
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::process::ExitCode;

fn run() -> Result<(), String> {
    let mut args = std::env::args_os().skip(1);
    let repo = PathBuf::from(
        args.next()
            .ok_or("usage: contract_gate <oracle-git-repo> [binding.json]")?,
    );
    let binding: Binding = if let Some(path) = args.next() {
        serde_json::from_slice(&std::fs::read(path).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?
    } else {
        contracts::checked_in_binding().map_err(|e| e.to_string())?
    };
    if args.next().is_some() {
        return Err("unexpected arguments".into());
    }
    // Reject untrusted source/path pins before using them for a Git lookup.
    let (history, snapshot) = obligations::checked_in().map_err(|e| e.to_string())?;
    obligations::validate(&history, &snapshot)
        .map_err(|e| format!("historical_registry: {e:?}"))?;
    let embedded = contracts::accepted_manifest().map_err(|e| e.to_string())?;
    contracts::validate_binding(&binding, &embedded, &history)
        .map_err(|e| format!("contract_binding: {e:?}"))?;
    let bytes = source_bytes::read_object(&repo, &binding.source_sha, &binding.manifest.path)?;
    source_bytes::verify_bytes(&binding.manifest, &bytes)?;
    let manifest: Manifest = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    contracts::validate_binding(&binding, &manifest, &history)
        .map_err(|e| format!("actual_contract_binding: {e:?}"))?;
    let objects =
        contracts::source_objects(&manifest).map_err(|e| format!("object_union: {e:?}"))?;
    let (_, package) = obligations::package::checked_in_package().map_err(|e| e.to_string())?;
    let mut reused = 0;
    for pin in objects.values() {
        let bytes = source_bytes::read_object(&repo, &binding.source_sha, &pin.path)?;
        source_bytes::verify_bytes(pin, &bytes)?;
        if let Some(old) = package.source_objects.iter().find(|p| p.path == pin.path) {
            if old.sha256 != pin.sha256 || old.size_bytes != pin.size_bytes {
                return Err(format!("package_source_identity: {}", pin.path));
            }
            reused += 1;
        }
    }
    let owners: BTreeSet<_> = manifest
        .contracts
        .iter()
        .flat_map(|c| c.owner_sources.iter().map(|p| &p.path))
        .collect();
    let changed = binding
        .contracts
        .iter()
        .filter(|r| {
            history.contracts.iter().any(|h| {
                h.source == r.source && h.evidence["owner_sources"] != r.evidence["owner_sources"]
            })
        })
        .count();
    println!("{}", serde_json::to_string_pretty(&serde_json::json!({
        "scope": "accepted-contract-inventory-only",
        "source_sha": binding.source_sha,
        "manifest_sha256": binding.manifest.sha256,
        "protocols": manifest.contracts.len(),
        "distinct_owner_paths": owners.len(),
        "distinct_source_objects_rehashed": objects.len(),
        "reused_package_object_pins": reused,
        "changed_contract_rows": changed,
        "fixture_identities_preserved": manifest.contracts.len(),
        "unexplained_contract_paths": 0,
        "runtime_qualification": false,
        "remaining": ["static-closure rebinding", "A6/test-history exact delta", "endpoint/profile rebinding", "numerical/native foundation qualification"]
    })).map_err(|e| e.to_string())?);
    Ok(())
}
fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("contract-inventory: {error}");
            ExitCode::FAILURE
        }
    }
}
