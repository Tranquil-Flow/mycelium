// SPDX-License-Identifier: AGPL-3.0-only
//! SE-SLICE1-RUNNER-VIEW public lifecycle contract over the real owning Runner.
//! State expectations retain FX-SLICE1-owner-run idle / stopped semantics; no GUI Quit claim.
use owner_run::chain::{StepEnv, StepError};
use owner_run::machine::{Identity, Reason};
use owner_run::runner::{Chain, Runner, RunnerView, SystemClocks, Timing};
use serde_json::Value;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
    mpsc,
};
use std::time::Duration;

struct HeldChain {
    entered: mpsc::Sender<()>,
    drops: Arc<AtomicUsize>,
}
impl Drop for HeldChain {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::SeqCst);
    }
}
impl Chain for HeldChain {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {}
    fn run_step(&mut self, _: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        self.entered.send(()).unwrap();
        // Even on assertion unwind Runner::drop sets Stop, wakes this wait and joins the driver.
        while !env.stopped() {
            env.pause(Timing::registered().poll.as_secs_f64());
        }
        Err(StepError::Stopped)
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
}

fn runner() -> (Runner, mpsc::Receiver<()>, Arc<AtomicUsize>) {
    let (tx, rx) = mpsc::channel();
    let drops = Arc::new(AtomicUsize::new(0));
    let runner = Runner::spawn(
        Box::new(HeldChain {
            entered: tx,
            drops: drops.clone(),
        }),
        Box::new(|| Identity {
            run_id: "view-run".into(),
            deployment_id: "view-deployment".into(),
        }),
        Arc::new(SystemClocks::new()),
        Timing::registered(),
    )
    .unwrap();
    (runner, rx, drops)
}

fn start_held(runner: &Runner, entered: &mpsc::Receiver<()>) {
    runner.start("start", None).unwrap();
    entered
        .recv_timeout(Duration::from_secs_f64(Timing::registered().rebuild_join))
        .unwrap();
}

#[test]
fn view_clone_and_drop_leave_driver_owned_and_alive() {
    fn assert_traits<T: Clone + Send + Sync>() {}
    assert_traits::<RunnerView>();
    let (runner, entered, drops) = runner();
    let view = runner.view();
    drop(view.clone());
    drop(view);
    assert_eq!(drops.load(Ordering::SeqCst), 0);
    start_held(&runner, &entered);
    let view = runner.view();
    assert!(view.active());
    assert_eq!(view.snapshot(), runner.snapshot());
    assert!(runner.thread_alive());
    assert_eq!(drops.load(Ordering::SeqCst), 0);
    drop(view);
    assert!(runner.thread_alive());
    runner.close();
    assert_eq!(drops.load(Ordering::SeqCst), 1);
}

#[test]
fn surviving_view_observes_close_and_keeps_only_final_state() {
    let (runner, entered, drops) = runner();
    let view = runner.view();
    start_held(&runner, &entered);
    assert!(view.active());
    assert_eq!(view.snapshot()["state"], "running");
    assert_eq!(view.while_inactive(|| panic!("active refresh")), None);
    runner.close();
    assert_eq!(
        drops.load(Ordering::SeqCst),
        1,
        "view must not retain the chain or driver"
    );
    assert!(!view.active());
    assert_eq!(view.snapshot()["state"], "stopped");
    assert_eq!(view.while_inactive(|| "refresh"), Some("refresh"));
    assert_eq!(view.clone().snapshot(), view.snapshot());
}

#[test]
fn surviving_view_observes_owning_drop_of_active_run() {
    let (runner, entered, drops) = runner();
    let view = runner.view();
    start_held(&runner, &entered);
    drop(runner);
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert!(!view.active());
    assert_eq!(view.snapshot()["state"], "stopped");
}

#[test]
fn idle_owner_close_does_not_invent_a_closed_state() {
    let (runner, _, drops) = runner();
    let view = runner.view();
    let before = view.snapshot();
    assert_eq!(before["state"], "idle");
    assert_eq!(view.while_inactive(|| "idle refresh"), Some("idle refresh"));
    runner.close();
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert_eq!(view.snapshot(), before);
    assert!(!view.active());
}
