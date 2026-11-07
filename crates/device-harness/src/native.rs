// SPDX-License-Identifier: AGPL-3.0-only
//! Unix leaf adapter. It only signals its own unreaped helper child on timeout.
//! Product app shutdown is deliberately NOT implemented here: use Apple Events.
//! No device or app is spawned by the fake-only lane qualification suite.
use crate::{
    Fault, FaultKind, Result,
    effects::{Clock, Command, Output, Transport},
    process::{self, Process},
};
use budgets::Span;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::{
    fs::{self, File, OpenOptions},
    io::Read,
    path::{Path, PathBuf},
    process::{Child, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};
static SEQUENCE: AtomicU64 = AtomicU64::new(0);
fn observation(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}
pub struct RealClock(Instant);
impl Default for RealClock {
    fn default() -> Self {
        Self(Instant::now())
    }
}
impl Clock for RealClock {
    fn now(&self) -> u64 {
        u64::try_from(self.0.elapsed().as_millis()).unwrap_or(u64::MAX)
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        std::thread::sleep(Duration::from_millis(s.millis()));
        Ok(())
    }
}
pub struct NativeTransport {
    pub scratch: PathBuf,
    pub uid: u32,
    pub poll: Span,
    pub max_output_bytes: u64,
    pub diagnostics: Vec<Fault>,
}
impl Transport for NativeTransport {
    fn execute(&mut self, command: &Command, bound: Span) -> Result<Output> {
        let m = fs::symlink_metadata(&self.scratch)
            .map_err(|_| observation("capture_directory_unavailable"))?;
        if !m.is_dir()
            || m.file_type().is_symlink()
            || m.uid() != self.uid
            || m.permissions().mode() & 0o077 != 0
            || self.max_output_bytes == 0
        {
            return Err(observation("capture_directory_not_private"));
        }
        let mut p = NativeChild {
            root: self.scratch.clone(),
            child: None,
            captures: Vec::new(),
            max: self.max_output_bytes,
        };
        process::supervise(
            &mut p,
            &mut RealClock::default(),
            command,
            bound,
            self.poll,
            &mut self.diagnostics,
        )
    }
}
struct NativeChild {
    root: PathBuf,
    child: Option<Child>,
    captures: Vec<PathBuf>,
    max: u64,
}
impl NativeChild {
    fn capture(&mut self, suffix: &str) -> Result<File> {
        let seq = SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let p = self.root.join(format!(
            "capture-{}-{seq}.{suffix}.private",
            std::process::id()
        ));
        let f = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&p)
            .map_err(|_| observation("capture_create_failed"))?;
        self.captures.push(p);
        Ok(f)
    }
    fn read(&self, path: &Path) -> Result<String> {
        let mut bytes = Vec::new();
        let f = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(path)
            .map_err(|_| observation("capture_open_failed"))?;
        f.take(
            self.max
                .checked_add(1)
                .ok_or(observation("capture_bound_invalid"))?,
        )
        .read_to_end(&mut bytes)
        .map_err(|_| observation("capture_read_failed"))?;
        if bytes.len() as u64 > self.max {
            return Err(observation("capture_bound_exceeded"));
        }
        String::from_utf8(bytes).map_err(|_| observation("capture_invalid_utf8"))
    }
}
impl Process for NativeChild {
    fn start(&mut self, command: &Command) -> Result<()> {
        let out = self.capture("stdout")?;
        let err = self.capture("stderr")?;
        let mut spawn = std::process::Command::new(&command.program);
        spawn
            .args(&command.args)
            .stdin(Stdio::null())
            .stdout(out)
            .stderr(err);
        // v0.1.4 used `close_fds=True`: a probe never kept a parent socket. macOS cannot
        // create sockets close-on-exec atomically, so the one audited L6 boundary must be
        // the LAST registration on the command (after every other pre_exec). A refusal
        // leaves the already-created capture files to `cleanup`; nothing was spawned.
        node::process::confine_descriptors(&mut spawn)
            .map_err(|_| observation("descriptor_confinement_failed"))?;
        self.child = Some(
            spawn
                .spawn()
                .map_err(|_| observation("command_spawn_failed"))?,
        );
        Ok(())
    }
    fn poll(&mut self) -> Result<Option<Output>> {
        for p in &self.captures {
            if fs::metadata(p)
                .map_err(|_| observation("capture_metadata_failed"))?
                .len()
                > self.max
            {
                return Err(observation("capture_bound_exceeded"));
            }
        }
        let child = self
            .child
            .as_mut()
            .ok_or(observation("child_not_started"))?;
        match child
            .try_wait()
            .map_err(|_| observation("child_observation_failed"))?
        {
            None => Ok(None),
            Some(status) => Ok(Some(Output {
                rc: status.code().unwrap_or(-1),
                stdout: self.read(&self.captures[0])?,
                stderr: self.read(&self.captures[1])?,
            })),
        }
    }
    fn terminate_owned(&mut self) -> Result<()> {
        if let Some(child) = &mut self.child
            && child
                .try_wait()
                .map_err(|_| observation("child_observation_failed"))?
                .is_none()
        {
            child
                .kill()
                .map_err(|_| Fault::new("owned_helper_stop_failed", FaultKind::Cleanup))?;
            child
                .wait()
                .map_err(|_| Fault::new("owned_helper_reap_failed", FaultKind::Cleanup))?;
        }
        Ok(())
    }
    fn cleanup(&mut self) -> Result<()> {
        let mut first = None;
        for p in &self.captures {
            if fs::remove_file(p).is_err() {
                first.get_or_insert(Fault::new("capture_cleanup_failed", FaultKind::Cleanup));
            }
        }
        match first {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }
}
