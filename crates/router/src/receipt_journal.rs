// SPDX-License-Identifier: AGPL-3.0-only
//! Port of `ReceiptJournal` (mycelium_router/token_path_authority.py:253-345) and
//! `CoalescedReceiptDelivery` (mycelium_router/token_receipt_delivery.py:1-69) @ f4e366e9.
//!
//! Single-producer ACK-clocked receipt ranges. Only authenticated journal eviction opens the
//! next delivery slot; the compute side buffers unsigned tokens, not signed receipts. Bounded
//! replay, never silent eviction: overflow stops the producer. Read and ACK never re-run
//! compute or call back into the producer. Retention is private; sealing full execution
//! evidence and authenticating the ACK caller are transport responsibilities, not done here.
//! A failed drain leaves reconciliation to the owner.
use budgets::{BudgetId, Entries, Span};
use protocol::{
    ContractError, canonical,
    identity::Identity,
    native::MAX_RECEIPT_BYTES,
    token::{AdmittedGrant, RECEIPT_PROTOCOL},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Condvar, Mutex, MutexGuard},
    time::Duration,
};

type Result<T> = std::result::Result<T, ContractError>;
const MAX_INT: u64 = (1 << 53) - 1;
const INVALID: &str = "token_path_receipt_invalid";
const GRANT_INVALID: ContractError = ContractError("token_path_grant_invalid");
const POISONED: ContractError = ContractError("receipt_journal_poisoned");

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BatchStatus {
    Decoding,
    Completed,
    Cancelled,
    Failed,
}
impl BatchStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Decoding => "DECODING",
            Self::Completed => "COMPLETED",
            Self::Cancelled => "CANCELLED",
            Self::Failed => "FAILED",
        }
    }
}
/// `token_path.TokenBatch`: one producer range in verified order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TokenBatch {
    pub sequence: u64,
    pub start_index: u64,
    pub token_ids: Vec<u32>,
    pub status: BatchStatus,
}
/// A participant's execution-receipt reference. Stage references are NOT liveness.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StageReceiptRef {
    pub node_id: String,
    pub receipt_digest: String,
}
/// Outcome of one bounded slot wait.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SlotWait {
    /// The slot was already open: returned at once without entering a blocking wait.
    Open,
    /// The slot opened during the wait.
    Opened,
    /// One slice elapsed with the slot still closed.
    Elapsed,
}
/// Host observation `(monotonic seconds, request control still current)`.
pub type Observe = Box<dyn Fn() -> (f64, bool) + Send + Sync>;
pub type Publish<'a> = Box<dyn FnMut(&Value) -> Result<()> + 'a>;
/// Caller's grant/cancellation fence; an error stops the delivery.
pub type Fence<'a> = Box<dyn FnMut() -> Result<()> + 'a>;
/// One bounded slot wait; receives the wait argument (never a sleep).
pub type Wait<'a> = Box<dyn FnMut(Span) -> Result<()> + 'a>;

fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok { Ok(()) } else { Err(ContractError(code)) }
}
fn text(s: &str) -> bool {
    !s.is_empty() && s.chars().count() <= 256
}
fn digest(s: &str) -> bool {
    s.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64 && hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    })
}
fn canonical_receipt(value: &Value) -> Result<Vec<u8>> {
    let bytes = canonical::to_bytes(value).map_err(|_| ContractError(INVALID))?;
    require(bytes.len() <= MAX_RECEIPT_BYTES, INVALID)?;
    Ok(bytes)
}

