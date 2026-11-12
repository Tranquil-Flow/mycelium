// SPDX-License-Identifier: AGPL-3.0-only
//! `OwnerRun` as a sans-IO state machine. Oracle: `owner_run.py:1-456 @ f4e366e9`.
//!
//! v0.1.4 runs one daemon thread per session that sequences the chain's steps, holds while
//! serving, tears down, and (for a rollover) starts the next run. Here that thread is a machine:
//! every method applies one event and returns the [`Cmd`]s the driver must perform next. The
//! machine reads no clock, spawns nothing and calls no chain; the driver (a thread, a test)
//! performs the commands and reports back. The mapping of Python's control flow onto events:
//!
//! | oracle | machine |
//! |---|---|
//! | `start()` (spawns the run thread) | [`OwnerRun::start`] -> `NewRun`, `RunStep(model)` |
//! | `chain.run_step(name, ctx)` returns / raises | [`OwnerRun::step_finished`] with a [`StepOutcome`] |
//! | `ctx.progress(d)`, `ctx.result[k] = v`, `ctx.state[...]` | [`OwnerRun::progress`], [`OwnerRun::set_result`], [`OwnerRun::set_rollover_binding`] |
//! | `stop()` (sets `ctx.stop_event`) | [`OwnerRun::stop`] -> `SignalStop` (+ `Teardown` while serving) |
//! | `chain.teardown(ctx, failed=)` returns / raises | [`OwnerRun::teardown_finished`] |
//! | the serving loop's `_end_at_ceiling` poll | [`OwnerRun::serving_poll`] |
//! | `status()`'s `monitor(ctx)` | [`OwnerRun::monitor_result`] |
//! | `_main`'s backoff wait | [`OwnerRun::tick`] |
//! | `rebuild_async()` / its worker thread | [`OwnerRun::rebuild_async`] + [`OwnerRun::rebuild_join_expired`] |
//!
//! Every behaviour below cites the oracle line it ports; the executed traces are
//! `tests/fixtures/oracle_owner_run.json` (FX-SLICE1-owner-run).
use crate::ceiling::{self, Ceiling};
use serde_json::{Map, Value};

/// `STEPS` (`owner_run.py:30-41`): name and label, in run order.
pub const STEPS: [(&str, &str); 10] = [
    ("model", "Model ready on this Mac"),
    ("contributor", "This Mac joins the run"),
    ("descriptors", "Joined Macs report in"),
    ("observe", "Native hello on every Mac"),
    ("compile", "Compile the route over the current members"),
    ("slices", "Every Mac fetches only its own slice"),
    ("publish", "Publish work to every Mac"),
    ("targets", "Every Mac accepts and starts its slice"),
    ("activate", "Activate and qualify the route"),
    ("serving", "Serving prompts"),
];
/// `EXECUTION_CEILING_CODE` (`owner_run.py:44`): the closed code of a run ended at its wall allowance.
pub const EXECUTION_CEILING_CODE: &str = "execution_ceiling_reached";
/// `STATUS protocol` (`owner_run.py:198`).
pub const PROTOCOL: &str = "mycelium.a13_owner_run.v1";
const STEP_FAILED: &str = "owner_run_step_failed";
/// `_CODE = [a-z][a-z0-9_]{0,79}` (`owner_run.py:45`).
const CODE_MAX: usize = 80;
/// `message[:600]` (`owner_run.py:56`).
const MESSAGE_MAX: usize = 600;
/// `str(detail)[:200]` (`owner_run.py:337`).
const DETAIL_MAX: usize = 200;
/// `rollover_max_failures = 3` (`owner_run.py:151`): a count, not registered as a budget.
const ROLLOVER_MAX_FAILURES: u32 = 3;

/// The closed `[a-z][a-z0-9_]{0,79}` shape (`re.fullmatch` with `\Z`).
pub fn closed_code(code: &str) -> bool {
    let b = code.as_bytes();
    !b.is_empty()
        && b.len() <= CODE_MAX
        && b[0].is_ascii_lowercase()
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_')
}

/// `OwnerRunError` (`owner_run.py:51-58`): a closed `code` (anything else is
/// `owner_run_step_failed`), a human `message` of at most 600 chars, and a `fix` pointer.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OwnerRunError {
    pub code: String,
    pub message: Option<String>,
    pub fix: Option<Map<String, Value>>,
}

