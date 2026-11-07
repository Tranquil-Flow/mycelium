// SPDX-License-Identifier: AGPL-3.0-only
#[cfg(feature = "metal")]
#[path = "src/pins.rs"]
mod pins;
#[cfg(feature = "metal")]
#[path = "src/rpath.rs"]
mod rpath;

#[cfg(feature = "metal")]
fn verify_native(headers: &str, libs: &str) -> Result<(), Box<dyn std::error::Error>> {
    use sha2::{Digest, Sha256};
    use std::{
        fs,
        io::Read,
        path::{Path, PathBuf},
    };
    fn walk(path: &Path, files: &mut Vec<PathBuf>) -> std::io::Result<()> {
        for entry in fs::read_dir(path)? {
            let p = entry?.path();
            if p.is_dir() {
                walk(&p, files)?;
            } else if p.extension().is_some_and(|e| e == "h") {
                files.push(p);
            }
        }
        Ok(())
    }
    let mut files = Vec::new();
    walk(&Path::new(headers).join("mlx/c"), &mut files)?;
    files.sort();
    let mut tree = Sha256::new();
    for file in &files {
        let relative = file.strip_prefix(headers)?.to_str().ok_or("header UTF8")?;
        tree.update(relative.as_bytes());
        tree.update([0]);
        tree.update(fs::read(file)?);
        println!("cargo:rerun-if-changed={}", file.display());
    }
    if files.len() != 44
        || format!("{:x}", tree.finalize())
            != "6a1ede0629032ef3ae66d71d28794b703ec6618470e81c1ea9747822b46b7a5f"
    {
        return Err("BLOCKED: mlx-c header pin mismatch".into());
    }
    let set = pins::select(|name| {
        let path = Path::new(libs).join(name);
        println!("cargo:rerun-if-changed={}", path.display());
        let mut file = fs::File::open(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        let mut hash = Sha256::new();
        let mut chunk = vec![0; 1024 * 1024];
        loop {
            let n = file
                .read(&mut chunk)
                .map_err(|e| format!("{}: {e}", path.display()))?;
            if n == 0 {
                break;
            }
            hash.update(&chunk[..n]);
        }
        Ok(format!("{:x}", hash.finalize()))
    })?;
    println!("cargo:rustc-env=MYCELIUM_MLX_NATIVE_PIN_SET={set}");
    Ok(())
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-env-changed=MLXC_INCLUDE_DIR");
    println!("cargo:rerun-if-env-changed=MLXC_LIB_DIR");
    #[cfg(feature = "metal")]
    {
        if std::env::var("CARGO_CFG_TARGET_OS")? != "macos" {
            return Err("MLX Metal requires macOS".into());
        }
        let headers = std::env::var("MLXC_INCLUDE_DIR")
            .map_err(|_| "BLOCKED: MLXC_INCLUDE_DIR must name pinned mlx-c 57731b00 headers")?;
        let libs = std::env::var("MLXC_LIB_DIR")
            .map_err(|_| "BLOCKED: MLXC_LIB_DIR must name pinned MLX installed libraries")?;
        verify_native(&headers, &libs)?;
        println!("cargo:rerun-if-changed={headers}/mlx/c");
        let bindings = bindgen::Builder::default()
            .header(format!("{headers}/mlx/c/mlx.h"))
            .clang_arg(format!("-I{headers}"))
            .allowlist_function("mlx_.*")
            .allowlist_type("mlx_.*")
            .allowlist_var("MLX_.*")
            .layout_tests(false)
            .generate()?;
        bindings
            .write_to_file(std::path::PathBuf::from(std::env::var("OUT_DIR")?).join("ffi.rs"))?;
        println!("cargo:rustc-link-search=native={libs}");
        println!("cargo:rustc-link-lib=dylib=mlxc");
        // Bundle path first (release layout), then the development install for tests/examples.
        // `rustc-link-arg` reaches only this package's own test/example binaries; a crate that
        // links backend-mlx into a shipped executable must emit BUNDLE_RPATH from its own build.rs.
        for arg in rpath::link_args(&libs)? {
            println!("cargo:rustc-link-arg={arg}");
        }
        println!("cargo:rustc-env=MYCELIUM_MLX_DEV_LIB_DIR={libs}");
    }
    Ok(())
}
