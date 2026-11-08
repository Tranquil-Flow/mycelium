// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded acquisition of one registered model snapshot: `acquisition.py` (`SelectedModelAcquisition`,
//! `_HTTPRepositoryTransport`) @ f4e366e9, checked against the executed oracle
//! (`tests/fixtures/oracle_acquisition.json`, 123 scenarios from `L10-evidence/r18-probe-acquisition.py`).
//!
//! It downloads and digest-verifies a pinned public Hugging Face repository into one private local
//! snapshot, then asks a [`Preparer`] (the native manifest compiler, a seam: `serving.prepare_local_source`)
//! to accept it. It never quantizes, loads tensors, starts a worker or marks a route ready.
//!
//! Differences kept on purpose: a JSON string with a lone surrogate is a parse error here
//! (`model_metadata_invalid`; Python would reach `model_manifest_invalid`), and a manifest path of exactly
//! `"."` is refused (Python's `PurePosixPath(".")` slips through). Hugging Face never produces either.
use crate::model_catalog::Model;
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{Read, Write},
    net::{TcpStream, ToSocketAddrs},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub const MANIFEST_PROTOCOL: &str = "mycelium.a13_selected_model_repository_manifest.v1";
/// `OFFLINE_ERROR_CODE` (`mycelium_platform_neutral/offline.py`).
pub const OFFLINE_ERROR_CODE: &str = "offline_model_source_required";
const HF_ORIGIN: &str = "https://huggingface.co";
const O_NOFOLLOW: i32 = 0x100;
const ACTIVE_STATES: [&str; 6] = [
    "starting",
    "resolving_metadata",
    "checking_capacity",
    "downloading",
    "verifying",
    "compiling",
];
const OTHER_STATES: [&str; 5] = ["idle", "prepared", "failed", "cancelled", "closed"];
/// Source-specific policies (acquisition.py:82/:84/:86 @ f4e366e9).
fn max_repository_files() -> usize {
    BudgetId::AcquisitionMaxRepositoryFiles
        .count()
        .map_or(0, |n| n.get() as usize)
}
fn max_path_parts() -> usize {
    BudgetId::AcquisitionMaxPathParts
        .count()
        .map_or(0, |n| n.get() as usize)
}
fn stream_chunk_bytes() -> usize {
    budget_bytes(BudgetId::AcquisitionStreamChunkBytes) as usize
}
/// `urllib.request.HTTPRedirectHandler.max_repeats` / `.max_redirections`.
const REDIRECT_REPEATS: u32 = 4;
const REDIRECT_LIMIT: usize = 10;
/// `thread.join(timeout=float(timeout) + 2.0)` (acquisition.py:1535-1541).
/// The caller transport timeout takes precedence over its default; this is an
/// additive join allowance, not a cap on an override.
fn close_patience(timeout: Duration) -> Duration {
    timeout
        + BudgetId::AcquisitionCloseJoinSlack
            .span(Entries::ZERO)
            .map_or(Duration::ZERO, |s| Duration::from_millis(s.millis()))
}
// HTTP framing is a distinct origin, not AcquisitionStreamChunkBytes; registration pending.
const MAX_REDIRECT_HEAD: usize = 64 * 1024;

#[cfg(test)]
#[path = "acquisition_bounds_tests.rs"]
mod bounds_tests;

fn budget_bytes(id: BudgetId) -> u64 {
    id.bytes().map_or(0, |b| b.get())
}
fn max_file_bytes() -> u64 {
    budget_bytes(BudgetId::PyExactMyceliumA13MacosAcquisitionL85C0)
}
fn max_metadata_bytes() -> u64 {
    budget_bytes(BudgetId::PyMyceliumA13MacosAcquisitionMaxMetadataBytesL81)
}
fn max_path_bytes() -> usize {
    usize::try_from(budget_bytes(
        BudgetId::PyMyceliumA13MacosAcquisitionMaxPathBytesL83,
    ))
    .unwrap_or(0)
}
fn max_status_bytes() -> u64 {
    budget_bytes(BudgetId::PyFinalMyceliumA13MacosAcquisitionMaxBytesL886C62)
}
fn partial_max_age_seconds() -> f64 {
    BudgetId::PyMyceliumA13MacosAcquisitionPartialMaxAgeSecondsL87
        .span(Entries::new(0))
        .map_or(0.0, |s| s.millis() as f64 / 1000.0)
}
/// `_DEFAULT_TIMEOUT_SECONDS`.
pub fn default_timeout() -> Duration {
    BudgetId::PyMyceliumA13MacosAcquisitionDefaultTimeoutSecondsL88
        .span(Entries::new(0))
        .map_or(Duration::ZERO, |s| Duration::from_millis(s.millis()))
}

/// A stable acquisition failure: the message is a non-sensitive code.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AcqError(pub String);
impl AcqError {
    pub fn code(&self) -> &str {
        &self.0
    }
}
fn raise<T>(code: &str) -> Result<T, AcqError> {
    Err(AcqError(code.to_owned()))
}
fn err(code: &str) -> AcqError {
    AcqError(code.to_owned())
}

/// `asdict(model)`: the identity carried in every status document.
pub fn model_identity(model: &Model) -> Value {
    json!({
        "model_id": model.model_id,
        "resolved_commit": model.resolved_commit,
        "source_representation": "bfloat16-safetensors",
        "runtime_backend": "mlx",
        "runtime_dtype": "float32",
        "runtime_quantization": "int8-weight-only",
        "decoding": "quantized_greedy_token_id",
        "sampling_seed": 0,
    })
}

/// One file of the pinned repository.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Record {
    pub path: String,
    pub size: u64,
    /// `sha256` (LFS) or `git-sha1` (a git blob id).
    pub algorithm: &'static str,
    pub digest: String,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Manifest {
    pub files: Vec<Record>,
    pub total: u64,
    pub expires_at_unix_ms: Option<i64>,
}

