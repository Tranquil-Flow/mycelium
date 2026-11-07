// SPDX-License-Identifier: AGPL-3.0-only
//! Local, unsigned `.app` assembly and the no-Python audit.
//!
//! Identity policy (S14, v0.2 local bundle): the user-visible name, bundle
//! identifier, icon, minimum OS and Info.plist key set are the released v0.1.4
//! values (`Contents/Info.plist` sha256 `e1220422…` in the certified package),
//! so a later installer can upgrade in place. Only the executable name and the
//! version string differ, and a test names that difference. Nothing here signs,
//! notarizes, installs, upgrades or touches an installed application.
use crate::package::{Entry, Kind, Manifest, PackageError};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    io::Read,
    path::{Path, PathBuf},
};

pub const DISPLAY_NAME: &str = "Mycelium A13 Demo";
pub const BUNDLE_ID: &str = "org.mycelium.a13-macos-demo";
pub const VERSION: &str = "0.2.0";
pub const MINIMUM_OS: &str = "15.6.1";
pub const EXECUTABLE: &str = "mycelium";
pub const ICON_FILE: &str = "icon-windowed.icns";
/// Certified v0.1.4 `Contents/Resources/icon-windowed.icns` (APP-FILES.sha256).
pub const ICON_SHA256: &str = "b90a3b56e59169be0f86fe0410699f42ccaa16a0c85d980ef0eb6068032f0b36";
pub const NATIVE_INPUT_SCHEMA: &str = "mycelium.foundation.native-input-evidence.v1";
const MAX_MANIFEST_BYTES: u64 = 4 * 1024 * 1024;

