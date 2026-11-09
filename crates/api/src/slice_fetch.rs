// SPDX-License-Identifier: AGPL-3.0-only
//! A member fetches exactly its own verified slice: `fetch_node_slice` (`slice_acquisition.py:311-586
//! @ f4e366e9`). The node plan is checked against its pinned digest before anything is read; every
//! segment is sha256-checked before it is journalled, every file against the plan before an atomic
//! publish. Verified files and journalled segments are reused (resumable, idempotent). The download
//! and disk caps, the free-disk check and Stop (`should_stop`, between segments) are enforced here.
//!
//! Bytes come from a [`SliceTransport`]. The only transport ported is the owner's verified snapshot
//! ([`SnapshotRangeTransport`], `demo_target.py:107-140`). v0.1.4's last-resort ranged HTTPS
//! repository transport (`acquisition._HTTPRepositoryTransport`) belongs to the reserved model
//! download work: a member with no peer source is refused by name ([`REPOSITORY_FALLBACK_NOT_PORTED`])
//! before any byte is read, never silently served from elsewhere.
use base64::Engine;
use budgets::BudgetId;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, FileExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
};

pub const NODE_PLAN_PROTOCOL: &str = "mycelium.a13_node_slice_plan.v1";
pub const REPORT_PROTOCOL: &str = "mycelium.a13_slice_fetch_report.v1";
/// The closed refusal for a member whose only byte source would be the repository (S1) fallback.
pub const REPOSITORY_FALLBACK_NOT_PORTED: &str = "slice_repository_fallback_not_ported";
const WORK: &str = ".slice-work";
/// `MAX_FILES_PER_NODE`, `MAX_SEGMENTS_PER_FILE` (`slice_acquisition.py:65-66`): plan shape bounds.
const MAX_FILES_PER_NODE: usize = 64;
const MAX_SEGMENTS_PER_FILE: usize = 16384;

/// `SliceError(code)`: a stable, non-sensitive refusal code.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SliceError(pub String);
impl SliceError {
    fn new(code: &str) -> Self {
        Self(code.to_owned())
    }
}
type Result<T> = std::result::Result<T, SliceError>;

/// The `read_range` seam (plus the optional `reject` / `clone_candidate` hooks).
pub trait SliceTransport: Send {
    /// At most `length` bytes at `offset` of `path` in the pinned snapshot. `Err` carries the final
    /// closed code (an acquisition refusal is `slice_transport_<code>`, as `_fetch_segment` names it).
    fn read_range(
        &mut self,
        model_id: &str,
        commit: &str,
        path: &str,
        offset: u64,
        length: u64,
    ) -> Result<Vec<u8>>;
    /// Quarantine the source of a segment that failed its digest; `true` = fetch it again.
    fn reject(&mut self, _path: &str, _offset: u64, _length: u64) -> bool {
        false
    }
    /// A local file to clone for `path` (verified by digest before it is published).
    fn clone_candidate(&mut self, _path: &str) -> Option<PathBuf> {
        None
    }
}

