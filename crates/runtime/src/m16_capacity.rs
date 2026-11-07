// SPDX-License-Identifier: AGPL-3.0-only
//! Live M16 per-placement capacities: the capacity part of `build_live_m16_runtime`
//! (mycelium_m16_runtime.py:1181-1230 @ f4e366e9) over a parsed execution graph.
//! No device state, admission, lease, scheduling or release is built here.
//!
//! The product path builds this coordinator straight from the parsed graph
//! (mycelium_live/registry.py:153-167, then router_port.py:453-552, which only stores the graph);
//! no runtime-type check sits between the graph parser and these lines. So every value
//! `execution_graph::parse` carries is computed as Python computes it: a `bool` is an `int`
//! (True = 1), ints are unbounded ([`PyInt`]), floats are IEEE doubles, an int meets a float by
//! exact comparison and, for `+`, by conversion to float; `max` keeps its first argument on a tie,
//! and each output keeps the int/float type it is computed with ([`PyNumber`]). Node and
//! placement ids are dict keys: Python-equal ids are one key, and the first id seen is kept.
//! A float output is refused later, by the coordinator's ledger (mycelium_m16_runtime.py:183-193,
//! `m16_capacity_invalid`), not here.
//!
//! Refusals are the oracle's own exceptions on the same input: `m16_capacity_unhashable_node_id`
//! (:1184 `setdefault` raises TypeError for a list or dict id) and
//! `m16_capacity_int_too_large_for_float` (:1227 `workspace + kv` raises OverflowError).
//! `m16_capacity_policy_unavailable` reports an unreadable budget row. The other codes are the
//! graph parser's own for the same input and cannot occur for a [`Graph`], which only
//! `execution_graph::parse` builds (execution_graph.rs:204).
use budgets::BudgetId;
use protocol::execution_graph::Graph;
use serde_json::{Map, Number, Value};
use std::cmp::Ordering;
use std::collections::BTreeMap;
use std::fmt;
use std::str::FromStr;

/// The node's projected entry (`placement_projection.nodes[]` keyed by its string `node_id`).
pub type ProjectedNodes = BTreeMap<String, Map<String, Value>>;

/// One placement's capacity record (mycelium_m16_runtime.py:1226-1231): the dict key the source
/// stores it under and its four values, each with the Python type the source computes.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlacementCapacity {
    pub placement_id: Value,
    pub memory_capacity_bytes: PyNumber,
    pub kv_capacity_bytes: PyNumber,
    pub workspace_capacity_bytes: PyNumber,
    pub maximum_reservations: u64,
}

/// A refusal with its specific code.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CapacityError(pub &'static str);

/// A Python number as the source computes it: an `int` (a `bool` is an `int`) or a `float`.
#[derive(Clone)]
pub enum PyNumber {
    Int(PyInt),
    Float(f64),
}

/// Same Python type and same value; floats compare bit for bit (output identity, not `==`).
impl PartialEq for PyNumber {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Int(a), Self::Int(b)) => a == b,
            (Self::Float(a), Self::Float(b)) => a.to_bits() == b.to_bits(),
            _ => false,
        }
    }
}
impl Eq for PyNumber {}

impl fmt::Debug for PyNumber {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Int(n) => write!(f, "{n}"),
            Self::Float(x) => write!(f, "{x:?}"),
        }
    }
}

impl From<u64> for PyNumber {
    fn from(n: u64) -> Self {
        Self::Int(PyInt::from(n))
    }
}

/// Decimal digits held by one limb of a [`PyInt`].
const LIMB_DIGITS: usize = 9;
/// The limb base, `10^LIMB_DIGITS`.
const LIMB: u32 = 1_000_000_000;

/// A non-negative Python `int` of any size: little-endian base-`10^9` limbs, no high zero limb
/// (zero has none).
#[derive(Clone, Default, Eq, PartialEq)]
pub struct PyInt(Vec<u32>);

