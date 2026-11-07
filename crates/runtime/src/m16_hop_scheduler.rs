// SPDX-License-Identifier: AGPL-3.0-only
//! M16 hop scheduler: `DuplicateHopError`, `BackpressureError` and `HopScheduler`
//! (mycelium_router/scheduler.py:16-302 @ f4e366e9), the device-wide fair queue the M16
//! coordinator builds as `HopScheduler(config)` (mycelium_m16_runtime.py:394) with the factory's
//! `RouterConfig(reservation_lease_seconds=3_600.0)` (:1260). Admission enqueues one `REQUEST`
//! item per admitted path (:452-480), `next_dispatch` pops with `pop_next(now)` (:500-506), and
//! cancellation, recovery cutover and recovery abort call `release_path` (:587, :903, :973); the
//! status reads `queue_depth` and `queued_payload_bytes` (:623-625). The member router and
//! relay use the same class (router.py:35, relay.py:236; `take`/`withdraw` at relay.py:1393-1409
//! and 1834-1850); their wiring is not here, nor is the coordinator, admission status, the
//! `M16AdmissionError` mapping, `PhaseAwareBatchController` or `a10_runtime_batch`.
//!
//! Values are Python's at that seam. The [`RouterConfig`] fields are [`PyReal`]s and meet the
//! scheduler's ints and floats as CPython makes them: builtin `max(first, second)` keeps
//! `first` unless `second` compares strictly above it (so `max(0, NaN)` is `0` and
//! `max(0.001, True)` is `True`), int-versus-float comparison is exact, and an int meeting a
//! float goes through `float()` (OverflowError past the double range). Priority keys
//! `(-priority, request_id, path_id, token_index, hop_index, offset)` and idempotency entries
//! `(seen_at, hop_identity)` order as CPython's tuples: the first element that is not equal
//! decides with `<`, the token and hop indices and the attempt are the items' Python numbers
//! (exact across int, float and bool; a NaN is below nothing and nothing is below it, and the
//! decoder's NaN is one object, so it equals itself; see [`py_same_element`]). `min` keeps the
//! first of keys none is below, and `sorted` is CPython's `list.sort` itself ([`list_sort`]), so
//! keys that do not form a total order come out as CPython leaves them. Hop identities are dict
//! keys under Python `==` (`True == 1 == 1.0`, `-0.0 == 0`), the idempotency entries in insertion
//! order, as a dict keeps them. A float batch maximum that reaches the slice `[:maximum]` raises
//! the slice's TypeError. Item times are the clocks' floats.
//!
//! Locking: the oracle serialises every operation on an `RLock` (scheduler.py:27-36, 42); here
//! every mutation takes `&mut self`, so the caller's own lock (the coordinator's, as
//! mycelium_m16_runtime.py:382 holds its `RLock` around each call) is the serialisation.
//!
//! Declared limits (RU-001, tagged V03; inputs no v0.1.4 producer makes, refused with a
//! specific code instead of reproducing an order CPython's sort leaves to its algorithm):
//! - an item whose `enqueued_at` is NaN ([`NAN_ENQUEUED_AT`]): its idempotency entry would sort
//!   unordered among the others in the oldest-first eviction (:293-299). Every producer stamps
//!   `enqueued_at` from a clock or `0.0` (mycelium_m16_runtime.py:451, relay.py:1378, 1804,
//!   2172, 2892).
//! - a batch whose compatible items (two or more) include a NaN effective priority
//!   ([`UNORDERED_BATCH_PRIORITY`]): `sorted` by the priority key (:138-145, :182-185) has no
//!   defined order for them. A NaN priority needs a non-finite priority field in the
//!   RouterConfig (the factory's are finite, :1260) or a non-finite clock reading; a NaN or
//!   infinite deficit ratio clamps to a finite deficit (:82).
use crate::m16_router_contracts::{
    BatchDecision, HopIdentity, HopPayload, HopWorkItem, IdMap, PyError, PyJson, PyReal,
    RouterConfig, RouterContractError, RuntimeBatch, hop_identity, hop_lt, py_add, py_cmp,
    py_float, py_lt, py_max, py_mul, py_same_element, same_hop, seconds,
};
use budgets::BudgetId;
use std::cmp::Ordering;
use std::collections::BTreeSet;

