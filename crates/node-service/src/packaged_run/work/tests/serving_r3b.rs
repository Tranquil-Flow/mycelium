// SPDX-License-Identifier: AGPL-3.0-only
//! Lane SRV patch 3b on the composed packaged Quit (REVIEW-laneSRV-r3-astra R3A-01, R3A-02): a
//! serving run's deferred workflow Stop (`workflow.stop()`, app.py:1835-1838 @ f4e366e9, issued
//! even after `run.join(60)` timed out, demo_app.py:199-203) is never skipped by the process exit.
//! Model-free: no native, no route. The shell's clock is the injected `Span` the lifecycle takes.
use super::serving_r3::{Facts, RealServingStep, StoppableWorkflow, hold, patience};
use super::*;
use api::actions::Action;
use budgets::Span;
use desktop::lifecycle::{Cleanup, CleanupError, QuitSource, Shutdown, State, quit_allowance};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;

/// A run parked in its serving wait after its Stop check: its first workflow read waits for
/// `release` (dropping it releases as well). Fields drop in order: the read is released first.
struct Served {
    release: mpsc::Sender<()>,
    _run: OwnerRun,
    config: RunConfig,
    f: Fixture,
}
fn served() -> Served {
    let f = Fixture::new();
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel::<()>();
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(RealServingStep {
        workflow: StoppableWorkflow {
            stopped: Arc::new(AtomicBool::new(false)),
            entered: Some(entered_tx),
            release: release_rx,
        },
    }));
    let config = RunConfig::default();
    let run = OwnerRun::install_with(
        f.host.clone(),
        &config,
        None,
        Box::new(move || chain.take().expect("one run")),
    );
    lock(&run.runs)
        .demo_action(Action::DemoStart, &Map::new(), &mut Facts)
        .expect("start");
    entered
        .recv_timeout(patience())
        .expect("the serving predicate reads the workflow after its Stop check");
    Served {
        release,
        _run: run,
        config,
        f,
    }
}

