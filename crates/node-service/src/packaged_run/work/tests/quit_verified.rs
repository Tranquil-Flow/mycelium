// SPDX-License-Identifier: AGPL-3.0-only
//! Lane B release, B5 / QV (owner ruling 15 Nov 09:20, ledger row 57): Quit says `Verified` only
//! when its cleanup is PROVEN, otherwise `Unproven` with the named reason (exit 3, v0.1.4's
//! `cleanup_incomplete`, `__main__.py:219-223 @ f4e366e9`). Model-free: real packaged hosts on
//! loopback, real stage custody, no native worker and no model. The cases:
//! (a) the owner's run close leaves the owner's work owed (a stage it cannot prove released), and
//!     a joined Mac's work service still holds a stage at its Quit;
//! (c) a workflow Stop that never returns (row 44) still lets Quit end, `Unproven`, bounded.
//! Case (b) (a failed publish closes the prepared stage) is `api/tests/owner_work.rs`.
use super::regressions::{Rescue, setup, stage_root};
use super::serving_r3::patience;
use super::*;
use budgets::Span;
use desktop::lifecycle::{Cleanup, CleanupError, QuitSource, Shutdown, State, quit_allowance};
use std::os::unix::fs::PermissionsExt;
use std::sync::mpsc;

/// An owner run installed on the fixture's host, never started (no chain is ever built).
fn installed(f: &Fixture) -> (OwnerRun, RunConfig) {
    let config = RunConfig::default();
    let run = OwnerRun::install_with(
        f.host.clone(),
        &config,
        None,
        Box::new(|| panic!("no run starts in this test")),
    );
    (run, config)
}
/// The run's close (Quit's run step) polled to its result, bounded by `patience()`.
fn closed(config: &RunConfig) -> Result<(), CleanupError> {
    let mut close = RunClose::new(config.clone());
    close.begin();
    let until = Instant::now() + patience();
    loop {
        if let Some(result) = close.poll() {
            return result;
        }
        assert!(Instant::now() < until, "the run's close never ended");
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Proves: B5/QV (a), owner side (D8: Quit `Ok(Verified)` while the owner's own retained work
/// debt was unproven, REPORT-laneTEN). The run's work close fails on a real custody fault (the
/// owner's contributor stage), so the owner's work is retained as debt; Quit's run step retries
/// it once more and, still owed, answers `Unproven("owner_work_cleanup_unproven")`, never
/// `Verified`, and keeps the owner for the next close. Control: once the fault is gone, the same
/// owner settles and the run step is proven.
#[test]
fn a_quit_whose_run_close_leaves_the_owners_work_owed_is_unproven() {
    let (f, mut ports, state, mut env) = setup();
    let mut rescue = Rescue {
        work: ports.work.clone(),
        mode: None,
    };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    ports.work.node_slice_plans().unwrap();
    let stage = stage_root(&owner_ports::contributor_dir(&f.root.join("owner")));
    rescue.mode = Some(stage.clone());
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o755)).unwrap();
    let failed = ports.close_work();
    let owed = ports.work.retained();
    let (_run, config) = installed(&f);
    let quit = closed(&config);
    let kept = ports.work.retained();
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o700)).unwrap();
    rescue.mode = None;
    let control = closed(&config);
    let settled = !ports.work.retained();
    eprintln!(
        "B5_QV_OWNER_DEBT failed={failed:?} owed={owed} quit={quit:?} kept={kept} control={control:?} settled={settled}"
    );
    assert!(
        failed.is_err() && owed,
        "the fault must reach the owner's cleanup"
    );
    assert_eq!(
        quit,
        Err(CleanupError::Unproven("owner_work_cleanup_unproven")),
        "Quit's run step said proven while the owner's work is still owed"
    );
    assert!(kept, "the unproven owner is kept for the next close");
    assert_eq!(
        control,
        Ok(()),
        "the restored owner settles: the step is proven"
    );
    assert!(settled, "the settled debt is released");
}

/// Proves: B5/QV (a), joined Mac side: Quit's relay step closes the joined Mac's work service
/// (`Network::quit`); a service whose close cannot prove its stage released (a real custody
/// fault on the member's stage) makes that step `Unproven("member_work_cleanup_unproven")`, never
/// `Verified`, and the same Service keeps the stage for a later close, which releases it once
/// the fault is gone.
#[test]
fn a_joined_macs_quit_with_its_work_service_holding_a_stage_is_unproven() {
    let (f, mut ports, state, mut env) = setup();
    let _rescue = Rescue {
        work: ports.work.clone(),
        mode: None,
    };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    ports.work.node_slice_plans().unwrap();
    let stage = stage_root(&f.root.join("member"));
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o755)).unwrap();
    let mut relay = crate::packaged_network::RelayClose::new(f.network.clone());
    relay.begin();
    let until = Instant::now() + patience();
    let quit = loop {
        if let Some(result) = relay.poll() {
            break result;
        }
        assert!(Instant::now() < until, "the relay step never ended");
        std::thread::sleep(Duration::from_millis(20));
    };
    let kept = stage.is_dir();
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o700)).unwrap();
    f.network.close_work();
    let released = !stage.exists();
    eprintln!("B5_QV_MEMBER_WORK quit={quit:?} kept={kept} released={released}");
    assert_eq!(
        quit,
        Err(CleanupError::Unproven("member_work_cleanup_unproven")),
        "the joined Mac's Quit said proven while its work service holds a stage"
    );
    assert!(kept, "a failed close must not unlink the member's stage");
    assert!(
        released,
        "the same Service owner releases it once the fault is gone"
    );
}