/// A scheduler operation's refusal, by the oracle's exception kind.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SchedulerError {
    /// scheduler.py:16-17 `DuplicateHopError(item.idempotency_key)` (:50-51).
    Duplicate { idempotency_key: String },
    /// scheduler.py:20-24 `BackpressureError(reason, retry_after_seconds)`: `work_limit`
    /// (:53-54) or `byte_limit` (:56-60), with `max(0.001, backpressure_retry_after_seconds)`
    /// (:52) as Python's `max` returns it.
    Backpressure {
        reason: &'static str,
        retry_after_seconds: PyReal,
    },
    /// IndexError: `empty_hop_queue` (:158-159).
    Index(&'static str),
    /// TypeError: `unsupported_hop_payload_for_byte_accounting` (:275), or one CPython raises on
    /// these lines (`m16_hop_scheduler_*`).
    Type(&'static str),
    /// OverflowError raised by these lines' int-to-float conversion.
    Overflow(&'static str),
    /// ValueError from `RuntimeBatch.__post_init__` (contracts.py:327-333).
    Contract(&'static str),
    /// A declared V03 refusal (no oracle counterpart; see the module documentation).
    Unsupported(&'static str),
}

impl SchedulerError {
    /// The specific code (the oracle's message where it is a code).
    pub fn code(&self) -> &str {
        match self {
            Self::Duplicate { idempotency_key } => idempotency_key,
            Self::Backpressure { reason, .. } => reason,
            Self::Index(code)
            | Self::Type(code)
            | Self::Overflow(code)
            | Self::Contract(code)
            | Self::Unsupported(code) => code,
        }
    }

    /// The exception kind: `duplicate`, `backpressure`, `index`, `type`, `overflow`,
    /// `contract` or `unsupported`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Duplicate { .. } => "duplicate",
            Self::Backpressure { .. } => "backpressure",
            Self::Index(_) => "index",
            Self::Type(_) => "type",
            Self::Overflow(_) => "overflow",
            Self::Contract(_) => "contract",
            Self::Unsupported(_) => "unsupported",
        }
    }
}

/// scheduler.py:159.
pub const EMPTY_HOP_QUEUE: &str = "empty_hop_queue";
/// scheduler.py:275.
pub const UNSUPPORTED_PAYLOAD: &str = "unsupported_hop_payload_for_byte_accounting";
/// scheduler.py:54.
pub const WORK_LIMIT: &str = "work_limit";
/// scheduler.py:60.
pub const BYTE_LIMIT: &str = "byte_limit";
/// TypeError "slice indices must be integers or None or have an __index__ method": a float
/// batch maximum at `[:maximum]` (:186).
pub const SLICE_INDEX_NOT_INT: &str = "m16_hop_scheduler_slice_indices_must_be_integers";
/// OverflowError "int too large to convert to float": an int priority field meeting the float
/// age or deficit (:81-84).
pub const INT_TOO_LARGE_FOR_FLOAT: &str = "m16_hop_scheduler_int_too_large_for_float";
/// TypeError "unhashable type": a list or dict placement id counted in `placement_depths`
/// (:259).
pub const UNHASHABLE_PLACEMENT_ID: &str = "m16_hop_scheduler_unhashable_placement_id";
/// Declared V03: an item stamped with a NaN `enqueued_at`.
pub const NAN_ENQUEUED_AT: &str = "m16_hop_scheduler_nan_enqueued_at";
/// Declared V03: a NaN effective priority among two or more compatible batch items.
pub const UNORDERED_BATCH_PRIORITY: &str = "m16_hop_scheduler_unordered_batch_priority";

fn arithmetic(error: PyError) -> SchedulerError {
    match error {
        // `*` and `+` raise no other arithmetic error.
        PyError::IntTooLargeForFloat
        | PyError::IntDivisionTooLargeForFloat
        | PyError::ZeroDivision => SchedulerError::Overflow(INT_TOO_LARGE_FOR_FLOAT),
    }
}

fn count(n: usize) -> PyReal {
    PyReal::int(n as i64)
}

/// `a >= b`.
fn py_ge(a: &PyReal, b: &PyReal) -> bool {
    matches!(py_cmp(a, b), Some(Ordering::Greater | Ordering::Equal))
}

/// Builtin `max(first, second)` of two floats.
fn float_max(first: f64, second: f64) -> f64 {
    if first < second { second } else { first }
}

/// Builtin `min(first, second)` of two floats.
fn float_min(first: f64, second: f64) -> f64 {
    if second < first { second } else { first }
}

/// scheduler.py:265-275 `_payload_size`.
fn payload_size(payload: &HopPayload) -> Result<u64, SchedulerError> {
    match payload {
        HopPayload::None => Ok(0),
        HopPayload::Bytes(bytes) => Ok(bytes.len() as u64),
        HopPayload::Tuple(values) | HopPayload::List(values)
            if values.iter().all(|v| matches!(v, PyReal::Int { .. })) =>
        {
            Ok(values.len() as u64 * 4)
        }
        _ => Err(SchedulerError::Type(UNSUPPORTED_PAYLOAD)),
    }
}

/// scheduler.py:226-235 `_priority_key`: `(-effective_priority, request_id, path_id,
/// token_index, hop_index, offset)`.
struct PriorityKey<'a> {
    negated_priority: f64,
    request_id: &'a str,
    path_id: &'a str,
    token_index: &'a PyReal,
    hop_index: &'a PyReal,
    offset: usize,
}

impl PriorityKey<'_> {
    /// CPython's tuple `<` (`tuplerichcompare`, and `unsafe_tuple_compare` in `list.sort`): the
    /// first element that is not equal decides with `<`. The negated priority is a fresh float
    /// per key, so a NaN priority equals nothing; the indices are the items' own objects
    /// ([`py_same_element`]).
    fn lt(&self, other: &Self) -> bool {
        if self.negated_priority != other.negated_priority {
            return self.negated_priority < other.negated_priority;
        }
        if self.request_id != other.request_id {
            return self.request_id < other.request_id;
        }
        if self.path_id != other.path_id {
            return self.path_id < other.path_id;
        }
        if !py_same_element(self.token_index, other.token_index) {
            return py_lt(self.token_index, other.token_index);
        }
        if !py_same_element(self.hop_index, other.hop_index) {
            return py_lt(self.hop_index, other.hop_index);
        }
        self.offset < other.offset
    }
}

/// One idempotency entry: its hop identity and `(enqueued_at, path_id)` (:61-64).
#[derive(Clone, Debug)]
struct Seen {
    identity: HopIdentity,
    seen_at: f64,
    path_id: String,
}

/// scheduler.py:39-302.
#[derive(Clone, Debug)]
pub struct HopScheduler {
    config: RouterConfig,
    /// scheduler.py:52 the retry floor `0.001`.
    retry_floor: PyReal,
    /// The queue in arrival order, each item with its payload size (sized once at enqueue:
    /// an item is an owned value, so its payload cannot change while queued).
    queue: Vec<(HopWorkItem, u64)>,
    queued_payload_bytes: u64,
    /// The `_seen_keys` dict: one entry per Python-equal identity ([`same_hop`]), in insertion
    /// order.
    seen_keys: Vec<Seen>,
}

