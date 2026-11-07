// SPDX-License-Identifier: AGPL-3.0-only
//! Assignment-local staging. Cached bytes and staging pins are not route authority.
use crate::{
    ArtifactKey, Cache, Error, Result, VerifiedArtifact, directory_not_link, encode, open_regular,
    private_directory,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};
const MANIFEST: &str = "assignment-materialization.json";

fn parent_of(destination: &Path) -> Result<&Path> {
    destination
        .parent()
        .filter(|_| destination.is_absolute() && destination.file_name().is_some())
        .ok_or_else(|| Error::new("artifact_path_invalid"))
}
fn validate(
    assignment: &str,
    required: &BTreeMap<String, ArtifactKey>,
    destination: &Path,
) -> Result<()> {
    if assignment.is_empty() || required.is_empty() {
        return Err(Error::new("artifact_assignment_invalid"));
    }
    parent_of(destination)?;
    let mut objects = BTreeSet::new();
    for (path, key) in required {
        if path.is_empty()
            || path.starts_with('/')
            || path.contains(['\\', '\0'])
            || path
                .split('/')
                .any(|p| p.is_empty() || p == "." || p == "..")
            || path == MANIFEST
            || path.starts_with(&format!("{MANIFEST}/"))
        {
            return Err(Error::new("artifact_path_invalid"));
        }
        if !objects.insert(key.object_id()?) {
            return Err(Error::new("artifact_assignment_invalid"));
        }
    }
    // A file cannot also be the ancestor of another assigned file.
    for path in required.keys() {
        let mut parent = Path::new(path).parent();
        while let Some(p) = parent.filter(|p| !p.as_os_str().is_empty()) {
            if required.contains_key(
                p.to_str()
                    .ok_or_else(|| Error::new("artifact_path_invalid"))?,
            ) {
                return Err(Error::new("artifact_path_invalid"));
            }
            parent = p.parent();
        }
    }
    Ok(())
}
fn report(
    assignment: &str,
    required: &BTreeMap<String, ArtifactKey>,
    entry_count: usize,
) -> Result<Value> {
    let objects:Result<Vec<_>>=required.iter().map(|(path,key)|Ok(json!({"relative_path":path,"object_id":key.object_id()?,"tensor_digest":key.tensor_digest,"size_bytes":key.size_bytes}))).collect();
    let objects = objects?;
    let opened: Vec<_> = objects.iter().map(|v| v["object_id"].clone()).collect();
    let unassigned = entry_count
        .checked_sub(required.len())
        .ok_or_else(|| Error::new("artifact_manifest_mismatch"))?;
    Ok(
        json!({"protocol":"mycelium.assignment_materialization.v1","assignment_id":assignment,"objects":objects,"opened_object_ids":opened,"cache_entry_count":entry_count,"unassigned_object_count":unassigned,"route_ready":false}),
    )
}
fn exists(path: &Path) -> Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(Error::io("artifact_io", e)),
    }
}
/// Exact private directory/file inventory; never follow links or delete an
/// unlisted descendant merely because its parent is an assignment directory.
fn inventory(root: &Path, required: &BTreeMap<String, ArtifactKey>) -> Result<Vec<PathBuf>> {
    directory_not_link(root)?;
    let mut files: BTreeSet<PathBuf> = required.keys().map(PathBuf::from).collect();
    files.insert(MANIFEST.into());
    let mut dirs = BTreeSet::new();
    for file in &files {
        let mut parent = file.parent();
        while let Some(p) = parent.filter(|p| !p.as_os_str().is_empty()) {
            dirs.insert(p.to_path_buf());
            parent = p.parent();
        }
    }
    let mut stack = vec![PathBuf::new()];
    let mut visited = Vec::new();
    let mut found = BTreeSet::new();
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(root.join(&dir)).map_err(|e| Error::io("artifact_io", e))? {
            let entry = entry.map_err(|e| Error::io("artifact_io", e))?;
            let relative = dir.join(entry.file_name());
            let meta =
                fs::symlink_metadata(entry.path()).map_err(|e| Error::io("artifact_io", e))?;
            if meta.is_dir() && dirs.contains(&relative) {
                // v0.1.4 leaves intermediate directories of a nested path at the default mode.
                directory_not_link(&entry.path())?;
                visited.push(relative.clone());
                stack.push(relative);
            } else if meta.is_file() && files.contains(&relative) {
                found.insert(relative);
            } else {
                return Err(Error::new("artifact_staging_foreign"));
            }
        }
    }
    if found != files {
        return Err(Error::new("artifact_missing"));
    }
    visited.sort_by_key(|p| std::cmp::Reverse(p.components().count()));
    Ok(visited)
}
impl Cache {
    /// Materialize only the caller's admitted assignment closure. The caller must
    /// already reserve its staging allowance through L6/L7. No download, model
    /// load, consent, route publication or reservation release happens here.
    /// The private parent has one managed writer; hostile same-UID races are not
    /// claimed. Pins are durable BEFORE publication; interruption may retain pins.
    pub fn materialize_assignment(
        &self,
        assignment_id: &str,
        required: &BTreeMap<String, ArtifactKey>,
        destination: &Path,
    ) -> Result<Value> {
        validate(assignment_id, required, destination)?;
        self.with_lock(|| {
            if exists(destination)? {
                return Err(Error::new("artifact_destination_exists"));
            }
            let mut index = self.read_index()?;
            // v0.1.4 creates a missing parent of the destination (default mode).
            let parent = parent_of(destination)?;
            fs::DirBuilder::new()
                .recursive(true)
                .create(parent)
                .map_err(|e| Error::io("artifact_directory_failed", e))?;
            directory_not_link(parent)?;
            let manifest = report(assignment_id, required, index.entries.len())?;
            let bytes = encode(&manifest)?;
            if bytes.len() as u64 > self.index_cap.get() {
                return Err(Error::new("artifact_index_too_large"));
            }
            let mut held = BTreeMap::new();
            for (path, key) in required {
                let id = key.object_id()?;
                if !index.entries.contains_key(&id) {
                    return Err(Error::new("artifact_missing"));
                }
                held.insert(path, self.staged_source(key, &self.object_path(&id))?);
            }
            let temp = self.temporary(parent);
            // Own the new directory before any cleanup can remove it.
            fs::DirBuilder::new()
                .create(&temp)
                .map_err(|e| Error::io("artifact_stage_create_failed", e))?;
            let result = (|| {
                fs::set_permissions(&temp, fs::Permissions::from_mode(0o700))
                    .map_err(|e| Error::io("artifact_stage_create_failed", e))?;
                for (path, key) in required {
                    let target = temp.join(path);
                    let relative = Path::new(path)
                        .parent()
                        .ok_or_else(|| Error::new("artifact_path_invalid"))?;
                    let mut dir = temp.clone();
                    for part in relative.components() {
                        dir.push(part);
                        private_directory(&dir)?;
                    }
                    let input = held
                        .get_mut(path)
                        .ok_or_else(|| Error::new("artifact_missing"))?;
                    let mut out = OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .mode(0o600)
                        .open(&target)
                        .map_err(|e| Error::io("artifact_population_failed", e))?;
                    let bound = key
                        .size_bytes
                        .checked_add(1)
                        .ok_or_else(|| Error::new("artifact_key_invalid"))?;
                    let count = std::io::copy(&mut input.take(bound), &mut out)
                        .map_err(|e| Error::io("artifact_population_failed", e))?;
                    if count != key.size_bytes {
                        return Err(Error::new("artifact_corrupt"));
                    }
                    out.sync_all()
                        .map_err(|e| Error::io("artifact_sync_failed", e))?;
                    self.verify_file(key, &target)?;
                    fs::set_permissions(&target, fs::Permissions::from_mode(0o400))
                        .map_err(|e| Error::io("artifact_population_failed", e))?;
                }
                let mut out = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o400)
                    .open(temp.join(MANIFEST))
                    .map_err(|e| Error::io("artifact_manifest_write_failed", e))?;
                out.write_all(&bytes)
                    .and_then(|()| out.sync_all())
                    .map_err(|e| Error::io("artifact_manifest_write_failed", e))?;
                for dir in inventory(&temp, required)? {
                    self.sync_dir(&temp.join(dir))?;
                }
                self.sync_dir(&temp)?;
                for key in required.values() {
                    let entry = index
                        .entries
                        .get_mut(&key.object_id()?)
                        .ok_or_else(|| Error::new("artifact_missing"))?;
                    // v0.1.4: pins become the sorted set, and the object counts as just used.
                    let mut pins: BTreeSet<String> = entry.pins().iter().cloned().collect();
                    pins.insert(assignment_id.into());
                    entry.pins = Some(pins.into_iter().collect());
                    entry.last_access_unix_ns = Some(crate::now_ns());
                }
                self.write_index(&index)?;
                if exists(destination)? {
                    return Err(Error::new("artifact_destination_exists"));
                }
                fs::rename(&temp, destination)
                    .map_err(|e| Error::io("artifact_stage_publish_failed", e))?;
                self.sync_dir(parent)?;
                Ok(manifest)
            })();
            // Only our unpublished temporary tree. A published destination is
            // retained on a late fsync failure, with its pins still held.
            if exists(&temp)? {
                match fs::remove_dir_all(&temp) {
                    Ok(()) => result,
                    Err(e) => match result {
                        Ok(_) => Err(Error::io("artifact_cleanup_failed", e)),
                        Err(mut original) => {
                            original.cleanup_io = Some(e.kind());
                            Err(original)
                        }
                    },
                }
            } else {
                result
            }
        })
    }
    fn verified_stage(
        &self,
        assignment_id: &str,
        required: &BTreeMap<String, ArtifactKey>,
        destination: &Path,
    ) -> Result<BTreeMap<String, VerifiedArtifact>> {
        inventory(destination, required)?;
        let mut bytes = Vec::new();
        let file = open_regular(&destination.join(MANIFEST), false)?;
        let bound = self
            .index_cap
            .get()
            .checked_add(1)
            .ok_or_else(|| Error::new("artifact_policy_invalid"))?;
        file.take(bound)
            .read_to_end(&mut bytes)
            .map_err(|e| Error::io("artifact_io", e))?;
        if bytes.len() as u64 > self.index_cap.get() {
            return Err(Error::new("artifact_index_too_large"));
        }
        let manifest =
            crate::unique::parse(&bytes).map_err(|_| Error::new("artifact_manifest_mismatch"))?;
        let count = manifest["cache_entry_count"]
            .as_u64()
            .and_then(|v| usize::try_from(v).ok())
            .ok_or_else(|| Error::new("artifact_manifest_mismatch"))?;
        if manifest != report(assignment_id, required, count)? {
            return Err(Error::new("artifact_manifest_mismatch"));
        }
        let index = self.read_index()?;
        let mut files = BTreeMap::new();
        for (path, key) in required {
            let entry = index
                .entries
                .get(&key.object_id()?)
                .ok_or_else(|| Error::new("artifact_missing"))?;
            if !entry.pins().iter().any(|p| p == assignment_id) {
                return Err(Error::new("artifact_assignment_unpinned"));
            }
            files.insert(
                path.clone(),
                VerifiedArtifact {
                    file: self.verify_file(key, &destination.join(path))?,
                    key: key.clone(),
                },
            );
        }
        Ok(files)
    }
    /// Verify exact manifest, cache identity/pins, staged SHA-256 and size, then
    /// return held read-only files positioned at zero. Not an unchecked pathname.
    pub fn verify_materialization(
        &self,
        assignment_id: &str,
        required: &BTreeMap<String, ArtifactKey>,
        destination: &Path,
    ) -> Result<BTreeMap<String, VerifiedArtifact>> {
        validate(assignment_id, required, destination)?;
        self.with_lock(|| self.verified_stage(assignment_id, required, destination))
    }
    /// Retire filesystem staging AFTER the caller has stopped its consumers.
    /// This is not worker/Metal cleanup proof and never releases L6 reservations.
    /// Any incomplete removal/unpin remains an explicit error with retained pins.
    pub fn retire_materialization(
        &self,
        assignment_id: &str,
        required: &BTreeMap<String, ArtifactKey>,
        destination: &Path,
    ) -> Result<()> {
        validate(assignment_id, required, destination)?;
        self.with_lock(|| {
            let files = self.verified_stage(assignment_id, required, destination)?;
            let dirs = inventory(destination, required)?;
            drop(files);
            for path in required.keys() {
                fs::remove_file(destination.join(path))
                    .map_err(|e| Error::io("artifact_cleanup_failed", e))?;
            }
            fs::remove_file(destination.join(MANIFEST))
                .map_err(|e| Error::io("artifact_cleanup_failed", e))?;
            for path in dirs {
                fs::remove_dir(destination.join(path))
                    .map_err(|e| Error::io("artifact_cleanup_failed", e))?;
            }
            fs::remove_dir(destination).map_err(|e| Error::io("artifact_cleanup_failed", e))?;
            self.sync_dir(
                destination
                    .parent()
                    .ok_or_else(|| Error::new("artifact_path_invalid"))?,
            )?;
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
    pub fn status(&self) -> Result<Value> {
        self.with_lock(|| {
            let index=self.read_index()?;
            let used=index.entries.values().try_fold(0u64,|total,e|total.checked_add(e.size_bytes).ok_or_else(||Error::new("artifact_capacity")))?;
            let pinned=index.entries.values().filter(|e|!e.pins().is_empty()).count();
            Ok(json!({"protocol":index.protocol,"max_bytes":index.max_bytes,"entry_count":index.entries.len(),"used_bytes":used,"pinned_object_count":pinned}))
        })
    }
}
