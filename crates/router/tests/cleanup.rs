// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-FAIL-04 (cleanup is proven only by a genuine signed target quorum; forged, incomplete, transplanted and stale-generation receipts never count); B-CHAT-19 (terminal once, needs both completion channels and matching cleanup); B-CHAT-17 (settlement after an authority change refuses).
//!
//! L5 cryptography + L9 quorum/terminal core. Local residue callback is a FAKE.
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant};
use protocol::ContractError;
use router::cleanup::*;
use serde_json::{Value, json};
fn instant(t: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), t)
}
fn fixture() -> (Value, CleanupPeer, Value) {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-cancel-vectors.json"
    ))
    .unwrap();
    let row = &rows[0];
    let c = &row["context"];
    let response = serde_json::from_str(row["envelope_utf8"].as_str().unwrap()).unwrap();
    (
        c["subject"].clone(),
        CleanupPeer {
            node: c["node_id"].as_str().unwrap().into(),
            host: c["host_id"].as_str().unwrap().into(),
            process: c["process_id"].clone(),
            run: c["run_id"].as_str().unwrap().into(),
            deployment: c["deployment_id"].as_str().unwrap().into(),
            endpoint: c["endpoint_id"].as_str().unwrap().into(),
            key: c["key"].clone(),
            generation: json!(5),
        },
        response,
    )
}
fn round(subject: Value, peer: CleanupPeer) -> CleanupRound {
    CleanupRound::new(
        subject,
        vec![peer],
        instant(0)
            .checked_add(BudgetId::CancelAndCleanup.span(Entries::ZERO).unwrap())
            .unwrap(),
    )
    .unwrap()
}
fn proof() -> CleanupProof {
    let (s, p, r) = fixture();
    let mut c = round(s, p.clone());
    c.accept(&p.node, &r, || (instant(0), true)).unwrap();
    c.finish(|| Ok(()), || (instant(0), true)).unwrap()
}
#[test]
fn signed_cleanup_quorum_local_residue_and_currentness_are_independent() {
    let (s, p, r) = fixture();
    let c = round(s.clone(), p.clone());
    assert_eq!(
        c.finish(|| panic!("missing quorum"), || (instant(0), true))
            .err()
            .unwrap()
            .0,
        "cleanup_quorum_missing"
    );
    let mut c = round(s.clone(), p.clone());
    c.accept(&p.node, &r, || (instant(0), true)).unwrap();
    assert_eq!(
        c.finish(
            || Err(ContractError("runtime_residue")),
            || (instant(0), true)
        )
        .err()
        .unwrap()
        .0,
        "runtime_residue"
    );
    let mut c = round(s, p.clone());
    c.accept(&p.node, &r, || (instant(0), true)).unwrap();
    let mut calls = 0;
    assert_eq!(
        c.finish(
            || Ok(()),
            || {
                calls += 1;
                (instant(0), calls == 1)
            }
        )
        .err()
        .unwrap()
        .0,
        "cleanup_authority_stale"
    );
}
#[test]
fn signed_incomplete_or_transplanted_cleanup_cannot_form_a_quorum() {
    let (s, p, r) = fixture();
    let mut c = round(s.clone(), p.clone());
    for field in [
        "runtime_clean",
        "transport_clean",
        "cancellation_worker_complete",
        "complete",
    ] {
        let mut bad = r.clone();
        bad["result"]["observation"]["details"]["request_cleanup"][field] = json!(false);
        let id = protocol::identity::Identity::from_secret("endpoint-phone", &[9; 32]).unwrap();
        bad["result"]["signature"] = id.sign(&bad["result"]["observation"]).unwrap();
        assert_eq!(
            c.accept(&p.node, &bad, || (instant(0), true))
                .unwrap_err()
                .0,
            "external_node_cancel_receipt_unsupported"
        );
    }
    let mut other = p.clone();
    other.node = "other-node".into();
    let deadline = instant(0)
        .checked_add(BudgetId::CancelAndCleanup.span(Entries::ZERO).unwrap())
        .unwrap();
    let mut c = CleanupRound::new(s, vec![p.clone(), other.clone()], deadline).unwrap();
    c.accept(&p.node, &r, || (instant(0), true)).unwrap();
    assert!(c.accept(&other.node, &r, || (instant(0), true)).is_err());
    assert_eq!(
        c.finish(|| panic!("incomplete quorum"), || (instant(0), true))
            .err()
            .unwrap()
            .0,
        "cleanup_quorum_missing"
    );
}
#[test]
fn cleanup_receipt_and_settlement_obey_inclusive_deadline() {
    let (s, p, r) = fixture();
    let deadline = instant(0)
        .checked_add(BudgetId::CancelAndCleanup.span(Entries::ZERO).unwrap())
        .unwrap();
    let mut c = round(s.clone(), p.clone());
    assert_eq!(
        c.accept(&p.node, &r, || (deadline, true)).unwrap_err().0,
        "cleanup_deadline_expired"
    );
    let mut owner = RequestOwner::new(s).unwrap();
    owner.cancel();
    assert_eq!(
        owner
            .settle(proof(), Terminal::Cancelled, || (deadline, true))
            .unwrap_err()
            .0,
        "cleanup_deadline_expired"
    );
    assert_eq!(owner.terminal(), None);
}
#[test]
fn forged_incomplete_and_stale_generation_cleanup_never_count() {
    let (s, p, r) = fixture();
    let mut c = round(s, p.clone());
    let mut bad = r.clone();
    bad["ok"] = json!(false);
    assert!(c.accept(&p.node, &bad, || (instant(0), true)).is_err());
    assert_eq!(
        c.accept(&p.node, &r, || (instant(0), false)).unwrap_err().0,
        "cleanup_authority_stale"
    );
    c.accept(&p.node, &r, || (instant(0), true)).unwrap();
    c.accept(&p.node, &r, || (instant(0), true)).unwrap();
    c.finish(|| Ok(()), || (instant(0), true)).unwrap();
    let (s, mut p, r) = fixture();
    p.generation = json!(6);
    let mut c = round(s, p.clone());
    assert_eq!(
        c.accept(&p.node, &r, || (instant(0), true)).unwrap_err().0,
        "cleanup_peer_generation_stale"
    );
}
#[test]
fn terminal_once_requires_both_completion_channels_and_matching_cleanup() {
    let (s, _, _) = fixture();
    let mut owner = RequestOwner::new(s.clone()).unwrap();
    owner.progress_completed();
    assert_eq!(
        owner
            .settle(proof(), Terminal::Completed, || (instant(0), true))
            .unwrap_err()
            .0,
        "request_completion_unproven"
    );
    assert_eq!(owner.terminal(), None);
    owner.native_completed();
    owner
        .settle(proof(), Terminal::Completed, || (instant(0), true))
        .unwrap();
    assert!(!owner.cancel());
    assert_eq!(
        owner
            .settle(proof(), Terminal::Cancelled, || (instant(0), true))
            .unwrap_err()
            .0,
        "request_already_terminal"
    );
    assert_eq!(owner.terminal(), Some(Terminal::Completed));
    let mut stale = s;
    stale["publisher_generation"] = json!(2);
    let mut successor = RequestOwner::new(stale).unwrap();
    successor.cancel();
    assert_eq!(
        successor
            .settle(proof(), Terminal::Cancelled, || (instant(0), true))
            .unwrap_err()
            .0,
        "cleanup_subject_stale"
    );
}
#[test]
fn cleanup_cannot_settle_after_authority_changes() {
    let (s, _, _) = fixture();
    let mut owner = RequestOwner::new(s).unwrap();
    owner.cancel();
    assert_eq!(
        owner
            .settle(proof(), Terminal::Cancelled, || (instant(0), false))
            .unwrap_err()
            .0,
        "cleanup_authority_stale"
    );
    assert_eq!(owner.terminal(), None);
}
#[test]
fn cancelled_native_failure_does_not_require_replaying_start() {
    let (s, _, _) = fixture();
    let mut owner = RequestOwner::new(s).unwrap();
    owner.cancel();
    // A stale_infer_decode_generation ordinary failure is not successful completion;
    // cancellation still finishes only from separately signed cleanup.
    owner
        .settle(proof(), Terminal::Cancelled, || (instant(0), true))
        .unwrap();
    assert_eq!(owner.terminal(), Some(Terminal::Cancelled));
}