impl HopScheduler {
    /// `HopScheduler(config)` (:40-45): an empty queue, no bytes, no idempotency entries.
    pub fn new(config: RouterConfig) -> Result<Self, RouterContractError> {
        Ok(Self {
            config,
            retry_floor: seconds(BudgetId::PyQuantityMyceliumRouterSchedulerL52C6)?,
            queue: Vec::new(),
            queued_payload_bytes: 0,
            seen_keys: Vec::new(),
        })
    }

    pub fn config(&self) -> &RouterConfig {
        &self.config
    }

    /// scheduler.py:47-67, in its order: evict at `item.enqueued_at`; DuplicateHopError when the
    /// hop identity is already recorded; `work_limit` when `len(queue) >= max(0,
    /// maximum_pending_hops)`; TypeError for a payload with no byte size;
    /// `byte_limit` when `queued + size > max(0, maximum_pending_bytes)`; then record the
    /// identity at `(enqueued_at, path_id)`, append, and evict again. A refusal records nothing
    /// (the first eviction has run).
    pub fn enqueue(&mut self, item: HopWorkItem) -> Result<(), SchedulerError> {
        if item.enqueued_at.is_nan() {
            return Err(SchedulerError::Unsupported(NAN_ENQUEUED_AT));
        }
        self.evict_seen(item.enqueued_at);
        let identity = hop_identity(&item);
        if self.seen(&identity).is_some() {
            return Err(SchedulerError::Duplicate {
                idempotency_key: item.idempotency_key,
            });
        }
        let retry_after = py_max(
            &self.retry_floor,
            &self.config.backpressure_retry_after_seconds,
        );
        let work_limit = py_max(&PyReal::int(0), &self.config.maximum_pending_hops);
        if py_ge(&count(self.queue.len()), &work_limit) {
            return Err(SchedulerError::Backpressure {
                reason: WORK_LIMIT,
                retry_after_seconds: retry_after,
            });
        }
        let payload_bytes = payload_size(&item.payload)?;
        let byte_limit = py_max(&PyReal::int(0), &self.config.maximum_pending_bytes);
        let total = PyReal::signed(false, (self.queued_payload_bytes + payload_bytes).into());
        if py_lt(&byte_limit, &total) {
            return Err(SchedulerError::Backpressure {
                reason: BYTE_LIMIT,
                retry_after_seconds: retry_after,
            });
        }
        self.seen_keys.push(Seen {
            identity,
            seen_at: item.enqueued_at,
            path_id: item.path_id.clone(),
        });
        let enqueued_at = item.enqueued_at;
        self.queue.push((item, payload_bytes));
        self.queued_payload_bytes += payload_bytes;
        self.evict_seen(enqueued_at);
        Ok(())
    }

    /// scheduler.py:69-84: the qos class's base (`batch` or any other class's interactive
    /// base), plus `max(0.0, now - enqueued_at) * aging_priority_per_second` times
    /// `batch_aging_multiplier` for `batch` work (`1.0` otherwise), plus
    /// `max(0.0, min(1.0, deficit_ratio)) * maximum_deficit_boost`.
    pub fn effective_priority(&self, item: &HopWorkItem, now: f64) -> Result<f64, SchedulerError> {
        let batch = item.qos_class == "batch";
        let base = if batch {
            &self.config.batch_base_priority
        } else {
            &self.config.interactive_base_priority
        };
        let age = float_max(0.0, now - item.enqueued_at);
        let one = PyReal::Float(1.0);
        let aging_multiplier = if batch {
            &self.config.batch_aging_multiplier
        } else {
            &one
        };
        let aging = py_mul(
            &py_mul(&PyReal::Float(age), &self.config.aging_priority_per_second)
                .map_err(arithmetic)?,
            aging_multiplier,
        )
        .map_err(arithmetic)?;
        let deficit = float_max(0.0, float_min(1.0, item.deficit_ratio));
        let deficit_boost = py_mul(&PyReal::Float(deficit), &self.config.maximum_deficit_boost)
            .map_err(arithmetic)?;
        let total = py_add(&py_add(base, &aging).map_err(arithmetic)?, &deficit_boost)
            .map_err(arithmetic)?;
        // The float age makes `aging`, and so the sum, a float; `float()` returns it unchanged.
        py_float(&total).map_err(arithmetic)
    }

    /// scheduler.py:86-87: `pop_batch(now, maximum_items=1).items[0]`.
    pub fn pop_next(&mut self, now: f64) -> Result<HopWorkItem, SchedulerError> {
        self.pop_batch(now, Some(&PyReal::int(1)), None, None)?
            .into_items()
            .into_iter()
            .next()
            .ok_or(SchedulerError::Index(EMPTY_HOP_QUEUE))
    }

    /// scheduler.py:89-104: remove and return the first queued item with this hop identity
    /// (Python `==`, which covers `is`; its payload bytes leave the total), as it was queued;
    /// its idempotency entry stays.
    pub fn take(&mut self, item: &HopWorkItem) -> Option<HopWorkItem> {
        let identity = hop_identity(item);
        let offset = self
            .queue
            .iter()
            .position(|(queued, _)| same_hop(&hop_identity(queued), &identity))?;
        let (queued, size) = self.queue.remove(offset);
        self.queued_payload_bytes -= size;
        Some(queued)
    }

