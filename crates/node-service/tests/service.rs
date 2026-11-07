// SPDX-License-Identifier: AGPL-3.0-only
//! Inert drivers exercise host gates; these tests make no native-output claim.
use api::{actions::Backend, requests::Service, schema::PublicError};
use node_service::service::{Driver, Job, NodeService};
use serde_json::{Value, json};
use std::sync::Arc;
fn qualification() -> Value {
    json!({"qualification_id":"inert-test","qualification_digest":format!("sha256:{}","a".repeat(64)),"deployment_id":"test","deployment_epoch":1,"topology_version":1,"model_id":"test","resolved_commit":"test","manifest_digest":format!("sha256:{}","b".repeat(64)),"path_manifest_digest":format!("sha256:{}","c".repeat(64)),"stage_load_proof_digests":[]})
}
fn document() -> Value {
    json!({"protocol":"mycelium.request_gateway.v1","prompt":"test","max_new_tokens":1,"qualification":qualification()})
}
struct Unverified;
impl Driver for Unverified {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        job.token(0, "unverified text".into())
    }
}
#[test]
fn native_or_driver_reply_cannot_be_an_unverified_token_source() {
    let service = NodeService::new(
        Arc::new(Unverified),
        "private-owner-token".into(),
        qualification(),
    );
    let accepted = service.submit(document()).unwrap();
    let mut stream = service
        .subscribe(&accepted.request_id, None, Some(&accepted.session_token))
        .unwrap();
    match stream.next() {
        Err(error) => assert_eq!(error.code, "token_not_verified"),
        Ok(Some(event)) => {
            stream.ack(event["sequence"].as_u64().unwrap()).unwrap();
            stream.close().unwrap();
            panic!("unverified token reached L10")
        }
        Ok(None) => panic!("missing refusal"),
    }
    // RU-020 (round 9, v0.1.4 executed): a failed run, whatever its cause, allows the next Start;
    // before round 9 this asserted `cleanup_unproven`. The refusal above is the claim of this test.
    assert_eq!(service.rollover().unwrap(), 2);
}
#[test]
fn api_actions_publish_real_runtime_status_and_label_development_seams() {
    let mut service = NodeService::new(
        Arc::new(Unverified),
        "private-owner-token".into(),
        qualification(),
    );
    service.publish().unwrap();
    let status = service.published_status().unwrap();
    assert_eq!(status["running"], false);
    assert_eq!(status["test_seams_active"], true);
    assert_eq!(
        status["test_seams"]["transport"],
        if cfg!(feature = "iroh-loopback") {
            "Iroh-signed-NodeControl/ArtifactChunk/Router"
        } else {
            "loopback-TCP-development"
        }
    );
}
struct Empty;
impl Driver for Empty {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        Ok(())
    }
}
#[test]
fn successful_driver_return_is_not_cleanup_evidence() {
    let service = NodeService::new(Arc::new(Empty), "bearer".into(), qualification());
    let a = service.submit(document()).unwrap();
    let mut stream = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    assert!(stream.next().unwrap().is_none());
    assert_eq!(service.stop().unwrap_err().code, "cleanup_unproven");
    assert_eq!(service.rollover().unwrap_err().code, "cleanup_unproven");
}
struct Failure;
impl Driver for Failure {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        Err(PublicError::new(503, "controlled_driver_failure"))
    }
}
#[test]
fn authentication_holds_and_a_failed_run_releases_the_generation_for_the_next_start() {
    let service = NodeService::new(Arc::new(Failure), "bearer".into(), qualification());
    assert!(service.submit(json!({})).is_err());
    let accepted = service.submit(document()).unwrap();
    assert!(service.cancel(&accepted.request_id, Some("wrong")).is_err());
    assert!(service.subscribe(&accepted.request_id, None, None).is_err());
    assert!(
        service
            .subscribe(
                &accepted.request_id,
                Some((1, 0)),
                Some(&accepted.session_token)
            )
            .is_err()
    );
    let mut stream = service
        .subscribe(&accepted.request_id, None, Some(&accepted.session_token))
        .unwrap();
    assert!(
        service
            .subscribe(&accepted.request_id, None, Some(&accepted.session_token))
            .is_err()
    );
    assert_eq!(stream.next().unwrap_err().code, "controlled_driver_failure");
    // The run is active until it is rolled over: a second Start is refused ...
    assert!(service.submit(document()).is_err());
    assert_eq!(service.generation().unwrap(), 1);
    // ... and RU-020 (v0.1.4: `next_start_allowed` after a failed run, also unproven) allows
    // the next one after the failed run is released; before round 9 this stayed fenced.
    assert_eq!(service.rollover().unwrap(), 2);
    assert_eq!(service.generation().unwrap(), 2);
    assert!(service.submit(document()).is_ok());
}
