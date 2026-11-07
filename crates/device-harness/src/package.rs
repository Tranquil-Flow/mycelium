// SPDX-License-Identifier: AGPL-3.0-only
//! Inert-byte archive core; callers supply isolated package reads and finalizers.
use crate::{Fault, FaultKind, Result};
use serde_json::Value;
use std::collections::BTreeMap;
pub const APP: &str = "Mycelium A13 Demo.app";
pub const EXE: &str = "Contents/MacOS/Mycelium A13 Demo";
#[derive(Clone, Debug)]
pub enum Entry {
    File { bytes: Vec<u8>, executable: bool },
    Link(String),
}
impl Entry {
    pub fn file(bytes: Vec<u8>) -> Self {
        Self::File {
            bytes,
            executable: false,
        }
    }
    pub fn bytes(&self) -> &[u8] {
        match self {
            Self::File { bytes, .. } => bytes,
            Self::Link(s) => s.as_bytes(),
        }
    }
}
#[derive(Clone)]
pub struct Package {
    pub root: String,
    pub source_sha: String,
    pub checkout: String,
    pub dirty_lines: u64,
    pub version: String,
    pub executable_path: String,
    pub exe_sha256: String,
    pub entries: BTreeMap<String, Entry>,
}
pub trait Finalizer {
    fn finalize(&mut self, p: &Package, version: &str) -> Result<BTreeMap<String, Entry>>;
}
#[derive(Debug)]
pub struct Artifact {
    pub name: String,
    pub bytes: Vec<u8>,
    pub manifest: Value,
}
pub fn digest(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}
fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Fault::new(code, FaultKind::Admission))
    }
}
pub(crate) fn hex(value: &str, n: usize) -> bool {
    value.len() == n
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
fn safe_relative(path: &str) -> bool {
    !path.is_empty()
        && !path.contains(['\\', '\n', '\r', '\0'])
        && path
            .split('/')
            .all(|p| !p.is_empty() && p != "." && p != "..")
}
fn resolve_link(entries: &BTreeMap<String, Entry>, path: &str, target: &str) -> Result<()> {
    require(
        !target.starts_with('/') && !target.contains(['\\', '\n', '\r', '\0']),
        "bundle_link_invalid",
    )?;
    let parent = path.rsplit_once('/').map(|(p, _)| p).unwrap_or("");
    let mut parts: Vec<String> = if parent.is_empty() {
        vec![]
    } else {
        parent.split('/').map(str::to_owned).collect()
    };
    for p in target.split('/') {
        match p {
            "." | "" => {}
            ".." => {
                require(parts.pop().is_some(), "bundle_link_invalid")?;
            }
            s => parts.push(s.into()),
        }
    }
    for _ in 0..=entries.len() {
        let mut expanded = false;
        for i in 0..parts.len() {
            let prefix = parts[..=i].join("/");
            if let Some(Entry::Link(next)) = entries.get(&prefix) {
                require(
                    !next.starts_with('/') && !next.contains(['\\', '\n', '\r', '\0']),
                    "bundle_link_invalid",
                )?;
                let mut replacement = parts[..i].to_vec();
                for p in next.split('/') {
                    match p {
                        "." | "" => {}
                        ".." => {
                            require(replacement.pop().is_some(), "bundle_link_invalid")?;
                        }
                        s => replacement.push(s.into()),
                    }
                }
                replacement.extend_from_slice(&parts[i + 1..]);
                parts = replacement;
                expanded = true;
                break;
            }
        }
        if !expanded {
            let key = parts.join("/");
            return require(
                entries.contains_key(&key)
                    || entries.keys().any(|p| p.starts_with(&(key.clone() + "/"))),
                "bundle_link_invalid",
            );
        }
    }
    Err(Fault::new("bundle_link_invalid", FaultKind::Admission))
}
pub fn bundle_digest(entries: &BTreeMap<String, Entry>) -> Result<String> {
    let mut tree = BTreeMap::new();
    for (path, entry) in entries {
        require(safe_relative(path), "bundle_path_invalid")?;
        for (other, value) in entries {
            require(
                !matches!(value, Entry::Link(_)) || !path.starts_with(&(other.clone() + "/")),
                "bundle_link_invalid",
            )?;
        }
        let value = match entry {
            Entry::File { bytes, .. } => format!("sha256:{}", digest(bytes)),
            Entry::Link(target) => {
                resolve_link(entries, path, target)?;
                format!("link:{target}")
            }
        };
        tree.insert(path, value);
    }
    let bytes = serde_json::to_vec(&tree)
        .map_err(|_| Fault::new("bundle_encoding_failed", FaultKind::Input))?;
    Ok(format!("sha256:{}", digest(&bytes)))
}
pub(crate) fn version_ok(v: &str) -> bool {
    let (core, suffix) = v
        .split_once('-')
        .map(|(a, b)| (a, Some(b)))
        .unwrap_or((v, None));
    core.split('.').count() == 3
        && core
            .split('.')
            .all(|p| !p.is_empty() && p.bytes().all(|c| c.is_ascii_digit()))
        && suffix.is_none_or(|s| {
            !s.is_empty()
                && s.bytes()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'.')
        })
}
pub fn package_identity(p: &Package) -> Result<()> {
    require(
        p.root.starts_with('/') && safe_relative(p.root.trim_start_matches('/')),
        "package_root_invalid",
    )?;
    require(
        hex(&p.source_sha, 40)
            && p.root.rsplit('/').next() == Some(format!("l2-final-{}", p.source_sha).as_str()),
        "package_source_mismatch",
    )?;
    require(p.dirty_lines == 0, "package_checkout_dirty")?;
    require(version_ok(&p.version), "package_version_invalid")?;
    require(
        p.checkout == format!("{}/checkout", p.root),
        "package_checkout_path_invalid",
    )?;
    require(
        p.executable_path == format!("{}/package/out/{}/{APP}/{EXE}", p.root, p.version),
        "package_exe_path_invalid",
    )?;
    let entry = p
        .entries
        .get(EXE)
        .ok_or(Fault::new("package_exe_path_invalid", FaultKind::Admission))?;
    require(
        matches!(entry, Entry::File { .. }),
        "package_exe_path_invalid",
    )?;
    require(
        hex(&p.exe_sha256, 64) && digest(entry.bytes()) == p.exe_sha256,
        "package_exe_hash_mismatch",
    )?;
    Ok(())
}
fn json_entry(entries: &BTreeMap<String, Entry>, name: &str) -> Result<Value> {
    let e = entries
        .get(name)
        .ok_or(Fault::new("package_metadata_missing", FaultKind::Admission))?;
    require(matches!(e, Entry::File { .. }), "package_metadata_invalid")?;
    serde_json::from_slice(e.bytes())
        .map_err(|_| Fault::new("package_metadata_invalid", FaultKind::Admission))
}
fn app_entries(entries: &BTreeMap<String, Entry>) -> BTreeMap<String, Entry> {
    let prefix = format!("{APP}/");
    entries
        .iter()
        .filter_map(|(p, e)| p.strip_prefix(&prefix).map(|name| (name.into(), e.clone())))
        .collect()
}
fn metadata(
    entries: &BTreeMap<String, Entry>,
    source_sha: &str,
    version: &str,
    bd: &str,
) -> Result<()> {
    let source = json_entry(entries, "install-source.json")?;
    let manifest = json_entry(entries, "installation-package-manifest.json")?;
    let identity = json_entry(entries, "artifact-identity.json")?;
    require(
        source["artifact_name"] == format!("mycelium-{version}-macos-arm64")
            && identity["version"] == version,
        "install_version_mismatch",
    )?;
    require(
        source["artifact_digest"] == bd
            && manifest["package_digest"] == bd
            && identity["bundle_digest"] == bd,
        "install_bundle_digest_mismatch",
    )?;
    require(
        manifest["source_revision"] == source_sha && identity["source_revision"] == source_sha,
        "install_source_mismatch",
    )?;
    let sums = entries.get("SHA256SUMS").ok_or(Fault::new(
        "install_checksums_incomplete",
        FaultKind::Admission,
    ))?;
    let text = std::str::from_utf8(sums.bytes())
        .map_err(|_| Fault::new("install_checksums_mismatch", FaultKind::Admission))?;
    let mut covered = std::collections::BTreeSet::new();
    for line in text.lines() {
        let (hash, name) = line.split_once("  ").ok_or(Fault::new(
            "install_checksums_mismatch",
            FaultKind::Admission,
        ))?;
        require(
            !name.contains('/') && safe_relative(name) && covered.insert(name),
            "install_checksums_mismatch",
        )?;
        let e = entries.get(name).ok_or(Fault::new(
            "install_checksums_mismatch",
            FaultKind::Admission,
        ))?;
        require(
            matches!(e, Entry::File { .. }) && digest(e.bytes()) == hash,
            "install_checksums_mismatch",
        )?;
    }
    for name in [
        "install-source.json",
        "installation-package-manifest.json",
        "Install Mycelium.command",
        "APP-FILES.sha256",
    ] {
        require(covered.contains(name), "install_checksums_incomplete")?;
    }
    Ok(())
}
pub fn build_device<F: Finalizer>(
    p: &Package,
    version: &str,
    spent: bool,
    f: &mut F,
) -> Result<Artifact> {
    use std::io::{Cursor, Write};
    package_identity(p)?;
    require(version_ok(version), "unsupported_version")?;
    let bd = bundle_digest(&p.entries)?;
    require(!spent, "device_output_already_spent")?;
    let mut entries = f.finalize(p, version)?;
    let app = app_entries(&entries);
    require(
        app.get(EXE)
            .is_some_and(|e| matches!(e, Entry::File { .. }) && digest(e.bytes()) == p.exe_sha256),
        "finalized_exe_hash_mismatch",
    )?;
    require(bundle_digest(&app)? == bd, "finalized_bundle_mismatch")?;
    metadata(&entries, &p.source_sha, version, &bd)?;
    let mut manifest = serde_json::json!({"schema":"mycelium.device_test_package.v1","package_root":p.root,"source_sha":p.source_sha,"exe_sha256":p.exe_sha256,"version":version,"bundle_digest":bd,"pre_ready_device_test":true,"release_qualified":false});
    let bytes = serde_json::to_vec(&manifest)
        .map_err(|_| Fault::new("manifest_encoding_failed", FaultKind::Input))?;
    entries.insert("DEVICE-TEST-MANIFEST.json".into(), Entry::file(bytes));
    let prefix = format!("mycelium-{version}-macos-arm64");
    let name = format!("{prefix}-DEVICE-TEST-NOT-FOR-RELEASE.zip");
    let mut writer = zip::ZipWriter::new(Cursor::new(vec![]));
    let zip_error = |_| Fault::new("device_zip_write_failed", FaultKind::Observation);
    for (path, entry) in &entries {
        require(safe_relative(path), "device_zip_member_invalid")?;
        let name = format!("{prefix}/{path}");
        let options =
            zip::write::FileOptions::default().compression_method(zip::CompressionMethod::Deflated);
        match entry {
            Entry::Link(target) => writer
                .add_symlink(name, target, options)
                .map_err(zip_error)?,
            Entry::File { bytes, executable } => {
                writer
                    .start_file(
                        name,
                        options.unix_permissions(if *executable { 0o755 } else { 0o644 }),
                    )
                    .map_err(zip_error)?;
                writer
                    .write_all(bytes)
                    .map_err(|_| Fault::new("device_zip_write_failed", FaultKind::Observation))?;
            }
        }
    }
    let bytes = writer.finish().map_err(zip_error)?.into_inner();
    let hash = digest(&bytes);
    verify_device(&name, &bytes, version, &hash, &p.exe_sha256)?;
    manifest["zip_sha256"] = Value::from(hash);
    Ok(Artifact {
        name,
        bytes,
        manifest,
    })
}
pub fn verify_device(
    name: &str,
    bytes: &[u8],
    version: &str,
    zip: &str,
    exe: &str,
) -> Result<Value> {
    use std::io::{Cursor, Read};
    require(
        name.ends_with("-DEVICE-TEST-NOT-FOR-RELEASE.zip"),
        "device_zip_name_required",
    )?;
    require(
        hex(zip, 64) && digest(bytes) == zip,
        "device_zip_hash_mismatch",
    )?;
    let mut archive = zip::ZipArchive::new(Cursor::new(bytes))
        .map_err(|_| Fault::new("device_zip_invalid", FaultKind::Admission))?;
    let prefix = format!("mycelium-{version}-macos-arm64/");
    let mut entries = BTreeMap::new();
    let mut names = std::collections::BTreeSet::new();
    let mut total = 0u64;
    for index in 0..archive.len() {
        let mut file = archive
            .by_index(index)
            .map_err(|_| Fault::new("device_zip_invalid", FaultKind::Admission))?;
        require(
            names.insert(file.name().to_owned()),
            "device_zip_duplicate_member",
        )?;
        let path = file
            .name()
            .strip_prefix(&prefix)
            .ok_or(Fault::new(
                "device_zip_member_invalid",
                FaultKind::Admission,
            ))?
            .trim_end_matches('/')
            .to_owned();
        require(safe_relative(&path), "device_zip_member_invalid")?;
        // This in-memory core is intentionally bounded; a streaming native adapter is separate work.
        total = total
            .checked_add(file.size())
            .ok_or(Fault::new("device_zip_size_limit", FaultKind::Admission))?;
        require(total <= 512 * 1024 * 1024, "device_zip_size_limit")?;
        if file.is_dir() {
            continue;
        }
        let mode = file.unix_mode().unwrap_or(0o100644);
        let mut body = vec![];
        file.read_to_end(&mut body)
            .map_err(|_| Fault::new("device_zip_read_failed", FaultKind::Observation))?;
        let e = if mode & 0o170000 == 0o120000 {
            Entry::Link(
                String::from_utf8(body)
                    .map_err(|_| Fault::new("bundle_link_invalid", FaultKind::Admission))?,
            )
        } else {
            require(mode & 0o170000 == 0o100000, "bundle_special_file")?;
            Entry::File {
                bytes: body,
                executable: mode & 0o111 != 0,
            }
        };
        require(
            entries.insert(path, e).is_none(),
            "device_zip_duplicate_member",
        )?;
    }
    let manifest = json_entry(&entries, "DEVICE-TEST-MANIFEST.json")?;
    require(
        manifest["schema"] == "mycelium.device_test_package.v1"
            && manifest["pre_ready_device_test"] == true
            && manifest["release_qualified"] == false,
        "device_manifest_invalid",
    )?;
    let source = manifest["source_sha"].as_str().ok_or(Fault::new(
        "device_manifest_source_invalid",
        FaultKind::Admission,
    ))?;
    let root = manifest["package_root"].as_str().ok_or(Fault::new(
        "device_manifest_source_invalid",
        FaultKind::Admission,
    ))?;
    require(
        hex(source, 40)
            && root.starts_with('/')
            && safe_relative(root.trim_start_matches('/'))
            && root.rsplit('/').next() == Some(format!("l2-final-{source}").as_str()),
        "device_manifest_source_invalid",
    )?;
    require(
        manifest["version"] == version && manifest["exe_sha256"] == exe,
        "device_manifest_identity_mismatch",
    )?;
    let app = app_entries(&entries);
    require(
        app.get(EXE)
            .is_some_and(|e| matches!(e, Entry::File { .. }) && digest(e.bytes()) == exe),
        "device_zip_exe_hash_mismatch",
    )?;
    let bd = bundle_digest(&app)?;
    require(
        manifest["bundle_digest"] == bd,
        "device_zip_bundle_mismatch",
    )?;
    metadata(&entries, source, version, &bd)?;
    Ok(manifest)
}
pub fn check_ready(p: &Package, manifest: &Value, ready: &Value, result: &Value) -> Result<Value> {
    require(
        manifest["schema"] == "mycelium.device_test_package.v1"
            && manifest["pre_ready_device_test"] == true,
        "device_manifest_invalid",
    )?;
    require(
        result["stranger_path_passed"] == true && result["pre_ready_device_test"] == true,
        "device_run_not_passed",
    )?;
    for key in ["exe_sha256", "zip_sha256", "source_sha", "package_root"] {
        require(
            !manifest[key].is_null() && result[key] == manifest[key],
            "device_result_identity_mismatch",
        )?;
    }
    require(
        ready["verdict"] == "READY" && ready["dry_run"] == false && ready["fire_ready"] == true,
        "ready_not_admitted",
    )?;
    require(
        ready["sha"] == manifest["source_sha"]
            && ready["package_root"] == manifest["package_root"]
            && manifest["source_sha"] == p.source_sha
            && manifest["package_root"] == p.root,
        "ready_source_mismatch",
    )?;
    package_identity(p)?;
    require(
        ready["exe"]["sha256"] == manifest["exe_sha256"]
            && manifest["exe_sha256"] == p.exe_sha256
            && ready["exe"]["path"] == p.executable_path,
        "ready_exe_hash_mismatch",
    )?;
    Ok(
        serde_json::json!({"same_bytes":true,"exe_sha256":p.exe_sha256,"source_sha":p.source_sha,"release_qualified":false,"scope":"executable_identity_only"}),
    )
}
