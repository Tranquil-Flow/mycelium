// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded process supervision, independently testable without starting a child.
use crate::{
    Fault, FaultKind, Result,
    effects::{Clock, Command, Output},
};
use budgets::Span;
pub trait Process {
    fn start(&mut self, command: &Command) -> Result<()>;
    fn poll(&mut self) -> Result<Option<Output>>;
    fn terminate_owned(&mut self) -> Result<()>;
    fn cleanup(&mut self) -> Result<()>;
}
pub fn supervise<P: Process, C: Clock>(
    p: &mut P,
    clock: &mut C,
    command: &Command,
    bound: Span,
    poll: Span,
    diagnostics: &mut Vec<Fault>,
) -> Result<Output> {
    let invalid = || Fault::new("invalid_process_budget", FaultKind::Input);
    if bound == Span::ZERO || poll == Span::ZERO {
        return Err(invalid());
    }
    let end = clock
        .now()
        .checked_add(bound.millis())
        .ok_or_else(invalid)?;
    let result = (|| {
        p.start(command)?;
        loop {
            if clock.now() >= end {
                return Err(Fault::new("command_timeout", FaultKind::Observation));
            }
            let observed_at = clock.now();
            let output = p.poll()?;
            let before = clock.now();
            if before < observed_at || before > end {
                return Err(Fault::new("command_timeout", FaultKind::Observation));
            }
            if let Some(out) = output {
                return Ok(out);
            }
            if before == end {
                return Err(Fault::new("command_timeout", FaultKind::Observation));
            }
            clock.sleep(poll.min(Span::from_millis(end - before)))?;
            if clock.now() <= before {
                return Err(Fault::new("clock_not_advancing", FaultKind::Observation));
            }
        }
    })();
    if let Err(e) = &result {
        diagnostics.push(*e);
        if let Err(e) = p.terminate_owned() {
            diagnostics.push(e);
        }
    }
    match p.cleanup() {
        Ok(()) => result,
        Err(e) => {
            diagnostics.push(e);
            match result {
                Err(first) => Err(first),
                Ok(_) => Err(e),
            }
        }
    }
}