fn is_lower_hex(text: &str, len: usize) -> bool {
    text.len() == len
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
/// `_safe_relative_path`.
fn safe_relative_path(value: &Value) -> Result<String, AcqError> {
    let bad = || err("model_manifest_invalid");
    let text = value.as_str().ok_or_else(bad)?;
    if text.is_empty()
        || text.contains('\0')
        || text.len() > max_path_bytes()
        || text.starts_with('/')
    {
        return Err(bad());
    }
    let parts: Vec<&str> = text.split('/').collect();
    if parts.len() > max_path_parts() || parts.iter().any(|p| matches!(*p, "" | "." | "..")) {
        return Err(bad());
    }
    Ok(text.to_owned())
}
/// Python's `type(x) is int` over a JSON number (a bool or a float is not).
fn json_int(value: &Value) -> Option<i128> {
    let Value::Number(n) = value else { return None };
    if let Some(v) = n.as_i64() {
        return Some(i128::from(v));
    }
    if let Some(v) = n.as_u64() {
        return Some(i128::from(v));
    }
    let text = n.to_string();
    if text.bytes().all(|b| b.is_ascii_digit() || b == b'-') && !text.is_empty() {
        // A whole number beyond 64 bits: Python keeps it as an int.
        return Some(if text.starts_with('-') {
            i128::MIN
        } else {
            i128::MAX
        });
    }
    None
}
fn positive_size(value: &Value) -> Option<u64> {
    let v = json_int(value)?;
    u64::try_from(v)
        .ok()
        .filter(|s| *s > 0 && *s <= max_file_bytes())
}

/// `_validate_manifest` over already-typed records plus `HTTPRepositoryTransport.resolve`'s per-sibling
/// checks: build the manifest from the hub's metadata document.
pub fn manifest_from_metadata(document: &Value, model: &Model) -> Result<Manifest, AcqError> {
    let invalid = || err("model_metadata_invalid");
    let object = document.as_object();
    if !object.is_some_and(|o| {
        o.get("private") == Some(&Value::Bool(false))
            && o.get("sha").and_then(Value::as_str) == Some(model.resolved_commit)
            && o.get("id").and_then(Value::as_str) == Some(model.model_id)
            && o.get("modelId").and_then(Value::as_str) == Some(model.model_id)
            && o.get("siblings").is_some_and(Value::is_array)
    }) {
        return raise("model_metadata_identity_mismatch");
    }
    let siblings = document["siblings"].as_array().ok_or_else(invalid)?;
    let mut raw: Vec<(Value, u64, &'static str, String)> = Vec::new();
    for sibling in siblings {
        let sibling = sibling.as_object().ok_or_else(invalid)?;
        let path = safe_relative_path(sibling.get("rfilename").unwrap_or(&Value::Null))?;
        let size_value = sibling.get("size").unwrap_or(&Value::Null);
        let size = positive_size(size_value).ok_or_else(invalid)?;
        let (algorithm, digest) = match sibling.get("lfs") {
            Some(Value::Null) | None => {
                let blob = sibling
                    .get("blobId")
                    .and_then(Value::as_str)
                    .filter(|b| is_lower_hex(b, 40))
                    .ok_or_else(invalid)?;
                ("git-sha1", blob.to_owned())
            }
            Some(lfs) => {
                let lfs = lfs.as_object().ok_or_else(invalid)?;
                let sha = lfs
                    .get("sha256")
                    .and_then(Value::as_str)
                    .filter(|d| is_lower_hex(d, 64));
                if lfs.get("size") != Some(size_value) || sha.is_none() {
                    return Err(invalid());
                }
                ("sha256", sha.unwrap_or_default().to_owned())
            }
        };
        raw.push((Value::String(path), size, algorithm, digest));
    }
    build_manifest(raw)
}
/// The second half of `_validate_manifest` (counts, duplicates, required files, totals, ordering).
fn build_manifest(raw: Vec<(Value, u64, &'static str, String)>) -> Result<Manifest, AcqError> {
    let bad = || err("model_manifest_invalid");
    if raw.is_empty() || raw.len() > max_repository_files() {
        return Err(bad());
    }
    let mut seen = BTreeSet::new();
    let mut total: u64 = 0;
    let mut files = Vec::new();
    for (path, size, algorithm, digest) in raw {
        let path = path.as_str().ok_or_else(bad)?.to_owned();
        if !seen.insert(path.clone()) {
            return Err(bad());
        }
        total = total.checked_add(size).ok_or_else(bad)?;
        if total > max_file_bytes() {
            return Err(bad());
        }
        files.push(Record {
            path,
            size,
            algorithm,
            digest,
        });
    }
    if !seen.contains("config.json") || !seen.iter().any(|p| p.ends_with(".safetensors")) {
        return Err(bad());
    }
    files.sort_by(|a, b| a.path.as_bytes().cmp(b.path.as_bytes()));
    Ok(Manifest {
        files,
        total,
        expires_at_unix_ms: None,
    })
}

/// One response to a file request.
pub trait FileResponse: Read + Send {
    fn status(&self) -> u16;
    /// Case-insensitive header lookup (the first value).
    fn header(&self, name: &str) -> Option<String>;
}
/// Where the pinned file list and the file bytes come from.
pub trait Transport: Send + Sync {
    fn resolve(&self, model: &Model) -> Result<Manifest, AcqError>;
    fn open_file(
        &self,
        model: &Model,
        record: &Record,
        offset: u64,
    ) -> Result<Box<dyn FileResponse>, AcqError>;
    fn timeout(&self) -> Duration {
        default_timeout()
    }
}
/// Why a [`Preparer`] refused: `ServingPreparationError` (a closed code) or anything else.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PrepareError {
    Serving(String),
    Other,
}
/// `serving.prepare_local_source`: accept the verified snapshot (the native manifest compiler).
pub trait Preparer: Send + Sync {
    fn prepare(&self, snapshot: &Path, model: &Model) -> Result<(), PrepareError>;
    /// The same acceptance, keeping what `PreparedSource` carries (`source_bytes`, `manifest_digest`) for
    /// the owner run chain (SE-L10-MODEL-FACTS). A preparer that keeps none answers `None`.
    fn prepare_facts(
        &self,
        snapshot: &Path,
        model: &Model,
    ) -> Result<Option<PreparedFacts>, PrepareError> {
        self.prepare(snapshot, model).map(|()| None)
    }
}
/// `PreparedSource.source_bytes` / `.manifest_digest` of the accepted snapshot.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PreparedFacts {
    pub source_bytes: u64,
    /// `sha256:<64 hex>`.
    pub manifest_digest: String,
}
/// `shutil.disk_usage` could not be read.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DiskProbeFailed;
/// `_disk_free_bytes(path)`.
pub type DiskFree = Box<dyn Fn(&Path) -> Result<u64, DiskProbeFailed> + Send + Sync>;
/// `_test_observer`-style hook.
pub type StatusObserver = Box<dyn Fn(&Status) + Send + Sync>;
/// Constructor seams (`_disk_free_bytes`, `_clock_unix_ms`, `_transport`).
pub struct Options {
    pub transport: Arc<dyn Transport>,
    pub preparer: Arc<dyn Preparer>,
    pub disk_free: DiskFree,
    pub clock_unix_ms: Box<dyn Fn() -> i64 + Send + Sync>,
    /// Called with every status that was just persisted by an attempt.
    pub observer: Option<StatusObserver>,
}
/// Free bytes under `path` (`shutil.disk_usage(path).free`).
#[allow(unsafe_code)] // statvfs(3) into a zeroed struct for a valid path
pub fn disk_free_bytes(path: &Path) -> Result<u64, DiskProbeFailed> {
    use std::os::unix::ffi::OsStrExt;
    let text = std::ffi::CString::new(path.as_os_str().as_bytes()).map_err(|_| DiskProbeFailed)?;
    let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(text.as_ptr(), &mut stat) } != 0 {
        return Err(DiskProbeFailed);
    }
    Ok((stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64))
}
pub fn system_clock_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| i64::try_from(d.as_millis()).unwrap_or(i64::MAX))
}

// ------------------------------------------------------------------ files

