// SPDX-License-Identifier: AGPL-3.0-only
//! Hermetic fixture roots. A test never inspects the ambient `TMPDIR` for host
//! hygiene: it creates its own private, canonical, owner-only directory and passes
//! that path explicitly to the code under test. The root policy is a pure function
//! of an explicit path (and optional explicit location marker), so it is tested
//! with injected paths in `scratch_policy.rs`.
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::{
    fs,
    ops::Deref,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);

/// Policy for a fixture root. `marker`, when injected, additionally requires the
/// canonical path text to contain it (the operator's host-scratch convention).
/// The root must be absolute, canonical (no symlink component, no `.`/`..`), a real
/// directory owned by `uid` and exactly mode 0700.
pub fn verify_private_root(
    path: &Path,
    uid: u32,
    marker: Option<&str>,
) -> Result<PathBuf, &'static str> {
    if !path.is_absolute() {
        return Err("root_not_absolute");
    }
    if let Some(marker) = marker
        && !path.to_string_lossy().contains(marker)
    {
        return Err("root_outside_required_location");
    }
    let link = fs::symlink_metadata(path).map_err(|_| "root_unobserved")?;
    if link.file_type().is_symlink() {
        return Err("root_is_symlink");
    }
    if !link.is_dir() {
        return Err("root_not_directory");
    }
    let canonical = fs::canonicalize(path).map_err(|_| "root_unobserved")?;
    if canonical != path {
        return Err("root_not_canonical");
    }
    if link.uid() != uid {
        return Err("root_wrong_owner");
    }
    if link.permissions().mode() & 0o777 != 0o700 {
        return Err("root_not_private");
    }
    Ok(canonical)
}

/// A uniquely named private directory removed on drop. Dereferences to its path.
pub struct PrivateRoot(PathBuf);
impl PrivateRoot {
    pub fn new(prefix: &str) -> Self {
        let parent = fs::canonicalize(std::env::temp_dir()).expect("canonical temp parent");
        let path = parent.join(format!(
            "v02-l12-{prefix}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .expect("fresh private fixture root");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).expect("mode 0700");
        // This process just created the directory, so its owner is the expected uid.
        let uid = fs::metadata(&path).expect("fixture metadata").uid();
        let path = verify_private_root(&path, uid, None).expect("fixture root policy");
        Self(path)
    }
    pub fn uid(&self) -> u32 {
        fs::metadata(&self.0).expect("fixture metadata").uid()
    }
}
impl Deref for PrivateRoot {
    type Target = Path;
    fn deref(&self) -> &Path {
        &self.0
    }
}
impl Drop for PrivateRoot {
    fn drop(&mut self) {
        if let Err(e) = fs::remove_dir_all(&self.0)
            && !std::thread::panicking()
        {
            panic!("fixture cleanup failed: {e}");
        }
    }
}