    /// scheduler.py:106-123: remove the first queued item with this hop identity and drop its
    /// idempotency entry; `false` (nothing dropped) when none is queued.
    pub fn withdraw(&mut self, item: &HopWorkItem) -> bool {
        let identity = hop_identity(item);
        let Some(offset) = self
            .queue
            .iter()
            .position(|(queued, _)| same_hop(&hop_identity(queued), &identity))
        else {
            return false;
        };
        let (_, size) = self.queue.remove(offset);
        self.queued_payload_bytes -= size;
        if let Some(entry) = self.seen(&identity) {
            self.seen_keys.remove(entry);
        }
        true
    }

    /// scheduler.py:125-147: the anchor alone when it has no batch key, else every queued item
    /// with the anchor's key in priority order, one per request; nothing is removed.
    pub fn batch_candidates(&self, now: f64) -> Result<Vec<HopWorkItem>, SchedulerError> {
        if self.queue.is_empty() {
            return Ok(Vec::new());
        }
        let keys = self.priority_keys(now)?;
        let anchor_index = anchor(&keys);
        let offsets = match self.compatible(anchor_index) {
            None => vec![anchor_index],
            Some(compatible) => {
                let sorted = sorted_by_key(compatible, &keys)?;
                self.one_member_per_request(sorted)
            }
        };
        Ok(offsets
            .into_iter()
            .map(|offset| self.queue[offset].0.clone())
            .collect())
    }

    /// scheduler.py:149-206. IndexError `empty_hop_queue` on an empty queue. The anchor is the
    /// item with the least priority key; without a batch key it is the batch. Otherwise the
    /// queued items with its key, in key order, one per request, sliced to
    /// `max(1, maximum_items or maximum_runtime_batch_size)`; with `maximum_bytes`, items are
    /// kept while their bytes fit `max(1, maximum_bytes)`, the first always. The selected items
    /// leave the queue with their bytes; the batch carries the anchor's key and `decision`.
    pub fn pop_batch(
        &mut self,
        now: f64,
        maximum_items: Option<&PyReal>,
        maximum_bytes: Option<&PyReal>,
        decision: Option<BatchDecision>,
    ) -> Result<RuntimeBatch, SchedulerError> {
        if self.queue.is_empty() {
            return Err(SchedulerError::Index(EMPTY_HOP_QUEUE));
        }
        let maximum = py_max(
            &PyReal::int(1),
            maximum_items.unwrap_or(&self.config.maximum_runtime_batch_size),
        );
        let keys = self.priority_keys(now)?;
        let anchor_index = anchor(&keys);
        let mut selected = match self.compatible(anchor_index) {
            None => vec![anchor_index],
            Some(compatible) => {
                // The slice raises after the sort, whatever the sort's order.
                if matches!(maximum, PyReal::Float(_)) {
                    return Err(SchedulerError::Type(SLICE_INDEX_NOT_INT));
                }
                let sorted = sorted_by_key(compatible, &keys)?;
                let kept = self.one_member_per_request(sorted);
                kept.into_iter()
                    .enumerate()
                    .take_while(|(position, _)| py_lt(&count(*position), &maximum))
                    .map(|(_, offset)| offset)
                    .collect()
            }
        };
        if let Some(maximum_bytes) = maximum_bytes {
            let byte_limit = py_max(&PyReal::int(1), maximum_bytes);
            let mut bounded = Vec::new();
            let mut bounded_bytes = 0u64;
            for offset in selected {
                let size = self.queue[offset].1;
                let total = PyReal::signed(false, (bounded_bytes + size).into());
                if !bounded.is_empty() && py_lt(&byte_limit, &total) {
                    break;
                }
                bounded.push(offset);
                bounded_bytes += size;
            }
            selected = bounded;
        }
        let items: Vec<HopWorkItem> = selected
            .iter()
            .map(|offset| self.queue[*offset].0.clone())
            .collect();
        let compatibility_key = self.queue[anchor_index].0.batch_key.clone();
        let anchor_items: Vec<bool> = selected.iter().map(|o| *o == anchor_index).collect();
        let mut removal = selected;
        removal.sort_unstable_by(|a, b| b.cmp(a));
        for offset in removal {
            let (_, size) = self.queue.remove(offset);
            self.queued_payload_bytes -= size;
        }
        // The batch's key is the anchor's own key object (:201-205).
        RuntimeBatch::assembled(compatibility_key, items, decision, |k| anchor_items[k])
            .map_err(|error| SchedulerError::Contract(error.0))
    }

    /// scheduler.py:237-247: drop the path's queued items and every idempotency entry it
    /// recorded; the byte total is recomputed from what stays.
    pub fn release_path(&mut self, path_id: &str) {
        self.queue.retain(|(item, _)| item.path_id != path_id);
        self.queued_payload_bytes = self.queue.iter().map(|(_, size)| size).sum();
        self.seen_keys.retain(|seen| seen.path_id != path_id);
    }

    /// scheduler.py:249-251.
    pub fn queue_depth(&self) -> usize {
        self.queue.len()
    }

    /// scheduler.py:253-255.
    pub fn queued_path_ids(&self) -> BTreeSet<String> {
        self.queue
            .iter()
            .map(|(item, _)| item.path_id.clone())
            .collect()
    }

    /// `self._queue`: the queued items in arrival order, unchanged (read-only; the M16
    /// coordinator shows its queued REQUEST items through it).
    pub fn queued_items(&self) -> impl Iterator<Item = &HopWorkItem> {
        self.queue.iter().map(|(item, _)| item)
    }

