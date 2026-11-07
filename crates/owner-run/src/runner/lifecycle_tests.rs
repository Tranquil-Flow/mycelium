// SPDX-License-Identifier: AGPL-3.0-only
//! Callback boundaries on the actual owning Runner, with scripted steps/IO.
//! No prepared workflow, atomic host publication, or native cleanup is implied.
use super::*;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    mpsc,
};

struct HeldHooks {
    at: &'static str,
    due: bool,
    entered: mpsc::Sender<&'static str>,
    release: mpsc::Receiver<()>,
    calls: Arc<Mutex<Vec<&'static str>>>,
    drops: Arc<AtomicUsize>,
}
impl HeldHooks {
    fn hold(&self, stage: &'static str) {
        if self.entered.send(stage).is_ok() {
            // A disconnected sender releases unwind before owning Runner Drop.
            match self.release.recv_timeout(patience()) {
                Ok(()) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    lock(&self.calls).push("release_disconnected");
                }
                Err(mpsc::RecvTimeoutError::Timeout) => panic!("test release timed out: {stage}"),
            }
        }
    }
    fn call(&self, name: &'static str) {
        lock(&self.calls).push(name);
        if self.at == name {
            self.hold(name);
        }
    }
}
impl Drop for HeldHooks {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::SeqCst);
    }
}
impl Chain for HeldHooks {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {}
    fn run_step(&mut self, step: &str, _: &mut dyn StepEnv) -> Result<(), StepError> {
        if step == "serving" {
            self.hold("serving-step");
        }
        Ok(())
    }
    fn teardown(&mut self, _: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        lock(&self.calls).push("teardown");
        assert!(env.stopped());
        // An unproven host cleanup stays observable, not a successful teardown.
        Err("CleanupUnavailable".into())
    }
    fn rollover_due(&mut self) -> Option<bool> {
        self.call("rollover_due");
        Some(self.due)
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        self.call("ceiling_reached");
        CeilingProbe::Code(None)
    }
    fn monitor(&mut self) -> MonitorProbe {
        self.call("monitor");
        MonitorProbe::Code(Some("stale_monitor_failure".into()))
    }
}
fn patience() -> Duration {
    Duration::from_secs_f64(Timing::registered().rebuild_join)
}

#[test]
fn disconnected_release_is_recorded_and_owning_runner_still_joins() {
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel();
    let calls = Arc::new(Mutex::new(vec![]));
    let drops = Arc::new(AtomicUsize::new(0));
    let runner = Runner::spawn(
        Box::new(HeldHooks {
            at: "rollover_due",
            due: false,
            entered: entered_tx,
            release: release_rx,
            calls: calls.clone(),
            drops: drops.clone(),
        }),
        Box::new(|| Identity {
            run_id: "disconnect-run".into(),
            deployment_id: "disconnect-deployment".into(),
        }),
        Arc::new(SystemClocks::new()),
        Timing::registered(),
    )
    .unwrap();
    // Same unwind-safe ordering as the held-hook test: disconnect before owner Drop joins.
    let release_on_unwind = release;
    runner.start("start", None).unwrap();
    assert_eq!(entered.recv_timeout(patience()).unwrap(), "serving-step");
    let view = runner.view();
    assert_eq!(runner.stop()["state"], "stopping");
    assert_eq!(drops.load(Ordering::SeqCst), 0);
    drop(release_on_unwind);
    drop(runner);
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert_eq!(*lock(&calls), ["release_disconnected", "teardown"]);
    let final_state = view.snapshot();
    assert_eq!(final_state["state"], "stopped");
    assert_eq!(
        final_state["cleanup_unproven"]["code"],
        "owner_teardown_failed"
    );
    assert_eq!(
        final_state["cleanup_unproven"]["exception_class"],
        "CleanupUnavailable"
    );
}

#[test]
fn held_serving_hooks_release_locks_stop_and_join_only_with_owner() {
    for (at, due, expected) in [
        ("rollover_due", true, vec!["rollover_due", "teardown"]),
        (
            "rollover_due",
            false,
            vec!["rollover_due", "ceiling_reached", "teardown"],
        ),
        (
            "ceiling_reached",
            false,
            vec!["rollover_due", "ceiling_reached", "teardown"],
        ),
        (
            "monitor",
            false,
            vec!["rollover_due", "ceiling_reached", "monitor", "teardown"],
        ),
    ] {
        let (entered_tx, entered) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let calls = Arc::new(Mutex::new(vec![]));
        let drops = Arc::new(AtomicUsize::new(0));
        let runner = Runner::spawn(
            Box::new(HeldHooks {
                at,
                due,
                entered: entered_tx,
                release: release_rx,
                calls: calls.clone(),
                drops: drops.clone(),
            }),
            Box::new(|| Identity {
                run_id: "hooks-run".into(),
                deployment_id: "hooks-deployment".into(),
            }),
            Arc::new(SystemClocks::new()),
            Timing::registered(),
        )
        .unwrap();
        // Declared after owner so assertion unwind disconnects before owner joins.
        let release = release_tx;
        runner.start("start", None).unwrap();
        assert_eq!(entered.recv_timeout(patience()).unwrap(), "serving-step");
        let probe = Arc::new(Probe {
            done: Mutex::new(false),
            finished: Condvar::new(),
        });
        lock(&runner.shared.queue).probes.push(probe.clone());
        release.send(()).unwrap();
        assert_eq!(entered.recv_timeout(patience()).unwrap(), at);
        let run_free = runner.shared.run.try_lock().is_ok();
        let queue_state = runner.shared.queue.try_lock().ok().map(|q| q.in_call);
        // Release first on a faulty lock-holding implementation: no hung Drop.
        if !run_free || queue_state != Some(true) {
            drop(release);
            runner.close();
            assert!(run_free, "{at}: callback held run lock");
            assert_eq!(
                queue_state,
                Some(true),
                "{at}: queue locked or false quiescence"
            );
            continue;
        }
        let view = runner.view();
        assert_eq!(view.snapshot()["state"], "serving");
        drop(view.clone());
        assert_eq!(drops.load(Ordering::SeqCst), 0);
        assert_eq!(runner.stop()["state"], "stopping");
        assert_eq!(view.snapshot()["state"], "stopping");
        assert!(runner.shared.stop.is_set());
        release.send(()).unwrap();
        // Both explicit close and owning Drop join; views own neither operation.
        if at == "monitor" {
            drop(runner);
        } else {
            runner.close();
        }
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        assert_eq!(
            *lock(&calls),
            expected,
            "{at} due={due}: source Stop callback order"
        );
        assert!(*lock(&probe.done));
        let final_state = view.snapshot();
        assert_eq!(final_state["state"], "stopped");
        assert_eq!(
            final_state["error_code"],
            Value::Null,
            "stale monitor cannot fail stopped context"
        );
        assert_eq!(
            final_state["cleanup_unproven"]["code"],
            "owner_teardown_failed"
        );
        assert_eq!(
            final_state["cleanup_unproven"]["exception_class"],
            "CleanupUnavailable"
        );
        println!("SERVING-LIFECYCLE-R18 {at} due={due}");
    }
}
