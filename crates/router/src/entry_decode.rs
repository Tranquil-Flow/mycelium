// SPDX-License-Identifier: AGPL-3.0-only
//! Port of `drive_entry_decode` (mycelium_router/token_path.py:29-142) @ f4e366e9.
//!
//! Default-off entry-local decode orchestration; not a wire authority boundary. Call only after
//! authenticated request admission. Callbacks keep ownership of transport, cancellation fences,
//! runtime evidence and cleanup; this driver does not verify signatures, interrupt blocking
//! calls or mint cleanup receipts. Deadlines are already on the caller's monotonic clock (float
//! seconds, as in the oracle); the run deadline must originate at process start, not admission.
//! The caller must bound blocking decode/publication by the same deadline: the checks after a
//! callback returns cannot preempt it.
use crate::receipt_journal::{BatchStatus, TokenBatch};
use budgets::{BudgetId, Entries};
use protocol::ContractError;
use std::time::Duration;

type Result<T> = std::result::Result<T, ContractError>;

const DISABLED: &str = "token_path_disabled";
const INVALID_LIMITS: &str = "token_path_invalid_limits";
const INVALID_DEADLINE: &str = "token_path_invalid_deadline";
const INVALID_BATCH_INTERVAL: &str = "token_path_invalid_batch_interval";
const DEADLINE_EXCEEDED: &str = "command_deadline_exceeded";
const OUTPUT_INVALID: &str = "token_path_output_invalid";
const INFERENCE_FAILED: &str = "token_path_inference_failed";
const DECODE_STALLED: &str = "route_decode_stalled";
const BUDGET: &str = "token_path_budget";
/// token_path.py:55 `1 <= batch_tokens <= 128` (a count, not a duration).
const MAX_BATCH_TOKENS: usize = 128;
/// token_path.py:57 `len(stop_token_ids) > 128` is refused.
const MAX_STOP_TOKENS: usize = 128;

/// `token_path.DecodeSnapshot`: the runtime's cumulative output and status. A runtime status
/// other than DECODING, COMPLETED or CANCELLED is carried as [`BatchStatus::Failed`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DecodeSnapshot {
    pub token_ids: Vec<u32>,
    pub status: BatchStatus,
}

/// The oracle's keyword arguments other than the callbacks. Token ids are `u32`, so the
/// oracle's non-negative id checks (:58, :100) hold by type.
#[derive(Clone, Debug, PartialEq)]
pub struct EntryDecodeRequest {
    pub max_new_tokens: usize,
    pub stop_token_ids: Vec<u32>,
    pub deadline_monotonic_s: f64,
    pub run_deadline_monotonic_s: f64,
    pub enabled: bool,
    pub batch_tokens: usize,
    pub batch_seconds: f64,
}

/// Reads the runtime's current snapshot (`initial`) or runs one decode step (`decode`).
pub type Snapshot<'a> = Box<dyn FnMut() -> Result<DecodeSnapshot> + 'a>;
/// Delivers one batch; the caller bounds a blocking delivery by the same deadline.
pub type Publish<'a> = Box<dyn FnMut(&TokenBatch) -> Result<()> + 'a>;

/// Caller-owned effects. An `Err` from any callback propagates unchanged; it is never turned
/// into a success-shaped batch.
pub struct EntryDecodeCallbacks<'a> {
    pub initial: Snapshot<'a>,
    pub decode: Snapshot<'a>,
    pub publish: Publish<'a>,
    pub cancelled: Box<dyn FnMut() -> bool + 'a>,
    /// Monotonic seconds on the same clock as the request deadlines.
    pub clock: Box<dyn FnMut() -> f64 + 'a>,
}

fn fail<T>(code: &'static str) -> Result<T> {
    Err(ContractError(code))
}

/// The batch-interval ceiling of token_path.py:63, in the clock's seconds.
fn max_batch_seconds() -> Result<f64> {
    let ceiling = BudgetId::PyUpperGuardMyceliumRouterTokenPathL63C11
        .span(Entries::ZERO)
        .map_err(|_| ContractError(BUDGET))?;
    Ok(Duration::from_millis(ceiling.millis()).as_secs_f64())
}

/// The not yet published tail of `visible` (empty once everything was published).
fn rest(visible: &[u32], emitted: usize) -> &[u32] {
    visible.get(emitted..).unwrap_or(&[])
}

