// SPDX-License-Identifier: AGPL-3.0-only
//! Round 8: what v0.1.4 does AFTER a native worker dies, executed (fixture
//! `FX-SLICE1-post-worker-death`, probes `SLICE-1-evidence/r8-oracle/`):
//!   * the run is `failed` with `worker_native_exited`; Stop on it does not fail;
//!   * the worker object proves its cleanup once the exact pid is reaped (`cleanup_proven`
//!     true, process group absent), so the next run is allowed at once (`start` /
//!     `rebuild` after a failed run);
//!   * an UNPROVEN cleanup is not released for a rollover (`rollover_cleanup_unproven`) and
//!     makes the launcher's quit exit 3 (`cleanup_incomplete`); a proven one exits 0;
//!   * a worker death never ends the application by itself.
//!
//! The port: a driver that failed with the worker's death and holds a local reap proof
//! (every spawned child reaped, exact pids gone, staged copy discarded) verifies the Quit.
//! Round 9 (RU-020): the next Start no longer waits for that proof (see `failed_run_restart`);
//! a failure without a proof still does not verify the Quit.
use api::{requests::Service, schema::PublicError};
use backend_mlx::{process::ProcessLimits, resources::ResourcePool};
use budgets::{BudgetId as B, Bytes, Entries, Reservation};
use node_service::{
    reap::{Ledger, Refusal},
    service::{Driver, Job, NodeService},
    spawn,
};
use rustix::process::{Pid, Signal, kill_process};
use serde_json::{Value, json};
use std::{
    process::Command,
    sync::Arc,
    time::{Duration, Instant},
};

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/post-worker-death-oracle.json")).unwrap()
}
fn qualification() -> Value {
    json!({"qualification_id":"inert-test","qualification_digest":format!("sha256:{}","a".repeat(64)),"deployment_id":"test","deployment_epoch":1,"topology_version":1,"model_id":"test","resolved_commit":"test","manifest_digest":format!("sha256:{}","b".repeat(64)),"path_manifest_digest":format!("sha256:{}","c".repeat(64)),"stage_load_proof_digests":[]})
}
fn document() -> Value {
    json!({"protocol":"mycelium.request_gateway.v1","prompt":"test","max_new_tokens":1,"qualification":qualification()})
}
/// A real child, killed by its exact pid and reaped: the pid is gone afterwards.
fn dead_child() -> u32 {
    let mut child = Command::new("/bin/sleep").arg("30").spawn().unwrap();
    let pid = child.id();
    child.kill().unwrap();
    child.wait().unwrap();
    pid
}
/// Fails like the native owner after a worker death; `proof` decides whether it holds one.
struct Died {
    proof: bool,
}
impl Driver for Died {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        if self.proof {
            let ledger = Ledger::default();
            let pid = dead_child();
            ledger.record("member", pid, true);
            // A refusal must fail the run visibly, never panic the driver thread (that would hang the host).
            let proof = ledger
                .prove(pid, true)
                .map_err(|_| PublicError::new(503, "test_proof_refused"))?;
            job.record_reap(proof)?;
        }
        Err(PublicError::new(503, "worker_native_exited"))
    }
}
/// `poll_shutdown` reaps only an already finished driver (`Ok(false)` until then): poll it as
/// the desktop shell does, bounded by the registered cleanup allowance.
fn settled_poll(service: &NodeService) -> Result<bool, PublicError> {
    let deadline = Instant::now() + node_service::service::duration(B::StageCleanup);
    loop {
        match service.poll_shutdown() {
            Ok(false) if Instant::now() < deadline => {
                std::thread::sleep(node_service::service::duration(B::RegistryPoll));
            }
            other => return other,
        }
    }
}
fn failed_run(proof: bool) -> (NodeService, String) {
    let service = NodeService::new(Arc::new(Died { proof }), "bearer".into(), qualification());
    let accepted = service.submit(document()).unwrap();
    let mut stream = service
        .subscribe(&accepted.request_id, None, Some(&accepted.session_token))
        .unwrap();
    let code = stream.next().unwrap_err().code.to_owned();
    (service, code)
}

