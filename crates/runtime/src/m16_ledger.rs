// SPDX-License-Identifier: AGPL-3.0-only
//! M16 resource ledger: `_Reservation` and `_ResourceLedgerCapacityPort`
//! (mycelium_m16_runtime.py:140-346 @ f4e366e9) with `ReservationRequest`, `ReservationResult`
//! and `ReservationCommitResult` (mycelium_router/contracts.py:186-208). It reserves, commits and
//! releases per-placement memory, KV and workspace bytes and reports them per placement. No
//! admission, scheduling, routing or recovery is done here.
//!
//! The ledger takes the parsed graph and the capacities exactly as
//! [`live_placement_capacities`](crate::m16_capacity::live_placement_capacities) builds them for
//! the coordinator (:1252-1262), and computes with Python's values at that seam: a `bool` is an
//! `int`, ints are unbounded ([`PyInt`]), floats are IEEE doubles, an int meets a float by exact
//! comparison and, for `+` and `-`, by `float()` conversion, and `sum()` is CPython 3.14's (see
//! `py_sum`). Placement ids and deployment epochs are the graph's own JSON values compared with
//! Python `==` (`1 == 1.0 == True`, `"1" != 1`); a placement id is a dict key.
//!
//! Refusals are the oracle's own exceptions on the same input: `m16_capacity_placement_mismatch`
//! and `m16_capacity_invalid` (:172-193, ValueError), `m16_ledger_unhashable_placement_id` (:198,
//! TypeError for a list or dict id), `m16_ledger_int_too_large_for_float` (OverflowError of an
//! int-to-float conversion in :228-236 or :318-333) and `m16_ledger_unorderable_placement_ids`
//! (:315, `sorted` TypeError).
use crate::m16_capacity::{PlacementCapacity, PyInt, PyNumber};
use protocol::execution_graph::Graph;
use serde_json::Value;
use std::cmp::Ordering;
use std::fmt;

/// The lease clock the ledger reads (`clock.now()`, :216 and :274).
pub trait LeaseClock {
    fn now(&self) -> f64;
}

/// The id source the ledger draws reservation ids from (`id_source.new("reservation")`, :239).
pub trait IdSource {
    fn new_id(&self, prefix: &str) -> String;
}

impl<T: LeaseClock + ?Sized> LeaseClock for &T {
    fn now(&self) -> f64 {
        (**self).now()
    }
}

impl<T: IdSource + ?Sized> IdSource for &T {
    fn new_id(&self, prefix: &str) -> String {
        (**self).new_id(prefix)
    }
}

/// A refusal with its specific code.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LedgerError(pub &'static str);

/// :172-174 the capacity ids are not the graph's placement-id set (ValueError).
const PLACEMENT_MISMATCH: LedgerError = LedgerError("m16_capacity_placement_mismatch");
/// :183-193 a capacity value that is not an int (bool excluded) above 0 (ValueError).
const CAPACITY_INVALID: LedgerError = LedgerError("m16_capacity_invalid");
/// :198 `placement_id in self._capacities` hashes the id; a list or dict raises TypeError.
const UNHASHABLE_PLACEMENT_ID: LedgerError = LedgerError("m16_ledger_unhashable_placement_id");
/// An int meeting a float in `+`, `-` or `sum()` is converted with `float()`, which raises
/// OverflowError past the double range (:228-236, :318-333).
const INT_TOO_LARGE_FOR_FLOAT: LedgerError = LedgerError("m16_ledger_int_too_large_for_float");
/// :315 `sorted(self._capacities)` raises TypeError when ids of different kinds meet (a string
/// next to a number, or `None` next to anything).
const UNORDERABLE_PLACEMENT_IDS: LedgerError = LedgerError("m16_ledger_unorderable_placement_ids");
/// The graph parser's code for an activation size that is not a number above 0
/// (execution_graph.rs:224-238); a parsed [`Graph`] never carries one.
const INVALID_ACTIVATION: LedgerError = LedgerError("invalid_activation_bytes");

