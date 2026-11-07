// SPDX-License-Identifier: AGPL-3.0-only
//! Exact no-model archive intake, not a general tar extractor.
use serde_json::Value;
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};
type Result<T> = std::result::Result<T, &'static str>;
struct OwnedPath {
    path: PathBuf,
    device: u64,
    inode: u64,
}
impl OwnedPath {
    fn capture(path: PathBuf) -> Result<Self> {
        let m = fs::symlink_metadata(&path).map_err(|_| "bootstrap_stage_unavailable")?;
        if m.file_type().is_symlink() || m.uid() != rustix::process::geteuid().as_raw() {
            return Err("bootstrap_stage_identity_invalid");
        }
        Ok(Self {
            path,
            device: m.dev(),
            inode: m.ino(),
        })
    }
    fn check(&self) -> Result<()> {
        let m = fs::symlink_metadata(&self.path).map_err(|_| "bootstrap_stage_unavailable")?;
        if m.file_type().is_symlink() || (m.dev(), m.ino()) != (self.device, self.inode) {
            return Err("bootstrap_stage_identity_changed");
        }
        Ok(())
    }
}
pub struct BootstrapStage {
    directory: OwnedPath,
    files: Vec<OwnedPath>,
    archive: Vec<u8>,
    removed: bool,
    directory_removed: bool,
}
impl BootstrapStage {
    pub fn create(path: &Path, scope: &Value, offer: &Value, archive: &[u8]) -> Result<Self> {
        protocol::managed::validate_binding(scope)
            .map_err(|_| "bootstrap_stage_binding_invalid")?;
        let run = scope["run_id"]
            .as_str()
            .ok_or("bootstrap_stage_binding_invalid")?;
        let deployment = scope["deployment_id"]
            .as_str()
            .ok_or("bootstrap_stage_binding_invalid")?;
        if scope
            .as_object()
            .is_none_or(|s| s.iter().any(|(k, v)| offer[k] != *v))
            || archive != runtime::observe::bootstrap_archive(run, deployment)
            || offer["archive_bytes"].as_u64() != Some(archive.len() as u64)
            || offer["archive_digest"] != protocol::artifact_grant::range_digest(archive)
        {
            return Err("offer_archive_digest_mismatch");
        }
        // Structural ustar offsets, not a policy allowance. The entire archive
        // already equals the sole canonical producer's bytes above.
        let octal = std::str::from_utf8(&archive[124..136])
            .map_err(|_| "bootstrap_stage_invalid")?
            .trim_end_matches('\0');
        let length = usize::from_str_radix(octal, 8).map_err(|_| "bootstrap_stage_invalid")?;
        let payload = archive
            .get(512..512 + length)
            .ok_or("bootstrap_stage_invalid")?;
        let value: Value =
            serde_json::from_slice(payload).map_err(|_| "bootstrap_stage_invalid")?;
        if value["protocol"] != "mycelium.a13.hello_bootstrap.v1"
            || value["model_load_authorized"] != false
            || value["run_id"] != run
            || value["deployment_id"] != deployment
        {
            return Err("bootstrap_model_load_not_authorized");
        }
        let parent = path.parent().ok_or("bootstrap_stage_invalid")?;
        if fs::canonicalize(parent).map_err(|_| "bootstrap_stage_invalid")? != parent {
            return Err("bootstrap_stage_invalid");
        }
        fs::DirBuilder::new()
            .mode(0o700)
            .create(path)
            .map_err(|_| "bootstrap_stage_create_failed")?;
        let mut this = Self {
            directory: OwnedPath::capture(path.to_owned())?,
            files: Vec::new(),
            archive: archive.to_vec(),
            removed: false,
            directory_removed: false,
        };
        for (name, bytes) in [("offer.tar", archive), ("bootstrap.json", payload)] {
            let path = path.join(name);
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)
                .map_err(|_| "bootstrap_stage_write_failed")?;
            this.files.push(OwnedPath::capture(path)?);
            file.write_all(bytes)
                .and_then(|()| file.sync_all())
                .map_err(|_| "bootstrap_stage_write_failed")?;
        }
        File::open(this.path())
            .and_then(|f| f.sync_all())
            .map_err(|_| "bootstrap_stage_write_failed")?;
        this.verify()?;
        Ok(this)
    }
    pub fn path(&self) -> &Path {
        &self.directory.path
    }
    pub fn verify(&self) -> Result<()> {
        self.directory.check()?;
        for f in &self.files {
            f.check()?;
        }
        if self.removed
            || self.files.len() != 2
            || fs::read(self.path().join("offer.tar")).map_err(|_| "bootstrap_stage_unavailable")?
                != self.archive
        {
            return Err("bootstrap_stage_changed");
        }
        let length = usize::from_str_radix(
            std::str::from_utf8(&self.archive[124..136])
                .map_err(|_| "bootstrap_stage_invalid")?
                .trim_end_matches('\0'),
            8,
        )
        .map_err(|_| "bootstrap_stage_invalid")?;
        if fs::read(self.path().join("bootstrap.json"))
            .map_err(|_| "bootstrap_stage_unavailable")?
            != self.archive[512..512 + length]
        {
            return Err("bootstrap_stage_changed");
        }
        Ok(())
    }
    pub fn remove(&mut self) -> Result<()> {
        if self.removed {
            return Ok(());
        }
        if !self.directory_removed {
            self.directory.check()?;
            while let Some(file) = self.files.last() {
                file.check()?;
                fs::remove_file(&file.path).map_err(|_| "bootstrap_stage_cleanup_unproven")?;
                self.files.pop();
            }
            self.directory.check()?;
            fs::remove_dir(self.path()).map_err(|_| "bootstrap_stage_cleanup_unproven")?;
            // Unlink is irreversible even if the following durability step fails.
            self.directory_removed = true;
        }
        // Never delete or acknowledge a replacement while durability is unproven.
        if !matches!(fs::symlink_metadata(self.path()), Err(error) if error.kind() == std::io::ErrorKind::NotFound)
        {
            return Err("bootstrap_stage_identity_changed");
        }
        File::open(
            self.path()
                .parent()
                .ok_or("bootstrap_stage_cleanup_unproven")?,
        )
        .and_then(|f| f.sync_all())
        .map_err(|_| "bootstrap_stage_cleanup_unproven")?;
        self.removed = true;
        Ok(())
    }
}
impl Drop for BootstrapStage {
    fn drop(&mut self) {
        if let Err(code) = self.remove() {
            eprintln!("bootstrap staging cleanup: {code}");
        }
    }
}
