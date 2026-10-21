// SPDX-License-Identifier: AGPL-3.0-only
//! Digest-verified, content-addressed per-tensor artifact store for v2 stage
//! manifests (plan M2; A12 spec §4.3: digest integrity, resumable acquisition,
//! atomic promotion).
//!
//! Layout under the store root (created owner-only):
//!
//! ```text
//! objects/<sha256 hex>        promoted tensors: complete, digest-verified
//! partial/<sha256 hex>        in-progress downloads (resumed by length)
//! manifests/<sha256 hex>.json seed-signed manifest envelopes, by pack digest
//! tmp/                        scratch for atomic manifest writes
//! ```
//!
//! Acquisition is transport-agnostic: a [`ChunkSource`] returns byte ranges of
//! one object by digest (the A2 chunk protocol client, a seed endpoint or a
//! test double). Bytes are appended to `partial/<hex>`; a restart resumes at
//! the partial's length. When the length is reached the whole partial is
//! re-hashed: a mismatch deletes it and fails closed; a match is fsynced and
//! renamed into `objects/` (then the directory is fsynced), so `objects/`
//! never holds unverified bytes. Loading re-verifies every object's digest
//! again before the bytes reach the quantizer, and deletes a corrupt object so
//! the next acquisition fetches it afresh.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::qwen2::{Qwen2Stage, RawTensor, TensorSource};
use crate::runtime::RuntimeError;
use crate::stage_manifest::{self, StageManifest, TensorSpec};

/// Upper bound on one fetched chunk (the A2 chunk size is at most this).
pub const MAX_CHUNK_BYTES: usize = 4 << 20;
/// Kept free on the volume beyond what an acquisition needs.
pub const FREE_SPACE_RESERVE_BYTES: u64 = 256 << 20;
/// Durable progress: fsync the partial every this many appended bytes.
const SYNC_EVERY_BYTES: u64 = 16 << 20;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ArtifactError(pub String);

fn fail(code: &str) -> ArtifactError {
    ArtifactError(code.to_owned())
}

fn io(code: &str) -> impl Fn(std::io::Error) -> ArtifactError + '_ {
    move |_| fail(code)
}

