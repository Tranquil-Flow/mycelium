// SPDX-License-Identifier: AGPL-3.0-only
//! The threaded loop that performs the machine's [`Cmd`]s on one thread (v0.1.4's `a13-owner-run`
//! thread, `owner_run.py _main/_run_once`), std only. Hosts (`api` headless, `node-service` desktop)
//! hand it a [`Chain`] over their ports and call [`Runner::apply`]/[`Runner::stop`]/...; no step logic
//! lives here or in a host.
//!
//! Concurrency rules (B-RUN-17, CL-L10-08): the run lock protects the in-memory machine only; it is never
//! held across a chain call, so a status read is answerable while a step waits. Lock order is `run`
//! then `queue`. `status()` is the oracle's live `OwnerRun.status()` (ceiling, rollover and monitor
//! checks, then the snapshot); `snapshot()` is `status_snapshot()` (never ticks anything).
#[cfg(test)]
mod fence_tests;
#[cfg(test)]
mod lifecycle_tests;
#[cfg(test)]
mod probe_tests;
#[cfg(test)]
mod view_tests;

use crate::action::{self, Applied, Plan, Refusal};
use crate::chain::{StepEnv, StepError};
use crate::machine::{
    CeilingProbe, Clock, Cmd, Identity, MonitorProbe, OwnerRun, OwnerRunError, Reason, StepOutcome,
};
use budgets::{BudgetId, Entries};
use serde_json::Value;
use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// `chain.run_step` / `chain.teardown` and the serving probes of v0.1.4's `AppDemoChain`, as the host
/// implements them over its real collaborators.
pub trait Chain: Send {
    /// A fresh `RunContext` (private state, clear stop event) for this run.
    fn new_run(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        reason: Reason,
        ceiling: Option<&Value>,
        rollover_binding: Option<&Value>,
    );
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError>;
    /// The descriptors step's `ctx.state['rollover_binding']`, copied into a successor context.
    /// Chains without descriptors have no binding; hosts must not reconstruct it from current members.
    fn rollover_binding(&self) -> Option<Value> {
        None
    }
    /// `chain.teardown(ctx, failed=)`; an error carries only its exception class. The teardown may leave
    /// results (`cleanup_unproven`) and diagnostics through `env`.
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String>;
    /// `chain.rollover_due(ctx)`; `None` when the chain has none.
    fn rollover_due(&mut self) -> Option<bool> {
        None
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        CeilingProbe::Unavailable
    }
    fn monitor(&mut self) -> MonitorProbe {
        MonitorProbe::Code(None)
    }
}

/// The two clocks a run reads (`time.monotonic`, `time.time`); injectable so tests drive time.
pub trait Clocks: Send + Sync {
    fn mono(&self) -> f64;
    fn wall(&self) -> f64;
    /// A simulated clock moves only when something waits on it: `pause` then advances it by the wait
    /// instead of sleeping (a 3,600 s budget runs in microseconds under test). The system clocks sleep.
    fn simulated(&self) -> bool {
        false
    }
    fn advance(&self, _seconds: f64) {}
}

/// The operating system's clocks.
pub struct SystemClocks {
    origin: Instant,
}
impl SystemClocks {
    pub fn new() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}
impl Default for SystemClocks {
    fn default() -> Self {
        Self::new()
    }
}
impl Clocks for SystemClocks {
    fn mono(&self) -> f64 {
        self.origin.elapsed().as_secs_f64()
    }
    fn wall(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    }
}

/// The intervals of the loop (registry ids, never literals).
#[derive(Clone, Copy, Debug)]
pub struct Timing {
    /// How often the idle loop looks at a serving run, a rebuild's join or a rollover backoff
    /// (`_main`'s `stop_event.wait(poll)`; `OwnerRunOwnerrunCeilingPollSL155`, 1 s).
    pub poll: Duration,
    /// `rebuild(join_seconds=180)`.
    pub rebuild_join: f64,
}
impl Timing {
    pub fn registered() -> Self {
        let span = |id: BudgetId| id.span(Entries::ZERO).map_or(0, |s| s.millis());
        Self {
            poll: Duration::from_millis(span(
                BudgetId::PyMyceliumA13MacosOwnerRunOwnerrunCeilingPollSL155,
            )),
            rebuild_join: span(BudgetId::PyMyceliumA13MacosOwnerRunOwnerrunRebuildJoinSecondsL284)
                as f64
                / 1000.0,
        }
    }
}

