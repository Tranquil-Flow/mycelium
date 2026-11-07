// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-CHAT-13 (receipt stream: overflow is sticky, reorder and wrong generation never advance the cursor, seal failure cannot publish); B-CHAT-19 (delivered terminal is once); B-CHAT-17 (observed cancellation survives the finalizer).
//!
//! O:F1/O:F2/O:F3/O:F16 narrow production core controls; no live IO.
use protocol::{ContractError, canonical, identity::Identity, token::*};
use router::owner::{OwnerAttempt, RegistrationBook, Retirement};
use router::stream::ReceiptStream;
use serde_json::{Value, json};
use std::cell::Cell;
fn registered() -> RegistrationBook {
    let (stream, _) = fixture(None);
    let mut book = RegistrationBook::default();
    book.open(
        "command".into(),
        stream,
        Retirement {
            grant_digest: "pinned-grant".into(),
            target_endpoint_id: "pinned-endpoint".into(),
        },
    )
    .unwrap();
    book
}
#[test]
fn l16_observed_cancellation_survives_finalizer_without_reentry() {
    let mut book = registered();
    let mut attempt = OwnerAttempt::default();
    attempt.start().unwrap();
    assert_eq!(attempt.start().unwrap_err().0, "token_path_grant_replay");
    assert_eq!(
        attempt.fence(|| Ok(true)).unwrap_err().0,
        "request_cancelled"
    );
    attempt
        .finalize(&mut book, "command", || {
            Err(ContractError("must_not_reenter"))
        })
        .unwrap();
    assert!(book.stream("command").is_err());
    assert_eq!(
        book.retirement("command").unwrap().grant_digest,
        "pinned-grant"
    );
    // Forwarding retirement is ephemeral, not recreated from command rows.
    assert!(RegistrationBook::default().retirement("command").is_none());
    book.close();
    assert!(book.retirement("command").is_none());
}
#[test]
fn l16_raising_predicate_still_closes_without_retirement_authority() {
    let mut book = registered();
    let mut attempt = OwnerAttempt::default();
    assert_eq!(
        attempt
            .finalize(&mut book, "command", || Err(ContractError(
                "authority_unproven"
            )))
            .unwrap_err()
            .0,
        "authority_unproven"
    );
    assert!(book.stream("command").is_err());
    assert!(book.retirement("command").is_none());
}
#[test]
fn ordinary_close_never_grants_retirement_and_registration_never_replays() {
    let mut book = registered();
    book.close_stream("command", false);
    assert!(book.retirement("command").is_none());
    let (stream, _) = fixture(None);
    assert_eq!(
        book.open(
            "command".into(),
            stream,
            Retirement {
                grant_digest: "g".into(),
                target_endpoint_id: "e".into()
            }
        )
        .unwrap_err()
        .0,
        "token_progress_stream_replayed"
    );
}

fn fixture(capacity: Option<usize>) -> (ReceiptStream, Value) {
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
    (
        ReceiptStream::new(grant, row["context"]["entry_key"].clone(), capacity).unwrap(),
        row["envelope"].clone(),
    )
}
fn next(first: &Value) -> Value {
    let mut b = first["statement"].clone();
    b["sequence"] = json!(1);
    b["start_index"] = b["end_index"].clone();
    b["token_ids"] = json!([]);
    b["status"] = json!("COMPLETED");
    b["previous_receipt_digest"] = json!(canonical::digest(&first["statement"]).unwrap());
    let who = Identity::from_secret("endpoint-entry", &[18; 32]).unwrap();
    json!({"statement":b,"signature":who.sign(&b).unwrap()})
}
#[test]
fn duplicate_retry_is_idempotent_even_at_full_capacity() {
    let (mut stream, first) = fixture(Some(1));
    let sealed = Cell::new(0);
    let a = stream
        .accept(
            &first,
            || (10.0, true),
            |_| {
                sealed.set(sealed.get() + 1);
                Ok(())
            },
        )
        .unwrap();
    assert!(!stream.can_pull());
    let b = stream
        .accept(&first, || (10.0, true), |_| panic!("duplicate resealed"))
        .unwrap();
    assert_eq!(a.receipt_digest(), b.receipt_digest());
    assert_eq!(sealed.get(), 1);
    assert_eq!(
        stream.read(|| (10.0, true)).unwrap().unwrap().token_ids(),
        &[10, 11]
    );
    assert!(stream.read(|| (10.0, true)).unwrap().is_none());
    assert!(stream.can_pull());
}
#[test]
fn overflow_is_sticky_and_never_exposes_queued_tokens() {
    let (mut stream, first) = fixture(Some(1));
    stream.accept(&first, || (10.0, true), |_| Ok(())).unwrap();
    assert_eq!(
        stream
            .accept(
                &next(&first),
                || (10.0, true),
                |_| panic!("overflow sealed")
            )
            .unwrap_err()
            .0,
        "token_progress_overflow"
    );
    assert_eq!(
        stream.read(|| (10.0, true)).unwrap_err().0,
        "token_progress_overflow"
    );
}
#[test]
fn seal_failure_and_authority_loss_after_seal_cannot_publish() {
    for revoke in [false, true] {
        let (mut stream, first) = fixture(None);
        let current = Cell::new(true);
        let error = stream
            .accept(
                &first,
                || (10.0, current.get()),
                |_| {
                    if revoke {
                        current.set(false);
                        Ok(())
                    } else {
                        Err(ContractError("disk_failure"))
                    }
                },
            )
            .unwrap_err();
        assert_eq!(
            error.0,
            if revoke {
                "token_path_grant_stale"
            } else {
                "token_progress_seal_failed"
            }
        );
        assert!(stream.read(|| (10.0, current.get())).is_err());
    }
}
#[test]
fn delivered_terminal_is_once_and_expired_duplicate_refuses() {
    let (mut stream, first) = fixture(None);
    stream.accept(&first, || (10.0, true), |_| Ok(())).unwrap();
    assert_eq!(
        stream
            .accept(&first, || (90.0, true), |_| panic!("expired"))
            .unwrap_err()
            .0,
        "token_path_grant_expired"
    );
    stream.read(|| (10.0, true)).unwrap().unwrap();
    stream
        .accept(&next(&first), || (10.0, true), |_| Ok(()))
        .unwrap();
    assert_eq!(
        stream.read(|| (10.0, true)).unwrap().unwrap().status(),
        "COMPLETED"
    );
    assert_eq!(
        stream.read(|| (10.0, true)).unwrap_err().0,
        "token_progress_terminal"
    );
}
#[test]
fn reorder_and_wrong_generation_never_advance_cursor() {
    let (mut stream, first) = fixture(None);
    assert_eq!(
        stream
            .accept(&next(&first), || (10.0, true), |_| panic!("out of order"))
            .unwrap_err()
            .0,
        "token_path_receipt_order"
    );
    assert_eq!(
        stream
            .accept(&first, || (10.0, false), |_| panic!("stale"))
            .unwrap_err()
            .0,
        "token_path_grant_stale"
    );
    stream.accept(&first, || (10.0, true), |_| Ok(())).unwrap();
    assert_eq!(
        stream.read(|| (10.0, false)).unwrap_err().0,
        "token_path_grant_stale"
    );
    assert_eq!(stream.read(|| (10.0, true)).unwrap().unwrap().sequence(), 0);
}