/// contracts.py:186-193.
#[derive(Clone, Debug, PartialEq)]
pub struct ReservationRequest {
    pub request_id: String,
    pub path_id: String,
    pub path_attempt: u64,
    /// The graph's placement id (any hashable JSON value).
    pub placement_id: Value,
    pub kv_bytes: PyNumber,
    /// The graph's deployment epoch, kept as the value it is (`true` stays `true`).
    pub deployment_epoch: Value,
    pub lease_expires_at: f64,
}

/// contracts.py:197-202; a refusal keeps the defaults `""`, `-1` and `0.0`.
#[derive(Clone, Debug, PartialEq)]
pub struct ReservationResult {
    pub accepted: bool,
    pub reservation_id: String,
    pub reason: &'static str,
    pub deployment_epoch: Value,
    pub expires_at: f64,
}

impl ReservationResult {
    fn refused(reason: &'static str) -> Self {
        Self {
            accepted: false,
            reservation_id: String::new(),
            reason,
            deployment_epoch: Value::from(-1),
            expires_at: 0.0,
        }
    }
}

/// contracts.py:206-208.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ReservationCommitResult {
    pub accepted: bool,
    pub reason: &'static str,
}

impl ReservationCommitResult {
    fn refused(reason: &'static str) -> Self {
        Self {
            accepted: false,
            reason,
        }
    }
}

/// :140-152 `_Reservation`.
#[derive(Clone, Debug, PartialEq)]
pub struct Reservation {
    pub reservation_id: String,
    pub request_id: String,
    pub path_id: String,
    pub path_attempt: u64,
    pub placement_id: Value,
    pub deployment_epoch: Value,
    pub kv_bytes: PyNumber,
    pub workspace_bytes: PyNumber,
    pub memory_bytes: PyNumber,
    pub expires_at: f64,
    pub committed: bool,
    pub released: bool,
}

/// `capacity - reserved` (:327-333) as Python computes it: an int, or a float when the sum is a
/// float. An int falls below zero only when float sums let int reservations past the capacity
/// (an int + float check rounds) and the floats are released later.
#[derive(Clone)]
pub enum FreeBytes {
    /// An int at or above zero.
    Int(PyInt),
    /// An int below zero, by this magnitude (above zero).
    NegativeInt(PyInt),
    Float(f64),
}

/// Same Python type and same value; floats compare bit for bit (output identity, not `==`).
impl PartialEq for FreeBytes {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Int(a), Self::Int(b)) | (Self::NegativeInt(a), Self::NegativeInt(b)) => a == b,
            (Self::Float(a), Self::Float(b)) => a.to_bits() == b.to_bits(),
            _ => false,
        }
    }
}
impl Eq for FreeBytes {}

impl fmt::Debug for FreeBytes {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Int(n) => write!(f, "{n}"),
            Self::NegativeInt(n) => write!(f, "-{n}"),
            Self::Float(x) => write!(f, "{x:?}"),
        }
    }
}

/// One `placement_status()` entry (:321-336), fields in the source's order. `V` holds its two
/// graph values: owned ([`ResourceLedger::placement_status`]) or borrowed from the ledger
/// ([`ResourceLedger::placement_status_borrowed`], which copies no graph value).
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlacementStatus<V = Value> {
    pub placement_id: V,
    pub node_id: V,
    pub memory_capacity_bytes: PyInt,
    pub reserved_memory_bytes: PyNumber,
    pub free_memory_bytes: FreeBytes,
    pub kv_capacity_bytes: PyInt,
    pub reserved_kv_bytes: PyNumber,
    pub free_kv_bytes: FreeBytes,
    pub workspace_capacity_bytes: PyInt,
    pub reserved_workspace_bytes: PyNumber,
    pub free_workspace_bytes: FreeBytes,
    pub active_reservations: u64,
    pub maximum_reservations: u64,
}

/// One validated `self._capacities` entry (:194) and the node `placement_status` names for it
/// (:309-313: the graph is frozen, so the lookup is the same at construction).
struct Capacity {
    placement_id: Value,
    node_id: Value,
    memory: PyInt,
    kv: PyInt,
    workspace: PyInt,
    maximum_reservations: u64,
}