/// The packaged window's Quit as `packaged::compose` builds it (`assemble`, then the run's close
/// first), over model-free services and the test's run, its clock bound as the shell binds it.
fn window_quit(served: &Served) -> (Arc<dyn desktop::core::Core>, crate::packaged::AppCleanup) {
    let root = served.f.root.join("window");
    let dirs = crate::packaged::prepare_dirs(&root).unwrap();
    let node = NodeService::new(
        Arc::new(NoNative),
        "r3b-window".into(),
        crate::native::development_qualification().unwrap(),
    );
    let source = crate::app_host::ModelSource::hub(root.clone());
    let services = crate::desktop_host::DesktopServices::with_model_source(
        node,
        crate::packaged::CALLBACK_CAPACITY,
        &dirs.private,
        source.clone(),
    );
    let (core, mut cleanup) = crate::packaged::assemble(services, source);
    crate::packaged::close_run_first(&mut cleanup, served.config.clone());
    cleanup.bind_clock(Instant::now()).unwrap();
    (core, cleanup)
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r3-astra R3A-01: v0.1.4 calls `workflow.stop()` after
/// `run.join(60)` even when the join timed out (demo_app.py:199-203, app.py:1835-1838 @ f4e366e9);
/// through the composed Shutdown / AppCleanup, with a serving run that never ends and its close
/// thread still inside the join, the shell's deadline makes the Quit exit-eligible only once the
/// workflow Stop was issued.)
#[test]
fn at_the_shell_deadline_a_serving_quit_stops_the_workflow_before_it_may_exit() {
    let served = served();
    let stops = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let count = stops.clone();
    lock(&served.f.host.custody).work_stop = Some(Arc::new(move || {
        count.fetch_add(1, Ordering::SeqCst);
    }));
    let (_core, cleanup) = window_quit(&served);
    let allowance = quit_allowance().unwrap();
    let mut shutdown = Shutdown::new(cleanup, allowance).unwrap();
    shutdown.request(QuitSource::Menu, Span::ZERO);
    let before = (shutdown.tick(Span::ZERO), stops.load(Ordering::SeqCst));
    // The shell's clock reaches the Quit deadline; the run is still in its serving wait. The relay
    // step starts its close there (v0.1.4 closes the relay after the run's join, app.py:1848-1850)
    // and the Quit is ticked at the deadline until that close ended (lane CHATPAGE patch 3).
    let until = Instant::now() + patience();
    let at_deadline = loop {
        let state = shutdown.tick(allowance);
        if state != State::Stopping || Instant::now() >= until {
            break state;
        }
        std::thread::yield_now();
    };
    let exit = shutdown.exit_status();
    let stops_at_exit = stops.load(Ordering::SeqCst);
    lock(&served.f.host.custody).work_stop = None;
    assert_eq!(
        before,
        (State::Stopping, 0),
        "a serving run's workflow Stop waits for the run's join"
    );
    assert_eq!(
        (at_deadline, exit),
        (State::Failed(CleanupError::Deadline), Some(0)),
        "the allowance ran out: v0.1.4 exits 0"
    );
    assert_eq!(
        stops_at_exit, 1,
        "no exit may precede the workflow Stop (app.py:1835-1838)"
    );
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r3-astra R3A-01, the delayed close thread: the run ended and
/// the close thread is inside the workflow Stop when the shell's deadline arrives; the Quit stays
/// pending (no exit status) until that Stop has returned, then exits 0, and the workflow is
/// stopped once, not again by the deadline.)
#[test]
fn a_quit_deadline_waits_for_the_workflow_stop_the_close_thread_is_issuing() {
    let served = served();
    let stops = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let count = stops.clone();
    let (entered_tx, stop_entered) = mpsc::channel();
    let (finish, finish_rx) = mpsc::channel::<()>();
    let parked = Mutex::new((Some(entered_tx), finish_rx));
    lock(&served.f.host.custody).work_stop = Some(Arc::new(move || {
        count.fetch_add(1, Ordering::SeqCst);
        let mut parked = lock(&parked);
        if let Some(entered) = parked.0.take()
            && entered.send(()).is_ok()
        {
            hold(&parked.1);
        }
    }));
    let (_core, cleanup) = window_quit(&served);
    let allowance = quit_allowance().unwrap();
    let mut shutdown = Shutdown::new(cleanup, allowance).unwrap();
    // Dropped first when an assertion unwinds: the parked workflow Stop is released.
    let finish = finish;
    shutdown.request(QuitSource::Menu, Span::ZERO);
    // The serving read resumes: the run ends `stopped`, the close thread's join returns and it
    // issues the workflow Stop, which parks.
    served.release.send(()).expect("the serving read waits");
    let issuing = stop_entered.recv_timeout(patience());
    let at_deadline = shutdown.tick(allowance);
    let exit_while_issuing = shutdown.exit_status();
    finish.send(()).expect("the workflow Stop waits");
    let until = Instant::now() + patience();
    let after = loop {
        let state = shutdown.tick(allowance);
        if state != State::Stopping || Instant::now() >= until {
            break state;
        }
        std::thread::yield_now();
    };
    let exit = shutdown.exit_status();
    let stops_total = stops.load(Ordering::SeqCst);
    lock(&served.f.host.custody).work_stop = None;
    assert!(issuing.is_ok(), "the close thread issues the workflow Stop");
    assert_eq!(
        (at_deadline, exit_while_issuing),
        (State::Stopping, None),
        "no exit while the workflow Stop is still being issued"
    );
    assert_eq!(
        (after, exit),
        (State::Failed(CleanupError::Deadline), Some(0))
    );
    assert_eq!(stops_total, 1, "the workflow is stopped once");
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r3-astra R3A-02: a serving run's workflow Stop is deferred to
/// the close thread; when that thread cannot be spawned, Quit still issues the Stop, at once and
/// without any join, and reports the failed close step (v0.1.4 runs `workflow.stop()` even when the
/// preceding close step failed, app.py:1820-1825, 1835-1838 @ f4e366e9).)
#[test]
fn a_close_thread_that_cannot_start_still_stops_a_serving_workflow_and_reports_it() {
    let served = served();
    let stops = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let count = stops.clone();
    lock(&served.f.host.custody).work_stop = Some(Arc::new(move || {
        count.fetch_add(1, Ordering::SeqCst);
    }));
    let mut close = RunClose::new(served.config.clone());
    crate::packaged_run::refuse_next_close_spawn();
    close.begin();
    let stops_at_begin = stops.load(Ordering::SeqCst);
    let result = close.poll();
    lock(&served.f.host.custody).work_stop = None;
    assert_eq!(
        stops_at_begin, 1,
        "the workflow Stop is issued although the close thread never started"
    );
    assert_eq!(
        result,
        Some(Err(CleanupError::Poll)),
        "the failure is reported"
    );
}
