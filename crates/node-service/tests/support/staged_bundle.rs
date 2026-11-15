// SPDX-License-Identifier: AGPL-3.0-only
//! Lane BND: a bundle-shaped directory around a built packaged `mycelium` executable, holding
//! exactly the pinned payload (`desktop/package-pins.json` + `desktop::package::helper_pins()`),
//! so that the executable's worker roles pass the SAME admission they pass in the shipped app
//! (`desktop::package::admit_worker`: the executable is `<root>/Contents/MacOS/mycelium`, every
//! pinned file is present with its pinned bytes, nothing unpinned under `Contents/MacOS` or
//! `Contents/Frameworks`). The payload's bytes are copied from `MYCELIUM_PINNED_PAYLOAD_APP`, any
//! app bundle built from these pins (this Mac's installed one qualifies); admission, not this
//! file, checks them. Not an installed bundle: no `programs/<version>` location, no installer, no
//! code signature, no `Info.plist`, no window.
use std::path::{Path, PathBuf};

pub const PAYLOAD_ENV: &str = "MYCELIUM_PINNED_PAYLOAD_APP";
const NATIVE_PINS: &str = include_str!("../../../desktop/package-pins.json");

/// The pinned entries of the shipped manifest other than the executable itself.
pub fn payload_pins() -> Vec<desktop::package::Entry> {
    let mut pins: Vec<desktop::package::Entry> = serde_json::from_str(NATIVE_PINS).unwrap();
    pins.extend(desktop::package::helper_pins().unwrap());
    pins
}
/// The app whose pinned payload bytes are copied (`MYCELIUM_PINNED_PAYLOAD_APP`).
pub fn payload_app() -> PathBuf {
    PathBuf::from(std::env::var_os(PAYLOAD_ENV).unwrap_or_else(|| {
        panic!("{PAYLOAD_ENV} = an app bundle holding the pinned payload (package-pins.json, helper-pins.json)")
    }))
}
/// `<under>/Mycelium.app` with `executable` as `Contents/MacOS/mycelium` and the pinned payload
/// copied from `payload` (the Metal library as a regular file, which admission accepts). Answers
/// the staged executable's canonical path.
pub fn stage(executable: &Path, payload: &Path, under: &Path) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;
    let root = under.join("Mycelium.app");
    for entry in payload_pins() {
        let to = root.join(&entry.path);
        std::fs::create_dir_all(to.parent().unwrap()).unwrap();
        // `copy` follows the payload's own Metal link to its bytes; on APFS it is a clone.
        std::fs::copy(payload.join(&entry.path), &to)
            .unwrap_or_else(|e| panic!("{}: {e}", entry.path));
    }
    let exe = root.join("Contents/MacOS/mycelium");
    std::fs::copy(executable, &exe).unwrap();
    std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
    exe.canonicalize().unwrap()
}
/// The staged executable's identity through the shipped startup path (`bin/mycelium.rs::run`):
/// the manifest with the measured executable, OS + payload admission of the bundle, then
/// `packaged::admitted_native_program` over the admitted bundle (used by `packaged_ten_rows`).
#[allow(dead_code)]
pub fn admitted_program(exe: &Path) -> api::native_compiler::NativeProgram {
    let manifest = desktop::package::with_measured_executable(payload_pins(), exe).unwrap();
    let verified = desktop::package::admit_bundle(
        exe,
        &manifest,
        std::env::consts::OS,
        std::env::consts::ARCH,
        &desktop::platform::host_version().unwrap(),
    )
    .unwrap_or_else(|e| panic!("the staged bundle is not admitted: {e:?}"));
    let entry = manifest
        .entries
        .iter()
        .find(|e| e.kind == desktop::package::Kind::Executable)
        .unwrap();
    node_service::packaged::admitted_native_program(&verified, entry, exe).unwrap()
}