impl OwnerRunError {
    pub fn new(code: &str) -> Self {
        Self::with(code, None, None)
    }
    pub fn with(code: &str, message: Option<&str>, fix: Option<Map<String, Value>>) -> Self {
        Self {
            code: if closed_code(code) {
                code.to_owned()
            } else {
                // Log-only: the published code stays the closed `owner_run_step_failed`, but the
                // real one must be visible somewhere or a failing step is undiagnosable.
                eprintln!("owner_run_step_code_masked:{code}");
                STEP_FAILED.to_owned()
            },
            message: message
                .filter(|m| !m.is_empty())
                .map(|m| m.chars().take(MESSAGE_MAX).collect()),
            fix,
        }
    }
}

/// Why a run starts (`ctx.reason`).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Reason {
    Start,
    Rebuild,
    Rollover,
}

impl Reason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Start => "start",
            Self::Rebuild => "rebuild",
            Self::Rollover => "rollover",
        }
    }
    /// `reason not in {'start', 'rebuild'}` (`owner_run.py:217`): a rollover is never requested.
    pub fn parse_request(reason: &str) -> Result<Self, OwnerRunError> {
        match reason {
            "start" => Ok(Self::Start),
            "rebuild" => Ok(Self::Rebuild),
            _ => Err(OwnerRunError::new("owner_run_reason_invalid")),
        }
    }
}

/// `{run_id, deployment_id}` of one run (`new_run_identity`, `owner_run.py:65-68`).
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Identity {
    pub run_id: String,
    pub deployment_id: String,
}

/// `a13-<3 random bytes hex>-qwen-<UTC %Y%m%dT%H%M%SZ>` and a uuid4 (`owner_run.py:65-68`), from
/// randomness and the UTC stamp the host supplies (this crate reads neither).
pub fn new_run_identity(random: [u8; 19], utc_stamp: &str) -> Identity {
    let hex = |bytes: &[u8]| bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    let mut u = [0u8; 16];
    u.copy_from_slice(&random[3..]);
    u[6] = (u[6] & 0x0f) | 0x40; // version 4
    u[8] = (u[8] & 0x3f) | 0x80; // RFC 4122 variant
    let uuid = format!(
        "{}-{}-{}-{}-{}",
        hex(&u[0..4]),
        hex(&u[4..6]),
        hex(&u[6..8]),
        hex(&u[8..10]),
        hex(&u[10..16])
    );
    Identity {
        run_id: format!("a13-{}-qwen-{utc_stamp}", hex(&random[..3])),
        deployment_id: uuid,
    }
}

/// Seconds, supplied by the driver on every call that needs them.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Clock {
    /// `time.monotonic()` (the injectable `clock`): backoff deadlines.
    pub mono: f64,
    /// `time.time()`: `started_at`.
    pub wall: f64,
}

/// `OwnerRun._state` (`owner_run.py:137`, `_ACTIVE` at :48).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum State {
    Idle,
    Running,
    Serving,
    Stopping,
    Failing,
    Failed,
    Stopped,
}

impl State {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Running => "running",
            Self::Serving => "serving",
            Self::Stopping => "stopping",
            Self::Failing => "failing",
            Self::Failed => "failed",
            Self::Stopped => "stopped",
        }
    }
    /// `_ACTIVE = {'running', 'serving', 'stopping', 'failing'}`.
    pub fn active(self) -> bool {
        matches!(
            self,
            Self::Running | Self::Serving | Self::Stopping | Self::Failing
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StepState {
    Pending,
    Active,
    Done,
    Failed,
}

impl StepState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Active => "active",
            Self::Done => "done",
            Self::Failed => "failed",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StepRow {
    pub state: StepState,
    pub detail: Option<String>,
}

/// How a step ended, as the driver saw the chain's `run_step` end.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StepOutcome {
    Ok,
    /// `raise OwnerRunError(...)`.
    Refused(OwnerRunError),
    /// Any other exception: only its class name is kept (no exception text reaches the UI).
    Exception {
        class: String,
    },
    /// `_Stopped`: the step noticed the owner's Stop (`ctx.check_stopped` / `ctx.wait_until`).
    Stopped,
}

/// What the serving loop's ceiling check gave (`_end_at_ceiling`).
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CeilingProbe {
    /// The chain has no `ceiling_reached` (`getattr(chain, 'ceiling_reached', None) is None`).
    Unavailable,
    Code(Option<String>),
    Raised {
        class: String,
    },
}

/// What `monitor(ctx)` gave.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum MonitorProbe {
    Code(Option<String>),
    Raised { class: String },
}

