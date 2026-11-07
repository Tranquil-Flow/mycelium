// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: CL-L1-07 ACK-clocked receipt delivery (no B-id yet): only the exact-digest ACK evicts and
//! opens the delivery slot, the slot wait takes one budgeted slice per loop and returns at once when
//! open, and finish publishes the terminal range without waiting for its ACK but stops on the fence.
//!
//! Oracle: mycelium_router/token_path_authority.py:272-280,309-322 and
//! mycelium_router/token_receipt_delivery.py:43-69 @ f4e366e9. Expected values are literals from
//! those lines; ACK digests are recomputed here from the published statement, never read back
//! from the system under test.
use budgets::Span;
use protocol::{ContractError, canonical, identity::Identity, token::*};
use router::receipt_journal::{
    BatchStatus, CoalescedReceiptDelivery, ReceiptJournal, SlotWait, TokenBatch,
};
use serde_json::{Value, json};
use std::cell::{Cell, RefCell};

/// The oracle's `.05` wait argument (token_receipt_delivery.py:69).
const SLICE: Span = Span::from_millis(50);

fn grant_row() -> Value {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-token-vectors.json"
    ))
    .unwrap();
    rows.into_iter()
        .find(|r| r["name"] == "DECODING-2")
        .unwrap()
}
fn admitted(row: &Value) -> AdmittedGrant {
    let c = &row["context"]["grant_context"];
    GrantRegistry::new(4096)
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
        .unwrap()
}
/// Fresh journal on the pinned grant, signed by the scope's entry endpoint, observed at a
/// current instant inside the grant window.
fn journal() -> ReceiptJournal {
    let entry = Identity::from_secret("endpoint-entry", &[18; 32]).unwrap();
    ReceiptJournal::new(admitted(&grant_row()), entry, 64, Box::new(|| (10.0, true))).unwrap()
}
fn batch(sequence: u64, start_index: u64, token_ids: &[u32], status: BatchStatus) -> TokenBatch {
    TokenBatch {
        sequence,
        start_index,
        token_ids: token_ids.to_vec(),
        status,
    }
}
fn range(receipt: &Value) -> Value {
    let s = &receipt["statement"];
    json!({"sequence":s["sequence"],"start_index":s["start_index"],
        "token_ids":s["token_ids"],"status":s["status"]})
}
fn ranges(receipts: &[Value]) -> Vec<Value> {
    receipts.iter().map(range).collect()
}
fn first() -> Value {
    json!({"sequence":0,"start_index":0,"token_ids":[11],"status":"DECODING"})
}
fn terminal() -> Value {
    json!({"sequence":1,"start_index":1,"token_ids":[12],"status":"COMPLETED"})
}
/// The ACK input: sha256 of the canonical published statement, computed independently.
fn statement_digest(receipt: &Value) -> String {
    canonical::digest(&receipt["statement"]).unwrap()
}

#[test]
fn receipt_journal_open_slot_returns_immediately() {
    let journal = journal();
    // :272-273 the slot starts set; :279-280 waiting on a set slot does not block.
    assert_eq!(journal.delivery_ready(), Ok(true));
    assert_eq!(journal.wait_for_delivery_slot(SLICE), Ok(SlotWait::Open));
    assert_eq!(journal.delivery_ready(), Ok(true));
}

