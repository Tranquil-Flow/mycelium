// SPDX-License-Identifier: AGPL-3.0-only

use std::num::NonZeroUsize;
#[cfg(unix)]
use std::os::fd::RawFd;
use std::path::PathBuf;
use std::process::ExitCode;
#[cfg(any(windows, feature = "loopback-test"))]
use std::sync::Arc;

use clap::{Args, Parser};
#[cfg(any(windows, feature = "loopback-test"))]
use mycelium_iroh_transport::local_ipc::{SystemConnectionTable, read_bootstrap_stdin};
#[cfg(unix)]
use mycelium_iroh_transport::sidecar::read_bootstrap_secret;
use mycelium_iroh_transport::sidecar::{
    ArtifactSourceConfig, DEFAULT_QUEUE_CAPACITY, LocalChannel, SidecarConfig, SidecarError,
    log_event, read_endpoint_secret, run_sidecar,
};
use zeroize::Zeroizing;

/// POSIX hosts: the Router reaches the sidecar over a Unix-domain socket.
#[cfg(all(unix, not(feature = "loopback-test")))]
#[derive(Debug, Parser)]
#[command(
    name = "mycelium-iroh-sidecar",
    version,
    disable_help_subcommand = true
)]
struct Arguments {
    /// Unix-domain socket used by the local Router process.
    #[arg(long, value_name = "PATH")]
    uds: PathBuf,

    /// Inherited pipe descriptor containing exactly 32 bootstrap bytes.
    #[arg(long, value_name = "FD")]
    bootstrap_fd: RawFd,

    #[command(flatten)]
    common: CommonArguments,
}

#[cfg(all(unix, not(feature = "loopback-test")))]
impl Arguments {
    fn channel(&self) -> Result<(LocalChannel, Zeroizing<[u8; 32]>), SidecarError> {
        let secret = read_bootstrap_secret(self.bootstrap_fd)?;
        Ok((LocalChannel::UnixSocket(self.uds.clone()), secret))
    }
}

/// Test builds on POSIX hosts: either the Unix socket or the loopback channel.
#[cfg(all(unix, feature = "loopback-test"))]
#[derive(Debug, Parser)]
#[command(
    name = "mycelium-iroh-sidecar",
    version,
    disable_help_subcommand = true
)]
struct Arguments {
    /// Unix-domain socket used by the local Router process.
    #[arg(
        long,
        value_name = "PATH",
        required_unless_present = "loopback",
        conflicts_with = "loopback",
        requires = "bootstrap_fd"
    )]
    uds: Option<PathBuf>,

    /// Inherited pipe descriptor containing exactly 32 bootstrap bytes.
    #[arg(long, value_name = "FD", conflicts_with = "loopback", requires = "uds")]
    bootstrap_fd: Option<RawFd>,

    #[command(flatten)]
    loopback: LoopbackArguments,

    #[command(flatten)]
    common: CommonArguments,
}

#[cfg(all(unix, feature = "loopback-test"))]
impl Arguments {
    fn channel(&self) -> Result<(LocalChannel, Zeroizing<[u8; 32]>), SidecarError> {
        match (&self.uds, self.bootstrap_fd) {
            (Some(uds), Some(fd)) => {
                let secret = read_bootstrap_secret(fd)?;
                Ok((LocalChannel::UnixSocket(uds.clone()), secret))
            }
            _ => self.loopback.channel(),
        }
    }
}

/// Windows hosts: the Router reaches the sidecar over authenticated loopback.
#[cfg(windows)]
#[derive(Debug, Parser)]
#[command(
    name = "mycelium-iroh-sidecar",
    version,
    disable_help_subcommand = true
)]
struct Arguments {
    #[command(flatten)]
    loopback: LoopbackArguments,

    #[command(flatten)]
    common: CommonArguments,
}

#[cfg(windows)]
impl Arguments {
    fn channel(&self) -> Result<(LocalChannel, Zeroizing<[u8; 32]>), SidecarError> {
        self.loopback.channel()
    }
}

#[cfg(any(windows, feature = "loopback-test"))]
#[derive(Debug, Args)]
struct LoopbackArguments {
    /// Serve the local Router on an ephemeral 127.0.0.1 TCP port, announced
    /// as `local_port` in the ready record.
    #[arg(long, requires_all = ["bootstrap_stdin", "allowed_client_pid"])]
    #[cfg_attr(windows, arg(required = true))]
    loopback: bool,

    /// Read exactly 32 bootstrap bytes from stdin, which must be a pipe
    /// closed right after them.
    #[arg(long, requires = "loopback")]
    bootstrap_stdin: bool,