/// What the driver must do. Commands are produced in the order the oracle's thread would act.
#[derive(Clone, Debug, PartialEq)]
pub enum Cmd {
    /// Create the chain's `RunContext` (private state, stop flag) for this run.
    NewRun {
        run_id: String,
        deployment_id: String,
        reason: Reason,
        ceiling: Option<Value>,
        rollover_binding: Option<Value>,
    },
    /// `chain.run_step(step, ctx)`; end it with [`OwnerRun::step_finished`].
    RunStep { step: &'static str },
    /// `chain.teardown(ctx, failed=)`; end it with [`OwnerRun::teardown_finished`].
    Teardown { failed: bool },
    /// `ctx.stop_event.set()`: a waiting step must end with [`StepOutcome::Stopped`].
    SignalStop,
}

/// One `cleanup_errors` row (`record_exception`, `owner_run.py:71-87`).
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CleanupRow {
    pub code: String,
    pub exception_class: String,
    pub count: u64,
}

/// `ctx.result`: chain-owned results shown in status.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct RunResult {
    pub cleanup_errors: Vec<CleanupRow>,
    pub cleanup_unproven: Option<Value>,
    /// `registry_origin`, `participants`, `compilation_cache_hit`, `build_seconds` and any other
    /// key the chain sets.
    pub other: Map<String, Value>,
}

#[derive(Clone, Debug)]
struct Ctx {
    identity: Identity,
    reason: Reason,
    stop: bool,
    result: RunResult,
    ceiling: Option<Ceiling>,
    rollover_binding: Option<Value>,
    rollover_requested: bool,
    reached_serving: bool,
}

impl Ctx {
    fn new(identity: Identity, reason: Reason) -> Self {
        Self {
            identity,
            reason,
            stop: false,
            result: RunResult::default(),
            ceiling: None,
            rollover_binding: None,
            rollover_requested: false,
            reached_serving: false,
        }
    }
}

/// `record_exception(result, code, error)`: closed call-site code and class only, coalesced.
fn record_exception(result: &mut RunResult, code: &str, class: &str) -> String {
    let valid = {
        let b = class.as_bytes();
        !b.is_empty()
            && b.len() <= CODE_MAX
            && (b[0].is_ascii_alphabetic() || b[0] == b'_')
            && b.iter().all(|c| c.is_ascii_alphanumeric() || *c == b'_')
    };
    let name = if valid { class } else { "Exception" };
    if let Some(row) = result
        .cleanup_errors
        .iter_mut()
        .find(|r| r.code == code && r.exception_class == name)
    {
        row.count += 1;
    } else {
        result.cleanup_errors.push(CleanupRow {
            code: code.to_owned(),
            exception_class: name.to_owned(),
            count: 1,
        });
    }
    name.to_owned()
}

/// `_rollover` (`owner_run.py:149-150`).
#[derive(Clone, Debug, PartialEq)]
pub struct Rollover {
    pub state: &'static str,
    pub completed: u64,
    pub consecutive_failures: u32,
    pub retry_at: Option<f64>,
    pub last_error: Option<String>,
}

impl Rollover {
    fn idle() -> Self {
        Self {
            state: "idle",
            completed: 0,
            consecutive_failures: 0,
            retry_at: None,
            last_error: None,
        }
    }
}

/// Where the (oracle) run thread is.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Phase {
    /// No thread: never started, or the last session ended.
    Idle,
    /// Inside `chain.run_step`.
    Step,
    /// The serving hold loop.
    Holding,
    /// Inside `chain.teardown`.
    Teardown { failed: bool },
    /// `_main`'s wait for the next rollover attempt.
    Backoff { deadline: f64 },
}

/// The effective execution policy of a rollover attempt's backoff (`owner_run.py:379`).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Backoff {
    /// `rollover_backoff_seconds = 5.0` (`owner_run.py:152`).
    pub base_seconds: f64,
    /// `min(60.0, ...)` (`owner_run.py:379`).
    pub cap_seconds: f64,
}

impl Backoff {
    pub fn registered() -> Self {
        let secs = |id: budgets::BudgetId| {
            id.span(budgets::Entries::ZERO)
                .map(|s| s.millis() as f64 / 1000.0)
                .unwrap_or(f64::NAN)
        };
        Self {
            base_seconds: secs(
                budgets::BudgetId::PyFinalMyceliumA13MacosOwnerRunSelfRolloverBackoffSecondsL152C8,
            ),
            cap_seconds: secs(
                budgets::BudgetId::PyFinalMyceliumA13MacosOwnerRunL379C48CeilingOperand,
            ),
        }
    }
}