/// `ctx.stop_event`.
struct StopEvent {
    set: Mutex<bool>,
    changed: Condvar,
}
impl StopEvent {
    fn assign(&self, value: bool) {
        *lock(&self.set) = value;
        self.changed.notify_all();
    }
    fn is_set(&self) -> bool {
        *lock(&self.set)
    }
    /// `stop_event.wait(seconds)`: true when the event is set.
    fn wait(&self, span: Duration) -> bool {
        let guard = lock(&self.set);
        let (guard, _) = self
            .changed
            .wait_timeout_while(guard, span, |set| !*set)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        *guard
    }
}

/// A live `status()` waiting for the loop thread to run the serving checks.
struct Probe {
    done: Mutex<bool>,
    finished: Condvar,
}

#[derive(Default)]
struct Queue {
    cmds: VecDeque<Cmd>,
    probes: Vec<Arc<Probe>>,
    in_call: bool,
    closing: bool,
    /// Loop iterations that found nothing queued (tests wait on it to know a pass saw their input).
    idle_passes: u64,
}

struct Shared {
    run: Mutex<OwnerRun>,
    queue: Mutex<Queue>,
    wake: Condvar,
    stop: StopEvent,
    clocks: Arc<dyn Clocks>,
    timing: Timing,
}

impl Shared {
    fn clock(&self) -> Clock {
        Clock {
            mono: self.clocks.mono(),
            wall: self.clocks.wall(),
        }
    }
    /// Hand commands to the loop. A Stop in the same instant as a start is visible before the first step
    /// (`check_stopped`); a fresh context otherwise starts with a clear stop event.
    fn dispatch(&self, queue: &mut Queue, cmds: Vec<Cmd>) {
        let simultaneous = cmds.iter().any(|c| matches!(c, Cmd::SignalStop));
        if simultaneous {
            self.stop.assign(true);
        }
        for cmd in cmds {
            match cmd {
                Cmd::SignalStop => {}
                Cmd::NewRun { .. } => {
                    self.stop.assign(simultaneous);
                    queue.cmds.push_back(cmd);
                }
                other => queue.cmds.push_back(other),
            }
        }
        self.wake.notify_all();
    }
}

struct Env<'a> {
    shared: &'a Shared,
}
impl StepEnv for Env<'_> {
    fn progress(&mut self, detail: &str) {
        lock(&self.shared.run).progress(Some(detail));
    }
    fn stopped(&self) -> bool {
        self.shared.stop.is_set()
    }
    fn now(&mut self) -> f64 {
        self.shared.clocks.mono()
    }
    fn wall(&mut self) -> f64 {
        self.shared.clocks.wall()
    }
    fn set_result(&mut self, key: &str, value: Value) {
        lock(&self.shared.run).set_result(key, value);
    }
    fn record_exception(&mut self, code: &str, class: &str) {
        lock(&self.shared.run).record_exception(code, class);
    }
    /// The stop event is held across `commit` (`OwnerRun.stop` writes under the lock the oracle's
    /// commit holds, demo_chain.py:1073-1080). Lock order: dispatch takes run -> queue -> stop
    /// event; this takes the stop event -> the host's own locks, and no host lock holder ever
    /// takes the run, queue or stop event lock, so neither waits on the other.
    fn fenced(
        &mut self,
        commit: &mut dyn FnMut() -> Result<(), StepError>,
    ) -> Result<(), StepError> {
        let set = lock(&self.shared.stop.set);
        if *set {
            return Err(StepError::Stopped);
        }
        let result = commit();
        drop(set);
        result
    }
    fn pause(&mut self, seconds: f64) -> bool {
        if self.shared.clocks.simulated() {
            self.shared.clocks.advance(seconds);
            return self.shared.stop.is_set();
        }
        self.shared
            .stop
            .wait(Duration::try_from_secs_f64(seconds).unwrap_or(Duration::ZERO))
    }
}

/// A cloneable view of one installed run, with no ownership of its driver thread.
///
/// Dropping a view cannot stop or join the driver; [`Runner`] remains its sole owner.
/// A surviving view retains the machine's final snapshot after owner close/Drop (idle
/// stays idle, a stopped active run becomes stopped). It does not represent an absent
/// or lazily installed run: hosts retain that optional state themselves.
#[derive(Clone)]
pub struct RunnerView {
    shared: Arc<Shared>,
}

impl RunnerView {
    /// Read the machine only: no tick, clock, chain collaborator or live status probe.
    pub fn snapshot(&self) -> Value {
        lock(&self.shared.run).snapshot().to_value()
    }

    /// The same active-state predicate as [`Runner::active`], not driver liveness.
    pub fn active(&self) -> bool {
        lock(&self.shared.run).active()
    }

