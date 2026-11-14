// SPDX-License-Identifier: AGPL-3.0-only
//! The threaded loop that performs the machine's [`Cmd`]s on one thread (v0.1.4's `a13-owner-run`
//! thread, `owner_run.py _main/_run_once`), std only. Hosts (`api` headless, `node-service` desktop)
//! hand it a [`Chain`] over their ports and call [`Runner::apply`]/[`Runner::stop`]/...; no step logic
//! lives here or in a host.
//!
//! Concurrency rules (B-RUN-17, CL-L10-08): the run lock protects the in-memory machine only; it is never
//! held across a chain call, so a status read is answerable while a step waits. The one exception is
//! the serving publication's short in-memory commit ([`StepEnv::fenced`]), which v0.1.4 also runs
//! under `OwnerRun._lock`. Lock order is `run` then `queue`. `status()` is the oracle's live
//! `OwnerRun.status()` (ceiling, rollover and monitor
//! checks, then the snapshot); `snapshot()` is `status_snapshot()` (never ticks anything).
#[cfg(test)]
mod fence_tests;
#[cfg(test)]
mod lifecycle_tests;
#[cfg(test)]
mod probe_tests;
#[cfg(test)]
mod replaced_tests;
#[cfg(test)]
mod test_hooks;
#[cfg(test)]
mod view_tests;
#[cfg(test)]
mod watcher_tests;

use crate::action::{self, Applied, Plan, Refusal};
use crate::chain::{StepEnv, StepError};
use crate::machine::{
    CeilingProbe, Clock, Cmd, Identity, MonitorProbe, OwnerRun, OwnerRunError, Reason,
    RebuildAttempt, StepOutcome,
};
use budgets::{BudgetId, Entries};
use serde_json::Value;
use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    #[cfg(test)]
    test_hooks::before_lock(mutex);
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

/// `ctx.stop_event`, one per run context (`RunContext.__init__`, owner_run.py:96): the event of the
/// context `context` names. An older context's event is set for good: a context is only replaced
/// after its Stop (a new Start needs an inactive machine, and the one way to leave a call
/// outstanding there is an async rebuild whose join expired after `stop()`).
struct StopEvent {
    set: Mutex<StopFlag>,
    changed: Condvar,
}
#[derive(Default)]
struct StopFlag {
    /// The machine generation of the context this event belongs to.
    context: u64,
    set: bool,
}
impl StopEvent {
    fn new() -> Self {
        Self {
            set: Mutex::new(StopFlag::default()),
            changed: Condvar::new(),
        }
    }
    /// Set or clear the current context's event.
    fn assign(&self, value: bool) {
        lock(&self.set).set = value;
        self.changed.notify_all();
    }
    /// A fresh context's own event; every older context's event stays set.
    fn renew(&self, context: u64, value: bool) {
        *lock(&self.set) = StopFlag {
            context,
            set: value,
        };
        self.changed.notify_all();
    }
    /// The current context's event (tests read it).
    #[cfg(test)]
    fn is_set(&self) -> bool {
        lock(&self.set).set
    }
    /// `ctx.stop_event.is_set()` for the context `context`.
    fn is_set_for(&self, context: u64) -> bool {
        let flag = lock(&self.set);
        flag.context != context || flag.set
    }
    /// `ctx.stop_event.wait(seconds)` for the context `context`: true when its event is set.
    fn wait_for(&self, context: u64, span: Duration) -> bool {
        let guard = lock(&self.set);
        let (guard, _) = self
            .changed
            .wait_timeout_while(guard, span, |flag| flag.context == context && !flag.set)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        guard.context != context || guard.set
    }
}

/// A live `status()` waiting for the loop thread to run the serving checks.
struct Probe {
    done: Mutex<bool>,
    finished: Condvar,
}

