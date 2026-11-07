// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: CL-L1-07 entry-local decode driver (no B-id yet): the effective deadline is the earlier
//! of the grant and the run deadline and expires at equality, before and after every publication;
//! non-finite deadlines and out-of-range batch intervals are refused before any callback runs;
//! a stop token truncates the output; a late publication is not success; a stalled decode is
//! refused.
//!
//! Oracle: mycelium_router/token_path.py:29-142 @ f4e366e9. Expected batches and codes are
//! literals traced by hand through those lines on a fake clock; nothing is read back from the
//! system under test.
use protocol::ContractError;
use router::entry_decode::{
    DecodeSnapshot, EntryDecodeCallbacks, EntryDecodeRequest, drive_entry_decode,
};
use router::receipt_journal::{
    BatchStatus::{self, Cancelled, Completed, Decoding},
    TokenBatch,
};
use std::cell::{Cell, RefCell};
use std::collections::VecDeque;

const DEADLINE: ContractError = ContractError("command_deadline_exceeded");

fn snap(token_ids: &[u32], status: BatchStatus) -> DecodeSnapshot {
    DecodeSnapshot {
        token_ids: token_ids.to_vec(),
        status,
    }
}
fn batch(sequence: u64, start_index: u64, token_ids: &[u32], status: BatchStatus) -> TokenBatch {
    TokenBatch {
        sequence,
        start_index,
        token_ids: token_ids.to_vec(),
        status,
    }
}
/// Enabled, 8-token batches, the oracle's 50 ms interval; `grant` and `run` are the two deadlines.
fn request(max_new_tokens: usize, stop: &[u32], grant: f64, run: f64) -> EntryDecodeRequest {
    EntryDecodeRequest {
        max_new_tokens,
        stop_token_ids: stop.to_vec(),
        deadline_monotonic_s: grant,
        run_deadline_monotonic_s: run,
        enabled: true,
        batch_tokens: 8,
        batch_seconds: 0.05,
    }
}