pub struct OwnerRun {
    identities: Box<dyn FnMut() -> Identity + Send>,
    state: State,
    steps: Vec<StepRow>,
    step: Option<usize>,
    failed_step: Option<usize>,
    error: Option<String>,
    error_message: Option<String>,
    error_fix: Option<Map<String, Value>>,
    reason: Option<Reason>,
    started_at: Option<f64>,
    ever_started: bool,
    rebuilding: bool,
    session_stop: bool,
    rollover: Rollover,
    ctx: Option<Ctx>,
    phase: Phase,
    /// `rebuild()` waiting for the old run's thread to end before it starts the fresh one.
    pending_rebuild: bool,
    pub backoff: Backoff,
}

fn fresh_steps() -> Vec<StepRow> {
    STEPS
        .iter()
        .map(|_| StepRow {
            state: StepState::Pending,
            detail: None,
        })
        .collect()
}

impl OwnerRun {
    pub fn new(identities: Box<dyn FnMut() -> Identity + Send>) -> Self {
        Self {
            identities,
            state: State::Idle,
            steps: fresh_steps(),
            step: None,
            failed_step: None,
            error: None,
            error_message: None,
            error_fix: None,
            reason: None,
            started_at: None,
            ever_started: false,
            rebuilding: false,
            session_stop: false,
            rollover: Rollover::idle(),
            ctx: None,
            phase: Phase::Idle,
            pending_rebuild: false,
            backoff: Backoff::registered(),
        }
    }

    // -- public (owner_run.py:157-331) ---------------------------------------------------------

    /// `active()`.
    pub fn active(&self) -> bool {
        self.state.active()
    }
    pub fn state(&self) -> State {
        self.state
    }
    /// A run thread is alive in the oracle (a driver call is outstanding or a hold/wait loop runs).
    pub fn thread_alive(&self) -> bool {
        self.phase != Phase::Idle
    }
    /// The serving hold loop is running: the driver should poll the ceiling and may monitor.
    /// A `rebuild()` waits for the old run's thread to end before it starts the fresh run.
    pub fn rebuild_pending(&self) -> bool {
        self.pending_rebuild
    }
    pub fn serving(&self) -> bool {
        self.state == State::Serving && self.ctx.is_some()
    }
    /// `ctx.state['execution_ceiling']`.
    pub fn ceiling(&self) -> Option<Value> {
        self.ctx
            .as_ref()
            .and_then(|c| c.ceiling.as_ref())
            .map(Ceiling::to_value)
    }
    pub fn run_id(&self) -> Option<&str> {
        self.ctx.as_ref().map(|c| c.identity.run_id.as_str())
    }

    /// `start(reason, execution_ceiling=)`: the refusals, then a fresh run whose first step begins.
    pub fn start(
        &mut self,
        reason: &str,
        execution_ceiling: Option<&Value>,
        clock: Clock,
    ) -> Result<Vec<Cmd>, OwnerRunError> {
        let reason = Reason::parse_request(reason)?;
        let ceiling = match execution_ceiling {
            Some(value) => {
                Some(ceiling::checked(value).map_err(|_| OwnerRunError::new(ceiling::INVALID))?)
            }
            None => None,
        };
        if self.state.active() {
            return Err(OwnerRunError::new("owner_run_active"));
        }
        let identity = (self.identities)();
        let mut ctx = Ctx::new(identity, reason);
        ctx.ceiling = ceiling;
        self.session_stop = false;
        self.rollover = Rollover::idle();
        self.install(ctx, reason, clock);
        self.ever_started = true;
        Ok(self.begin_step(0))
    }

    /// `stop()`.
    pub fn stop(&mut self) -> Vec<Cmd> {
        self.session_stop = true;
        let mut cmds = Vec::new();
        if self.state.active()
            && let Some(ctx) = self.ctx.as_mut()
        {
            self.state = State::Stopping;
            ctx.stop = true;
            cmds.push(Cmd::SignalStop);
            // The serving hold loop wakes on the stop event and tears down.
            if self.phase == Phase::Holding {
                cmds.extend(self.leave_hold());
            }
        }
        cmds
    }