    /// Execute `f` iff inactive, atomically with respect to run transitions.
    /// Returns `None` without invoking `f` when active, otherwise `Some(f())`.
    ///
    /// The run lock is held across `f`; no queue lock is taken. Prepare slow work and
    /// collect external observations before calling this method. The callback must
    /// be short and nonblocking, must not call back into this run/view (including
    /// snapshot/active), and must not close/drop its owning Runner. Any host locks it
    /// takes must follow run -> host order everywhere; callers must not hold a host
    /// lock that a run-lock holder can need. This is not a general observer hook.
    pub fn while_inactive<R>(&self, f: impl FnOnce() -> R) -> Option<R> {
        let run = lock(&self.shared.run);
        if run.active() {
            return None;
        }
        let result = f();
        drop(run);
        Some(result)
    }
}

/// The run thread and its machine.
pub struct Runner {
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
    watchers: Mutex<Vec<JoinHandle<()>>>,
}

impl Runner {
    /// Share observation and the atomic inactive operation, never close/join ownership.
    pub fn view(&self) -> RunnerView {
        RunnerView {
            shared: self.shared.clone(),
        }
    }

    pub fn spawn(
        chain: Box<dyn Chain>,
        identities: Box<dyn FnMut() -> Identity + Send>,
        clocks: Arc<dyn Clocks>,
        timing: Timing,
    ) -> std::io::Result<Self> {
        let shared = Arc::new(Shared {
            run: Mutex::new(OwnerRun::new(identities)),
            queue: Mutex::new(Queue::default()),
            wake: Condvar::new(),
            stop: StopEvent {
                set: Mutex::new(false),
                changed: Condvar::new(),
            },
            clocks,
            timing,
        });
        let worker = shared.clone();
        let mut chain = chain;
        let thread = std::thread::Builder::new()
            .name("a13-owner-run".into())
            .spawn(move || drive(&worker, chain.as_mut()))?;
        Ok(Self {
            shared,
            thread: Some(thread),
            watchers: Mutex::new(Vec::new()),
        })
    }

    /// Run `f` on the machine under the run lock and hand the commands it returns to the loop. The result
    /// is `f`'s answer. Nothing blocking may happen inside `f`.
    pub fn command<T, E>(
        &self,
        f: impl FnOnce(&mut OwnerRun, Clock) -> Result<(Vec<Cmd>, T), E>,
    ) -> Result<T, E> {
        let mut run = lock(&self.shared.run);
        let (cmds, answer) = f(&mut run, self.shared.clock())?;
        let mut queue = lock(&self.shared.queue);
        self.shared.dispatch(&mut queue, cmds);
        let waiting = run.rebuild_pending();
        drop(queue);
        drop(run);
        if waiting {
            self.watch_rebuild_join(false);
        }
        Ok(answer)
    }

    /// `rebuild()`'s `thread.join(join_seconds)` in v0.1.4 runs on the caller (or the `a13-owner-rebuild`
    /// worker), never on the run thread, so a run thread blocked in its teardown cannot delay it.
    fn watch_rebuild_join(&self, asynchronous: bool) {
        let shared = self.shared.clone();
        let spawned = std::thread::Builder::new()
            .name("a13-owner-rebuild".into())
            .spawn(move || join_rebuild(&shared, asynchronous));
        match spawned {
            Ok(handle) => lock(&self.watchers).push(handle),
            Err(_) => lock(&self.shared.run)
                .rebuild_join_expired(asynchronous)
                .unwrap_or(()),
        }
    }

    /// Apply an admitted `/api/demo/*` plan; the answer is the status at that instant (taken under the same
    /// lock) and whether `rebuild_requested` is added.
    pub fn apply(&self, plan: &Plan) -> Result<(Value, bool), Refusal> {
        let mut run = lock(&self.shared.run);
        let Applied {
            cmds,
            rebuild_requested,
        } = action::apply(plan, &mut run, self.shared.clock())?;
        let answer = run.snapshot().to_value();
        let mut queue = lock(&self.shared.queue);
        self.shared.dispatch(&mut queue, cmds);
        let waiting = rebuild_requested && run.rebuild_pending();
        drop(queue);
        drop(run);
        if waiting {
            self.watch_rebuild_join(true);
        }
        Ok((answer, rebuild_requested))
    }

    pub fn start(&self, reason: &str, ceiling: Option<&Value>) -> Result<Value, OwnerRunError> {
        self.command(|run, clock| {
            let cmds = run.start(reason, ceiling, clock)?;
            Ok((cmds, run.snapshot().to_value()))
        })
    }