/// `_ResourceLedgerCapacityPort` (:155-346).
pub struct ResourceLedger<C, I> {
    graph: Graph,
    clock: C,
    id_source: I,
    /// `self._capacities`, in dict order.
    capacities: Vec<Capacity>,
    /// `self._reservations`, in dict order (keyed by reservation id).
    reservations: Vec<Reservation>,
}

impl<C: LeaseClock, I: IdSource> ResourceLedger<C, I> {
    /// :156-195. `placement_capacities` is the caller's mapping in its order; a list or dict id
    /// cannot be a dict key, and a later Python-equal id replaces the value and keeps the first
    /// id, as dict assignment does (live_placement_capacities never yields either).
    pub fn new(
        graph: &Graph,
        placement_capacities: &[PlacementCapacity],
        clock: C,
        id_source: I,
    ) -> Result<Self, LedgerError> {
        let mut mapping: Vec<(&Value, &PlacementCapacity)> = Vec::new();
        for capacity in placement_capacities {
            if !hashable(&capacity.placement_id) {
                return Err(UNHASHABLE_PLACEMENT_ID);
            }
            match mapping
                .iter_mut()
                .find(|(id, _)| py_eq(id, &capacity.placement_id))
            {
                Some(held) => held.1 = capacity,
                None => mapping.push((&capacity.placement_id, capacity)),
            }
        }
        // :167-172 the graph's placement ids (dict keys: the first id is kept) and, as
        // :309-313 builds it, each id's node (a later equal id's node replaces it).
        let mut placements: Vec<(&Value, &Value)> = Vec::new();
        let g = graph.as_value();
        for stage in g["stages"].as_array().into_iter().flatten() {
            for placement in stage["placements"].as_array().into_iter().flatten() {
                let id = &placement["placement_id"];
                let node = &placement["node_id"];
                match placements.iter_mut().find(|(held, _)| py_eq(held, id)) {
                    Some(held) => held.1 = node,
                    None => placements.push((id, node)),
                }
            }
        }
        // :173 set equality of two duplicate-free key sets.
        if mapping.len() != placements.len()
            || !mapping
                .iter()
                .all(|(id, _)| placements.iter().any(|(held, _)| py_eq(held, id)))
        {
            return Err(PLACEMENT_MISMATCH);
        }
        let mut capacities = Vec::with_capacity(mapping.len());
        for (placement_id, raw) in mapping {
            // :186-191 `not isinstance(value, int) or isinstance(value, bool) or value <= 0`;
            // [`PyNumber`] holds no bool and a [`PyInt`] is never below zero.
            let (Some(memory), Some(kv), Some(workspace)) = (
                positive_int(&raw.memory_capacity_bytes),
                positive_int(&raw.kv_capacity_bytes),
                positive_int(&raw.workspace_capacity_bytes),
            ) else {
                return Err(CAPACITY_INVALID);
            };
            if raw.maximum_reservations == 0 {
                return Err(CAPACITY_INVALID);
            }
            let node_id = placements
                .iter()
                .find(|(held, _)| py_eq(held, placement_id))
                .map(|(_, node)| (*node).clone())
                .ok_or(PLACEMENT_MISMATCH)?;
            capacities.push(Capacity {
                placement_id: placement_id.clone(),
                node_id,
                memory,
                kv,
                workspace,
                maximum_reservations: raw.maximum_reservations,
            });
        }
        Ok(Self {
            graph: graph.clone(),
            clock,
            id_source,
            capacities,
            reservations: Vec::new(),
        })
    }

