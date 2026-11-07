// SPDX-License-Identifier: AGPL-3.0-only
//! Controlled cleanup environment, real signed L5/L9 receipt and L10 ownership.
use api::{requests::Service, schema::PublicError};
use node_service::{
    receipts,
    service::{Driver, Job, NodeService},
};
use serde_json::json;
use std::sync::Arc;
struct Controlled(std::sync::mpsc::Sender<()>);
impl Driver for Controlled {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        let result = (|| -> node_service::loopback::Result<()> {
            let owner = receipts::identity("slice-owner")?;
            let member = receipts::identity("slice-member")?;
            let (mut source, session) = receipts::Receipts::open(
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
            let envelope = source.sign(
                &owner,
                &[17],
                false,
                &protocol::canonical::digest(&json!("controlled-test"))?,
            )?;
            assert_eq!(job.receipt(envelope, 0., |_| Ok(()))?, [17]);
            assert_eq!(
                job.token(0, "controlled".into()).unwrap_err().code,
                "request_cancelled"
            );
            self.0.send(())?;
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
            // Controlled inert environment, explicitly not a native residue proof.
            let proof = round.finish(|| Ok(()), || (now, true))?;
            job.settle(proof, router::cleanup::Terminal::Cancelled, now)?;
            Ok(())
        })();
        result.map_err(|_| PublicError::new(503, "controlled_driver_failed"))
    }
}
#[test]
fn cancellation_between_frame_delivery_and_flush_ack_preserves_sequence() {
    let digest = protocol::canonical::digest(&json!("controlled-test")).unwrap();
    let q = json!({"qualification_id":"controlled","qualification_digest":digest,"deployment_id":"slice-development","deployment_epoch":1,"topology_version":1,"model_id":"controlled","resolved_commit":"controlled","manifest_digest":digest,"path_manifest_digest":digest,"stage_load_proof_digests":[]});
    let (ready, observed) = std::sync::mpsc::channel();
    let service = NodeService::new(Arc::new(Controlled(ready)), "bearer".into(), q.clone());
    let a=service.submit(json!({"protocol":"mycelium.request_gateway.v1","prompt":"controlled","max_new_tokens":2,"qualification":q})).unwrap();
    let mut stream = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    let token = stream.next().unwrap().unwrap();
    assert_eq!(token["sequence"], 0);
    assert!(
        service
            .cancel(&a.request_id, Some(&a.session_token))
            .unwrap()
    );
    // The producer must observe cancellation while this delivered frame still
    // awaits its flush ACK. Waiting on terminal-before-ACK would be a deadlock.
    observed
        .recv_timeout(node_service::service::duration(
            budgets::BudgetId::WorkerStartup,
        ))
        .unwrap();
    stream.ack(0).unwrap();
    let terminal = stream.next().unwrap().unwrap();
    assert_eq!(terminal["type"], "cancelled");
    assert_eq!(terminal["sequence"], 1);
    stream.ack(1).unwrap();
    assert!(stream.next().unwrap().is_none());
    assert_eq!(service.rollover().unwrap(), 2);
}