fn random_hex() -> Result<String, AcqError> {
    let mut bytes = [0u8; 16];
    fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| err("artifact_storage_failure"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn fsync_directory(path: &Path) -> Result<(), AcqError> {
    fs::File::open(path)
        .and_then(|d| d.sync_all())
        .map_err(|_| err("artifact_storage_failure"))
}
/// `_atomic_json`: the canonical (sorted, compact, newline-terminated) document through a private temp file.
fn atomic_json(path: &Path, value: &Value) -> Result<(), AcqError> {
    let parent = path.parent().unwrap_or(Path::new("/"));
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("status");
    let temporary = parent.join(format!(".{name}.{}.tmp", random_hex()?));
    let mut text = serde_json::to_string(value).map_err(|_| err("artifact_storage_failure"))?;
    text.push('\n');
    let result = (|| -> std::io::Result<()> {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(O_NOFOLLOW)
            .open(&temporary)?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        // Whether or not the temporary could be removed, the write failed.
        fs::remove_file(&temporary).unwrap_or_default();
        return raise("artifact_storage_failure");
    }
    fsync_directory(parent)
}
fn euid() -> u32 {
    rustix::process::geteuid().as_raw()
}
/// `_private_root`.
fn private_root(candidate: &Path) -> Result<PathBuf, AcqError> {
    let unsafe_root = || err("acquisition_state_root_unsafe");
    if !candidate.is_absolute()
        || fs::symlink_metadata(candidate).is_ok_and(|m| m.file_type().is_symlink())
    {
        return Err(unsafe_root());
    }
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(candidate)
        .map_err(|_| unsafe_root())?;
    // Admit the trusted root once, including legitimate ancestor aliases such
    // as macOS /var. Retain only its physical path; secure_directory and file
    // no-follow checks still refuse every symlink below this boundary.
    let resolved = fs::canonicalize(candidate).map_err(|_| unsafe_root())?;
    let meta = fs::symlink_metadata(candidate).map_err(|_| unsafe_root())?;
    if meta.file_type().is_symlink()
        || !meta.is_dir()
        || meta.uid() != euid()
        || meta.mode() & 0o077 != 0
    {
        return Err(unsafe_root());
    }
    Ok(resolved)
}
/// `_secure_directory`.
fn secure_directory(path: &Path, create: bool) -> Result<PathBuf, AcqError> {
    let unsafe_dir = || err("artifact_storage_unsafe");
    if create {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)
            .map_err(|_| unsafe_dir())?;
    }
    let meta = fs::symlink_metadata(path).map_err(|_| unsafe_dir())?;
    let resolved = fs::canonicalize(path).map_err(|_| unsafe_dir())?;
    if resolved != path
        || meta.file_type().is_symlink()
        || !meta.is_dir()
        || meta.uid() != euid()
        || meta.mode() & 0o022 != 0
    {
        return Err(unsafe_dir());
    }
    Ok(path.to_owned())
}
/// `_secure_file_size`.
fn secure_file_size(path: &Path, maximum: u64) -> Result<u64, AcqError> {
    let meta = fs::symlink_metadata(path).map_err(|_| err("artifact_storage_unsafe"))?;
    if meta.file_type().is_symlink()
        || !meta.is_file()
        || meta.nlink() != 1
        || meta.uid() != euid()
        || meta.mode() & 0o077 != 0
        || meta.len() > maximum
    {
        return raise("artifact_storage_unsafe");
    }
    Ok(meta.len())
}
enum Hasher {
    Sha256(Sha256),
    Sha1(sha1::Sha1),
}
/// `_digest_file`: the file is a single-link regular file of the recorded size whose digest matches.
fn digest_file(path: &Path, record: &Record) -> bool {
    let Ok(mut file) = fs::OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(path)
    else {
        return false;
    };
    let Ok(meta) = file.metadata() else {
        return false;
    };
    if !meta.is_file() || meta.nlink() != 1 || meta.uid() != euid() || meta.len() != record.size {
        return false;
    }
    let mut hasher = if record.algorithm == "sha256" {
        Hasher::Sha256(Sha256::new())
    } else {
        let mut h = sha1::Sha1::new();
        sha1::Digest::update(&mut h, format!("blob {}\0", meta.len()).as_bytes());
        Hasher::Sha1(h)
    };
    let mut block = vec![0u8; stream_chunk_bytes()];
    loop {
        match file.read(&mut block) {
            Ok(0) => break,
            Ok(n) => match &mut hasher {
                Hasher::Sha256(h) => Digest::update(h, &block[..n]),
                Hasher::Sha1(h) => sha1::Digest::update(h, &block[..n]),
            },
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return false,
        }
    }
    let hex = |bytes: &[u8]| bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    let got = match hasher {
        Hasher::Sha256(h) => hex(&h.finalize()),
        Hasher::Sha1(h) => hex(&sha1::Digest::finalize(h)),
    };
    got == record.digest
}

// ------------------------------------------------------------------ status

/// The five-key status document.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Status {
    pub state: String,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub error_code: Option<String>,
}
impl Status {
    fn idle() -> Self {
        Self {
            state: "idle".into(),
            downloaded_bytes: 0,
            total_bytes: 0,
            error_code: None,
        }
    }
    pub fn to_value(&self, model: &Model) -> Value {
        json!({
            "state": self.state,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "error_code": self.error_code,
            "model": model_identity(model),
        })
    }
}
fn valid_error_code(code: &str) -> bool {
    let mut bytes = code.bytes();
    bytes.next().is_some_and(|b| b.is_ascii_lowercase())
        && code.len() <= 128
        && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}
/// `_validate_status`.
fn validate_status(value: &Value, model: &Model) -> Result<Status, AcqError> {
    let corrupt = || err("acquisition_state_corrupt");
    let object = value.as_object().ok_or_else(corrupt)?;
    let keys: BTreeSet<&str> = object.keys().map(String::as_str).collect();
    if keys
        != BTreeSet::from([
            "state",
            "downloaded_bytes",
            "total_bytes",
            "error_code",
            "model",
        ])
    {
        return Err(corrupt());
    }
    let state = object["state"].as_str().ok_or_else(corrupt)?;
    if !ACTIVE_STATES.contains(&state) && !OTHER_STATES.contains(&state) {
        return Err(corrupt());
    }
    let count = |v: &Value| json_int(v).and_then(|n| u64::try_from(n).ok());
    let downloaded = count(&object["downloaded_bytes"]).ok_or_else(corrupt)?;
    let total = count(&object["total_bytes"]).ok_or_else(corrupt)?;
    let error = match &object["error_code"] {
        Value::Null => None,
        Value::String(code) if valid_error_code(code) => Some(code.clone()),
        _ => return Err(corrupt()),
    };
    if downloaded > total || object["model"] != model_identity(model) {
        return Err(corrupt());
    }
    Ok(Status {
        state: state.to_owned(),
        downloaded_bytes: downloaded,
        total_bytes: total,
        error_code: error,
    })
}

// ------------------------------------------------------------------ the acquisition

struct State {
    status: Status,
    active: bool,
    closed: bool,
    prepared: Option<PathBuf>,
    prepared_facts: Option<PreparedFacts>,
    verified_manifest: Option<Manifest>,
    network_bytes: u64,
    lock: Option<fs::File>,
    thread: Option<JoinHandle<()>>,
}
struct Inner {
    model: &'static Model,
    state_root: PathBuf,
    status_path: PathBuf,
    options: Options,
    guard: Mutex<State>,
    cancel: AtomicBool,
    /// Set when the worker's body has finished (the `close` join waits on it).
    finished: (Mutex<bool>, Condvar),
}
/// `SelectedModelAcquisition`.
#[derive(Clone)]
pub struct Acquisition {
    inner: Arc<Inner>,
}
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

impl Acquisition {
    /// `SelectedModelAcquisition(state_root, model=..., ...)`: take the process lock, recover the status.
    pub fn open(
        state_root: &Path,
        model: &'static Model,
        options: Options,
    ) -> Result<Self, AcqError> {
        let root = private_root(state_root)?;
        let lock_file = acquire_lock(&root)?;
        let status_path = root.join("status.json");
        let status = recover_status(&status_path, model)?;
        Ok(Self {
            inner: Arc::new(Inner {
                model,
                state_root: root,
                status_path,
                options,
                guard: Mutex::new(State {
                    status,
                    active: false,
                    closed: false,
                    prepared: None,
                    prepared_facts: None,
                    verified_manifest: None,
                    network_bytes: 0,
                    lock: Some(lock_file),
                    thread: None,
                }),
                cancel: AtomicBool::new(false),
                finished: (Mutex::new(true), Condvar::new()),
            }),
        })
    }
    pub fn state_root(&self) -> &Path {
        &self.inner.state_root
    }
    pub fn model(&self) -> &'static Model {
        self.inner.model
    }
    pub fn status(&self) -> Status {
        lock(&self.inner.guard).status.clone()
    }
    /// The status document as `/api/status` carries it (with the model identity).
    pub fn status_value(&self) -> Value {
        self.status().to_value(self.inner.model)
    }
    /// The prepared snapshot root and its verified manifest, without re-hashing.
    pub fn verified_snapshot(&self) -> Option<(PathBuf, Manifest)> {
        let state = lock(&self.inner.guard);
        if state.closed || state.status.state != "prepared" {
            return None;
        }
        Some((state.prepared.clone()?, state.verified_manifest.clone()?))
    }
    /// `prepared_source` (`acquisition.py:1492-1514`): the native source-only preparation after complete
    /// verification. The snapshot is re-verified on every call; a snapshot that no longer verifies leaves
    /// the acquisition `failed` with that code. A preparer that kept no facts answers `source_facts_unavailable`.
    pub fn prepared_source(&self) -> Result<(PathBuf, PreparedFacts), AcqError> {
        let (root, manifest, facts) = {
            let state = lock(&self.inner.guard);
            if state.closed {
                return raise("acquisition_closed");
            }
            match (&state.prepared, &state.verified_manifest) {
                (Some(root), Some(manifest)) if state.status.state == "prepared" => {
                    (root.clone(), manifest.clone(), state.prepared_facts.clone())
                }
                _ => return raise("source_not_prepared"),
            }
        };
        if let Err(error) = self.verify_snapshot(&root, &manifest) {
            {
                let mut state = lock(&self.inner.guard);
                state.prepared = None;
                state.prepared_facts = None;
                state.verified_manifest = None;
            }
            let code = error.code().to_owned();
            self.set_status(|s| {
                s.state = "failed".into();
                s.error_code = Some(code);
            })?;
            return Err(error);
        }
        let facts = facts.ok_or_else(|| err("source_facts_unavailable"))?;
        Ok((root, facts))
    }
    fn set_status(&self, change: impl FnOnce(&mut Status)) -> Result<Status, AcqError> {
        let mut state = lock(&self.inner.guard);
        self.set_status_locked(&mut state, change)
    }
    fn set_status_locked(
        &self,
        state: &mut State,
        change: impl FnOnce(&mut Status),
    ) -> Result<Status, AcqError> {
        let mut next = state.status.clone();
        change(&mut next);
        let value = next.to_value(self.inner.model);
        validate_status(&value, self.inner.model)?;
        atomic_json(&self.inner.status_path, &value)?;
        state.status = next.clone();
        if let Some(observer) = &self.inner.options.observer {
            observer(&next);
        }
        Ok(next)
    }
    /// `reverify_closed_snapshot`: re-hash a complete snapshot left on disk by an earlier run (a grant of one
    /// network byte: nothing can be downloaded).
    pub fn reverify_closed_snapshot(&self, storage_limit_bytes: u64) -> Result<Status, AcqError> {
        {
            let state = lock(&self.inner.guard);
            if !["closed", "idle", "failed", "cancelled"].contains(&state.status.state.as_str()) {
                return raise(if state.active {
                    "acquisition_in_progress"
                } else {
                    "source_already_prepared"
                });
            }
            let (_stage, snapshot) = self.paths_if_present();
            if snapshot.is_none() {
                return raise("source_not_on_disk");
            }
        }
        self.start(&json!({
            "download_acknowledged": true,
            "max_download_bytes": 1,
            "storage_limit_bytes": storage_limit_bytes,
        }))
    }
    fn paths_if_present(&self) -> (Option<PathBuf>, Option<PathBuf>) {
        let snapshots = self.snapshots_dir();
        let stage = snapshots.join(format!(".{}.acquiring", self.inner.model.resolved_commit));
        let snapshot = snapshots.join(self.inner.model.resolved_commit);
        let staged = stage.exists()
            || fs::symlink_metadata(&stage).is_ok_and(|m| m.file_type().is_symlink());
        let final_dir = snapshot.is_dir()
            && !fs::symlink_metadata(&snapshot).is_ok_and(|m| m.file_type().is_symlink());
        (
            staged.then_some(stage),
            (final_dir && !staged).then_some(snapshot),
        )
    }
    fn snapshots_dir(&self) -> PathBuf {
        self.inner
            .state_root
            .join(format!(
                "models--{}",
                self.inner.model.model_id.replace('/', "--")
            ))
            .join("snapshots")
    }
    fn begin(&self, consent: &Value) -> Result<Consent, AcqError> {
        let checked = validate_consent(consent)?;
        let mut state = lock(&self.inner.guard);
        if state.closed {
            return raise("acquisition_closed");
        }
        if state.active {
            return raise("acquisition_in_progress");
        }
        if state.prepared.is_some() {
            return raise("source_already_prepared");
        }
        state.active = true;
        self.inner.cancel.store(false, Ordering::SeqCst);
        state.network_bytes = 0;
        self.set_status_locked(&mut state, |s| {
            s.state = "starting".into();
            s.downloaded_bytes = 0;
            s.total_bytes = 0;
            s.error_code = None;
        })?;
        Ok(checked)
    }
    /// `start`: one bounded background acquisition after exact explicit consent.
    pub fn start(&self, consent: &Value) -> Result<Status, AcqError> {
        let checked = self.begin(consent)?;
        *lock(&self.inner.finished.0) = false;
        let this = self.clone();
        let spawned = std::thread::Builder::new()
            .name("mycelium-a13-acquisition".into())
            .spawn(move || this.worker(&checked));
        match spawned {
            Ok(handle) => {
                lock(&self.inner.guard).thread = Some(handle);
                Ok(self.status())
            }
            Err(_) => {
                lock(&self.inner.guard).active = false;
                *lock(&self.inner.finished.0) = true;
                self.set_status(|s| {
                    s.state = "failed".into();
                    s.error_code = Some("acquisition_start_failed".into());
                })?;
                raise("acquisition_start_failed")
            }
        }
    }
    /// `_run_blocking`: the deterministic seam, the same state machine as [`Self::start`].
    pub fn run_blocking(&self, consent: &Value) -> Result<Status, AcqError> {
        let checked = self.begin(consent)?;
        self.worker(&checked);
        Ok(self.status())
    }
    /// Wait until the background attempt has ended (a test and shutdown helper).
    pub fn wait(&self, patience: Duration) -> bool {
        let (mutex, condvar) = &self.inner.finished;
        let mut done = lock(mutex);
        let deadline = std::time::Instant::now() + patience;
        while !*done {
            let left = deadline.saturating_duration_since(std::time::Instant::now());
            if left.is_zero() {
                return false;
            }
            done = condvar
                .wait_timeout(done, left)
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .0;
        }
        true
    }
    fn worker(&self, consent: &Consent) {
        let outcome =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.execute(consent)));
        let (state, code) = match outcome {
            Ok(Ok(())) => (None, None),
            Ok(Err(AcqError(code))) if code == "acquisition_cancelled" => (Some("cancelled"), None),
            Ok(Err(AcqError(code))) => (Some("failed"), Some(code)),
            Err(_) => (Some("failed"), Some("acquisition_failed".to_owned())),
        };
        if let Some(state) = state
            && self
                .set_status(|s| {
                    s.state = state.into();
                    s.error_code = code;
                })
                .is_err()
        {
            // The status could not be written: nothing more can be recorded.
            lock(&self.inner.guard).status.state = state.into();
        }
        lock(&self.inner.guard).active = false;
        let (mutex, condvar) = &self.inner.finished;
        *lock(mutex) = true;
        condvar.notify_all();
    }
    fn check_cancelled(&self) -> Result<(), AcqError> {
        if self.inner.cancel.load(Ordering::SeqCst) {
            return raise("acquisition_cancelled");
        }
        Ok(())
    }
    fn resolve_manifest(&self) -> Result<Manifest, AcqError> {
        self.set_status(|s| s.state = "resolving_metadata".into())?;
        self.check_cancelled()?;
        let manifest = self.inner.options.transport.resolve(self.inner.model)?;
        let now = (self.inner.options.clock_unix_ms)();
        if now < 1 {
            return raise("acquisition_clock_invalid");
        }
        if manifest.expires_at_unix_ms.is_some_and(|e| now >= e) {
            return raise("model_manifest_expired");
        }
        Ok(manifest)
    }
    fn paths(&self) -> Result<(PathBuf, PathBuf), AcqError> {
        let model_root = secure_directory(
            &self.inner.state_root.join(format!(
                "models--{}",
                self.inner.model.model_id.replace('/', "--")
            )),
            true,
        )?;
        let snapshots = secure_directory(&model_root.join("snapshots"), true)?;
        Ok((
            snapshots.join(format!(".{}.acquiring", self.inner.model.resolved_commit)),
            snapshots.join(self.inner.model.resolved_commit),
        ))
    }
    fn check_disk(&self, required: u64) -> Result<(), AcqError> {
        match (self.inner.options.disk_free)(&self.inner.state_root) {
            Err(DiskProbeFailed) => raise("disk_availability_unavailable"),
            Ok(free) if free < required => raise("insufficient_disk"),
            Ok(_) => Ok(()),
        }
    }
    fn execute(&self, consent: &Consent) -> Result<(), AcqError> {
        let manifest = self.resolve_manifest()?;
        let total = manifest.total;
        self.set_status(|s| {
            s.state = "checking_capacity".into();
            s.total_bytes = total;
        })?;
        if total > consent.storage_limit_bytes {
            return raise("storage_limit_exceeded");
        }
        self.check_cancelled()?;
        let (stage, snapshot) = self.paths()?;
        if snapshot.exists() {
            if stage.exists()
                || fs::symlink_metadata(&stage).is_ok_and(|m| m.file_type().is_symlink())
            {
                return raise("artifact_storage_unsafe");
            }
            self.verify_snapshot(&snapshot, &manifest)?;
        } else {
            let stage = secure_directory(&stage, true)?;
            let have = self.scan_stage(&stage, &manifest)?;
            self.set_status(|s| s.downloaded_bytes = have)?;
            let remaining = total - have;
            if remaining > consent.max_download_bytes {
                return raise("download_limit_exceeded");
            }
            match (self.inner.options.disk_free)(&self.inner.state_root) {
                Err(DiskProbeFailed) => return raise("disk_availability_unavailable"),
                Ok(free) if free < remaining => return raise("insufficient_disk"),
                Ok(_) => {}
            }
            self.set_status(|s| s.state = "downloading".into())?;
            for record in &manifest.files {
                self.check_cancelled()?;
                let (final_path, partial) = stage_file_paths(&stage, &record.path)?;
                if final_path.exists() {
                    continue;
                }
                self.download_file(record, &partial, &final_path, total, consent)?;
            }
            self.set_status(|s| {
                s.state = "verifying".into();
                s.downloaded_bytes = total;
            })?;
            self.verify_snapshot(&stage, &manifest)?;
            fs::rename(&stage, &snapshot).map_err(|_| err("artifact_storage_failure"))?;
            fsync_directory(snapshot.parent().unwrap_or(Path::new("/")))?;
            self.verify_snapshot(&snapshot, &manifest)?;
        }
        self.check_cancelled()?;
        self.set_status(|s| {
            s.state = "compiling".into();
            s.downloaded_bytes = total;
        })?;
        let facts = match self
            .inner
            .options
            .preparer
            .prepare_facts(&snapshot, self.inner.model)
        {
            Ok(facts) => facts,
            Err(PrepareError::Serving(_)) => return raise("native_manifest_compile_failed"),
            Err(PrepareError::Other) => return raise("acquisition_failed"),
        };
        self.verify_snapshot(&snapshot, &manifest)?;
        {
            let mut state = lock(&self.inner.guard);
            state.prepared = Some(snapshot.clone());
            state.prepared_facts = facts;
            state.verified_manifest = Some(manifest);
        }
        self.set_status(|s| {
            s.state = "prepared".into();
            s.downloaded_bytes = total;
            s.total_bytes = total;
            s.error_code = None;
        })?;
        Ok(())
    }
    fn scan_stage(&self, stage: &Path, manifest: &Manifest) -> Result<u64, AcqError> {
        let unsafe_stage = || err("artifact_storage_unsafe");
        let expected_files: BTreeSet<&str> =
            manifest.files.iter().map(|r| r.path.as_str()).collect();
        let mut expected_dirs: BTreeSet<String> = BTreeSet::new();
        for path in &expected_files {
            let mut parent = Path::new(path).parent();
            while let Some(p) = parent {
                if p.as_os_str().is_empty() {
                    break;
                }
                expected_dirs.insert(p.to_string_lossy().into_owned());
                parent = p.parent();
            }
        }
        let mut pending = vec![stage.to_owned()];
        while let Some(dir) = pending.pop() {
            for entry in fs::read_dir(&dir).map_err(|_| unsafe_stage())? {
                let entry = entry.map_err(|_| unsafe_stage())?;
                let path = entry.path();
                let meta = fs::symlink_metadata(&path).map_err(|_| unsafe_stage())?;
                let relative = path
                    .strip_prefix(stage)
                    .map_err(|_| unsafe_stage())?
                    .to_string_lossy()
                    .into_owned();
                // `os.walk(followlinks=False)` lists a directory symlink as a directory (and does not enter it).
                let listed_as_dir = meta.is_dir()
                    || (meta.file_type().is_symlink()
                        && fs::metadata(&path).is_ok_and(|m| m.is_dir()));
                if listed_as_dir {
                    if meta.file_type().is_symlink() || !expected_dirs.contains(&relative) {
                        return Err(unsafe_stage());
                    }
                    pending.push(path);
                } else {
                    let plain = relative.strip_suffix(".partial").unwrap_or(&relative);
                    if !expected_files.contains(plain)
                        || (relative != plain && relative != format!("{plain}.partial"))
                    {
                        return Err(unsafe_stage());
                    }
                }
            }
        }
        let mut downloaded = 0u64;
        let now_seconds = (self.inner.options.clock_unix_ms)() as f64 / 1_000.0;
        for record in &manifest.files {
            let (final_path, partial) = stage_file_paths(stage, &record.path)?;
            let partial_exists = partial.exists()
                || fs::symlink_metadata(&partial).is_ok_and(|m| m.file_type().is_symlink());
            if final_path.exists()
                || fs::symlink_metadata(&final_path).is_ok_and(|m| m.file_type().is_symlink())
            {
                secure_file_size(&final_path, record.size)?;
                if !digest_file(&final_path, record) {
                    return raise("artifact_digest_mismatch");
                }
                if partial_exists {
                    return raise("artifact_storage_unsafe");
                }
                downloaded += record.size;
                continue;
            }
            if !partial_exists {
                continue;
            }
            let size = secure_file_size(&partial, record.size)?;
            let modified = fs::metadata(&partial)
                .and_then(|m| m.modified())
                .map_err(|_| unsafe_stage())?;
            let modified_seconds = modified
                .duration_since(UNIX_EPOCH)
                .map_or(0.0, |d| d.as_secs_f64());
            if now_seconds - modified_seconds > partial_max_age_seconds() {
                fs::remove_file(&partial).map_err(|_| err("artifact_storage_failure"))?;
                fsync_directory(partial.parent().unwrap_or(stage))?;
                continue;
            }
            if size == record.size {
                if !digest_file(&partial, record) {
                    if let Err(e) = fs::remove_file(&partial)
                        && e.kind() != std::io::ErrorKind::NotFound
                    {
                        return raise("artifact_storage_failure");
                    }
                    fsync_directory(partial.parent().unwrap_or(stage))?;
                    return raise("artifact_digest_mismatch");
                }
                fs::rename(&partial, &final_path).map_err(|_| err("artifact_storage_failure"))?;
                fsync_directory(final_path.parent().unwrap_or(stage))?;
                downloaded += size;
            } else {
                downloaded += size;
            }
        }
        Ok(downloaded)
    }
    fn download_file(
        &self,
        record: &Record,
        partial: &Path,
        final_path: &Path,
        total: u64,
        consent: &Consent,
    ) -> Result<(), AcqError> {
        let mut offset = 0u64;
        if partial.exists()
            || fs::symlink_metadata(partial).is_ok_and(|m| m.file_type().is_symlink())
        {
            offset = secure_file_size(partial, record.size)?;
        }
        let remaining = record.size - offset;
        if remaining == 0 {
            return raise("artifact_storage_unsafe");
        }
        self.check_disk(remaining)?;
        let mut response =
            self.inner
                .options
                .transport
                .open_file(self.inner.model, record, offset)?;
        let status = response.status();
        if status != 200 && status != 206 {
            return raise("source_response_invalid");
        }
        if response.header("Content-Encoding").is_some()
            && response.header("Content-Encoding").as_deref() != Some("identity")
        {
            return raise("source_response_invalid");
        }
        let mut restart = offset > 0 && status == 200;
        if status == 206 {
            let expected_range = format!("bytes {offset}-{}/{}", record.size - 1, record.size);
            if response.header("Content-Range").as_deref() != Some(expected_range.as_str()) {
                return raise("source_response_invalid");
            }
        } else if offset == 0 {
            restart = false;
        }
        let expected = if restart {
            record.size
        } else {
            record.size - offset
        };
        if let Some(declared) = response.header("Content-Length") {
            let parsed = python_int(&declared).ok_or_else(|| err("source_response_invalid"))?;
            if parsed != i128::from(expected) {
                return raise("source_response_invalid");
            }
        }
        let mut handle = open_partial(partial)?;
        if restart {
            handle
                .set_len(0)
                .map_err(|_| err("artifact_storage_unsafe"))?;
            let on_disk = self.stage_bytes_for_status()?;
            self.set_status(|s| s.downloaded_bytes = on_disk)?;
        } else {
            let end = std::io::Seek::seek(&mut handle, std::io::SeekFrom::End(0))
                .map_err(|_| err("artifact_storage_unsafe"))?;
            if end != offset {
                return raise("artifact_storage_unsafe");
            }
        }
        let mut written = 0u64;
        let mut buffer = vec![0u8; stream_chunk_bytes()];
        while written < expected {
            self.check_cancelled()?;
            let request_size =
                usize::try_from((expected - written).min(stream_chunk_bytes() as u64))
                    .unwrap_or(stream_chunk_bytes());
            let got = read_fill(&mut *response, &mut buffer[..request_size]);
            let Ok(got) = got else {
                return raise("source_disconnected");
            };
            if got == 0 {
                return raise("source_disconnected");
            }
            if written + got as u64 > expected {
                return raise("source_response_invalid");
            }
            self.write_chunk(&mut handle, &buffer[..got], total, consent)?;
            written += got as u64;
        }
        let mut extra = [0u8; 1];
        match read_fill(&mut *response, &mut extra) {
            Err(_) => return raise("source_disconnected"),
            Ok(n) if n > 0 => return raise("source_response_invalid"),
            Ok(_) => {}
        }
        handle
            .sync_all()
            .map_err(|_| err("artifact_storage_failure"))?;
        drop(handle);
        if !digest_file(partial, record) {
            if let Err(e) = fs::remove_file(partial)
                && e.kind() != std::io::ErrorKind::NotFound
            {
                return raise("artifact_storage_failure");
            }
            fsync_directory(partial.parent().unwrap_or(Path::new("/")))?;
            let on_disk = self.stage_bytes_for_status()?;
            self.set_status(|s| s.downloaded_bytes = on_disk)?;
            return raise("artifact_digest_mismatch");
        }
        fs::rename(partial, final_path).map_err(|_| err("artifact_storage_failure"))?;
        fsync_directory(final_path.parent().unwrap_or(Path::new("/")))
    }
    fn write_chunk(
        &self,
        handle: &mut fs::File,
        chunk: &[u8],
        total: u64,
        consent: &Consent,
    ) -> Result<(), AcqError> {
        let len = chunk.len() as u64;
        if lock(&self.inner.guard).network_bytes + len > consent.max_download_bytes {
            return raise("download_limit_exceeded");
        }
        self.check_disk(len)?;
        let current = self.status().downloaded_bytes;
        if current + len > total || current + len > consent.storage_limit_bytes {
            return raise("storage_limit_exceeded");
        }
        if let Err(e) = handle.write_all(chunk) {
            return raise(if matches!(e.raw_os_error(), Some(28 | 69)) {
                "insufficient_disk"
            } else {
                "artifact_storage_failure"
            });
        }
        lock(&self.inner.guard).network_bytes += len;
        self.set_status(|s| s.downloaded_bytes = current + len)?;
        Ok(())
    }
    /// `_stage_bytes_for_status`: bytes on disk under the snapshot (or stage), capped at the status total.
    fn stage_bytes_for_status(&self) -> Result<u64, AcqError> {
        let status_total = self.status().total_bytes;
        let (stage, snapshot) = self.paths()?;
        let root = if snapshot.exists() { snapshot } else { stage };
        if !root.exists() {
            return Ok(0);
        }
        let mut total = 0u64;
        let mut pending = vec![root];
        while let Some(dir) = pending.pop() {
            let Ok(entries) = fs::read_dir(&dir) else {
                return Ok(0);
            };
            for entry in entries {
                let Ok(entry) = entry else { return Ok(0) };
                let path = entry.path();
                let Ok(meta) = fs::symlink_metadata(&path) else {
                    return Ok(0);
                };
                if meta.is_dir() {
                    pending.push(path);
                } else if meta.is_file() {
                    total += meta.len();
                    if total > status_total {
                        return Ok(status_total);
                    }
                }
            }
        }
        Ok(total)
    }
    fn verify_snapshot(&self, root: &Path, manifest: &Manifest) -> Result<(), AcqError> {
        secure_directory(root, false)?;
        let expected: BTreeMap<&str, &Record> = manifest
            .files
            .iter()
            .map(|r| (r.path.as_str(), r))
            .collect();
        let mut observed = BTreeSet::new();
        let mut pending = vec![root.to_owned()];
        while let Some(dir) = pending.pop() {
            for entry in fs::read_dir(&dir).map_err(|_| err("artifact_storage_unsafe"))? {
                let path = entry.map_err(|_| err("artifact_storage_unsafe"))?.path();
                let meta =
                    fs::symlink_metadata(&path).map_err(|_| err("artifact_storage_unsafe"))?;
                let listed_as_dir = meta.is_dir()
                    || (meta.file_type().is_symlink()
                        && fs::metadata(&path).is_ok_and(|m| m.is_dir()));
                if listed_as_dir {
                    if meta.file_type().is_symlink()
                        || !meta.is_dir()
                        || meta.uid() != euid()
                        || meta.mode() & 0o022 != 0
                    {
                        return raise("artifact_storage_unsafe");
                    }
                    pending.push(path);
                } else {
                    let relative = path
                        .strip_prefix(root)
                        .map_err(|_| err("artifact_storage_unsafe"))?
                        .to_string_lossy()
                        .into_owned();
                    if !expected.contains_key(relative.as_str()) {
                        return raise("artifact_storage_unsafe");
                    }
                    observed.insert(relative);
                }
            }
        }
        if observed.len() != expected.len()
            || observed.iter().any(|p| !expected.contains_key(p.as_str()))
        {
            return raise("source_incomplete");
        }
        for (path, record) in &expected {
            let mut candidate = root.to_owned();
            for part in path.split('/') {
                candidate.push(part);
            }
            if !digest_file(&candidate, record) {
                return raise("artifact_digest_mismatch");
            }
        }
        Ok(())
    }
    /// `cancel`: request bounded cancellation; safe partials remain resumable.
    pub fn cancel(&self) {
        self.inner.cancel.store(true, Ordering::SeqCst);
    }
    /// `close`: cancel and join an active attempt, then release the process lock.
    pub fn close(&self) -> Result<(), AcqError> {
        if lock(&self.inner.guard).closed {
            return Ok(());
        }
        self.inner.cancel.store(true, Ordering::SeqCst);
        // `thread.join(timeout=float(timeout) + 2.0)`.
        let patience = close_patience(self.inner.options.transport.timeout());
        if !self.wait(patience) {
            return raise("acquisition_close_timeout");
        }
        let handle = lock(&self.inner.guard).thread.take();
        if let Some(handle) = handle
            && handle.thread().id() != std::thread::current().id()
            && handle.join().is_err()
        {
            return raise("acquisition_failed");
        }
        let mut state = lock(&self.inner.guard);
        state.closed = true;
        state.active = false;
        let result = self.set_status_locked(&mut state, |s| {
            s.state = "closed".into();
            s.error_code = None;
        });
        state.lock = None;
        result.map(|_| ())
    }
    /// `_release_lock_for_test`: simulate process death in a recovery test.
    pub fn release_lock_for_test(&self) {
        lock(&self.inner.guard).lock = None;
    }
}
/// `int(text)` as Python reads a Content-Length value (surrounding blanks allowed, underscores between digits too).
fn python_int(text: &str) -> Option<i128> {
    let t = text.trim();
    let digits = t.strip_prefix(['+', '-']).unwrap_or(t);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    t.parse::<i128>().ok()
}
fn read_fill(reader: &mut dyn Read, buffer: &mut [u8]) -> std::io::Result<usize> {
    let mut filled = 0;
    while filled < buffer.len() {
        match reader.read(&mut buffer[filled..]) {
            Ok(0) => break,
            Ok(n) => filled += n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(filled)
}
fn stage_file_paths(stage: &Path, value: &str) -> Result<(PathBuf, PathBuf), AcqError> {
    let parts: Vec<&str> = value.split('/').collect();
    let mut parent = stage.to_owned();
    for part in &parts[..parts.len() - 1] {
        parent = secure_directory(&parent.join(part), true)?;
    }
    let name = parts[parts.len() - 1];
    Ok((parent.join(name), parent.join(format!("{name}.partial"))))
}
fn open_partial(path: &Path) -> Result<fs::File, AcqError> {
    let unsafe_file = || err("artifact_storage_unsafe");
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(O_NOFOLLOW)
        .open(path)
        .map_err(|_| unsafe_file())?;
    let meta = file.metadata().map_err(|_| unsafe_file())?;
    if !meta.is_file() || meta.nlink() != 1 || meta.uid() != euid() || meta.mode() & 0o077 != 0 {
        return Err(unsafe_file());
    }
    Ok(file)
}
fn acquire_lock(root: &Path) -> Result<fs::File, AcqError> {
    let unsafe_root = || err("acquisition_state_root_unsafe");
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(O_NOFOLLOW)
        .open(root.join("acquisition.lock"))
        .map_err(|_| unsafe_root())?;
    let meta = file.metadata().map_err(|_| unsafe_root())?;
    if !meta.is_file() || meta.nlink() != 1 || meta.uid() != euid() || meta.mode() & 0o077 != 0 {
        return Err(unsafe_root());
    }
    match file.try_lock() {
        Ok(()) => Ok(file),
        Err(fs::TryLockError::WouldBlock) => raise("acquisition_in_progress"),
        Err(fs::TryLockError::Error(_)) => Err(unsafe_root()),
    }
}
/// `_recover_status`: an interrupted transfer or a prepared source is settled to `failed`.
fn recover_status(path: &Path, model: &Model) -> Result<Status, AcqError> {
    let corrupt = || err("acquisition_state_corrupt");
    let meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Status::idle()),
        Err(_) => return Err(corrupt()),
    };
    if meta.file_type().is_symlink()
        || !meta.is_file()
        || meta.nlink() != 1
        || meta.uid() != euid()
        || meta.permissions().mode() & 0o077 != 0
        || meta.len() > max_status_bytes()
    {
        return Err(corrupt());
    }
    let text = fs::read_to_string(path).map_err(|_| corrupt())?;
    let value: Value = serde_json::from_str(&text).map_err(|_| corrupt())?;
    let mut status = validate_status(&value, model)?;
    if ACTIVE_STATES.contains(&status.state.as_str()) {
        status.state = "failed".into();
        status.error_code = Some("interrupted_transfer".into());
        atomic_json(path, &status.to_value(model))?;
    } else if status.state == "prepared" {
        status.state = "failed".into();
        status.error_code = Some("prepared_source_revalidation_required".into());
        atomic_json(path, &status.to_value(model))?;
    }
    Ok(status)
}