#[derive(Debug, Eq, PartialEq)]
pub enum BundleError {
    NativeInputs(&'static str),
    /// An executable rpath that is not bundle-relative, or `missing_frameworks_rpath`.
    Rpath(String),
    /// An executable dependency outside the system and the bundle.
    Link(String),
    SourceIdentity(String),
    DestinationExists,
    Package(PackageError),
    Io(std::io::ErrorKind),
}
impl From<PackageError> for BundleError {
    fn from(error: PackageError) -> Self {
        Self::Package(error)
    }
}
fn io(error: std::io::Error) -> BundleError {
    BundleError::Io(error.kind())
}

#[derive(Debug, Deserialize)]
pub struct SourceFile {
    pub path: String,
    pub path_base: String,
    pub size_bytes: u64,
    pub sha256: String,
    #[serde(default)]
    pub role: String,
    #[serde(default)]
    pub macho_build_version: Option<String>,
}
#[derive(Debug, Deserialize)]
pub struct NativeInputs {
    pub schema: String,
    pub observed_at: String,
    pub native_inputs: Vec<SourceFile>,
    pub license_notices: Vec<SourceFile>,
}
pub const CANDIDATE_SCHEMA: &str = "mycelium.l8.native-inputs-candidate.v1";
#[derive(Debug, Deserialize)]
struct CandidateFile {
    path: String,
    size_bytes: u64,
    sha256: String,
    #[serde(default)]
    role: String,
    #[serde(default)]
    minos: Option<String>,
}
#[derive(Debug, Deserialize)]
struct Candidate {
    observed_at: String,
    native_inputs: Vec<CandidateFile>,
    license_notices: Vec<CandidateFile>,
}
/// L8's proposed manifest, normalised to the genuine shape. Only `build` calls this, and only with
/// a verified sha256 pin; every source file is still size+sha checked before use.
fn parse_candidate(bytes: &[u8]) -> Result<NativeInputs, BundleError> {
    let candidate: Candidate = serde_json::from_slice(bytes)
        .map_err(|_| BundleError::NativeInputs("native_inputs_unparseable"))?;
    let convert = |file: CandidateFile, notice: bool| SourceFile {
        path: file.path,
        path_base: "absolute".into(),
        size_bytes: file.size_bytes,
        sha256: file.sha256,
        role: match file.role.as_str() {
            "linked_dylib" | "transitive_dylib" => "linked_or_transitive_dylib".into(),
            _ if notice => String::new(),
            other => other.into(),
        },
        macho_build_version: file.minos.map(|m| format!("minos {m}")),
    };
    let inputs = NativeInputs {
        schema: NATIVE_INPUT_SCHEMA.into(),
        observed_at: candidate.observed_at,
        native_inputs: candidate
            .native_inputs
            .into_iter()
            .map(|f| convert(f, false))
            .collect(),
        license_notices: candidate
            .license_notices
            .into_iter()
            .map(|f| convert(f, true))
            .collect(),
    };
    validate(inputs)
}
/// Parse the genuine foundation-exit manifest (richer than we consume).
pub fn parse_native_inputs(bytes: &[u8]) -> Result<NativeInputs, BundleError> {
    let inputs: NativeInputs = serde_json::from_slice(bytes)
        .map_err(|_| BundleError::NativeInputs("native_inputs_unparseable"))?;
    if inputs.schema != NATIVE_INPUT_SCHEMA {
        return Err(BundleError::NativeInputs("native_inputs_schema"));
    }
    validate(inputs)
}
fn validate(inputs: NativeInputs) -> Result<NativeInputs, BundleError> {
    for file in inputs.native_inputs.iter().chain(&inputs.license_notices) {
        if file.path_base != "absolute"
            || !Path::new(&file.path).is_absolute()
            || file.size_bytes == 0
            || file.sha256.len() != 64
        {
            return Err(BundleError::NativeInputs("native_inputs_entry"));
        }
    }
    for file in &inputs.native_inputs {
        destination(file)?;
    }
    for file in &inputs.license_notices {
        file_name(file)?;
    }
    Ok(inputs)
}
fn file_name(file: &SourceFile) -> Result<&str, BundleError> {
    Path::new(&file.path)
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|n| !n.contains('/') && !n.is_empty())
        .ok_or(BundleError::NativeInputs("native_inputs_name"))
}
/// Streamed `(bytes, sha256)` of a regular, non-symlink file.
pub fn measure(path: &Path) -> Result<(u64, String), BundleError> {
    let meta = std::fs::symlink_metadata(path).map_err(io)?;
    if !meta.file_type().is_file() {
        return Err(BundleError::Package(PackageError::Symlink));
    }
    let mut file = std::fs::File::open(path).map_err(io)?;
    let mut hash = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    let mut total = 0u64;
    loop {
        let n = file.read(&mut buffer).map_err(io)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
        total += n as u64;
    }
    Ok((total, format!("{:x}", hash.finalize())))
}
fn verify_source(file: &SourceFile) -> Result<(), BundleError> {
    let (bytes, sha) = measure(Path::new(&file.path))?;
    if bytes != file.size_bytes || sha != file.sha256 {
        return Err(BundleError::SourceIdentity(file.path.clone()));
    }
    Ok(())
}
/// Deterministic, v0.1.4-key-set Info.plist (tab indented, sorted keys).
pub fn info_plist() -> String {
    let string =
        |key: &str, value: &str| format!("\t<key>{key}</key>\n\t<string>{value}</string>\n");
    let mut out = String::from(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\">\n<dict>\n",
    );
    out += &string("CFBundleDisplayName", DISPLAY_NAME);
    out += &string("CFBundleExecutable", EXECUTABLE);
    out += &string("CFBundleIconFile", ICON_FILE);
    out += &string("CFBundleIdentifier", BUNDLE_ID);
    out += &string("CFBundleInfoDictionaryVersion", "6.0");
    out += &string("CFBundleName", DISPLAY_NAME);
    out += &string("CFBundlePackageType", "APPL");
    out += &string("CFBundleShortVersionString", VERSION);
    out += &string("LSMinimumSystemVersion", MINIMUM_OS);
    out += "\t<key>NSHighResolutionCapable</key>\n\t<true/>\n</dict>\n</plist>\n";
    out
}