    /// scheduler.py:257-259 `dict(Counter(placement_id))`: in first-seen order, Python-equal
    /// ids counted as one (`1 == 1.0 == True`, the first spelling kept).
    pub fn placement_depths(&self) -> Result<IdMap<u64, PyJson>, SchedulerError> {
        let mut depths = IdMap::new();
        for (item, _) in &self.queue {
            let depth = depths
                .get(&item.placement_id)
                .map_err(|_| SchedulerError::Type(UNHASHABLE_PLACEMENT_ID))?
                .copied()
                .unwrap_or(0);
            depths
                .insert(item.placement_id.clone(), depth + 1)
                .map_err(|_| SchedulerError::Type(UNHASHABLE_PLACEMENT_ID))?;
        }
        Ok(depths)
    }

    /// scheduler.py:261-263.
    pub fn queued_payload_bytes(&self) -> u64 {
        self.queued_payload_bytes
    }

    /// scheduler.py:277-279.
    pub fn seen_key_count(&self) -> usize {
        self.seen_keys.len()
    }

    /// The position of this identity's idempotency entry (a dict lookup: Python `==`).
    fn seen(&self, identity: &HopIdentity) -> Option<usize> {
        self.seen_keys
            .iter()
            .position(|seen| same_hop(&seen.identity, identity))
    }

    /// scheduler.py:208-224: each request's first offset, in the given order.
    fn one_member_per_request(&self, offsets: Vec<usize>) -> Vec<usize> {
        let mut seen = BTreeSet::new();
        offsets
            .into_iter()
            .filter(|offset| seen.insert(self.queue[*offset].0.request_id.as_str()))
            .collect()
    }

    /// Every queued item's priority key, in queue order (CPython's `min` and `sorted` evaluate
    /// each; any OverflowError aborts with nothing removed).
    fn priority_keys(&self, now: f64) -> Result<Vec<PriorityKey<'_>>, SchedulerError> {
        self.queue
            .iter()
            .enumerate()
            .map(|(offset, (item, _))| {
                Ok(PriorityKey {
                    negated_priority: -self.effective_priority(item, now)?,
                    request_id: &item.request_id,
                    path_id: &item.path_id,
                    token_index: &item.token_index,
                    hop_index: &item.hop_index,
                    offset,
                })
            })
            .collect()
    }

    /// The offsets with the anchor's batch key, or `None` for an anchor without one (:135-136,
    /// :173-174): `item.batch_key == anchor.batch_key` is true for the anchor itself (the same
    /// key object) and Python `==` for every other item's own key.
    fn compatible(&self, anchor_index: usize) -> Option<Vec<usize>> {
        let key = self.queue[anchor_index].0.batch_key.as_ref()?;
        Some(
            self.queue
                .iter()
                .enumerate()
                .filter(|(offset, (item, _))| {
                    *offset == anchor_index || item.batch_key.as_ref() == Some(key)
                })
                .map(|(offset, _)| offset)
                .collect(),
        )
    }

    /// scheduler.py:281-302 `_evict_seen`: drop inactive entries older than
    /// `max(0.0, idempotency_retention_seconds)` (strict `>`), then inactive entries
    /// oldest-first by `(seen_at, identity)` while more than `max(1,
    /// maximum_idempotency_entries)` remain. Entries of queued items are never dropped.
    fn evict_seen(&mut self, now: f64) {
        let active: Vec<HopIdentity> = self
            .queue
            .iter()
            .map(|(item, _)| hop_identity(item))
            .collect();
        let is_active = |identity: &HopIdentity| active.iter().any(|a| same_hop(a, identity));
        let retention = py_max(
            &PyReal::Float(0.0),
            &self.config.idempotency_retention_seconds,
        );
        self.seen_keys.retain(|seen| {
            is_active(&seen.identity) || !py_lt(&retention, &PyReal::Float(now - seen.seen_at))
        });
        let maximum = py_max(&PyReal::int(1), &self.config.maximum_idempotency_entries);
        // `sorted((seen_at, key) ...)` over the dict in insertion order. No `seen_at` is NaN
        // (enqueue refuses one), so the first element decides by float `<` unless equal.
        let inactive: Vec<(f64, HopIdentity)> = self
            .seen_keys
            .iter()
            .filter(|seen| !is_active(&seen.identity))
            .map(|seen| (seen.seen_at, seen.identity.clone()))
            .collect();
        let mut order: Vec<usize> = (0..inactive.len()).collect();
        list_sort(&mut order, &mut |a, b| {
            let ((a_at, a_key), (b_at, b_key)) = (&inactive[*a], &inactive[*b]);
            if a_at != b_at {
                a_at < b_at
            } else {
                hop_lt(a_key, b_key)
            }
        });
        let mut oldest = order.into_iter();
        while py_lt(&maximum, &count(self.seen_keys.len())) {
            let Some(next) = oldest.next() else {
                break;
            };
            if let Some(entry) = self.seen(&inactive[next].1) {
                self.seen_keys.remove(entry);
            }
        }
    }
}

