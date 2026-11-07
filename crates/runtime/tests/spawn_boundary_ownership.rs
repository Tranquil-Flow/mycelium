// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-HYG-03 (descriptors do not leak into children: one canonical confinement boundary, runtime has no second one).
//!
//! The canonical child-spawn descriptor boundary is `node::process::confine_descriptors`
//! (L6 owns it; `NativeChild::spawn` applies it before any owned resource exists).
//! `runtime` has no production spawn site, so it must not carry a second
//! implementation: two audited `pre_exec` boundaries would drift (this one once failed
//! open where L6's refuses the spawn).
use std::{fs, path::Path};

fn sources(dir: &Path, found: &mut Vec<(String, String)>) {
    for entry in fs::read_dir(dir).expect("read runtime source dir") {
        let path = entry.expect("source entry").path();
        if path.is_dir() {
            sources(&path, found);
        } else if path.extension().is_some_and(|ext| ext == "rs") {
            found.push((
                path.display().to_string(),
                fs::read_to_string(&path).expect("read runtime source"),
            ));
        }
    }
}

#[test]
fn runtime_production_code_has_no_second_descriptor_confinement() {
    let mut files = Vec::new();
    sources(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
        &mut files,
    );
    assert!(
        files.iter().any(|(name, _)| name.ends_with("lib.rs")),
        "scan must see the crate root"
    );
    for (name, text) in &files {
        for forbidden in [
            "pre_exec",
            "confine_descriptors",
            "child_fds",
            "FD_CLOEXEC",
            "allow(unsafe_code)",
        ] {
            assert!(
                !text.contains(forbidden),
                "{name} carries `{forbidden}`: the canonical boundary is node::process"
            );
        }
    }
}

#[test]
fn the_canonical_boundary_is_the_one_runtime_callers_use() {
    // Compile-time pin: the symbol exists where the integrator ruled it lives.
    let _: fn(&mut std::process::Command) -> std::io::Result<()> =
        node::process::confine_descriptors;
}
