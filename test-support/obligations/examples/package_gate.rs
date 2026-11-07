// SPDX-License-Identifier: AGPL-3.0-only
//! Offline inventory gate; no package/runtime or filesystem payload is executed.
use obligations::package::{checked_in_package, validate_package};
use std::process::ExitCode;

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let (registry, census) = checked_in_package()?;
    let (source, snapshot) = obligations::checked_in()?;
    obligations::validate(&source, &snapshot)
        .map_err(|errors| format!("source registry: {} violations", errors.len()))?;
    let binding = obligations::contracts::checked_in_binding()?;
    let manifest = obligations::contracts::accepted_manifest()?;
    obligations::contracts::validate_binding(&binding, &manifest, &source)
        .map_err(|errors| format!("accepted contract registry: {errors:?}"))?;
    // The accepted static successor is additive: historical registry/package
    // evidence stays immutable, and byte authentication belongs to static_gate.
    let static_binding = obligations::static_source::checked_in_binding()?;
    let static_manifest = obligations::static_source::accepted_manifest()?;
    obligations::static_source::validate_binding(
        &static_binding,
        &static_manifest,
        &source,
        &census,
    )
    .map_err(|error| format!("accepted static registry: {error}"))?;
    match validate_package(&registry, &census, &source) {
        Ok(coverage) => println!("{}", serde_json::to_string_pretty(&coverage)?),
        Err(errors) => {
            for error in &errors {
                eprintln!("{}: {}", error.code, error.subject);
            }
            return Err(format!("package registry: {} violations", errors.len()).into());
        }
    }
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("package-inventory: {error}");
            ExitCode::FAILURE
        }
    }
}
