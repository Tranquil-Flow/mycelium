// SPDX-License-Identifier: AGPL-3.0-only
//! Relocatable assembly hooks; no release operation implicit in library use.
use serde::{Deserialize, Serialize};
use std::path::Path;
#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
pub enum Kind {
    Executable,
    Dylib,
    Metal,
    Resource,
    /// A helper executable the packaged app runs beside itself (`native/resource_probe`).
    Helper,
}
/// Where `api::resources::ProbeObserver::beside_executable` looks for the resource probe.
pub const RESOURCE_PROBE_PATH: &str = "Contents/MacOS/native/resource_probe";
/// Where `api::relay::installed_relay_binary` looks for the bundled relay client
/// (`<exe dir>/native/cloudflared`; v0.1.4 `public_relay.py:250-255`, bundled by
/// `scripts/build_a13_macos.py:285-288,318` @ f4e366e9).
pub const RELAY_CLIENT_PATH: &str = "Contents/MacOS/native/cloudflared";
/// The native iroh transport a run spawns to reach other Macs
/// (`api::member_agent::installed_sidecar`: `<exe dir>/native/mycelium-iroh-sidecar`, owned by this
/// user, executable, not writable by others). Without it every packaged run stops at "joined Macs
/// report in" with `native_sidecar_installation_invalid`.
pub const IROH_SIDECAR_PATH: &str = "Contents/MacOS/native/mycelium-iroh-sidecar";
/// Where the relay client's licence ships (v0.1.4: `Resources/third_party/cloudflared-LICENSE`,
/// `build_a13_macos.py:325`; v0.2 keeps every notice under `Resources/licenses/`).
pub const RELAY_LICENSE_PATH: &str = "Contents/Resources/licenses/cloudflared-LICENSE";
/// The relay client's licence: Apache-2.0 text of cloudflared 2026.10.2, byte-identical to v0.1.4's
/// `third_party/cloudflared-LICENSE` (its release `APP-FILES.sha256`) and Homebrew's
/// `Cellar/cloudflared/2026.10.2/LICENSE`.
pub const RELAY_LICENSE_SHA256: &str =
    "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd";