/// The oracle's closure state (:66-69) and its `check_deadline`/`send` helpers (:71-87).
struct Driver<'c, 'a> {
    callbacks: &'c mut EntryDecodeCallbacks<'a>,
    deadline: f64,
    emitted: usize,
    sequence: u64,
    last_publish: f64,
}
impl Driver<'_, '_> {
    fn clock(&mut self) -> f64 {
        (self.callbacks.clock)()
    }
    fn cancelled(&mut self) -> bool {
        (self.callbacks.cancelled)()
    }
    fn check_deadline(&mut self) -> Result<()> {
        if self.clock() >= self.deadline {
            fail(DEADLINE_EXCEEDED)
        } else {
            Ok(())
        }
    }
    fn send(&mut self, tokens: &[u32], status: BatchStatus) -> Result<TokenBatch> {
        self.check_deadline()?;
        let batch = TokenBatch {
            sequence: self.sequence,
            start_index: self.emitted as u64,
            token_ids: tokens.to_vec(),
            status,
        };
        (self.callbacks.publish)(&batch)?;
        // A late return cannot become successful completion. Publication itself needs the
        // caller's deadline fence; already delivered data is not recalled.
        self.check_deadline()?;
        // A failed publication must never trigger another decode.
        self.emitted += tokens.len();
        self.sequence += 1;
        self.last_publish = self.clock();
        Ok(batch)
    }
    fn cancel(&mut self) -> Result<TokenBatch> {
        self.send(&[], BatchStatus::Cancelled)
    }
}

/// Drives existing decode locally and publishes bounded, contiguous deltas; returns the final
/// published batch. Checks run in the oracle's order with its exact error codes.
pub fn drive_entry_decode(
    request: &EntryDecodeRequest,
    callbacks: &mut EntryDecodeCallbacks<'_>,
) -> Result<TokenBatch> {
    use BatchStatus::{Cancelled, Completed, Decoding};
    let r = request;
    if !r.enabled {
        return fail(DISABLED);
    }
    if r.max_new_tokens == 0
        || !(1..=MAX_BATCH_TOKENS).contains(&r.batch_tokens)
        || r.stop_token_ids.len() > MAX_STOP_TOKENS
    {
        return fail(INVALID_LIMITS);
    }
    let instants = [
        r.deadline_monotonic_s,
        r.run_deadline_monotonic_s,
        r.batch_seconds,
    ];
    if instants.iter().any(|value| !value.is_finite()) {
        return fail(INVALID_DEADLINE);
    }
    if !(0.0 < r.batch_seconds && r.batch_seconds <= max_batch_seconds()?) {
        return fail(INVALID_BATCH_INTERVAL);
    }
    // :65 `min(deadline, run_deadline)`: the earlier instant; the first argument on a tie.
    let deadline = if r.run_deadline_monotonic_s < r.deadline_monotonic_s {
        r.run_deadline_monotonic_s
    } else {
        r.deadline_monotonic_s
    };
    let last_publish = (callbacks.clock)();
    let mut d = Driver {
        callbacks,
        deadline,
        emitted: 0,
        sequence: 0,
        last_publish,
    };
    let mut previous: Vec<u32> = Vec::new();

    d.check_deadline()?;
    if d.cancelled() {
        return d.cancel();
    }
    let mut state = (d.callbacks.initial)()?;
    let mut first_snapshot = true;
    loop {
        d.check_deadline()?;
        if d.cancelled() || state.status == Cancelled {
            return d.cancel();
        }
        let tokens = state.token_ids;
        if !tokens.starts_with(&previous)
            || tokens.len() < previous.len()
            || tokens.len() > r.max_new_tokens
        {
            return fail(OUTPUT_INVALID);
        }
        if !matches!(state.status, Decoding | Completed) {
            return fail(INFERENCE_FAILED);
        }
        let stop = tokens.iter().position(|t| r.stop_token_ids.contains(t));
        let visible = stop.map_or(&tokens[..], |index| &tokens[..index]);
        let terminal =
            stop.is_some() || tokens.len() == r.max_new_tokens || state.status == Completed;
        // Chunk even a large prefill/speculative output; no batch exceeds the bound.
        while visible.len().saturating_sub(d.emitted) >= r.batch_tokens {
            if d.cancelled() {
                return d.cancel();
            }
            let (start, end) = (d.emitted, d.emitted + r.batch_tokens);
            let last = terminal && end == visible.len();
            let result = d.send(
                &visible[start..end],
                if last { Completed } else { Decoding },
            )?;
            if last {
                return Ok(result);
            }
        }
        if terminal {
            if d.cancelled() {
                return d.cancel();
            }
            return d.send(rest(visible, d.emitted), Completed);
        }
        // Preserve TTFT: the first snapshot's tokens never wait for the first decode batch;
        // later snapshots use the batch interval.
        if visible.len() > d.emitted
            && (first_snapshot || d.clock() - d.last_publish >= r.batch_seconds)
        {
            if d.cancelled() {
                return d.cancel();
            }
            d.send(rest(visible, d.emitted), Decoding)?;
        }
        first_snapshot = false;
        d.check_deadline()?;
        if d.cancelled() {
            return d.cancel();
        }
        previous = tokens;
        state = (d.callbacks.decode)()?;
        d.check_deadline()?;
        if !d.cancelled() && state.status == Decoding && state.token_ids.len() <= previous.len() {
            return fail(DECODE_STALLED);
        }
    }
}