/// `SnapshotRangeTransport`: `read_range` over the owner's own verified snapshot (no network).
pub struct SnapshotRangeTransport {
    root: PathBuf,
    model_id: String,
    commit: String,
    clone_root: Option<PathBuf>,
}
impl SnapshotRangeTransport {
    pub fn new(root: PathBuf, model_id: &str, commit: &str, clone_root: Option<PathBuf>) -> Self {
        Self {
            root,
            model_id: model_id.to_owned(),
            commit: commit.to_owned(),
            clone_root,
        }
    }
}
impl SliceTransport for SnapshotRangeTransport {
    fn read_range(
        &mut self,
        model_id: &str,
        commit: &str,
        path: &str,
        offset: u64,
        length: u64,
    ) -> Result<Vec<u8>> {
        if model_id != self.model_id || commit != self.commit {
            return Err(SliceError::new("slice_transport_model_mismatch"));
        }
        let invalid = || SliceError::new("slice_transport_path_invalid");
        // A missing file is an OSError in the source: no closed code survives it.
        let failed = || SliceError::new("slice_fetch_failed");
        let base = fs::canonicalize(&self.root).map_err(|_| failed())?;
        let target = fs::canonicalize(self.root.join(path)).map_err(|_| failed())?;
        if !target.starts_with(&base) || target == base {
            return Err(invalid());
        }
        // `handle.seek(offset); handle.read(length)`: the plan's digest decides a short read.
        let mut file = File::open(&target).map_err(|_| failed())?;
        std::io::Seek::seek(&mut file, std::io::SeekFrom::Start(offset)).map_err(|_| failed())?;
        let mut data = Vec::new();
        file.take(length)
            .read_to_end(&mut data)
            .map_err(|_| failed())?;
        Ok(data)
    }
    fn clone_candidate(&mut self, path: &str) -> Option<PathBuf> {
        let root = self.clone_root.as_ref()?;
        let relative = Path::new(path);
        if relative.is_absolute()
            || path.is_empty()
            || relative
                .components()
                .any(|c| !matches!(c, Component::Normal(_)))
        {
            return None;
        }
        let candidate = root.join(relative);
        let meta = fs::symlink_metadata(&candidate).ok()?;
        meta.is_file().then_some(candidate)
    }
}

