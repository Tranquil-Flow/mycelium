// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-CHAT-13 (native progress rechecked, queue full never hides revocation); B-CHAT-17 (cancellation retires, never completes); B-CHAT-19 (a missing terminal is not hidden).
//!
//! Recorded L5 signed receipts through the actual L9 event-driven composition.
//! Dispatch/currentness/sealing are controlled effects, not live L6-L8 processes.
use protocol::{ContractError, token::*};
use runtime::composition::{Pull, Session};
use serde_json::{Value, json};
fn fixture() -> (Session, Value) {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-token-vectors.json"
    ))
    .unwrap();
    let row = rows.iter().find(|r| r["name"] == "DECODING-2").unwrap();
    let c = &row["context"]["grant_context"];
    let grant = GrantRegistry::new(4096)
        .unwrap()
        .admit(
            &row["context"]["grant"],
            &GrantExpectation {
                scope: &c["expected_scope"],
                owner_key: &c["owner_key"],
                max_new_tokens: c["max_new_tokens"].as_u64().unwrap(),
                stop_token_ids: &c["stop_token_ids"],
                window: GrantWindow {
                    now_unix_ms: c["now_unix_ms"].as_u64().unwrap(),
                    now_monotonic_s: c["now_monotonic_s"].as_f64().unwrap(),
                    process_deadline_monotonic_s: c["process_deadline_monotonic_s"]
                        .as_f64()
                        .unwrap(),
                    lease_deadline_monotonic_s: c["lease_deadline_monotonic_s"].as_f64().unwrap(),
                    clock_uncertainty_ms: c["clock_uncertainty_ms"].as_u64().unwrap(),
                },
            },
        )
        .unwrap();
    let mut subject = c["expected_scope"]["control"].clone();
    subject["request_id"] = c["expected_scope"]["request_id"].clone();
    subject["cancellation_generation"] = json!(1);
    (
        Session::new(grant, row["context"]["entry_key"].clone(), subject, Some(1)).unwrap(),
        row["envelope"].clone(),
    )
}
#[test]
fn pull_applies_backpressure_before_transport_and_cancellation_retires_not_completes() {
    let (mut session, receipt) = fixture();
    session.start(|| Ok(())).unwrap();
    assert_eq!(
        session.start(|| panic!("start replay")).unwrap_err().0,
        "token_path_grant_replay"
    );
    assert_eq!(
        session
            .pull(|| Ok(receipt), || (10.0, true), |_| Ok(()))
            .unwrap(),
        Pull::Accepted
    );
    assert_eq!(
        session
            .pull(
                || panic!("backpressure polled transport"),
                || (10.0, true),
                |_| Ok(())
            )
            .unwrap(),
        Pull::Backpressure
    );
    assert_eq!(
        session.poll(|| (10.0, true)).unwrap().unwrap().token_ids(),
        &[10, 11]
    );
    session.cancel(|| Ok(true)).unwrap();
    assert!(session.retirement().is_some());
    assert_eq!(session.terminal(), None);
    session
        .native_result(Err(ContractError("stale_infer_decode_generation")))
        .unwrap();
    assert_eq!(session.terminal(), None);
    assert_eq!(
        session.poll(|| (10.0, true)).unwrap_err().0,
        "token_progress_disabled"
    );
}
#[test]
fn full_queue_does_not_hide_revocation() {
    let (mut session, receipt) = fixture();
    session.start(|| Ok(())).unwrap();
    session
        .pull(|| Ok(receipt), || (10.0, true), |_| Ok(()))
        .unwrap();
    assert_eq!(
        session
            .pull(|| panic!("revoked"), || (10.0, false), |_| Ok(()))
            .unwrap_err()
            .0,
        "token_path_grant_stale"
    );
}
#[test]
fn cancellation_predicate_error_still_closes_registration_without_retirement() {
    let (mut session, _) = fixture();
    session.start(|| Ok(())).unwrap();
    assert_eq!(
        session
            .cancel(|| Err(ContractError("authority_unproven")))
            .unwrap_err()
            .0,
        "authority_unproven"
    );
    assert!(session.retirement().is_none());
    assert_eq!(
        session.poll(|| (10.0, true)).unwrap_err().0,
        "token_progress_disabled"
    );
}
#[test]
fn ordinary_native_response_cannot_supply_tokens_or_hide_missing_terminal() {
    let (mut session, _) = fixture();
    session.start(|| Ok(())).unwrap();
    session.native_result(Ok(())).unwrap();
    assert_eq!(
        session.poll(|| (10.0, true)).unwrap_err().0,
        "token_progress_terminal_missing"
    );
    assert_eq!(session.terminal(), None);
}
#[test]
fn native_completion_rechecks_already_queued_progress_before_raising() {
    let (mut session, receipt) = fixture();
    session.start(|| Ok(())).unwrap();
    session
        .pull(|| Ok(receipt), || (10.0, true), |_| Ok(()))
        .unwrap();
    session
        .native_result(Err(ContractError("native_refusal")))
        .unwrap();
    assert_eq!(
        session.poll(|| (10.0, true)).unwrap().unwrap().token_ids(),
        &[10, 11]
    );
    assert_eq!(
        session.poll(|| (10.0, true)).unwrap_err().0,
        "native_refusal"
    );
}