/// One scripted `decode()` return: the fake clock moves to `now` and cancellation to `cancel`.
struct Step {
    now: f64,
    cancel: bool,
    snapshot: DecodeSnapshot,
}
fn step(now: f64, cancel: bool, snapshot: DecodeSnapshot) -> Step {
    Step {
        now,
        cancel,
        snapshot,
    }
}
#[derive(Clone, Copy)]
enum Publish {
    Returns,
    /// The publication returns with the fake clock at this instant.
    ReturnsAt(f64),
    Fails(&'static str),
}
struct Outcome {
    result: Result<TokenBatch, ContractError>,
    published: Vec<TokenBatch>,
    initial_calls: usize,
    decode_calls: usize,
    clock_reads: usize,
}

fn drive(
    request: &EntryDecodeRequest,
    start: f64,
    initial: DecodeSnapshot,
    steps: Vec<Step>,
    publish: Publish,
) -> Outcome {
    let now = Cell::new(start);
    let cancel = Cell::new(false);
    let clock_reads = Cell::new(0);
    let initial_calls = Cell::new(0);
    let decode_calls = Cell::new(0);
    let published = RefCell::new(Vec::new());
    let steps = RefCell::new(VecDeque::from(steps));
    let result = drive_entry_decode(
        request,
        &mut EntryDecodeCallbacks {
            initial: Box::new(|| {
                initial_calls.set(initial_calls.get() + 1);
                Ok(initial.clone())
            }),
            decode: Box::new(|| {
                decode_calls.set(decode_calls.get() + 1);
                let next = steps.borrow_mut().pop_front();
                let next = next.ok_or(ContractError("test_unscripted_decode"))?;
                now.set(next.now);
                cancel.set(next.cancel);
                Ok(next.snapshot)
            }),
            publish: Box::new(|batch| {
                published.borrow_mut().push(batch.clone());
                match publish {
                    Publish::Returns => Ok(()),
                    Publish::ReturnsAt(instant) => {
                        now.set(instant);
                        Ok(())
                    }
                    Publish::Fails(code) => Err(ContractError(code)),
                }
            }),
            cancelled: Box::new(|| cancel.get()),
            clock: Box::new(|| {
                clock_reads.set(clock_reads.get() + 1);
                now.get()
            }),
        },
    );
    Outcome {
        result,
        published: published.into_inner(),
        initial_calls: initial_calls.get(),
        decode_calls: decode_calls.get(),
        clock_reads: clock_reads.get(),
    }
}

#[test]
fn entry_decode_deadline_is_min_of_grant_and_run() {
    // :65 deadline = min(10.0, 7.5) = 7.5 in either argument order; :71-73 expire at clock >= 7.5.
    let done = batch(0, 0, &[11, 12], Completed);
    for (grant, run) in [(10.0, 7.5), (7.5, 10.0)] {
        let req = request(4, &[], grant, run);
        let before = drive(
            &req,
            7.4999,
            snap(&[11, 12], Completed),
            vec![],
            Publish::Returns,
        );
        assert_eq!(before.result, Ok(done.clone()), "grant {grant} run {run}");
        assert_eq!(
            before.published,
            vec![done.clone()],
            "grant {grant} run {run}"
        );
        let at = drive(
            &req,
            7.5,
            snap(&[11, 12], Completed),
            vec![],
            Publish::Returns,
        );
        assert_eq!(at.result, Err(DEADLINE), "grant {grant} run {run}");
        assert_eq!(at.published, vec![], "grant {grant} run {run}");
        assert_eq!(at.initial_calls, 0, "grant {grant} run {run}");
    }
}

#[test]
fn entry_decode_equal_deadline_boundary() {
    // grant == run == 3.0; :72 `clock() >= deadline` expires at exactly 3.0.
    let req = request(4, &[], 3.0, 3.0);
    let ttft = batch(0, 0, &[7], Decoding);
    // decode() returns at exactly 3.0: the check after decode (:139) expires.
    let at = drive(
        &req,
        1.0,
        snap(&[7], Decoding),
        vec![step(3.0, false, snap(&[7, 8], Completed))],
        Publish::Returns,
    );
    assert_eq!(at.result, Err(DEADLINE));
    assert_eq!(at.published, vec![ttft.clone()]);
    assert_eq!(at.decode_calls, 1);
    // decode() returns at 2.999: the run continues to the terminal batch.
    let below = drive(
        &req,
        1.0,
        snap(&[7], Decoding),
        vec![step(2.999, false, snap(&[7, 8], Completed))],
        Publish::Returns,
    );
    assert_eq!(below.result, Ok(batch(1, 1, &[8], Completed)));
    assert_eq!(below.published, vec![ttft, batch(1, 1, &[8], Completed)]);
    // Already at 3.0 on entry (:89): expires before initial().
    let entry = drive(&req, 3.0, snap(&[7], Decoding), vec![], Publish::Returns);
    assert_eq!(entry.result, Err(DEADLINE));
    assert_eq!(entry.published, vec![]);
    assert_eq!(entry.initial_calls, 0);
}

#[test]
fn entry_decode_non_finite_deadline_refused() {
    // :60-62 each of deadline, run deadline and batch interval must be finite; refused before
    // the clock or any callback runs (:68 is the first clock read). A non-finite batch interval
    // is a deadline refusal, not an interval refusal (:60-62 precede :63-64).
    let finite = request(4, &[], 10.0, 10.0);
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        for req in [
            EntryDecodeRequest {
                deadline_monotonic_s: value,
                ..finite.clone()
            },
            EntryDecodeRequest {
                run_deadline_monotonic_s: value,
                ..finite.clone()
            },
            EntryDecodeRequest {
                batch_seconds: value,
                ..finite.clone()
            },
        ] {
            let out = drive(&req, 1.0, snap(&[5], Completed), vec![], Publish::Returns);
            let code = ContractError("token_path_invalid_deadline");
            assert_eq!(out.result, Err(code), "{req:?}");
            assert_eq!(out.published, vec![], "{req:?}");
            assert_eq!((out.initial_calls, out.clock_reads), (0, 0), "{req:?}");
        }
    }
    // Control: the same request with finite values is driven.
    let ok = drive(
        &finite,
        1.0,
        snap(&[5], Completed),
        vec![],
        Publish::Returns,
    );
    assert_eq!(ok.result, Ok(batch(0, 0, &[5], Completed)));
    assert_eq!(ok.published, vec![batch(0, 0, &[5], Completed)]);
}

