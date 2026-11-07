// SPDX-License-Identifier: AGPL-3.0-only
//! The threaded [`Runner`] against traces recorded by EXECUTING v0.1.4's `owner_run.OwnerRun`
//! (FX-SLICE1-owner-run, the same 39 scenarios `oracle_owner_run.rs` replays through the deterministic
//! `Sim`). Here the commands are performed by the real loop thread: a scripted chain blocks in each step
//! or teardown until the script gives its outcome (exactly the probe's fake chain), time is an injected
//! clock the script advances, and every wait is an explicit condition (blocked at X, state is S, an idle
//! pass of the loop completed); no sleeps decide an outcome. Compared exactly: every snapshot, the
//! start/stop/rebuild answers and the chain's entries (reason, run id, deployment id, ceiling, `failed`).
//!
//! Not replayed here: the two scenarios that use the blocking `rebuild()` (the Runner offers the
//! non-blocking form HTTP uses; the blocking join is covered by the machine replay in `oracle_owner_run`).
mod support;
use owner_run::chain::{StepEnv, StepError};
use owner_run::machine::{CeilingProbe, Identity, MonitorProbe, OwnerRunError, Reason};
use owner_run::runner::{Chain, Clocks, Runner, Timing};
use serde_json::{Map, Value, json};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Instant;

const FIXTURE: &str = include_str!("fixtures/oracle_owner_run.json");
const ORACLE_CLOCK: f64 = 1000.0;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

struct VClock(Mutex<f64>);
impl Clocks for VClock {
    fn mono(&self) -> f64 {
        *lock(&self.0)
    }
    fn wall(&self) -> f64 {
        *lock(&self.0)
    }
}