/// Byte ranges of one object, addressed by its `sha256:` digest.
pub trait ChunkSource {
    /// Return up to `max_len` bytes of object `sha256` starting at `offset`.
    /// Returning fewer bytes is allowed; returning none is a stall.
    fn fetch(&mut self, sha256: &str, offset: u64, max_len: usize) -> Result<Vec<u8>, ArtifactError>;
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AcquireReport {
    /// Objects already present and promoted before this call.
    pub reused_objects: usize,
    /// Objects promoted by this call.
    pub promoted_objects: usize,
    /// Bytes fetched from the source by this call.
    pub fetched_bytes: u64,
    /// Bytes found in partial files and not fetched again.
    pub resumed_bytes: u64,
}

pub struct ArtifactStore {
    root: PathBuf,
    quota_bytes: u64,
}

fn is_hex64(name: &str) -> bool {
    name.len() == 64 && name.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn make_private_dir(path: &Path) -> Result<(), ArtifactError> {
    fs::create_dir_all(path).map_err(io("artifact_store_unavailable"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).map_err(io("artifact_store_unavailable"))?;
    }
    Ok(())
}

fn sync_dir(path: &Path) -> Result<(), ArtifactError> {
    // Directory fsync makes a rename durable (POSIX; Android's ext4/f2fs).
    #[cfg(unix)]
    File::open(path).and_then(|d| d.sync_all()).map_err(io("artifact_sync_failed"))?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

fn sha256_file(path: &Path) -> Result<(String, u64), ArtifactError> {
    let mut f = File::open(path).map_err(io("artifact_object_unreadable"))?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    let mut total = 0u64;
    loop {
        let n = f.read(&mut buf).map_err(io("artifact_object_unreadable"))?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
        total += n as u64;
    }
    Ok((format!("sha256:{}", hex::encode(h.finalize())), total))
}

fn file_len(path: &Path) -> Option<u64> {
    fs::symlink_metadata(path).ok().filter(|m| m.file_type().is_file()).map(|m| m.len())
}

impl ArtifactStore {
    /// Open (or create) a store and run boot cleanup: scratch files and any
    /// entry that is not a well-formed content address are removed; partial
    /// downloads are kept for resumption.
    pub fn open(root: &Path, quota_bytes: u64) -> Result<Self, ArtifactError> {
        let store = Self { root: root.to_path_buf(), quota_bytes };
        for d in ["", "objects", "partial", "manifests", "tmp"] {
            make_private_dir(&store.root.join(d))?;
        }
        store.boot_cleanup()?;
        Ok(store)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    fn object_path(&self, hex: &str) -> PathBuf {
        self.root.join("objects").join(hex)
    }

    fn partial_path(&self, hex: &str) -> PathBuf {
        self.root.join("partial").join(hex)
    }

    fn manifest_path(&self, pack_digest: &str) -> Result<PathBuf, ArtifactError> {
        let hex = pack_digest.strip_prefix("sha256:").filter(|h| is_hex64(h)).ok_or_else(|| fail("artifact_manifest_digest_invalid"))?;
        Ok(self.root.join("manifests").join(format!("{hex}.json")))
    }

    fn boot_cleanup(&self) -> Result<(), ArtifactError> {
        for entry in fs::read_dir(self.root.join("tmp")).map_err(io("artifact_store_unavailable"))?.flatten() {
            let _ = fs::remove_file(entry.path());
        }
        for (dir, ok) in [
            ("objects", &(|n: &str| is_hex64(n)) as &dyn Fn(&str) -> bool),
            ("partial", &|n: &str| is_hex64(n)),
            ("manifests", &|n: &str| n.strip_suffix(".json").is_some_and(is_hex64)),
        ] {
            for entry in fs::read_dir(self.root.join(dir)).map_err(io("artifact_store_unavailable"))?.flatten() {
                let name = entry.file_name();
                let regular = entry.file_type().is_ok_and(|t| t.is_file());
                if !regular || !name.to_str().is_some_and(ok) {
                    let path = entry.path();
                    let _ = if entry.file_type().is_ok_and(|t| t.is_dir()) { fs::remove_dir_all(path) } else { fs::remove_file(path) };
                }
            }
        }
        Ok(())
    }

    /// Bytes held by promoted objects and partial downloads.
    pub fn used_bytes(&self) -> u64 {
        ["objects", "partial"]
            .iter()
            .filter_map(|d| fs::read_dir(self.root.join(d)).ok())
            .flat_map(|it| it.flatten())
            .filter_map(|e| e.metadata().ok())
            .map(|m| m.len())
            .sum()
    }

    /// Present as a promoted object of the right length (digest is re-checked at load).
    pub fn has_object(&self, spec: &TensorSpec) -> bool {
        file_len(&self.object_path(spec.hex())) == Some(spec.size_bytes)
    }

    /// Bytes still to fetch for `manifest` (partial progress counted as held).
    pub fn missing_bytes(&self, manifest: &StageManifest) -> u64 {
        let mut seen = std::collections::BTreeSet::new();
        manifest
            .tensors
            .iter()
            .filter(|t| seen.insert(t.hex().to_owned()) && !self.has_object(t))
            .map(|t| t.size_bytes - file_len(&self.partial_path(t.hex())).filter(|l| *l <= t.size_bytes).unwrap_or(0))
            .sum()
    }

    /// Storage admission: the store quota, and (when the platform reports it)
    /// the volume's free space less a fixed reserve.
    pub fn admit(&self, manifest: &StageManifest, available_bytes: Option<u64>) -> Result<u64, ArtifactError> {
        let need = self.missing_bytes(manifest);
        if self.used_bytes().saturating_add(need) > self.quota_bytes {
            return Err(fail("artifact_quota_exceeded"));
        }
        if let Some(free) = available_bytes
            && need.saturating_add(FREE_SPACE_RESERVE_BYTES) > free
        {
            return Err(fail("artifact_disk_space_insufficient"));
        }
        Ok(need)
    }

    /// Fetch every missing tensor of `manifest`, resuming partial downloads.
    pub fn acquire(
        &self,
        manifest: &StageManifest,
        source: &mut dyn ChunkSource,
        chunk_bytes: usize,
        available_bytes: Option<u64>,
    ) -> Result<AcquireReport, ArtifactError> {
        self.acquire_with(manifest, source, chunk_bytes, available_bytes, &mut || Ok(()))
    }

    /// [`acquire`](Self::acquire) with a policy gate asked before every chunk
    /// (thermal, battery, budget, cancellation): an `Err` pauses acquisition
    /// at once, keeping the durable partial for the next call to resume.
    pub fn acquire_with(
        &self,
        manifest: &StageManifest,
        source: &mut dyn ChunkSource,
        chunk_bytes: usize,
        available_bytes: Option<u64>,
        gate: &mut dyn FnMut() -> Result<(), ArtifactError>,
    ) -> Result<AcquireReport, ArtifactError> {
        if chunk_bytes == 0 || chunk_bytes > MAX_CHUNK_BYTES {
            return Err(fail("artifact_chunk_size_invalid"));
        }
        self.admit(manifest, available_bytes)?;
        let mut report = AcquireReport::default();
        for spec in &manifest.tensors {
            if self.has_object(spec) {
                report.reused_objects += 1;
                continue;
            }
            self.acquire_object(spec, source, chunk_bytes, &mut report, gate)?;
            report.promoted_objects += 1;
        }
        Ok(report)
    }

    fn acquire_object(
        &self,
        spec: &TensorSpec,
        source: &mut dyn ChunkSource,
        chunk_bytes: usize,
        report: &mut AcquireReport,
        gate: &mut dyn FnMut() -> Result<(), ArtifactError>,
    ) -> Result<(), ArtifactError> {
        let partial = self.partial_path(spec.hex());
        let mut file = OpenOptions::new().create(true).append(true).open(&partial).map_err(io("artifact_partial_unwritable"))?;
        let mut offset = file.metadata().map_err(io("artifact_partial_unwritable"))?.len();
        if offset > spec.size_bytes {
            // Longer than the object can be: never trust it.
            file.set_len(0).map_err(io("artifact_partial_unwritable"))?;
            offset = 0;
        }
        report.resumed_bytes += offset;
        let mut since_sync = 0u64;
        while offset < spec.size_bytes {
            let want = (spec.size_bytes - offset).min(chunk_bytes as u64) as usize;
            if let Err(paused) = gate() {
                file.sync_data().map_err(io("artifact_sync_failed"))?;
                return Err(paused);
            }
            let chunk = source.fetch(&spec.sha256, offset, want)?;
            if chunk.is_empty() {
                file.sync_data().map_err(io("artifact_sync_failed"))?;
                return Err(fail("artifact_source_stalled"));
            }
            if chunk.len() > want {
                file.sync_data().map_err(io("artifact_sync_failed"))?;
                return Err(fail("artifact_chunk_oversized"));
            }
            file.write_all(&chunk).map_err(io("artifact_partial_unwritable"))?;
            offset += chunk.len() as u64;
            report.fetched_bytes += chunk.len() as u64;
            since_sync += chunk.len() as u64;
            if since_sync >= SYNC_EVERY_BYTES {
                file.sync_data().map_err(io("artifact_sync_failed"))?;
                since_sync = 0;
            }
        }
        file.sync_all().map_err(io("artifact_sync_failed"))?;
        drop(file);
        self.promote(spec)
    }

    /// Re-hash a complete partial and atomically rename it into `objects/`.
    fn promote(&self, spec: &TensorSpec) -> Result<(), ArtifactError> {
        let partial = self.partial_path(spec.hex());
        let (digest, len) = sha256_file(&partial)?;
        if digest != spec.sha256 || len != spec.size_bytes {
            let _ = fs::remove_file(&partial);
            return Err(fail("artifact_digest_mismatch"));
        }
        fs::rename(&partial, self.object_path(spec.hex())).map_err(io("artifact_promotion_failed"))?;
        sync_dir(&self.root.join("objects"))?;
        sync_dir(&self.root.join("partial"))?;
        Ok(())
    }

    /// Persist a seed-signed manifest envelope atomically (tmp, fsync, rename).
    pub fn store_manifest(&self, envelope: &Value, pinned_seed_key: &Value) -> Result<StageManifest, ArtifactError> {
        let manifest = StageManifest::verify_signed(envelope, pinned_seed_key).map_err(|e| ArtifactError(e.0))?;
        let target = self.manifest_path(&manifest.pack_digest)?;
        let tmp = self.root.join("tmp").join(format!("{}.json", &manifest.pack_digest[7..]));
        let bytes = serde_json::to_vec(envelope).map_err(|_| fail("artifact_manifest_unwritable"))?;
        let mut f = File::create(&tmp).map_err(io("artifact_manifest_unwritable"))?;
        f.write_all(&bytes).map_err(io("artifact_manifest_unwritable"))?;
        f.sync_all().map_err(io("artifact_sync_failed"))?;
        drop(f);
        fs::rename(&tmp, &target).map_err(io("artifact_manifest_unwritable"))?;
        sync_dir(&self.root.join("manifests"))?;
        Ok(manifest)
    }

    /// Re-read and re-verify a persisted manifest (boot path).
    pub fn load_manifest(&self, pack_digest: &str, pinned_seed_key: &Value) -> Result<StageManifest, ArtifactError> {
        let raw = fs::read(self.manifest_path(pack_digest)?).map_err(io("artifact_manifest_missing"))?;
        let envelope: Value = serde_json::from_slice(&raw).map_err(|_| fail("artifact_manifest_invalid"))?;
        let manifest = StageManifest::verify_signed(&envelope, pinned_seed_key).map_err(|e| ArtifactError(e.0))?;
        if manifest.pack_digest != pack_digest {
            return Err(fail("artifact_manifest_invalid"));
        }
        Ok(manifest)
    }

    /// Every tensor of the manifest is promoted (lengths; digests at load).
    pub fn is_complete(&self, manifest: &StageManifest) -> bool {
        manifest.tensors.iter().all(|t| self.has_object(t))
    }

    /// Load and quantize the stage from verified objects, then run the
    /// manifest's golden self-check when it carries one.
    pub fn load_stage(&self, manifest: &StageManifest) -> Result<Qwen2Stage, ArtifactError> {
        let mut src = StoreTensorSource { store: self, manifest };
        let stage = Qwen2Stage::load(manifest.config.clone(), manifest.start_layer, manifest.end_layer_exclusive, &mut src)
            .map_err(|e| ArtifactError(e.0))?;
        if let Some(golden) = &manifest.golden {
            stage_manifest::self_check(&stage, golden).map_err(|e| ArtifactError(e.0))?;
        }
        Ok(stage)
    }

    /// [`load_stage`](Self::load_stage) for a stage that will serve traffic:
    /// the manifest must carry a golden, so loading proves bit-exact
    /// agreement with the host build before any frame is accepted.
    pub fn load_stage_for_serving(&self, manifest: &StageManifest) -> Result<Qwen2Stage, ArtifactError> {
        if manifest.golden.is_none() {
            return Err(fail("stage_manifest_golden_required"));
        }
        self.load_stage(manifest)
    }

    /// Remove objects, partials and manifests no kept manifest references.
    /// Returns the bytes freed.
    pub fn retain(&self, keep: &[&StageManifest]) -> u64 {
        let wanted: std::collections::BTreeSet<&str> = keep.iter().flat_map(|m| m.tensors.iter().map(TensorSpec::hex)).collect();
        let packs: std::collections::BTreeSet<String> = keep.iter().map(|m| format!("{}.json", &m.pack_digest[7..])).collect();
        let mut freed = 0;
        for dir in ["objects", "partial", "manifests"] {
            for entry in fs::read_dir(self.root.join(dir)).into_iter().flatten().flatten() {
                let name = entry.file_name().to_string_lossy().into_owned();
                let keep = if dir == "manifests" { packs.contains(&name) } else { wanted.contains(name.as_str()) };
                if !keep {
                    freed += entry.metadata().map(|m| m.len()).unwrap_or(0);
                    let _ = fs::remove_file(entry.path());
                }
            }
        }
        freed
    }
}

/// [`TensorSource`] over promoted objects: every read is length- and
/// digest-checked against the manifest before the bytes are used.
pub struct StoreTensorSource<'a> {
    store: &'a ArtifactStore,
    manifest: &'a StageManifest,
}

impl TensorSource for StoreTensorSource<'_> {
    fn tensor(&mut self, name: &str) -> Result<RawTensor, RuntimeError> {
        let spec = self.manifest.tensor(name).ok_or_else(|| RuntimeError("qwen2_tensor_missing".into()))?;
        let path = self.store.object_path(spec.hex());
        let mut f = File::open(&path).map_err(|_| RuntimeError("artifact_object_missing".into()))?;
        let mut bytes = Vec::with_capacity(spec.size_bytes as usize);
        f.seek(SeekFrom::Start(0)).and_then(|_| f.read_to_end(&mut bytes)).map_err(|_| RuntimeError("artifact_object_unreadable".into()))?;
        let digest = format!("sha256:{}", hex::encode(Sha256::digest(&bytes)));
        if bytes.len() as u64 != spec.size_bytes || digest != spec.sha256 {
            // Never load it; drop it so the next acquisition refetches.
            let _ = fs::remove_file(&path);
            return Err(RuntimeError("artifact_object_digest_mismatch".into()));
        }
        Ok(RawTensor { dtype: spec.dtype, shape: spec.shape.clone(), bytes })
    }
}

/// Bytes available to an unprivileged writer on the volume holding `path`
/// (`statvfs` `f_bavail * f_frsize`), for storage admission.
#[cfg(unix)]
pub fn available_bytes(path: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;
    let c = std::ffi::CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut st = std::mem::MaybeUninit::<libc::statvfs>::zeroed();
    // SAFETY: `c` is a valid NUL-terminated path and `st` is a properly sized,
    // writable statvfs buffer; it is only read after statvfs reports success.
    let rc = unsafe { libc::statvfs(c.as_ptr(), st.as_mut_ptr()) };
    if rc != 0 {
        return None;
    }
    // SAFETY: statvfs returned 0, so it initialised the buffer.
    let st = unsafe { st.assume_init() };
    #[allow(clippy::unnecessary_cast)] // field widths differ by platform
    Some((st.f_bavail as u64).saturating_mul(st.f_frsize as u64))
}

#[cfg(not(unix))]
pub fn available_bytes(_path: &Path) -> Option<u64> {
    None
}

/// The owner's daily network quota for artifact traffic
/// (`quota.network_bytes_per_day`), counted per UTC day and persisted in
/// `<store>/network-budget.json` so a restart cannot reset it.
pub struct NetworkBudget {
    path: PathBuf,
    limit: u64,
    day: u64,
    used: u64,
}

impl NetworkBudget {
    pub fn open(store_root: &Path, limit: u64, day: u64) -> Self {
        let path = store_root.join("network-budget.json");
        // Fail closed: a record that exists but cannot be read or parsed
        // counts as the day's quota spent (a reset never refunds traffic).
        let used = match fs::read(&path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
            Err(_) => limit,
            Ok(raw) => match serde_json::from_slice::<Value>(&raw).ok().and_then(|v| Some((v["day"].as_u64()?, v["bytes"].as_u64()?))) {
                Some((d, bytes)) if d == day => bytes,
                Some(_) => 0,
                None => limit,
            },
        };
        Self { path, limit, day, used }
    }

    pub fn used(&self) -> u64 {
        self.used
    }

    fn roll(&mut self, day: u64) {
        if day != self.day {
            self.day = day;
            self.used = 0;
        }
    }

    /// May `bytes` more be fetched today?
    pub fn admit(&mut self, day: u64, bytes: u64) -> Result<(), ArtifactError> {
        self.roll(day);
        if self.used.saturating_add(bytes) > self.limit {
            return Err(fail("network_budget_exhausted"));
        }
        Ok(())
    }

    /// Count fetched bytes and persist (tmp + rename, so a crash keeps either
    /// the old or the new count, never a torn one).
    pub fn record(&mut self, day: u64, bytes: u64) -> Result<(), ArtifactError> {
        self.roll(day);
        self.used = self.used.saturating_add(bytes);
        let tmp = self.path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_vec(&serde_json::json!({"day": self.day, "bytes": self.used})).unwrap_or_default())
            .and_then(|_| fs::rename(&tmp, &self.path))
            .map_err(io("artifact_network_budget_unwritable"))
    }
}

/// A [`ChunkSource`] that spends a [`NetworkBudget`]: a range is fetched only
/// if the day's quota covers its full requested length, then the bytes
/// actually received are counted.
pub struct MeteredSource<'a> {
    pub inner: &'a mut dyn ChunkSource,
    pub budget: &'a mut NetworkBudget,
    /// UTC day number (`unix_seconds / 86400`); injectable for tests.
    pub day: &'a dyn Fn() -> u64,
}

impl ChunkSource for MeteredSource<'_> {
    fn fetch(&mut self, sha256: &str, offset: u64, max_len: usize) -> Result<Vec<u8>, ArtifactError> {
        let day = (self.day)();
        self.budget.admit(day, max_len as u64)?;
        let data = self.inner.fetch(sha256, offset, max_len)?;
        self.budget.record(day, data.len() as u64)?;
        Ok(data)
    }
}

/// Today's UTC day number.
pub fn utc_day() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() / 86_400).unwrap_or(0)
}
