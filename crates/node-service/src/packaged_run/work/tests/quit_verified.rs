// SPDX-License-Identifier: AGPL-3.0-only
//! Lane B release, B5 / QV (owner ruling 15 Nov 09:20, ledger row 57): Quit says `Verified` only
//! when its cleanup is PROVEN, otherwise `Unproven` with the named reason (exit 3, v0.1.4's
//! `cleanup_incomplete`, `__main__.py:219-223 @ f4e366e9`). Model-free: real packaged hosts on
//! loopback, real stage custody, no native worker and no model. The cases:
//! (a) the owner's run close leaves the owner's work owed (a stage it cannot prove released), and
//!     a joined Mac's work service still holds a stage at its Quit;
//! Case (b) (a failed publish closes the prepared stage) is `api/tests/owner_work.rs`.
use super::regressions::{Rescue, setup, stage_root};
use super::serving_r3::patience;
use super::*;
use desktop::lifecycle::CleanupError;
use std::os::unix::fs::PermissionsExt;

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
    assert!(failed.is_err() && owed, "the fault must reach the owner's cleanup");
    assert_eq!(
        quit,
        Err(CleanupError::Unproven("owner_work_cleanup_unproven")),
        "Quit's run step said proven while the owner's work is still owed"
    );
    assert!(kept, "the unproven owner is kept for the next close");
    assert_eq!(control, Ok(()), "the restored owner settles: the step is proven");
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
    assert!(released, "the same Service owner releases it once the fault is gone");
}