/// A validated download grant.
#[derive(Clone, Debug)]
pub struct Consent {
    pub max_download_bytes: u64,
    pub storage_limit_bytes: u64,
}
/// `_validate_consent`.
pub fn validate_consent(consent: &Value) -> Result<Consent, AcqError> {
    let invalid = || err("acquisition_consent_invalid");
    let object = consent.as_object().ok_or_else(invalid)?;
    let keys: BTreeSet<&str> = object.keys().map(String::as_str).collect();
    if keys
        != BTreeSet::from([
            "download_acknowledged",
            "max_download_bytes",
            "storage_limit_bytes",
        ])
    {
        return Err(invalid());
    }
    let positive = |v: &Value| {
        json_int(v)
            .filter(|n| *n > 0)
            .map(|n| u64::try_from(n).unwrap_or(u64::MAX))
    };
    match (
        object["download_acknowledged"] == Value::Bool(true),
        positive(&object["max_download_bytes"]),
        positive(&object["storage_limit_bytes"]),
    ) {
        (true, Some(max_download_bytes), Some(storage_limit_bytes)) => Ok(Consent {
            max_download_bytes,
            storage_limit_bytes,
        }),
        _ => Err(invalid()),
    }
}

// ------------------------------------------------------------------ HTTP transport

/// `_HTTPRepositoryTransport`: Web-PKI HTTPS to one pinned public repository (plain HTTP to loopback only
/// in tests).
pub struct HttpTransport {
    api_origin: String,
    download_origin: String,
    allow_test_http: bool,
    timeout: Duration,
    offline: bool,
    tls: Mutex<Option<Arc<rustls::ClientConfig>>>,
}
struct Url {
    https: bool,
    host: String,
    port: u16,
    /// `host` or `host:port` exactly as the URL spelled it (the `Host` header).
    authority: String,
    target: String,
}
fn parse_url(text: &str) -> Option<Url> {
    let (https, rest) = if let Some(rest) = text.strip_prefix("https://") {
        (true, rest)
    } else {
        (false, text.strip_prefix("http://")?)
    };
    let (authority, target) = match rest.find(['/', '?']) {
        Some(i) if rest.as_bytes()[i] == b'/' => (&rest[..i], rest[i..].to_owned()),
        Some(i) => (&rest[..i], format!("/{}", &rest[i..])),
        None => (rest, "/".to_owned()),
    };
    let target = target.split('#').next().unwrap_or("/").to_owned();
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) if !h.ends_with(']') || h.starts_with('[') => (h, p.parse::<u16>().ok()?),
        _ => (authority, if https { 443 } else { 80 }),
    };
    if host.is_empty() || authority.contains('@') {
        return None;
    }
    Some(Url {
        https,
        host: host.trim_matches(['[', ']']).to_owned(),
        port,
        authority: authority.to_owned(),
        target,
    })
}
fn loopback_host(host: &str) -> bool {
    matches!(host, "127.0.0.1" | "::1" | "localhost")
}
/// `urllib.parse.quote(text, safe=...)`.
fn quote(text: &str, safe: &str) -> String {
    let mut out = String::new();
    for b in text.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) || safe.as_bytes().contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}
