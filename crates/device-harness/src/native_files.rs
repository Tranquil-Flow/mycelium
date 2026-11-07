// SPDX-License-Identifier: AGPL-3.0-only
//! Native private-file implementation of the S14 [`Files`] seam. It reads and atomically
//! replaces files in two exclusively owned private directories and reports what it found;
//! policy (owner, 0600, single link, regular, size cap) stays in `NativeIo`, so a hostile
//! file is *reported*, never silently normalised.
//!
//! * `app_root` is the application's private state directory (`native-control.json`,
//!   `open-demo.html`); the harness only reads it.
//! * `session_dir` is the harness's own directory for the candidate-bound session record.
//!
//! Both must be absolute, canonical (no symlink component), owned by the caller's uid and
//! exactly 0700, and are re-identified (device+inode) before every operation. Files are
//! opened `O_NOFOLLOW|O_NONBLOCK` and judged from `fstat` of the open descriptor, so a FIFO
//! cannot block the harness and a swapped path cannot change what was measured.
//! Compare-then-rename of the session record is not an atomic compare-and-swap against a
//! hostile writer with the same uid; the directory's exclusive 0700 ownership is the guard.
//! Process and listener identity come from an injected [`Probe`], never from the endpoint.
use crate::{
    Fault, FaultKind, Result,
    native_io::{FileIdentity, Files, PrivateFile, Slot},
    native_os::Probe,
    native_session::{Binding, Observation},
    stranger::Candidate,
};
use budgets::Span;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::Instant,
};

/// v0.1.4 file names (mycelium_a13_macos/app.py: `native-control.json`, `open-demo.html`).
pub const CONTROL: &str = "native-control.json";
pub const LAUNCH: &str = "open-demo.html";
/// Harness-owned record; not a v0.1.4 name.
pub const SESSION: &str = "harness-native-session.json";

static SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}

/// The independently admitted facts a binding is made of. Hashes and the candidate come
/// from verified package evidence (L8/L11); this adapter only carries them.
#[derive(Clone, Debug)]
pub struct Admitted {
    pub candidate: Candidate,
    pub pid: u32,
    pub launch_epoch_ms: u64,
    pub desktop_manifest_sha256: String,
    pub native_inputs_sha256: String,
}

struct Root {
    path: PathBuf,
    device: u64,
    inode: u64,
}
impl Root {
    fn open(path: &Path, uid: u32) -> Result<Self> {
        let invalid = || fault("native_root_invalid");
        let m = fs::symlink_metadata(path).map_err(|_| invalid())?;
        if !path.is_absolute()
            || m.file_type().is_symlink()
            || !m.is_dir()
            || m.uid() != uid
            || m.mode() & 0o7777 != 0o700
            || fs::canonicalize(path).map_err(|_| invalid())? != path
        {
            return Err(invalid());
        }
        Ok(Self {
            path: path.to_path_buf(),
            device: m.dev(),
            inode: m.ino(),
        })
    }
    fn check(&self, uid: u32) -> Result<()> {
        let changed = || fault("native_root_changed");
        let m = fs::symlink_metadata(&self.path).map_err(|_| changed())?;
        if m.file_type().is_symlink()
            || !m.is_dir()
            || (m.dev(), m.ino()) != (self.device, self.inode)
            || m.uid() != uid
            || m.mode() & 0o7777 != 0o700
        {
            return Err(changed());
        }
        Ok(())
    }
}

/// One shared deadline across the sub-steps of an operation.
pub(crate) struct Deadline(Instant, u64);
impl Deadline {
    pub(crate) fn new(bound: Span) -> Self {
        Self(Instant::now(), bound.millis())
    }
    pub(crate) fn remaining(&self) -> Result<Span> {
        let spent = u64::try_from(self.0.elapsed().as_millis()).unwrap_or(u64::MAX);
        self.1
            .checked_sub(spent)
            .filter(|n| *n > 0)
            .map(Span::from_millis)
            .ok_or(fault("native_io_timeout"))
    }
}