/// A validated statement and the state it advances to.
struct Statement {
    body: Value,
    sequence: u64,
    end: u64,
    status: BatchStatus,
    digest: String,
}
/// `_ReceiptState`: grant-bound order, chain, budget and terminality checks shared by the
/// journal and the delivery's producer-range source.
struct ReceiptState {
    grant_digest: String,
    deadline: f64,
    max_new_tokens: u64,
    stop_token_ids: Vec<u64>,
    participants: Vec<String>,
    sequence: u64,
    index: u64,
    previous: String,
    terminal: Option<BatchStatus>,
}
impl ReceiptState {
    fn new(grant: &AdmittedGrant) -> Result<Self> {
        let body = grant.statement();
        let stop_token_ids = body["stop_token_ids"]
            .as_array()
            .ok_or(GRANT_INVALID)?
            .iter()
            .map(|t| t.as_u64().ok_or(GRANT_INVALID))
            .collect::<Result<_>>()?;
        let participants = body["scope"]["participants"]
            .as_array()
            .ok_or(GRANT_INVALID)?
            .iter()
            .map(|p| {
                p["node_id"]
                    .as_str()
                    .map(str::to_owned)
                    .ok_or(GRANT_INVALID)
            })
            .collect::<Result<_>>()?;
        Ok(Self {
            grant_digest: grant.digest().to_owned(),
            deadline: grant.deadline_monotonic_s(),
            max_new_tokens: body["max_new_tokens"].as_u64().ok_or(GRANT_INVALID)?,
            stop_token_ids,
            participants,
            sequence: 0,
            index: 0,
            previous: grant.digest().to_owned(),
            terminal: None,
        })
    }
    fn fence(&self, observe: &dyn Fn() -> (f64, bool)) -> Result<()> {
        let (now, current) = observe();
        require(current, "token_path_grant_stale")?;
        require(
            now.is_finite() && now >= 0.0 && now < self.deadline,
            "token_path_grant_expired",
        )
    }
    /// `_validate` over the body this state would sign; changes nothing.
    fn validate(
        &self,
        batch: &TokenBatch,
        error_code: Option<&str>,
        refs: &[StageReceiptRef],
        observe: &dyn Fn() -> (f64, bool),
    ) -> Result<Statement> {
        self.fence(observe)?;
        require(self.terminal.is_none(), "token_path_receipt_terminal")?;
        let tokens = &batch.token_ids;
        let end = batch
            .start_index
            .checked_add(tokens.len() as u64)
            .ok_or(ContractError(INVALID))?;
        require(
            batch.sequence <= MAX_INT && batch.start_index <= MAX_INT && end <= MAX_INT,
            INVALID,
        )?;
        require(
            batch.sequence == self.sequence && batch.start_index == self.index,
            "token_path_receipt_order",
        )?;
        require(
            tokens.len() as u64 <= self.max_new_tokens
                && tokens.iter().all(|&t| t <= i32::MAX as u32),
            INVALID,
        )?;
        require(end <= self.max_new_tokens, INVALID)?;
        require(
            tokens
                .iter()
                .all(|&t| !self.stop_token_ids.contains(&u64::from(t))),
            INVALID,
        )?;
        let status = batch.status;
        require(
            (status != BatchStatus::Decoding || !tokens.is_empty())
                && (!matches!(status, BatchStatus::Cancelled | BatchStatus::Failed)
                    || tokens.is_empty()),
            INVALID,
        )?;
        require(
            if status == BatchStatus::Failed {
                error_code.is_some_and(text)
            } else {
                error_code.is_none()
            },
            INVALID,
        )?;
        require(refs.len() <= 64, INVALID)?;
        let mut seen = BTreeSet::new();
        for r in refs {
            require(
                self.participants.contains(&r.node_id)
                    && digest(&r.receipt_digest)
                    && seen.insert(r.node_id.as_str()),
                INVALID,
            )?;
        }
        let body = json!({
            "protocol": RECEIPT_PROTOCOL,
            "grant_digest": self.grant_digest,
            "previous_receipt_digest": self.previous,
            "sequence": batch.sequence,
            "start_index": batch.start_index,
            "end_index": end,
            "token_ids": tokens,
            "status": status.as_str(),
            "error_code": error_code,
            "stage_receipt_refs": refs
                .iter()
                .map(|r| json!({"node_id": r.node_id, "receipt_digest": r.receipt_digest}))
                .collect::<Vec<_>>(),
        });
        let digest = canonical::sha256_prefixed(&canonical_receipt(&body)?);
        Ok(Statement {
            body,
            sequence: batch.sequence,
            end,
            status,
            digest,
        })
    }
    /// `_advance`: infallible once `validate` has produced the statement.
    fn advance(&mut self, statement: &Statement) {
        self.previous = statement.digest.clone();
        self.sequence = statement.sequence + 1;
        self.index = statement.end;
        if statement.status != BatchStatus::Decoding {
            self.terminal = Some(statement.status);
        }
    }
}

