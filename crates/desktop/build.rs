// SPDX-License-Identifier: AGPL-3.0-only
fn main() {
    #[cfg(feature = "tauri-shell")]
    tauri_build::build();
    // The packaged executable finds bundled dylibs (`@rpath/libmlx.dylib` ...) next to itself in
    // `Contents/Frameworks`, wherever the bundle is installed. This is the only rpath it carries;
    // `bundle::build` refuses any other (an absolute build-tree rpath from a native backend's
    // build script) and `desktop-package` is not told where the build tree was.
    if std::env::var_os("CARGO_FEATURE_TAURI_SHELL").is_some()
        && std::env::var("CARGO_CFG_TARGET_OS").is_ok_and(|os| os == "macos")
    {
        println!("cargo:rustc-link-arg-bin=mycelium=-Wl,-rpath,@executable_path/../Frameworks");
    }
}