    pub fn stop(&self) -> Value {
        let cmds_and_answer = |run: &mut OwnerRun, _| -> Result<_, std::convert::Infallible> {
            let cmds = run.stop();
            Ok((cmds, run.snapshot().to_value()))
        };
        self.command(cmds_and_answer).unwrap_or(Value::Null)
    }

    /// `rebuild_async()`: Stop now; the fresh `rebuild` run starts when the old run's thread has ended.
    pub fn rebuild_async(&self) -> Result<Value, OwnerRunError> {
        let mut run = lock(&self.shared.run);
        let cmds = run.rebuild_async(self.shared.clock())?;
        let mut answer = run.snapshot().to_value();
        if let Some(o) = answer.as_object_mut() {
            o.insert("rebuild_requested".into(), true.into());
        }
        let mut queue = lock(&self.shared.queue);
        self.shared.dispatch(&mut queue, cmds);
        let waiting = run.rebuild_pending();
        drop(queue);
        drop(run);
        if waiting {
            self.watch_rebuild_join(true);
        }
        Ok(answer)
    }

    /// `status_snapshot()`: progress only, never a tick and never a chain call.
    pub fn snapshot(&self) -> Value {
        lock(&self.shared.run).snapshot().to_value()
    }

    /// `OwnerRun.status()`: while serving, the loop thread runs the ceiling / rollover / monitor checks (the
    /// chain lives on that thread) and the answer is the snapshot after them.
    pub fn status(&self) -> Value {
        let serving = lock(&self.shared.run).serving();
        if serving {
            let probe = Arc::new(Probe {
                done: Mutex::new(false),
                finished: Condvar::new(),
            });
            {
                let mut queue = lock(&self.shared.queue);
                if queue.closing {
                    // snapshot takes `run`; never invert the dispatch lock order (`run` -> `queue`).
                    drop(queue);
                    return self.snapshot();
                }
                queue.probes.push(probe.clone());
                self.shared.wake.notify_all();
            }
            let mut done = lock(&probe.done);
            while !*done {
                let (guard, _) = probe
                    .finished
                    .wait_timeout(done, self.shared.timing.poll)
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                done = guard;
                // End the queue guard's lifetime before testing the run state.
                let closing = lock(&self.shared.queue).closing;
                if closing || !lock(&self.shared.run).serving() {
                    break;
                }
            }
        }
        self.snapshot()
    }

    pub fn active(&self) -> bool {
        lock(&self.shared.run).active()
    }

    /// An oracle `thread.is_alive()`.
    pub fn thread_alive(&self) -> bool {
        lock(&self.shared.run).thread_alive()
    }

    /// Nothing queued and no chain call outstanding.
    pub fn quiescent(&self) -> bool {
        let queue = lock(&self.shared.queue);
        queue.cmds.is_empty() && !queue.in_call
    }

    /// Wake the loop (a test moved the injected clock).
    pub fn nudge(&self) {
        self.shared.wake.notify_all();
    }

    /// How many idle passes (backoff tick, join expiry, serving checks) the loop completed.
    pub fn idle_passes(&self) -> u64 {
        lock(&self.shared.queue).idle_passes
    }

    /// Stop the run and end the thread; a chain call in progress must return first.
    pub fn close(mut self) {
        self.end();
    }

    fn end(&mut self) {
        {
            let mut run = lock(&self.shared.run);
            let cmds = run.stop();
            let mut queue = lock(&self.shared.queue);
            self.shared.dispatch(&mut queue, cmds);
            queue.closing = true;
        }
        self.shared.wake.notify_all();
        self.shared.stop.assign(true);
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap_or(());
        }
        let watchers = std::mem::take(&mut *lock(&self.watchers));
        for watcher in watchers {
            watcher.join().unwrap_or(());
        }
    }
}