/// `min(range(n), key=_priority_key)`: the first offset unless a later key is strictly less.
fn anchor(keys: &[PriorityKey<'_>]) -> usize {
    let mut best = 0;
    for (offset, key) in keys.iter().enumerate().skip(1) {
        if key.lt(&keys[best]) {
            best = offset;
        }
    }
    best
}

/// `sorted(offsets, key=_priority_key)` ([`list_sort`]); refused (declared V03) when two or more
/// offsets are sorted and one has a NaN priority.
fn sorted_by_key(
    mut offsets: Vec<usize>,
    keys: &[PriorityKey<'_>],
) -> Result<Vec<usize>, SchedulerError> {
    if offsets.len() > 1
        && offsets
            .iter()
            .any(|offset| keys[*offset].negated_priority.is_nan())
    {
        return Err(SchedulerError::Unsupported(UNORDERED_BATCH_PRIORITY));
    }
    list_sort(&mut offsets, &mut |a, b| keys[*a].lt(&keys[*b]));
    Ok(offsets)
}

/// CPython's `list.sort` (Objects/listobject.c @ v3.14.2: `list_sort_impl`, `count_run`,
/// `binarysort`, the powersort merge policy `found_new_run`/`powerloop`/`merge_force_collapse`,
/// `merge_at`, `merge_lo`/`merge_hi` and `gallop_left`/`gallop_right` with the adaptive
/// `min_gallop`), driven only by `lt` as CPython drives `__lt__`. On keys that are totally
/// ordered it is a stable sort; on keys that are not (a NaN that is below nothing), it leaves
/// them exactly where CPython's algorithm leaves them.
fn list_sort<T: Copy>(v: &mut [T], lt: &mut dyn FnMut(&T, &T) -> bool) {
    let n = v.len();
    if n < 2 {
        return;
    }
    let mut sort = ListSort {
        v,
        lt,
        min_gallop: MIN_GALLOP,
        pending: Vec::new(),
        listlen: n,
    };
    let minrun = merge_compute_minrun(n);
    let (mut lo, mut nremaining) = (0, n);
    while nremaining > 0 {
        let mut run = sort.count_run(lo, nremaining);
        if run < minrun {
            let force = nremaining.min(minrun);
            sort.binarysort(lo, force, run);
            run = force;
        }
        sort.found_new_run(run);
        sort.pending.push(Run {
            base: lo,
            len: run,
            power: 0,
        });
        lo += run;
        nremaining -= run;
    }
    sort.merge_force_collapse();
}

/// listobject.c `MIN_GALLOP`.
const MIN_GALLOP: usize = 7;
/// listobject.c `MAX_MINRUN`.
const MAX_MINRUN: usize = 64;

/// listobject.c `merge_compute_minrun`.
fn merge_compute_minrun(mut n: usize) -> usize {
    let mut r = 0;
    while n >= MAX_MINRUN {
        r |= n & 1;
        n >>= 1;
    }
    n + r
}

/// listobject.c `powerloop`.
fn powerloop(s1: usize, n1: usize, n2: usize, n: usize) -> u32 {
    let mut result = 0;
    let mut a = 2 * s1 + n1;
    let mut b = a + n1 + n2;
    loop {
        result += 1;
        if a >= n {
            a -= n;
            b -= n;
        } else if b >= n {
            break;
        }
        a <<= 1;
        b <<= 1;
    }
    result
}

/// A pending run (listobject.c `s_slice`).
struct Run {
    base: usize,
    len: usize,
    power: u32,
}

/// listobject.c `MergeState` over the slice being sorted.
struct ListSort<'a, 'f, T> {
    v: &'a mut [T],
    lt: &'f mut dyn FnMut(&T, &T) -> bool,
    min_gallop: usize,
    pending: Vec<Run>,
    listlen: usize,
}

impl<T: Copy> ListSort<'_, '_, T> {
    fn lt(&mut self, a: T, b: T) -> bool {
        (self.lt)(&a, &b)
    }

    /// listobject.c `binarysort`: extend the sorted `v[lo..lo+ok]` to `v[lo..lo+n]`.
    fn binarysort(&mut self, lo: usize, n: usize, ok: usize) {
        for ok in ok.max(1)..n {
            let pivot = self.v[lo + ok];
            let (mut l, mut r) = (0, ok);
            loop {
                let m = (l + r) >> 1;
                if self.lt(pivot, self.v[lo + m]) {
                    r = m;
                } else {
                    l = m + 1;
                }
                if l >= r {
                    break;
                }
            }
            self.v.copy_within(lo + l..lo + ok, lo + l + 1);
            self.v[lo + l] = pivot;
        }
    }

    /// listobject.c `count_run`: the length of the run at `v[lo..]`, made ascending (a
    /// descending run is reversed, its runs of equal elements first, so it stays stable).
    fn count_run(&mut self, lo: usize, nremaining: usize) -> usize {
        let mut n = 1;
        while n < nremaining {
            if self.lt(self.v[lo + n], self.v[lo + n - 1]) {
                break;
            }
            n += 1;
        }
        if n == nremaining {
            return n;
        }
        if n > 1 {
            if self.lt(self.v[lo], self.v[lo + n - 1]) {
                return n;
            }
            self.v[lo..lo + n].reverse();
        }
        n += 1;
        let mut neq = 0;
        while n < nremaining {
            if self.lt(self.v[lo + n], self.v[lo + n - 1]) {
                if neq > 0 {
                    neq += 1;
                    self.v[lo + n - neq..lo + n].reverse();
                    neq = 0;
                }
            } else if self.lt(self.v[lo + n - 1], self.v[lo + n]) {
                break;
            } else {
                neq += 1;
            }
            n += 1;
        }
        if neq > 0 {
            neq += 1;
            self.v[lo + n - neq..lo + n].reverse();
        }
        self.v[lo..lo + n].reverse();
        while n < nremaining {
            if self.lt(self.v[lo + n], self.v[lo + n - 1]) {
                break;
            }
            n += 1;
        }
        n
    }

    /// listobject.c `found_new_run`.
    fn found_new_run(&mut self, n2: usize) {
        if let Some(last) = self.pending.last() {
            let power = powerloop(last.base, last.len, n2, self.listlen);
            while self.pending.len() > 1 && self.pending[self.pending.len() - 2].power > power {
                self.merge_at(self.pending.len() - 2);
            }
            if let Some(last) = self.pending.last_mut() {
                last.power = power;
            }
        }
    }

    /// listobject.c `merge_force_collapse`.
    fn merge_force_collapse(&mut self) {
        while self.pending.len() > 1 {
            let mut n = self.pending.len() - 2;
            if n > 0 && self.pending[n - 1].len < self.pending[n + 1].len {
                n -= 1;
            }
            self.merge_at(n);
        }
    }

    /// listobject.c `merge_at`: merge runs `i` and `i + 1`.
    fn merge_at(&mut self, i: usize) {
        let (mut pa, mut na) = (self.pending[i].base, self.pending[i].len);
        let (pb, nb) = (self.pending[i + 1].base, self.pending[i + 1].len);
        self.pending[i].len = na + nb;
        self.pending.remove(i + 1);
        let key = self.v[pb];
        let k = gallop_right(self.lt, key, &self.v[pa..pa + na], 0);
        pa += k;
        na -= k;
        if na == 0 {
            return;
        }
        let key = self.v[pa + na - 1];
        let nb = gallop_left(self.lt, key, &self.v[pb..pb + nb], nb - 1);
        if nb == 0 {
            return;
        }
        if na <= nb {
            self.merge_lo(pa, na, pb, nb);
        } else {
            self.merge_hi(pa, na, pb, nb);
        }
    }

    /// listobject.c `merge_lo` (`na <= nb`; run A copied out).
    fn merge_lo(&mut self, pa: usize, mut na: usize, pb: usize, mut nb: usize) {
        let tmp: Vec<T> = self.v[pa..pa + na].to_vec();
        let (mut ia, mut ib, mut dest) = (0, pb, pa);
        self.v[dest] = self.v[ib];
        dest += 1;
        ib += 1;
        nb -= 1;
        let copy_b = 'merge: {
            if nb == 0 {
                break 'merge false;
            }
            if na == 1 {
                break 'merge true;
            }
            let mut min_gallop = self.min_gallop;
            loop {
                let (mut acount, mut bcount) = (0, 0);
                loop {
                    if self.lt(self.v[ib], tmp[ia]) {
                        self.v[dest] = self.v[ib];
                        dest += 1;
                        ib += 1;
                        bcount += 1;
                        acount = 0;
                        nb -= 1;
                        if nb == 0 {
                            break 'merge false;
                        }
                        if bcount >= min_gallop {
                            break;
                        }
                    } else {
                        self.v[dest] = tmp[ia];
                        dest += 1;
                        ia += 1;
                        acount += 1;
                        bcount = 0;
                        na -= 1;
                        if na == 1 {
                            break 'merge true;
                        }
                        if acount >= min_gallop {
                            break;
                        }
                    }
                }
                min_gallop += 1;
                loop {
                    min_gallop -= usize::from(min_gallop > 1);
                    self.min_gallop = min_gallop;
                    let k = gallop_right(self.lt, self.v[ib], &tmp[ia..ia + na], 0);
                    acount = k;
                    if k > 0 {
                        self.v[dest..dest + k].copy_from_slice(&tmp[ia..ia + k]);
                        dest += k;
                        ia += k;
                        na -= k;
                        if na == 1 {
                            break 'merge true;
                        }
                        if na == 0 {
                            break 'merge false;
                        }
                    }
                    self.v[dest] = self.v[ib];
                    dest += 1;
                    ib += 1;
                    nb -= 1;
                    if nb == 0 {
                        break 'merge false;
                    }
                    let k = gallop_left(self.lt, tmp[ia], &self.v[ib..ib + nb], 0);
                    bcount = k;
                    if k > 0 {
                        self.v.copy_within(ib..ib + k, dest);
                        dest += k;
                        ib += k;
                        nb -= k;
                        if nb == 0 {
                            break 'merge false;
                        }
                    }
                    self.v[dest] = tmp[ia];
                    dest += 1;
                    ia += 1;
                    na -= 1;
                    if na == 1 {
                        break 'merge true;
                    }
                    if acount < MIN_GALLOP && bcount < MIN_GALLOP {
                        break;
                    }
                }
                min_gallop += 1;
                self.min_gallop = min_gallop;
            }
        };
        if copy_b {
            // CopyB: the rest of B, then A's last element.
            self.v.copy_within(ib..ib + nb, dest);
            self.v[dest + nb] = tmp[ia];
        } else if na > 0 {
            // Succeed: what is left of A.
            self.v[dest..dest + na].copy_from_slice(&tmp[ia..ia + na]);
        }
    }

    /// listobject.c `merge_hi` (`na > nb`; run B copied out, merged from the right).
    fn merge_hi(&mut self, pa: usize, mut na: usize, pb: usize, mut nb: usize) {
        let tmp: Vec<T> = self.v[pb..pb + nb].to_vec();
        // Positions as isize: A's cursor and the destination can step past index 0.
        let base_a = pa;
        let mut dest = (pb + nb - 1) as isize;
        let mut ia = (pa + na - 1) as isize;
        let mut ib = nb as isize - 1;
        let at = |i: isize| i as usize;
        self.v[at(dest)] = self.v[at(ia)];
        dest -= 1;
        ia -= 1;
        na -= 1;
        let copy_a = 'merge: {
            if na == 0 {
                break 'merge false;
            }
            if nb == 1 {
                break 'merge true;
            }
            let mut min_gallop = self.min_gallop;
            loop {
                let (mut acount, mut bcount) = (0, 0);
                loop {
                    if self.lt(tmp[at(ib)], self.v[at(ia)]) {
                        self.v[at(dest)] = self.v[at(ia)];
                        dest -= 1;
                        ia -= 1;
                        acount += 1;
                        bcount = 0;
                        na -= 1;
                        if na == 0 {
                            break 'merge false;
                        }
                        if acount >= min_gallop {
                            break;
                        }
                    } else {
                        self.v[at(dest)] = tmp[at(ib)];
                        dest -= 1;
                        ib -= 1;
                        bcount += 1;
                        acount = 0;
                        nb -= 1;
                        if nb == 1 {
                            break 'merge true;
                        }
                        if bcount >= min_gallop {
                            break;
                        }
                    }
                }
                min_gallop += 1;
                loop {
                    min_gallop -= usize::from(min_gallop > 1);
                    self.min_gallop = min_gallop;
                    let k = na
                        - gallop_right(self.lt, tmp[at(ib)], &self.v[base_a..base_a + na], na - 1);
                    acount = k;
                    if k > 0 {
                        dest -= k as isize;
                        ia -= k as isize;
                        let from = at(ia + 1);
                        self.v.copy_within(from..from + k, at(dest + 1));
                        na -= k;
                        if na == 0 {
                            break 'merge false;
                        }
                    }
                    self.v[at(dest)] = tmp[at(ib)];
                    dest -= 1;
                    ib -= 1;
                    nb -= 1;
                    if nb == 1 {
                        break 'merge true;
                    }
                    let k = nb - gallop_left(self.lt, self.v[at(ia)], &tmp[..nb], nb - 1);
                    bcount = k;
                    if k > 0 {
                        dest -= k as isize;
                        ib -= k as isize;
                        let (to, from) = (at(dest + 1), at(ib + 1));
                        self.v[to..to + k].copy_from_slice(&tmp[from..from + k]);
                        nb -= k;
                        if nb == 1 {
                            break 'merge true;
                        }
                        if nb == 0 {
                            break 'merge false;
                        }
                    }
                    self.v[at(dest)] = self.v[at(ia)];
                    dest -= 1;
                    ia -= 1;
                    na -= 1;
                    if na == 0 {
                        break 'merge false;
                    }
                    if acount < MIN_GALLOP && bcount < MIN_GALLOP {
                        break;
                    }
                }
                min_gallop += 1;
                self.min_gallop = min_gallop;
            }
        };
        if copy_a {
            // CopyA: the rest of A slides right, then B's first element lands before it.
            let from = at(ia + 1) - na;
            self.v.copy_within(from..from + na, at(dest + 1) - na);
            self.v[at(dest) - na] = tmp[at(ib)];
        } else if nb > 0 {
            // Succeed: what is left of B.
            let to = at(dest + 1) - nb;
            self.v[to..to + nb].copy_from_slice(&tmp[..nb]);
        }
    }
}

