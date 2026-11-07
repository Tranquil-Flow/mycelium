// SPDX-License-Identifier: AGPL-3.0-only
//! Round 5: the token-path grant window and the receipt clock must share one origin.
//! The native owner admits the grant after setup (artifact staging, member and leaf
//! readiness) and then presents receipts at "seconds since the job began". The grant
//! deadline must therefore be expressed on that same job clock; a deadline measured from
//! the moment of admission expires a healthy run once setup has used half its wall.
use budgets::{BudgetId as B, Entries};
use node_service::receipts::{self, Receipts};
use runtime::composition::{Pull, Session};
use serde_json::{Value, json};
use std::time::Duration;

fn total() -> Duration {
    Duration::from_millis(B::MemberChosenWall.span(Entries::ZERO).unwrap().millis())
}

fn document() -> Value {
    let digest = protocol::canonical::digest(&json!("window-test")).unwrap();
    json!({"protocol":"mycelium.request_gateway.v1","prompt":"window","max_new_tokens":2,
        "qualification":{"qualification_id":"window","qualification_digest":digest,"deployment_id":"slice-development",
        "deployment_epoch":1,"topology_version":1,"model_id":"window","resolved_commit":"window",
        "manifest_digest":digest,"path_manifest_digest":digest,"stage_load_proof_digests":[]}})
}

/// A signed first receipt presented at job-clock `now`, after `setup` of the job's wall
/// was spent before the grant was admitted.
fn first_receipt_at(setup: Duration, now: f64) -> Result<Pull, String> {
    let owner = receipts::identity("window-owner").unwrap();
    let member = receipts::identity("window-member").unwrap();
    let (mut source, mut session): (Receipts, Session) = Receipts::open(
        &owner,
        (&member.public_key_record(), member.endpoint_id()),
        "request-window",
        1,
        &document(),
        setup,
        total() - setup,
    )
    .unwrap();
    let envelope = source
        .sign(
            &owner,
            &[17],
            false,
            &protocol::canonical::digest(&json!("stage")).unwrap(),
        )
        .unwrap();
    session
        .pull(|| Ok(envelope), || (now, true), |_| Ok(()))
        .map_err(|e| e.0.to_owned())
}

#[test]
fn a_run_whose_setup_used_more_than_half_the_wall_still_gets_its_first_receipt() {
    // Setup took two thirds of the wall (slow host, large artifact); the first receipt
    // arrives one second later on the job clock, well inside the job's own deadline.
    let setup = total() / 3 * 2;
    let outcome = first_receipt_at(setup, setup.as_secs_f64() + 1.0);
    assert_eq!(outcome, Ok(Pull::Accepted));
}

#[test]
fn the_grant_still_expires_at_the_job_deadline_on_the_job_clock() {
    let setup = total() / 3 * 2;
    let outcome = first_receipt_at(setup, total().as_secs_f64());
    assert_eq!(outcome, Err("token_path_grant_expired".to_owned()));
}

#[test]
fn an_immediate_run_is_unchanged() {
    let outcome = first_receipt_at(Duration::ZERO, 0.0);
    assert_eq!(outcome, Ok(Pull::Accepted));
}