fn destination(file: &SourceFile) -> Result<(String, Kind, u32), BundleError> {
    match file.role.as_str() {
        "linked_or_transitive_dylib" if file_name(file)?.ends_with(".dylib") => Ok((
            format!("Contents/Frameworks/{}", file_name(file)?),
            Kind::Dylib,
            0o755,
        )),
        // MLX resolves its default library beside `libmlx.dylib` (`Frameworks/mlx.metallib`) and
        // nowhere under `Resources/`; a bundle laid out per the older plan made every worker trap in
        // `mlx::core::metal::Device::Device()` (L11 round 7, `06-member-crash-*.ips.txt`).
        "runtime_metal_library" if file_name(file)? == "mlx.metallib" => Ok((
            "Contents/Frameworks/mlx.metallib".to_string(),
            Kind::Metal,
            0o644,
        )),
        _ => Err(BundleError::NativeInputs("native_inputs_role")),
    }
}
/// Trusted-at-compile-time identities of the native payload, taken from the
/// genuine manifest (never from a mutable bundle). No executable entry.
pub fn native_pins(inputs: &NativeInputs) -> Result<Vec<Entry>, BundleError> {
    let mut entries = Vec::new();
    for file in &inputs.native_inputs {
        let (path, kind, _) = destination(file)?;
        entries.push(Entry {
            path,
            kind,
            bytes: file.size_bytes,
            sha256: file.sha256.clone(),
        });
    }
    entries.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(entries)
}