pub struct NativeFiles<P> {
    app: Root,
    session: Root,
    uid: u32,
    admitted: Admitted,
    probe: P,
}
impl<P: Probe> NativeFiles<P> {
    pub fn new(
        app_root: &Path,
        session_dir: &Path,
        uid: u32,
        admitted: Admitted,
        probe: P,
    ) -> Result<Self> {
        let app = Root::open(app_root, uid)?;
        let session = Root::open(session_dir, uid)?;
        if (app.device, app.inode) == (session.device, session.inode) {
            // The harness record must not live in the application's directory.
            return Err(fault("native_root_invalid"));
        }
        Ok(Self {
            app,
            session,
            uid,
            admitted,
            probe,
        })
    }
    fn root(&self, slot: Slot) -> (&Root, &'static str) {
        match slot {
            Slot::Control => (&self.app, CONTROL),
            Slot::Launch => (&self.app, LAUNCH),
            Slot::Session => (&self.session, SESSION),
        }
    }
}

fn millis(m: &fs::Metadata) -> Result<u64> {
    let seconds = u64::try_from(m.mtime()).map_err(|_| fault("native_file_time_invalid"))?;
    let nanos = u64::try_from(m.mtime_nsec()).map_err(|_| fault("native_file_time_invalid"))?;
    seconds
        .checked_mul(1000)
        .and_then(|s| s.checked_add(nanos / 1_000_000))
        .ok_or(fault("native_file_time_invalid"))
}
fn flagged(symlink: bool) -> PrivateFile {
    PrivateFile {
        identity: FileIdentity {
            device: 0,
            inode: 0,
        },
        uid: u32::MAX,
        mode: 0,
        links: 0,
        regular: false,
        symlink,
        modified_ms: 0,
        bytes: Vec::new(),
    }
}
/// `Ok(None)` when absent; a final symlink is the distinct `native_file_is_symlink`.
fn open(path: &Path) -> Result<Option<File>> {
    match OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
    {
        Ok(f) => Ok(Some(f)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) if e.raw_os_error() == Some(libc::ELOOP) => Err(fault("native_file_is_symlink")),
        Err(_) => Err(fault("native_file_open_failed")),
    }
}

impl<P: Probe> Files for NativeFiles<P> {
    fn observe(&mut self, bound: Span) -> Result<Observation> {
        let deadline = Deadline::new(bound);
        self.app.check(self.uid)?;
        let control = match open(&self.app.path.join(CONTROL)) {
            Ok(Some(f)) => f,
            Ok(None) => return Err(fault("native_control_missing")),
            Err(_) => return Err(fault("native_private_file_invalid")),
        };
        let meta = control
            .metadata()
            .map_err(|_| fault("native_file_metadata_failed"))?;
        if !meta.is_file() {
            return Err(fault("native_private_file_invalid"));
        }
        let control_modified_ms = millis(&meta)?;
        let pid = self.admitted.pid;
        let process = self.probe.process(pid, deadline.remaining()?)?;
        if process.pid != pid {
            return Err(fault("native_process_identity_mismatch"));
        }
        let port = self.probe.listener_port(pid, deadline.remaining()?)?;
        deadline.remaining()?;
        Ok(Observation {
            binding: Binding {
                candidate: self.admitted.candidate.clone(),
                process,
                launch_epoch_ms: self.admitted.launch_epoch_ms,
                desktop_manifest_sha256: self.admitted.desktop_manifest_sha256.clone(),
                native_inputs_sha256: self.admitted.native_inputs_sha256.clone(),
            },
            control_modified_ms,
            port,
        })
    }

