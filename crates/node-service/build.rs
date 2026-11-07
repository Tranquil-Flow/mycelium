// SPDX-License-Identifier: AGPL-3.0-only
fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-env-changed=MLXC_LIB_DIR");
    if std::env::var_os("CARGO_FEATURE_METAL").is_some() {
        // L8 verifies this directory's exact native hashes. An rlib's link-arg
        // does not propagate its rpath to downstream executables.
        let path = std::env::var("MLXC_LIB_DIR")?;
        if path.contains(['\n', '\r', ',']) {
            return Err("invalid native library path".into());
        }
        // The development install directory serves the qualification binary and test
        // targets only. The packaged `mycelium` executable must not carry it: it resolves
        // the dylibs through its bundle (`bundle::build` refuses any absolute rpath).
        let dev = format!("-Wl,-rpath,{path}");
        println!("cargo:rustc-link-arg-bin=mycelium-node={dev}");
        println!("cargo:rustc-link-arg-tests={dev}");
        if std::env::var_os("CARGO_FEATURE_PACKAGED").is_some() {
            println!("cargo:rustc-link-arg-bin=mycelium=-Wl,-rpath,@executable_path/../Frameworks");
        }
    }
    Ok(())
}