/// listobject.c `gallop_left`: the `k` with `a[k-1] < key <= a[k]`, searched from `hint`.
fn gallop_left<T: Copy>(lt: &mut dyn FnMut(&T, &T) -> bool, key: T, a: &[T], hint: usize) -> usize {
    let n = a.len() as isize;
    let hint = hint as isize;
    let (mut lastofs, mut ofs): (isize, isize) = (0, 1);
    if lt(&a[hint as usize], &key) {
        let maxofs = n - hint;
        while ofs < maxofs {
            if lt(&a[(hint + ofs) as usize], &key) {
                lastofs = ofs;
                ofs = (ofs << 1) + 1;
            } else {
                break;
            }
        }
        ofs = ofs.min(maxofs);
        lastofs += hint;
        ofs += hint;
    } else {
        let maxofs = hint + 1;
        while ofs < maxofs {
            if lt(&a[(hint - ofs) as usize], &key) {
                break;
            }
            lastofs = ofs;
            ofs = (ofs << 1) + 1;
        }
        ofs = ofs.min(maxofs);
        let k = lastofs;
        lastofs = hint - ofs;
        ofs = hint - k;
    }
    lastofs += 1;
    while lastofs < ofs {
        let m = lastofs + ((ofs - lastofs) >> 1);
        if lt(&a[m as usize], &key) {
            lastofs = m + 1;
        } else {
            ofs = m;
        }
    }
    ofs as usize
}