fn system_tls() -> Arc<rustls::ClientConfig> {
    let mut roots = rustls::RootCertStore::empty();
    roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map(|b| b.with_root_certificates(roots).with_no_client_auth());
    Arc::new(config.unwrap_or_else(|_| {
        rustls::ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map(|b| {
            b.with_root_certificates(rustls::RootCertStore::empty())
                .with_no_client_auth()
        })
        .expect("tls13 is always available")
    }))
}
/// A response whose head is parsed and whose body streams.
struct HttpResponse {
    status: u16,
    headers: Vec<(String, String)>,
    body: Box<dyn Read + Send>,
}
impl Read for HttpResponse {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        self.body.read(buffer)
    }
}
impl FileResponse for HttpResponse {
    fn status(&self) -> u16 {
        self.status
    }
    fn header(&self, name: &str) -> Option<String> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.clone())
    }
}
/// A body that ends after `remaining` bytes.
struct Limited<R> {
    inner: R,
    remaining: u64,
}
impl<R: Read> Read for Limited<R> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        if self.remaining == 0 {
            return Ok(0);
        }
        let cap = usize::try_from(self.remaining)
            .unwrap_or(usize::MAX)
            .min(buffer.len());
        let n = self.inner.read(&mut buffer[..cap])?;
        self.remaining -= n as u64;
        Ok(n)
    }
}
/// `Transfer-Encoding: chunked` decoding.
struct Chunked<R> {
    inner: std::io::BufReader<R>,
    left: u64,
    done: bool,
}
impl<R: Read> Chunked<R> {
    fn line(&mut self) -> std::io::Result<String> {
        let mut line = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            if self.inner.read(&mut byte)? == 0 {
                return Err(std::io::ErrorKind::UnexpectedEof.into());
            }
            if byte[0] == b'\n' {
                break;
            }
            line.push(byte[0]);
            if line.len() > MAX_REDIRECT_HEAD {
                return Err(std::io::ErrorKind::InvalidData.into());
            }
        }
        Ok(String::from_utf8_lossy(&line)
            .trim_end_matches('\r')
            .to_owned())
    }
}
impl<R: Read> Read for Chunked<R> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        if self.done {
            return Ok(0);
        }
        if self.left == 0 {
            let size_line = self.line()?;
            let size_text = size_line.split(';').next().unwrap_or("").trim();
            let size = u64::from_str_radix(size_text, 16)
                .map_err(|_| std::io::Error::from(std::io::ErrorKind::InvalidData))?;
            if size == 0 {
                self.done = true;
                return Ok(0);
            }
            self.left = size;
        }
        let cap = usize::try_from(self.left)
            .unwrap_or(usize::MAX)
            .min(buffer.len());
        let n = self.inner.read(&mut buffer[..cap])?;
        if n == 0 {
            return Err(std::io::ErrorKind::UnexpectedEof.into());
        }
        self.left -= n as u64;
        if self.left == 0 {
            let end = self.line()?;
            if !end.is_empty() {
                return Err(std::io::ErrorKind::InvalidData.into());
            }
        }
        Ok(n)
    }
}