impl PyInt {
    fn normalized(mut limbs: Vec<u32>) -> Self {
        while limbs.last() == Some(&0) {
            limbs.pop();
        }
        Self(limbs)
    }

    fn is_zero(&self) -> bool {
        self.0.is_empty()
    }

    /// `self * factor`.
    fn mul_small(&self, factor: u64) -> Self {
        let mut limbs = Vec::with_capacity(self.0.len() + 3);
        let mut carry = 0u128;
        for limb in &self.0 {
            let product = u128::from(*limb) * u128::from(factor) + carry;
            limbs.push((product % u128::from(LIMB)) as u32);
            carry = product / u128::from(LIMB);
        }
        while carry > 0 {
            limbs.push((carry % u128::from(LIMB)) as u32);
            carry /= u128::from(LIMB);
        }
        Self::normalized(limbs)
    }

    /// `self // divisor` for a positive divisor (floor division of non-negative ints).
    fn floor_div(&self, divisor: u64) -> Self {
        let mut limbs = vec![0; self.0.len()];
        let mut remainder = 0u128;
        for (i, limb) in self.0.iter().enumerate().rev() {
            let current = remainder * u128::from(LIMB) + u128::from(*limb);
            limbs[i] = (current / u128::from(divisor)) as u32;
            remainder = current % u128::from(divisor);
        }
        Self::normalized(limbs)
    }

    /// `self + other`.
    fn add(&self, other: &Self) -> Self {
        let len = self.0.len().max(other.0.len());
        let mut limbs = Vec::with_capacity(len + 1);
        let mut carry = 0;
        for i in 0..len {
            let sum = self.0.get(i).map_or(0, |l| *l) + other.0.get(i).map_or(0, |l| *l) + carry;
            limbs.push(sum % LIMB);
            carry = sum / LIMB;
        }
        limbs.push(carry);
        Self::normalized(limbs)
    }

    /// `float(self)`: correctly rounded, ties to even; `None` where Python raises OverflowError
    /// (the value rounds past `f64::MAX`).
    fn to_f64(&self) -> Option<f64> {
        match self.to_string().parse::<f64>() {
            Ok(x) if x.is_finite() => Some(x),
            _ => None,
        }
    }

    /// Python's exact `int` versus `float` comparison (the int is never rounded); `None` for NaN.
    fn cmp_f64(&self, x: f64) -> Option<Ordering> {
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
        let whole: Self = if floor == 0.0 {
            Self::default()
        } else {
            format!("{floor:.0}").parse().ok()?
        };
        let fraction = if x > floor {
            Ordering::Less
        } else {
            Ordering::Equal
        };
        Some(self.cmp(&whole).then(fraction))
    }
}

impl Ord for PyInt {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0
            .len()
            .cmp(&other.0.len())
            .then_with(|| self.0.iter().rev().cmp(other.0.iter().rev()))
    }
}
impl PartialOrd for PyInt {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl From<u64> for PyInt {
    fn from(mut n: u64) -> Self {
        let mut limbs = Vec::new();
        while n > 0 {
            limbs.push((n % u64::from(LIMB)) as u32);
            n /= u64::from(LIMB);
        }
        Self(limbs)
    }
}

/// ASCII decimal digits, no sign.
impl FromStr for PyInt {
    type Err = CapacityError;
    fn from_str(text: &str) -> Result<Self, CapacityError> {
        if text.is_empty() || !text.bytes().all(|b| b.is_ascii_digit()) {
            return Err(CapacityError("m16_capacity_invalid_int_text"));
        }
        let limbs = text
            .as_bytes()
            .rchunks(LIMB_DIGITS)
            .map(|chunk| {
                chunk
                    .iter()
                    .fold(0, |limb, digit| limb * 10 + u32::from(digit - b'0'))
            })
            .collect();
        Ok(Self::normalized(limbs))
    }
}

impl fmt::Display for PyInt {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Some((high, rest)) = self.0.split_last() else {
            return f.write_str("0");
        };
        write!(f, "{high}")?;
        for limb in rest.iter().rev() {
            write!(f, "{limb:09}")?;
        }
        Ok(())
    }
}