    /// :197-258.
    pub fn reserve(
        &mut self,
        request: &ReservationRequest,
    ) -> Result<ReservationResult, LedgerError> {
        if !hashable(&request.placement_id) {
            return Err(UNHASHABLE_PLACEMENT_ID);
        }
        let Some(slot) = self
            .capacities
            .iter()
            .position(|c| py_eq(&c.placement_id, &request.placement_id))
        else {
            return Ok(ReservationResult::refused("unknown_placement"));
        };
        // :200-224 the first live (unreleased) record of this request/path/attempt/placement.
        if let Some(existing) = self.reservations.iter().find(|item| {
            !item.released
                && item.request_id == request.request_id
                && item.path_id == request.path_id
                && item.path_attempt == request.path_attempt
                && py_eq(&item.placement_id, &request.placement_id)
        }) {
            if !py_eq(&existing.deployment_epoch, &request.deployment_epoch)
                || !number_eq(&existing.kv_bytes, &request.kv_bytes)
                || existing.expires_at <= self.clock.now()
            {
                return Ok(ReservationResult::refused("conflicting_reservation"));
            }
            return Ok(ReservationResult {
                accepted: true,
                reservation_id: existing.reservation_id.clone(),
                reason: "",
                deployment_epoch: existing.deployment_epoch.clone(),
                expires_at: existing.expires_at,
            });
        }
        let capacity = &self.capacities[slot];
        let active = self.active_for(&capacity.placement_id);
        // :227-228
        let workspace_bytes = workspace_bytes(&self.graph)?;
        let memory_bytes = add(&request.kv_bytes, &workspace_bytes)?;
        // :229-238, evaluated left to right and only as far as Python's `or` goes.
        let unavailable = active.len() as u64 >= capacity.maximum_reservations
            || greater(
                &add(
                    &py_sum(active.iter().map(|r| &r.kv_bytes))?,
                    &request.kv_bytes,
                )?,
                &capacity.kv,
            )
            || greater(
                &add(
                    &py_sum(active.iter().map(|r| &r.workspace_bytes))?,
                    &workspace_bytes,
                )?,
                &capacity.workspace,
            )
            || greater(
                &add(
                    &py_sum(active.iter().map(|r| &r.memory_bytes))?,
                    &memory_bytes,
                )?,
                &capacity.memory,
            );
        if unavailable {
            return Ok(ReservationResult::refused("resource_unavailable"));
        }
        // :239-252 `self._reservations[reservation_id] = record`: an id the source repeats
        // replaces the earlier record in its place.
        let reservation_id = self.id_source.new_id("reservation");
        let record = Reservation {
            reservation_id: reservation_id.clone(),
            request_id: request.request_id.clone(),
            path_id: request.path_id.clone(),
            path_attempt: request.path_attempt,
            placement_id: request.placement_id.clone(),
            deployment_epoch: request.deployment_epoch.clone(),
            kv_bytes: request.kv_bytes.clone(),
            workspace_bytes,
            memory_bytes,
            expires_at: request.lease_expires_at,
            committed: false,
            released: false,
        };
        match self
            .reservations
            .iter_mut()
            .find(|held| held.reservation_id == reservation_id)
        {
            Some(held) => *held = record,
            None => self.reservations.push(record),
        }
        Ok(ReservationResult {
            accepted: true,
            reservation_id,
            reason: "",
            deployment_epoch: request.deployment_epoch.clone(),
            expires_at: request.lease_expires_at,
        })
    }

    /// :260-284, all or nothing.
    pub fn commit(
        &mut self,
        reservation_ids: &[String],
        deployment_epoch: &Value,
    ) -> ReservationCommitResult {
        let distinct = reservation_ids
            .iter()
            .enumerate()
            .all(|(i, id)| !reservation_ids[..i].contains(id));
        if reservation_ids.is_empty() || !distinct {
            return ReservationCommitResult::refused("reservation_set_invalid");
        }
        let mut slots = Vec::with_capacity(reservation_ids.len());
        for id in reservation_ids {
            match self.position(id) {
                Some(slot) => slots.push(slot),
                None => return ReservationCommitResult::refused("unknown_reservation"),
            }
        }
        for &slot in &slots {
            let item = &self.reservations[slot];
            if item.released
                || !py_eq(&item.deployment_epoch, deployment_epoch)
                || item.expires_at <= self.clock.now()
            {
                return ReservationCommitResult::refused("reservation_not_live");
            }
        }
        if slots.iter().any(|&slot| self.reservations[slot].committed) {
            return ReservationCommitResult::refused("reservation_already_committed");
        }
        for slot in slots {
            self.reservations[slot].committed = true;
        }
        ReservationCommitResult {
            accepted: true,
            reason: "",
        }
    }

