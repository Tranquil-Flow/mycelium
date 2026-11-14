// SPDX-License-Identifier: AGPL-3.0-only
//! What a chain step may do besides calling its collaborators: report progress, notice the
//! owner's Stop, read a clock and wait. `RunContext.wait_until` (`owner_run.py:113-125 @ f4e366e9`)
//! as a function over an environment, so a step is testable without a thread or a real clock.
use crate::machine::{OwnerRunError, closed_code};
use serde_json::Value;

/// How a step ended other than by returning: a closed refusal, the owner's Stop (`_Stopped`, never a
/// failure) or any other exception (only its class name survives).
#[derive(Clone, Debug, PartialEq)]
pub enum StepError {
    Refused(OwnerRunError),
    Stopped,
    Exception { class: String },
}

impl From<OwnerRunError> for StepError {
    fn from(e: OwnerRunError) -> Self {
        Self::Refused(e)
    }
}

/// `RunContext` as a step sees it.
pub trait StepEnv {
    /// `ctx.progress(detail)`.
    fn progress(&mut self, detail: &str);
    /// `ctx.stop_event.is_set()` (`ctx.check_stopped`).
    fn stopped(&self) -> bool;
    /// The injectable monotonic clock, in seconds (`time.monotonic`).
    fn now(&mut self) -> f64;
    /// The wall clock, in seconds since the epoch (`time.time`): descriptor expiry, report silence.
    fn wall(&mut self) -> f64;
    /// `ctx.result[key] = value` (chain-owned results shown in status: `participants`, `build_seconds`, ...).
    fn set_result(&mut self, key: &str, value: Value);
    /// `ctx.record_exception(code, error)`: a closed call-site code and the exception class only.
    fn record_exception(&mut self, code: &str, class: &str);
    /// `ctx.stop_event.wait(interval)`: sleeps up to `seconds`; true when Stop ended the wait.
    fn pause(&mut self, seconds: f64) -> bool;
    /// Run a short publication `commit` so that the owner's Stop cannot land between its checks
    /// and its effect (`with OwnerRun._lock: if ctx.stop_event.is_set(): raise _Stopped()`,
    /// `demo_chain.py:1074-1080 @ f4e366e9`): a Stop that came first ends the step stopped and
    /// `commit` never runs; a Stop that comes later waits until `commit` returned. `commit` must
    /// not call back into this environment (no progress, result or clock) and must not block on
    /// anything remote. This default only checks first; the threaded Runner holds its stop event
    /// across `commit`.
    fn fenced(
        &mut self,
        commit: &mut dyn FnMut() -> Result<(), StepError>,
    ) -> Result<(), StepError> {
        if self.stopped() {
            return Err(StepError::Stopped);
        }
        commit()
    }
}

/// `ctx.wait_until(predicate, budget=, code=, interval=)`: poll until the predicate yields a value;
/// Stop interrupts; budget expiry fails with `code` (no message).
pub fn wait_until<T>(
    env: &mut dyn StepEnv,
    mut predicate: impl FnMut() -> Result<Option<T>, StepError>,
    budget: f64,
    code: &str,
    interval: f64,
) -> Result<T, StepError> {
    wait_with(env, |_| predicate(), budget, code, interval)
}

/// [`wait_until`] for a predicate that reads the clock, reports progress or sets results itself.
pub fn wait_with<T>(
    env: &mut dyn StepEnv,
    mut predicate: impl FnMut(&mut dyn StepEnv) -> Result<Option<T>, StepError>,
    budget: f64,
    code: &str,
    interval: f64,
) -> Result<T, StepError> {
    let deadline = env.now() + budget;
    loop {
        if env.stopped() {
            return Err(StepError::Stopped);
        }
        if let Some(value) = predicate(env)? {
            return Ok(value);
        }
        if env.now() >= deadline {
            return Err(OwnerRunError::new(code).into());
        }
        if env.pause(interval) {
            return Err(StepError::Stopped);
        }
    }
}

/// What a collaborator call can raise, as the chain sees it (`demo_chain._code`): an `OwnerRunError` passes
/// through; any other exception contributes its `.code` (or, failing that, its text) when that is a closed
/// code, else the call site's default.
#[derive(Clone, Debug, PartialEq)]
pub enum PortError {
    Refused(OwnerRunError),
    Failed { code: Option<String>, text: String },
}

impl PortError {
    /// `_code(error, default)`.
    pub fn code_or(&self, default: &str) -> String {
        match self {
            Self::Refused(e) => e.code.clone(),
            Self::Failed { code, text } => {
                let candidate = code.as_deref().filter(|c| !c.is_empty()).unwrap_or(text);
                if closed_code(candidate) {
                    candidate.to_owned()
                } else {
                    default.to_owned()
                }
            }
        }
    }
    /// `except OwnerRunError: raise; except Exception as error: raise OwnerRunError(_code(error, default))`.
    pub fn into_step_error(self, default: &str) -> StepError {
        match self {
            Self::Refused(e) => StepError::Refused(e),
            other => OwnerRunError::new(&other.code_or(default)).into(),
        }
    }
}