impl fmt::Debug for PyInt {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(self, f)
    }
}

/// Python `a > b`.
fn greater(a: &PyNumber, b: &PyNumber) -> bool {
    match (a, b) {
        (PyNumber::Int(a), PyNumber::Int(b)) => a > b,
        (PyNumber::Float(a), PyNumber::Float(b)) => a > b,
        (PyNumber::Int(a), PyNumber::Float(b)) => a.cmp_f64(*b) == Some(Ordering::Greater),
        (PyNumber::Float(a), PyNumber::Int(b)) => b.cmp_f64(*a) == Some(Ordering::Less),
    }
}

/// Python `max(first, second)`: the first argument unless the second is greater.
fn max(first: PyNumber, second: PyNumber) -> PyNumber {
    if greater(&second, &first) {
        second
    } else {
        first
    }
}

/// Python `value * factor` with an int factor (a float converts it, exactly for these factors).
fn times(value: &PyNumber, factor: u64) -> PyNumber {
    match value {
        PyNumber::Int(n) => PyNumber::Int(n.mul_small(factor)),
        PyNumber::Float(x) => PyNumber::Float(x * factor as f64),
    }
}

/// Python `a + b`: an int meeting a float is converted with `float()` first.
fn plus(a: &PyNumber, b: &PyNumber) -> Result<PyNumber, CapacityError> {
    Ok(match (a, b) {
        (PyNumber::Int(a), PyNumber::Int(b)) => PyNumber::Int(a.add(b)),
        (PyNumber::Float(a), PyNumber::Float(b)) => PyNumber::Float(a + b),
        (PyNumber::Int(n), PyNumber::Float(x)) | (PyNumber::Float(x), PyNumber::Int(n)) => {
            PyNumber::Float(n.to_f64().ok_or(INT_TOO_LARGE_FOR_FLOAT)? + x)
        }
    })
}

const ALLOCATABLE_KEY_DEFAULT: BudgetId = BudgetId::M16LiveAllocatableKeyDefault; // :1193
const ALLOCATABLE_INVALID_DEFAULT: BudgetId = BudgetId::M16LiveAllocatableInvalidDefault; // :1195
const PER_PLACEMENT_FLOOR: BudgetId = BudgetId::M16LivePerPlacementFloor; // :1196
const KV_CONTEXT_TOKENS: BudgetId = BudgetId::M16LiveKvContextTokens; // :1223
const MAXIMUM_RESERVATIONS: BudgetId = BudgetId::PyR16M16BuilderReservations; // :1230

/// :1184 `placements_by_node.setdefault(placement.node_id, [])` raises TypeError (unhashable).
const UNHASHABLE_NODE_ID: CapacityError = CapacityError("m16_capacity_unhashable_node_id");
/// :1187 `stages_by_placement[placement.placement_id]` would raise TypeError; the parser refuses
/// such an id first (execution_graph.rs:136-148, `invalid_graph_field_type`).
const UNHASHABLE_PLACEMENT_ID: CapacityError =
    CapacityError("m16_capacity_unhashable_placement_id");
/// :1227 `workspace + kv` raises OverflowError (int too large to convert to float).
const INT_TOO_LARGE_FOR_FLOAT: CapacityError =
    CapacityError("m16_capacity_int_too_large_for_float");
const POLICY_UNAVAILABLE: CapacityError = CapacityError("m16_capacity_policy_unavailable");
// The parser's codes for the same input; a parsed Graph never carries it.
/// Non-numeric size or cost: execution_graph.rs:149-157 (validation.py:48/:71 raise TypeError).
const NOT_A_NUMBER: CapacityError = CapacityError("invalid_graph_field_type");
/// Activation size not above 0: execution_graph.rs:224-238, validation.py:48.
const INVALID_ACTIVATION: CapacityError = CapacityError("invalid_activation_bytes");
/// KV cost below 0: execution_graph.rs:273-291, validation.py:71.
const INVALID_KV_COST: CapacityError = CapacityError("invalid_kv_cost");
/// Stages not a list: execution_graph.rs:239-242, validation.py:51.
const MISSING_STAGES: CapacityError = CapacityError("missing_stages");
/// Placements not a list: execution_graph.rs:269-272, validation.py:60.
const NO_PLACEMENTS: CapacityError = CapacityError("stage_without_placements");

