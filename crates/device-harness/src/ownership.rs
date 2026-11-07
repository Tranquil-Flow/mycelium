// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Fault, FaultKind, Result, effects::Clock, preflight::ProcessIdentity};
use budgets::Span;
use serde::{Deserialize, Serialize};
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct OwnedRecord {
    pub process: ProcessIdentity,
    pub attempt: String,
    pub kind: String,
    pub marker: String,
    pub awake_record: Option<String>,
}
/// The adapter must inspect an opened, no-follow regular file: owner UID,
/// exact 0600 mode, single link. Never trust metadata cached before opening.
#[derive(Clone, Debug)]
pub struct RecordRead {
    pub record: OwnedRecord,
    pub private_regular_owned: bool,
}
pub trait OwnedProcessIo {
    fn record(&mut self) -> Result<RecordRead>;
    fn identity(&mut self, pid: u32) -> Result<Option<ProcessIdentity>>;
    /// Recheck birth+executable immediately before SIGINT; never signal by pattern.
    fn interrupt_exact(&mut self, identity: &ProcessIdentity) -> Result<()>;
    fn marker_exists(&mut self, path: &str) -> Result<bool>;
    fn stop_awake_exact(&mut self, record: &str) -> Result<()>;
}
#[derive(Clone, Copy, Debug)]
pub struct StopLimits {
    pub grace: Span,
    pub poll: Span,
}
fn end<C: Clock>(clock: &C, limits: StopLimits) -> Result<u64> {
    if limits.grace == Span::ZERO || limits.poll == Span::ZERO {
        return Err(Fault::new("invalid_stop_budget", FaultKind::Input));
    }
    clock
        .now()
        .checked_add(limits.grace.millis())
        .ok_or(Fault::new("invalid_stop_budget", FaultKind::Input))
}
fn pause<C: Clock>(clock: &mut C, end: u64, poll: Span) -> Result<()> {
    let before = clock.now();
    let remaining = end
        .checked_sub(before)
        .ok_or(Fault::new("stop_deadline", FaultKind::Cleanup))?;
    clock.sleep(Span::from_millis(remaining.min(poll.millis())))?;
    if clock.now() <= before {
        return Err(Fault::new("clock_not_advancing", FaultKind::Observation));
    }
    Ok(())
}
pub fn stop<I: OwnedProcessIo, C: Clock>(
    io: &mut I,
    clock: &mut C,
    self_pid: u32,
    attempt: &str,
    limits: StopLimits,
) -> Result<()> {
    let deadline = end(clock, limits)?;
    let read = io.record()?;
    if !read.private_regular_owned {
        return Err(Fault::new(
            "harness_pid_record_not_private",
            FaultKind::Admission,
        ));
    }
    let r = read.record;
    if r.process.pid <= 1
        || r.process.pid == self_pid
        || r.attempt != attempt
        || r.attempt.is_empty()
        || r.process.birth.is_empty()
        || r.process.executable.is_empty()
        || !matches!(r.kind.as_str(), "soak" | "stranger")
    {
        return Err(Fault::new(
            "harness_pid_record_invalid",
            FaultKind::Admission,
        ));
    }
    if let Some(current) = io.identity(r.process.pid)? {
        if current != r.process {
            return Err(Fault::new(
                "harness_pid_identity_changed",
                FaultKind::Admission,
            ));
        }
        io.interrupt_exact(&r.process)?;
        loop {
            match io.identity(r.process.pid)? {
                None => break,
                Some(p) if p != r.process => {
                    return Err(Fault::new(
                        "harness_pid_identity_changed",
                        FaultKind::Cleanup,
                    ));
                }
                Some(_) => {}
            }
            if clock.now() >= deadline {
                return Err(Fault::new(
                    "harness_stop_timeout_no_force_kill",
                    FaultKind::Cleanup,
                ));
            }
            pause(clock, deadline, limits.poll)?;
        }
    }
    let final_read = io.record()?;
    let last = final_read.record;
    if !final_read.private_regular_owned
        || last.process != r.process
        || last.attempt != r.attempt
        || last.kind != r.kind
        || last.marker != r.marker
    {
        return Err(Fault::new("harness_pid_record_changed", FaultKind::Cleanup));
    }
    if io.marker_exists(&last.marker)? {
        return Err(Fault::new(
            "harness_busy_cleanup_unproven",
            FaultKind::Cleanup,
        ));
    }
    if let Some(awake) = last.awake_record {
        io.stop_awake_exact(&awake)?;
    }
    Ok(())
}

pub trait AppQuitIo {
    fn census(&mut self, bundle: &str) -> Result<Vec<ProcessIdentity>>;
    fn apple_event_quit(&mut self, bundle: &str, bound: Span) -> Result<i32>;
    fn listener_present(&mut self, bundle: &str) -> Result<bool>;
    fn apple_event_error_code(&self) -> Option<i32> {
        None
    }
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct QuitReceipt {
    pub apple_event_rc: i32,
    pub apple_event_error: Option<String>,
    pub seconds_to_exit: Option<f64>,
    pub processes_before: usize,
    pub processes_left: usize,
    pub listener_left: bool,
    pub quit_clean: bool,
}
pub fn quit<I: AppQuitIo, C: Clock>(
    io: &mut I,
    clock: &mut C,
    bundle: &str,
    limits: StopLimits,
) -> Result<QuitReceipt> {
    let deadline = end(clock, limits)?;
    let started = clock.now();
    let before = io.census(bundle)?;
    if before.is_empty() {
        return Err(Fault::new("app_not_running", FaultKind::Admission));
    }
    let rc = io.apple_event_quit(bundle, limits.grace)?;
    let left = loop {
        let current = io.census(bundle)?;
        if current.is_empty() || clock.now() >= deadline {
            break current;
        }
        pause(clock, deadline, limits.poll)?;
    };
    let listener_left = io.listener_present(bundle)?;
    Ok(QuitReceipt {
        apple_event_rc: rc,
        apple_event_error: io.apple_event_error_code().map(|code| code.to_string()),
        seconds_to_exit: if left.is_empty() {
            clock.now().checked_sub(started).map(|ms| ms as f64 / 1000.)
        } else {
            None
        },
        processes_before: before.len(),
        processes_left: left.len(),
        listener_left,
        quit_clean: rc == 0 && left.is_empty() && !listener_left,
    })
}