/// The packaged window's Quit (`assemble`, then the run's close first) over model-free services
/// and `config`'s run.
fn window_quit(f: &Fixture, config: &RunConfig) -> crate::packaged::AppCleanup {
    let root = f.root.join("window");
    let dirs = crate::packaged::prepare_dirs(&root).unwrap();
    let node = NodeService::new(
        Arc::new(NoNative),
        "qv-window".into(),
        crate::native::development_qualification().unwrap(),
    );
    let source = crate::app_host::ModelSource::hub(root.clone());
    let services = crate::desktop_host::DesktopServices::with_model_source(
        node,
        crate::packaged::CALLBACK_CAPACITY,
        &dirs.private,
        source.clone(),
    );
    let (_core, mut cleanup) = crate::packaged::assemble(services, source);
    crate::packaged::close_run_first(&mut cleanup, config.clone());
    cleanup
}
/// What the shell's Quit ended with: its state, its exit status and the real time it took.
type Verdict = (State, Option<i32>, Duration);
/// The shell's Quit over `cleanup`, requested `into` its allowance ago (the cleanup's clock origin
/// bound that far back: no real wait), ticked every 20 ms on its own thread until it has a result.
fn quit_on_thread(
    mut cleanup: crate::packaged::AppCleanup,
    into: Duration,
) -> mpsc::Receiver<Verdict> {
    let allowance = quit_allowance().unwrap();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let began = Instant::now();
        let origin = began
            .checked_sub(into)
            .expect("the clock reaches that far back");
        cleanup.bind_clock(origin).unwrap();
        let mut shutdown = Shutdown::new(cleanup, allowance).unwrap();
        let shell_now = || Span::from_millis(u64::try_from(origin.elapsed().as_millis()).unwrap());
        shutdown.request(QuitSource::Menu, Span::ZERO);
        let state = loop {
            let state = shutdown.tick(shell_now());
            if state != State::Stopping {
                break state;
            }
            std::thread::sleep(Duration::from_millis(20));
        };
        tx.send((state, shutdown.exit_status(), began.elapsed()))
            .unwrap_or_default();
    });
    rx
}

/// Proves: B5/QV (c) (ledger row 44's residual: "if the workflow Stop itself never returns, Quit
/// never completes"; trunk issued that Stop on the window's thread). A run outside its serving
/// step has its workflow Stop issued as Quit begins; that Stop never returns here. Quit still
/// ends: at its deadline it waits for the Stop only within the post-deadline window the relay's
/// own close budgets already give it (lane CHATPAGE patch 3), then reports
/// `Unproven("workflow_stop_unreturned")` and exit 3, never `Verified` and never forever.
#[test]
fn a_workflow_stop_that_never_returns_still_ends_the_quit_unproven_within_its_bound() {
    let f = Fixture::new();
    let (_run, config) = installed(&f);
    let (entered_tx, entered) = mpsc::channel();
    let (release, held) = mpsc::channel::<()>();
    let parked = Mutex::new((Some(entered_tx), held));
    lock(&f.host.custody).work_stop = Some(Arc::new(move || {
        let mut parked = lock(&parked);
        if let Some(entered) = parked.0.take() {
            entered.send(()).unwrap_or_default();
            // Never returns while the test runs: released (or dropped) only after the verdict.
            parked.1.recv().unwrap_or(());
        }
    }));
    let allowance = Duration::from_millis(quit_allowance().unwrap().millis());
    let left = Duration::from_secs(2);
    let verdict = quit_on_thread(window_quit(&f, &config), allowance - left);
    let issued = entered.recv_timeout(patience());
    let relay = crate::packaged_network::RelayClose::allowance().unwrap();
    let ended = verdict.recv_timeout(left + relay + patience());
    drop(release);
    lock(&f.host.custody).work_stop = None;
    eprintln!("B5_QV_STOP_UNRETURNED issued={issued:?} ended={ended:?}");
    assert!(issued.is_ok(), "Quit issues the workflow Stop as it begins");
    let (state, exit, took) = ended.expect("Quit never ended: the workflow Stop held it");
    assert_eq!(
        (state, exit),
        (
            State::Failed(CleanupError::Unproven("workflow_stop_unreturned")),
            Some(3)
        ),
        "after {took:?}"
    );
    assert!(took >= left, "Quit ended before its deadline: {took:?}");
    assert!(took < left + relay + patience(), "{took:?}");
}