#[derive(Default)]
struct Inner {
    blocked: Option<String>,
    given: Option<Value>,
    entered: Vec<Value>,
    ctx: (String, String, &'static str, Option<Value>),
    monitor: Value,
    ceiling: Value,
    rollover_due: bool,
    /// the step is inside `ctx.wait_until(lambda: False)`
    parked: bool,
    /// Release scripted calls on assertion unwinding, before Runner joins its thread.
    closing: bool,
    binding: Option<Value>,
    bindings: Vec<Value>,
}

#[derive(Clone)]
struct World(Arc<(Mutex<Inner>, Condvar)>);

struct ScriptChain(World);

fn obj(v: &Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

impl ScriptChain {
    fn block(&self, key: &str, failed: Option<bool>) -> Value {
        let (mutex, cv) = &*self.0.0;
        let mut inner = lock(mutex);
        let ctx = inner.ctx.clone();
        inner.entered.push(json!({
            "at": key, "reason": ctx.2, "run_id": ctx.0, "deployment_id": ctx.1,
            "ceiling": ctx.3, "failed": failed,
        }));
        inner.blocked = Some(key.to_owned());
        cv.notify_all();
        while inner.given.is_none() && !inner.closing {
            inner = cv
                .wait(inner)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
        inner.blocked = None;
        inner.given.take().unwrap_or(json!({"ok": true}))
    }
}

impl Chain for ScriptChain {
    fn new_run(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        reason: Reason,
        ceiling: Option<&Value>,
        binding: Option<&Value>,
    ) {
        let mut inner = lock(&self.0.0.0);
        inner.binding = binding.cloned();
        inner.bindings.push(json!([run_id, binding]));
        inner.ctx = (
            run_id.to_owned(),
            deployment_id.to_owned(),
            reason.as_str(),
            ceiling.cloned(),
        );
    }
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        let outcome = self.block(&format!("step:{step}"), None);
        for d in outcome["progress"].as_array().into_iter().flatten() {
            env.progress(d.as_str().unwrap_or_default());
        }
        for (k, v) in obj(&outcome["result"]) {
            env.set_result(&k, v);
        }
        if let Some(binding) = outcome["state"].get("rollover_binding") {
            lock(&self.0.0.0).binding = Some(binding.clone());
        }
        if let Some(e) = outcome.get("error") {
            let fix = e.get("fix").filter(|f| !f.is_null()).map(obj);
            return Err(OwnerRunError::with(
                e["code"].as_str().unwrap_or_default(),
                e["message"].as_str(),
                fix,
            )
            .into());
        }
        if let Some(class) = outcome["exception"].as_str() {
            return Err(StepError::Exception {
                class: class.to_owned(),
            });
        }
        if outcome["wait"] == true {
            // `ctx.wait_until(lambda: False)`: only Stop ends it
            lock(&self.0.0.0).parked = true;
            while !env.stopped() {
                env.pause(0.002);
            }
            lock(&self.0.0.0).parked = false;
            return Err(StepError::Stopped);
        }
        Ok(())
    }
    fn rollover_binding(&self) -> Option<Value> {
        lock(&self.0.0.0).binding.clone()
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        let outcome = self.block("teardown", Some(failed));
        for (k, v) in obj(&outcome["result"]) {
            env.set_result(&k, v);
        }
        match outcome["exception"].as_str() {
            Some(class) => Err(class.to_owned()),
            None => Ok(()),
        }
    }
    fn rollover_due(&mut self) -> Option<bool> {
        Some(lock(&self.0.0.0).rollover_due)
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        match &lock(&self.0.0.0).ceiling {
            Value::Null => CeilingProbe::Code(None),
            Value::String(s) => CeilingProbe::Code(Some(s.clone())),
            other => CeilingProbe::Raised {
                class: other["raise"].as_str().unwrap_or("Exception").to_owned(),
            },
        }
    }
    fn monitor(&mut self) -> MonitorProbe {
        match &lock(&self.0.0.0).monitor {
            Value::Null => MonitorProbe::Code(None),
            Value::String(s) => MonitorProbe::Code(Some(s.clone())),
            other => MonitorProbe::Raised {
                class: other["raise"].as_str().unwrap_or("Exception").to_owned(),
            },
        }
    }
}

fn eventually(what: &str, mut condition: impl FnMut() -> bool) {
    let started = Instant::now();
    while !condition() {
        assert!(
            started.elapsed() < support::patience(),
            "timed out waiting for {what}"
        );
        std::thread::sleep(support::tick() / 5);
    }
}

struct Harness {
    runner: Runner,
    world: World,
    clock: Arc<VClock>,
    flat: bool,
}

impl Drop for Harness {
    fn drop(&mut self) {
        // A failed snapshot assertion must not leave the chain blocked forever in a scripted
        // step/teardown while Runner::drop joins. Stop first, then release all present/future calls.
        self.runner.stop();
        let (mutex, cv) = &*self.world.0;
        lock(mutex).closing = true;
        cv.notify_all();
    }
}

impl Harness {
    fn new(backoff: Option<f64>, join: f64, flat: bool) -> Self {
        let world = World(Arc::new((
            Mutex::new(Inner {
                monitor: Value::Null,
                ceiling: Value::Null,
                ..Inner::default()
            }),
            Condvar::new(),
        )));
        let clock = Arc::new(VClock(Mutex::new(ORACLE_CLOCK)));
        let mut n = 0;
        let runner = Runner::spawn(
            Box::new(ScriptChain(world.clone())),
            Box::new(move || {
                n += 1;
                Identity {
                    run_id: format!("run-{n}"),
                    deployment_id: format!("dep-{n}"),
                }
            }),
            clock.clone(),
            Timing {
                poll: support::tick(),
                rebuild_join: join,
            },
        )
        .expect("the loop thread starts");
        if let Some(base) = backoff {
            // the probe patched the first backoff step before its first run
            runner
                .command::<_, std::convert::Infallible>(|run, _| {
                    run.backoff.base_seconds = base;
                    Ok((Vec::new(), ()))
                })
                .expect("infallible");
        }
        Self {
            runner,
            world,
            clock,
            flat,
        }
    }

    fn blocked(&self) -> Option<String> {
        lock(&self.world.0.0).blocked.clone()
    }

    /// The time the oracle's threads get between two probe operations: the backoff wait ends or notices a
    /// Stop and the serving loop polls its ceiling. Done when the loop completed a few idle passes, or when
    /// it is inside a chain call (the oracle's thread is then blocked too).
    fn settle(&self) {
        let before = self.runner.idle_passes();
        self.runner.nudge();
        eventually("an idle pass of the loop", || {
            self.runner.idle_passes() >= before + 3
                || self.blocked().is_some()
                || lock(&self.world.0.0).parked
        });
    }

    fn give(&self, key: &str, outcome: &Value) {
        eventually(&format!("the chain to block at {key}"), || {
            self.blocked().as_deref() == Some(key)
        });
        let (mutex, cv) = &*self.world.0;
        lock(mutex).given = Some(outcome.clone());
        cv.notify_all();
        self.settle();
    }

    fn norm(&self, mut v: Value) -> Value {
        let o = v.as_object_mut().expect("status is an object");
        let started = !o["started_at"].is_null();
        o.insert("started_at".into(), started.into());
        if self.flat {
            for row in o["cleanup_errors"].as_array_mut().into_iter().flatten() {
                let n = row["count"].as_u64().unwrap_or(0);
                row["count"] = u64::from(n >= 1).into();
            }
        }
        o.insert("active".into(), self.runner.active().into());
        v
    }

    fn state(&self) -> String {
        self.runner.snapshot()["state"]
            .as_str()
            .unwrap_or_default()
            .to_owned()
    }
}

fn replay(name: &str) {
    let fx: Value = serde_json::from_str(FIXTURE).expect("fixture parses");
    replay_fixture(name, &fx);
}

fn replay_fixture(name: &str, fx: &Value) {
    let backoff = fx["config"][name]["backoff"].as_f64();
    let flat = fx["config"][name]["flat_counts"].as_bool().unwrap_or(false);
    let scenario = &fx["scenarios"][name];
    let script = fx["scripts"][name].as_array().expect("script");
    let patched = script.iter().find(|o| o["op"] == "patch_rebuild_join");
    let join = patched.map_or(180.0, |p| p["seconds"].as_f64().expect("seconds"));
    let h = Harness::new(backoff, join, flat);
    let mut recorded = scenario["ops"].as_array().expect("ops").iter();
    let mut seen = 0usize;
    for op in script {
        let kind = op["op"].as_str().expect("op kind");
        match kind {
            "start" => {
                seen += 1;
                let r = recorded.next().expect("a record");
                let reason = op["reason"].as_str().unwrap_or("start").to_owned();
                let ceiling = op.get("ceiling").filter(|c| !c.is_null());
                match h.runner.start(&reason, ceiling) {
                    Ok(answer) => {
                        assert_eq!(r["ok"], true, "{name}: start should have been refused: {r}");
                        assert_eq!(r["state"], answer["state"], "{name}: start state");
                        assert_eq!(r["reason"], answer["reason"], "{name}: start reason");
                        assert_eq!(r["run_id"], answer["run_id"], "{name}: run id");
                    }
                    Err(e) => {
                        assert_eq!(r["ok"], false, "{name}: start was refused with {}", e.code);
                        assert_eq!(r["code"], e.code, "{name}: start refusal");
                    }
                }
            }
            "start_stop_atomic" => {
                h.runner
                    .command(|run, clock| {
                        let mut cmds = run.start("start", None, clock)?;
                        cmds.extend(run.stop());
                        Ok::<_, OwnerRunError>((cmds, ()))
                    })
                    .expect("start");
            }
            "stop" | "rebuild_async" => {
                seen += 1;
                let r = recorded.next().expect("a record");
                let outcome = if kind == "stop" {
                    Ok(h.runner.stop())
                } else {
                    h.runner.rebuild_async()
                };
                match outcome {
                    Ok(answer) => {
                        assert_eq!(
                            r["ok"], true,
                            "{name}: {kind} should have been refused: {r}"
                        );
                        assert_eq!(r["state"], answer["state"], "{name}: {kind} state");
                        assert_eq!(
                            r["rebuild_requested"].as_bool(),
                            (kind == "rebuild_async").then_some(true),
                            "{name}: rebuild_requested"
                        );
                    }
                    Err(e) => {
                        assert_eq!(r["ok"], false, "{name}: {kind} was refused with {}", e.code);
                        assert_eq!(r["code"], e.code, "{name}: {kind} refusal");
                    }
                }
                h.settle();
            }
            "status" => {
                seen += 1;
                let r = recorded.next().expect("a record");
                let answer = h.runner.status();
                h.settle();
                assert_eq!(
                    r["snapshot"],
                    h.norm(answer),
                    "{name}: live status snapshot"
                );
            }
            "await" => {
                let what = op["what"].as_str().expect("what");
                if let Some(key) = what.strip_prefix("blocked:") {
                    eventually(&format!("{name}: the chain to block at {key}"), || {
                        h.blocked().as_deref() == Some(key)
                    });
                } else if let Some(state) = what.strip_prefix("state:") {
                    eventually(&format!("{name}: state {state}"), || h.state() == state);
                } else if what == "finished" {
                    eventually(&format!("{name}: the run thread to end"), || {
                        !h.runner.thread_alive()
                    });
                } else if let Some(state) = what.strip_prefix("rollover:") {
                    eventually(&format!("{name}: rollover {state}"), || {
                        h.runner.snapshot()["rollover"]["state"] == state
                    });
                } else if what == "rebuild_idle" {
                    // the old thread did not end within the join: time passes, the loop gives up
                    if patched.is_some() {
                        // the join allowance (0.05 s, as patched in the probe) runs out in real time
                        eventually(&format!("{name}: the rebuild worker to give up"), || {
                            h.state() == "failed"
                        });
                    }
                } else {
                    panic!("unknown await {what}");
                }
            }
            "give" => h.give(
                op["key"].as_str().expect("key"),
                &op.get("outcome").cloned().unwrap_or(json!({"ok": true})),
            ),
            "facts" => {
                {
                    let mut inner = lock(&h.world.0.0);
                    for (k, v) in op["facts"].as_object().expect("facts") {
                        match k.as_str() {
                            "monitor" => inner.monitor = v.clone(),
                            "ceiling" => inner.ceiling = v.clone(),
                            "rollover_due" => inner.rollover_due = v.as_bool().unwrap_or(false),
                            other => panic!("unknown fact {other}"),
                        }
                    }
                }
                h.settle();
            }
            "clock" => {
                *lock(&h.clock.0) += op["advance"].as_f64().expect("advance");
                h.settle();
            }
            "patch_rebuild_join" | "snap" => {}
            other => panic!("unknown op {other}"),
        }
        let wants_snapshot = kind == "snap" || op.get("snap").is_some_and(|s| !s.is_null());
        if wants_snapshot {
            let r = recorded.next().expect("an expected snapshot");
            seen += 1;
            assert_eq!(r["op"], "snap");
            assert_eq!(
                r["snapshot"],
                h.norm(h.runner.snapshot()),
                "{name}: snapshot {:?} differs from the executed oracle",
                r["label"]
            );
        }
    }
    assert!(
        recorded.next().is_none(),
        "{name}: unreplayed expected records"
    );
    // the probe leaves nothing running: stop, then finish whatever the chain is blocked in
    h.runner.stop();
    eventually(
        &format!("{name}: the run thread to end after the probe's cleanup"),
        || {
            if let Some(_key) = h.blocked() {
                let (mutex, cv) = &*h.world.0;
                lock(mutex).given = Some(json!({"ok": true}));
                cv.notify_all();
            }
            !h.runner.thread_alive()
        },
    );
    assert!(seen > 0, "{name}: nothing was compared");
    assert_eq!(
        Value::Array(lock(&h.world.0.0).entered.clone()),
        scenario["entered"],
        "{name}: the chain was called differently from the oracle"
    );
    if let Some(bindings) = scenario.get("bindings") {
        assert_eq!(
            json!(lock(&h.world.0.0).bindings),
            *bindings,
            "{name}: inherited descriptor binding"
        );
    }
    drop(h); // joins the runner after releasing any scripted calls, including on assertion failure
}

/// Proves: B-CONSENT-15, B-ROLL-02 (the descriptors binding survives the actual Runner context boundary).
#[test]
fn descriptor_binding_survives_rollover() {
    let fx: Value =
        serde_json::from_str(include_str!("fixtures/oracle_rollover_binding_r12.json")).unwrap();
    replay_fixture("descriptor_binding_survives_rollover", &fx);
}

/// Harness contract: a failed comparison releases the scripted chain before joining the driver.
#[test]
fn a_failed_assertion_reaps_the_scripted_runner() {
    let h = Harness::new(None, Timing::registered().rebuild_join, false);
    let world = h.world.clone();
    let failed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
        h.runner.start("start", None).expect("start");
        eventually("model step to block", || {
            h.blocked().as_deref() == Some("step:model")
        });
        panic!("injected comparison failure");
    }));
    assert!(failed.is_err());
    assert!(lock(&world.0.0).blocked.is_none());
    // The ScriptChain owned by the runner thread has been dropped, not detached.
    assert_eq!(Arc::strong_count(&world.0), 1);
}