fn bytes(id: BudgetId) -> Result<PyInt, CapacityError> {
    id.bytes()
        .map(|bytes| PyInt::from(bytes.get()))
        .map_err(|_| POLICY_UNAVAILABLE)
}

fn count(id: BudgetId) -> Result<u64, CapacityError> {
    id.count()
        .map(budgets::Entries::get)
        .map_err(|_| POLICY_UNAVAILABLE)
}

/// A JSON number as Python's json module reads it: a literal with `.`, `e` or `E` is a `float`,
/// any other an `int`. `None` for a negative int, which [`PyInt`] does not hold.
fn json_number(number: &Number) -> Option<PyNumber> {
    let text = number.to_string();
    if text.contains(['.', 'e', 'E']) {
        return text.parse().ok().map(PyNumber::Float);
    }
    match text.strip_prefix('-') {
        Some(digits) => digits
            .bytes()
            .all(|b| b == b'0')
            .then(|| PyNumber::Int(PyInt::default())),
        None => text.parse().ok().map(PyNumber::Int),
    }
}

/// A graph size or cost as Python holds it (a `bool` is an `int`).
fn graph_number(v: &Value, negative: CapacityError) -> Result<PyNumber, CapacityError> {
    match v {
        Value::Bool(b) => Ok(PyNumber::from(u64::from(*b))),
        Value::Number(number) => json_number(number).ok_or(negative),
        _ => Err(NOT_A_NUMBER),
    }
}

/// A dict key: lists and dicts are unhashable.
fn key(v: &Value, unhashable: CapacityError) -> Result<&Value, CapacityError> {
    if v.is_array() || v.is_object() {
        Err(unhashable)
    } else {
        Ok(v)
    }
}

/// A hashable id's numeric value, if it is a number or a bool: (below zero, magnitude).
fn id_number(v: &Value) -> Option<(bool, PyNumber)> {
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
                PyNumber::Int(n) => n.is_zero(),
                PyNumber::Float(x) => *x == 0.0,
            };
            Some((negative && !zero, magnitude))
        }
        _ => None,
    }
}

/// Python `==` between two hashable ids, i.e. dict-key identity: numbers and bools by exact
/// value across int and float (1 == 1.0 == True), strings by text, `None` only to `None`.
fn same_key(a: &Value, b: &Value) -> bool {
    match (id_number(a), id_number(b)) {
        (Some((a_negative, a)), Some((b_negative, b))) => {
            a_negative == b_negative && !greater(&a, &b) && !greater(&b, &a)
        }
        (None, None) => a == b,
        _ => false,
    }
}

/// :1191-1195 the projected `fast_allocatable_bytes` if it is a positive `int` (not a `bool`),
/// else the source default.
fn allocatable(projection: Option<&Map<String, Value>>) -> Result<PyInt, CapacityError> {
    let Some(raw) = projection.and_then(|p| p.get("fast_allocatable_bytes")) else {
        return bytes(ALLOCATABLE_KEY_DEFAULT);
    };
    if let Value::Number(number) = raw
        && let Some(PyNumber::Int(n)) = json_number(number)
        && !n.is_zero()
    {
        return Ok(n);
    }
    bytes(ALLOCATABLE_INVALID_DEFAULT)
}

