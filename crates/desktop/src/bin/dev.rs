// SPDX-License-Identifier: AGPL-3.0-only
//! Explicitly fake, no product state, no worker, no model, no HTTP listener.
use budgets::Span;
use desktop::{
    channel::Request,
    lifecycle::{Cleanup, CleanupError, Poll, quit_allowance},
    services::{ServiceRequest, Services, adapt},
    shell::{Core, Reply},
};
use std::{process::ExitCode, sync::Arc};
struct Unavailable;
impl Core for Unavailable {
    fn submit(&self, _request: Request, complete: Box<dyn FnOnce(Reply) + Send>) {
        complete(Reply::unavailable());
    }
}
struct EmptyCleanup;
impl Cleanup for EmptyCleanup {
    fn begin(&mut self, _deadline: Span) -> Result<(), CleanupError> {
        println!("L11_FAKE_CLEANUP_BEGIN");
        Ok(())
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        Ok(Poll::Verified)
    }
}
/// Test fixture for the unproven-cleanup exit: begin succeeds, the receipt poll then fails, as a
/// supervisor whose owned workers could not be proven gone.
struct UnprovenCleanup;
impl Cleanup for UnprovenCleanup {
    fn begin(&mut self, _deadline: Span) -> Result<(), CleanupError> {
        println!("L11_FAKE_CLEANUP_BEGIN");
        Ok(())
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        Err(CleanupError::Poll)
    }
}
/// Test fixture for RU-032: a teardown that never finishes (begin succeeds, the receipt never
/// arrives), as v0.1.4's run whose teardown outlasts `run.join(60)`.
struct SlowCleanup;
impl Cleanup for SlowCleanup {
    fn begin(&mut self, _deadline: Span) -> Result<(), CleanupError> {
        println!("L11_FAKE_CLEANUP_BEGIN");
        Ok(())
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        Ok(Poll::Pending)
    }
}
struct DevelopmentServices;
impl Services for DevelopmentServices {
    fn submit(&self, _request: ServiceRequest, complete: desktop::core::Completion) {
        complete(Reply::unavailable());
    }
    fn begin_shutdown(&self, deadline: std::time::Instant) -> Result<(), CleanupError> {
        if deadline <= std::time::Instant::now() {
            return Err(CleanupError::Deadline);
        }
        println!("L11_FAKE_CLEANUP_BEGIN");
        Ok(())
    }
    fn poll_shutdown(&self) -> Result<Poll, CleanupError> {
        Ok(Poll::Verified)
    }
}
fn run() -> Result<(), String> {
    if !cfg!(debug_assertions) {
        return Err("development_shell_refused_in_release".into());
    }
    let args: Vec<_> = std::env::args().skip(1).collect();
    #[cfg(debug_assertions)]
    if let [isolated, option, path, cap_option, capacity] = args.as_slice()
        && isolated == "--isolated-dev"
        && option == "--probe-script"
        && cap_option == "--service-capacity"
    {
        let capacity = capacity.parse().map_err(|_| "invalid_service_capacity")?;
        let script = std::fs::read_to_string(path)
            .map_err(|e| format!("probe_script_read:{:?}", e.kind()))?;
        let budget = quit_allowance().map_err(|e| format!("dev_budget:{e:?}"))?;
        let (core, cleanup) = adapt(Arc::new(DevelopmentServices), capacity);
        return desktop::shell::run_development_with_probe(core, cleanup, budget, script);
    }
    if args == ["--isolated-dev", "--unproven-cleanup"] {
        let budget = quit_allowance().map_err(|e| format!("dev_budget:{e:?}"))?;
        return desktop::shell::run_development(
            Arc::new(Unavailable),
            UnprovenCleanup,
            budget,
            false,
        );
    }
    if args == ["--isolated-dev", "--slow-cleanup"] {
        let budget = quit_allowance().map_err(|e| format!("dev_budget:{e:?}"))?;
        return desktop::shell::run_development(Arc::new(Unavailable), SlowCleanup, budget, false);
    }
    let adapter_capacity = match args.as_slice() {
        [isolated, probe, option, capacity]
            if isolated == "--isolated-dev"
                && probe == "--probe"
                && option == "--service-capacity" =>
        {
            Some(
                capacity
                    .parse::<std::num::NonZeroUsize>()
                    .map_err(|_| "invalid_service_capacity")?,
            )
        }
        _ if args == ["--isolated-dev"] || args == ["--isolated-dev", "--probe"] => None,
        _ => {
            return Err(
                "usage: mycelium-desktop-dev --isolated-dev [--probe [--service-capacity N]]"
                    .into(),
            );
        }
    };
    let budget = quit_allowance().map_err(|e| format!("dev_budget:{e:?}"))?;
    if let Some(capacity) = adapter_capacity {
        let (core, cleanup) = adapt(Arc::new(DevelopmentServices), capacity);
        desktop::shell::run_development(core, cleanup, budget, true)
    } else {
        desktop::shell::run_development(
            Arc::new(Unavailable),
            EmptyCleanup,
            budget,
            args.len() == 2,
        )
    }
}
fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
