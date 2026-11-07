// SPDX-License-Identifier: AGPL-3.0-only
//! Read-only foundation input discovery. A pinned manifest is not a package,
//! minimum-OS, native-execution or release qualification capability.
use crate::{Fault, FaultKind, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
pub struct Content {
    pub size_bytes: u64,
    pub sha256: String,
}
#[derive(Clone, Debug, Deserialize)]
pub struct Reference {
    pub path: String,
    pub path_base: String,
    #[serde(flatten)]
    pub content: Content,
}
#[derive(Clone, Debug)]
pub struct Pins {
    pub native: Content,
    pub oracle: Content,
}
#[derive(Clone, Debug)]
pub struct NativeResource {
    pub source: Reference,
    pub destination: &'static str,
}
#[derive(Clone, Debug)]
pub struct Inputs {
    pins: Pins,
    resources: Vec<NativeResource>,
    rust_source: String,
    oracle_source: String,
    oracle_executable: Reference,
    oracle_zip: Reference,
}
/// Native file leaf -> bundle destination. MLX resolves its default Metal library only beside
/// `libmlx.dylib`, so the metallib lives in `Contents/Frameworks/` (the old
/// `Contents/Resources/metal/` layout made every real worker trap in
/// `mlx::core::metal::Device::Device()`; L11 round 7).
pub const NATIVE_DESTINATIONS: [(&str, &str); 4] = [
    ("libmlxc.dylib", "Contents/Frameworks/libmlxc.dylib"),
    ("libmlx.dylib", "Contents/Frameworks/libmlx.dylib"),
    ("libjaccl.dylib", "Contents/Frameworks/libjaccl.dylib"),
    ("mlx.metallib", "Contents/Frameworks/mlx.metallib"),
];
fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Admission)
}
impl Reference {
    pub fn resolve(&self, repository: &Path) -> Result<PathBuf> {
        let path = Path::new(&self.path);
        if self.path.is_empty()
            || self.path.contains(['\0', '\\', '\n', '\r'])
            || self.path.split('/').any(|p| p == ".." || p == ".")
        {
            return Err(fault("evidence_path_invalid"));
        }
        match self.path_base.as_str() {
            "absolute" if path.is_absolute() => Ok(path.into()),
            "repository" if !path.is_absolute() && repository.is_absolute() => {
                Ok(repository.join(path))
            }
            _ => Err(fault("evidence_path_invalid")),
        }
    }
}
impl Content {
    pub fn verify(&self, bytes: &[u8]) -> Result<()> {
        use sha2::{Digest, Sha256};
        if self.size_bytes == 0
            || bytes.len() as u64 != self.size_bytes
            || !crate::preflight::valid_digest(&self.sha256, 64)
            || format!("{:x}", Sha256::digest(bytes)) != self.sha256
        {
            return Err(fault("evidence_content_mismatch"));
        }
        Ok(())
    }
}
#[derive(Deserialize)]
struct NativeDocument {
    schema: String,
    rust_candidate: RustCandidate,
    native_inputs: Vec<Reference>,
}
#[derive(Deserialize)]
struct RustCandidate {
    source_commit: String,
}
#[derive(Deserialize)]
struct OracleDocument {
    schema: String,
    rust_candidate_source: String,
    accepted_source: AcceptedSource,
    certified_executable: Reference,
    published_zip: Reference,
}
#[derive(Deserialize)]
struct AcceptedSource {
    commit: String,
}
impl Inputs {
    /// Pins must come from the independently trusted seal/parent, not from the
    /// same untrusted documents. This authenticates discovery, not native loads.
    pub fn parse(native: &[u8], oracle: &[u8], pins: Pins) -> Result<Self> {
        pins.native.verify(native)?;
        pins.oracle.verify(oracle)?;
        let native: NativeDocument =
            serde_json::from_slice(native).map_err(|_| fault("evidence_manifest_invalid"))?;
        let oracle: OracleDocument =
            serde_json::from_slice(oracle).map_err(|_| fault("evidence_manifest_invalid"))?;
        if native.schema != "mycelium.foundation.native-input-evidence.v1"
            || oracle.schema != "mycelium.foundation.oracle-evidence.v1"
            || native.rust_candidate.source_commit != oracle.rust_candidate_source
            || !crate::preflight::valid_digest(&native.rust_candidate.source_commit, 40)
            || !crate::preflight::valid_digest(&oracle.accepted_source.commit, 40)
        {
            return Err(fault("evidence_manifest_binding_invalid"));
        }
        let expected = NATIVE_DESTINATIONS;
        if native.native_inputs.len() != expected.len() {
            return Err(fault("evidence_native_closure_invalid"));
        }
        let mut resources = Vec::new();
        for (leaf, destination) in expected {
            let matches: Vec<_> = native
                .native_inputs
                .iter()
                .filter(|r| Path::new(&r.path).file_name().and_then(|n| n.to_str()) == Some(leaf))
                .collect();
            if matches.len() != 1 {
                return Err(fault("evidence_native_closure_invalid"));
            }
            resources.push(NativeResource {
                source: matches[0].clone(),
                destination,
            });
        }
        for r in resources
            .iter()
            .map(|r| &r.source)
            .chain([&oracle.certified_executable, &oracle.published_zip])
        {
            r.resolve(Path::new("/repository"))?;
            if r.content.size_bytes == 0 || !crate::preflight::valid_digest(&r.content.sha256, 64) {
                return Err(fault("evidence_reference_invalid"));
            }
        }
        Ok(Self {
            pins,
            resources,
            rust_source: native.rust_candidate.source_commit,
            oracle_source: oracle.accepted_source.commit,
            oracle_executable: oracle.certified_executable,
            oracle_zip: oracle.published_zip,
        })
    }
    /// Read exactly the two named, bounded manifests. Never open any referenced
    /// executable/library/oracle, accept final-component symlinks, run code or
    /// mutate evidence. Ancestor selection is caller-owned; exact pins bind bytes.
    pub fn read_env(pins: Pins) -> Result<Self> {
        fn read(name: &str, expected: &Content) -> Result<Vec<u8>> {
            use std::{fs::OpenOptions, io::Read, os::unix::fs::OpenOptionsExt};
            let path = std::env::var_os(name)
                .filter(|p| !p.is_empty())
                .ok_or(fault("evidence_environment_missing"))?;
            let f = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                .open(path)
                .map_err(|_| fault("evidence_open_refused"))?;
            let m = f
                .metadata()
                .map_err(|_| fault("evidence_metadata_failed"))?;
            if !m.is_file() || m.len() != expected.size_bytes {
                return Err(fault("evidence_content_mismatch"));
            }
            let max = expected
                .size_bytes
                .checked_add(1)
                .ok_or(fault("evidence_size_invalid"))?;
            let mut bytes = Vec::new();
            f.take(max)
                .read_to_end(&mut bytes)
                .map_err(|_| fault("evidence_read_failed"))?;
            expected.verify(&bytes)?;
            Ok(bytes)
        }
        let native = read("MYCELIUM_NATIVE_INPUT_MANIFEST", &pins.native)?;
        let oracle = read("MYCELIUM_ORACLE_MANIFEST", &pins.oracle)?;
        Self::parse(&native, &oracle, pins)
    }
    pub fn resources(&self) -> &[NativeResource] {
        &self.resources
    }
    pub fn native_identity(&self) -> &Content {
        &self.pins.native
    }
    pub fn oracle_identity(&self) -> &Content {
        &self.pins.oracle
    }
    pub fn rust_source(&self) -> &str {
        &self.rust_source
    }
    pub fn oracle_source(&self) -> &str {
        &self.oracle_source
    }
    pub fn oracle_executable(&self) -> &Reference {
        &self.oracle_executable
    }
    pub fn oracle_zip(&self) -> &Reference {
        &self.oracle_zip
    }
}
