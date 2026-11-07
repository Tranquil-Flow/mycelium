// SPDX-License-Identifier: AGPL-3.0-only
//! Effect boundaries contain no process launch or network implementation.
use crate::{Fault, FaultKind, Result};
use budgets::Span;

/// A monotonic clock owned by one controller. Adapters must not mix host clocks.
pub trait Clock {
    fn now(&self) -> u64;
    fn sleep(&mut self, span: Span) -> Result<()>;
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CommandKind {
    Ssh,
    Scp,
    Local,
}
/// Deliberately not Debug/Serialize: argv and outputs can contain credentials.
pub struct Command {
    pub kind: CommandKind,
    pub program: String,
    pub args: Vec<String>,
}
pub struct Output {
    pub rc: i32,
    pub stdout: String,
    pub stderr: String,
}
pub trait Transport {
    fn execute(&mut self, command: &Command, bound: Span) -> Result<Output>;
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RetryReceipt {
    pub attempt: u8,
    pub rc: i32,
    pub delay: Span,
}
/// TODO(L1): register DeviceCommand and DeviceSshRetryDelay budget IDs. Until
/// then callers inject named typed allowances; there are no hidden defaults.
#[derive(Clone, Copy, Debug)]
pub struct CommandLimits {
    pub attempt: Span,
    pub retry_delay: Span,
}
pub fn execute<T: Transport, C: Clock>(
    io: &mut T,
    clock: &mut C,
    command: &Command,
    limits: CommandLimits,
    receipts: &mut Vec<RetryReceipt>,
) -> Result<Output> {
    let invalid = || Fault::new("invalid_command_budget", FaultKind::Input);
    if limits.attempt == Span::ZERO {
        return Err(invalid());
    }
    let total = limits
        .attempt
        .checked_mul(2)
        .and_then(|s| s.checked_add(limits.retry_delay))
        .map_err(|_| invalid())?;
    let end = clock
        .now()
        .checked_add(total.millis())
        .ok_or_else(invalid)?;
    let mut wrapped = Command {
        kind: command.kind,
        program: command.program.clone(),
        args: command.args.clone(),
    };
    if command.kind == CommandKind::Ssh {
        let remote = wrapped
            .args
            .last_mut()
            .ok_or(Fault::new("ssh_command_missing", FaultKind::Input))?;
        *remote = format!(
            "/bin/sh -c {}; rc=$?; printf '\\nMYCELIUM_REMOTE_EXIT=%s\\n' \"$rc\" >&2; exit \"$rc\"",
            shell_quote(remote)
        );
    }
    for attempt in 0..2 {
        let before = clock.now();
        let out = io.execute(&wrapped, limits.attempt)?;
        if clock.now() < before
            || clock.now() > end
            || clock.now() - before > limits.attempt.millis()
        {
            return Err(Fault::new("command_timeout", FaultKind::Observation));
        }
        let remote_finished = out.stderr.lines().any(|line| {
            line.strip_prefix("MYCELIUM_REMOTE_EXIT=")
                .is_some_and(|v| v.parse::<u8>().is_ok())
        });
        let connection_lost = command.kind == CommandKind::Ssh && out.rc == 255 && !remote_finished;
        if connection_lost && attempt == 0 {
            receipts.push(RetryReceipt {
                attempt: 1,
                rc: 255,
                delay: limits.retry_delay,
            });
            clock.sleep(limits.retry_delay)?;
            if clock.now() > end {
                return Err(Fault::new("command_timeout", FaultKind::Observation));
            }
            continue;
        }
        return if connection_lost {
            Err(Fault::new("ssh_observation_lost", FaultKind::Observation))
        } else if out.rc != 0 {
            Err(Fault::new("command_failed", FaultKind::Observation))
        } else {
            Ok(out)
        };
    }
    Err(Fault::new("ssh_observation_lost", FaultKind::Observation))
}

/// Safe compatibility diagnostic; no argv, stdout or stderr is retained.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandFailure {
    pub fault: Fault,
    pub legacy: String,
}
impl std::fmt::Display for CommandFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.legacy)
    }
}
impl std::error::Error for CommandFailure {}
pub fn execute_reported<T: Transport, C: Clock>(
    io: &mut T,
    clock: &mut C,
    command: &Command,
    limits: CommandLimits,
    receipts: &mut Vec<RetryReceipt>,
) -> std::result::Result<Output, CommandFailure> {
    struct Observed<'a, T> {
        inner: &'a mut T,
        exit: Option<i32>,
    }
    impl<T: Transport> Transport for Observed<'_, T> {
        fn execute(&mut self, command: &Command, bound: Span) -> Result<Output> {
            // Clear the previous exit before a retry that may itself time out.
            self.exit = None;
            let output = self.inner.execute(command, bound)?;
            self.exit = Some(output.rc);
            Ok(output)
        }
    }
    let mut observed = Observed {
        inner: io,
        exit: None,
    };
    let result = execute(&mut observed, clock, command, limits, receipts);
    result.map_err(|fault| {
        let name = command.program.rsplit('/').next().unwrap_or("");
        let safe = !name.is_empty()
            && name.len() <= 160
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b));
        let legacy = match observed.exit {
            Some(rc) if safe && matches!(fault.code, "command_failed" | "ssh_observation_lost") => {
                format!("command_failed:{name}:rc{rc}")
            }
            _ => fault.code.into(),
        };
        CommandFailure { legacy, fault }
    })
}

pub fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}