    /// `rebuild_async()`: the refusals; Stop now; the fresh `reason='rebuild'` run starts once the
    /// old run's thread has ended (at once when none is alive). The status the call answers has
    /// `rebuild_requested` added by the host.
    pub fn rebuild_async(&mut self, clock: Clock) -> Result<Vec<Cmd>, OwnerRunError> {
        if !self.ever_started {
            return Err(OwnerRunError::new("owner_run_rebuild_requires_run"));
        }
        if self.rebuilding {
            return Err(OwnerRunError::new("owner_run_rebuild_in_progress"));
        }
        self.rebuilding = true;
        self.rebuild_common(clock)
    }

    /// The blocking `rebuild()` (`owner_run.py:284-297`): same as the async worker but a caller
    /// that is told the join timed out gets `owner_run_stop_incomplete` back.
    pub fn rebuild(&mut self, clock: Clock) -> Result<Vec<Cmd>, OwnerRunError> {
        if !self.ever_started {
            return Err(OwnerRunError::new("owner_run_rebuild_requires_run"));
        }
        self.rebuild_common(clock)
    }

    fn rebuild_common(&mut self, clock: Clock) -> Result<Vec<Cmd>, OwnerRunError> {
        let mut cmds = self.stop();
        if self.phase == Phase::Idle {
            // `thread.join()` returns at once: `start('rebuild')` with the same ceiling.
            self.rebuilding = false;
            cmds.extend(self.start_rebuild(clock)?);
        } else {
            self.pending_rebuild = true;
        }
        Ok(cmds)
    }

    fn start_rebuild(&mut self, clock: Clock) -> Result<Vec<Cmd>, OwnerRunError> {
        let ceiling = self.ceiling();
        self.start("rebuild", ceiling.as_ref(), clock)
    }

    /// `thread.join(join_seconds)` timed out with the old run's thread still alive. Blocking
    /// `rebuild`: the caller gets `owner_run_stop_incomplete` and nothing else changes. Async
    /// worker (`owner_run.py:311-316`): the code is recorded on the run, `_error` is set and the
    /// state is forced to `failed`; the old thread runs on (its teardown still ends it later).
    pub fn rebuild_join_expired(&mut self, asynchronous: bool) -> Result<(), OwnerRunError> {
        if !self.pending_rebuild || self.phase == Phase::Idle {
            return Ok(());
        }
        self.pending_rebuild = false;
        let error = OwnerRunError::new("owner_run_stop_incomplete");
        if asynchronous {
            if let Some(ctx) = self.ctx.as_mut() {
                record_exception(&mut ctx.result, &error.code, "OwnerRunError");
            }
            self.error = Some(error.code);
            self.state = State::Failed;
            self.rebuilding = false;
            Ok(())
        } else {
            Err(error)
        }
    }

    // -- chain callbacks (RunContext) ------------------------------------------------------------

    /// `ctx.progress(detail)`: `str(detail)[:200]` on the current step.
    pub fn progress(&mut self, detail: Option<&str>) {
        if self.ctx.is_some()
            && let Some(step) = self.step
        {
            self.steps[step].detail = detail.map(|d| d.chars().take(DETAIL_MAX).collect());
        }
    }

    /// `ctx.result[key] = value` (chain-owned results shown in status).
    pub fn set_result(&mut self, key: &str, value: Value) {
        let Some(ctx) = self.ctx.as_mut() else {
            return;
        };
        match key {
            "cleanup_unproven" => ctx.result.cleanup_unproven = Some(value),
            "cleanup_errors" => {}
            _ => {
                ctx.result.other.insert(key.to_owned(), value);
            }
        }
    }

    /// `ctx.state['rollover_binding']`: copied into each rollover run (`owner_run.py:393-395`).
    pub fn set_rollover_binding(&mut self, binding: Value) {
        if let Some(ctx) = self.ctx.as_mut() {
            ctx.rollover_binding = Some(binding);
        }
    }

    /// `ctx.record_exception(code, error)` for a chain that records its own cleanup diagnostics.
    pub fn record_exception(&mut self, code: &str, class: &str) {
        if let Some(ctx) = self.ctx.as_mut() {
            record_exception(&mut ctx.result, code, class);
        }
    }

    // -- the run thread's events -------------------------------------------------------------------