    /// :286-290; unknown ids are skipped.
    pub fn release(&mut self, reservation_ids: &[String]) {
        for id in reservation_ids {
            if let Some(slot) = self.position(id) {
                self.reservations[slot].released = true;
            }
        }
    }

    /// :292-299.
    pub fn release_request(&mut self, request_id: &str) {
        for item in &mut self.reservations {
            if item.request_id == request_id && !item.released {
                item.released = true;
            }
        }
    }

    /// :301-306 the request's unreleased records (expired or committed ones included), in
    /// reservation order.
    pub fn request_reservations(&self, request_id: &str) -> Vec<Reservation> {
        self.reservations
            .iter()
            .filter(|item| item.request_id == request_id && !item.released)
            .cloned()
            .collect()
    }

    /// :308-338, sorted by placement id.
    pub fn placement_status(&self) -> Result<Vec<PlacementStatus>, LedgerError> {
        Ok(self
            .placement_status_borrowed()?
            .into_iter()
            .map(|status| PlacementStatus {
                placement_id: status.placement_id.clone(),
                node_id: status.node_id.clone(),
                memory_capacity_bytes: status.memory_capacity_bytes,
                reserved_memory_bytes: status.reserved_memory_bytes,
                free_memory_bytes: status.free_memory_bytes,
                kv_capacity_bytes: status.kv_capacity_bytes,
                reserved_kv_bytes: status.reserved_kv_bytes,
                free_kv_bytes: status.free_kv_bytes,
                workspace_capacity_bytes: status.workspace_capacity_bytes,
                reserved_workspace_bytes: status.reserved_workspace_bytes,
                free_workspace_bytes: status.free_workspace_bytes,
                active_reservations: status.active_reservations,
                maximum_reservations: status.maximum_reservations,
            })
            .collect())
    }

    /// [`Self::placement_status`] with each placement's id and node borrowed from the ledger:
    /// no graph value is copied or dropped, so a caller may convert one of any depth with its
    /// own bounded walk (the status projection does).
    pub fn placement_status_borrowed(&self) -> Result<Vec<PlacementStatus<&Value>>, LedgerError> {
        let mut output = Vec::with_capacity(self.capacities.len());
        for slot in sorted_slots(&self.capacities)? {
            let capacity = &self.capacities[slot];
            let active = self.active_for(&capacity.placement_id);
            let reserved_memory = py_sum(active.iter().map(|r| &r.memory_bytes))?;
            let reserved_kv = py_sum(active.iter().map(|r| &r.kv_bytes))?;
            let reserved_workspace = py_sum(active.iter().map(|r| &r.workspace_bytes))?;
            output.push(PlacementStatus {
                placement_id: &capacity.placement_id,
                node_id: &capacity.node_id,
                memory_capacity_bytes: capacity.memory.clone(),
                free_memory_bytes: subtract(&capacity.memory, &reserved_memory)?,
                reserved_memory_bytes: reserved_memory,
                kv_capacity_bytes: capacity.kv.clone(),
                free_kv_bytes: subtract(&capacity.kv, &reserved_kv)?,
                reserved_kv_bytes: reserved_kv,
                workspace_capacity_bytes: capacity.workspace.clone(),
                free_workspace_bytes: subtract(&capacity.workspace, &reserved_workspace)?,
                reserved_workspace_bytes: reserved_workspace,
                active_reservations: active.len() as u64,
                maximum_reservations: capacity.maximum_reservations,
            });
        }
        Ok(output)
    }

    /// :340-345 every unreleased record on the placement, in reservation order.
    fn active_for(&self, placement_id: &Value) -> Vec<&Reservation> {
        self.reservations
            .iter()
            .filter(|item| py_eq(&item.placement_id, placement_id) && !item.released)
            .collect()
    }

    fn position(&self, reservation_id: &str) -> Option<usize> {
        self.reservations
            .iter()
            .position(|item| item.reservation_id == reservation_id)
    }
}

