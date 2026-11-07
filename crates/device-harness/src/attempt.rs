// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit attempt ownership and session storage. Parents must be private and single-writer.
use crate::{
    Fault, FaultKind, Result,
    preflight::ProcessIdentity,
    private_fs::{MarkerLease, create_private},
};
use serde_json::{Value, json};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};
pub enum Parallel {
    Quiet,
    CertAllowed,
}
pub struct Attempt {
    lease: MarkerLease,
    result: PathBuf,
}
#[derive(Debug)]
pub struct FinalizeFailure {
    pub first: Fault,
    pub cleanup: Option<Fault>,
}
fn error(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}
fn directory(path: &Path, uid: u32) -> Result<()> {
    if let Err(e) = fs::create_dir(path)
        && e.kind() != std::io::ErrorKind::AlreadyExists
    {
        return Err(error("attempt_directory_create_failed"));
    }
    // Existing directories are admitted only by the type/owner checks below.
    let m = fs::symlink_metadata(path).map_err(|_| error("attempt_directory_unobserved"))?;
    if !m.is_dir() || m.file_type().is_symlink() || m.uid() != uid {
        return Err(error("attempt_directory_invalid"));
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .map_err(|_| error("attempt_directory_mode_failed"))
}
fn unspent(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Ok(_) => Err(error("attempt_already_spent")),
        Err(_) => Err(error("attempt_path_unobserved")),
    }
}
fn quiet(markers: &Path, parallel: &Parallel, own: bool) -> Result<()> {
    for entry in fs::read_dir(markers).map_err(|_| error("attempt_markers_unobserved"))? {
        let entry = entry.map_err(|_| error("attempt_markers_unobserved"))?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.ends_with(".busy")
            && !(own && name == "DEVICE.busy")
            && !(matches!(parallel, Parallel::CertAllowed) && name == "CERT.busy")
        {
            return Err(Fault::new("preflight_busy_marker", FaultKind::Admission));
        }
    }
    Ok(())
}
impl Attempt {
    pub fn open(
        out: &Path,
        markers: &Path,
        kind: &str,
        process: ProcessIdentity,
        parallel: Parallel,
        uid: u32,
    ) -> Result<Self> {
        if !matches!(kind, "soak" | "stranger")
            || process.pid <= 1
            || process.birth.is_empty()
            || process.executable.is_empty()
        {
            return Err(error("attempt_identity_invalid"));
        }
        directory(out, uid)?;
        directory(markers, uid)?;
        let result = out.join(if kind == "soak" {
            "SOAK-RESULT.json"
        } else {
            "RESULT.json"
        });
        let log = out.join("events.jsonl");
        let pid = out.join("harness-pid.json");
        for path in [&result, &log, &pid] {
            unspent(path)?;
        }
        quiet(markers, &parallel, false)?;
        let marker = markers.join("DEVICE.busy");
        let lease = MarkerLease::acquire(&marker, &json!({"pid":process.pid,"job":out}))?;
        let record = json!({"pid":process.pid,"identity":crate::package::digest(format!("{} {}",process.birth,process.executable).as_bytes()),"process":process,"attempt":out,"kind":kind,"marker":marker,"awake_record":null});
        let setup = (|| -> Result<()> {
            quiet(markers, &parallel, true)?;
            create_private(&log, b"")?;
            create_private(
                &pid,
                &serde_json::to_vec(&record)
                    .map_err(|_| error("attempt_record_encoding_failed"))?,
            )?;
            Ok(())
        })();
        if let Err(e) = setup {
            lease.close()?;
            return Err(e);
        }
        Ok(Self { lease, result })
    }
    pub fn finish(self, result: &Value) -> std::result::Result<(), FinalizeFailure> {
        let write = serde_json::to_vec(&crate::observe::redact(result, &[]))
            .map_err(|_| error("attempt_result_encoding_failed"))
            .and_then(|bytes| create_private(&self.result, &bytes));
        let cleanup = self.lease.close();
        match (write, cleanup) {
            (Err(first), cleanup) => Err(FinalizeFailure {
                first,
                cleanup: cleanup.err(),
            }),
            (Ok(()), Err(first)) => Err(FinalizeFailure {
                first,
                cleanup: None,
            }),
            (Ok(()), Ok(())) => Ok(()),
        }
    }
    /// Failure diagnostics are supplied independently; an outer safety-fence panic
    /// is deliberately NOT caught and can never be mistaken for product refusal.
    pub fn execute(
        self,
        operation: impl FnOnce() -> Result<Value>,
        on_failure: impl FnOnce(Fault) -> Result<()>,
    ) -> std::result::Result<Value, FinalizeFailure> {
        let result = match operation() {
            Ok(v) => v,
            Err(e) => {
                let snapshot = on_failure(e);
                json!({"outcome":if e.incomplete(){"incomplete"}else{"failed"},"error":e.code,"incomplete":e.incomplete(),"product_failed":!e.incomplete(),"snapshot_error":snapshot.err().map(|e|e.code),"release_qualified":false})
            }
        };
        self.finish(&result)?;
        Ok(result)
    }
}
pub fn rewrite_session(path: &Path, uid: u32, bytes: &[u8]) -> Result<()> {
    if bytes.is_empty() || bytes.len() > 16384 {
        return Err(error("session_size_invalid"));
    }
    let mut f = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| error("session_open_refused"))?;
    let m = f.metadata().map_err(|_| error("session_metadata_failed"))?;
    if !m.is_file() || m.uid() != uid || m.nlink() != 1 {
        return Err(error("session_file_invalid"));
    }
    f.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|_| error("session_mode_failed"))?;
    f.set_len(0).map_err(|_| error("session_truncate_failed"))?;
    f.write_all(bytes)
        .map_err(|_| error("session_write_failed"))?;
    f.sync_all().map_err(|_| error("session_sync_failed"))
}