fn digest_value(value: &Value) -> Result<String> {
    planner::digest(value, true).map_err(|_| SliceError::new("slice_plan_invalid"))
}
fn sha(data: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(data))
}
fn file_sha(path: &Path) -> std::io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut chunk = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        hasher.update(&chunk[..n]);
    }
    Ok(format!("sha256:{:x}", hasher.finalize()))
}
fn is_digest(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        s.len() == 71
            && s.starts_with("sha256:")
            && s[7..]
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
/// `_relative`: a plain relative POSIX path, no hidden or traversing part.
fn relative(value: &Value) -> Result<&str> {
    let bad = || SliceError::new("slice_path_invalid");
    let text = value.as_str().ok_or_else(bad)?;
    if text.is_empty() || text.contains('\0') || text.len() > 1024 {
        return Err(bad());
    }
    let parts: Vec<&str> = text.split('/').collect();
    if text.starts_with('/')
        || parts.len() > 16
        || parts
            .iter()
            .any(|p| p.is_empty() || *p == "." || *p == ".." || p.starts_with('.'))
    {
        return Err(bad());
    }
    Ok(text)
}
fn positive_int(value: &Value) -> Option<u64> {
    value.as_u64().filter(|v| *v > 0)
}
fn budget_bytes(id: BudgetId) -> Result<u64> {
    id.bytes()
        .map(|b| b.get())
        .map_err(|_| SliceError::new("budget_unavailable"))
}

/// `_validated_node_plan`: the exact plan pinned by `expected`, with every structural bound.
pub fn validated_node_plan(node_plan: &Value, expected: &str) -> Result<Value> {
    if !is_digest(&json!(expected)) {
        return Err(SliceError::new("slice_plan_digest_mismatch"));
    }
    let mut body = node_plan
        .as_object()
        .cloned()
        .ok_or_else(|| SliceError::new("slice_plan_invalid"))?;
    body.remove("node_plan_digest");
    if node_plan["protocol"] != NODE_PLAN_PROTOCOL
        || node_plan["node_plan_digest"] != expected
        || digest_value(&Value::Object(body))? != expected
    {
        return Err(SliceError::new("slice_plan_digest_mismatch"));
    }
    structure(node_plan).map_err(|e| {
        if e.0 == "slice_path_invalid" {
            e
        } else {
            SliceError::new("slice_plan_invalid")
        }
    })?;
    Ok(node_plan.clone())
}
fn structure(plan: &Value) -> Result<()> {
    let bad = || SliceError::new("slice_plan_invalid");
    let segment_limit =
        budget_bytes(BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63)?;
    let inline_limit =
        budget_bytes(BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxInlineBytesPerNodeL64)?;
    let mut sizes = BTreeMap::new();
    for row in plan["source_files"].as_array().ok_or_else(bad)? {
        let path = relative(&row["path"])?;
        sizes.insert(path, positive_int(&row["size_bytes"]).ok_or_else(bad)?);
    }
    let files = plan["files"].as_array().ok_or_else(bad)?;
    if files.is_empty() || files.len() > MAX_FILES_PER_NODE {
        return Err(bad());
    }
    let paths: Vec<&str> = files
        .iter()
        .map(|r| r["path"].as_str().ok_or_else(bad))
        .collect::<Result<_>>()?;
    let unique: BTreeSet<&str> = paths.iter().copied().collect();
    if paths != unique.into_iter().collect::<Vec<_>>() {
        return Err(bad());
    }
    let (mut download, mut inline) = (0u64, 0u64);
    for row in files {
        relative(&row["path"])?;
        let size = positive_int(&row["size_bytes"]).ok_or_else(bad)?;
        let segments = row["segments"].as_array().ok_or_else(bad)?;
        if !is_digest(&row["content_digest"])
            || segments.is_empty()
            || segments.len() > MAX_SEGMENTS_PER_FILE
        {
            return Err(bad());
        }
        let mut cursor = 0u64;
        for segment in segments {
            let keys: BTreeSet<&str> = segment
                .as_object()
                .ok_or_else(bad)?
                .keys()
                .map(String::as_str)
                .collect();
            let length = positive_int(&segment["length"])
                .filter(|l| *l <= segment_limit)
                .ok_or_else(bad)?;
            if segment["offset"].as_u64() != Some(cursor) || !is_digest(&segment["sha256"]) {
                return Err(bad());
            }
            if keys.contains("inline_base64") {
                if keys != BTreeSet::from(["offset", "length", "sha256", "inline_base64"]) {
                    return Err(bad());
                }
                let data = base64::engine::general_purpose::STANDARD
                    .decode(segment["inline_base64"].as_str().ok_or_else(bad)?)
                    .map_err(|_| bad())?;
                if data.len() as u64 != length || sha(&data) != segment["sha256"] {
                    return Err(bad());
                }
                inline = inline.checked_add(length).ok_or_else(bad)?;
            } else {
                if keys
                    != BTreeSet::from([
                        "offset",
                        "length",
                        "sha256",
                        "source_path",
                        "source_offset",
                    ])
                {
                    return Err(bad());
                }
                let source = sizes
                    .get(segment["source_path"].as_str().ok_or_else(bad)?)
                    .ok_or_else(bad)?;
                let start = segment["source_offset"].as_u64().ok_or_else(bad)?;
                if start.checked_add(length).is_none_or(|end| end > *source) {
                    return Err(bad());
                }
                download = download.checked_add(length).ok_or_else(bad)?;
            }
            cursor = cursor.checked_add(length).ok_or_else(bad)?;
        }
        if cursor != size {
            return Err(bad());
        }
    }
    if plan["download_bytes"].as_u64() != Some(download)
        || plan["inline_bytes"].as_u64() != Some(inline)
        || inline > inline_limit
    {
        return Err(bad());
    }
    Ok(())
}

/// `_private_dir`.
fn private_dir(path: &Path) -> Result<()> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)
        .map_err(|_| SliceError::new("slice_destination_invalid"))?;
    let meta =
        fs::symlink_metadata(path).map_err(|_| SliceError::new("slice_destination_invalid"))?;
    if !meta.is_dir() {
        return Err(SliceError::new("slice_destination_invalid"));
    }
    Ok(())
}
/// `_atomic_json`.
fn atomic_json(path: &Path, value: &Value) -> Result<()> {
    let failed = || SliceError::new("slice_write_failed");
    let temporary = path.with_file_name(format!(
        "{}.tmp",
        path.file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("journal")
    ));
    let text = planner::canonical(value, true).map_err(|_| failed())?;
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&temporary)
        .map_err(|_| failed())?;
    file.write_all(text.as_bytes())
        .and_then(|()| file.sync_all())
        .map_err(|_| failed())?;
    fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600)).map_err(|_| failed())?;
    fs::rename(&temporary, path).map_err(|_| failed())
}
fn remove_tree(path: &Path) {
    if let Err(error) = fs::remove_dir_all(path)
        && error.kind() != std::io::ErrorKind::NotFound
    {
        eprintln!("slice work cleanup failed: {}", error.kind());
    }
}
fn work_name(row: &Value) -> Result<String> {
    Ok(digest_value(row)?
        .split_once(':')
        .map(|(_, h)| h.to_owned())
        .unwrap_or_default())
}