struct Journal {
    state: ReceiptState,
    pending: BTreeMap<u64, Value>,
    acked: Option<(u64, String)>,
    slot_open: bool,
}
/// Bounded replay, never silent eviction. Overflow must stop the producer.
pub struct ReceiptJournal {
    grant: AdmittedGrant,
    signer: Identity,
    capacity: usize,
    observe: Observe,
    inner: Mutex<Journal>,
    slot: Condvar,
}
impl ReceiptJournal {
    pub fn new(
        grant: AdmittedGrant,
        signer: Identity,
        capacity: usize,
        observe: Observe,
    ) -> Result<Self> {
        let scope = &grant.statement()["scope"];
        require(
            scope["entry_endpoint_id"] == signer.endpoint_id()
                && scope["entry_key_digest"] == signer.verification_key_digest().as_str(),
            "token_path_receipt_signer",
        )?;
        require((1..=64).contains(&capacity), "token_path_receipt_capacity")?;
        let state = ReceiptState::new(&grant)?;
        Ok(Self {
            grant,
            signer,
            capacity,
            observe,
            inner: Mutex::new(Journal {
                state,
                pending: BTreeMap::new(),
                acked: None,
                slot_open: true,
            }),
            slot: Condvar::new(),
        })
    }
    pub fn grant(&self) -> &AdmittedGrant {
        &self.grant
    }
    fn lock(&self) -> Result<MutexGuard<'_, Journal>> {
        self.inner.lock().map_err(|_| POISONED)
    }
    /// Only authenticated acknowledgement opens the next delivery slot.
    pub fn delivery_ready(&self) -> Result<bool> {
        Ok(self.lock()?.slot_open)
    }
    /// Waits at most one `slice` for the slot; an open slot returns at once.
    pub fn wait_for_delivery_slot(&self, slice: Span) -> Result<SlotWait> {
        let journal = self.lock()?;
        if journal.slot_open {
            return Ok(SlotWait::Open);
        }
        let (journal, _) = self
            .slot
            .wait_timeout_while(journal, Duration::from_millis(slice.millis()), |j| {
                !j.slot_open
            })
            .map_err(|_| POISONED)?;
        Ok(if journal.slot_open {
            SlotWait::Opened
        } else {
            SlotWait::Elapsed
        })
    }
    /// Signs and retains the next range, closing the delivery slot until it is ACKed.
    pub fn append(
        &self,
        batch: &TokenBatch,
        error_code: Option<&str>,
        stage_receipt_refs: &[StageReceiptRef],
    ) -> Result<Value> {
        let mut journal = self.lock()?;
        let statement =
            journal
                .state
                .validate(batch, error_code, stage_receipt_refs, &*self.observe)?;
        require(
            journal.pending.len() < self.capacity,
            "token_path_receipt_overflow",
        )?;
        let signature = self
            .signer
            .sign(&statement.body)
            .map_err(|_| ContractError(INVALID))?;
        let envelope = json!({"statement": &statement.body, "signature": signature});
        canonical_receipt(&envelope)?;
        // Signing must not make an expired/stale receipt visible.
        journal.state.fence(&*self.observe)?;
        journal.pending.insert(statement.sequence, envelope.clone());
        journal.slot_open = false;
        journal.state.advance(&statement);
        Ok(envelope)
    }
    /// Replays retained receipts after `after_sequence` (-1 = from the first).
    pub fn read(&self, after_sequence: i64) -> Result<Vec<Value>> {
        let journal = self.lock()?;
        journal.state.fence(&*self.observe)?;
        require(
            (-1..=MAX_INT as i64).contains(&after_sequence)
                && after_sequence < journal.state.sequence as i64,
            "token_path_receipt_order",
        )?;
        require(
            journal
                .acked
                .as_ref()
                .is_none_or(|(acked, _)| after_sequence >= *acked as i64),
            "token_path_receipt_prefix_lost",
        )?;
        Ok(journal
            .pending
            .range((after_sequence + 1) as u64..)
            .map(|(_, receipt)| receipt.clone())
            .collect())
    }
    /// Exact-digest ACK evicts the whole prefix through `sequence`; a repeat of the last
    /// ACK is idempotent. The slot opens only when nothing remains pending.
    pub fn acknowledge(&self, sequence: u64, receipt_digest: &str) -> Result<()> {
        let code = "token_path_receipt_ack";
        let mut journal = self.lock()?;
        require(sequence <= MAX_INT, code)?;
        if let Some((acked, acked_digest)) = &journal.acked
            && *acked == sequence
        {
            return require(acked_digest == receipt_digest, code);
        }
        let receipt = journal.pending.get(&sequence).ok_or(ContractError(code))?;
        require(
            receipt["signature"]["signed_statement_digest"] == receipt_digest,
            code,
        )?;
        journal.pending = journal.pending.split_off(&(sequence + 1));
        journal.acked = Some((sequence, receipt_digest.to_owned()));
        if journal.pending.is_empty() {
            journal.slot_open = true;
            self.slot.notify_all();
        }
        Ok(())
    }
}

