// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged executable with the real core. Same entry decisions as v0.1.4 (bare launch
//! outside `programs/<version>` refuses; installer flags run the per-user installer), then OS
//! and pinned native-payload admission BEFORE the node service exists, then the unchanged
//! window over SLICE-1's node service. The same executable is the node's member / owner-leaf
//! worker: the owner re-executes `current_exe()` with `member|owner-leaf <launch> <state>`,
//! and each worker passes the same admission first. The member agent re-executes it once more as
//! `native-worker` (the compute child, RU-020), and the run's route codec as
//! `tokenize <tokenizer.json>` (the tokenizer helper, `api::joined_owner::codec::NativeCodec`).
//!
//! No probe, script or dev flag exists here. `qualification-probe` (test builds only) swaps
//! the final window call for one that carries the chat-driver script.
use desktop::{
    entry::{self, Route},
    install,
    package::{self, Entry},
    shell,
};
use std::{path::Path, process::ExitCode};

const PINS: &str = include_str!("../../../desktop/package-pins.json");

fn manifest() -> Result<package::Manifest, String> {
    let mut pins: Vec<Entry> =
        serde_json::from_str(PINS).map_err(|_| "embedded_pins_invalid".to_string())?;
    // The pinned helpers this tree builds (`native/resource_probe`): admitted by their pinned bytes
    // like the native payload, so a bundle without the probe, or with other bytes, never starts.
    pins.extend(package::helper_pins().map_err(|e| format!("embedded_helper_pins:{e:?}"))?);
    let executable = std::env::current_exe().map_err(|e| format!("executable:{:?}", e.kind()))?;
    package::with_measured_executable(pins, &executable)
        .map_err(|e| format!("executable_measure:{e:?}"))
}
fn state_root() -> Result<std::path::PathBuf, String> {
    Ok(install::Roots::for_current_user()
        .map_err(|e| e.0.to_string())?
        .state())
}
/// v0.1.4's `main` routing; see `desktop::entry`.
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
/// A node worker re-executed by the owner. Admission first, then the worker body.
fn worker(argv: &[String]) -> Result<(), String> {
    package::admit_worker(&manifest()?)?;
    node_service::native::member(&argv[2], Path::new(&argv[3]), argv[1] == "owner-leaf")
        .map_err(|e| format!("worker:{e}"))
}
/// The member agent's native worker child (RU-020): `native-worker`, stdout = the private link to
/// the agent. The same admission as every other worker precedes anything native; the agent spawns
/// it with `current_exe()`, so in a bundle it is this very executable.
fn native_worker() -> Result<(), String> {
    package::admit_worker(&manifest()?)?;
    node_service::native::native_worker().map_err(|e| format!("worker:{e}"))
}
/// The route codec's tokenizer helper (lane BND, REPORT-laneTEN D1): `tokenize <tokenizer.json>`,
/// the line protocol on stdin/stdout `mycelium-node tokenize` serves. The packaged run starts it
/// with the admitted program (`PinnedCodec` -> `NativeCodec::start`), which in a bundle is this
/// executable; the same admission as every other worker role precedes the tokenizer load.
fn tokenize(tokenizer: &str) -> Result<(), String> {
    package::admit_worker(&manifest()?)?;
    node_service::tokenize::serve(Path::new(tokenizer)).map_err(|e| format!("tokenize:{e}"))
}
fn run() -> Result<(), String> {
    let manifest = manifest()?;
    let budget = desktop::lifecycle::quit_allowance().map_err(|e| format!("budget:{e:?}"))?;
    let state = state_root()?;
    let executable = std::env::current_exe().map_err(|e| format!("executable:{:?}", e.kind()))?;
    // This is the entry measured at startup and subsequently admitted by the
    // shell, not a fresh expected hash measured when a worker is spawned.
    let entry = manifest
        .entries
        .iter()
        .find(|entry| entry.kind == package::Kind::Executable)
        .ok_or("native_executable_entry_missing")?;
    let compose = |verified: package::VerifiedBundle| {
        let program =
            node_service::packaged::admitted_native_program(&verified, entry, &executable)?;
        node_service::packaged::core_with_native_program(&state, program)
    };
    #[cfg(not(feature = "qualification-probe"))]
    return shell::run_packaged(&manifest, compose, budget);
    #[cfg(feature = "qualification-probe")]
    return shell::run_packaged_with_probe(&manifest, compose, budget, probe_script(&state)?);
}
/// Test builds only: the frozen chat page over the run's route with no run serving (lane CHAT):
/// the page's not-ready state, then a send of the oracle question (as many tokens as the recorded
/// oracle run) refused `chat_not_ready` and an unknown request's events refused.
#[cfg(feature = "qualification-probe")]
fn probe_script(_state: &Path) -> Result<String, String> {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("../../data/qwen2-oracle.json"))
            .map_err(|e| format!("oracle:{e}"))?;
    let tokens = fixture["tokens"].as_array().ok_or("oracle_tokens")?.len();
    Ok(include_str!("../packaged/probe.js")
        .replace(
            "__PROMPT_JSON__",
            &serde_json::to_string(node_service::chat_template::ORACLE_USER_PROMPT)
                .map_err(|e| e.to_string())?,
        )
        .replace("__TOKENS_JSON__", &format!("\"{tokens}\"")))
}
fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().collect();
    // Worker roles, exactly the argument shapes `mycelium-node` accepts.
    let role = if argv.len() == 4 && matches!(argv[1].as_str(), "member" | "owner-leaf") {
        Some(worker(&argv))
    } else if argv.len() == 2 && argv[1] == "native-worker" {
        Some(native_worker())
    } else if argv.len() == 3 && argv[1] == "tokenize" {
        Some(tokenize(&argv[2]))
    } else {
        None
    };
    if let Some(outcome) = role {
        return match outcome {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("{error}");
                ExitCode::FAILURE
            }
        };
    }
    match route() {
        Ok(Some(code)) => return code,
        Ok(None) => {}
        Err(error) => {
            eprintln!("{error}");
            return ExitCode::FAILURE;
        }
    }
    // v0.1.4's `preflight` refusal: an unsupported Mac says so (a Finder launch has no terminal,
    // so also as an alert), starts nothing and exits 2. It precedes any native load.
    match entry::host_platform_refusal() {
        Ok(None) => {}
        Ok(Some(refusal)) => {
            eprintln!("{}", refusal.event);
            entry::show_alert(&refusal.message);
            return ExitCode::from(refusal.exit_code);
        }
        // The OS version could not be read: no claim about the Mac (what `run` would report).
        Err(error) => {
            eprintln!("platform:{error:?}");
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