    fn read(&mut self, slot: Slot, max_bytes: u64, bound: Span) -> Result<Option<PrivateFile>> {
        let deadline = Deadline::new(bound);
        let (root, name) = self.root(slot);
        root.check(self.uid)?;
        let file = match open(&root.path.join(name)) {
            Ok(Some(f)) => f,
            Ok(None) => return Ok(None),
            Err(e) if e.code == "native_file_is_symlink" => return Ok(Some(flagged(true))),
            Err(e) => return Err(e),
        };
        let m = file
            .metadata()
            .map_err(|_| fault("native_file_metadata_failed"))?;
        if !m.is_file() {
            return Ok(Some(flagged(false)));
        }
        let mut bytes = Vec::new();
        // The read itself is bounded: the cap plus one byte lets the caller refuse. A
        // source-unbounded cap (`SOURCE_UNBOUNDED_READ`, u64::MAX; RU-014, V03-L12-1)
        // saturates and reads to EOF, exactly like v0.1.4.
        file.take(max_bytes.saturating_add(1))
            .read_to_end(&mut bytes)
            .map_err(|_| fault("native_file_read_failed"))?;
        deadline.remaining()?;
        Ok(Some(PrivateFile {
            identity: FileIdentity {
                device: m.dev(),
                inode: m.ino(),
            },
            uid: m.uid(),
            mode: m.mode() & 0o7777,
            links: m.nlink(),
            regular: true,
            symlink: false,
            modified_ms: millis(&m)?,
            bytes,
        }))
    }

    fn replace_session(
        &mut self,
        old: Option<FileIdentity>,
        bytes: &[u8],
        bound: Span,
    ) -> Result<()> {
        let deadline = Deadline::new(bound);
        self.session.check(self.uid)?;
        let dir = self.session.path.clone();
        let target = dir.join(SESSION);
        match (fs::symlink_metadata(&target), old) {
            (Err(e), None) if e.kind() == std::io::ErrorKind::NotFound => {}
            (Ok(m), Some(id)) if m.is_file() && (m.dev(), m.ino()) == (id.device, id.inode) => {}
            (Ok(m), _) if !m.is_file() => return Err(fault("native_session_target_invalid")),
            _ => return Err(fault("native_session_identity_changed")),
        }
        let temp = dir.join(format!(
            ".{SESSION}.{}.{}.tmp",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let result = self.write_then_swap(&temp, &target, bytes, &deadline);
        if result.is_err() {
            // After a successful rename the temp name no longer exists. A residue that
            // cannot be removed is reported (Cleanup) instead of hidden behind the cause.
            let removed = fs::remove_file(&temp);
            if removed
                .as_ref()
                .is_err_and(|e| e.kind() != std::io::ErrorKind::NotFound)
            {
                return Err(Fault::new(
                    "native_session_temp_cleanup_failed",
                    FaultKind::Cleanup,
                ));
            }
        }
        result
    }
}
impl<P> NativeFiles<P> {
    fn write_then_swap(
        &self,
        temp: &Path,
        target: &Path,
        bytes: &[u8],
        deadline: &Deadline,
    ) -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(temp)
            .map_err(|_| fault("native_session_write_failed"))?;
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|_| fault("native_session_write_failed"))?;
        let written = file
            .metadata()
            .map_err(|_| fault("native_session_write_failed"))?;
        file.write_all(bytes)
            .and_then(|()| file.sync_all())
            .map_err(|_| fault("native_session_write_failed"))?;
        deadline.remaining()?;
        // Re-prove the parent is still the one we admitted right before the swap.
        self.session.check(self.uid)?;
        fs::rename(temp, target).map_err(|_| fault("native_session_replace_failed"))?;
        let now =
            fs::symlink_metadata(target).map_err(|_| fault("native_session_replace_unverified"))?;
        if !now.is_file()
            || (now.dev(), now.ino()) != (written.dev(), written.ino())
            || now.nlink() != 1
            || now.uid() != self.uid
            || now.mode() & 0o7777 != 0o600
        {
            return Err(fault("native_session_replace_unverified"));
        }
        File::open(&self.session.path)
            .and_then(|parent| parent.sync_all())
            .map_err(|_| fault("native_session_parent_sync_failed"))?;
        deadline.remaining()?;
        Ok(())
    }
}