#[test]
fn receipt_journal_wait_uses_one_slice_per_loop() {
    let journal = journal();
    let published = RefCell::new(Vec::<Value>::new());
    let waits = RefCell::new(Vec::<Span>::new());
    let mut delivery = CoalescedReceiptDelivery::new(
        &journal,
        Box::new(|r: &Value| {
            published.borrow_mut().push(r.clone());
            Ok(())
        }),
        Box::new(|| Ok(())),
    )
    .unwrap()
    .with_wait(Box::new(|slice| {
        waits.borrow_mut().push(slice);
        if waits.borrow().len() == 2 {
            let receipt0 = published.borrow()[0].clone();
            journal.acknowledge(0, &statement_digest(&receipt0))?;
        }
        Ok(())
    }));
    assert_eq!(
        delivery.submit(&batch(0, 0, &[11], BatchStatus::Decoding)),
        Ok(())
    );
    // The first receipt occupies the slot ...
    assert_eq!(ranges(&published.borrow()), vec![first()]);
    assert_eq!(journal.delivery_ready(), Ok(false));
    // ... so the terminal tokens buffer instead of publishing.
    assert_eq!(
        delivery.submit(&batch(1, 1, &[12], BatchStatus::Completed)),
        Ok(())
    );
    assert_eq!(ranges(&published.borrow()), vec![first()]);
    assert_eq!(delivery.finish(), Ok(()));
    // Wait 1 leaves receipt 0 pending, wait 2 ACKs it, the third pass publishes: no third wait.
    assert_eq!(*waits.borrow(), vec![SLICE, SLICE]);
    assert_eq!(ranges(&published.borrow()), vec![first(), terminal()]);
    assert!(delivery.finished());
}

#[test]
fn receipt_journal_ack_wrong_digest_keeps_pending() {
    let journal = journal();
    let receipt0 = journal.append(&batch(0, 0, &[11], BatchStatus::Decoding), None, &[]);
    assert_eq!(receipt0.as_ref().map(range), Ok(first()));
    let receipt0 = receipt0.unwrap();
    let exact = statement_digest(&receipt0);
    assert_eq!(
        receipt0["signature"]["signed_statement_digest"],
        exact.as_str()
    );
    let wrong = format!("sha256:{}", "0".repeat(64));
    assert_eq!(
        journal.acknowledge(0, &wrong),
        Err(ContractError("token_path_receipt_ack"))
    );
    assert_eq!(journal.read(-1), Ok(vec![receipt0.clone()]));
    assert_eq!(journal.delivery_ready(), Ok(false));
    // Positive control: the exact digest evicts and opens the slot.
    assert_eq!(journal.acknowledge(0, &exact), Ok(()));
    assert_eq!(journal.read(0), Ok(vec![]));
    assert_eq!(journal.delivery_ready(), Ok(true));
}

#[test]
fn receipt_journal_duplicate_ack_is_idempotent() {
    let journal = journal();
    let receipt0 = journal.append(&batch(0, 0, &[11], BatchStatus::Decoding), None, &[]);
    assert_eq!(receipt0.as_ref().map(range), Ok(first()));
    let exact = statement_digest(&receipt0.unwrap());
    assert_eq!(journal.acknowledge(0, &exact), Ok(()));
    assert_eq!(journal.acknowledge(0, &exact), Ok(()));
    assert_eq!(journal.read(0), Ok(vec![]));
    // :303 a reader behind the acknowledged prefix has lost it.
    assert_eq!(
        journal.read(-1),
        Err(ContractError("token_path_receipt_prefix_lost"))
    );
    assert_eq!(journal.delivery_ready(), Ok(true));
}

#[test]
fn receipt_journal_ack_evicts_prefix_through_sequence() {
    let journal = journal();
    let receipt0 = journal.append(&batch(0, 0, &[11], BatchStatus::Decoding), None, &[]);
    let receipt1 = journal.append(&batch(1, 1, &[12], BatchStatus::Decoding), None, &[]);
    assert_eq!(
        [&receipt0, &receipt1].map(|r| r.as_ref().map(range)),
        [
            Ok(first()),
            Ok(json!({"sequence":1,"start_index":1,"token_ids":[12],"status":"DECODING"}))
        ]
    );
    let receipt1 = receipt1.unwrap();
    // A closed slot waits one bounded slice and returns; it never waits for an ACK.
    assert_eq!(journal.wait_for_delivery_slot(SLICE), Ok(SlotWait::Elapsed));
    // :318 one ACK removes the whole prefix through its sequence, not only that sequence.
    assert_eq!(journal.acknowledge(1, &statement_digest(&receipt1)), Ok(()));
    assert_eq!(journal.read(1), Ok(vec![]));
    assert_eq!(
        journal.read(0),
        Err(ContractError("token_path_receipt_prefix_lost"))
    );
    assert_eq!(
        journal.acknowledge(0, &statement_digest(&receipt0.unwrap())),
        Err(ContractError("token_path_receipt_ack"))
    );
    assert_eq!(journal.delivery_ready(), Ok(true));
    assert_eq!(journal.wait_for_delivery_slot(SLICE), Ok(SlotWait::Open));
}

