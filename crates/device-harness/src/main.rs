// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Result,
    cli::{self, Action, Bounds},
    effects::{Command, CommandKind, Transport},
    native::{NativeTransport, RealClock},
    ownership::{self, StopLimits},
    stop_command::StopAdapter,
};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::{
    fs::{self, OpenOptions},
    io::Read,
    path::Path,
    process::ExitCode,
};
fn input(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Input)
}
fn read(path: &Path, max: u64) -> Result<Vec<u8>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| input("input_file_unavailable"))?;
    let metadata = file
        .metadata()
        .map_err(|_| input("input_file_unavailable"))?;
    if !metadata.is_file() || metadata.len() > max {
        return Err(input("input_file_invalid"));
    }
    let mut bytes = Vec::new();
    file.take(max + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| input("input_file_read_failed"))?;
    if bytes.len() as u64 > max {
        return Err(input("input_file_too_large"));
    }
    Ok(bytes)
}
fn dispatch(action: Action) -> Result<u8> {
    match action {
        Action::Help => {
            print!("{}", cli::HELP);
            Ok(0)
        }
        Action::Evaluate(path) => {
            let value = cli::evaluate(&read(&path, 64 * 1024 * 1024)?)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&value)
                    .map_err(|_| input("verdict_encoding_failed"))?
            );
            Ok(if value["scenario_passed"] == true {
                0
            } else {
                1
            })
        }
        Action::Stop { attempt, budgets } => {
            // No hard-coded timeout: all operational spans are explicit until L1
            // registers device-runner IDs. Unknown JSON fields are rejected.
            let b: Bounds = serde_json::from_slice(&read(&budgets, 16384)?)
                .map_err(|_| input("invalid_stop_budgets"))?;
            if [b.command_ms, b.grace_ms, b.poll_ms, b.max_output_bytes].contains(&0) {
                return Err(input("invalid_stop_budgets"));
            }
            let m = fs::symlink_metadata(&attempt).map_err(|_| input("attempt_unavailable"))?;
            if !m.is_dir() || m.file_type().is_symlink() || m.permissions().mode() & 0o077 != 0 {
                return Err(input("attempt_not_private"));
            }
            let attempt = attempt
                .canonicalize()
                .map_err(|_| input("attempt_unavailable"))?;
            let mut transport = NativeTransport {
                scratch: attempt.clone(),
                uid: m.uid(),
                poll: Span::from_millis(b.poll_ms),
                max_output_bytes: b.max_output_bytes,
                diagnostics: vec![],
            };
            // Obtain the effective owner through a fixed bounded command, never UID
            // from user environment or a caller-supplied PID/username.
            let uid = transport.execute(
                &Command {
                    kind: CommandKind::Local,
                    program: "/usr/bin/id".into(),
                    args: vec!["-u".into()],
                },
                Span::from_millis(b.command_ms),
            )?;
            if uid.rc != 0 || uid.stdout.trim().parse::<u32>().ok() != Some(m.uid()) {
                return Err(input("attempt_not_owned"));
            }
            let exe = std::env::current_exe()
                .and_then(|p| p.canonicalize())
                .map_err(|_| input("harness_executable_unavailable"))?;
            let mut io = StopAdapter {
                transport,
                record_path: attempt.join("harness-pid.json"),
                expected_executable: exe.to_string_lossy().into(),
                uid: m.uid(),
                command_bound: Span::from_millis(b.command_ms),
            };
            ownership::stop(
                &mut io,
                &mut RealClock::default(),
                std::process::id(),
                attempt.to_str().ok_or(input("attempt_path_invalid"))?,
                StopLimits {
                    grace: Span::from_millis(b.grace_ms),
                    poll: Span::from_millis(b.poll_ms),
                },
            )?;
            println!("{{\"outcome\":\"stopped\",\"exact_owned_harness\":true}}");
            Ok(0)
        }
    }
}
fn main() -> ExitCode {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    match cli::parse(&args).and_then(dispatch) {
        Ok(code) => ExitCode::from(code),
        Err(f) => {
            eprintln!("{}", f);
            ExitCode::from(2)
        }
    }
}
