// SPDX-License-Identifier: AGPL-3.0-only
//! The rebuild watcher is bound to the rebuild it watches (REVIEW-laneSRV-r2-astra R2A-01).
//! v0.1.4's `rebuild()` joins THE thread it captured (owner_run.py:288-297) and only its own
//! worker clears `_rebuilding` (:318-319), so an old join can never expire a newer run's rebuild.
//! Schedule, deterministic (the watchers are parked at the run lock by a test gate; every
//! watcher's allowance is already spent when it first looks): rebuild A is admitted and its
//! watcher parks; A ends and its successor B starts; rebuild B is admitted and its watcher
//! parks; then A's old watcher resumes past its deadline.
use super::test_hooks::Gate;
use super::*;
use serde_json::json;
use std::sync::mpsc;

fn patience() -> Duration {
    Duration::from_secs_f64(Timing::registered().rebuild_join)
}

/// Waits for a release; a dropped sender (the test unwound) releases as well.
fn hold(release: &mpsc::Receiver<()>) {
    let timed_out = matches!(
        release.recv_timeout(patience()),
        Err(mpsc::RecvTimeoutError::Timeout)
    );
    assert!(!timed_out, "test release timed out");
}

/// Every run's first step parks until the test releases it, then ends as stopped.
struct Parked {
    entered: mpsc::Sender<usize>,
    releases: Vec<mpsc::Receiver<()>>,
    calls: usize,
}
impl Chain for Parked {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {}
    fn run_step(&mut self, _: &str, _: &mut dyn StepEnv) -> Result<(), StepError> {
        let call = self.calls;
        self.calls += 1;
        if self.entered.send(call).is_ok()
            && let Some(release) = self.releases.get(call)
        {
            hold(release);
        }
        Err(StepError::Stopped)
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
}

/// Proves: B-RUN-17 (REVIEW-laneSRV-r2-astra R2A-01: a rebuild watcher whose rebuild already
/// completed cannot expire a NEWER run's pending rebuild before that run's own allowance; the
/// newer rebuild's own watcher still expires it as `owner_run_stop_incomplete`
/// (owner_run.py:288-297, 304-319).)
#[test]
fn an_old_rebuild_watcher_cannot_expire_a_newer_rebuild() {
    let (entered_tx, entered) = mpsc::channel();
    let (release_a, release_a_rx) = mpsc::channel::<()>();
    let (release_b, release_b_rx) = mpsc::channel::<()>();
    let timing = Timing {
        rebuild_join: 0.0,
        ..Timing::registered()
    };
    let runner = Runner::spawn(
        Box::new(Parked {
            entered: entered_tx,
            releases: vec![release_a_rx, release_b_rx],
            calls: 0,
        }),
        Box::new(|| Identity {
            run_id: "watched-run".into(),
            deployment_id: "watched-deployment".into(),
        }),
        Arc::new(SystemClocks::new()),
        timing,
    )
    .expect("runner");
    // Declared after the Runner: dropped first when an assertion unwinds, so every parked
    // watcher and step is released before the Runner's Drop joins them.
    let gate = Gate::close(&runner.shared.run, "a13-owner-rebuild");
    let release_a = release_a;
    let release_b = release_b;

    runner.start("start", None).expect("start");
    assert_eq!(entered.recv_timeout(patience()), Ok(0), "run A's step");
    runner.rebuild_async().expect("rebuild A admitted");
    let old = gate.arrivals(1, patience()).expect("A's watcher parked")[0];

    release_a.send(()).expect("A's step waits");
    assert_eq!(
        entered.recv_timeout(patience()),
        Ok(1),
        "successor B's step"
    );
    runner.rebuild_async().expect("rebuild B admitted");
    let new = gate.arrivals(2, patience()).expect("B's watcher parked")[1];

    // A's watcher resumes long past its deadline, while B's rebuild is pending.
    gate.free(old);
    let watcher = lock(&runner.watchers).remove(0);
    assert_eq!(watcher.thread().id(), old, "the first watcher is A's");
    watcher.join().expect("A's watcher ends");
    let after_old = runner.snapshot();

    // B's own watcher, at its own (spent) allowance, still expires B's rebuild.
    gate.free(new);
    let watcher = lock(&runner.watchers).remove(0);
    watcher.join().expect("B's watcher ends");
    let after_new = runner.snapshot();

    release_b.send(()).expect("B's step waits");
    drop(gate);
    runner.close();
    assert_eq!(
        (&after_old["state"], &after_old["error_code"]),
        (&json!("stopping"), &Value::Null),
        "an old watcher must not expire the newer rebuild: {after_old}"
    );
    assert_eq!(
        (&after_new["state"], &after_new["error_code"]),
        (&json!("failed"), &json!("owner_run_stop_incomplete")),
        "the newer rebuild's own watcher expires it: {after_new}"
    );
}

/// Proves: B-RUN-17 (REVIEW-laneSRV-r2-astra R2A-01, the check/write gap: a watcher that checked
/// its OWN rebuild as pending and is about to expire it, but takes the run lock only after that
/// rebuild completed and a newer one was admitted, writes nothing. The expiry re-checks the
/// attempt under the same lock as its write (owner_run.py:304-316 fails only for its own join).)
#[test]
fn a_watcher_between_its_check_and_its_expiry_cannot_expire_a_newer_rebuild() {
    let (entered_tx, entered) = mpsc::channel();
    let (release_a, release_a_rx) = mpsc::channel::<()>();
    let (release_b, release_b_rx) = mpsc::channel::<()>();
    let timing = Timing {
        rebuild_join: 0.0,
        ..Timing::registered()
    };
    let runner = Runner::spawn(
        Box::new(Parked {
            entered: entered_tx,
            releases: vec![release_a_rx, release_b_rx],
            calls: 0,
        }),
        Box::new(|| Identity {
            run_id: "gap-run".into(),
            deployment_id: "gap-deployment".into(),
        }),
        Arc::new(SystemClocks::new()),
        timing,
    )
    .expect("runner");
    let gate = Gate::close(&runner.shared.run, "a13-owner-rebuild");
    let release_a = release_a;
    let release_b = release_b;

    runner.start("start", None).expect("start");
    assert_eq!(entered.recv_timeout(patience()), Ok(0), "run A's step");
    runner.rebuild_async().expect("rebuild A admitted");
    let old = gate
        .arrivals(1, patience())
        .expect("A's watcher at its check")[0];
    // A's watcher checks: its own rebuild is pending, A's thread alive, its allowance spent;
    // it comes back for the run lock to expire it.
    gate.pass(old);
    let second = gate
        .arrivals(2, patience())
        .expect("A's watcher at its expiry")[1];
    assert_eq!(second, old, "A's watcher came back to expire");

    release_a.send(()).expect("A's step waits");
    assert_eq!(
        entered.recv_timeout(patience()),
        Ok(1),
        "successor B's step"
    );
    runner.rebuild_async().expect("rebuild B admitted");
    let new = gate.arrivals(3, patience()).expect("B's watcher parked")[2];

    gate.free(old);
    let watcher = lock(&runner.watchers).remove(0);
    assert_eq!(watcher.thread().id(), old, "the first watcher is A's");
    watcher.join().expect("A's watcher ends");
    let after_old = runner.snapshot();

    gate.free(new);
    let watcher = lock(&runner.watchers).remove(0);
    watcher.join().expect("B's watcher ends");
    let after_new = runner.snapshot();

    release_b.send(()).expect("B's step waits");
    drop(gate);
    runner.close();
    assert_eq!(
        (&after_old["state"], &after_old["error_code"]),
        (&json!("stopping"), &Value::Null),
        "a watcher must not expire a rebuild that is not its own: {after_old}"
    );
    assert_eq!(
        (&after_new["state"], &after_new["error_code"]),
        (&json!("failed"), &json!("owner_run_stop_incomplete")),
        "the newer rebuild's own watcher expires it: {after_new}"
    );
}