#[test]
fn the_fixture_is_v014_executed_output() {
    let fx = oracle();
    assert_eq!(fx["id"], "FX-SLICE1-post-worker-death");
    assert!(
        fx["oracle"]
            .as_str()
            .unwrap()
            .contains("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    let owner = &fx["observations"]["p2_owner_run"];
    assert_eq!(owner["error_code"], "worker_native_exited");
    assert_eq!(owner["state_after_death"], "failed");
    assert_eq!(
        fx["observations"]["p0_real_worker_killed"]["cleanup_proven_after_close"],
        true
    );
    assert_eq!(
        fx["observations"]["p0_real_worker_killed"]["pid_gone"],
        true
    );
}
#[test]
fn a_death_with_a_local_reap_proof_fails_the_run_and_releases_the_generation() {
    let fx = oracle();
    let owner = &fx["observations"]["p2_owner_run"];
    let (service, code) = failed_run(true);
    // The failed run's code is the oracle's (v0.1.4 worker_native_exited).
    assert_eq!(code, owner["error_code"].as_str().unwrap());
    // Stop on a failed run does not raise (oracle `stop_on_failed_run_raises:false`).
    assert_eq!(owner["stop_on_failed_run_raises"], false);
    service
        .stop()
        .expect("Stop on a failed run is not an error");
    // The next run is allowed (oracle `next_start_allowed:true`).
    assert_eq!(owner["next_start_allowed"], true);
    assert_eq!(service.rollover().unwrap(), 2);
    assert!(service.submit(document()).is_ok());
    // That next run is another `Died` driver, which spawns and reaps a real child. Settle it as the
    // desktop shell does (Quit joins the driver) so the child never outlives the test holding its stdio.
    service.request_shutdown().unwrap();
    assert!(settled_poll(&service).expect("the next run settles at Quit"));
}
fn quit_oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/quit-after-failure-oracle.json")).unwrap()
}
#[test]
fn the_quit_fixture_is_v014_executed_output_of_the_real_close() {
    // Round 10: the round-8 launcher probe STUBBED DemoApp.close and set `cleanup_error` by hand;
    // this fixture runs the real DemoApp.close / demo_app.close / OwnerRun / launcher main().
    let fx = quit_oracle();
    assert_eq!(fx["id"], "FX-SLICE1-quit-after-failure");
    assert!(
        fx["oracle"]
            .as_str()
            .unwrap()
            .contains("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert_eq!(fx["cases"], 4);
    assert_eq!(fx["observations"].as_object().unwrap().len(), 4);
}
#[test]
fn a_death_without_a_reap_proof_allows_the_next_start_and_a_quit_that_exits_zero() {
    // Oracle p3 (r8): after an UNPROVEN failure the plain next Start is allowed
    // (`plain_start_after_unproven_failure.next_start_allowed:true`, RU-020); only a rollover
    // continuation refuses (`rollover_cleanup_unproven`).
    let fx = oracle();
    assert_eq!(
        fx["observations"]["p3_unproven"]["rollover_with_unproven_cleanup"]["error_code"],
        "rollover_cleanup_unproven"
    );
    assert_eq!(
        fx["observations"]["p3_unproven"]["plain_start_after_unproven_failure"]["next_start_allowed"],
        true
    );
    // Round 10 (SE-SLICE1-L11-QUIT-UNPROVEN, executed): v0.1.4's launcher exits 3 only when
    // DemoApp.close recorded a close-step exception (`cleanup_error`). A FAILED run is not active
    // (`owner_run.py:48`), so Quit does not touch it, whatever its `cleanup_unproven` says: the
    // real close returns, no event on stderr, exit 0 (q2).
    let q2 = &quit_oracle()["observations"]["q2_failed_run_cleanup_unproven"];
    assert_eq!(q2["run_state_at_quit"], "failed");
    assert_eq!(q2["run_error_code"], "worker_native_exited");
    assert_eq!(
        q2["run_cleanup_unproven"]["code"],
        "owner_target_cleanup_unproven"
    );
    assert_eq!(q2["run_active_at_quit"], false);
    assert_eq!(q2["exit_code"], 0);
    assert_eq!(q2["app_cleanup_error"], Value::Null);
    assert_eq!(q2["stderr"], json!([]));
    let (service, code) = failed_run(false);
    assert_eq!(code, "worker_native_exited");
    service
        .stop()
        .expect("Stop on a failed run is not an error");
    // The proof stays recorded evidence (absent here); it is not what verifies the Quit.
    assert!(!service.failed_run_cleanup_proven().unwrap());
    service.request_shutdown().unwrap();
    assert!(
        settled_poll(&service).expect("a failed run without a proof still lets the Quit finish")
    );
    assert_eq!(service.rollover().unwrap(), 2);
    assert_eq!(service.generation().unwrap(), 2);
}
#[test]
fn quit_after_a_proven_failure_or_an_active_run_exits_zero_in_the_oracle() {
    let fx = quit_oracle();
    let obs = &fx["observations"];
    for case in ["q1_failed_run_cleanup_proven", "q3_active_run"] {
        assert_eq!(obs[case]["exit_code"], 0, "{case}");
        assert_eq!(obs[case]["app_cleanup_error"], Value::Null, "{case}");
        assert_eq!(obs[case]["stderr"], json!([]), "{case}");
    }
    // An active run is stopped and joined by Quit (q3: `stopped` afterwards).
    assert_eq!(obs["q3_active_run"]["run_active_at_quit"], true);
    assert_eq!(obs["q3_active_run"]["run_state_after"], "stopped");
}
#[test]
fn a_proven_death_verifies_the_shutdown_like_a_quit_that_exits_zero() {
    let fx = oracle();
    assert_eq!(fx["launcher"]["quit_with_proven_cleanup_exit_code"], 0);
    let (service, _) = failed_run(true);
    assert!(service.failed_run_cleanup_proven().unwrap());
    service.request_shutdown().unwrap();
    assert!(settled_poll(&service).expect("the reaped generation verifies"));
}
#[test]
fn a_worker_death_never_closes_the_application_by_itself() {
    let fx = oracle();
    assert_eq!(
        fx["launcher"]["worker_death_exits_launcher_by_itself"],
        false
    );
    let (service, _) = failed_run(true);
    // Not closed until the user quits: shutdown has not been requested, health still answers.
    assert_eq!(
        service.poll_shutdown().unwrap_err().code,
        "shutdown_not_requested"
    );
    assert_eq!(service.health().unwrap()["ok"], true);
}
#[test]
fn a_proof_never_releases_a_run_that_did_not_fail() {
    struct OkWithProof;
    impl Driver for OkWithProof {
        fn run(&self, job: &mut Job) -> Result<(), PublicError> {
            let ledger = Ledger::default();
            let pid = dead_child();
            ledger.record("member", pid, true);
            let proof = ledger
                .prove(pid, true)
                .map_err(|_| PublicError::new(503, "test_proof_refused"))?;
            job.record_reap(proof)
        }
    }
    let service = NodeService::new(Arc::new(OkWithProof), "bearer".into(), qualification());
    let a = service.submit(document()).unwrap();
    let mut stream = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    assert!(stream.next().unwrap().is_none());
    assert_eq!(service.rollover().unwrap_err().code, "cleanup_unproven");
}

#[test]
fn the_ledger_proves_only_reaped_children_whose_pids_are_gone() {
    let pid = dead_child();
    let ledger = Ledger::default();
    assert_eq!(
        ledger.prove(pid, true).unwrap_err(),
        Refusal::NothingSpawned
    );
    ledger.record("member", pid, true);
    let proof = ledger
        .prove(pid, true)
        .expect("reaped, pid gone, staged discarded");
    assert_eq!(proof.children().len(), 1);
    assert_eq!(proof.children()[0].pid, pid);
    // The dead worker must be one of the ledger's children.
    assert_eq!(
        ledger.prove(pid + 1, true).unwrap_err(),
        Refusal::UnknownWorker(pid + 1)
    );
    // A leftover staged copy is a residue.
    assert_eq!(
        ledger.prove(pid, false).unwrap_err(),
        Refusal::StagedCopyRemains
    );
    // A child the supervisor could not prove reaped.
    let other = Ledger::default();
    other.record("member", pid, false);
    assert_eq!(other.prove(pid, true).unwrap_err(), Refusal::NotReaped(pid));
    // A child that is still alive (not reaped by anyone) is a process residue.
    let mut alive = Command::new("/bin/sleep").arg("30").spawn().unwrap();
    let live = Ledger::default();
    live.record("member", alive.id(), true);
    assert_eq!(
        live.prove(alive.id(), true).unwrap_err(),
        Refusal::PidPresent(alive.id())
    );
    alive.kill().unwrap();
    alive.wait().unwrap();
    assert!(live.prove(alive.id(), true).is_ok());
    // Refusal codes are closed, lower-case identifiers.
    assert_eq!(Refusal::NothingSpawned.code(), "reap_nothing_spawned");
}

/// The real supervised worker, killed by its exact pid from outside (what the injected-death
/// qualification does): dropping the tracked handle enters the reaped child in the ledger.
#[test]
fn a_tracked_worker_killed_from_outside_is_entered_as_reaped_and_proves() {
    let ledger = Ledger::default();
    let mut command = Command::new("/bin/sh");
    command.args(["-c", "exec cat"]);
    let reservation = Reservation {
        resident: Bytes::new(1),
        staged: Bytes::ZERO,
        staging_overhead: Bytes::ZERO,
        scratch: Bytes::ZERO,
        kv: Bytes::ZERO,
    };
    let pool = ResourcePool::new(reservation.total().unwrap());
    let span = |id: B| id.span(Entries::ZERO).unwrap();
    let worker = spawn::spawn_worker(
        command,
        pool,
        reservation,
        ProcessLimits {
            deadline: Instant::now() + Duration::from_millis(span(B::MemberChosenWall).millis()),
            close: span(B::RouterClose),
            reap: span(B::WorkerCleanupReserve),
            poll: span(B::RegistryPoll),
        },
    )
    .unwrap();
    let tracked = spawn::Tracked::new(worker, "member", &ledger);
    let pid = tracked.pid();
    // Alive and unreaped: no proof, and nothing is entered before the handle ends.
    assert!(ledger.entries().is_empty());
    kill_process(
        Pid::from_raw(i32::try_from(pid).unwrap()).unwrap(),
        Signal::KILL,
    )
    .unwrap();
    drop(tracked);
    let entries = ledger.entries();
    assert_eq!(entries.len(), 1);
    assert_eq!(
        (entries[0].role, entries[0].pid, entries[0].proven),
        ("member", pid, true)
    );
    let proof = ledger.prove(pid, true).expect("reaped and gone");
    assert_eq!(proof.children()[0].pid, pid);
}

#[test]
fn a_finished_driver_that_neither_failed_nor_settled_does_not_verify_the_quit() {
    // V03-SLICE-1-1: v0.1.4's run ends `stopped` or `failed` (`owner_run.py` states; none is
    // "finished, unsettled"), so this input never occurs there and the stricter refusal is the
    // only stance: the shell reports `launcher_cleanup_failed` / exit 3 for it. Mutation control
    // for the Quit-after-failure change (EV r10-mutant-unsettled survived without this test).
    struct Ends;
    impl Driver for Ends {
        fn run(&self, _: &mut Job) -> Result<(), PublicError> {
            Ok(())
        }
    }
    let service = NodeService::new(Arc::new(Ends), "bearer".into(), qualification());
    let a = service.submit(document()).unwrap();
    let mut stream = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    assert!(stream.next().unwrap().is_none());
    service.request_shutdown().unwrap();
    assert_eq!(settled_poll(&service).unwrap_err().code, "cleanup_unproven");
}