/// :227 `graph.activation_bytes * 2` (a `bool` is an `int`; a float stays a float).
fn workspace_bytes(graph: &Graph) -> Result<PyNumber, LedgerError> {
    match &graph.as_value()["activation_bytes"] {
        Value::Bool(b) => Ok(PyNumber::from(2 * u64::from(*b))),
        Value::Number(number) => {
            let text = number.to_string();
            if text.contains(['.', 'e', 'E']) {
                let x: f64 = text.parse().map_err(|_| INVALID_ACTIVATION)?;
                Ok(PyNumber::Float(x * 2.0))
            } else {
                // A negative int is not above 0; the parser refuses it.
                let n: PyInt = text.parse().map_err(|_| INVALID_ACTIVATION)?;
                Ok(PyNumber::Int(int_add(&n, &n)))
            }
        }
        _ => Err(INVALID_ACTIVATION),
    }
}

fn positive_int(value: &PyNumber) -> Option<PyInt> {
    match value {
        PyNumber::Int(n) if *n > PyInt::default() => Some(n.clone()),
        _ => None,
    }
}

/// A list or dict cannot be a dict key.
fn hashable(v: &Value) -> bool {
    !v.is_array() && !v.is_object()
}

/// The decimal digits of `n`, most significant first (`0` for zero).
fn decimal(n: &PyInt) -> Vec<u8> {
    n.to_string().bytes().map(|b| b - b'0').collect()
}

/// The [`PyInt`] of decimal digits, most significant first.
fn from_decimal(digits: &[u8]) -> PyInt {
    let text: String = digits.iter().map(|d| char::from(b'0' + d)).collect();
    // ASCII digits, never empty: [`PyInt`]'s parser accepts every such text.
    text.parse().expect("decimal digits")
}

/// Python `a + b` for ints.
fn int_add(a: &PyInt, b: &PyInt) -> PyInt {
    let (a, b) = (decimal(a), decimal(b));
    let mut sum = Vec::with_capacity(a.len().max(b.len()) + 1);
    let mut carry = 0;
    let (mut i, mut j) = (a.len(), b.len());
    while i > 0 || j > 0 || carry > 0 {
        let x = if i > 0 { a[i - 1] } else { 0 };
        let y = if j > 0 { b[j - 1] } else { 0 };
        let d = x + y + carry;
        sum.push(d % 10);
        carry = d / 10;
        i = i.saturating_sub(1);
        j = j.saturating_sub(1);
    }
    sum.reverse();
    from_decimal(&sum)
}

/// Python `a - b` for ints with `a >= b`.
fn int_sub(a: &PyInt, b: &PyInt) -> PyInt {
    let (a, b) = (decimal(a), decimal(b));
    let mut difference = Vec::with_capacity(a.len());
    let mut borrow = 0;
    let mut j = b.len();
    for &x in a.iter().rev() {
        let y = if j > 0 { b[j - 1] } else { 0 };
        j = j.saturating_sub(1);
        let (d, next) = if x >= y + borrow {
            (x - y - borrow, 0)
        } else {
            (x + 10 - y - borrow, 1)
        };
        difference.push(d);
        borrow = next;
    }
    difference.reverse();
    from_decimal(&difference)
}

/// `float(n)`: correctly rounded, ties to even; `None` where Python raises OverflowError.
fn to_f64(n: &PyInt) -> Option<f64> {
    match n.to_string().parse::<f64>() {
        Ok(x) if x.is_finite() => Some(x),
        _ => None,
    }
}

fn float(n: &PyInt) -> Result<f64, LedgerError> {
    to_f64(n).ok_or(INT_TOO_LARGE_FOR_FLOAT)
}

/// Python's exact `int` versus `float` comparison (the int is never rounded); `None` for NaN.
fn int_cmp_f64(n: &PyInt, x: f64) -> Option<Ordering> {
    if x.is_nan() {
        return None;
    }
    if x == f64::INFINITY {
        return Some(Ordering::Less);
    }
    if x < 0.0 {
        return Some(Ordering::Greater);
    }
    let floor = x.floor();
    // A finite, non-negative, integral double prints as its exact decimal digits.
    let whole: PyInt = if floor == 0.0 {
        PyInt::default()
    } else {
        format!("{floor:.0}")
            .parse()
            .expect("integral double digits")
    };
    let fraction = if x > floor {
        Ordering::Less
    } else {
        Ordering::Equal
    };
    Some(n.cmp(&whole).then(fraction))
}