macro_rules! scenario {
    ($($(#[$doc:meta])* $name:ident),* $(,)?) => {$(
        $(#[$doc])*
        #[test]
        fn $name() { replay(stringify!($name)); }
    )*};
}

scenario! {
    /// Proves: B-RUN-01, B-RUN-04, B-RUN-17 (ten steps in order, serving, Stop tears down, snapshots never tick).
    happy,
    /// Proves: B-RUN-03 (a run-own refusal keeps its code, message and fix; the failed step is named).
    step_error_model_with_message_fix,
    /// Proves: B-RUN-03.
    step_error_mid_chain,
    /// Proves: B-RUN-03 (an unclosed code becomes owner_run_step_failed).
    step_error_code_not_closed,
    /// Proves: B-RUN-03 (the 80 / 81 character code boundary).
    step_error_code_80_and_81_chars,
    /// Proves: B-RUN-03 (progress and message truncation).
    step_progress_truncation,
    /// Proves: B-RUN-03 (an unexpected exception keeps only its class).
    step_generic_exception,
    /// Proves: B-RUN-04 (Stop interrupts a waiting step; a new Start is allowed).
    stop_during_step,
    /// Proves: B-RUN-04.
    stop_when_idle,
    /// Proves: B-RUN-04, B-RUN-03.
    teardown_exception_after_stop,
    /// Proves: B-RUN-03.
    teardown_exception_after_failure_with_chain_unproven,
    /// Proves: B-RUN-03.
    teardown_ok_with_chain_unproven,
    /// Proves: B-RUN-02 (a second Start while running is refused; refusal ladder of the machine).
    start_refusals,
    /// Proves: B-RUN-15, B-CONSENT-16 (Rebuild keeps the execution ceiling).
    start_with_ceiling_then_rebuild,
    /// Proves: B-RUN-15 (a rebuild whose old run will not end gives up and fails).
    rebuild_async_stop_incomplete,
    /// Proves: B-RUN-15.
    rebuild_of_failed_run_runs_fresh,
    /// Proves: B-RUN-10 (the owner ends the run at its wall allowance).
    ceiling_reached_in_serving,
    /// Proves: B-RUN-10.
    ceiling_check_raises_then_clears,
    /// Proves: B-RUN-17, B-RUN-03 (the monitor runs on a live status read only).
    monitor_code_via_status,
    /// Proves: B-RUN-17, B-RUN-03.
    monitor_raises_via_status,
    /// Proves: B-RUN-03.
    monitor_code_bad_closure,
    /// Proves: B-RUN-17.
    status_while_not_serving_skips_monitor,
    /// Proves: B-ROLL-02, B-ROLL-04.
    rollover_success,
    /// Proves: B-ROLL-04 (backoff 5 s, 10 s, then fatal after three failures).
    rollover_backoff_then_fatal,
    /// Proves: B-ROLL-04.
    rollover_failure_then_success,
    /// Proves: B-ROLL-04 (backoff capped at 60 s).
    rollover_backoff_capped_60,
    /// Proves: B-CONSENT-15 (rollover_members_changed is fatal).
    rollover_members_changed_fatal,
    /// Proves: B-CONSENT-15 (rollover_model_changed is fatal).
    rollover_model_changed_fatal,
    /// Proves: B-ROLL-02 (an unproven cleanup stops the continuation).
    rollover_cleanup_unproven_fatal,
    /// Proves: B-ROLL-02, B-RUN-04 (Stop during the backoff is final).
    stop_during_rollover_backoff,
    /// Proves: B-ROLL-02, B-RUN-04.
    stop_during_rollover_teardown,
    /// Proves: B-ROLL-02, B-RUN-04.
    rollover_run_stopped_by_user_midstep,
    /// Proves: B-ROLL-02, B-RUN-10.
    rollover_due_wins_over_ceiling,
    /// Proves: B-RUN-10.
    serving_failure_not_rollover_is_final,
    /// Proves: B-RUN-03, B-RUN-04 (teardown gets failed=false on Stop, true on failure).
    teardown_runs_with_failed_false_on_stop_and_true_on_failure,
    /// Proves: B-RUN-04 (a Stop that arrives with the start is seen before the first step).
    stop_before_first_step_runs,
    /// Proves: B-RUN-04.
    stop_after_step_ok_before_next,
}