#[derive(Default)]
struct Queue {
    /// Each command with the machine generation it was dispatched under.
    cmds: VecDeque<(Cmd, u64)>,
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
    /// (`check_stopped`); a fresh context otherwise starts with its own clear stop event. The caller
    /// holds the run lock; `generation` is the machine's current context (a `NewRun` is always
    /// dispatched in the same critical section that installed its context).
    fn dispatch(&self, generation: u64, queue: &mut Queue, cmds: Vec<Cmd>) {
        let simultaneous = cmds.iter().any(|c| matches!(c, Cmd::SignalStop));
        if simultaneous {
            self.stop.assign(true);
        }
        for cmd in cmds {
            match cmd {
                Cmd::SignalStop => {}
                Cmd::NewRun { .. } => {
                    self.stop.renew(generation, simultaneous);
                    queue.cmds.push_back((cmd, generation));
                }
                other => queue.cmds.push_back((other, generation)),
            }
        }
        self.wake.notify_all();
    }
}

/// A chain call's `RunContext`: the run context `context` (a machine generation) it was made for.
/// Once a newer context replaced it, its event reads set and its progress, results and
/// diagnostics stay with it (owner_run.py:96-105, 334-337): nothing reaches the newer context.
struct Env<'a> {
    shared: &'a Shared,
    context: u64,
}
impl Env<'_> {
    /// The machine, only while this call's context is still the current one.
    fn current(&self) -> Option<MutexGuard<'_, OwnerRun>> {
        let run = lock(&self.shared.run);
        (run.generation() == self.context).then_some(run)
    }
}
impl StepEnv for Env<'_> {
    fn progress(&mut self, detail: &str) {
        if let Some(mut run) = self.current() {
            run.progress(Some(detail));
        }
    }
    fn stopped(&self) -> bool {
        self.shared.stop.is_set_for(self.context)
    }
    fn now(&mut self) -> f64 {
        self.shared.clocks.mono()
    }
    fn wall(&mut self) -> f64 {
        self.shared.clocks.wall()
    }
    fn set_result(&mut self, key: &str, value: Value) {
        if let Some(mut run) = self.current() {
            run.set_result(key, value);
        }
    }
    fn record_exception(&mut self, code: &str, class: &str) {
        if let Some(mut run) = self.current() {
            run.record_exception(code, class);
        }
    }
    /// One transaction for the publication and its result (`demo_chain.py:1074-1097`): the Stop
    /// check, the live run check, `commit` and `ctx.result[key] = value` all run under the run
    /// lock, the lock `OwnerRun.stop` and every status snapshot take (owner_run.py:194, 275-282),
    /// so a Stop or a status read lands before the check or after the result, never between the
    /// announcement and its origin. The check is bound to this call's own context: a replaced
    /// context's event is set (`_Stopped`, :1077-1080); a current context that is no longer
    /// `running` refuses `serving_workflow_identity_changed` (`owner._ctx is not ctx or
    /// owner._state != 'running'`, :1086-1088).
    ///
    /// Lock order (one order everywhere; `->` = acquired while holding):
    ///   host outer lock (the packaged resident workflow, the oracle's `app.guard`) -> run ->
    ///   stop event (read and released) -> host workflow lock -> exchange lock.
    ///   Stop / start / rebuild / close / the driver: run -> queue -> stop event.
    ///   `RunnerView::while_inactive` callbacks: run -> exchange lock.
    /// No holder of the queue, the stop event, a host workflow lock or the exchange lock ever
    /// takes the run lock, and nothing holding the run lock takes the host outer lock. Never take
    /// the run lock while holding the stop event: that inverts dispatch's order (ABBA).
    fn fenced(
        &mut self,
        commit: &mut dyn FnMut() -> Result<(), StepError>,
        key: &str,
        value: Value,
    ) -> Result<(), StepError> {
        let mut run = lock(&self.shared.run);
        if run.generation() != self.context
            || run.context_stopped()
            || self.shared.stop.is_set_for(self.context)
        {
            return Err(StepError::Stopped);
        }
        if run.state() != crate::machine::State::Running {
            return Err(OwnerRunError::new("serving_workflow_identity_changed").into());
        }
        commit()?;
        run.set_result(key, value);
        drop(run);
        Ok(())
    }
    fn pause(&mut self, seconds: f64) -> bool {
        if self.shared.clocks.simulated() {
            self.shared.clocks.advance(seconds);
            return self.shared.stop.is_set_for(self.context);
        }
        self.shared.stop.wait_for(
            self.context,
            Duration::try_from_secs_f64(seconds).unwrap_or(Duration::ZERO),
        )
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

/// The host maintenance's handle on one installed run: `OwnerRun.status()` (the ceiling, rollover
/// and monitor checks on the run's own thread, then the snapshot), with no ownership of that
/// thread. v0.1.4's `PublishedStatus.refresh` calls it from its own maintenance thread
/// (published_status.py:32-48, 72-76) while readers stay observational ([`RunnerView`]). Call it
/// outside every host lock; it waits only for the run thread's probe pass.
#[derive(Clone)]
pub struct RunnerProbe {
    shared: Arc<Shared>,
}

impl RunnerProbe {
    /// `OwnerRun.status()`.
    pub fn status(&self) -> Value {
        live_status(&self.shared)
    }
}

/// The run thread and its machine.
pub struct Runner {
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
    watchers: Mutex<Vec<JoinHandle<()>>>,
}

impl Runner {
    /// The live status for host maintenance, never close/join ownership.
    pub fn prober(&self) -> RunnerProbe {
        RunnerProbe {
            shared: self.shared.clone(),
        }
    }

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
            stop: StopEvent::new(),
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
        self.shared.dispatch(run.generation(), &mut queue, cmds);
        let waiting = run.rebuild_attempt();
        drop(queue);
        drop(run);
        if let Some(attempt) = waiting {
            self.watch_rebuild_join(false, attempt);
        }
        Ok(answer)
    }

    /// `rebuild()`'s `thread.join(join_seconds)` in v0.1.4 runs on the caller (or the `a13-owner-rebuild`
    /// worker), never on the run thread, so a run thread blocked in its teardown cannot delay it.
    /// The watcher is bound to `attempt`, read under the run lock that admitted the rebuild.
    fn watch_rebuild_join(&self, asynchronous: bool, attempt: RebuildAttempt) {
        let shared = self.shared.clone();
        let spawned = std::thread::Builder::new()
            .name("a13-owner-rebuild".into())
            .spawn(move || join_rebuild(&shared, asynchronous, attempt));
        match spawned {
            Ok(handle) => lock(&self.watchers).push(handle),
            Err(_) => lock(&self.shared.run)
                .rebuild_join_expired_for(asynchronous, attempt)
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
        self.shared.dispatch(run.generation(), &mut queue, cmds);
        let waiting = run.rebuild_attempt().filter(|_| rebuild_requested);
        drop(queue);
        drop(run);
        if let Some(attempt) = waiting {
            self.watch_rebuild_join(true, attempt);
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
        self.shared.dispatch(run.generation(), &mut queue, cmds);
        let waiting = run.rebuild_attempt();
        drop(queue);
        drop(run);
        if let Some(attempt) = waiting {
            self.watch_rebuild_join(true, attempt);
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
        live_status(&self.shared)
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
            self.shared.dispatch(run.generation(), &mut queue, cmds);
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

/// `OwnerRun.status()` ([`Runner::status`], [`RunnerProbe::status`]).
fn live_status(shared: &Shared) -> Value {
    let serving = lock(&shared.run).serving();
    if serving {
        let probe = Arc::new(Probe {
            done: Mutex::new(false),
            finished: Condvar::new(),
        });
        {
            let mut queue = lock(&shared.queue);
            if queue.closing {
                // snapshot takes `run`; never invert the dispatch lock order (`run` -> `queue`).
                drop(queue);
                return lock(&shared.run).snapshot().to_value();
            }
            queue.probes.push(probe.clone());
            shared.wake.notify_all();
        }
        let mut done = lock(&probe.done);
        while !*done {
            let (guard, _) = probe
                .finished
                .wait_timeout(done, shared.timing.poll)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            done = guard;
            // End the queue guard's lifetime before testing the run state.
            let closing = lock(&shared.queue).closing;
            if closing || !lock(&shared.run).serving() {
                break;
            }
        }
    }
    lock(&shared.run).snapshot().to_value()
}

/// The rebuild worker: wait for the old run's thread to end, up to the join allowance; past it an async
/// rebuild forces `failed` with `owner_run_stop_incomplete` (the old thread still tears down later).
/// Bound to its own `attempt`: once that rebuild completed (its attempt retired) it ends, and its
/// expiry is checked and written under one run lock, so it never acts on a newer rebuild.
fn join_rebuild(shared: &Shared, asynchronous: bool, attempt: RebuildAttempt) {
    let deadline = Instant::now() + Duration::from_secs_f64(shared.timing.rebuild_join);
    loop {
        {
            let run = lock(&shared.run);
            if run.rebuild_attempt() != Some(attempt) || !run.thread_alive() {
                return;
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            lock(&shared.run)
                .rebuild_join_expired_for(asynchronous, attempt)
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
    shared.dispatch(run.generation(), &mut queue, cmds);
}

/// [`finish`] for a call made for the run context `context`: the machine hears of it only while
/// that context is current. A replaced context's call ends without any machine event (its
/// outcome belongs to the old `RunContext`, `_fail`'s `ctx is not self._ctx`, owner_run.py:340-341)
/// and leaves the call open (`in_call`) for the caller to close; returns whether it was current.
fn finish_current(
    shared: &Shared,
    context: u64,
    event: impl FnOnce(&mut OwnerRun, Clock) -> Vec<Cmd>,
) -> bool {
    let mut run = lock(&shared.run);
    if run.generation() != context {
        return false;
    }
    let cmds = event(&mut run, shared.clock());
    let mut queue = lock(&shared.queue);
    queue.in_call = false;
    shared.dispatch(run.generation(), &mut queue, cmds);
    true
}

fn drive(shared: &Shared, chain: &mut dyn Chain) {
    // The run context the chain currently holds (the generation of its last `NewRun`).
    let mut context = 0;
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
        let Some((cmd, generation)) = next else {
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
                context = generation;
                finish(shared, |_, _| Vec::new());
            }
            Cmd::RunStep { step } => {
                // A Stop that arrived with the start is seen before the step begins.
                let outcome = if shared.stop.is_set_for(context) {
                    StepOutcome::Stopped
                } else {
                    match chain.run_step(step, &mut Env { shared, context }) {
                        Ok(()) => StepOutcome::Ok,
                        Err(StepError::Refused(e)) => StepOutcome::Refused(e),
                        Err(StepError::Stopped) => StepOutcome::Stopped,
                        Err(StepError::Exception { class }) => StepOutcome::Exception { class },
                    }
                };
                let binding = chain.rollover_binding();
                let failed = matches!(
                    outcome,
                    StepOutcome::Refused(_) | StepOutcome::Exception { .. }
                );
                let current = finish_current(shared, context, |run, _| {
                    if let Some(binding) = binding {
                        run.set_rollover_binding(binding);
                    }
                    run.step_finished(outcome)
                });
                if !current {
                    // A newer context replaced this one while its step ran (an async rebuild's
                    // join expired, then a Start): the old thread's `finally` still tears its own
                    // context down (owner_run.py:440-446), with every write kept in that context.
                    // A failure is recorded in that context only, which no status shows.
                    if let Err(class) = chain.teardown(failed, &mut Env { shared, context }) {
                        eprintln!("owner run: replaced context teardown failed ({class})");
                    }
                    finish(shared, |_, _| Vec::new());
                }
            }
            Cmd::Teardown { failed } => {
                let result = chain.teardown(failed, &mut Env { shared, context });
                if !finish_current(shared, context, |run, clock| {
                    run.teardown_finished(result, clock)
                }) {
                    finish(shared, |_, _| Vec::new());
                }
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
        shared.dispatch(run.generation(), &mut queue, cmds);
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
            shared.dispatch(run.generation(), &mut queue, cmds);
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