pub const RELAY_LICENSE_BYTES: u64 = 11_357;
/// Provenance of the pinned relay client, recorded in `CONTENT-MANIFEST.json` (`helper-pins.json`
/// holds its bytes and sha256).
pub const RELAY_CLIENT_SOURCE: &str = "pinned third-party binary: cloudflared 2026.10.2 arm64, the exact bytes v0.1.4 bundles (its release APP-FILES.sha256: Contents/Frameworks/mycelium_a13_macos/native/cloudflared); Homebrew bottle 2026.10.2 arm64_tahoe re-signed ad-hoc (identical once both signatures are removed, except the __LINKEDIT vmsize field)";
/// `LC_BUILD_VERSION` of the pinned relay client (`otool -l`): above `LSMinimumSystemVersion`.
pub const RELAY_CLIENT_BUILD_VERSION: &str = "minos 26.0 sdk 26.5";
/// The licence pin, a `Kind::Resource` entry for assembly (not part of startup admission: it is
/// never executed; the bundle seal covers it).
pub fn relay_license_pin() -> Entry {
    Entry {
        path: RELAY_LICENSE_PATH.into(),
        kind: Kind::Resource,
        bytes: RELAY_LICENSE_BYTES,
        sha256: RELAY_LICENSE_SHA256.into(),
    }
}
/// Compile-time pins of the helper executables: the release `resource_probe` this source tree
/// builds and the pinned third-party relay client (`cloudflared`); the native payload pins stay in
/// `package-pins.json`, taken from the genuine native-input manifest.
pub const HELPER_PINS: &str = include_str!("../helper-pins.json");
/// The helper pins, parsed (a malformed table is a layout error, never an empty list).
pub fn helper_pins() -> Result<Vec<Entry>, PackageError> {
    let pins: Vec<Entry> = serde_json::from_str(HELPER_PINS).map_err(|_| PackageError::Layout)?;
    if pins.is_empty() || pins.iter().any(|p| p.kind != Kind::Helper) {
        return Err(PackageError::Layout);
    }
    Ok(pins)
}
/// Ruling 31 (RELF2): the only relay client the PACKAGED host may run, for
/// `api::relay::Hooks::pinned`: [`relay_client_beside`] this process's executable.
pub fn bundled_relay_client() -> Option<(std::path::PathBuf, String)> {
    relay_client_beside(&std::env::current_exe().ok()?)
}
/// The relay client of the bundle holding `executable` (`<bundle>/Contents/MacOS/mycelium`):
/// `<bundle>/`[`RELAY_CLIENT_PATH`] with the sha256 `helper-pins.json` pins for it, the same pin
/// startup admission checks (the one copy of that hash). `None` when there is no such pin.
pub fn relay_client_beside(executable: &Path) -> Option<(std::path::PathBuf, String)> {
    let root = executable.parent()?.parent()?.parent()?;
    let pin = helper_pins()
        .ok()?
        .into_iter()
        .find(|p| p.path == RELAY_CLIENT_PATH)?;
    Some((root.join(RELAY_CLIENT_PATH), pin.sha256))
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub path: String,
    pub kind: Kind,
    pub bytes: u64,
    pub sha256: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema: u32,
    pub entries: Vec<Entry>,
}
#[derive(Debug, Eq, PartialEq)]
pub enum PackageError {
    Layout,
    Missing,
    Identity,
    Symlink,
    DestinationExists,
    Io(std::io::ErrorKind),
    /// A file or link beside the executable or in `Frameworks` that the manifest does not pin
    /// (bundle-relative path): admission refuses code it has not checked.
    Unpinned(String),
    /// A pinned helper (bundle-relative path) that is not an executable regular file: the recipe
    /// ships helpers at 0o755, and a copy the relay could not execute must not be admitted.
    NotExecutable(String),
}
#[derive(Debug, Eq, PartialEq)]
pub enum AdmissionError {
    Platform(crate::platform::PlatformError),
    Package(PackageError),
    ExecutableLayout,
}
/// Constructible only after all native identities and OS are admitted.
#[derive(Debug)]
pub struct VerifiedBundle {
    root: std::path::PathBuf,
}
impl VerifiedBundle {
    /// The installed executable bound by this admission (identity, not a
    /// publisher-authentication claim). No new helper kind or layout is admitted.
    pub fn executable(&self) -> std::path::PathBuf {
        self.root.join("Contents/MacOS/mycelium")
    }
    pub fn metal(&self) -> std::path::PathBuf {
        self.root.join("Contents/Frameworks/mlx.metallib")
    }
    pub fn frameworks(&self) -> std::path::PathBuf {
        self.root.join("Contents/Frameworks")
    }
}
pub fn admit_bundle(
    executable: &Path,
    manifest: &Manifest,
    os: &str,
    arch: &str,
    version: &str,
) -> Result<VerifiedBundle, AdmissionError> {
    crate::platform::admit(os, arch, version).map_err(AdmissionError::Platform)?;
    admit_native_payload(executable, manifest)
}
/// A worker process the packaged executable re-executes (the node's member / owner-leaf) goes
/// through the same OS and payload admission as the window process before it loads anything.
pub fn admit_worker(manifest: &Manifest) -> Result<VerifiedBundle, String> {
    crate::platform::probe().map_err(|e| format!("platform:{e:?}"))?;
    let executable = std::env::current_exe().map_err(|e| format!("executable:{:?}", e.kind()))?;
    admit_native_payload(&executable, manifest).map_err(|e| format!("payload:{e:?}"))
}
pub(crate) fn admit_native_payload(
    executable: &Path,
    manifest: &Manifest,
) -> Result<VerifiedBundle, AdmissionError> {
    let root = executable
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .ok_or(AdmissionError::ExecutableLayout)?;
    if executable != root.join("Contents/MacOS/mycelium") {
        return Err(AdmissionError::ExecutableLayout);
    }
    verify_closed(root, manifest).map_err(AdmissionError::Package)?;
    Ok(VerifiedBundle { root: root.into() })
}
fn io(error: std::io::Error) -> PackageError {
    if error.kind() == std::io::ErrorKind::NotFound {
        PackageError::Missing
    } else {
        PackageError::Io(error.kind())
    }
}
fn layout(manifest: &Manifest) -> Result<(), PackageError> {
    use std::{collections::BTreeSet, path::Component};
    if manifest.schema != 1 {
        return Err(PackageError::Layout);
    }
    let mut paths = BTreeSet::new();
    for entry in &manifest.entries {
        if !entry.path.starts_with("Contents/")
            || entry.path.contains('\\')
            || !Path::new(&entry.path)
                .components()
                .all(|c| matches!(c, Component::Normal(_)))
            || entry
                .path
                .split('/')
                .any(|c| c.is_empty() || c == "." || c == "..")
            || !paths.insert(entry.path.as_str())
            || entry.bytes == 0
            || entry.sha256.len() != 64
            || !entry
                .sha256
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        {
            return Err(PackageError::Layout);
        }
        let kind_ok = match entry.kind {
            Kind::Executable => entry.path == "Contents/MacOS/mycelium",
            Kind::Dylib => {
                entry.path.starts_with("Contents/Frameworks/") && entry.path.ends_with(".dylib")
            }
            Kind::Metal => entry.path == "Contents/Frameworks/mlx.metallib",
            Kind::Resource => {
                entry.path == "Contents/Info.plist"
                    || entry.path == "Contents/Resources/icon-windowed.icns"
                    || entry.path.starts_with("Contents/Resources/licenses/")
                    || entry.path == CODE_RESOURCES_PATH
            }
            Kind::Helper => {
                entry.path == RESOURCE_PROBE_PATH
                    || entry.path == RELAY_CLIENT_PATH
                    || entry.path == IROH_SIDECAR_PATH
            }
        };
        if !kind_ok {
            return Err(PackageError::Layout);
        }
    }
    for (path, kind) in [
        ("Contents/MacOS/mycelium", Kind::Executable),
        ("Contents/Frameworks/libmlx.dylib", Kind::Dylib),
        ("Contents/Frameworks/libmlxc.dylib", Kind::Dylib),
        ("Contents/Frameworks/mlx.metallib", Kind::Metal),
    ] {
        if !manifest
            .entries
            .iter()
            .any(|e| e.path == path && e.kind == kind)
        {
            return Err(PackageError::Layout);
        }
    }
    Ok(())
}
fn regular_path(root: &Path, relative: &str) -> Result<std::path::PathBuf, PackageError> {
    let mut path = root.to_path_buf();
    if std::fs::symlink_metadata(&path)
        .map_err(io)?
        .file_type()
        .is_symlink()
    {
        return Err(PackageError::Symlink);
    }
    for component in Path::new(relative).components() {
        path.push(component);
        if std::fs::symlink_metadata(&path)
            .map_err(io)?
            .file_type()
            .is_symlink()
        {
            return Err(PackageError::Symlink);
        }
    }
    if !std::fs::metadata(&path).map_err(io)?.is_file() {
        return Err(PackageError::Layout);
    }
    Ok(path)
}
/// Where the Metal library's bytes are stored: `codesign` treats every file in
/// `Contents/Frameworks` as nested code, so a non-Mach-O file there makes the bundle unsignable.
/// As v0.1.4 does (`Frameworks/mlx/lib/mlx.metallib -> ../../../Resources/mlx/lib/mlx.metallib`),
/// the bytes live in `Resources` and the pinned `Frameworks/mlx.metallib` (where MLX looks, beside
/// `libmlx.dylib`) is a relative link to them.
pub const METAL_STORE_PATH: &str = "Contents/Resources/mlx.metallib";
/// The exact target of the `Contents/Frameworks/mlx.metallib` link.
pub const METAL_LINK_TARGET: &str = "../Resources/mlx.metallib";
/// The file holding a Metal entry's bytes: the entry itself when it is a regular file, or the
/// stored file when the entry is exactly the in-bundle link; any other link is refused.
fn metal_path(root: &Path, relative: &str) -> Result<std::path::PathBuf, PackageError> {
    let entry = root.join(relative);
    if std::fs::symlink_metadata(&entry)
        .map_err(io)?
        .file_type()
        .is_symlink()
    {
        if std::fs::read_link(&entry).map_err(io)? != Path::new(METAL_LINK_TARGET) {
            return Err(PackageError::Symlink);
        }
        return regular_path(root, METAL_STORE_PATH);
    }
    regular_path(root, relative)
}
/// Verify streamed bytes before native loading. L8/L12 provide trusted release
/// metadata, never unauthenticated metadata from the mutable bundle itself.
/// Do not embed a self-referential executable hash in that same executable.
/// Caller owns a non-concurrently-writable staging root; this is not a sandbox
/// against a hostile same-UID writer swapping files between verify and dlopen.
pub fn verify(root: &Path, manifest: &Manifest) -> Result<(), PackageError> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    layout(manifest)?;
    for entry in &manifest.entries {
        let path = if entry.kind == Kind::Metal {
            metal_path(root, &entry.path)?
        } else {
            regular_path(root, &entry.path)?
        };
        let mut file = std::fs::File::open(path).map_err(io)?;
        if file.metadata().map_err(io)?.len() != entry.bytes {
            return Err(PackageError::Identity);
        }
        let mut hash = Sha256::new();
        let mut buffer = [0u8; 65_536];
        loop {
            let n = file.read(&mut buffer).map_err(io)?;
            if n == 0 {
                break;
            }
            hash.update(&buffer[..n]);
        }
        if format!("{:x}", hash.finalize()) != entry.sha256 {
            return Err(PackageError::Identity);
        }
    }
    Ok(())
}
/// The code directories admission closes: everything in them must be pinned by the manifest.
pub const CODE_DIRECTORIES: [&str; 2] = ["Contents/MacOS", "Contents/Frameworks"];
/// [`verify`] plus closure: every file or link under [`CODE_DIRECTORIES`] is a manifest entry (the
/// Metal entry is the one link, already checked by `verify`). Startup admission and `admit` use
/// this, so a planted helper (`native/cloudflared`) or an extra dylib is refused before anything
/// can execute it. Directories are walked, never followed.
pub fn verify_closed(root: &Path, manifest: &Manifest) -> Result<(), PackageError> {
    verify(root, manifest)?;
    // Review RELF F12(i) / ruling 31: a pinned helper is admitted only as an executable regular
    // file (`verify` already refused links on its path and other bytes).
    for entry in manifest.entries.iter().filter(|e| e.kind == Kind::Helper) {
        use std::os::unix::fs::PermissionsExt;
        let meta = std::fs::symlink_metadata(root.join(&entry.path)).map_err(io)?;
        if !meta.is_file() || meta.permissions().mode() & 0o111 == 0 {
            return Err(PackageError::NotExecutable(entry.path.clone()));
        }
    }
    fn walk(
        root: &Path,
        dir: &Path,
        manifest: &Manifest,
        found: &mut Vec<String>,
    ) -> Result<(), PackageError> {
        let mut children: Vec<_> = std::fs::read_dir(dir)
            .map_err(io)?
            .collect::<Result<_, _>>()
            .map_err(io)?;
        children.sort_by_key(std::fs::DirEntry::file_name);
        for child in children {
            let path = child.path();
            let relative = path
                .strip_prefix(root)
                .map_err(|_| PackageError::Layout)?
                .to_str()
                .ok_or(PackageError::Layout)?
                .to_string();
            let kind = child.file_type().map_err(io)?;
            if kind.is_dir() {
                walk(root, &path, manifest, found)?;
                continue;
            }
            let pinned = manifest.entries.iter().find(|e| e.path == relative);
            match pinned {
                Some(entry) if !kind.is_symlink() || entry.kind == Kind::Metal => {}
                _ => found.push(relative),
            }
        }
        Ok(())
    }
    let mut found = Vec::new();
    for directory in CODE_DIRECTORIES {
        let path = root.join(directory);
        if std::fs::symlink_metadata(&path)
            .map_err(io)?
            .file_type()
            .is_symlink()
        {
            return Err(PackageError::Symlink);
        }
        walk(root, &path, manifest, &mut found)?;
    }
    match found.into_iter().next() {
        Some(first) => Err(PackageError::Unpinned(first)),
        None => Ok(()),
    }
}
/// Assembly into an exclusively created staging directory only. Never install,
/// upgrade, replace an existing target, mutate user state or sign implicitly.
/// Failed owned staging is removed; installation publication belongs to L12.
pub fn assemble(
    source: &Path,
    destination: &Path,
    manifest: &Manifest,
) -> Result<(), PackageError> {
    verify(source, manifest)?;
    match std::fs::create_dir(destination) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            return Err(PackageError::DestinationExists);
        }
        Err(e) => return Err(io(e)),
    }
    let result = (|| {
        for entry in &manifest.entries {
            let from = regular_path(source, &entry.path)?;
            let to = destination.join(&entry.path);
            std::fs::create_dir_all(to.parent().ok_or(PackageError::Layout)?).map_err(io)?;
            std::fs::copy(from, to).map_err(io)?;
        }
        verify(destination, manifest)
    })();
    if result.is_err() {
        std::fs::remove_dir_all(destination).map_err(io)?;
    }
    result
}
/// Compile-time pins (native payload only) plus the running executable's own
/// bytes. The executable entry satisfies the manifest layout; it does NOT
/// authenticate the executable (a self-hash cannot). That trust is the code
/// signature, which is v0.3 (Developer ID) and absent from this local build.
pub fn with_measured_executable(
    mut pins: Vec<Entry>,
    executable: &Path,
) -> Result<Manifest, PackageError> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let mut file = std::fs::File::open(executable).map_err(io)?;
    let mut hash = Sha256::new();
    let mut bytes = 0u64;
    let mut buffer = [0u8; 65_536];
    loop {
        let n = file.read(&mut buffer).map_err(io)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
        bytes += n as u64;
    }
    pins.push(Entry {
        path: "Contents/MacOS/mycelium".into(),
        kind: Kind::Executable,
        bytes,
        sha256: format!("{:x}", hash.finalize()),
    });
    Ok(Manifest {
        schema: 1,
        entries: pins,
    })
}
/// The resource seal `codesign` writes into the bundle when it signs it.
pub const CODE_RESOURCES_PATH: &str = "Contents/_CodeSignature/CodeResources";
pub fn signing_plan(manifest: &Manifest) -> Result<Vec<String>, PackageError> {
    layout(manifest)?;
    let mut code: Vec<_> = manifest
        .entries
        .iter()
        .filter(|e| matches!(e.kind, Kind::Dylib | Kind::Helper))
        .map(|e| e.path.clone())
        .collect();
    code.sort();
    code.push("Contents/MacOS/mycelium".into());
    code.push(".".into());
    Ok(code)
}
/// Preliminary dependency/rpath policy; does not substitute for Mach-O closure,
/// install-name resolution, LC_BUILD_VERSION and actual relocated GPU proof.
pub fn relocatable_link(value: &str) -> bool {
    [
        "@rpath/",
        "@loader_path/",
        "@executable_path/../Frameworks/",
        "/usr/lib/",
        "/System/Library/",
    ]
    .iter()
    .any(|prefix| {
        value.strip_prefix(prefix).is_some_and(|suffix| {
            !suffix.is_empty()
                && suffix
                    .split('/')
                    .all(|part| !matches!(part, ".." | "." | ""))
        })
    })
}
/// An `LC_RPATH` value that stays inside the bundle: the canonical Frameworks directory, a
/// sub-directory of it, or a `@loader_path` directory without parent hops. Never absolute.
pub fn relocatable_rpath(value: &str) -> bool {
    let normal = |suffix: &str| {
        suffix
            .split('/')
            .all(|part| !matches!(part, "" | ".." | "."))
    };
    value == crate::bundle::FRAMEWORKS_RPATH
        || value == "@loader_path"
        || value
            .strip_prefix("@executable_path/../Frameworks/")
            .is_some_and(normal)
        || value.strip_prefix("@loader_path/").is_some_and(normal)
}