impl HttpTransport {
    /// The production composition: Web-PKI HTTPS to Hugging Face. `offline` refuses that origin before any socket.
    pub fn hugging_face(offline: bool) -> Self {
        Self {
            api_origin: HF_ORIGIN.into(),
            download_origin: HF_ORIGIN.into(),
            allow_test_http: false,
            timeout: default_timeout(),
            offline,
            tls: Mutex::new(None),
        }
    }
    /// A loopback fixture origin (`allow_test_http`): the oracle's test-only constructor seam.
    pub fn loopback_for_tests(origin: &str) -> Self {
        Self {
            api_origin: origin.trim_end_matches('/').to_owned(),
            download_origin: origin.trim_end_matches('/').to_owned(),
            allow_test_http: true,
            timeout: default_timeout(),
            offline: false,
            tls: Mutex::new(None),
        }
    }
    /// The oracle's `ssl_context` seam: trust exactly these roots (a loopback TLS fixture), not the system store.
    pub fn with_tls_roots_pem(self, ca_pem: &[u8]) -> Self {
        use rustls_pki_types::{CertificateDer, pem::PemObject};
        let mut roots = rustls::RootCertStore::empty();
        roots.add_parsable_certificates(
            CertificateDer::pem_slice_iter(ca_pem).filter_map(Result::ok),
        );
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        if let Ok(builder) = rustls::ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
        {
            *lock(&self.tls) = Some(Arc::new(
                builder.with_root_certificates(roots).with_no_client_auth(),
            ));
        }
        self
    }
    pub fn with_offline(mut self, offline: bool) -> Self {
        self.offline = offline;
        self
    }
    fn scheme_allowed(&self, url: &Url) -> bool {
        url.https || (self.allow_test_http && loopback_host(&url.host))
    }
    /// `_open`: one GET, following redirects as urllib does; HTTP errors map to closed codes.
    fn open(&self, first_url: &str, headers: &[(&str, String)]) -> Result<HttpResponse, AcqError> {
        let mut url_text = first_url.to_owned();
        let mut visited: BTreeMap<String, u32> = BTreeMap::new();
        loop {
            if self.offline && url_text.starts_with(&format!("{HF_ORIGIN}/")) {
                return raise(OFFLINE_ERROR_CODE);
            }
            let url = parse_url(&url_text).ok_or_else(|| err("network_error"))?;
            let response = self.exchange(&url, headers)?;
            let code = response.status;
            if matches!(code, 301 | 302 | 303 | 307 | 308) {
                let location = response
                    .header("Location")
                    .or_else(|| response.header("uri"));
                if let Some(location) = location {
                    let mut new_url = join_url(&url_text, location.trim());
                    if let Some((scheme, _)) = new_url.split_once("://")
                        && !["http", "https", "ftp"].contains(&scheme)
                    {
                        return raise("network_error");
                    }
                    new_url = new_url.replace(' ', "%20");
                    // `_SchemeRedirectHandler.redirect_request`.
                    let allowed = parse_url(&new_url).is_some_and(|u| self.scheme_allowed(&u));
                    if !allowed {
                        return raise("https_redirect_required");
                    }
                    let seen = visited.get(&new_url).copied().unwrap_or(0);
                    if seen >= REDIRECT_REPEATS || visited.len() >= REDIRECT_LIMIT {
                        return raise("network_error");
                    }
                    visited.insert(new_url.clone(), seen + 1);
                    url_text = new_url;
                    continue;
                }
            }
            if !(200..300).contains(&code) {
                return raise(match code {
                    401 | 403 => "source_expired",
                    404 => "source_unavailable",
                    416 => "range_unsatisfiable",
                    _ => "network_error",
                });
            }
            if !self.scheme_allowed(&url) {
                return raise("https_redirect_required");
            }
            return Ok(response);
        }
    }
    fn exchange(&self, url: &Url, headers: &[(&str, String)]) -> Result<HttpResponse, AcqError> {
        let network = || err("network_error");
        let addresses: Vec<_> = (url.host.as_str(), url.port)
            .to_socket_addrs()
            .map_err(|_| network())?
            .collect();
        let mut tcp = None;
        for address in &addresses {
            if let Ok(stream) = TcpStream::connect_timeout(address, self.timeout) {
                tcp = Some(stream);
                break;
            }
        }
        let tcp = tcp.ok_or_else(network)?;
        tcp.set_read_timeout(Some(self.timeout))
            .and_then(|()| tcp.set_write_timeout(Some(self.timeout)))
            .map_err(|_| network())?;
        let mut request = format!("GET {} HTTP/1.1\r\nHost: {}\r\n", url.target, url.authority);
        for (name, value) in headers {
            request.push_str(&format!("{name}: {value}\r\n"));
        }
        request.push_str("Connection: close\r\n\r\n");
        let mut stream: Box<dyn ReadWrite> = if url.https {
            let config = lock(&self.tls).get_or_insert_with(system_tls).clone();
            let name =
                rustls::pki_types::ServerName::try_from(url.host.clone()).map_err(|_| network())?;
            let connection = rustls::ClientConnection::new(config, name).map_err(|_| network())?;
            Box::new(rustls::StreamOwned::new(connection, tcp))
        } else {
            Box::new(tcp)
        };
        stream
            .write_all(request.as_bytes())
            .and_then(|()| stream.flush())
            .map_err(|_| network())?;
        // Head: byte by byte up to the blank line, so the body stays unread.
        let mut head = Vec::new();
        let mut byte = [0u8; 1];
        while !head.ends_with(b"\r\n\r\n") {
            match stream.read(&mut byte) {
                Ok(1) => head.push(byte[0]),
                _ => return Err(network()),
            }
            if head.len() > MAX_REDIRECT_HEAD {
                return Err(network());
            }
        }
        let text = String::from_utf8_lossy(&head).into_owned();
        let mut lines = text.split("\r\n");
        let status = lines
            .next()
            .and_then(|l| l.split(' ').nth(1))
            .and_then(|s| s.parse::<u16>().ok())
            .ok_or_else(network)?;
        let mut parsed = Vec::new();
        for line in lines {
            if let Some((name, value)) = line.split_once(':') {
                parsed.push((name.trim().to_owned(), value.trim().to_owned()));
            }
        }
        let find = |name: &str| {
            parsed
                .iter()
                .find(|(k, _)| k.eq_ignore_ascii_case(name))
                .map(|(_, v)| v.clone())
        };
        let body: Box<dyn Read + Send> = if find("Transfer-Encoding")
            .is_some_and(|v| v.eq_ignore_ascii_case("chunked"))
        {
            Box::new(Chunked {
                inner: std::io::BufReader::new(stream),
                left: 0,
                done: false,
            })
        } else if let Some(length) = find("Content-Length").and_then(|v| v.parse::<u64>().ok()) {
            Box::new(Limited {
                inner: stream,
                remaining: length,
            })
        } else {
            Box::new(stream)
        };
        Ok(HttpResponse {
            status,
            headers: parsed,
            body,
        })
    }
}
trait ReadWrite: Read + Write + Send {}
impl<T: Read + Write + Send> ReadWrite for T {}