#[test]
fn entry_decode_batch_interval_bounds() {
    // :63-64 `0 < batch_seconds <= 0.05`; out of range is refused before any callback.
    for seconds in [0.0, 0.0500001, -0.05] {
        let req = EntryDecodeRequest {
            batch_seconds: seconds,
            ..request(8, &[], 10.0, 10.0)
        };
        let out = drive(&req, 1.0, snap(&[1], Decoding), vec![], Publish::Returns);
        let code = ContractError("token_path_invalid_batch_interval");
        assert_eq!(out.result, Err(code), "batch_seconds {seconds}");
        assert_eq!(out.published, vec![], "batch_seconds {seconds}");
        assert_eq!(
            (out.initial_calls, out.clock_reads),
            (0, 0),
            "batch_seconds {seconds}"
        );
    }
    // 0.05 is accepted and applied (:129): after the first-snapshot publish at 1.0, new tokens
    // are held 0.03125 s later and published 0.0625 s later.
    let req = EntryDecodeRequest {
        batch_seconds: 0.05,
        ..request(8, &[], 10.0, 10.0)
    };
    let out = drive(
        &req,
        1.0,
        snap(&[1], Decoding),
        vec![
            step(1.03125, false, snap(&[1, 2], Decoding)),
            step(1.0625, false, snap(&[1, 2, 3], Decoding)),
            step(1.0625, false, snap(&[1, 2, 3, 4], Completed)),
        ],
        Publish::Returns,
    );
    assert_eq!(out.result, Ok(batch(2, 3, &[4], Completed)));
    assert_eq!(
        out.published,
        vec![
            batch(0, 0, &[1], Decoding),
            batch(1, 1, &[2, 3], Decoding),
            batch(2, 3, &[4], Completed),
        ]
    );
    assert_eq!(out.decode_calls, 3);
}

#[test]
fn entry_decode_stop_token_truncates() {
    // :109-113 the first stop token ends the output; it and everything after it stay unpublished.
    let req = request(8, &[99], 10.0, 10.0);
    let out = drive(
        &req,
        1.0,
        snap(&[4, 5], Decoding),
        vec![step(1.0, false, snap(&[4, 5, 6, 99, 7], Decoding))],
        Publish::Returns,
    );
    assert_eq!(out.result, Ok(batch(1, 2, &[6], Completed)));
    assert_eq!(
        out.published,
        vec![batch(0, 0, &[4, 5], Decoding), batch(1, 2, &[6], Completed)]
    );
    assert_eq!(out.decode_calls, 1);
    // A stop token first: nothing is visible; one empty COMPLETED batch, no decode.
    let first = drive(
        &req,
        1.0,
        snap(&[99, 3], Decoding),
        vec![],
        Publish::Returns,
    );
    assert_eq!(first.result, Ok(batch(0, 0, &[], Completed)));
    assert_eq!(first.published, vec![batch(0, 0, &[], Completed)]);
    assert_eq!(first.decode_calls, 0);
    // Truncated output larger than a batch is chunked (:115-122); no batch exceeds 2 tokens.
    let two = EntryDecodeRequest {
        batch_tokens: 2,
        ..req
    };
    for (initial, expected) in [
        (
            snap(&[1, 2, 3, 99, 4], Decoding),
            vec![batch(0, 0, &[1, 2], Decoding), batch(1, 2, &[3], Completed)],
        ),
        (
            snap(&[1, 2, 3, 4, 99], Decoding),
            vec![
                batch(0, 0, &[1, 2], Decoding),
                batch(1, 2, &[3, 4], Completed),
            ],
        ),
    ] {
        let out = drive(&two, 1.0, initial, vec![], Publish::Returns);
        assert_eq!(out.result, Ok(expected[1].clone()));
        assert_eq!(out.published, expected);
        assert_eq!(out.decode_calls, 0);
    }
}