/// Capacities in the source's order: nodes by first placement in graph order, then each node's
/// placements in graph order.
pub fn live_placement_capacities(
    graph: &Graph,
    projected_nodes: &ProjectedNodes,
) -> Result<Vec<PlacementCapacity>, CapacityError> {
    let g = graph.as_value();
    // :1181-1188 placements_by_node (node id -> its placement ids, first-seen key order) and
    // stages_by_placement (placement id -> its stage; a later equal id overwrites the stage).
    let mut nodes: Vec<(&Value, Vec<(&Value, usize)>)> = Vec::new();
    let mut stages_by_placement: Vec<(&Value, &Value)> = Vec::new();
    for stage in g["stages"].as_array().ok_or(MISSING_STAGES)? {
        let kv_cost = &stage["stage_cost"]["kv_bytes_per_context_token"];
        for placement in stage["placements"].as_array().ok_or(NO_PLACEMENTS)? {
            let node_id = key(&placement["node_id"], UNHASHABLE_NODE_ID)?;
            let placement_id = key(&placement["placement_id"], UNHASHABLE_PLACEMENT_ID)?;
            let slot = match stages_by_placement
                .iter()
                .position(|(held, _)| same_key(held, placement_id))
            {
                Some(slot) => {
                    stages_by_placement[slot].1 = kv_cost;
                    slot
                }
                None => {
                    stages_by_placement.push((placement_id, kv_cost));
                    stages_by_placement.len() - 1
                }
            };
            match nodes.iter_mut().find(|(held, _)| same_key(held, node_id)) {
                Some((_, held)) => held.push((placement_id, slot)),
                None => nodes.push((node_id, vec![(placement_id, slot)])),
            }
        }
    }
    let activation_bytes = graph_number(&g["activation_bytes"], INVALID_ACTIVATION)?;
    let floor = bytes(PER_PLACEMENT_FLOOR)?;
    let context_tokens = count(KV_CONTEXT_TOKENS)?;
    let maximum_reservations = count(MAXIMUM_RESERVATIONS)?;
    let mut capacities: Vec<PlacementCapacity> = Vec::new();
    for (node_id, placements) in &nodes {
        // :1191 projected_nodes.get(node_id, {}): only string ids have projected entries.
        let allocatable = allocatable(node_id.as_str().and_then(|id| projected_nodes.get(id)))?;
        // :1196 per_placement = max(1_048_576, allocatable // len(placement_ids))
        let share = allocatable.floor_div(placements.len() as u64);
        let per_placement = if share > floor { share } else { floor.clone() };
        for (placement_id, slot) in placements {
            let kv_bytes_per_context_token =
                graph_number(stages_by_placement[*slot].1, INVALID_KV_COST)?;
            // :1220 minimum_workspace = max(graph.activation_bytes * 2, 1)
            let minimum_workspace = max(times(&activation_bytes, 2), PyNumber::from(1));
            // :1221 workspace = max(minimum_workspace, per_placement // 4)
            let workspace = max(minimum_workspace, PyNumber::Int(per_placement.floor_div(4)));
            // :1222-1225 kv = max(kv_bytes_per_context_token * 4_096, per_placement // 2)
            let kv = max(
                times(&kv_bytes_per_context_token, context_tokens),
                PyNumber::Int(per_placement.floor_div(2)),
            );
            // :1227 memory_capacity_bytes = max(per_placement, workspace + kv)
            let memory = max(PyNumber::Int(per_placement.clone()), plus(&workspace, &kv)?);
            let capacity = PlacementCapacity {
                placement_id: (*placement_id).clone(),
                memory_capacity_bytes: memory,
                kv_capacity_bytes: kv,
                workspace_capacity_bytes: workspace,
                maximum_reservations,
            };
            // :1226 capacities[placement_id] = {...}: an equal key keeps its place and first id.
            match capacities
                .iter_mut()
                .find(|held| same_key(&held.placement_id, placement_id))
            {
                Some(held) => {
                    *held = PlacementCapacity {
                        placement_id: held.placement_id.clone(),
                        ..capacity
                    }
                }
                None => capacities.push(capacity),
            }
        }
    }
    Ok(capacities)
}