/// `_clone_verified`: publish `row` from a local candidate (owned, single-link, right size) once its
/// copy has the plan's digest; `false` falls back to fetching.
fn clone_verified(row: &Value, root: &Path, transport: &mut dyn SliceTransport) -> Result<bool> {
    let path = row["path"].as_str().unwrap_or_default();
    let size = row["size_bytes"].as_u64().unwrap_or(0);
    let Some(candidate) = transport.clone_candidate(path) else {
        return Ok(false);
    };
    let work = root.join(WORK).join(format!("clone-{}", work_name(row)?));
    remove_tree(&work);
    private_dir(&work)?;
    let partial = work.join("partial");
    let owned = |meta: &fs::Metadata| {
        meta.is_file()
            && meta.nlink() == 1
            && meta.uid() == rustix::process::geteuid().as_raw()
            && meta.len() == size
    };
    let verified = (|| -> std::io::Result<bool> {
        let source = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&candidate)?;
        if !owned(&source.metadata()?) {
            return Ok(false);
        }
        // `_clone_file(fd, partial)`: an APFS clone of the file just opened and checked, never the
        // path again (CLONE_NOFOLLOW); no clone falls back to fetching. The digest below decides.
        if rustix::fs::fclonefileat(
            &source,
            rustix::fs::CWD,
            &partial,
            rustix::fs::CloneFlags::NOFOLLOW,
        )
        .is_err()
        {
            return Ok(false);
        }
        let meta = fs::symlink_metadata(&partial)?;
        if !meta.is_file()
            || meta.nlink() != 1
            || meta.len() != size
            || file_sha(&partial)? != row["content_digest"]
        {
            return Ok(false);
        }
        fs::set_permissions(&partial, fs::Permissions::from_mode(0o600))?;
        Ok(true)
    })()
    .unwrap_or(false);
    // `_private_dir(final.parent)`: a parent that is not a private directory of the slice root
    // (a symlink) is refused by name, never followed.
    let final_path = root.join(path);
    let parent = match final_path.parent() {
        Some(parent) if verified => private_dir(parent),
        _ => Ok(()),
    };
    let published = parent.is_ok() && verified && fs::rename(&partial, &final_path).is_ok();
    remove_tree(&work);
    parent.map(|()| published)
}

/// `_fetch_segment`: one digest-verified segment; (data, range requests, bytes received).
fn fetch_segment(
    transport: &mut dyn SliceTransport,
    plan: &Value,
    segment: &Value,
) -> Result<(Vec<u8>, u64, u64)> {
    let retries = BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentRetriesL444
        .count()
        .map_err(|_| SliceError::new("budget_unavailable"))?
        .get();
    let path = segment["source_path"].as_str().unwrap_or_default();
    let offset = segment["source_offset"].as_u64().unwrap_or(0);
    let length = segment["length"].as_u64().unwrap_or(0);
    let (mut requests, mut received) = (0, 0);
    for attempt in 0..=retries {
        let data = transport.read_range(
            plan["model_id"].as_str().unwrap_or_default(),
            plan["resolved_commit"].as_str().unwrap_or_default(),
            path,
            offset,
            length,
        )?;
        requests += 1;
        received += data.len() as u64;
        if data.len() as u64 == length && sha(&data) == segment["sha256"] {
            return Ok((data, requests, received));
        }
        if attempt == retries || !transport.reject(path, offset, length) {
            break;
        }
    }
    Err(SliceError::new("slice_segment_digest_mismatch"))
}

/// `shutil.disk_usage(root).free`.
fn free_bytes(root: &Path) -> Result<u64> {
    let stat = rustix::fs::statvfs(root).map_err(|_| SliceError::new("slice_disk_unavailable"))?;
    Ok(stat.f_bavail.saturating_mul(stat.f_frsize))
}

