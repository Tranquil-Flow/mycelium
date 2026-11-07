// SPDX-License-Identifier: AGPL-3.0-only
//! Small private evidence store. No implicit home/state paths.
use crate::{Fault, FaultKind, Result, observe::SnapshotStore};
use serde_json::Value;
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::{
    fs::{File, OpenOptions},
    path::{Path, PathBuf},
};

fn io_error(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}
pub fn create_private(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| io_error("private_create_refused"))?;
    f.write_all(bytes)
        .map_err(|_| io_error("private_write_failed"))?;
    f.sync_all().map_err(|_| io_error("private_sync_failed"))?;
    if let Some(parent) = path.parent() {
        File::open(parent)
            .and_then(|f| f.sync_all())
            .map_err(|_| io_error("private_parent_sync_failed"))?;
    }
    Ok(())
}
pub fn read_private(path: &Path, uid: u32, max_bytes: u64) -> Result<Vec<u8>> {
    let f = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| io_error("private_open_refused"))?;
    let m = f
        .metadata()
        .map_err(|_| io_error("private_metadata_failed"))?;
    if !m.is_file()
        || m.uid() != uid
        || m.permissions().mode() & 0o777 != 0o600
        || m.nlink() != 1
        || m.len() > max_bytes
    {
        return Err(Fault::new("private_file_invalid", FaultKind::Admission));
    }
    let bound = max_bytes
        .checked_add(1)
        .ok_or(io_error("private_read_bound_invalid"))?;
    let mut bytes = Vec::new();
    f.take(bound)
        .read_to_end(&mut bytes)
        .map_err(|_| io_error("private_read_failed"))?;
    if bytes.len() as u64 > max_bytes {
        return Err(io_error("private_read_bound_exceeded"));
    }
    Ok(bytes)
}
pub fn read_join(path: &Path, uid: u32) -> Result<String> {
    let bytes = read_private(path, uid, 16384)?;
    let value = String::from_utf8(bytes)
        .map_err(|_| io_error("join_file_invalid_utf8"))?
        .trim()
        .to_owned();
    if value.is_empty() {
        return Err(io_error("join_file_empty"));
    }
    Ok(value)
}
pub struct FileSnapshot {
    pub path: PathBuf,
}
impl SnapshotStore for FileSnapshot {
    fn exists(&mut self) -> Result<bool> {
        match std::fs::symlink_metadata(&self.path) {
            Ok(m) if m.is_file() && !m.file_type().is_symlink() => Ok(true),
            Ok(_) => Err(io_error("snapshot_path_invalid")),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(_) => Err(io_error("snapshot_metadata_failed")),
        }
    }
    fn create_first(&mut self, snapshot: &Value) -> Result<()> {
        let bytes = serde_json::to_vec_pretty(snapshot)
            .map_err(|_| io_error("snapshot_encoding_failed"))?;
        create_private(&self.path, &bytes)
    }
}
/// Exclusive marker ownership is device+inode, never PID or filename alone.
pub struct MarkerLease {
    path: PathBuf,
    device: u64,
    inode: u64,
}
impl MarkerLease {
    pub fn acquire(path: &Path, value: &Value) -> Result<Self> {
        let bytes = serde_json::to_vec(value).map_err(|_| io_error("marker_encoding_failed"))?;
        create_private(path, &bytes)?;
        let m =
            std::fs::symlink_metadata(path).map_err(|_| io_error("marker_identity_unobserved"))?;
        Ok(Self {
            path: path.to_path_buf(),
            device: m.dev(),
            inode: m.ino(),
        })
    }
    /// Caller must hold the private parent directory; pathname unlink is not an
    /// atomic compare-and-unlink against hostile writers with the same UID.
    pub fn close(self) -> Result<()> {
        let m = std::fs::symlink_metadata(&self.path)
            .map_err(|_| io_error("marker_identity_unobserved"))?;
        if m.dev() != self.device || m.ino() != self.inode || !m.is_file() {
            return Err(Fault::new("marker_identity_changed", FaultKind::Cleanup));
        }
        std::fs::remove_file(&self.path)
            .map_err(|_| Fault::new("marker_remove_failed", FaultKind::Cleanup))
    }
}
