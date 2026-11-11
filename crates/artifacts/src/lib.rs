// SPDX-License-Identifier: AGPL-3.0-only
//! Offline content-addressed cache. It stores bytes, never execution authority.
//! Unix private-root implementation; no network fallback. `store` repairs a damaged entry and
//! evicts least-recently-used unpinned entries over the cap exactly as v0.1.4's
//! `AssignmentArtifactCache`; every hash check is kept and `verify`/`resolve_offline` stay
//! read-only (and serve a hard-linked object, as v0.1.4's offline start does).
//!
//! v0.2 upgrades v0.1.4 installs in place, so it opens every on-disk state v0.1.4 writes: a
//! symlink ABOVE the cache root, a root or `objects/` directory with any mode (the root is
//! chmodded to 0700 exactly as v0.1.4 does), missing parents (created), staging trees whose
//! intermediate directories have the default mode, hard-linked sources, index and lock files,
//! and the index entries v0.1.4 writes (its entry shape is kept verbatim, including any extra
//! field). Symlinks INSIDE the root are refused, as is a malformed object id (v0.1.4 would
//! unlink outside `objects/`). A second lock holder BLOCKS, unbounded, as v0.1.4 does.
use budgets::Bytes;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};
pub mod acquisition;
mod materialize;
mod startup;
mod unique;
pub use startup::{StartupRefusal, Unavailable};
static NEXT: AtomicU64 = AtomicU64::new(0);
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactKey {
    pub model_revision: String,
    pub manifest_digest: String,
    pub format: String,
    pub quantization: String,
    pub tensor_digest: String,
    pub size_bytes: u64,
}
#[derive(Debug)]
pub struct Error {
    pub code: &'static str,
    pub io: Option<std::io::ErrorKind>,
    pub cleanup_io: Option<std::io::ErrorKind>,
}
pub type Result<T> = std::result::Result<T, Error>;
impl Error {
    fn new(code: &'static str) -> Self {
        Self {
            code,
            io: None,
            cleanup_io: None,
        }
    }
    fn io(code: &'static str, e: std::io::Error) -> Self {
        Self {
            code,
            io: Some(e.kind()),
            cleanup_io: None,
        }
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}:{:?}; cleanup={:?}",
            self.code, self.io, self.cleanup_io
        )
    }
}
impl std::error::Error for Error {}
#[derive(Debug)]
pub struct Cache {
    root: PathBuf,
    cap: Bytes,
    index_cap: Bytes,
    chunk: Bytes,
}
/// What `store` did, with v0.1.4's names (`mycelium_assignment_cache.py`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoreOutcome {
    /// New entry, bytes copied in.
    Populated,
    /// Entry and bytes already verified; only the access time was renewed.
    Reused,
    /// A damaged entry or foreign file was quarantined and the bytes copied in again.
    Repaired,
}
impl StoreOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Populated => "populated",
            Self::Reused => "reused",
            Self::Repaired => "repaired",
        }
    }
}
fn now_ns() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_nanos()).unwrap_or(u64::MAX))
}
/// Verified open file, positioned at zero. No path re-open is necessary.
/// The cache will not replace it. L8 must still hash at load boundaries; this is
/// not protection against a hostile process with the same Unix identity.
#[derive(Debug)]
pub struct VerifiedArtifact {
    pub file: File,
    key: ArtifactKey,
}
impl VerifiedArtifact {
    pub fn key(&self) -> &ArtifactKey {
        &self.key
    }
}
/// An index entry exactly as v0.1.4 treats it: only the size, the access time and the pins are
/// read; every other field (`key`, `relative_path`, anything a hand edit added) is carried
/// through unchanged and never used to locate or authorise bytes.
#[derive(Serialize, Deserialize)]
struct Entry {
    size_bytes: u64,
    /// Absent in a hand-edited entry; v0.1.4 only fails on it at the next eviction, if it is
    /// still absent then. It sorts first here.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    last_access_unix_ns: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pins: Option<Vec<String>>,
    #[serde(flatten)]
    rest: Map<String, Value>,
}
impl Entry {
    fn new(key: &ArtifactKey, id: &str, pins: Vec<String>) -> Result<Self> {
        let mut rest = Map::new();
        rest.insert(
            "key".into(),
            serde_json::to_value(key).map_err(|_| Error::new("artifact_index_invalid"))?,
        );
        rest.insert(
            "relative_path".into(),
            format!("objects/{}", &id[7..]).into(),
        );
        Ok(Self {
            size_bytes: key.size_bytes,
            last_access_unix_ns: Some(now_ns()),
            pins: Some(pins),
            rest,
        })
    }
    fn pins(&self) -> &[String] {
        self.pins.as_deref().unwrap_or(&[])
    }
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Index {
    protocol: String,
    max_bytes: u64,
    entries: BTreeMap<String, Entry>,
}
fn sha_ref(s: &str) -> bool {
    s.len() == 71
        && s.starts_with("sha256:")
        && s[7..]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn encode<T: Serialize>(v: &T) -> Result<Vec<u8>> {
    let value = serde_json::to_value(v).map_err(|_| Error::new("artifact_index_invalid"))?;
    let mut bytes = serde_json::to_vec(&value).map_err(|_| Error::new("artifact_index_invalid"))?;
    bytes.push(b'\n');
    Ok(bytes)
}
impl ArtifactKey {
    pub fn validate(&self) -> Result<()> {
        if self.size_bytes == 0
            || !sha_ref(&self.manifest_digest)
            || !sha_ref(&self.tensor_digest)
            || [&self.model_revision, &self.format, &self.quantization]
                .iter()
                .any(|s| s.is_empty() || s.chars().count() > 256)
        {
            return Err(Error::new("artifact_key_invalid"));
        }
        Ok(())
    }
    pub fn object_id(&self) -> Result<String> {
        self.validate()?;
        Ok(format!("sha256:{:x}", Sha256::digest(encode(self)?)))
    }
}
/// A real directory (never a symlink) at exactly this path. Ancestors may be links: v0.1.4
/// follows a symlinked HOME or volume, and the cache root is canonicalised once at `open`.
fn directory_not_link(path: &Path) -> Result<()> {
    let m = fs::symlink_metadata(path).map_err(|e| Error::io("artifact_unsafe", e))?;
    if !m.is_dir() || m.file_type().is_symlink() {
        return Err(Error::new("artifact_unsafe"));
    }
    Ok(())
}
/// A directory this crate creates: private. Used for staging trees it builds itself.
fn safe_directory(path: &Path) -> Result<()> {
    directory_not_link(path)?;
    let m = fs::symlink_metadata(path).map_err(|e| Error::io("artifact_unsafe", e))?;
    if m.mode() & 0o077 != 0 {
        return Err(Error::new("artifact_unsafe"));
    }
    Ok(())
}
fn private_directory(path: &Path) -> Result<()> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => safe_directory(path),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => safe_directory(path),
        Err(e) => Err(Error::io("artifact_directory_failed", e)),
    }
}
/// `objects/` and `quarantine/`: created 0700, but a directory that exists keeps its mode (v0.1.4
/// never touches it; the root is 0700, so nothing inside is reachable by other users).
fn ensure_directory(path: &Path) -> Result<()> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => directory_not_link(path),
        Err(e) => Err(Error::io("artifact_directory_failed", e)),
    }
}
/// v0.1.4 `__init__`: refuse a symlink root, create missing parents (default mode) and the
/// root (0700), chmod the root to 0700, then canonicalise it ONCE so later checks only need to
/// look at components inside the root.
fn prepare_root(root: &Path) -> Result<PathBuf> {
    match fs::symlink_metadata(root) {
        Ok(m) if m.is_dir() => {}
        Ok(_) => return Err(Error::new("artifact_unsafe")),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            if let Some(parent) = root.parent() {
                fs::DirBuilder::new()
                    .recursive(true)
                    .create(parent)
                    .map_err(|e| Error::io("artifact_directory_failed", e))?;
            }
            match fs::DirBuilder::new().mode(0o700).create(root) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                    directory_not_link(root)?;
                }
                Err(e) => return Err(Error::io("artifact_directory_failed", e)),
            }
        }
        Err(e) => return Err(Error::io("artifact_directory_failed", e)),
    }
    fs::set_permissions(root, fs::Permissions::from_mode(0o700))
        .map_err(|e| Error::io("artifact_directory_failed", e))?;
    fs::canonicalize(root).map_err(|e| Error::io("artifact_unsafe", e))
}
/// An open regular file, never a symlink at the final component. `links` accepts a file with
/// more than one name: v0.1.4 accepts those everywhere (it never creates one), and for a
/// source, the index and the lock file the bytes are copied or validated, not trusted by name.
fn open_regular(path: &Path, links: bool) -> Result<File> {
    let before = fs::symlink_metadata(path).map_err(|e| {
        Error::io(
            if e.kind() == std::io::ErrorKind::NotFound {
                "artifact_missing"
            } else {
                "artifact_io"
            },
            e,
        )
    })?;
    if !before.is_file() || before.file_type().is_symlink() || (!links && before.nlink() != 1) {
        return Err(Error::new("artifact_unsafe"));
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|e| Error::io("artifact_unsafe", e))?;
    let after = file.metadata().map_err(|e| Error::io("artifact_io", e))?;
    if !after.is_file()
        || (before.dev(), before.ino()) != (after.dev(), after.ino())
        || (!links && after.nlink() != 1)
    {
        return Err(Error::new("artifact_unsafe"));
    }
    Ok(file)
}
fn stamp(m: &fs::Metadata) -> (u64, u64, u64, i64, i64, i64, i64) {
    (
        m.dev(),
        m.ino(),
        m.len(),
        m.mtime(),
        m.mtime_nsec(),
        m.ctime(),
        m.ctime_nsec(),
    )
}
impl Cache {
    /// All limits are caller/L1-owned. A cache cap is not a member execution grant.
    pub fn open(root: &Path, cap: Bytes, index_cap: Bytes, chunk: Bytes) -> Result<Self> {
        if !root.is_absolute()
            || cap == Bytes::ZERO
            || index_cap == Bytes::ZERO
            || chunk == Bytes::ZERO
            || chunk.get() > usize::MAX as u64
        {
            return Err(Error::new("artifact_policy_invalid"));
        }
        let root = prepare_root(root)?;
        ensure_directory(&root.join("objects"))?;
        ensure_directory(&root.join("quarantine"))?;
        let c = Self {
            root,
            cap,
            index_cap,
            chunk,
        };
        c.with_lock(|| {
            if c.root
                .join("index.v1.json")
                .try_exists()
                .map_err(|e| Error::io("artifact_io", e))?
            {
                c.read_index()?;
            } else {
                c.write_index(&Index {
                    protocol: "mycelium.assignment_artifact_cache.v1".into(),
                    max_bytes: cap.get(),
                    entries: BTreeMap::new(),
                })?;
            }
            Ok(())
        })?;
        Ok(c)
    }
    fn object_path(&self, id: &str) -> PathBuf {
        self.root.join("objects").join(&id[7..])
    }
    /// v0.1.4 `_locked`: `flock(LOCK_EX)` with no timeout. A second holder (another process, or
    /// another thread with its own handle) blocks until the first operation finishes.
    /// Characterised as v0.1.4 behaviour; a bounded wait is a V03 change, not made here.
    fn with_lock<T>(&self, op: impl FnOnce() -> Result<T>) -> Result<T> {
        directory_not_link(&self.root)?;
        directory_not_link(&self.root.join("objects"))?;
        directory_not_link(&self.root.join("quarantine"))?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(self.root.join(".cache.lock"))
            .map_err(|e| Error::io("artifact_lock_failed", e))?;
        let m = lock
            .metadata()
            .map_err(|e| Error::io("artifact_lock_failed", e))?;
        if !m.is_file() {
            return Err(Error::new("artifact_unsafe"));
        }
        // A signal that interrupts the wait restarts it (EINTR is not a failure); anything else
        // is reported.
        loop {
            match lock.lock() {
                Ok(()) => break,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(Error::io("artifact_lock_failed", e)),
            }
        }
        let result = op();
        let unlocked = lock.unlock();
        match (result, unlocked) {
            (Ok(v), Ok(())) => Ok(v),
            (Err(e), Ok(())) => Err(e),
            (Ok(_), Err(e)) => Err(Error::io("artifact_unlock_failed", e)),
            (Err(mut original), Err(e)) => {
                original.cleanup_io = Some(e.kind());
                Err(original)
            }
        }
    }
    fn read_index(&self) -> Result<Index> {
        let file = open_regular(&self.root.join("index.v1.json"), true)?;
        if file
            .metadata()
            .map_err(|e| Error::io("artifact_io", e))?
            .len()
            > self.index_cap.get()
        {
            return Err(Error::new("artifact_index_too_large"));
        }
        let bound = self
            .index_cap
            .get()
            .checked_add(1)
            .ok_or_else(|| Error::new("artifact_policy_invalid"))?;
        let mut bytes = Vec::new();
        file.take(bound)
            .read_to_end(&mut bytes)
            .map_err(|e| Error::io("artifact_io", e))?;
        if bytes.len() as u64 > self.index_cap.get() {
            return Err(Error::new("artifact_index_too_large"));
        }
        let value = unique::parse(&bytes).map_err(|_| Error::new("artifact_index_invalid"))?;
        let index: Index =
            serde_json::from_value(value).map_err(|_| Error::new("artifact_index_invalid"))?;
        if index.protocol != "mycelium.assignment_artifact_cache.v1"
            || index.max_bytes != self.cap.get()
        {
            return Err(Error::new("artifact_index_invalid"));
        }
        // v0.1.4 reads only size, access time and pins of an entry and locates bytes from the
        // KEY IT IS ASKED FOR, never from the entry; it does not refuse an over-cap index nor an
        // unindexed file. The one check kept is the id shape: it names a file under `objects/`,
        // and a crafted id would make v0.1.4's eviction unlink a path outside the cache.
        for id in index.entries.keys() {
            if !sha_ref(id) {
                return Err(Error::new("artifact_index_invalid"));
            }
        }
        Ok(index)
    }
    fn temporary(&self, parent: &Path) -> PathBuf {
        parent.join(format!(
            ".l7-{}-{}.partial",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ))
    }
    fn cleanup(&self, path: &Path, result: Result<()>) -> Result<()> {
        match fs::remove_file(path) {
            Ok(()) => result,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => result,
            Err(e) => match result {
                Ok(()) => Err(Error::io("artifact_cleanup_failed", e)),
                Err(mut original) => {
                    original.cleanup_io = Some(e.kind());
                    Err(original)
                }
            },
        }
    }
    fn sync_dir(&self, path: &Path) -> Result<()> {
        File::open(path)
            .and_then(|f| f.sync_all())
            .map_err(|e| Error::io("artifact_sync_failed", e))
    }
    fn write_index(&self, index: &Index) -> Result<()> {
        let bytes = encode(index)?;
        if bytes.len() as u64 > self.index_cap.get() {
            return Err(Error::new("artifact_index_too_large"));
        }
        let tmp = self.temporary(&self.root);
        // Do not run cleanup until create_new proved we own this inode.
        let mut out = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)
            .map_err(|e| Error::io("artifact_index_write_failed", e))?;
        let result = (|| {
            out.write_all(&bytes)
                .and_then(|()| out.sync_all())
                .map_err(|e| Error::io("artifact_index_write_failed", e))?;
            fs::rename(&tmp, self.root.join("index.v1.json"))
                .map_err(|e| Error::io("artifact_index_commit_failed", e))?;
            self.sync_dir(&self.root)
        })();
        self.cleanup(&tmp, result)
    }
    fn verify_file(&self, key: &ArtifactKey, path: &Path) -> Result<File> {
        self.verify_with(key, path, false)
    }
    /// A source file or an offline-read object: v0.1.4 accepts one with several names (`st_nlink >= 1`).
    fn verify_linked(&self, key: &ArtifactKey, path: &Path) -> Result<File> {
        self.verify_with(key, path, true)
    }
    fn verify_with(&self, key: &ArtifactKey, path: &Path, links: bool) -> Result<File> {
        let mut file = open_regular(path, links)?;
        let before = file.metadata().map_err(|e| Error::io("artifact_io", e))?;
        if before.len() != key.size_bytes {
            return Err(Error::new("artifact_corrupt"));
        }
        let mut buffer = Vec::new();
        let size = usize::try_from(self.chunk.get().min(key.size_bytes))
            .map_err(|_| Error::new("artifact_policy_invalid"))?;
        buffer
            .try_reserve_exact(size)
            .map_err(|_| Error::new("artifact_buffer_capacity"))?;
        buffer.resize(size, 0);
        let mut hash = Sha256::new();
        let mut read = 0u64;
        loop {
            let n = file
                .read(&mut buffer)
                .map_err(|e| Error::io("artifact_io", e))?;
            if n == 0 {
                break;
            }
            read = read
                .checked_add(n as u64)
                .ok_or_else(|| Error::new("artifact_corrupt"))?;
            if read > key.size_bytes {
                return Err(Error::new("artifact_corrupt"));
            }
            hash.update(&buffer[..n]);
        }
        let after = file.metadata().map_err(|e| Error::io("artifact_io", e))?;
        let current = fs::symlink_metadata(path).map_err(|e| Error::io("artifact_io", e))?;
        if stamp(&before) != stamp(&after)
            || (current.dev(), current.ino()) != (after.dev(), after.ino())
            || current.file_type().is_symlink()
            || read != key.size_bytes
            || format!("sha256:{:x}", hash.finalize()) != key.tensor_digest
        {
            return Err(Error::new("artifact_corrupt"));
        }
        file.seek(SeekFrom::Start(0))
            .map_err(|e| Error::io("artifact_io", e))?;
        Ok(file)
    }
    /// Read-only: hold the hash-verified bytes of an indexed object. This is the offline startup
    /// read (`resolve_offline`), so it serves whatever v0.1.4's offline start serves, and v0.1.4
    /// stages an object that has a second name (`st_nlink >= 1`; it never creates one, a user
    /// can). The hash, size, type and no-symlink checks are unchanged; `store` and
    /// `materialize_assignment` still converge such an object to a private inode.
    pub fn verify(&self, key: &ArtifactKey) -> Result<VerifiedArtifact> {
        let id = key.object_id()?;
        self.with_lock(|| {
            let index = self.read_index()?;
            if !index.entries.contains_key(&id) {
                return Err(Error::new("artifact_missing"));
            }
            let file = self.verify_linked(key, &self.object_path(&id))?;
            Ok(VerifiedArtifact {
                file,
                key: key.clone(),
            })
        })
    }
    /// v0.1.4 `store`: verify the source against the key, then under the cache lock reuse a
    /// verified entry, or quarantine whatever damaged/foreign file sits at the object's path and
    /// copy the bytes in again (keeping the entry's pins), then evict least-recently-used
    /// unpinned entries until the cap holds. A damaged entry is never trusted: it is re-acquired.
    pub fn store(&self, key: &ArtifactKey, source: &Path) -> Result<StoreOutcome> {
        let id = key.object_id()?;
        let mut input = self.verify_linked(key, source)?;
        self.with_lock(|| {
            let mut index = self.read_index()?;
            let target = self.object_path(&id);
            let existing = index.entries.get(&id);
            if existing.is_some() && self.verify_file(key, &target).is_ok() {
                if let Some(entry) = index.entries.get_mut(&id) {
                    entry.last_access_unix_ns = Some(now_ns());
                }
                self.write_index(&index)?;
                return Ok(StoreOutcome::Reused);
            }
            // `Path.exists()` follows links: a dangling link at the path is neither "repaired"
            // nor quarantined, it is simply replaced by the rename below.
            let present = fs::metadata(&target).is_ok();
            let repaired = existing.is_some() || present;
            let mut pins: Vec<String> = existing.map(|e| e.pins().to_vec()).unwrap_or_default();
            if present {
                let quarantine =
                    self.root
                        .join("quarantine")
                        .join(format!("{}.{}", &id[7..], now_ns()));
                fs::rename(&target, &quarantine)
                    .map_err(|e| Error::io("artifact_quarantine_failed", e))?;
                self.sync_dir(&self.root.join("objects"))?;
            }
            self.populate(key, &mut input, &target)?;
            pins.sort();
            index
                .entries
                .insert(id.clone(), Entry::new(key, &id, pins)?);
            self.write_index(&index)?;
            self.evict_locked(&mut index)?;
            Ok(if repaired {
                StoreOutcome::Repaired
            } else {
                StoreOutcome::Populated
            })
        })
    }
    /// Copy `input` (already verified against `key`) into a fresh private inode next to
    /// `target`, verify the copy, make it read-only and rename it over `target`.
    fn populate(&self, key: &ArtifactKey, input: &mut File, target: &Path) -> Result<()> {
        let tmp = self.temporary(&self.root.join("objects"));
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)
            .map_err(|e| Error::io("artifact_population_failed", e))?;
        let result = (|| {
            let bound = key
                .size_bytes
                .checked_add(1)
                .ok_or_else(|| Error::new("artifact_key_invalid"))?;
            let count = std::io::copy(&mut Read::by_ref(input).take(bound), &mut output)
                .map_err(|e| Error::io("artifact_population_failed", e))?;
            if count != key.size_bytes {
                return Err(Error::new("artifact_corrupt"));
            }
            output
                .sync_all()
                .map_err(|e| Error::io("artifact_sync_failed", e))?;
            self.verify_file(key, &tmp)?;
            fs::set_permissions(&tmp, fs::Permissions::from_mode(0o400))
                .map_err(|e| Error::io("artifact_population_failed", e))?;
            fs::rename(&tmp, target).map_err(|e| Error::io("artifact_population_failed", e))?;
            self.sync_dir(&self.root.join("objects"))
        })();
        self.cleanup(&tmp, result)
    }
    /// Held read-only file of an indexed object for staging. v0.1.4 accepts an object that has
    /// a second name (it never makes one); this crate treats that as damage and converges: the
    /// verified bytes are copied to a fresh private inode that replaces the linked name (the
    /// other name is left alone). Anything else wrong with the object is an error, as before.
    fn staged_source(&self, key: &ArtifactKey, target: &Path) -> Result<File> {
        match self.verify_file(key, target) {
            Err(e) if e.code == "artifact_unsafe" => {
                let linked = fs::symlink_metadata(target)
                    .map(|m| m.is_file() && !m.file_type().is_symlink() && m.nlink() > 1)
                    .unwrap_or(false);
                if !linked {
                    return Err(e);
                }
                let mut bytes = self.verify_with(key, target, true)?;
                self.populate(key, &mut bytes, target)?;
                self.verify_file(key, target)
            }
            other => other,
        }
    }
    /// v0.1.4 `_evict_locked`: while over the cap, remove the unpinned entry with the oldest
    /// access time (ties by object id) together with its bytes; pinned entries are never evicted,
    /// so an all-pinned cache may stay over the cap. Rewrites the index.
    fn evict_locked(&self, index: &mut Index) -> Result<Vec<String>> {
        let mut total = index.entries.values().try_fold(0u64, |sum, e| {
            sum.checked_add(e.size_bytes)
                .ok_or_else(|| Error::new("artifact_capacity"))
        })?;
        let mut candidates: Vec<(u64, String)> = index
            .entries
            .iter()
            .filter(|(_, e)| e.pins().is_empty())
            .map(|(id, e)| (e.last_access_unix_ns.unwrap_or(0), id.clone()))
            .collect();
        candidates.sort();
        let mut evicted = Vec::new();
        for (_, id) in candidates {
            if total <= self.cap.get() {
                break;
            }
            // `unlink(missing_ok=True)`: an already-absent object is fine, any other failure stops.
            if let Err(e) = fs::remove_file(self.object_path(&id))
                && e.kind() != std::io::ErrorKind::NotFound
            {
                return Err(Error::io("artifact_cleanup_failed", e));
            }
            if let Some(entry) = index.entries.remove(&id) {
                total -= entry.size_bytes;
            }
            evicted.push(id);
        }
        self.write_index(index)?;
        Ok(evicted)
    }
    /// v0.1.4 `evict`: bring the cache back under its cap. Returns the evicted object ids.
    pub fn evict(&self) -> Result<Vec<String>> {
        self.with_lock(|| {
            let mut index = self.read_index()?;
            self.evict_locked(&mut index)
        })
    }
    /// v0.1.4 `unpin_assignment`: drop one assignment's pins; staging directories are untouched.
    pub fn unpin_assignment(&self, assignment_id: &str) -> Result<()> {
        self.with_lock(|| {
            let mut index = self.read_index()?;
            for entry in index.entries.values_mut() {
                entry.pins = Some(
                    entry
                        .pins()
                        .iter()
                        .filter(|p| *p != assignment_id)
                        .cloned()
                        .collect(),
                );
            }
            self.write_index(&index)
        })
    }
}

#[cfg(test)]
#[path = "../tests/support/temp.rs"]
mod test_temp;
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn index_temporary_collision_never_deletes_the_foreign_file() {
        let root = test_temp::directory("collision");
        let cache =
            Cache::open(&root, Bytes::new(1024), Bytes::new(65536), Bytes::new(4096)).unwrap();
        let collision = root.join(format!(
            ".l7-{}-{}.partial",
            std::process::id(),
            NEXT.load(Ordering::Relaxed)
        ));
        fs::write(&collision, b"foreign-owned").unwrap();
        let original = fs::read(root.join("index.v1.json")).unwrap();
        let error = cache
            .write_index(&Index {
                protocol: "mycelium.assignment_artifact_cache.v1".into(),
                max_bytes: 1024,
                entries: BTreeMap::new(),
            })
            .unwrap_err();
        assert_eq!(error.code, "artifact_index_write_failed");
        assert_eq!(fs::read(&collision).unwrap(), b"foreign-owned");
        assert_eq!(fs::read(root.join("index.v1.json")).unwrap(), original);
        fs::remove_dir_all(root).unwrap();
    }
}