/// `fetch_node_slice`: fetch, verify and publish exactly this member's slice files.
pub fn fetch_node_slice(
    node_plan: &Value,
    expected_node_plan_digest: &str,
    destination_root: &Path,
    transport: &mut dyn SliceTransport,
    max_download_bytes: u64,
    max_disk_bytes: u64,
    should_stop: &dyn Fn() -> bool,
) -> Result<Value> {
    let plan = validated_node_plan(node_plan, expected_node_plan_digest)?;
    if max_disk_bytes == 0 {
        return Err(SliceError::new("slice_fetch_request_invalid"));
    }
    let files = plan["files"].as_array().cloned().unwrap_or_default();
    let size = |row: &Value| row["size_bytes"].as_u64().unwrap_or(0);
    if plan["download_bytes"].as_u64().unwrap_or(u64::MAX) > max_download_bytes {
        return Err(SliceError::new("slice_download_limit_exceeded"));
    }
    if files.iter().map(size).sum::<u64>() > max_disk_bytes {
        return Err(SliceError::new("slice_disk_limit_exceeded"));
    }
    let root = destination_root;
    let root_ok = root.is_absolute()
        && fs::symlink_metadata(root).is_ok_and(|m| m.is_dir())
        && fs::canonicalize(root).ok().as_deref() == Some(root);
    if !root_ok {
        return Err(SliceError::new("slice_destination_invalid"));
    }
    let cancelled = || SliceError::new("slice_fetch_cancelled");
    let (mut downloaded, mut reused, mut requests, mut cloned) = (0u64, 0u64, 0u64, 0u64);
    let mut pending = Vec::new();
    for row in &files {
        let final_path = root.join(row["path"].as_str().unwrap_or_default());
        match fs::symlink_metadata(&final_path) {
            Ok(meta) if meta.file_type().is_symlink() => {
                return Err(SliceError::new("slice_destination_conflict"));
            }
            Ok(meta) => {
                if !meta.is_file()
                    || meta.len() != size(row)
                    || file_sha(&final_path).ok().as_deref() != row["content_digest"].as_str()
                {
                    return Err(SliceError::new("slice_destination_conflict"));
                }
                reused += size(row);
            }
            Err(_) => pending.push(row.clone()),
        }
    }
    let mut remaining = Vec::new();
    for row in pending {
        if should_stop() {
            return Err(cancelled());
        }
        if clone_verified(&row, root, transport)? {
            cloned += size(&row);
        } else {
            remaining.push(row);
        }
    }
    let pending = remaining;
    let needed: u64 = pending.iter().map(size).sum();
    if needed > 0 && free_bytes(root)? < needed {
        return Err(SliceError::new("slice_disk_unavailable"));
    }
    let write_failed = || SliceError::new("slice_write_failed");
    for row in &pending {
        let work = root.join(WORK).join(work_name(row)?);
        private_dir(&work)?;
        let (partial, journal) = (work.join("partial"), work.join("journal.json"));
        let mut completed: BTreeSet<u64> = BTreeSet::new();
        let resumable = journal.is_file()
            && fs::symlink_metadata(&partial).is_ok_and(|m| m.is_file() && m.len() == size(row));
        if resumable {
            completed = fs::read(&journal)
                .ok()
                .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                .and_then(|v| {
                    v["completed"]
                        .as_array()
                        .map(|a| a.iter().filter_map(Value::as_u64).collect())
                })
                .unwrap_or_default();
        } else {
            let file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&partial)
                .map_err(|_| write_failed())?;
            file.set_len(size(row)).map_err(|_| write_failed())?;
            fs::set_permissions(&partial, fs::Permissions::from_mode(0o600))
                .map_err(|_| write_failed())?;
        }
        let fd = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&partial)
            .map_err(|_| write_failed())?;
        let segments = row["segments"].as_array().cloned().unwrap_or_default();
        for index in completed.clone() {
            let keep = segments.get(index as usize).is_some_and(|segment| {
                let mut buffer = vec![0; segment["length"].as_u64().unwrap_or(0) as usize];
                fd.read_exact_at(&mut buffer, segment["offset"].as_u64().unwrap_or(0))
                    .is_ok()
                    && sha(&buffer) == segment["sha256"]
            });
            if keep {
                reused += segments[index as usize]["length"].as_u64().unwrap_or(0);
            } else {
                completed.remove(&index);
            }
        }
        for (index, segment) in segments.iter().enumerate() {
            if completed.contains(&(index as u64)) {
                continue;
            }
            if should_stop() {
                return Err(cancelled());
            }
            let data = if let Some(inline) = segment["inline_base64"].as_str() {
                let data = base64::engine::general_purpose::STANDARD
                    .decode(inline)
                    .map_err(|_| SliceError::new("slice_segment_digest_mismatch"))?;
                if sha(&data) != segment["sha256"] {
                    return Err(SliceError::new("slice_segment_digest_mismatch"));
                }
                data
            } else {
                let (data, n, bytes) = fetch_segment(transport, &plan, segment)?;
                requests += n;
                downloaded += bytes;
                data
            };
            fd.write_all_at(&data, segment["offset"].as_u64().unwrap_or(0))
                .and_then(|()| fd.sync_all())
                .map_err(|_| write_failed())?;
            completed.insert(index as u64);
            atomic_json(
                &journal,
                &json!({"file": row["path"], "completed": completed.iter().collect::<Vec<_>>()}),
            )?;
        }
        drop(fd);
        if file_sha(&partial).ok().as_deref() != row["content_digest"].as_str() {
            remove_tree(&work);
            return Err(SliceError::new("slice_file_digest_mismatch"));
        }
        let final_path = root.join(row["path"].as_str().unwrap_or_default());
        if let Some(parent) = final_path.parent() {
            private_dir(parent)?;
        }
        fs::rename(&partial, &final_path).map_err(|_| write_failed())?;
        remove_tree(&work);
    }
    let work_root = root.join(WORK);
    if fs::read_dir(&work_root).is_ok_and(|mut d| d.next().is_none())
        && let Err(error) = fs::remove_dir(&work_root)
    {
        eprintln!("slice work root cleanup failed: {}", error.kind());
    }
    Ok(json!({
        "protocol": REPORT_PROTOCOL, "state": "slice_verified_not_loaded",
        "node_id": plan["node_id"], "node_plan_digest": plan["node_plan_digest"],
        "plan_digest": plan["plan_digest"], "destination_root": root.to_string_lossy(),
        "files": files.iter().map(|r| json!({"path": r["path"], "size_bytes": r["size_bytes"],
            "content_digest": r["content_digest"]})).collect::<Vec<_>>(),
        "downloaded_bytes": downloaded, "reused_bytes": reused, "cloned_bytes": cloned,
        "range_requests": requests, "route_ready": false,
    }))
}