#[test]
fn receipt_journal_finish_drains_after_last_ack() {
    let journal = journal();
    let published = RefCell::new(Vec::<Value>::new());
    let waits = RefCell::new(Vec::<Span>::new());
    let mut delivery = CoalescedReceiptDelivery::new(
        &journal,
        Box::new(|r: &Value| {
            published.borrow_mut().push(r.clone());
            Ok(())
        }),
        Box::new(|| Ok(())),
    )
    .unwrap()
    .with_wait(Box::new(|slice| {
        waits.borrow_mut().push(slice);
        if waits.borrow().len() == 1 {
            let receipt0 = published.borrow()[0].clone();
            journal.acknowledge(0, &statement_digest(&receipt0))?;
        }
        Ok(())
    }));
    assert_eq!(
        delivery.submit(&batch(0, 0, &[11], BatchStatus::Decoding)),
        Ok(())
    );
    assert_eq!(
        delivery.submit(&batch(1, 1, &[12], BatchStatus::Completed)),
        Ok(())
    );
    assert_eq!(delivery.finish(), Ok(()));
    let published = published.borrow().clone();
    assert_eq!(ranges(&published), vec![first(), terminal()]);
    assert!(delivery.finished());
    assert_eq!(*waits.borrow(), vec![SLICE]);
    // finish returned with the terminal receipt still pending: it never waits for that ACK.
    assert_eq!(journal.read(0), Ok(vec![published[1].clone()]));
    assert_eq!(journal.delivery_ready(), Ok(false));
    assert_eq!(
        journal.acknowledge(1, &statement_digest(&published[1])),
        Ok(())
    );
    assert_eq!(journal.delivery_ready(), Ok(true));
    // The owner-side cursor verifies both published ranges (signature, chain, order).
    let row = grant_row();
    let mut cursor = ReceiptCursor::new(admitted(&row), row["context"]["entry_key"].clone());
    assert_eq!(cursor.accept(&published[0], || (10.0, true)), Ok(vec![11]));
    assert_eq!(cursor.accept(&published[1], || (10.0, true)), Ok(vec![12]));
    assert_eq!(cursor.terminal(), Some("COMPLETED"));
}

#[test]
fn receipt_journal_finish_stops_on_fence() {
    let journal = journal();
    let published = RefCell::new(Vec::<Value>::new());
    let waits = RefCell::new(Vec::<Span>::new());
    let expired = Cell::new(false);
    let fences = Cell::new(0_u32);
    let mut delivery = CoalescedReceiptDelivery::new(
        &journal,
        Box::new(|r: &Value| {
            published.borrow_mut().push(r.clone());
            Ok(())
        }),
        Box::new(|| {
            fences.set(fences.get() + 1);
            if expired.get() {
                return Err(ContractError("token_path_grant_expired"));
            }
            Ok(())
        }),
    )
    .unwrap()
    .with_wait(Box::new(|slice| {
        waits.borrow_mut().push(slice);
        Ok(())
    }));
    assert_eq!(
        delivery.submit(&batch(0, 0, &[11], BatchStatus::Decoding)),
        Ok(())
    );
    assert_eq!(
        delivery.submit(&batch(1, 1, &[12], BatchStatus::Completed)),
        Ok(())
    );
    assert_eq!(ranges(&published.borrow()), vec![first()]);
    expired.set(true);
    fences.set(0);
    assert_eq!(
        delivery.finish(),
        Err(ContractError("token_path_grant_expired"))
    );
    assert_eq!(fences.get(), 1);
    assert_eq!(ranges(&published.borrow()), vec![first()]);
    assert_eq!(*waits.borrow(), Vec::<Span>::new());
    assert!(!delivery.finished());
}