/// `urllib.parse.urljoin(base, reference)` for the cases a redirect produces.
fn join_url(base: &str, reference: &str) -> String {
    if reference.contains("://") {
        return reference.to_owned();
    }
    let Some(parsed) = parse_url(base) else {
        return reference.to_owned();
    };
    let origin = format!(
        "{}://{}",
        if parsed.https { "https" } else { "http" },
        parsed.authority
    );
    if let Some(rest) = reference.strip_prefix("//") {
        return format!("{}://{rest}", if parsed.https { "https" } else { "http" });
    }
    if reference.starts_with('/') {
        return format!("{origin}{reference}");
    }
    if reference.starts_with('?') {
        let path = parsed.target.split('?').next().unwrap_or("/");
        return format!("{origin}{path}{reference}");
    }
    let path = parsed.target.split('?').next().unwrap_or("/");
    let directory = &path[..=path.rfind('/').unwrap_or(0)];
    format!("{origin}{directory}{reference}")
}

impl Transport for HttpTransport {
    fn timeout(&self) -> Duration {
        self.timeout
    }
    fn resolve(&self, model: &Model) -> Result<Manifest, AcqError> {
        let path = format!(
            "/api/models/{}/revision/{}?blobs=true",
            quote(model.model_id, "/"),
            quote(model.resolved_commit, "")
        );
        let mut response = self.open(
            &format!("{}{path}", self.api_origin),
            &[
                ("Accept", "application/json".into()),
                ("Accept-Encoding", "identity".into()),
                ("User-Agent", crate::join_client::USER_AGENT.into()),
            ],
        )?;
        let invalid = || err("model_metadata_invalid");
        if response
            .header("Content-Encoding")
            .is_some_and(|v| v != "identity")
        {
            return Err(invalid());
        }
        if let Some(declared) = response.header("Content-Length") {
            let length = python_int(&declared).ok_or_else(invalid)?;
            if length < 0 || length > i128::from(max_metadata_bytes()) {
                return Err(invalid());
            }
        }
        let mut body = Vec::new();
        let cap = max_metadata_bytes() + 1;
        std::io::Read::take(&mut response, cap)
            .read_to_end(&mut body)
            .map_err(|_| err("network_error"))?;
        if body.len() as u64 > max_metadata_bytes() {
            return Err(invalid());
        }
        let document: Value = serde_json::from_slice(&body).map_err(|_| invalid())?;
        manifest_from_metadata(&document, model)
    }
    fn open_file(
        &self,
        model: &Model,
        record: &Record,
        offset: u64,
    ) -> Result<Box<dyn FileResponse>, AcqError> {
        let path = format!(
            "/{}/resolve/{}/{}?download=true",
            quote(model.model_id, "/"),
            quote(model.resolved_commit, ""),
            quote(&record.path, "/")
        );
        let mut headers = vec![
            ("Accept", "application/octet-stream".to_owned()),
            ("Accept-Encoding", "identity".to_owned()),
            ("User-Agent", crate::join_client::USER_AGENT.to_owned()),
        ];
        if offset > 0 {
            headers.push(("Range", format!("bytes={offset}-")));
        }
        Ok(Box::new(self.open(
            &format!("{}{path}", self.download_origin),
            &headers,
        )?))
    }
}

/// Convenience for hosts: the status `Value` of a possibly absent acquisition.
pub fn status_or_idle(acquisition: Option<&Acquisition>, model: &Model) -> Value {
    acquisition.map_or_else(|| Status::idle().to_value(model), Acquisition::status_value)
}
/// An empty JSON object (used by hosts that report nothing for `resource_observation`).
pub fn empty_object() -> Value {
    Value::Object(Map::new())
}

/// The closed codes a route can see from [`Acquisition::start`] / [`Acquisition::reverify_closed_snapshot`].
pub fn closed_code(code: &str) -> &'static str {
    const CODES: [&str; 8] = [
        "acquisition_consent_invalid",
        "acquisition_closed",
        "acquisition_in_progress",
        "source_already_prepared",
        "acquisition_start_failed",
        "source_not_on_disk",
        "acquisition_state_corrupt",
        "acquisition_state_root_unsafe",
    ];
    CODES
        .iter()
        .find(|known| **known == code)
        .copied()
        .unwrap_or("action_not_completed")
}
