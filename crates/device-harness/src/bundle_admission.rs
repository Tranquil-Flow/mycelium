// SPDX-License-Identifier: AGPL-3.0-only
//! Package admission of a built v0.2 `.app` against independently pinned documents.
//!
//! The expected file set is derived from the genuine foundation-exit manifest (native
//! libraries + license notices, bytes pinned by the caller's manifest digest) and
//! cross-checked against the desktop crate's `package-pins.json`; the three remaining
//! files (executable, `Info.plist`, icon) come from caller-supplied [`Statics`]. The bundle
//! must equal that set exactly: same relative paths, sizes and sha256, nothing extra,
//! nothing missing, no symlink, no unexpected directory (so a stale `Resources/metal/`
//! tree cannot hide beside the real `Frameworks/mlx.metallib`).
//!
//! This is content admission only: it is not a signature, notarization, relocation,
//! minimum-OS or release-qualification claim, and it is not a defence against a hostile
//! same-uid writer racing the walk (the caller owns a quiescent bundle).
use crate::evidence_inputs::{Content, NATIVE_DESTINATIONS, Reference};
use crate::{Fault, FaultKind, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::Path;

pub const NATIVE_SCHEMA: &str = "mycelium.foundation.native-input-evidence.v1";
pub const RECEIPT_SCHEMA: &str = "mycelium.l12.bundle-admission.v1";
pub const EXECUTABLE: &str = "Contents/MacOS/mycelium";
pub const INFO_PLIST: &str = "Contents/Info.plist";
pub const ICON: &str = "Contents/Resources/icon-windowed.icns";
const LICENSES: &str = "Contents/Resources/licenses";
/// Read chunk for hashing (a buffer size, not an operational bound).
const CHUNK: usize = 1 << 16;

fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Admission)
}