/// The rebuild worker: wait for the old run's thread to end, up to the join allowance; past it an async
/// rebuild forces `failed` with `owner_run_stop_incomplete` (the old thread still tears down later).
fn join_rebuild(shared: &Shared, asynchronous: bool) {
    let deadline = Instant::now() + Duration::from_secs_f64(shared.timing.rebuild_join);
    loop {
        {
            let run = lock(&shared.run);
            if !run.rebuild_pending() || !run.thread_alive() {
                return;
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            lock(&shared.run)
                .rebuild_join_expired(asynchronous)
                .unwrap_or(());
            return;
        }
        let queue = lock(&shared.queue);
        if queue.closing {
            return;
        }
        let step = remaining.min(shared.timing.poll);
        let waited = shared
            .wake
            .wait_timeout(queue, step)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        drop(waited);
    }
}

impl Drop for Runner {
    fn drop(&mut self) {
        self.end();
    }
}

/// The machine reports back after a command, under the run lock; the queue gets the next commands in the
/// same critical section that ends the call, so "quiescent" never shows a gap.
fn finish(shared: &Shared, event: impl FnOnce(&mut OwnerRun, Clock) -> Vec<Cmd>) {
    let mut run = lock(&shared.run);
    let cmds = event(&mut run, shared.clock());
    let mut queue = lock(&shared.queue);
    queue.in_call = false;
    shared.dispatch(&mut queue, cmds);
}

fn drive(shared: &Shared, chain: &mut dyn Chain) {
    loop {
        let next = {
            let mut queue = lock(&shared.queue);
            if queue.closing && queue.cmds.is_empty() {
                return;
            }
            let next = queue.cmds.pop_front();
            queue.in_call = next.is_some();
            next
        };
        let Some(cmd) = next else {
            idle(shared, chain);
            continue;
        };
        match cmd {
            Cmd::NewRun {
                run_id,
                deployment_id,
                reason,
                ceiling,
                rollover_binding,
            } => {
                chain.new_run(
                    &run_id,
                    &deployment_id,
                    reason,
                    ceiling.as_ref(),
                    rollover_binding.as_ref(),
                );
                finish(shared, |_, _| Vec::new());
            }
            Cmd::RunStep { step } => {
                // A Stop that arrived with the start is seen before the step begins.
                let outcome = if shared.stop.is_set() {
                    StepOutcome::Stopped
                } else {
                    match chain.run_step(step, &mut Env { shared }) {
                        Ok(()) => StepOutcome::Ok,
                        Err(StepError::Refused(e)) => StepOutcome::Refused(e),
                        Err(StepError::Stopped) => StepOutcome::Stopped,
                        Err(StepError::Exception { class }) => StepOutcome::Exception { class },
                    }
                };
                let binding = chain.rollover_binding();
                finish(shared, |run, _| {
                    if let Some(binding) = binding {
                        run.set_rollover_binding(binding);
                    }
                    run.step_finished(outcome)
                });
            }
            Cmd::Teardown { failed } => {
                let result = chain.teardown(failed, &mut Env { shared });
                finish(shared, |run, clock| run.teardown_finished(result, clock));
            }
            Cmd::SignalStop => finish(shared, |_, _| Vec::new()),
        }
    }
}

/// Nothing queued: the backoff wait ends, a rebuild's join may expire, a serving run is probed; then sleep
/// until work arrives or one poll interval passed.
fn idle(shared: &Shared, chain: &mut dyn Chain) {
    let (serving, probes) = {
        let mut run = lock(&shared.run);
        let clock = shared.clock();
        let cmds = run.tick(clock);
        let mut queue = lock(&shared.queue);
        let serving = run.serving() && run.thread_alive();
        if serving {
            queue.in_call = true;
        }
        let probes = std::mem::take(&mut queue.probes);
        shared.dispatch(&mut queue, cmds);
        (serving, probes)
    };
    if serving {
        let due = chain.rollover_due();
        // _end_at_ceiling returns immediately when rollover is due. Calling the
        // ceiling collaborator anyway can cancel work the source never cancels.
        let ceiling = if due == Some(true) {
            CeilingProbe::Unavailable
        } else {
            chain.ceiling_reached()
        };
        let monitor = {
            let mut run = lock(&shared.run);
            let cmds = run.serving_poll(due, ceiling);
            let mut queue = lock(&shared.queue);
            shared.dispatch(&mut queue, cmds);
            // Keep in_call set until the entire probe ends. No other driver can
            // install/serve a new context while this driver owns the chain;
            // Stop/rebuild may leave serving, which must suppress monitoring.
            !probes.is_empty() && run.serving()
        };
        // Never hold run/queue across host callbacks (including monitor).
        let result = monitor.then(|| chain.monitor());
        finish(shared, |run, _| {
            result.map_or_else(Vec::new, |probe| run.monitor_result(probe))
        });
    }
    for probe in probes {
        *lock(&probe.done) = true;
        probe.finished.notify_all();
    }
    let mut queue = lock(&shared.queue);
    queue.idle_passes += 1;
    if queue.cmds.is_empty() && !queue.closing && queue.probes.is_empty() {
        // Woken by a dispatch, or after one poll interval.
        let waited = shared
            .wake
            .wait_timeout(queue, shared.timing.poll)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        drop(waited);
    }
}