    /// `chain.run_step` ended (`_run_once`, `owner_run.py:404-446`).
    pub fn step_finished(&mut self, outcome: StepOutcome) -> Vec<Cmd> {
        if self.phase != Phase::Step {
            return Vec::new();
        }
        let Some(index) = self.step else {
            return Vec::new();
        };
        match outcome {
            StepOutcome::Ok => {
                self.steps[index].state = StepState::Done;
                if index + 1 < STEPS.len() {
                    return self.begin_step(index + 1);
                }
                self.after_last_step()
            }
            StepOutcome::Stopped => self.stopped_in_step(),
            StepOutcome::Refused(error) => {
                if let Some(ctx) = self.ctx.as_mut() {
                    record_exception(&mut ctx.result, &error.code, "OwnerRunError");
                }
                self.fail(index, &error.code, error.message, error.fix);
                self.enter_teardown(true)
            }
            StepOutcome::Exception { class } => {
                if let Some(ctx) = self.ctx.as_mut() {
                    record_exception(&mut ctx.result, STEP_FAILED, &class);
                }
                self.fail(index, STEP_FAILED, None, None);
                self.enter_teardown(true)
            }
        }
    }

    /// `chain.teardown` ended; `_main` decides what comes next (`owner_run.py:353-402`).
    pub fn teardown_finished(&mut self, result: Result<(), String>, clock: Clock) -> Vec<Cmd> {
        if !matches!(self.phase, Phase::Teardown { .. }) {
            return Vec::new();
        }
        if let Err(class) = result
            && let Some(ctx) = self.ctx.as_mut()
        {
            let name = record_exception(&mut ctx.result, "owner_teardown_failed", &class);
            // `{**ctx.result.get('cleanup_unproven', {}), 'code': ..., 'exception_class': ...}`
            let mut merged = match ctx.result.cleanup_unproven.take() {
                Some(Value::Object(old)) => old,
                _ => Map::new(),
            };
            merged.insert("code".into(), "owner_teardown_failed".into());
            merged.insert("exception_class".into(), name.into());
            ctx.result.cleanup_unproven = Some(Value::Object(merged));
        }
        self.main_after_run(clock)
    }

    /// One iteration of the serving hold loop (`_end_at_ceiling`, `owner_run.py:248-273`), also the
    /// first half of `status()`. `rollover_due` is `None` when the chain has no `rollover_due`.
    pub fn serving_poll(&mut self, rollover_due: Option<bool>, ceiling: CeilingProbe) -> Vec<Cmd> {
        if self.state != State::Serving || self.session_stop {
            return Vec::new();
        }
        let Some(ctx) = self.ctx.as_mut() else {
            return Vec::new();
        };
        if rollover_due == Some(true) {
            ctx.rollover_requested = true;
            self.rollover.state = "switching";
            self.state = State::Stopping;
            ctx.stop = true;
            let mut cmds = vec![Cmd::SignalStop];
            if self.phase == Phase::Holding {
                cmds.extend(self.leave_hold());
            }
            return cmds;
        }
        match ceiling {
            CeilingProbe::Unavailable | CeilingProbe::Code(None) => Vec::new(),
            CeilingProbe::Raised { class } => {
                record_exception(&mut ctx.result, "owner_ceiling_check_failed", &class);
                Vec::new()
            }
            CeilingProbe::Code(Some(code)) => self.fail_serving(&code),
        }
    }

    /// `status()`'s monitor (`owner_run.py:171-185`); only while the run is still serving.
    pub fn monitor_result(&mut self, probe: MonitorProbe) -> Vec<Cmd> {
        if self.state != State::Serving || self.ctx.is_none() {
            return Vec::new();
        }
        let code = match probe {
            MonitorProbe::Code(code) => code,
            MonitorProbe::Raised { class } => {
                if let Some(ctx) = self.ctx.as_mut() {
                    record_exception(&mut ctx.result, "owner_run_monitor_failed", &class);
                }
                Some("owner_run_monitor_failed".to_owned())
            }
        };
        match code.filter(|c| !c.is_empty()) {
            Some(code) => self.fail_serving(&code),
            None => Vec::new(),
        }
    }

    /// Time passes: `_main`'s backoff wait ends (`owner_run.py:384-402`), or notices a Stop.
    pub fn tick(&mut self, clock: Clock) -> Vec<Cmd> {
        let Phase::Backoff { deadline } = self.phase else {
            return Vec::new();
        };
        if self.session_stop {
            self.state = State::Stopped;
            self.rollover.state = "idle";
            self.rollover.retry_at = None;
            return self.thread_ended(clock);
        }
        if clock.mono < deadline {
            return Vec::new();
        }
        let identity = (self.identities)();
        let mut fresh = Ctx::new(identity, Reason::Rollover);
        if let Some(old) = &self.ctx {
            fresh.ceiling.clone_from(&old.ceiling);
            fresh.rollover_binding.clone_from(&old.rollover_binding);
        }
        self.install(fresh, Reason::Rollover, clock);
        self.rollover.state = "switching";
        self.rollover.retry_at = None;
        self.begin_step(0)
    }