/// listobject.c `gallop_right`: the `k` with `a[k-1] <= key < a[k]`, searched from `hint`.
fn gallop_right<T: Copy>(
    lt: &mut dyn FnMut(&T, &T) -> bool,
    key: T,
    a: &[T],
    hint: usize,
) -> usize {
    let n = a.len() as isize;
    let hint = hint as isize;
    let (mut lastofs, mut ofs): (isize, isize) = (0, 1);
    if lt(&key, &a[hint as usize]) {
        let maxofs = hint + 1;
        while ofs < maxofs {
            if lt(&key, &a[(hint - ofs) as usize]) {
                lastofs = ofs;
                ofs = (ofs << 1) + 1;
            } else {
                break;
            }
        }
        ofs = ofs.min(maxofs);
        let k = lastofs;
        lastofs = hint - ofs;
        ofs = hint - k;
    } else {
        let maxofs = n - hint;
        while ofs < maxofs {
            if lt(&key, &a[(hint + ofs) as usize]) {
                break;
            }
            lastofs = ofs;
            ofs = (ofs << 1) + 1;
        }
        ofs = ofs.min(maxofs);
        lastofs += hint;
        ofs += hint;
    }
    lastofs += 1;
    while lastofs < ofs {
        let m = lastofs + ((ofs - lastofs) >> 1);
        if lt(&key, &a[m as usize]) {
            ofs = m;
        } else {
            lastofs = m + 1;
        }
    }
    ofs as usize
}
