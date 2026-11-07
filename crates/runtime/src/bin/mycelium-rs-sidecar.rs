// SPDX-License-Identifier: AGPL-3.0-only
//! `mycelium-rs-sidecar`: the Rust counterpart of v0.1.4's `mycelium-iroh-sidecar` binary
//! (`--uds PATH --bootstrap-fd FD --local-only ...`). See `runtime::sidecar_main`.
#[cfg(unix)]
fn main() -> std::process::ExitCode {
    use runtime::{
        sidecar_artifact::DurableArtifactBackend,
        sidecar_main::{ProcessOutput, sidecar_main},
    };
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = sidecar_main(
        &args,
        std::sync::Arc::new(ProcessOutput),
        &DurableArtifactBackend::shipped(),
    );
    std::process::ExitCode::from(u8::try_from(code).unwrap_or(1))
}
#[cfg(not(unix))]
fn main() -> std::process::ExitCode {
    std::process::ExitCode::FAILURE
}