    // -- internals -----------------------------------------------------------------------------

    /// Common to `start` and a rollover's fresh context.
    fn install(&mut self, ctx: Ctx, reason: Reason, clock: Clock) {
        self.state = State::Running;
        self.steps = fresh_steps();
        self.step = None;
        self.failed_step = None;
        self.error = None;
        self.error_message = None;
        self.error_fix = None;
        self.reason = Some(reason);
        self.started_at = Some(clock.wall);
        self.ctx = Some(ctx);
    }

    /// `_run_once`'s loop head: the step becomes active, then `ctx.check_stopped()`.
    fn begin_step(&mut self, index: usize) -> Vec<Cmd> {
        let mut cmds = Vec::new();
        if index == 0
            && let Some(ctx) = &self.ctx
        {
            cmds.push(Cmd::NewRun {
                run_id: ctx.identity.run_id.clone(),
                deployment_id: ctx.identity.deployment_id.clone(),
                reason: ctx.reason,
                ceiling: ctx.ceiling.as_ref().map(Ceiling::to_value),
                rollover_binding: ctx.rollover_binding.clone(),
            });
        }
        self.step = Some(index);
        self.steps[index].state = StepState::Active;
        self.phase = Phase::Step;
        if self.ctx.as_ref().is_some_and(|c| c.stop) {
            cmds.extend(self.stopped_in_step());
        } else {
            cmds.push(Cmd::RunStep {
                step: STEPS[index].0,
            });
        }
        cmds
    }

    /// `except _Stopped`: `record_exception('owner_run_stopped', ...)`, an active step goes back to
    /// pending; `failed` stays False.
    fn stopped_in_step(&mut self) -> Vec<Cmd> {
        if let Some(ctx) = self.ctx.as_mut() {
            record_exception(&mut ctx.result, "owner_run_stopped", "_Stopped");
        }
        if let Some(step) = self.step
            && self.steps[step].state == StepState::Active
        {
            self.steps[step].state = StepState::Pending;
        }
        self.enter_teardown(false)
    }

    /// All ten steps done: serving, then hold (`owner_run.py:426-439`).
    fn after_last_step(&mut self) -> Vec<Cmd> {
        if self.state == State::Running {
            self.state = State::Serving;
            let rollover = self
                .ctx
                .as_ref()
                .is_some_and(|c| c.reason == Reason::Rollover);
            if let Some(ctx) = self.ctx.as_mut() {
                ctx.reached_serving = true;
            }
            if rollover {
                self.rollover.completed += 1;
                self.rollover.consecutive_failures = 0;
            }
            self.rollover.state = "idle";
            self.rollover.retry_at = None;
        }
        self.phase = Phase::Holding;
        // `while not ctx.stop_event.wait(poll)`: an already-set event ends the hold at once.
        if self.ctx.as_ref().is_some_and(|c| c.stop) {
            return self.leave_hold();
        }
        Vec::new()
    }

    /// The hold loop ended: `failed = (state == 'failing')`, then the `finally` teardown.
    fn leave_hold(&mut self) -> Vec<Cmd> {
        let failed = self.state == State::Failing;
        self.enter_teardown(failed)
    }

    /// `finally:` `if state != 'failing': state = 'stopping'`; `chain.teardown(ctx, failed=)`.
    fn enter_teardown(&mut self, failed: bool) -> Vec<Cmd> {
        if self.state != State::Failing {
            self.state = State::Stopping;
        }
        self.phase = Phase::Teardown { failed };
        vec![Cmd::Teardown { failed }]
    }

    /// `_fail(ctx, step, code, message, fix)`.
    fn fail(
        &mut self,
        step: usize,
        code: &str,
        message: Option<String>,
        fix: Option<Map<String, Value>>,
    ) {
        self.failed_step = Some(step);
        self.error = Some(code.to_owned());
        self.error_message = message;
        self.error_fix = fix;
        self.steps[step].state = StepState::Failed;
        self.state = State::Failing;
    }

    /// `_fail(ctx, 'serving', OwnerRunError(code).code)` and the stop event.
    fn fail_serving(&mut self, code: &str) -> Vec<Cmd> {
        let code = OwnerRunError::new(code).code;
        let serving = STEPS.len() - 1;
        self.fail(serving, &code, None, None);
        if let Some(ctx) = self.ctx.as_mut() {
            ctx.stop = true;
        }
        let mut cmds = vec![Cmd::SignalStop];
        if self.phase == Phase::Holding {
            cmds.extend(self.leave_hold());
        }
        cmds
    }

