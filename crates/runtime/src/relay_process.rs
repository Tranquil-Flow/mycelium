// SPDX-License-Identifier: AGPL-3.0-only
//! The quick-tunnel process as a [`RelayResource`] over L6's single spawn boundary
//! ([`NativeChild`]): `TemporaryHttpsRelay` of v0.1.4 (`mycelium_a13_macos/public_relay.py:296-450
//! @ f4e366e9`) with the exact owned child, not a `Popen` plus `terminate`.
//!
//! - [`quick_tunnel_command`] is the oracle's argv (`:339-341`) and scrubbed environment
//!   (`:333-337`: no `TUNNEL_*`, `CLOUDFLARE_*`, `CF_*`, no `NO_TLS_VERIFY`, `HOME` = the isolated
//!   home). One divergence: `--logfile <home>/relay.log` is added, because the spawn boundary
//!   nulls the child's stdio and the oracle reads the origin from the merged stdout pipe.
//! - [`provider_origin`] is `provider_event` (`:243-247`), pinned by its executed output.
//! - [`NativeRelay::stop`] is Ok only when [`WorkerAdapter::observe`] reports the pid gone (reaped)
//!   AND the exclusive root removed, and the isolated home and the base are removed too; a kill
//!   that was merely sent is never a stop. Foreign residue is reported, never deleted.
//!
//! The wait after the kill is bounded by [`RelayLimits`] (shipped: the oracle's
//! `process.wait(5)` after `kill`, `:447`); there is no `terminate` + 8 s grace because the spawn
//! boundary offers only the kill.
use crate::relay::RelayResource;
use budgets::{BudgetId as B, Entries, Refusal};
use node::{WorkerAdapter, WorkerError, process::NativeChild};
use std::{
    ffi::OsString,
    fs,
    io::Read,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};

const SCHEME: &str = "https://";
const SUFFIX: &str = ".trycloudflare.com";

/// `re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)`: the leftmost match's text.
pub fn provider_origin(line: &str) -> Option<String> {
    let bytes = line.as_bytes();
    let mut start = 0;
    while let Some(found) = line.get(start..).and_then(|rest| rest.find(SCHEME)) {
        let label_start = start + found + SCHEME.len();
        let label_len = bytes
            .get(label_start..)?
            .iter()
            .take_while(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || **byte == b'-')
            .count();
        let label_end = label_start + label_len;
        if label_len > 0
            && line
                .get(label_end..)
                .is_some_and(|rest| rest.starts_with(SUFFIX))
        {
            return line
                .get(start + found..label_end + SUFFIX.len())
                .map(str::to_owned);
        }
        start += found + 1;
    }
    None
}

/// First origin in a tunnel log, line by line. A line longer than `line_ceiling` ends the scan
/// as the oracle does (it terminates the process: `:347`).
pub fn origin_from_log(path: &Path, line_ceiling: usize) -> Result<Option<String>, &'static str> {
    let mut text = Vec::new();
    match fs::File::open(path) {
        Ok(file) => {
            file.take(
                u64::try_from(line_ceiling)
                    .unwrap_or(u64::MAX)
                    .saturating_mul(64),
            )
            .read_to_end(&mut text)
            .map_err(|_| "relay_log_unreadable")?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("relay_log_unreadable"),
    }
    for line in text.split(|byte| *byte == b'\n') {
        if line.len() > line_ceiling {
            return Err("relay_log_line_too_long");
        }
        if let Some(origin) = provider_origin(&String::from_utf8_lossy(line)) {
            return Ok(Some(origin));
        }
    }
    Ok(None)
}

