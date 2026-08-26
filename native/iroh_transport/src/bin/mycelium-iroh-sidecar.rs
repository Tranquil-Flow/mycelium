// SPDX-License-Identifier: AGPL-3.0-or-later

use std::num::NonZeroUsize;
use std::os::fd::RawFd;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;
use mycelium_iroh_transport::sidecar::{
    DEFAULT_QUEUE_CAPACITY, SidecarConfig, log_event, read_bootstrap_secret, read_endpoint_secret,
    run_sidecar,
};

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
}

#[tokio::main]
async fn main() -> ExitCode {
    let arguments = Arguments::parse();
    let secret = match read_bootstrap_secret(arguments.bootstrap_fd) {
        Ok(secret) => secret,
        Err(_) => {
            log_event("bootstrap_rejected");
            return ExitCode::FAILURE;
        }
    };
    let endpoint_secret = match arguments.endpoint_secret_file.as_deref() {
        Some(path) => match read_endpoint_secret(path) {
            Ok(secret) => Some(secret),
            Err(_) => {
                log_event("endpoint_secret_rejected");
                return ExitCode::FAILURE;
            }
        },
        None => None,
    };
    let config = SidecarConfig {
        uds: arguments.uds,
        queue_capacity: arguments.queue_capacity,
        local_only: arguments.local_only,
        force_relay: arguments.force_relay,
        endpoint_secret,
    };
    match run_sidecar(config, secret).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => {
            log_event("fatal");
            ExitCode::FAILURE
        }
    }
}
