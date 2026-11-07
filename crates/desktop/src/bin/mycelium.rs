// SPDX-License-Identifier: AGPL-3.0-only
//! Core-less shell entry (test double of the packaged window; NOT the shipped executable, which is
//! `node-service`'s `mycelium` bin with the real core). It admits the OS and the pinned native payload
//! (`package-pins.json`, taken from the genuine native-input manifest) before
//! anything else, then serves the unchanged UI with NO core: every core API
//! answers 503 `core_not_integrated`, and the cleanup is an inert placeholder.
//! This is not an inference app; the L6/L8/L10 composition factory is a
//! SLICE-1/L12 seam. No development probe, script or dev flag exists here.
use desktop::{
    core::Core,
    entry::{self, Route},
    install,
    lifecycle::{Cleanup, CleanupError, Poll},
    package::{self, Entry},
    shell::{self, Reply},
};
use std::{process::ExitCode, sync::Arc};

const PINS: &str = include_str!("../../package-pins.json");

struct NoCore;
impl Core for NoCore {
    fn submit(&self, _request: desktop::channel::Request, complete: Box<dyn FnOnce(Reply) + Send>) {
        complete(Reply::unavailable());
    }
}
struct NoCleanup;
impl Cleanup for NoCleanup {
    fn begin(&mut self, _deadline: budgets::Span) -> Result<(), CleanupError> {
        Ok(())
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        Ok(Poll::Verified)
    }
}
/// v0.1.4's `main`: a bare launch outside `programs/<version>` refuses (event line, alert, exit 2);
/// the installer family runs the per-user installer for `$HOME`; a bare launch from the install
/// starts the shell; every other argument is refused. Nothing below runs before this decision.
fn route() -> Result<Option<ExitCode>, String> {
    let argv: Vec<String> = std::env::args().collect();
    let executable = std::env::current_exe()
        .and_then(|p| p.canonicalize())
        .map_err(|e| format!("executable:{:?}", e.kind()))?;
    let home = std::env::var_os("HOME")
        .map(std::path::PathBuf::from)
        .ok_or("home_unavailable")?;
    match entry::route(&argv, &executable, &home) {
        Route::Run => Ok(None),
        Route::Reject => Err("no_arguments_accepted".into()),
        Route::Refuse(refusal) => {
            eprintln!("{}", entry::refusal_event(&refusal));
            entry::show_alert(refusal.message);
            Ok(Some(ExitCode::from(2)))
        }
        Route::Installer => {
            let roots = install::Roots::for_current_user().map_err(|e| e.0.to_string())?;
            let outcome = entry::run_installer(&argv, &executable, &roots, &install::Seams::host());
            print!("{}", outcome.stdout);
            eprint!("{}", outcome.stderr);
            Ok(Some(ExitCode::from(
                u8::try_from(outcome.code).unwrap_or(2),
            )))
        }
    }
}
fn run() -> Result<(), String> {
    let pins: Vec<Entry> =
        serde_json::from_str(PINS).map_err(|_| "embedded_pins_invalid".to_string())?;
    let executable = std::env::current_exe().map_err(|e| format!("executable:{:?}", e.kind()))?;
    let manifest = package::with_measured_executable(pins, &executable)
        .map_err(|e| format!("executable_measure:{e:?}"))?;
    let budget = desktop::lifecycle::quit_allowance().map_err(|e| format!("budget:{e:?}"))?;
    shell::run_packaged(
        &manifest,
        |_verified| Ok((Arc::new(NoCore) as Arc<dyn Core>, NoCleanup)),
        budget,
    )
}
fn main() -> ExitCode {
    match route() {
        Ok(Some(code)) => return code,
        Ok(None) => {}
        Err(error) => {
            eprintln!("{error}");
            return ExitCode::FAILURE;
        }
    }
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