pub struct Spec<'a> {
    pub executable: &'a Path,
    pub native_inputs: &'a Path,
    pub icon: &'a Path,
    pub out_dir: &'a Path,
    /// Lowercase sha256 the manifest bytes must have (a candidate manifest is consumed only when
    /// its digest was pinned by the integrator); `None` keeps the committed genuine manifest.
    pub expected_native_inputs_sha256: Option<&'a str>,
}
#[derive(Debug)]
pub struct Built {
    pub app: PathBuf,
    pub manifest: Manifest,
    pub native_inputs_sha256: String,
    pub observed_at: String,
    /// `(bundle path, source path, source role, recorded Mach-O build version)`.
    pub sources: Vec<(String, String, String, Option<String>)>,
}
fn copy_file(from: &Path, root: &Path, relative: &str, mode: u32) -> Result<(), BundleError> {
    use std::os::unix::fs::PermissionsExt;
    let to = root.join(relative);
    std::fs::create_dir_all(to.parent().ok_or(PackageError::Layout)?).map_err(io)?;
    std::fs::copy(from, &to).map_err(io)?;
    std::fs::set_permissions(&to, std::fs::Permissions::from_mode(mode)).map_err(io)
}
/// Build `<out_dir>/<DISPLAY_NAME>.app` exclusively; remove it again on failure.
pub fn build(spec: &Spec) -> Result<Built, BundleError> {
    let mut limited = Vec::new();
    std::fs::File::open(spec.native_inputs)
        .map_err(io)?
        .take(MAX_MANIFEST_BYTES + 1)
        .read_to_end(&mut limited)
        .map_err(io)?;
    if limited.len() as u64 > MAX_MANIFEST_BYTES {
        return Err(BundleError::NativeInputs("native_inputs_too_large"));
    }
    let native_inputs_sha256 = format!("{:x}", Sha256::digest(&limited));
    if spec
        .expected_native_inputs_sha256
        .is_some_and(|pin| pin != native_inputs_sha256)
    {
        return Err(BundleError::NativeInputs("native_inputs_sha256_mismatch"));
    }
    let schema = serde_json::from_slice::<serde_json::Value>(&limited)
        .ok()
        .and_then(|v| v["schema"].as_str().map(String::from));
    let inputs = if schema.as_deref() == Some(CANDIDATE_SCHEMA) {
        if spec.expected_native_inputs_sha256.is_none() {
            return Err(BundleError::NativeInputs(
                "native_inputs_candidate_requires_pin",
            ));
        }
        parse_candidate(&limited)?
    } else {
        parse_native_inputs(&limited)?
    };
    check_executable_links(spec.executable)?;
    for file in inputs.native_inputs.iter().chain(&inputs.license_notices) {
        verify_source(file)?;
    }
    let (icon_bytes, icon_sha) = measure(spec.icon)?;
    if icon_sha != ICON_SHA256 {
        return Err(BundleError::SourceIdentity(spec.icon.display().to_string()));
    }
    let app = spec.out_dir.join(format!("{DISPLAY_NAME}.app"));
    match std::fs::create_dir(&app) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            return Err(BundleError::DestinationExists);
        }
        Err(e) => return Err(io(e)),
    }
    let result = (|| {
        let mut sources = Vec::new();
        let mut plan: Vec<(PathBuf, String, Kind, u32)> = vec![(
            spec.executable.to_path_buf(),
            format!("Contents/MacOS/{EXECUTABLE}"),
            Kind::Executable,
            0o755,
        )];
        for file in &inputs.native_inputs {
            let (relative, kind, mode) = destination(file)?;
            sources.push((
                relative.clone(),
                file.path.clone(),
                file.role.clone(),
                file.macho_build_version.clone(),
            ));
            plan.push((PathBuf::from(&file.path), relative, kind, mode));
        }
        for (index, file) in inputs.license_notices.iter().enumerate() {
            let relative = format!(
                "Contents/Resources/licenses/{:02}-{}",
                index + 1,
                file_name(file)?
            );
            sources.push((
                relative.clone(),
                file.path.clone(),
                "license_notice".into(),
                None,
            ));
            plan.push((PathBuf::from(&file.path), relative, Kind::Resource, 0o644));
        }
        plan.push((
            spec.icon.to_path_buf(),
            format!("Contents/Resources/{ICON_FILE}"),
            Kind::Resource,
            0o644,
        ));
        let mut entries = Vec::new();
        for (from, relative, kind, mode) in &plan {
            copy_file(from, &app, relative, *mode)?;
            let (bytes, sha256) = measure(&app.join(relative))?;
            entries.push(Entry {
                path: relative.clone(),
                kind: kind.clone(),
                bytes,
                sha256,
            });
        }
        let _ = icon_bytes;
        let plist = info_plist();
        std::fs::write(app.join("Contents/Info.plist"), plist.as_bytes()).map_err(io)?;
        let (bytes, sha256) = measure(&app.join("Contents/Info.plist"))?;
        entries.push(Entry {
            path: "Contents/Info.plist".into(),
            kind: Kind::Resource,
            bytes,
            sha256,
        });
        entries.sort_by(|a, b| a.path.cmp(&b.path));
        let manifest = Manifest { schema: 1, entries };
        crate::package::verify(&app, &manifest)?;
        // The source copies were verified before use; the bundle bytes now match.
        for ((relative, _, _, _), file) in sources
            .iter()
            .zip(inputs.native_inputs.iter().chain(&inputs.license_notices))
        {
            let entry = manifest
                .entries
                .iter()
                .find(|e| &e.path == relative)
                .ok_or(PackageError::Missing)?;
            if entry.sha256 != file.sha256 || entry.bytes != file.size_bytes {
                return Err(BundleError::SourceIdentity(relative.clone()));
            }
        }
        Ok(Built {
            app: app.clone(),
            manifest,
            native_inputs_sha256: native_inputs_sha256.clone(),
            observed_at: inputs.observed_at.clone(),
            sources,
        })
    })();
    if result.is_err() {
        std::fs::remove_dir_all(&app).map_err(io)?;
    }
    result
}