/// Python `a + b`: an int meeting a float is converted with `float()` first.
fn add(a: &PyNumber, b: &PyNumber) -> Result<PyNumber, LedgerError> {
    Ok(match (a, b) {
        (PyNumber::Int(a), PyNumber::Int(b)) => PyNumber::Int(int_add(a, b)),
        (PyNumber::Float(a), PyNumber::Float(b)) => PyNumber::Float(a + b),
        (PyNumber::Int(n), PyNumber::Float(x)) => PyNumber::Float(float(n)? + x),
        (PyNumber::Float(x), PyNumber::Int(n)) => PyNumber::Float(x + float(n)?),
    })
}

/// Python `capacity - reserved` for an int capacity.
fn subtract(capacity: &PyInt, reserved: &PyNumber) -> Result<FreeBytes, LedgerError> {
    Ok(match reserved {
        PyNumber::Int(r) if capacity >= r => FreeBytes::Int(int_sub(capacity, r)),
        PyNumber::Int(r) => FreeBytes::NegativeInt(int_sub(r, capacity)),
        PyNumber::Float(x) => FreeBytes::Float(float(capacity)? - x),
    })
}

/// Python `a > capacity` for an int capacity.
fn greater(a: &PyNumber, capacity: &PyInt) -> bool {
    match a {
        PyNumber::Int(a) => a > capacity,
        PyNumber::Float(x) => int_cmp_f64(capacity, *x) == Some(Ordering::Less),
    }
}

/// Python `a == b` for numbers.
fn number_eq(a: &PyNumber, b: &PyNumber) -> bool {
    match (a, b) {
        (PyNumber::Int(a), PyNumber::Int(b)) => a == b,
        (PyNumber::Float(a), PyNumber::Float(b)) => a == b,
        (PyNumber::Int(n), PyNumber::Float(x)) | (PyNumber::Float(x), PyNumber::Int(n)) => {
            int_cmp_f64(n, *x) == Some(Ordering::Equal)
        }
    }
}

/// The largest int CPython's `sum()` accumulates in a machine word (`PY_SSIZE_T_MAX`, 64-bit).
const WORD_MAX: u64 = i64::MAX as u64;

/// CPython 3.14's `sum()` over ints and floats (Python/bltinmodule.c `builtin_sum_impl`, v3.14.2;
/// the oracle's CI interpreter). Ints accumulate exactly in a machine word while the total fits;
/// a float switches to Neumaier-compensated double accumulation (`cs_add`), where every later int
/// is added as `float(int)` (OverflowError past the double range); an int total past the word
/// continues with plain `+` to the end, without compensation.
fn py_sum<'a>(items: impl IntoIterator<Item = &'a PyNumber>) -> Result<PyNumber, LedgerError> {
    enum Sum {
        Word(u64),
        Compensated { hi: f64, lo: f64 },
        General(PyNumber),
    }
    let mut sum = Sum::Word(0);
    for item in items {
        sum = match sum {
            Sum::Word(total) => match item {
                PyNumber::Int(n) if *n <= PyInt::from(WORD_MAX - total) => {
                    let b: u64 = n.to_string().parse().expect("a word-sized int");
                    Sum::Word(total + b)
                }
                _ => match add(&PyNumber::from(total), item)? {
                    PyNumber::Float(x) => Sum::Compensated { hi: x, lo: 0.0 },
                    int => Sum::General(int),
                },
            },
            Sum::Compensated { hi, lo } => {
                let x = match item {
                    PyNumber::Float(x) => *x,
                    PyNumber::Int(n) => float(n)?,
                };
                let t = hi + x;
                let lo = if hi.abs() >= x.abs() {
                    lo + ((hi - t) + x)
                } else {
                    lo + ((x - t) + hi)
                };
                Sum::Compensated { hi: t, lo }
            }
            Sum::General(total) => Sum::General(add(&total, item)?),
        };
    }
    Ok(match sum {
        Sum::Word(total) => PyNumber::from(total),
        // cs_to_double: a zero or non-finite compensation is not added.
        Sum::Compensated { hi, lo } if lo != 0.0 && lo.is_finite() => PyNumber::Float(hi + lo),
        Sum::Compensated { hi, .. } => PyNumber::Float(hi),
        Sum::General(total) => total,
    })
}

