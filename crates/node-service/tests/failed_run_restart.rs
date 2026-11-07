// SPDX-License-Identifier: AGPL-3.0-only
//! Round 9, RU-020 (`registry/RULINGS.tsv`): v0.1.4's executed behaviour after a failed run
//! (`SLICE-1-evidence/r8-oracle/`, fixture `FX-SLICE1-post-worker-death`, `p2_owner_run`):
//!   * Stop on a failed run does not raise (`stop_on_failed_run_raises:false`);
//!   * the NEXT Start is allowed at once, for ANY failed run and also when the cleanup is
//!     unproven (`next_start_allowed:true`; only a rollover CONTINUATION refuses an unproven
//!     cleanup, `rollover_cleanup_unproven`, which this host-generation fence does not model);
//!   * a run that did NOT fail but has no cleanup proof is still fenced.
//!
//! A request session reaches `Terminal::Failed` only through a real signed cleanup quorum
//! (the member agent survives its native worker and signs; `SE-SLICE1-FAILED-SETTLE`).
//! `member-process-topology-oracle.json` (`FX-SLICE1-member-process-topology`) is the executed
//! v0.1.4 proof that this is the oracle's process structure.
use api::{requests::Service, schema::PublicError};
use node_service::{
    receipts,
    service::{Driver, Job, NodeService},
};
use serde_json::{Value, json};
use std::sync::Arc;

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/post-worker-death-oracle.json")).unwrap()
}
fn qualification() -> Value {
    let digest = protocol::canonical::digest(&json!("failed-run-restart")).unwrap();
    json!({"qualification_id":"inert-test","qualification_digest":digest,"deployment_id":"slice-development","deployment_epoch":1,"topology_version":1,"model_id":"test","resolved_commit":"test","manifest_digest":digest,"path_manifest_digest":digest,"stage_load_proof_digests":[]})
}
fn document() -> Value {
    json!({"protocol":"mycelium.request_gateway.v1","prompt":"test","max_new_tokens":1,"qualification":qualification()})
}

/// Fails with a controlled code and nothing else: no reap proof, no cleanup quorum.
struct Fails(&'static str);
impl Driver for Fails {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        Err(PublicError::new(503, self.0))
    }
}
/// The member agent survived its worker and signed: a real quorum settles the session as
/// `Failed`, then the run reports the worker's death.
struct DeadWorkerQuorum;
impl Driver for DeadWorkerQuorum {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        let result = (|| -> node_service::loopback::Result<()> {
            let owner = receipts::identity("slice-owner")?;
            let member = receipts::identity("slice-member")?;
            let (source, session) = receipts::Receipts::open(
                &owner,
                (&member.public_key_record(), member.endpoint_id()),
                &job.id,
                job.generation,
                &job.document,
                std::time::Duration::ZERO,
                job.deadline
                    .saturating_duration_since(std::time::Instant::now()),
            )?;
            job.install(session)?;
            let now = budgets::MonoInstant::new(budgets::ClockDomain(1), 0);
            let deadline = now
                .checked_add(
                    budgets::BudgetId::StageCleanup
                        .span(budgets::Entries::ZERO)
                        .unwrap(),
                )
                .unwrap();
            let mut round = router::cleanup::CleanupRound::new(
                source.subject.clone(),
                vec![router::cleanup::CleanupPeer {
                    node: "b-member".into(),
                    host: "loopback-mac".into(),
                    process: json!(std::process::id()),
                    run: format!("slice-{}", job.generation),
                    deployment: "slice-development".into(),
                    endpoint: member.endpoint_id().into(),
                    key: member.public_key_record(),
                    generation: json!(job.generation),
                }],
                deadline,
            )?;
            round.accept(
                "b-member",
                &receipts::cleaned(&member, &source.subject, job.generation, "b-member")?,
                || (now, true),
            )?;
            let proof = round.finish(|| Ok(()), || (now, true))?;
            job.settle_failed(proof, "worker_native_exited", now)?;
            Ok(())
        })();
        result.map_err(|_| PublicError::new(503, "controlled_driver_failed"))?;
        Err(PublicError::new(503, "worker_native_exited"))
    }
}

/// `poll_shutdown` only reaps an already finished driver: poll it like the desktop shell does.
fn shutdown_verified(service: &NodeService) -> bool {
    let deadline = std::time::Instant::now()
        + node_service::service::duration(budgets::BudgetId::StageCleanup);
    loop {
        if service
            .poll_shutdown()
            .expect("the failed run verifies the Quit")
        {
            return true;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "driver never finished"
        );
        std::thread::sleep(node_service::service::duration(
            budgets::BudgetId::RegistryPoll,
        ));
    }
}

