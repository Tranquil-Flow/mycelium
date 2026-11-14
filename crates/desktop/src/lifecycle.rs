// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId, Entries, Span};
/// C-1: never advertise readiness from setup or a wall-clock sleep.
#[derive(Default)]
pub struct Readiness {
    native: bool,
    barrier: bool,
}
impl Readiness {
    pub fn native_ready(&mut self) {
        self.native = true;
    }
    pub fn event_barrier(&mut self) {
        if self.native {
            self.barrier = true;
        }
    }
    pub fn is_ready(&self) -> bool {
        self.native && self.barrier
    }
}
#[derive(Clone, Copy, Debug)]
pub enum QuitSource {
    Menu,
    CommandQ,
    AppleEvent,
    WebUi,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CleanupError {
    Deadline,
    Begin,
    Poll,
    Clock,
    InvalidBudget,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Poll {
    Pending,
    Verified,
}
/// Implementations MUST be nonblocking: enqueue owned-worker cleanup in begin,
/// and try-read its receipt in poll. No joins, I/O, locks held over backend work,
/// or speculative success. L6 supplies verified owned-resource teardown.
pub trait Cleanup {
    /// Bind the shell's actual monotonic origin before any request. Adapters
    /// translating Span deadlines to Instant must not use their creation time.
    fn bind_clock(&mut self, _origin: std::time::Instant) -> Result<(), CleanupError> {
        Ok(())
    }
    fn begin(&mut self, deadline: Span) -> Result<(), CleanupError>;
    fn poll(&mut self) -> Result<Poll, CleanupError>;
    /// The Quit's deadline arrived: issue now what must still precede the process exit and say
    /// whether it was issued (`Verified`) or an owned worker is still issuing it (`Pending`: the
    /// Quit stays stopping and is asked again on the next tick). Nonblocking, as `poll`. v0.1.4
    /// calls `workflow.stop()` after `run.join(60)` even when that join timed out
    /// (`demo_app.py:199-203`, `app.py:1835-1838 @ f4e366e9`). Default: nothing to issue.
    fn expire(&mut self) -> Result<Poll, CleanupError> {
        Ok(Poll::Verified)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Running,
    Stopping,
    Verified,
    Failed(CleanupError),
}
/// The launcher status of a Quit where a close step raised: `__main__.py:219-223 @ f4e366e9`
/// (`launcher_cleanup_failed` / `cleanup_incomplete`, state retained). A proven cleanup is 0.
pub const EXIT_CLEANUP_INCOMPLETE: i32 = 3;
/// v0.1.4's stderr line for that case, byte for byte (Python `json.dumps` separators), as
/// executed in `SLICE-1-evidence/r8-oracle/launcher-unproven.log`.
pub const CLEANUP_INCOMPLETE_EVENT: &str =
    r#"{"event": "launcher_cleanup_failed", "code": "cleanup_incomplete", "state_retained": true}"#;
/// How long Quit waits for the run's teardown: the L1 id `PyMyceliumA13MacosDemoAppCloseRunJoinL203`
/// (`demo_app.py:202-203 @ f4e366e9`: `run.stop(); run.join(60)`). Every production shell entry
/// (packaged `mycelium`, the node-service packaged and desktop-shell hosts, the development shell)
/// takes its allowance from here (RU-032 / SE-SLICE1-L11-QUIT-JOIN-60); it is not a stage-cleanup
/// or router budget.
pub fn quit_allowance() -> Result<Span, CleanupError> {
    BudgetId::PyMyceliumA13MacosDemoAppCloseRunJoinL203
        .span(Entries::ZERO)
        .map_err(|_| CleanupError::InvalidBudget)
}
pub struct Shutdown<C> {
    core: C,
    budget: Span,
    deadline: Span,
    last: Span,
    state: State,
}
impl<C: Cleanup> Shutdown<C> {
    /// `budget` is an injected L1/L6 allowance, not permission to extend consent.
    pub fn new(core: C, budget: Span) -> Result<Self, CleanupError> {
        if budget == Span::ZERO {
            return Err(CleanupError::InvalidBudget);
        }
        Ok(Self {
            core,
            budget,
            deadline: Span::ZERO,
            last: Span::ZERO,
            state: State::Running,
        })
    }
    pub fn request(&mut self, _source: QuitSource, now: Span) {
        if self.state != State::Running {
            return;
        }
        self.last = now;
        self.deadline = match now.checked_add(self.budget) {
            Ok(value) => value,
            Err(_) => {
                self.state = State::Failed(CleanupError::Clock);
                return;
            }
        };
        self.state = match self.core.begin(self.deadline) {
            Ok(()) => State::Stopping,
            Err(e) => State::Failed(e),
        };
    }
    pub fn tick(&mut self, now: Span) -> State {
        if self.state != State::Stopping {
            return self.state;
        }
        if now < self.last {
            self.state = State::Failed(CleanupError::Clock);
        } else if now >= self.deadline {
            // The allowance ran out (exit 0), but never before what must precede the exit.
            self.state = match self.core.expire() {
                Ok(Poll::Verified) => State::Failed(CleanupError::Deadline),
                Ok(Poll::Pending) => State::Stopping,
                Err(e) => State::Failed(e),
            };
        } else {
            self.state = match self.core.poll() {
                Ok(Poll::Verified) => State::Verified,
                Ok(Poll::Pending) => State::Stopping,
                Err(e) => State::Failed(e),
            };
        }
        self.last = now;
        self.state
    }
    pub fn state(&self) -> State {
        self.state
    }
    pub fn can_exit(&self) -> bool {
        self.state == State::Verified
    }
    /// The process status once the Quit has a result. 0 when the cleanup is verified, and also 0
    /// when the join allowance ran out with the teardown still running: v0.1.4 ignores the result
    /// of `run.join(60)` (`demo_app.py:203`) and exits 0 (executed q4, `SLICE-1-evidence/r10-oracle/`;
    /// RU-032). 3 only where v0.1.4 returns it - a close step raised (`__main__.py:219-223`):
    /// here a begin or receipt-poll error. A clock fault or an invalid budget are inputs v0.1.4
    /// never produces; they keep the stricter 3 (V03-L11-1: the process cannot tell what happened).
    /// None while the Quit is still running or pending. Unlike `can_exit` this never claims the
    /// cleanup succeeded: `Failed(Deadline)` exits 0 without being `Verified`.
    pub fn exit_status(&self) -> Option<i32> {
        match self.state {
            State::Verified | State::Failed(CleanupError::Deadline) => Some(0),
            State::Failed(_) => Some(EXIT_CLEANUP_INCOMPLETE),
            State::Running | State::Stopping => None,
        }
    }
}