/// A JSON number or bool as Python holds it: (below zero, magnitude); `None` for other values.
fn number_parts(v: &Value) -> Option<(bool, PyNumber)> {
    match v {
        Value::Bool(b) => Some((false, PyNumber::from(u64::from(*b)))),
        Value::Number(number) => {
            let text = number.to_string();
            let (negative, magnitude) = text
                .strip_prefix('-')
                .map_or((false, text.as_str()), |m| (true, m));
            let magnitude = if magnitude.contains(['.', 'e', 'E']) {
                PyNumber::Float(magnitude.parse().ok()?)
            } else {
                PyNumber::Int(magnitude.parse().ok()?)
            };
            let zero = match &magnitude {
                PyNumber::Int(n) => *n == PyInt::default(),
                PyNumber::Float(x) => *x == 0.0,
            };
            Some((negative && !zero, magnitude))
        }
        _ => None,
    }
}

/// Python `<` between two numbers, exact across int and float; `None` for NaN.
fn numeric_cmp(a: &(bool, PyNumber), b: &(bool, PyNumber)) -> Option<Ordering> {
    match (a.0, b.0) {
        (true, false) => Some(Ordering::Less),
        (false, true) => Some(Ordering::Greater),
        (negative, _) => {
            let order = match (&a.1, &b.1) {
                (PyNumber::Int(x), PyNumber::Int(y)) => Some(x.cmp(y)),
                (PyNumber::Float(x), PyNumber::Float(y)) => x.partial_cmp(y),
                (PyNumber::Int(x), PyNumber::Float(y)) => int_cmp_f64(x, *y),
                (PyNumber::Float(x), PyNumber::Int(y)) => int_cmp_f64(y, *x).map(Ordering::reverse),
            }?;
            Some(if negative { order.reverse() } else { order })
        }
    }
}

/// Python `a == b` between JSON values: numbers and bools by exact value across int and float
/// (1 == 1.0 == True), strings by text, `None` only to `None`, lists and dicts item by item.
fn py_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| py_eq(x, y))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(key, x)| b.get(key).is_some_and(|y| py_eq(x, y)))
        }
        _ => match (number_parts(a), number_parts(b)) {
            (Some(x), Some(y)) => numeric_cmp(&x, &y) == Some(Ordering::Equal),
            (None, None) => a == b,
            _ => false,
        },
    }
}

/// :315 `sorted(self._capacities)`: the capacity slots in ascending id order. Python's `<`
/// orders strings by code point (UTF-8 byte order) and numbers by exact value; a sort that
/// succeeds compares every adjacent pair, so two ids of different kinds (a string and a number,
/// or `None` and anything) always raise TypeError.
fn sorted_slots(capacities: &[Capacity]) -> Result<Vec<usize>, LedgerError> {
    enum Key<'a> {
        Text(&'a str),
        Number((bool, PyNumber)),
    }
    let keys: Vec<Option<Key>> = capacities
        .iter()
        .map(|c| match &c.placement_id {
            Value::String(text) => Some(Key::Text(text)),
            other => number_parts(other).map(Key::Number),
        })
        .collect();
    if keys.len() > 1 {
        let texts = keys
            .iter()
            .filter(|k| matches!(k, Some(Key::Text(_))))
            .count();
        let numbers = keys
            .iter()
            .filter(|k| matches!(k, Some(Key::Number(_))))
            .count();
        if texts != keys.len() && numbers != keys.len() {
            return Err(UNORDERABLE_PLACEMENT_IDS);
        }
    }
    let mut slots: Vec<usize> = (0..capacities.len()).collect();
    slots.sort_by(|&a, &b| match (&keys[a], &keys[b]) {
        (Some(Key::Text(x)), Some(Key::Text(y))) => x.cmp(y),
        (Some(Key::Number(x)), Some(Key::Number(y))) => {
            numeric_cmp(x, y).unwrap_or(Ordering::Equal)
        }
        _ => Ordering::Equal,
    });
    Ok(slots)
}