    /// `_main` after `_run_once` returned (`owner_run.py:354-383`).
    fn main_after_run(&mut self, clock: Clock) -> Vec<Cmd> {
        let (continuing, unproven) = match &self.ctx {
            Some(ctx) => (
                ctx.rollover_requested
                    || (ctx.reason == Reason::Rollover
                        && !ctx.reached_serving
                        && self.state == State::Failing),
                ctx.result.cleanup_unproven.as_ref().is_some_and(truthy),
            ),
            None => (false, false),
        };
        if self.session_stop || !continuing {
            self.rollover.state = "idle";
            self.rollover.retry_at = None;
            self.state = if self.state == State::Failing {
                State::Failed
            } else {
                State::Stopped
            };
            return self.thread_ended(clock);
        }
        let mut fatal: Option<String> = None;
        if unproven {
            fatal = Some("rollover_cleanup_unproven".into());
        } else if matches!(
            self.error.as_deref(),
            Some("rollover_members_changed" | "rollover_model_changed")
        ) {
            fatal = self.error.clone();
        } else if self.state == State::Failing {
            self.rollover.consecutive_failures += 1;
            self.rollover.last_error.clone_from(&self.error);
            if self.rollover.consecutive_failures >= ROLLOVER_MAX_FAILURES {
                fatal = Some("rollover_failed".into());
            }
        }
        if let Some(code) = fatal {
            let step = self.step.unwrap_or(STEPS.len() - 1);
            self.fail(step, &code, None, None);
            self.state = State::Failed;
            self.rollover.state = "failed";
            self.rollover.retry_at = None;
            return self.thread_ended(clock);
        }
        let failures = self.rollover.consecutive_failures;
        let wait = if failures > 0 {
            let doubled =
                self.backoff.base_seconds * 2f64.powi(i32::try_from(failures - 1).unwrap_or(0));
            doubled.min(self.backoff.cap_seconds)
        } else {
            0.0
        };
        let deadline = clock.mono + wait;
        self.state = State::Running;
        self.rollover.state = if failures > 0 { "backoff" } else { "switching" };
        self.rollover.retry_at = (failures > 0).then_some(deadline);
        self.phase = Phase::Backoff { deadline };
        // `while clock() < deadline and not session_stop.wait(poll)`: no wait at all when due now.
        self.tick(clock)
    }

    /// The oracle thread ends; a pending `rebuild()` continues on its worker.
    fn thread_ended(&mut self, clock: Clock) -> Vec<Cmd> {
        self.phase = Phase::Idle;
        if self.pending_rebuild {
            self.pending_rebuild = false;
            self.rebuilding = false;
            return self.start_rebuild(clock).unwrap_or_default();
        }
        Vec::new()
    }

    // -- status (owner_run.py:188-214) ------------------------------------------------------------

    /// `status_snapshot()`: progress only, never a tick.
    pub fn snapshot(&self) -> crate::status::Status {
        let ctx = self.ctx.as_ref();
        crate::status::Status {
            state: self.state.as_str(),
            reason: self.reason.map(Reason::as_str),
            step: self.step.map(|i| STEPS[i].0),
            failed_step: self.failed_step.map(|i| STEPS[i].0),
            error_code: self.error.clone(),
            error_message: self.error_message.clone(),
            error_fix: self.error_fix.clone(),
            run_id: ctx.map(|c| c.identity.run_id.clone()),
            deployment_id: ctx.map(|c| c.identity.deployment_id.clone()),
            steps: self.steps.clone(),
            registry_origin: ctx.and_then(|c| c.result.other.get("registry_origin").cloned()),
            participants: ctx.and_then(|c| c.result.other.get("participants").cloned()),
            started_at: self.started_at,
            route_ready: self.state == State::Serving,
            cleanup_unproven: ctx.and_then(|c| c.result.cleanup_unproven.clone()),
            cleanup_errors: ctx.map_or_else(Vec::new, |c| c.result.cleanup_errors.clone()),
            rollover: self.rollover.clone(),
            compilation_cache_hit: ctx
                .and_then(|c| c.result.other.get("compilation_cache_hit").cloned()),
            build_seconds: ctx.and_then(|c| c.result.other.get("build_seconds").cloned()),
        }
    }
}

/// Python truthiness of a JSON value (`if ctx.result.get('cleanup_unproven')`).
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