    /// Only the process with this id may own a local connection's client end.
    #[arg(
        long,
        value_name = "PID",
        requires = "loopback",
        value_parser = clap::value_parser!(u32).range(1..)
    )]
    allowed_client_pid: Option<u32>,
}

#[cfg(any(windows, feature = "loopback-test"))]
impl LoopbackArguments {
    fn channel(&self) -> Result<(LocalChannel, Zeroizing<[u8; 32]>), SidecarError> {
        let (true, true, Some(allowed_client_pid)) =
            (self.loopback, self.bootstrap_stdin, self.allowed_client_pid)
        else {
            return Err(SidecarError::InvalidBootstrapFd);
        };
        let secret = read_bootstrap_stdin()?;
        Ok((
            LocalChannel::Loopback {
                allowed_client_pid,
                owners: Arc::new(SystemConnectionTable),
            },
            secret,
        ))
    }
}

#[derive(Debug, Args)]
struct CommonArguments {
    /// Host-local raw 32-byte Iroh identity key; never copied by the controller.
    #[arg(long, value_name = "PATH")]
    endpoint_secret_file: Option<PathBuf>,

    /// Disable relays and bind iroh only to the IPv4 loopback interface.
    #[arg(long)]
    local_only: bool,

    /// Retain production relays while disabling every direct IP transport.
    #[arg(long, conflicts_with = "local_only")]
    force_relay: bool,

    /// Independent capacity of the inbound and outbound message queues.
    #[arg(long, default_value_t = NonZeroUsize::new(DEFAULT_QUEUE_CAPACITY).unwrap())]
    queue_capacity: NonZeroUsize,

    /// Also serve phone stage objects (`<sha256 hex>` files in this directory)
    /// on the ALPN `mycelium.artifact_chunk.v1` under seed grants.
    #[arg(long, value_name = "DIR", requires_all = ["artifact_seed_key", "artifact_state", "artifact_signing_key_file"])]
    artifact_objects: Option<PathBuf>,

    /// The seed's public key record (JSON); grants must verify under it.
    #[arg(long, value_name = "PATH", requires = "artifact_objects")]
    artifact_seed_key: Option<PathBuf>,

    /// Directory of the durable artifact admission journal.
    #[arg(long, value_name = "DIR", requires = "artifact_objects")]
    artifact_state: Option<PathBuf>,

    /// Host-local raw 32-byte receipt-signing key (0600, like the endpoint key).
    #[arg(long, value_name = "PATH", requires = "artifact_objects")]
    artifact_signing_key_file: Option<PathBuf>,
}

fn artifact_config(common: &CommonArguments) -> Result<Option<ArtifactSourceConfig>, ()> {
    let Some(objects) = common.artifact_objects.clone() else { return Ok(None) };
    let (Some(seed_key), Some(state_dir), Some(key_file)) =
        (common.artifact_seed_key.as_deref(), common.artifact_state.clone(), common.artifact_signing_key_file.as_deref())
    else {
        return Err(());
    };
    let seed_key = std::fs::read(seed_key).map_err(|_| ())?;
    let seed_key: serde_json::Value = serde_json::from_slice(&seed_key).map_err(|_| ())?;
    let signing_secret = read_endpoint_secret(key_file).map_err(|_| ())?;
    Ok(Some(ArtifactSourceConfig { objects, seed_key, state_dir, signing_secret }))
}

#[tokio::main]
async fn main() -> ExitCode {
    let arguments = Arguments::parse();
    let (channel, secret) = match arguments.channel() {
        Ok(selected) => selected,
        Err(_) => {
            log_event("bootstrap_rejected");
            return ExitCode::FAILURE;
        }
    };
    let common = arguments.common;
    let endpoint_secret = match common.endpoint_secret_file.as_deref() {
        Some(path) => match read_endpoint_secret(path) {
            Ok(secret) => Some(secret),
            Err(_) => {
                log_event("endpoint_secret_rejected");
                return ExitCode::FAILURE;
            }
        },
        None => None,
    };
    let artifact = match artifact_config(&common) {
        Ok(artifact) => artifact,
        Err(()) => {
            log_event("artifact_source_rejected");
            return ExitCode::FAILURE;
        }
    };
    let config = SidecarConfig {
        channel,
        queue_capacity: common.queue_capacity,
        local_only: common.local_only,
        force_relay: common.force_relay,
        endpoint_secret,
        artifact,
    };
    match run_sidecar(config, secret).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => {
            log_event("fatal");
            ExitCode::FAILURE
        }
    }
}