#[test]
fn entry_decode_late_publish_is_not_success() {
    // :77-82 the fence runs again after publish(): a publication that returns at the deadline
    // (min(5.0, 6.0) = 5.0) is a timeout although the batch was delivered, and no decode follows.
    let req = request(4, &[], 5.0, 6.0);
    for initial in [snap(&[3], Decoding), snap(&[3], Completed)] {
        let status = initial.status;
        let out = drive(
            &req,
            1.0,
            initial,
            vec![step(1.0, false, snap(&[3, 4], Completed))],
            Publish::ReturnsAt(5.0),
        );
        assert_eq!(out.result, Err(DEADLINE), "{status:?}");
        assert_eq!(out.published, vec![batch(0, 0, &[3], status)], "{status:?}");
        assert_eq!(out.decode_calls, 0, "{status:?}");
    }
    // A failed publication propagates its own code and never triggers another decode (:83).
    let failed = drive(
        &req,
        1.0,
        snap(&[3], Decoding),
        vec![step(1.0, false, snap(&[3, 4], Completed))],
        Publish::Fails("test_publish_transport_closed"),
    );
    assert_eq!(
        failed.result,
        Err(ContractError("test_publish_transport_closed"))
    );
    assert_eq!(failed.published, vec![batch(0, 0, &[3], Decoding)]);
    assert_eq!(failed.decode_calls, 0);
    // Control: publications returning at 4.999 succeed and decoding continues.
    let in_time = drive(
        &req,
        1.0,
        snap(&[3], Decoding),
        vec![step(4.999, false, snap(&[3, 4], Completed))],
        Publish::ReturnsAt(4.999),
    );
    assert_eq!(in_time.result, Ok(batch(1, 1, &[4], Completed)));
    assert_eq!(
        in_time.published,
        vec![batch(0, 0, &[3], Decoding), batch(1, 1, &[4], Completed)]
    );
    assert_eq!(in_time.decode_calls, 1);
}

#[test]
fn entry_decode_stalled_decode_refused() {
    // :139-142 after decode(), an uncancelled DECODING snapshot that did not grow is refused,
    // before the output checks of the next loop (a shrunk snapshot is a stall too).
    let req = request(4, &[], 10.0, 10.0);
    let ttft = batch(0, 0, &[1], Decoding);
    for stalled in [snap(&[1], Decoding), snap(&[], Decoding)] {
        let out = drive(
            &req,
            1.0,
            snap(&[1], Decoding),
            vec![step(1.0, false, stalled.clone())],
            Publish::Returns,
        );
        assert_eq!(
            out.result,
            Err(ContractError("route_decode_stalled")),
            "{stalled:?}"
        );
        assert_eq!(out.published, vec![ttft.clone()], "{stalled:?}");
        assert_eq!(out.decode_calls, 1, "{stalled:?}");
    }
    // Not stalls: the same length COMPLETED ends with an empty final batch; a cancelled request
    // ends CANCELLED.
    let done = drive(
        &req,
        1.0,
        snap(&[1], Decoding),
        vec![step(1.0, false, snap(&[1], Completed))],
        Publish::Returns,
    );
    assert_eq!(done.result, Ok(batch(1, 1, &[], Completed)));
    assert_eq!(
        done.published,
        vec![ttft.clone(), batch(1, 1, &[], Completed)]
    );
    let cancelled = drive(
        &req,
        1.0,
        snap(&[1], Decoding),
        vec![step(1.0, true, snap(&[1], Decoding))],
        Publish::Returns,
    );
    assert_eq!(cancelled.result, Ok(batch(1, 1, &[], Cancelled)));
    assert_eq!(cancelled.published, vec![ttft, batch(1, 1, &[], Cancelled)]);
}