/// Identities the genuine manifest cannot carry (the executable is built after it; the plist
/// and icon are assembled by the packager). Supplied by the caller from an independent source.
#[derive(Clone, Debug)]
pub struct Statics {
    pub executable: Content,
    pub info_plist: Content,
    pub icon: Content,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExpectedFile {
    pub bytes: u64,
    pub sha256: String,
    pub executable: bool,
}

#[derive(Debug)]
pub struct Expected {
    files: BTreeMap<String, ExpectedFile>,
    manifest: Content,
    pins: Content,
}

#[derive(Deserialize)]
struct Document {
    schema: String,
    native_inputs: Vec<Reference>,
    license_notices: Vec<Reference>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PinEntry {
    path: String,
    kind: String,
    bytes: u64,
    sha256: String,
}

fn leaf(reference: &Reference) -> Result<&str> {
    Path::new(&reference.path)
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|n| !n.is_empty() && !n.contains(['/', '\\', '\n', '\r', '\0']))
        .ok_or(fault("bundle_manifest_invalid"))
}
fn valid(content: &Content) -> bool {
    content.size_bytes != 0 && crate::preflight::valid_digest(&content.sha256, 64)
}
fn digest_of(bytes: &[u8]) -> Content {
    Content {
        size_bytes: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
    }
}

impl Expected {
    /// `manifest_pin` is the independently trusted digest of the genuine manifest bytes.
    /// `pins` (`package-pins.json`) must describe the same four native files, byte for byte.
    pub fn from_documents(
        manifest: &[u8],
        manifest_pin: &Content,
        pins: &[u8],
        statics: &Statics,
    ) -> Result<Self> {
        manifest_pin.verify(manifest)?;
        let document: Document =
            serde_json::from_slice(manifest).map_err(|_| fault("bundle_manifest_invalid"))?;
        if document.schema != NATIVE_SCHEMA
            || document.native_inputs.len() != NATIVE_DESTINATIONS.len()
        {
            return Err(fault("bundle_manifest_invalid"));
        }
        let mut files = BTreeMap::new();
        let mut put = |path: String, content: &Content, executable: bool| -> Result<()> {
            if !valid(content)
                || files
                    .insert(
                        path,
                        ExpectedFile {
                            bytes: content.size_bytes,
                            sha256: content.sha256.clone(),
                            executable,
                        },
                    )
                    .is_some()
            {
                return Err(fault("bundle_manifest_invalid"));
            }
            Ok(())
        };
        for (name, destination) in NATIVE_DESTINATIONS {
            let mut found = document
                .native_inputs
                .iter()
                .filter(|r| leaf(r).ok() == Some(name));
            let (Some(reference), None) = (found.next(), found.next()) else {
                return Err(fault("bundle_manifest_invalid"));
            };
            put(destination.into(), &reference.content, false)?;
        }
        for (index, reference) in document.license_notices.iter().enumerate() {
            put(
                format!("{LICENSES}/{:02}-{}", index + 1, leaf(reference)?),
                &reference.content,
                false,
            )?;
        }
        put(EXECUTABLE.into(), &statics.executable, true)?;
        put(INFO_PLIST.into(), &statics.info_plist, false)?;
        put(ICON.into(), &statics.icon, false)?;
        let entries: Vec<PinEntry> =
            serde_json::from_slice(pins).map_err(|_| fault("bundle_pins_invalid"))?;
        let mut pinned = BTreeSet::new();
        for entry in &entries {
            let expected = NATIVE_DESTINATIONS
                .iter()
                .any(|(_, d)| *d == entry.path)
                .then(|| files.get(&entry.path))
                .flatten();
            let kind_ok = matches!(
                (entry.kind.as_str(), entry.path.ends_with(".dylib")),
                ("Dylib", true) | ("Metal", false)
            );
            if !kind_ok
                || expected.is_none_or(|e| e.bytes != entry.bytes || e.sha256 != entry.sha256)
                || !pinned.insert(entry.path.as_str())
            {
                return Err(fault("bundle_pins_disagree"));
            }
        }
        if pinned.len() != NATIVE_DESTINATIONS.len() {
            return Err(fault("bundle_pins_disagree"));
        }
        Ok(Self {
            files,
            manifest: digest_of(manifest),
            pins: digest_of(pins),
        })
    }
    pub fn files(&self) -> &BTreeMap<String, ExpectedFile> {
        &self.files
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct AdmittedFile {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Serialize)]
pub struct Receipt {
    pub schema: &'static str,
    pub verdict: &'static str,
    pub manifest_sha256: String,
    pub package_pins_sha256: String,
    pub file_count: usize,
    pub total_bytes: u64,
    pub files: Vec<AdmittedFile>,
    /// What this receipt does not establish.
    pub not_claimed: [&'static str; 5],
}

fn hash_file(path: &Path, expected: &ExpectedFile) -> Result<String> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| fault("bundle_file_unreadable"))?;
    let meta = file
        .metadata()
        .map_err(|_| fault("bundle_file_unreadable"))?;
    if !meta.is_file() || meta.nlink() != 1 {
        return Err(fault("bundle_entry_not_regular"));
    }
    if meta.mode() & 0o022 != 0 || (expected.executable && meta.mode() & 0o100 == 0) {
        return Err(fault("bundle_file_mode"));
    }
    // Early reject before hashing. Observably redundant with the bounded read loop below, which
    // reports the same code for a longer or shorter file (mutant M16 is equivalent; both
    // directions are pinned by a test).
    if meta.len() != expected.bytes {
        return Err(fault("bundle_file_size_mismatch"));
    }
    let mut hash = Sha256::new();
    let mut buffer = vec![0u8; CHUNK];
    let mut remaining = expected.bytes;
    let mut reader = file.take(expected.bytes + 1);
    loop {
        let n = reader
            .read(&mut buffer)
            .map_err(|_| fault("bundle_file_unreadable"))?;
        if n == 0 {
            break;
        }
        if n as u64 > remaining {
            return Err(fault("bundle_file_size_mismatch"));
        }
        remaining -= n as u64;
        hash.update(&buffer[..n]);
    }
    if remaining != 0 {
        return Err(fault("bundle_file_size_mismatch"));
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn walk(
    root: &Path,
    relative: &str,
    expected: &Expected,
    seen: &mut BTreeMap<String, AdmittedFile>,
) -> Result<()> {
    let dir = root.join(relative);
    let mut children = std::fs::read_dir(&dir)
        .map_err(|_| fault("bundle_directory_unreadable"))?
        .collect::<std::io::Result<Vec<_>>>()
        .map_err(|_| fault("bundle_directory_unreadable"))?;
    children.sort_by_key(std::fs::DirEntry::file_name);
    for child in children {
        let name = child
            .file_name()
            .into_string()
            .map_err(|_| fault("bundle_unexpected_entry"))?;
        let path = if relative.is_empty() {
            name
        } else {
            format!("{relative}/{name}")
        };
        let kind = child
            .file_type()
            .map_err(|_| fault("bundle_directory_unreadable"))?;
        if kind.is_symlink() {
            return Err(fault("bundle_symlink_refused"));
        }
        if kind.is_dir() {
            let prefix = format!("{path}/");
            if !expected.files.keys().any(|p| p.starts_with(&prefix)) {
                return Err(fault("bundle_unexpected_entry"));
            }
            walk(root, &path, expected, seen)?;
        } else if let Some(want) = expected.files.get(&path) {
            let sha256 = hash_file(&child.path(), want)?;
            if sha256 != want.sha256 {
                return Err(fault("bundle_file_sha_mismatch"));
            }
            seen.insert(
                path.clone(),
                AdmittedFile {
                    path,
                    bytes: want.bytes,
                    sha256,
                },
            );
        } else {
            return Err(fault("bundle_unexpected_entry"));
        }
    }
    Ok(())
}

/// Admit `app` (the `.app` directory) iff its regular-file list, sizes and sha256 equal
/// `expected` exactly.
pub fn admit(app: &Path, expected: &Expected) -> Result<Receipt> {
    let meta = std::fs::symlink_metadata(app).map_err(|_| fault("bundle_root_unreadable"))?;
    // lstat: a symlink reports `is_dir() == false`, so a linked root is refused here and the
    // root is never canonicalised or followed.
    if !meta.is_dir() {
        return Err(fault("bundle_root_invalid"));
    }
    let mut seen = BTreeMap::new();
    walk(app, "", expected, &mut seen)?;
    if let Some(missing) = expected.files.keys().find(|p| !seen.contains_key(*p)) {
        let _ = missing;
        return Err(fault("bundle_file_missing"));
    }
    let files: Vec<_> = seen.into_values().collect();
    Ok(Receipt {
        schema: RECEIPT_SCHEMA,
        verdict: "admitted_content_only",
        manifest_sha256: expected.manifest.sha256.clone(),
        package_pins_sha256: expected.pins.sha256.clone(),
        file_count: files.len(),
        total_bytes: files.iter().map(|f| f.bytes).sum(),
        files,
        not_claimed: [
            "signature_or_notarization",
            "relocation",
            "minimum_os",
            "execution",
            "release_qualification",
        ],
    })
}