/// The tunnel client's command: oracle argv, `HOME` isolated, provider credentials scrubbed.
pub fn quick_tunnel_command(
    binary: &Path,
    origin: &str,
    home: &Path,
    log_file: &Path,
    parent_env: Vec<(OsString, OsString)>,
) -> Command {
    let mut command = Command::new(binary);
    command
        .args(["--no-autoupdate", "tunnel", "--url", origin])
        .args(["--protocol", "http2", "--metrics", "127.0.0.1:0"])
        .args(["--grace-period", "5s", "--management-diagnostics=false"])
        .arg("--logfile")
        .arg(log_file)
        .env_clear();
    for (key, value) in parent_env {
        let name = key.to_string_lossy();
        let scrubbed = name.starts_with("TUNNEL_")
            || name.starts_with("CLOUDFLARE_")
            || name.starts_with("CF_")
            || name == "NO_TLS_VERIFY"
            || name == "HOME";
        if !scrubbed {
            command.env(key, value);
        }
    }
    command.env("HOME", home);
    command
}

#[derive(Clone, Copy, Debug)]
pub struct RelayLimits {
    stop_wait: Duration,
    poll: Duration,
}
impl RelayLimits {
    pub const fn new(stop_wait: Duration, poll: Duration) -> Self {
        Self { stop_wait, poll }
    }
    /// The oracle's bounds: `wait(5)` after the kill, polled at the relay poll interval.
    pub fn shipped() -> Result<Self, Refusal> {
        Ok(Self {
            stop_wait: Duration::from_millis(
                B::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfProcessWaitL447
                    .span(Entries::ZERO)?
                    .millis(),
            ),
            poll: Duration::from_millis(B::RelayPoll.span(Entries::ZERO)?.millis()),
        })
    }
}

pub struct NativeRelay {
    child: NativeChild,
    base: PathBuf,
    home: PathBuf,
    limits: RelayLimits,
    stopped: bool,
}
impl NativeRelay {
    /// `base` must not exist: it is created owner-only and holds the child's exclusive root
    /// (`base/relay`, created by the spawn boundary) and the isolated home (`base/home`). A
    /// failed start leaves nothing behind.
    pub fn start(
        command: &mut Command,
        base: &Path,
        limits: RelayLimits,
    ) -> Result<Self, WorkerError> {
        fs::DirBuilder::new().mode(0o700).create(base)?;
        let home = base.join("home");
        let started = fs::DirBuilder::new()
            .mode(0o700)
            .create(&home)
            .map_err(WorkerError::from)
            .and_then(|()| NativeChild::spawn(command, &base.join("relay")));
        match started {
            Ok(child) => Ok(Self {
                child,
                base: base.to_path_buf(),
                home,
                limits,
                stopped: false,
            }),
            Err(error) => {
                if fs::remove_dir(&home).is_err() || fs::remove_dir(base).is_err() {
                    // Not ours to force: a directory that will not go is reported by the error.
                }
                Err(error)
            }
        }
    }
    pub fn pid(&self) -> u32 {
        self.child.pid()
    }
    pub fn home(&self) -> &Path {
        &self.home
    }
    /// Where `quick_tunnel_command` was told to log.
    pub fn log_file(&self) -> PathBuf {
        self.home.join("relay.log")
    }
}
impl RelayResource for NativeRelay {
    fn stop(&mut self) -> Result<(), &'static str> {
        if self.stopped {
            return Ok(());
        }
        self.child
            .request_stop()
            .map_err(|_| "relay_stop_not_sent")?;
        let started = Instant::now();
        loop {
            match self.child.observe() {
                Ok(residue) if residue.process_gone && residue.resources_gone => break,
                Ok(_) => {}
                // The process is gone but its exclusive root is not removable (foreign
                // residue, or the directory identity changed): reported, never forced.
                Err(_) => return Err("relay_resource_residue"),
            }
            if started.elapsed() >= self.limits.stop_wait {
                return Err("relay_stop_unproven");
            }
            std::thread::sleep(self.limits.poll);
        }
        // The process is gone and reaped: its isolated home is ours to remove, then the base.
        fs::remove_dir_all(&self.home).map_err(|_| "relay_home_residue")?;
        fs::remove_dir(&self.base).map_err(|_| "relay_base_residue")?;
        self.stopped = true;
        Ok(())
    }
}
