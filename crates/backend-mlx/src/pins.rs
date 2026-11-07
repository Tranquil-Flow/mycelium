// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned digests of the installed native MLX libraries.
//!
//! This file is `#[path]`-included by `build.rs` (which hashes the real files at build time), so
//! it must stay std-only and warning-free in both places. The digests are the only copy in
//! source; `evidence/foundation-exit/native-inputs.json` and `crates/desktop/package-pins.json`
//! are cross-checked against them by tests in `crates/desktop`.

/// One complete, mutually consistent set of pinned native library digests.
pub struct PinSet {
    pub name: &'static str,
    /// `(file name, lower-case SHA-256 of the exact bytes)`.
    pub libs: &'static [(&'static str, &'static str)],
}

/// The only accepted native-library set: L8's offline rebuild of the vendored MLX 0.32.4 / mlx-c
/// 57731b00 sources with `MACOSX_DEPLOYMENT_TARGET=14.0` (every dylib `LC_BUILD_VERSION minos
/// 14.0`, `@rpath` install names, `LC_RPATH @loader_path`, metallib `air64_v26-apple-macosx14.0.0`).
/// Recipe, provenance and the independent round-5 re-hash are in `runs/v2/v02/` (L8-evidence and
/// FOUNDATION-EXIT-round5). The name is kept from L8's evidence for traceability. The original
/// `foundation-exit-minos-26.5.1` build (minos 26.5.1; cannot load on macOS 15) is removed: its
/// digests survive only as test inputs that must be refused.
pub const PIN_SET: PinSet = PinSet {
    name: "candidate-minos-14.0",
    libs: &[
        (
            "libmlxc.dylib",
            "8117d43fcd12f4af9a76b948e792803b151f8f46c28a2042a5bdff3fd0e4247d",
        ),
        (
            "libmlx.dylib",
            "e70c48ea5da63b86d4e11408f83cc5396d6228fd84ae4857d383d7480654dac3",
        ),
        (
            "libjaccl.dylib",
            "645a45cd177916b5c71890dd7ebb270ec1a5254eed72821ede1540dd36a4d2a4",
        ),
        (
            "mlx.metallib",
            "d70ab213d4cfa9f2673e4db2c2a4474c86ad9cc7674564bec4dd4ac9409da675",
        ),
    ],
};

/// Name of the accepted set when every installed file digest matches. `digest_of(file)` must
/// return the lower-case SHA-256 of the installed file, or an error that is passed through.
pub fn select(
    mut digest_of: impl FnMut(&str) -> Result<String, String>,
) -> Result<&'static str, String> {
    let mut bad = Vec::new();
    for (name, pin) in PIN_SET.libs {
        if digest_of(name)? != *pin {
            bad.push(*name);
        }
    }
    if bad.is_empty() {
        Ok(PIN_SET.name)
    } else {
        Err(format!(
            "BLOCKED: native pin mismatch ({}: {})",
            PIN_SET.name,
            bad.join(",")
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The superseded foundation-exit build (minos 26.5.1; cannot load on macOS 15).
    const OLD: [(&str, &str); 4] = [
        (
            "libmlxc.dylib",
            "524cfcb750590c20fb22add68f0ed5c6847874ea6c33a94776fdbdf983b404c8",
        ),
        (
            "libmlx.dylib",
            "b05f5026e2744341ecba4d1fcdf2814c74ac5b1d20229f5c5f525f6ab6c2a6b7",
        ),
        (
            "libjaccl.dylib",
            "cd74c2359f5e168b7a17b0446afa5b414f671f59a7c62ab2c759c7a3a7b36a4f",
        ),
        (
            "mlx.metallib",
            "ad01281395f80177e8ec51e66251ac52443f6c815a8aa7c26f1237bfa2106124",
        ),
    ];
    /// L8's offline rebuild with MACOSX_DEPLOYMENT_TARGET=14.0 (independently re-hashed).
    const MINOS_14: [(&str, &str); 4] = [
        (
            "libmlxc.dylib",
            "8117d43fcd12f4af9a76b948e792803b151f8f46c28a2042a5bdff3fd0e4247d",
        ),
        (
            "libmlx.dylib",
            "e70c48ea5da63b86d4e11408f83cc5396d6228fd84ae4857d383d7480654dac3",
        ),
        (
            "libjaccl.dylib",
            "645a45cd177916b5c71890dd7ebb270ec1a5254eed72821ede1540dd36a4d2a4",
        ),
        (
            "mlx.metallib",
            "d70ab213d4cfa9f2673e4db2c2a4474c86ad9cc7674564bec4dd4ac9409da675",
        ),
    ];

    fn lookup(set: &[(&'static str, &'static str)]) -> impl FnMut(&str) -> Result<String, String> {
        let table: Vec<(String, String)> = set
            .iter()
            .map(|(n, d)| ((*n).to_string(), (*d).to_string()))
            .collect();
        move |name| {
            table
                .iter()
                .find(|(n, _)| n == name)
                .map(|(_, d)| d.clone())
                .ok_or_else(|| format!("no such file {name}"))
        }
    }

    #[test]
    fn minos_14_libraries_are_accepted_as_the_default_set() {
        assert_eq!(select(lookup(&MINOS_14)), Ok("candidate-minos-14.0"));
    }

    #[test]
    fn superseded_minos_26_5_1_libraries_are_refused_naming_every_stale_file() {
        let error = select(lookup(&OLD)).unwrap_err();
        assert!(error.starts_with("BLOCKED: native pin mismatch"), "{error}");
        for (name, _) in OLD {
            assert!(error.contains(name), "{name} missing from {error}");
        }
        assert!(
            !error.contains("foundation-exit-minos-26.5.1"),
            "the superseded set must not be a named accepted set: {error}"
        );
    }

    #[test]
    fn a_single_stale_or_mixed_library_is_refused_naming_only_that_file() {
        for stale in 0..MINOS_14.len() {
            let mut mixed = MINOS_14;
            mixed[stale] = OLD[stale];
            let error = select(lookup(&mixed)).unwrap_err();
            assert!(error.contains(MINOS_14[stale].0), "{error}");
            for (index, (name, _)) in MINOS_14.iter().enumerate() {
                assert_eq!(error.contains(name), index == stale, "{name}: {error}");
            }
        }
    }

    /// `build.rs` hashed the real installed files with this same table before this test compiled.
    #[cfg(feature = "metal")]
    #[test]
    fn metal_build_selected_the_only_accepted_set() {
        assert_eq!(env!("MYCELIUM_MLX_NATIVE_PIN_SET"), PIN_SET.name);
    }

    #[test]
    fn a_tampered_digest_and_an_unreadable_file_are_refused() {
        let mut honest = lookup(&MINOS_14);
        let tampered = |name: &str| {
            if name == "mlx.metallib" {
                Ok("0".repeat(64))
            } else {
                honest(name)
            }
        };
        let error = select(tampered).unwrap_err();
        assert!(error.contains("mlx.metallib"), "{error}");
        assert_eq!(
            select(lookup(&MINOS_14[..2])),
            Err("no such file libjaccl.dylib".to_string())
        );
    }
}