/// Coalesces producer ranges into ACK-clocked receipts on one journal. No extra reader or
/// thread; ACK handlers never publish or call back into this delivery.
pub struct CoalescedReceiptDelivery<'a> {
    journal: &'a ReceiptJournal,
    publish: Publish<'a>,
    fence: Fence<'a>,
    wait: Wait<'a>,
    slice: Span,
    source: ReceiptState,
    tokens: Vec<u32>,
    sequence: u64,
    index: u64,
    terminal: Option<BatchStatus>,
    finished: bool,
}
impl<'a> CoalescedReceiptDelivery<'a> {
    pub fn new(
        journal: &'a ReceiptJournal,
        publish: Publish<'a>,
        fence: Fence<'a>,
    ) -> Result<Self> {
        let slice = BudgetId::PyR16ReceiptDeliveryWaitSlice
            .span(Entries::ZERO)
            .map_err(|_| ContractError("receipt_delivery_budget"))?;
        Ok(Self {
            journal,
            publish,
            fence,
            wait: Box::new(move |slice| journal.wait_for_delivery_slot(slice).map(drop)),
            slice,
            source: ReceiptState::new(journal.grant())?,
            tokens: Vec::new(),
            sequence: 0,
            index: 0,
            terminal: None,
            finished: false,
        })
    }
    /// Replaces the journal's bounded slot wait (an observing driver or test).
    pub fn with_wait(mut self, wait: Wait<'a>) -> Self {
        self.wait = wait;
        self
    }
    pub fn finished(&self) -> bool {
        self.finished
    }
    /// Validates the producer's original range as well as the coalesced wire range:
    /// coalescing must never hide a gap, overlap or budget violation.
    pub fn submit(&mut self, batch: &TokenBatch) -> Result<()> {
        (self.fence)()?;
        let statement = self
            .source
            .validate(batch, None, &[], &*self.journal.observe)?;
        self.source.advance(&statement);
        self.tokens.extend_from_slice(&batch.token_ids);
        if batch.status != BatchStatus::Decoding {
            self.terminal = Some(batch.status);
        }
        self.flush()
    }
    pub fn flush(&mut self) -> Result<()> {
        (self.fence)()?;
        // Membership/peer revocation is independent of request-control equality.
        // Recheck even when an outstanding ACK prevents another append.
        self.source.fence(&*self.journal.observe)?;
        if self.finished || !self.journal.delivery_ready()? {
            return Ok(());
        }
        if self.tokens.is_empty() && self.terminal.is_none() {
            return Ok(());
        }
        // Failure/cancellation terminals never carry tokens. Publish any preceding
        // verified-order prefix as a separate range first.
        let mut status = self.terminal.unwrap_or(BatchStatus::Decoding);
        if !self.tokens.is_empty() && matches!(status, BatchStatus::Cancelled | BatchStatus::Failed)
        {
            status = BatchStatus::Decoding;
        }
        let batch = TokenBatch {
            sequence: self.sequence,
            start_index: self.index,
            token_ids: self.tokens.clone(),
            status,
        };
        let receipt = self.journal.append(&batch, None, &[])?;
        (self.fence)()?;
        (self.publish)(&receipt)?;
        (self.fence)()?;
        self.index += self.tokens.len() as u64;
        self.tokens.clear();
        self.sequence += 1;
        self.finished = status != BatchStatus::Decoding;
        Ok(())
    }
    /// No receipt-count limit and no new timeout: a stopped, revoked or expired request
    /// still fails closed through the fences; a healthy slow delivery grows one range.
    /// Returns once the terminal range is published, without waiting for its ACK.
    pub fn finish(&mut self) -> Result<()> {
        while !self.finished {
            (self.fence)()?;
            self.flush()?;
            if !self.finished {
                (self.wait)(self.slice)?;
            }
        }
        Ok(())
    }
}