/// What a bundle walk found. Symlinks are counted, never followed.
#[derive(Debug, Default)]
pub struct Census {
    pub files: Vec<(String, u64)>,
    pub directories: usize,
    pub symlinks: Vec<String>,
}
pub fn census(root: &Path) -> Result<Census, BundleError> {
    fn walk(root: &Path, dir: &Path, out: &mut Census) -> Result<(), BundleError> {
        let mut children: Vec<_> = std::fs::read_dir(dir)
            .map_err(io)?
            .collect::<Result<_, _>>()
            .map_err(io)?;
        children.sort_by_key(std::fs::DirEntry::file_name);
        for child in children {
            let path = child.path();
            let relative = path
                .strip_prefix(root)
                .map_err(|_| BundleError::Package(PackageError::Layout))?
                .to_string_lossy()
                .into_owned();
            let kind = child.file_type().map_err(io)?;
            if kind.is_symlink() {
                out.symlinks.push(relative);
            } else if kind.is_dir() {
                out.directories += 1;
                walk(root, &path, out)?;
            } else {
                out.files
                    .push((relative, child.metadata().map_err(io)?.len()));
            }
        }
        Ok(())
    }
    let mut out = Census::default();
    walk(root, root, &mut out)?;
    Ok(out)
}
/// `shasum -a 256 -c` compatible listing of every regular file in the bundle.
pub fn app_files_listing(root: &Path) -> Result<String, BundleError> {
    let mut out = String::new();
    for (relative, _) in census(root)?.files {
        let (_, sha) = measure(&root.join(&relative))?;
        out += &format!("{sha}  {relative}\n");
    }
    Ok(out)
}
fn version_tuple(text: &str) -> Vec<u32> {
    text.split('.').map(|p| p.parse().unwrap_or(0)).collect()
}
/// `minos` word of a recorded `LC_BUILD_VERSION` dump.
pub fn recorded_minos(text: &str) -> Option<String> {
    let mut words = text.split_whitespace();
    words.find(|w| *w == "minos")?;
    words.next().map(String::from)
}
pub fn content_manifest(built: &Built) -> Result<serde_json::Value, BundleError> {
    let census = census(&built.app)?;
    let total: u64 = census.files.iter().map(|(_, n)| n).sum();
    let entries: Vec<_> = built
        .manifest
        .entries
        .iter()
        .map(|e| {
            let source = built.sources.iter().find(|s| s.0 == e.path);
            serde_json::json!({
                "path": e.path, "kind": format!("{:?}", e.kind), "bytes": e.bytes, "sha256": e.sha256,
                "source": source.map(|s| s.1.clone()), "source_role": source.map(|s| s.2.clone()),
                "recorded_macho_build_version": source.and_then(|s| s.3.as_ref())
                    .map(|v| v.split_whitespace().collect::<Vec<_>>().join(" ")),
            })
        })
        .collect();
    let minimum_os_findings: Vec<_> = built
        .sources
        .iter()
        .filter_map(|(path, _, _, build)| {
            let minos = recorded_minos(build.as_deref()?)?;
            (version_tuple(&minos) > version_tuple(MINIMUM_OS)).then(|| format!("{path}: LC_BUILD_VERSION minos {minos} exceeds LSMinimumSystemVersion {MINIMUM_OS}; dyld would refuse to load it there"))
        })
        .collect();
    Ok(serde_json::json!({
        "schema": "mycelium.l11.local-bundle-content.v1",
        "spdx_license_identifier": "AGPL-3.0-only",
        "identity": {
            "display_name": DISPLAY_NAME, "bundle_identifier": BUNDLE_ID, "version": VERSION,
            "minimum_system_version": MINIMUM_OS, "executable": EXECUTABLE, "icon": ICON_FILE,
            "policy": "v0.1.4 name, identifier, icon and Info.plist key set retained for in-place upgrade; executable name and version differ",
        },
        "trust_level": {
            "signed": false, "notarized": false, "developer_id": "v0.3",
            "note": "unsigned local build; no codesign, no quarantine handling, not installed",
        },
        "native_inputs": {
            "manifest_sha256": built.native_inputs_sha256, "observed_at": built.observed_at,
            "minimum_os_qualified": false,
            "minimum_os_findings": minimum_os_findings,
            "note": if minimum_os_findings.is_empty() {
                "recorded dylib minos values do not exceed the claimed minimum; L8's MINOS-LOAD-PROOF covers the libraries, not this packaged executable: its own load on the minimum OS is NOT proven here"
            } else {
                "at least one dylib records a minos above the claimed minimum (see findings); the packaged executable's own load on the minimum OS is NOT proven here"
            },
        },
        "totals": {
            "regular_files": census.files.len(), "directories": census.directories,
            "symlinks": census.symlinks.len(), "content_bytes": total,
        },
        "entries": entries,
        "audited_files": census.files.iter().map(|(p, n)| serde_json::json!({"path": p, "bytes": n})).collect::<Vec<_>>(),
    }))
}

