// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XA (ruling 29 A, AB-PLAN A1): iroh's `portmapper` feature is compiled in, and features
//! unify across the workspace, so EVERY iroh endpoint in every crate now carries a portmapper
//! whose default (`PortmapperConfig::Enabled`) asks the local router for a mapping (UPnP SSDP
//! multicast, PCP, NAT-PMP: `portmapper-0.19.3` `get_mapping`) as soon as the endpoint binds an
//! IPv4 socket, relay or not. That multicast can raise a firewall prompt on macOS. So portmapping
//! is a user's choice, default OFF: every endpoint builder in the workspace sets it explicitly,
//! and only the cross-network builder turns it on, when the run's policy asks
//! (`LanPolicy::with_portmapping`). A source scan, so a new builder cannot silently start one.
use std::path::{Path, PathBuf};

fn sources(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if path.file_name().is_some_and(|n| n == "target") {
                continue;
            }
            sources(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

#[test]
fn every_endpoint_builder_in_the_workspace_sets_portmapping_explicitly() {
    let crates = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let mut files = Vec::new();
    sources(&crates, &mut files);
    assert!(
        files.len() > 100,
        "scanned the workspace crates: {}",
        files.len()
    );
    let mut builders = 0;
    let mut silent = Vec::new();
    for file in files {
        if file.ends_with("portmapping_default_off.rs") {
            continue;
        }
        let text = std::fs::read_to_string(&file).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        for (at, line) in lines.iter().enumerate() {
            let code = line.trim_start();
            if code.starts_with("//") || !code.contains("Endpoint::builder(") {
                continue;
            }
            builders += 1;
            // The builder chain: this statement up to and including its `;` (at most 24 lines).
            let mut chain = String::new();
            for line in lines[at..].iter().take(24) {
                chain.push_str(line);
                chain.push('\n');
                if line.trim_end().ends_with(';') {
                    break;
                }
            }
            if !chain.contains("portmapper_config(") {
                silent.push(format!("{}:{}", file.display(), at + 1));
            }
        }
    }
    assert!(builders >= 10, "found the endpoint builders: {builders}");
    assert!(
        silent.is_empty(),
        "endpoint builders that leave iroh's portmapper at its default (Enabled): {silent:#?}"
    );
}

/// Lane XB patch 2 (REVIEW XB-09): the guard is structural. Product code builds an iroh endpoint
/// ONLY through the runtime's builders (`iroh_host::mode_builder` and the builders it calls, in
/// `iroh_host.rs` / `iroh_cross.rs`), and the runtime's own unit test
/// (`relay_tests::every_product_endpoint_shape_is_built_with_portmapping_off_and_only_its_relay`)
/// builds every product shape through that real builder and reads its final configuration. So a
/// builder elsewhere, an alias (`use iroh::Endpoint as E`) or an `empty_builder` is refused here:
/// any preset or empty builder outside those two files, comments aside, test code aside.
#[test]
fn product_code_builds_endpoints_only_through_the_runtime_builders() {
    let crates = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let mut files = Vec::new();
    sources(&crates, &mut files);
    let central = ["runtime/src/iroh_host.rs", "runtime/src/iroh_cross.rs"];
    let mut outside = Vec::new();
    for file in files {
        let path = file.display().to_string();
        let product =
            path.contains("/src/") && !path.contains("/tests/") && !path.ends_with("tests.rs");
        if !product || central.iter().any(|c| path.ends_with(c)) {
            continue;
        }
        let text = std::fs::read_to_string(&file).unwrap();
        for (at, line) in text.lines().enumerate() {
            let code = line.trim_start();
            // An inline test module ends the product part of a file.
            if code.starts_with("#[cfg(test)]")
                && text[..]
                    .lines()
                    .nth(at + 1)
                    .is_some_and(|n| n.trim_start().starts_with("mod "))
            {
                break;
            }
            let code = code.split("//").next().unwrap_or_default();
            if code.contains("presets::")
                || code.contains("empty_builder")
                || code.contains("Endpoint::builder")
            {
                outside.push(format!("{path}:{}", at + 1));
            }
        }
    }
    assert!(
        outside.is_empty(),
        "product code that builds an iroh endpoint outside the runtime's builders: {outside:#?}"
    );
}