fn run_to_failure(driver: Arc<dyn Driver>) -> (NodeService, String) {
    let service = NodeService::new(driver, "bearer".into(), qualification());
    let accepted = service.submit(document()).unwrap();
    let mut stream = service
        .subscribe(&accepted.request_id, None, Some(&accepted.session_token))
        .unwrap();
    let code = loop {
        match stream.next() {
            Ok(Some(event)) => stream.ack(event["sequence"].as_u64().unwrap()).unwrap(),
            Ok(None) => panic!("the run was expected to fail"),
            Err(error) => break error.code.to_owned(),
        }
    };
    (service, code)
}

fn topology() -> Value {
    serde_json::from_str(include_str!("fixtures/member-process-topology-oracle.json")).unwrap()
}
#[test]
fn the_member_topology_fixture_is_v014_executed_output() {
    let fx = topology();
    assert_eq!(fx["id"], "FX-SLICE1-member-process-topology");
    assert!(
        fx["oracle"]
            .as_str()
            .unwrap()
            .contains("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    let o = &fx["observations"];
    // The signer is a different, longer-lived process than the compute worker ...
    assert_eq!(o["worker_is_direct_child_of_agent"], true);
    assert_eq!(o["worker_is_own_session_and_group_leader"], true);
    assert_eq!(o["signer_is_the_agent_pid_not_the_worker_pid"], true);
    // ... that survives the worker's death, reaps it, and signs the cleanup the owner accepts.
    assert_eq!(o["agent_alive_after_worker_kill"], true);
    assert_eq!(o["worker_pid_gone_after_kill"], true);
    assert_eq!(o["worker_error_code"], "worker_native_exited");
    assert_eq!(o["worker_cleanup_proven"], true);
    assert_eq!(o["agent_signs_cleanup_ack_removing_staged_copy"], true);
    assert_eq!(o["owner_accepts_the_agents_signed_receipt"], true);
}

#[test]
fn the_oracle_allows_the_next_start_after_any_failed_run() {
    let fx = oracle();
    let owner = &fx["observations"]["p2_owner_run"];
    assert_eq!(owner["next_start_allowed"], true);
    assert_eq!(owner["stop_on_failed_run_raises"], false);
    // Also with an unproven cleanup: the oracle's next start is not gated on it.
    let unproven = &fx["observations"]["p3_unproven"]["plain_start_after_unproven_failure"];
    assert_eq!(
        unproven["cleanup_unproven"]["code"],
        "owner_target_cleanup_unproven"
    );
    assert_eq!(unproven["next_start_allowed"], true);
    assert_eq!(unproven["first_error"], "worker_native_exited");
}

#[test]
fn any_failed_run_stops_without_error_and_allows_the_next_start() {
    for code in ["controlled_driver_failure", "token_not_verified"] {
        let (service, failed) = run_to_failure(Arc::new(Fails(code)));
        assert_eq!(failed, code);
        service
            .stop()
            .expect("Stop on a failed run is not an error");
        assert_eq!(service.rollover().expect("the next Start is allowed"), 2);
        assert_eq!(service.generation().unwrap(), 2);
        assert!(service.submit(document()).is_ok(), "{code}");
    }
}

#[test]
fn a_run_that_did_not_fail_without_cleanup_proof_stays_fenced() {
    struct Empty;
    impl Driver for Empty {
        fn run(&self, _: &mut Job) -> Result<(), PublicError> {
            Ok(())
        }
    }
    let service = NodeService::new(Arc::new(Empty), "bearer".into(), qualification());
    let a = service.submit(document()).unwrap();
    let mut stream = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    assert!(stream.next().unwrap().is_none());
    assert_eq!(service.stop().unwrap_err().code, "cleanup_unproven");
    assert_eq!(service.rollover().unwrap_err().code, "cleanup_unproven");
}

#[test]
fn a_signed_quorum_settles_a_dead_worker_run_as_failed_and_allows_the_next_start() {
    let (service, code) = run_to_failure(Arc::new(DeadWorkerQuorum));
    // v0.1.4's bare code is the run's error.
    assert_eq!(code, "worker_native_exited");
    service
        .stop()
        .expect("Stop on a failed run is not an error");
    // The settled failure verifies a Quit like a proven cleanup (exit 0), without a reap proof.
    service.request_shutdown().unwrap();
    assert!(shutdown_verified(&service), "terminal Failed verifies");
}

#[test]
fn the_failed_terminal_event_carries_the_workers_code() {
    let service = NodeService::new(Arc::new(DeadWorkerQuorum), "bearer".into(), qualification());
    let accepted = service.submit(document()).unwrap();
    let mut stream = service
        .subscribe(&accepted.request_id, None, Some(&accepted.session_token))
        .unwrap();
    let event = stream.next().unwrap().expect("a terminal event");
    assert_eq!(event["type"], "failed");
    assert_eq!(event["code"], "worker_native_exited");
    stream.ack(event["sequence"].as_u64().unwrap()).unwrap();
    assert_eq!(stream.next().unwrap_err().code, "worker_native_exited");
    assert_eq!(service.rollover().unwrap(), 2);
}