/// Names that exist only in a Python distribution or Python-frozen app.
fn python_name(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    const SUFFIXES: [&str; 8] = [
        ".py",
        ".pyc",
        ".pyo",
        ".pyd",
        ".pth",
        ".dist-info",
        ".egg-info",
        ".pyi",
    ];
    const EXACT: [&str; 7] = [
        "__pycache__",
        "site-packages",
        "base_library.zip",
        "pyvenv.cfg",
        "python.framework",
        "python",
        "python3",
    ];
    SUFFIXES.iter().any(|s| lower.ends_with(s))
        || EXACT.contains(&lower.as_str())
        || lower.starts_with("python3.")
        || lower.starts_with("libpython")
        || lower.contains(".cpython-")
        || lower.contains(".abi3.")
}
const NEEDLES: [&[u8]; 5] = [
    b"libpython",
    b"Python.framework",
    b"PYTHONHOME",
    b"Py_Initialize",
    b"PyConfig_",
];
fn scan_file(path: &Path) -> Result<Vec<String>, BundleError> {
    let mut file = std::fs::File::open(path).map_err(io)?;
    let mut findings = Vec::new();
    let mut head = [0u8; 256];
    let n = file.read(&mut head).map_err(io)?;
    if head[..n].starts_with(b"#!") {
        let line = String::from_utf8_lossy(&head[..n]);
        let line = line.lines().next().unwrap_or_default().to_ascii_lowercase();
        if line.contains("python") {
            findings.push(format!("python shebang: {line}"));
        }
    }
    let overlap = NEEDLES.iter().map(|n| n.len()).max().unwrap_or(0);
    let mut buffer = vec![0u8; (1 << 20) + overlap];
    let mut carry = 0usize;
    let mut file = std::fs::File::open(path).map_err(io)?;
    loop {
        let n = file.read(&mut buffer[carry..]).map_err(io)?;
        if n == 0 {
            break;
        }
        let end = carry + n;
        // First-byte filter keeps the 200+ MiB metallib scan fast without optimisation.
        for (index, byte) in buffer[..end].iter().enumerate() {
            if *byte == b'l' || *byte == b'P' {
                for needle in NEEDLES {
                    if buffer[index..end].starts_with(needle) {
                        findings.push(format!(
                            "python marker bytes {:?}",
                            String::from_utf8_lossy(needle)
                        ));
                    }
                }
            }
        }
        carry = overlap.min(end);
        buffer.copy_within(end - carry..end, 0);
    }
    findings.sort();
    findings.dedup();
    Ok(findings)
}
/// Every Python indicator under `root` as `(relative path, reason)`; empty is clean.
pub fn audit_no_python(root: &Path) -> Result<Vec<(String, String)>, BundleError> {
    let census = census(root)?;
    let mut findings = Vec::new();
    let mut seen_dirs = std::collections::BTreeSet::new();
    let paths = census.files.iter().map(|f| &f.0).chain(&census.symlinks);
    for relative in paths {
        let parts: Vec<_> = relative.split('/').collect();
        for (index, part) in parts.iter().enumerate() {
            if python_name(part) {
                let prefix = parts[..=index].join("/");
                let is_final = index + 1 == parts.len();
                if is_final || seen_dirs.insert(prefix.clone()) {
                    findings.push((prefix, format!("python-named component {part}")));
                }
            }
        }
    }
    for (relative, _) in &census.files {
        for reason in scan_file(&root.join(relative))? {
            findings.push((relative.clone(), reason));
        }
    }
    findings.sort();
    findings.dedup();
    Ok(findings)
}