/// `slice_progress(node_plan, root)`: bytes verified so far (published files + journals).
pub fn slice_progress(node_plan: &Value, root: &Path) -> Option<Value> {
    let files = node_plan.get("files")?.as_array()?;
    let size = |row: &Value| row["size_bytes"].as_u64().unwrap_or(0);
    let total: u64 = files.iter().map(size).sum();
    let mut done = 0u64;
    let mut lengths = BTreeMap::new();
    for row in files {
        let path = row["path"].as_str().unwrap_or_default();
        if fs::metadata(root.join(path)).is_ok_and(|m| m.is_file() && m.len() == size(row)) {
            done += size(row);
        }
        for (index, segment) in row["segments"].as_array().into_iter().flatten().enumerate() {
            lengths.insert(
                (path.to_owned(), index as u64),
                segment["length"].as_u64().unwrap_or(0),
            );
        }
    }
    if let Ok(entries) = fs::read_dir(root.join(WORK)) {
        for entry in entries.flatten() {
            let Some(value) = fs::read(entry.path().join("journal.json"))
                .ok()
                .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
            else {
                continue;
            };
            let file = value["file"].as_str().unwrap_or_default().to_owned();
            for index in value["completed"].as_array().into_iter().flatten() {
                if let Some(i) = index.as_u64() {
                    done += lengths.get(&(file.clone(), i)).copied().unwrap_or(0);
                }
            }
        }
    }
    Some(json!({"done_bytes": done.min(total), "total_bytes": total}))
}