/// The only rpath an executable in `Contents/MacOS` may use to find the bundled dylibs.
pub const FRAMEWORKS_RPATH: &str = "@executable_path/../Frameworks";

/// `LC_RPATH` paths from `otool -l` output, in load-command order.
pub fn parse_rpaths(otool_l: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut lines = otool_l.lines();
    while let Some(line) = lines.next() {
        if line.trim() == "cmd LC_RPATH"
            && let Some(path) = lines
                .by_ref()
                .find_map(|l| l.trim_start().strip_prefix("path "))
        {
            out.push(path.split(" (offset").next().unwrap_or(path).to_string());
        }
    }
    out
}
/// Dependent library paths from `otool -L` output (the first line names the file itself).
pub fn parse_dependencies(otool_big_l: &str) -> Vec<String> {
    otool_big_l
        .lines()
        .skip(1)
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(|l| l.split(" (compatibility").next().unwrap_or(l).to_string())
        .collect()
}
fn is_macho(path: &Path) -> bool {
    let mut magic = [0u8; 4];
    std::fs::File::open(path)
        .and_then(|mut f| f.read_exact(&mut magic))
        .is_ok()
        && matches!(
            magic,
            [0xcf, 0xfa, 0xed, 0xfe] | [0xca, 0xfe, 0xba, 0xbe] | [0xfe, 0xed, 0xfa, 0xcf]
        )
}
fn otool(flag: &str, path: &Path) -> Result<String, BundleError> {
    let output = std::process::Command::new("/usr/bin/otool")
        .arg(flag)
        .arg(path)
        .output()
        .map_err(io)?;
    if !output.status.success() {
        return Err(BundleError::Io(std::io::ErrorKind::InvalidData));
    }
    String::from_utf8(output.stdout).map_err(|_| BundleError::Io(std::io::ErrorKind::InvalidData))
}
/// `LC_RPATH` entries of a Mach-O file; none for any other file.
pub fn executable_rpaths(path: &Path) -> Result<Vec<String>, BundleError> {
    if !is_macho(path) {
        return Ok(Vec::new());
    }
    Ok(parse_rpaths(&otool("-l", path)?))
}
/// Relocatability of an executable's links (Mach-O only): every rpath bundle-relative, every
/// dependency system or `@rpath`, and the Frameworks rpath present when anything is `@rpath`.
pub fn check_executable_links(path: &Path) -> Result<(), BundleError> {
    if !is_macho(path) {
        return Ok(());
    }
    let rpaths = executable_rpaths(path)?;
    if let Some(bad) = rpaths
        .iter()
        .find(|r| !crate::package::relocatable_rpath(r))
    {
        return Err(BundleError::Rpath(bad.clone()));
    }
    for dependency in parse_dependencies(&otool("-L", path)?) {
        if dependency.starts_with("@rpath/") {
            if !rpaths.iter().any(|r| r == FRAMEWORKS_RPATH) {
                return Err(BundleError::Rpath("missing_frameworks_rpath".into()));
            }
        } else if !crate::package::relocatable_link(&dependency) {
            return Err(BundleError::Link(dependency));
        }
    }
    Ok(())
}
