// SPDX-License-Identifier: AGPL-3.0-only
//! M16 coordinator status projection (mycelium_m16_runtime.py @ f4e366e9): `status` (:601-652),
//! `_request_projection` (:993-1018), `attach_performance_budget` (:988-991) and
//! `validate_m16_runtime_status` with `_reject_private` and `_finite` (:1043-1160; `PROTOCOL`
//! :37, `_MAX_STATUS_REQUESTS` :38, `_PRIVATE_FIELDS` :39-57, `_TOP_LEVEL_FIELDS` :58-72), and
//! the budget contract they apply, `validate_performance_budget_v3` with `_identifier`,
//! `_positive_integer` and `_nonnegative_number` (mycelium_performance_budget.py:14-53,
//! 180-239). The owner reads the document for the chat/cancel/status leg
//! (mycelium_live/router_port.py:2371, :3242) and sums its `reservation_count`s
//! (mycelium_live/supervisor.py:5309).
//!
//! The document is a [`PyJson`]: the Python value `status()` returns, after the validator's
//! `json.loads(json.dumps(document, sort_keys=True, allow_nan=False))` round trip, so every
//! dict's items follow its original keys' code-point order, ints stay ints (of any size), floats
//! keep their bits and `-0.0`, bools stay bools, and each str is what `json.loads` reads back
//! from its `\uXXXX` escapes: a high surrogate directly followed by a low one becomes the one
//! code point they encode (in values and keys; keys that then coincide keep the first one's
//! place and the last one's value), every other surrogate stays. The status is built in the oracle's order: the queued records,
//! the newest `_MAX_STATUS_REQUESTS` records, then the dict literal, whose one clock read
//! (`generated_at_monotonic_s`) precedes the ledger's `placement_status()` (an exception there
//! propagates after the read) and the projections (a `float(request.admitted_at)` OverflowError
//! propagates).
//!
//! Errors keep the oracle's exception kinds apart: [`StatusValidationError`] is each
//! `validate_m16_runtime_status` refusal (a ValueError message each, and the OverflowError a
//! budget's `float(int)` raises, which the validator does not catch, :1146-1152);
//! [`StatusError`] adds the ledger's and the projection's exceptions; [`BudgetError`] is
//! `validate_performance_budget_v3`'s PerformanceBudgetError and the TypeError and
//! OverflowError it lets through (`attach_performance_budget` catches nothing). None is a
//! [`super::M16AdmissionError`] or a transition error.
//!
//! No existing carrier fits: `protocol::public_evidence::runtime` and
//! `protocol::performance::validate_as("mycelium.performance_budget.v3", ..)` check
//! `serde_json::Value`s with one code (`public_evidence_invalid` / `performance_budget_invalid`)
//! for every refusal, return no detached document, and cannot hold what the oracle accepts and
//! keeps here (a lone surrogate in a budget's `reason`, which `json.loads` decodes and v3
//! accepts) nor raise its TypeError or OverflowError. The v3 contract is self-contained (its
//! helpers and two regular expressions only), so it is ported here over [`PyJson`].
//!
//! Values at the seam are the ones the M16 producers deliver: the clocks return floats (the
//! factory's `time.monotonic()` and `_DistributedProtocolClock`, :93-107, :1256-1258), so
//! `float(clock.now())` is the read itself; the graph's values are `execution_graph::parse`'s
//! JSON values; records, incidents and placements are the core's and the ledger's. Declared
//! limits (RU-001, tagged V03):
//! - Request ids are Rust `String`s, so none holds a lone surrogate, which the oracle would
//!   project (the gateway's ids are validated, mycelium_request_gateway/contracts.py:35-36).
//! - RecursionError: `_reject_private` / `_finite` recurse in Python and raise it, outside the
//!   JSON-safe handler, for a value nested deeper than CPython's recursion limit allows; the
//!   port raises [`StatusValidationError::Recursion`] at the same point of its walk for a node
//!   more than [`MAX_STATUS_VALUE_DEPTH`] below a top-level field, the oracle's depth when the
//!   validator is called from a fresh top-level interpreter. CPython's real boundary also
//!   depends on its caller's stack and isinstance caches: on the oracle's producer path
//!   (`build_live_m16_runtime`, then `status()`, from a fresh top level) it raises from depth
//!   989, so from 989 to 993 the port accepts what that path refuses (a declared, stack-dependent
//!   difference; workers/l1b-m16-coordinator-status-a2/evidence/oracle_recursion_summary.json).
//! - An int past CPython's 4,300-digit `str()` limit is refused by the round trip
//!   (`json.dumps` raises ValueError: "M16 runtime status is not JSON-safe"), as the oracle's
//!   validator refuses it; the oracle's `json.loads` already refuses such a graph text.
use super::{Incident, Inner, M16RuntimeCoordinator, RequestRecord};
use crate::m16_capacity::{PyInt, PyNumber};
use crate::m16_ledger::{FreeBytes, LedgerError, PlacementStatus};
use crate::m16_router_contracts::{
    PyError, PyJson, PyReal, PyStr, RouterContractError, py_eq, py_float,
};
use budgets::BudgetId;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::ops::RangeInclusive;

/// :37 `PROTOCOL`.
pub const STATUS_PROTOCOL: &str = "mycelium.concurrent_request_runtime.v1";
/// mycelium_performance_budget.py:14 `PROTOCOL_V3`.
pub const PERFORMANCE_BUDGET_V3_PROTOCOL: &str = "mycelium.performance_budget.v3";
/// :39-57 `_PRIVATE_FIELDS`.
const PRIVATE_FIELDS: [&str; 17] = [
    "prompt",
    "response",
    "prompt_text",
    "response_text",
    "token_ids",
    "tokens",
    "activation",
    "activations",
    "tensor",
    "tensors",
    "kv_content",
    "credential",
    "secret",
    "runtime_endpoint",
    "endpoint_addr",
    "private_address",
    "artifact_root",
];
/// :58-72 `_TOP_LEVEL_FIELDS`.
const TOP_LEVEL_FIELDS: [&str; 13] = [
    "protocol",
    "generated_at_monotonic_s",
    "deployment_id",
    "deployment_epoch",
    "topology_version",
    "graph_digest",
    "queue",
    "placements",
    "requests",
    "incidents",
    "batch_state",
    "claim_boundary",
    "performance_budgets",
];
/// :1069-1078.
const QUEUE_FIELDS: [&str; 8] = [
    "depth",
    "maximum_items",
    "queued_bytes",
    "maximum_bytes",
    "interactive_depth",
    "batch_depth",
    "active_request_ids",
    "maximum_active_requests",
];
/// :1081-1095.
const PLACEMENT_FIELDS: [&str; 13] = [
    "placement_id",
    "node_id",
    "memory_capacity_bytes",
    "reserved_memory_bytes",
    "free_memory_bytes",
    "kv_capacity_bytes",
    "reserved_kv_bytes",
    "free_kv_bytes",
    "workspace_capacity_bytes",
    "reserved_workspace_bytes",
    "free_workspace_bytes",
    "active_reservations",
    "maximum_reservations",
];
/// :1096-1115.
const REQUEST_FIELDS: [&str; 18] = [
    "request_id",
    "workload_profile_id",
    "qos_class",
    "phase",
    "path_id",
    "path_attempt",
    "path_manifest_digest",
    "topology_version",
    "path_state",
    "candidate_placement_ids",
    "placement_ids",
    "reservation_count",
    "admitted_at_monotonic_s",
    "queued_at_monotonic_s",
    "dispatch_at_monotonic_s",
    "terminal_at_monotonic_s",
    "queue_wait_ms",
    "terminal_state",
];
/// :1116-1124.
const INCIDENT_FIELDS: [&str; 7] = [
    "incident_id",
    "kind",
    "request_id",
    "scope",
    "state",
    "observed_at_monotonic_s",
    "retry_after_seconds",
];
/// :1137-1143.
const BATCH_STATE_FIELDS: [&str; 5] = [
    "mode",
    "maximum_runtime_batch_size",
    "observed_batches",
    "continuous_batching",
    "pipeline_overlap",
];
/// :634 `self._incidents[-256:]` and :1132 `len(document["incidents"]) > 256`.
const MAX_STATUS_INCIDENTS: usize = 256;
/// :1145 `len(document["performance_budgets"]) > 16`.
const MAX_STATUS_BUDGETS: usize = 16;
/// :1016 `(dispatch_at - record["queued_at"]) * 1_000.0`.
const MILLISECONDS_PER_SECOND: f64 = 1_000.0;
/// CPython 3.14 `sys.int_info.default_max_str_digits`: `json.dumps` of an int with more decimal
/// digits raises ValueError ("Exceeds the limit (4300 digits) for integer string conversion").
const INT_MAX_STR_DIGITS: usize = 4_300;
/// The deepest node `_reject_private` and `_finite` (:1043-1062, one Python call per level) reach
/// without RecursionError, counted from a top-level field's value (depth 0), measured on the
/// oracle under CPython 3.14.2's default recursion limit (1,000) with
/// `validate_m16_runtime_status` called from a fresh interpreter's top level: `claim_boundary`
/// inside 993 lists or one-key dicts passes and inside 994 raises, for every leaf, in three
/// fresh runs each (workers/l1b-m16-coordinator-status-a2/evidence/oracle_recursion_measure.py
/// and oracle_recursion_summary.json; the fixture's `recursion` record).
const MAX_STATUS_VALUE_DEPTH: usize = 993;
/// UTF-16 high surrogates: `json.loads` joins one directly followed by a low surrogate.
const HIGH_SURROGATES: RangeInclusive<u32> = 0xd800..=0xdbff;
/// UTF-16 low surrogates.
const LOW_SURROGATES: RangeInclusive<u32> = 0xdc00..=0xdfff;
/// The first supplementary code point, which a high and low surrogate pair offsets from.
const SUPPLEMENTARY_BASE: u32 = 0x1_0000;
/// :636.
const BATCH_MODE: &str = "concurrent_request_sequential_stage_dispatch";
/// :643-646.
const CLAIM_BOUNDARY: &str = "bounded admission, immutable paths, QoS queue, and cleanup; \
                              physical microbatch and pipeline overlap unclaimed until observed";
/// mycelium_performance_budget.py:183-191.
const V3_FIELDS: [&str; 7] = [
    "protocol",
    "budget_id",
    "profile_id",
    "evidence_scope",
    "observed_request_count",
    "dimensions",
    "overall_state",
];
/// mycelium_performance_budget.py:195-204.
const REQUIRED_DIMENSIONS: [&str; 8] = [
    "admission_latency_p95_ms",
    "queue_wait_p95_ms",
    "maximum_queue_depth",
    "completed_requests",
    "interactive_latency_regression_ratio",
    "batch_starvation_interval_ms",
    "cancellation_release_latency_ms",
    "runtime_batch_size",
];
/// mycelium_performance_budget.py:205.
const DIMENSION_FIELDS: [&str; 7] = [
    "dimension",
    "state",
    "bound",
    "observed",
    "unit",
    "evidence_digest",
    "reason",
];
/// mycelium_performance_budget.py:211.
const DIMENSION_STATES: [&str; 3] = ["met", "failed", "approved_exclusion"];
/// mycelium_performance_budget.py:15 `MAX_SAFE_INTEGER`.
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;
/// mycelium_performance_budget.py:16 `[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z`: at most 128.
const IDENTIFIER_MAXIMUM: usize = 128;
/// mycelium_performance_budget.py:216 `sha256:[0-9a-f]{64}`.
const EVIDENCE_DIGEST_PREFIX: &str = "sha256:";
const EVIDENCE_DIGEST_HEX: usize = 64;

/// `float(request.admitted_at)` raised OverflowError "int too large to convert to float"
/// (:1012).
pub const STATUS_INT_TOO_LARGE_FOR_FLOAT: &str = "m16_status_int_too_large_for_float";
/// A JSON value with no Python counterpart (a parsed graph's number always has one, so this
/// never fires).
pub const STATUS_UNREPRESENTABLE_VALUE: &str = "m16_status_unrepresentable_value";
/// The `_MAX_STATUS_REQUESTS` budget row is unavailable (the real table has it).
const POLICY_UNAVAILABLE: RouterContractError =
    RouterContractError("m16_coordinator_policy_unavailable");

/// One `validate_m16_runtime_status` refusal (:1065-1160), in its check order.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StatusValidationError {
    /// :1066-1067 not a mapping, or its keys are not `_TOP_LEVEL_FIELDS`.
    Shape,
    /// :1068-1069 `document.get("protocol") != PROTOCOL`.
    Protocol,
    /// :1070-1080 `queue` is not a mapping of exactly its eight keys.
    QueueShape,
    /// :1125-1146 placements (a non-empty list of exact mappings), requests (at most
    /// `_MAX_STATUS_REQUESTS`), incidents (at most 256), batch_state, performance_budgets (a
    /// list of at most 16).
    NestedShape,
    /// :1147-1153 a budget's PerformanceBudgetError or TypeError.
    BudgetShape,
    /// :1043-1051 a mapping with a `_PRIVATE_FIELDS` key, at any depth.
    PrivateContent,
    /// :1054-1062 a NaN or infinite float, at any depth.
    NonFinite,
    /// :1043-1062 RecursionError("maximum recursion depth exceeded"): the private walk (:1155,
    /// outside the JSON-safe handler) reached a node deeper than the oracle's measured depth
    /// (993 nested containers below a top-level field; `MAX_STATUS_VALUE_DEPTH`).
    Recursion,
    /// :1156-1159 `json.dumps` refused the document (an int past 4,300 digits).
    NotJsonSafe,
    /// A budget's `float(int)` OverflowError, which :1152 does not catch.
    BudgetIntTooLargeForFloat,
    /// The `_MAX_STATUS_REQUESTS` budget row is unavailable (the real table has it).
    PolicyUnavailable,
}

impl StatusValidationError {
    /// The specific code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Shape => "m16_runtime_status_shape_invalid",
            Self::Protocol => "m16_runtime_status_protocol_invalid",
            Self::QueueShape => "m16_runtime_status_queue_shape_invalid",
            Self::NestedShape => "m16_runtime_status_nested_shape_invalid",
            Self::BudgetShape => "m16_runtime_status_budget_shape_invalid",
            Self::PrivateContent => "m16_runtime_status_private_content",
            Self::NonFinite => "m16_runtime_status_non_finite_value",
            Self::Recursion => "m16_runtime_status_recursion_error",
            Self::NotJsonSafe => "m16_runtime_status_not_json_safe",
            Self::BudgetIntTooLargeForFloat => "m16_runtime_status_budget_int_too_large_for_float",
            Self::PolicyUnavailable => POLICY_UNAVAILABLE.0,
        }
    }

    /// The Python exception kind: `value` (ValueError), `overflow` (OverflowError), `recursion`
    /// (RecursionError) or `config`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::BudgetIntTooLargeForFloat => "overflow",
            Self::Recursion => "recursion",
            Self::PolicyUnavailable => "config",
            _ => "value",
        }
    }

    /// The exception's `str()`.
    pub fn message(&self) -> &'static str {
        match self {
            Self::Shape => "M16 runtime status shape is invalid",
            Self::Protocol => "M16 runtime status protocol is invalid",
            Self::QueueShape => "M16 runtime status queue shape is invalid",
            Self::NestedShape => "M16 runtime status nested shape is invalid",
            Self::BudgetShape => "M16 runtime status budget shape is invalid",
            Self::PrivateContent => "M16 runtime status contains private content",
            Self::NonFinite => "M16 runtime status contains non-finite value",
            Self::Recursion => "maximum recursion depth exceeded",
            Self::NotJsonSafe => "M16 runtime status is not JSON-safe",
            Self::BudgetIntTooLargeForFloat => "int too large to convert to float",
            Self::PolicyUnavailable => POLICY_UNAVAILABLE.0,
        }
    }
}

/// A `status()` refusal, by the kind of exception the oracle raises.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StatusError {
    /// `validate_m16_runtime_status`'s refusal (:652).
    Validation(StatusValidationError),
    /// The ledger's `placement_status()` exception, unchanged (:621; e.g. TypeError for
    /// placement ids `sorted` cannot order).
    Ledger(LedgerError),
    /// A Python exception of the projection ([`STATUS_INT_TOO_LARGE_FOR_FLOAT`],
    /// [`STATUS_UNREPRESENTABLE_VALUE`]).
    Python(&'static str),
    /// The `_MAX_STATUS_REQUESTS` budget row is unavailable (the real table has it).
    Config(RouterContractError),
}

impl StatusError {
    /// The specific code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Validation(error) => error.code(),
            Self::Ledger(error) => error.0,
            Self::Python(code) => code,
            Self::Config(error) => error.0,
        }
    }

    /// The exception kind: `validation`, `ledger`, `python` or `config`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Validation(_) => "validation",
            Self::Ledger(_) => "ledger",
            Self::Python(_) => "python",
            Self::Config(_) => "config",
        }
    }
}

/// A `validate_performance_budget_v3` refusal (mycelium_performance_budget.py:180-239).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BudgetError {
    /// PerformanceBudgetError("performance_budget_invalid").
    Invalid,
    /// TypeError: a `dimension` (:208, the set of names) or `state` (:211, set membership)
    /// that is a `list` or `dict` (the type name).
    Unhashable(&'static str),
    /// OverflowError: `float(int)` of a `bound` or `observed` past the double range (:48).
    IntTooLargeForFloat,
}

impl BudgetError {
    /// The specific code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Invalid => "performance_budget_invalid",
            Self::Unhashable(_) => "performance_budget_unhashable",
            Self::IntTooLargeForFloat => "performance_budget_int_too_large_for_float",
        }
    }

    /// The Python exception kind: `performance_budget`, `type` or `overflow`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Invalid => "performance_budget",
            Self::Unhashable(_) => "type",
            Self::IntTooLargeForFloat => "overflow",
        }
    }

    /// The exception's `str()` (CPython 3.14).
    pub fn message(&self) -> String {
        match self {
            Self::Invalid => "performance_budget_invalid".to_owned(),
            Self::Unhashable(name) => {
                format!("cannot use '{name}' as a set element (unhashable type: '{name}')")
            }
            Self::IntTooLargeForFloat => "int too large to convert to float".to_owned(),
        }
    }
}

/// :38 `_MAX_STATUS_REQUESTS`, from its budget row.
fn max_status_requests() -> Result<usize, RouterContractError> {
    BudgetId::PyMyceliumM16RuntimeMaxStatusRequestsL38
        .count()
        .map_err(|_refusal| POLICY_UNAVAILABLE)
        .and_then(|n| usize::try_from(n.get()).map_err(|_overflow| POLICY_UNAVAILABLE))
}

fn text(value: &str) -> PyJson {
    PyJson::str(value)
}

fn int(n: u64) -> PyJson {
    PyJson::Num(PyReal::signed(false, PyInt::from(n)))
}

fn float(x: f64) -> PyJson {
    PyJson::Num(PyReal::Float(x))
}

fn flag(value: bool) -> PyJson {
    PyJson::Num(PyReal::Bool(value))
}

fn optional_float(x: Option<f64>) -> PyJson {
    x.map_or(PyJson::None, float)
}

fn optional_text(value: Option<&str>) -> PyJson {
    value.map_or(PyJson::None, text)
}

fn number(n: &PyNumber) -> PyJson {
    match n {
        PyNumber::Int(magnitude) => PyJson::Num(PyReal::signed(false, magnitude.clone())),
        PyNumber::Float(x) => float(*x),
    }
}

fn free(n: &FreeBytes) -> PyJson {
    match n {
        FreeBytes::Int(magnitude) => PyJson::Num(PyReal::signed(false, magnitude.clone())),
        FreeBytes::NegativeInt(magnitude) => PyJson::Num(PyReal::signed(true, magnitude.clone())),
        FreeBytes::Float(x) => float(*x),
    }
}

fn dict(entries: Vec<(&str, PyJson)>) -> PyJson {
    PyJson::Dict(
        entries
            .into_iter()
            .map(|(key, value)| (PyStr::new(key), value))
            .collect(),
    )
}

/// A graph value as the Python object it is.
fn json(v: &Value) -> Result<PyJson, StatusError> {
    PyJson::from_json(v).ok_or(StatusError::Python(STATUS_UNREPRESENTABLE_VALUE))
}

/// `float(v)`'s exception as the projection propagates it.
fn projection_error(error: PyError) -> StatusError {
    match error {
        PyError::IntTooLargeForFloat
        | PyError::IntDivisionTooLargeForFloat
        | PyError::ZeroDivision => StatusError::Python(STATUS_INT_TOO_LARGE_FOR_FLOAT),
    }
}

/// One `placement_status()` entry (:321-336), in its key order.
fn placement_projection(status: &PlacementStatus) -> Result<PyJson, StatusError> {
    Ok(dict(vec![
        ("placement_id", json(&status.placement_id)?),
        ("node_id", json(&status.node_id)?),
        (
            "memory_capacity_bytes",
            PyJson::Num(PyReal::signed(false, status.memory_capacity_bytes.clone())),
        ),
        (
            "reserved_memory_bytes",
            number(&status.reserved_memory_bytes),
        ),
        ("free_memory_bytes", free(&status.free_memory_bytes)),
        (
            "kv_capacity_bytes",
            PyJson::Num(PyReal::signed(false, status.kv_capacity_bytes.clone())),
        ),
        ("reserved_kv_bytes", number(&status.reserved_kv_bytes)),
        ("free_kv_bytes", free(&status.free_kv_bytes)),
        (
            "workspace_capacity_bytes",
            PyJson::Num(PyReal::signed(
                false,
                status.workspace_capacity_bytes.clone(),
            )),
        ),
        (
            "reserved_workspace_bytes",
            number(&status.reserved_workspace_bytes),
        ),
        ("free_workspace_bytes", free(&status.free_workspace_bytes)),
        ("active_reservations", int(status.active_reservations)),
        ("maximum_reservations", int(status.maximum_reservations)),
    ]))
}

/// :993-1018 `_request_projection(record)`: both placement lists from the manifest's hops, the
/// request's live reservations counted in the ledger, `float(request.admitted_at)` (an
/// OverflowError propagates), the clock times as they were read, `None` while unset, and
/// `queue_wait_ms = (dispatch_at - queued_at) * 1_000.0` in float arithmetic once dispatched.
fn request_projection(inner: &Inner, record: &RequestRecord) -> Result<PyJson, StatusError> {
    let request = &record.request;
    let manifest = &record.manifest;
    let candidate_placement_ids = manifest
        .ordered_hops
        .iter()
        .map(|hop| json(&hop.placement_id))
        .collect::<Result<Vec<_>, _>>()?;
    let placement_ids = manifest
        .ordered_hops
        .iter()
        .map(|hop| json(&hop.placement_id))
        .collect::<Result<Vec<_>, _>>()?;
    let reservation_count = inner.ledger.request_reservations(&request.request_id).len() as u64;
    let admitted_at = py_float(&request.admitted_at).map_err(projection_error)?;
    let queue_wait_ms = record
        .dispatch_at
        .map(|dispatch_at| (dispatch_at - record.queued_at) * MILLISECONDS_PER_SECOND);
    Ok(dict(vec![
        ("request_id", text(&request.request_id)),
        ("workload_profile_id", text(&record.workload_profile_id)),
        ("qos_class", text(&request.qos_class)),
        ("phase", text(&record.phase)),
        ("path_id", text(&manifest.path_id)),
        ("path_attempt", int(manifest.path_attempt)),
        ("path_manifest_digest", text(&record.path_manifest_digest)),
        ("topology_version", json(&manifest.topology_version)?),
        ("path_state", text("locked")),
        (
            "candidate_placement_ids",
            PyJson::List(candidate_placement_ids),
        ),
        ("placement_ids", PyJson::List(placement_ids)),
        ("reservation_count", int(reservation_count)),
        ("admitted_at_monotonic_s", float(admitted_at)),
        ("queued_at_monotonic_s", float(record.queued_at)),
        (
            "dispatch_at_monotonic_s",
            optional_float(record.dispatch_at),
        ),
        (
            "terminal_at_monotonic_s",
            optional_float(record.terminal_at),
        ),
        ("queue_wait_ms", optional_float(queue_wait_ms)),
        (
            "terminal_state",
            optional_text(record.terminal_state.as_deref()),
        ),
    ]))
}

/// One incident (:1031-1041), in its key order.
fn incident_projection(incident: &Incident) -> PyJson {
    dict(vec![
        ("incident_id", text(&incident.incident_id)),
        ("kind", text(incident.kind)),
        ("request_id", text(&incident.request_id)),
        ("scope", text(incident.scope)),
        ("state", text(incident.state)),
        (
            "observed_at_monotonic_s",
            float(incident.observed_at_monotonic_s),
        ),
        (
            "retry_after_seconds",
            incident
                .retry_after_seconds
                .clone()
                .map_or(PyJson::None, PyJson::Num),
        ),
    ])
}

impl M16RuntimeCoordinator {
    /// :601-652 `status()`, under the coordinator's lock: the queue counts over the queued
    /// records (Python `str ==` on `phase` and `qos_class`), the newest `_MAX_STATUS_REQUESTS`
    /// request projections in insertion order while `queue.active_request_ids` lists every
    /// active request sorted, the last 256 incidents, the budgets as attached, one clock read,
    /// then [`validate_m16_runtime_status`].
    pub fn status(&self) -> Result<PyJson, StatusError> {
        let guard = self.lock();
        let inner = &*guard;
        let bound = max_status_requests().map_err(StatusError::Config)?;
        let queued: Vec<&RequestRecord> = inner
            .requests
            .iter()
            .map(|held| &held.record)
            .filter(|record| record.phase == "queued")
            .collect();
        let newest = inner.requests.len().saturating_sub(bound);
        let generated_at = inner.clock.now();
        let graph = inner.graph.as_value();
        let deployment_id = json(&graph["deployment_id"])?;
        let deployment_epoch = json(&graph["deployment_epoch"])?;
        let topology_version = json(&graph["topology_version"])?;
        let qos_depth = |qos: &str| {
            queued
                .iter()
                .filter(|record| record.request.qos_class == qos)
                .count() as u64
        };
        let queue = dict(vec![
            ("depth", int(inner.scheduler.queue_depth() as u64)),
            (
                "maximum_items",
                PyJson::Num(inner.config.maximum_pending_hops.clone()),
            ),
            ("queued_bytes", int(inner.scheduler.queued_payload_bytes())),
            (
                "maximum_bytes",
                PyJson::Num(inner.config.maximum_pending_bytes.clone()),
            ),
            ("interactive_depth", int(qos_depth("interactive"))),
            ("batch_depth", int(qos_depth("batch"))),
            (
                "active_request_ids",
                PyJson::List(inner.active_request_ids.iter().map(|id| text(id)).collect()),
            ),
            (
                "maximum_active_requests",
                int(inner.max_concurrent_requests),
            ),
        ]);
        let placements = inner
            .ledger
            .placement_status()
            .map_err(StatusError::Ledger)?
            .iter()
            .map(placement_projection)
            .collect::<Result<Vec<_>, _>>()?;
        let requests = inner.requests[newest..]
            .iter()
            .map(|held| request_projection(inner, &held.record))
            .collect::<Result<Vec<_>, _>>()?;
        let oldest_incident = inner.incidents.len().saturating_sub(MAX_STATUS_INCIDENTS);
        let incidents = inner.incidents[oldest_incident..]
            .iter()
            .map(incident_projection)
            .collect();
        let batch_state = dict(vec![
            ("mode", text(BATCH_MODE)),
            (
                "maximum_runtime_batch_size",
                PyJson::Num(inner.config.maximum_runtime_batch_size.clone()),
            ),
            ("observed_batches", PyJson::List(Vec::new())),
            ("continuous_batching", flag(false)),
            ("pipeline_overlap", flag(false)),
        ]);
        let document = dict(vec![
            ("protocol", text(STATUS_PROTOCOL)),
            ("generated_at_monotonic_s", float(generated_at)),
            ("deployment_id", deployment_id),
            ("deployment_epoch", deployment_epoch),
            ("topology_version", topology_version),
            ("graph_digest", text(&inner.graph_digest)),
            ("queue", queue),
            ("placements", PyJson::List(placements)),
            ("requests", PyJson::List(requests)),
            ("incidents", PyJson::List(incidents)),
            ("batch_state", batch_state),
            ("claim_boundary", text(CLAIM_BOUNDARY)),
            (
                "performance_budgets",
                PyJson::List(inner.performance_budgets.clone()),
            ),
        ]);
        validate_m16_runtime_status(&document).map_err(StatusError::Validation)
    }

    /// :988-991 `attach_performance_budget(document)`: under the lock, the budget list becomes
    /// the one validated v3 document; a refusal leaves it as it was.
    pub fn attach_performance_budget(&self, document: &PyJson) -> Result<(), BudgetError> {
        let mut guard = self.lock();
        let validated = validate_performance_budget_v3(document)?;
        guard.performance_budgets = vec![validated];
        Ok(())
    }

    /// `self._performance_budgets` (read-only).
    pub fn performance_budgets(&self) -> Vec<PyJson> {
        self.lock().performance_budgets.clone()
    }
}

/// `key == name` for a Python str key.
fn key_is(key: &PyStr, name: &str) -> bool {
    key.code_points()
        .iter()
        .copied()
        .eq(name.chars().map(u32::from))
}

/// `mapping[name]` (the keys are distinct).
fn field<'a>(entries: &'a [(PyStr, PyJson)], name: &str) -> Option<&'a PyJson> {
    entries
        .iter()
        .find(|(key, _)| key_is(key, name))
        .map(|(_, value)| value)
}

/// `set(mapping) == set(names)`.
fn same_keys(entries: &[(PyStr, PyJson)], names: &[&str]) -> bool {
    entries.len() == names.len() && names.iter().all(|name| field(entries, name).is_some())
}

/// `isinstance(v, Mapping) and set(v) == set(names)`.
fn mapping_of(v: Option<&PyJson>, names: &[&str]) -> bool {
    matches!(v, Some(PyJson::Dict(entries)) if same_keys(entries, names))
}

/// `v == name` for a str literal (no other type equals a str).
fn is_text(v: Option<&PyJson>, name: &str) -> bool {
    matches!(v, Some(PyJson::Str(value)) if key_is(value, name))
}

/// `isinstance(v, list)` with its items.
fn list_of(v: Option<&PyJson>) -> Option<&[PyJson]> {
    match v {
        Some(PyJson::List(items)) => Some(items),
        _ => None,
    }
}

/// `isinstance(v, list) and len(v) <= maximum and all(<exact mapping>)`.
fn records_of(v: Option<&PyJson>, maximum: usize, names: &[&str]) -> bool {
    list_of(v).is_some_and(|items| {
        items.len() <= maximum && items.iter().all(|item| mapping_of(Some(item), names))
    })
}

/// :1043-1051 `_reject_private` of a value `depth` levels below a top-level field: a node deeper
/// than [`MAX_STATUS_VALUE_DEPTH`] raises RecursionError where the oracle's recursion reaches
/// it; a mapping's keys first, then its values in order; list items in order.
fn reject_private(v: &PyJson, depth: usize) -> Result<(), StatusValidationError> {
    if depth > MAX_STATUS_VALUE_DEPTH {
        return Err(StatusValidationError::Recursion);
    }
    match v {
        PyJson::Dict(entries) => {
            reject_private_items(entries.iter().map(|entry| (&entry.0, &entry.1)), depth + 1)
        }
        PyJson::List(items) => items
            .iter()
            .try_for_each(|item| reject_private(item, depth + 1)),
        PyJson::None | PyJson::Num(_) | PyJson::Str(_) => Ok(()),
    }
}

/// `_reject_private` of a mapping whose values sit `depth` levels below a top-level field:
/// `_PRIVATE_FIELDS.intersection(value)` over its keys, then each value in order.
fn reject_private_items<'a>(
    entries: impl Iterator<Item = (&'a PyStr, &'a PyJson)> + Clone,
    depth: usize,
) -> Result<(), StatusValidationError> {
    if entries
        .clone()
        .any(|(key, _)| PRIVATE_FIELDS.iter().any(|name| key_is(key, name)))
    {
        return Err(StatusValidationError::PrivateContent);
    }
    let mut entries = entries;
    entries.try_for_each(|(_, item)| reject_private(item, depth))
}

/// :1054-1062 `_finite` of a value `depth` levels below a top-level field (bounded like
/// [`reject_private`], which has walked the same document first).
fn finite(v: &PyJson, depth: usize) -> Result<(), StatusValidationError> {
    if depth > MAX_STATUS_VALUE_DEPTH {
        return Err(StatusValidationError::Recursion);
    }
    match v {
        PyJson::Num(PyReal::Float(x)) if !x.is_finite() => Err(StatusValidationError::NonFinite),
        PyJson::Dict(entries) => entries
            .iter()
            .try_for_each(|(_, item)| finite(item, depth + 1)),
        PyJson::List(items) => items.iter().try_for_each(|item| finite(item, depth + 1)),
        PyJson::None | PyJson::Num(_) | PyJson::Str(_) => Ok(()),
    }
}

/// `json.loads(json.dumps(text))` of a str: `json.dumps` writes every non-ASCII code point as a
/// `\uXXXX` escape (an astral one as its surrogate pair, a surrogate as itself) and `json.loads`
/// joins a high surrogate's escape directly followed by a low one's into the code point they
/// encode, so a high surrogate directly followed by a low one becomes that one code point and
/// every other surrogate stays (`None` never: a joined pair is at most U+10FFFF).
fn decoded(text: &PyStr) -> Option<PyStr> {
    let points = text.code_points();
    let mut out = Vec::with_capacity(points.len());
    let mut at = 0;
    while let Some(&point) = points.get(at) {
        match points.get(at + 1) {
            Some(&low) if HIGH_SURROGATES.contains(&point) && LOW_SURROGATES.contains(&low) => {
                out.push(
                    SUPPLEMENTARY_BASE
                        + ((point - HIGH_SURROGATES.start()) << 10)
                        + (low - LOW_SURROGATES.start()),
                );
                at += 2;
            }
            _ => {
                out.push(point);
                at += 1;
            }
        }
    }
    PyStr::from_code_points(out)
}

/// `json.loads(json.dumps(v, sort_keys=True, allow_nan=False))` on a value of str-keyed dicts,
/// lists, strs, numbers and None: each str [`decoded`], every dict through [`round_trip_dict`],
/// everything else unchanged (a float's repr reads back as the same double, `-0.0` included);
/// `None` where `json.dumps` raises (an infinite or NaN float, an int past 4,300 digits). Reached
/// only for depths already bounded: after [`reject_private`] or a budget's closed shape.
fn round_trip(v: &PyJson) -> Option<PyJson> {
    Some(match v {
        PyJson::Num(PyReal::Float(x)) if !x.is_finite() => return None,
        PyJson::Num(PyReal::Int { magnitude, .. })
            if magnitude.to_string().len() > INT_MAX_STR_DIGITS =>
        {
            return None;
        }
        PyJson::Str(text) => PyJson::Str(decoded(text)?),
        PyJson::None | PyJson::Num(_) => v.clone(),
        PyJson::List(items) => PyJson::List(items.iter().map(round_trip).collect::<Option<_>>()?),
        PyJson::Dict(entries) => round_trip_dict(entries.iter().map(|entry| (&entry.0, &entry.1)))?,
    })
}

/// A dict's round trip: `json.dumps(sort_keys=True)` writes the items in their original keys'
/// code-point order (surrogates included), every value encoded; `json.loads` then sets each
/// [`decoded`] key in turn, so keys that decode alike keep the first one's place and take the
/// last one's value, and the decoded keys are not sorted again.
fn round_trip_dict<'a>(entries: impl Iterator<Item = (&'a PyStr, &'a PyJson)>) -> Option<PyJson> {
    let mut sorted: Vec<(&PyStr, &PyJson)> = entries.collect();
    sorted.sort_by(|a, b| a.0.cmp(b.0));
    let mut out: Vec<(PyStr, PyJson)> = Vec::with_capacity(sorted.len());
    let mut places: BTreeMap<PyStr, usize> = BTreeMap::new();
    for (key, item) in sorted {
        let key = decoded(key)?;
        let item = round_trip(item)?;
        match places.get(&key).and_then(|place| out.get_mut(*place)) {
            Some(slot) => slot.1 = item,
            None => {
                places.insert(key.clone(), out.len());
                out.push((key, item));
            }
        }
    }
    Some(PyJson::Dict(out))
}

/// :1065-1160 `validate_m16_runtime_status(document)`, in its order: the top-level shape, the
/// protocol, the queue shape, the nested shapes, each budget through
/// [`validate_performance_budget_v3`] (its PerformanceBudgetError and TypeError become
/// `BudgetShape`, its OverflowError propagates), the private-field check, the finite check,
/// then the JSON round trip of the document holding the validated budgets. A node deeper than
/// `MAX_STATUS_VALUE_DEPTH` (993) below a top-level field is RecursionError, raised by the
/// private walk where the oracle's recursion reaches it (after the budgets, before the finite
/// check).
pub fn validate_m16_runtime_status(document: &PyJson) -> Result<PyJson, StatusValidationError> {
    use StatusValidationError as E;
    let PyJson::Dict(top) = document else {
        return Err(E::Shape);
    };
    if !same_keys(top, &TOP_LEVEL_FIELDS) {
        return Err(E::Shape);
    }
    if !is_text(field(top, "protocol"), STATUS_PROTOCOL) {
        return Err(E::Protocol);
    }
    if !mapping_of(field(top, "queue"), &QUEUE_FIELDS) {
        return Err(E::QueueShape);
    }
    let max_requests = max_status_requests().map_err(|_unavailable| E::PolicyUnavailable)?;
    let nested = list_of(field(top, "placements")).is_some_and(|items| {
        !items.is_empty()
            && items
                .iter()
                .all(|item| mapping_of(Some(item), &PLACEMENT_FIELDS))
    }) && records_of(field(top, "requests"), max_requests, &REQUEST_FIELDS)
        && records_of(
            field(top, "incidents"),
            MAX_STATUS_INCIDENTS,
            &INCIDENT_FIELDS,
        )
        && mapping_of(field(top, "batch_state"), &BATCH_STATE_FIELDS)
        && list_of(field(top, "performance_budgets"))
            .is_some_and(|items| items.len() <= MAX_STATUS_BUDGETS);
    if !nested {
        return Err(E::NestedShape);
    }
    let Some(budgets) = list_of(field(top, "performance_budgets")) else {
        return Err(E::NestedShape);
    };
    let mut validated = Vec::with_capacity(budgets.len());
    for item in budgets {
        match validate_performance_budget_v3(item) {
            Ok(budget) => validated.push(budget),
            Err(BudgetError::Invalid | BudgetError::Unhashable(_)) => {
                return Err(E::BudgetShape);
            }
            Err(BudgetError::IntTooLargeForFloat) => return Err(E::BudgetIntTooLargeForFloat),
        }
    }
    // :1149-1150 `document = dict(document)` with the validated budgets in their place; read in
    // place (no deep copy precedes the depth-bounded walk).
    let budgets = PyJson::List(validated);
    let detached: Vec<(&PyStr, &PyJson)> = top
        .iter()
        .map(|(key, value)| {
            if key_is(key, "performance_budgets") {
                (key, &budgets)
            } else {
                (key, value)
            }
        })
        .collect();
    reject_private_items(detached.iter().copied(), 0)?;
    detached
        .iter()
        .copied()
        .try_for_each(|(_, value)| finite(value, 0))?;
    round_trip_dict(detached.iter().copied()).ok_or(E::NotJsonSafe)
}

/// mycelium_performance_budget.py:27-28 `_identifier`: a str of 1 to 128 code points, the first
/// an ASCII letter or digit, the rest ASCII letters, digits or `._:~-`.
fn identifier(v: Option<&PyJson>) -> bool {
    let Some(PyJson::Str(value)) = v else {
        return false;
    };
    let ascii = |point: &u32, extra: &[u8]| {
        u8::try_from(*point).is_ok_and(|b| b.is_ascii_alphanumeric() || extra.contains(&b))
    };
    let points = value.code_points();
    points.first().is_some_and(|first| ascii(first, b""))
        && points.len() <= IDENTIFIER_MAXIMUM
        && points.iter().skip(1).all(|point| ascii(point, b"._:~-"))
}

/// mycelium_performance_budget.py:40-45 `_positive_integer`: an int (not a bool) in
/// `1..=MAX_SAFE_INTEGER`.
fn positive_integer(v: Option<&PyJson>) -> bool {
    matches!(
        v,
        Some(PyJson::Num(PyReal::Int { negative: false, magnitude }))
            if *magnitude >= PyInt::from(1) && *magnitude <= PyInt::from(MAX_SAFE_INTEGER)
    )
}

/// mycelium_performance_budget.py:47-53 `_nonnegative_number`: an int or float (not a bool)
/// whose `float()` is finite and `>= 0`; `float()` of an int past the double range raises
/// OverflowError, which propagates.
fn nonnegative_number(v: Option<&PyJson>) -> Result<bool, BudgetError> {
    match v {
        Some(PyJson::Num(n @ PyReal::Int { .. })) => {
            let x = py_float(n).map_err(|_overflow| BudgetError::IntTooLargeForFloat)?;
            Ok(x.is_finite() && x >= 0.0)
        }
        Some(PyJson::Num(PyReal::Float(x))) => Ok(x.is_finite() && *x >= 0.0),
        _ => Ok(false),
    }
}

/// mycelium_performance_budget.py:215-216: a str matching `sha256:[0-9a-f]{64}` whole.
fn evidence_digest(v: Option<&PyJson>) -> bool {
    let Some(PyJson::Str(value)) = v else {
        return false;
    };
    let points = value.code_points();
    let prefix = EVIDENCE_DIGEST_PREFIX.len();
    points.len() == prefix + EVIDENCE_DIGEST_HEX
        && points
            .iter()
            .take(prefix)
            .copied()
            .eq(EVIDENCE_DIGEST_PREFIX.chars().map(u32::from))
        && points.iter().skip(prefix).all(|point| {
            char::from_u32(*point).is_some_and(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
        })
}

/// A `dimension` or `state` hashed into a set: a list or dict raises TypeError.
fn hashable(v: Option<&PyJson>) -> Result<(), BudgetError> {
    match v {
        Some(PyJson::List(_)) => Err(BudgetError::Unhashable("list")),
        Some(PyJson::Dict(_)) => Err(BudgetError::Unhashable("dict")),
        _ => Ok(()),
    }
}

/// mycelium_performance_budget.py:180-239 `validate_performance_budget_v3(document)`, in its
/// order: the key set; the protocol, ids, scope and request count; `dimensions` a list of
/// eight exact mappings whose names (hashed in order, a list or dict raising TypeError) are the
/// eight required ones; then per dimension in order its state (hashed first), bound, observed
/// (`float()` may raise OverflowError), unit, evidence digest and reason; the overall state the
/// states imply; a `runtime_batch_size` under an approved exclusion observed `== 1`; and last
/// the detached JSON round trip (keys sorted).
pub fn validate_performance_budget_v3(document: &PyJson) -> Result<PyJson, BudgetError> {
    use BudgetError::Invalid;
    let PyJson::Dict(top) = document else {
        return Err(Invalid);
    };
    if !same_keys(top, &V3_FIELDS) {
        return Err(Invalid);
    }
    if !is_text(field(top, "protocol"), PERFORMANCE_BUDGET_V3_PROTOCOL)
        || !identifier(field(top, "budget_id"))
        || !identifier(field(top, "profile_id"))
        || !is_text(field(top, "evidence_scope"), "concurrent_physical_observed")
        || !positive_integer(field(top, "observed_request_count"))
    {
        return Err(Invalid);
    }
    let Some(items) = list_of(field(top, "dimensions")) else {
        return Err(Invalid);
    };
    if items.len() != REQUIRED_DIMENSIONS.len() {
        return Err(Invalid);
    }
    let mut dimensions = Vec::with_capacity(items.len());
    for item in items {
        match item {
            PyJson::Dict(entries) if same_keys(entries, &DIMENSION_FIELDS) => {
                dimensions.push(entries.as_slice());
            }
            _ => return Err(Invalid),
        }
    }
    let mut names = BTreeSet::new();
    let mut all_text = true;
    for entries in &dimensions {
        let name = field(entries, "dimension");
        hashable(name)?;
        match name {
            Some(PyJson::Str(value)) => {
                names.insert(value.clone());
            }
            _ => all_text = false,
        }
    }
    let required: BTreeSet<PyStr> = REQUIRED_DIMENSIONS.iter().map(|n| PyStr::new(n)).collect();
    if !all_text || names != required {
        return Err(Invalid);
    }
    for entries in &dimensions {
        let state = field(entries, "state");
        hashable(state)?;
        if !DIMENSION_STATES.iter().any(|name| is_text(state, name))
            || !nonnegative_number(field(entries, "bound"))?
            || !nonnegative_number(field(entries, "observed"))?
            || !identifier(field(entries, "unit"))
            || !evidence_digest(field(entries, "evidence_digest"))
            || !matches!(field(entries, "reason"), Some(PyJson::Str(reason)) if !reason.code_points().is_empty())
        {
            return Err(Invalid);
        }
    }
    let any_state = |name: &str| {
        dimensions
            .iter()
            .any(|entries| is_text(field(entries, "state"), name))
    };
    let expected_overall = if any_state("failed") {
        "failed"
    } else if any_state("approved_exclusion") {
        "met_with_approved_exclusions"
    } else {
        "met"
    };
    if !is_text(field(top, "overall_state"), expected_overall) {
        return Err(Invalid);
    }
    let Some(batch) = dimensions
        .iter()
        .find(|entries| is_text(field(entries, "dimension"), "runtime_batch_size"))
    else {
        return Err(Invalid);
    };
    let observed_one = matches!(
        field(batch, "observed"),
        Some(PyJson::Num(observed)) if py_eq(observed, &PyReal::int(1))
    );
    if is_text(field(batch, "state"), "approved_exclusion") && !observed_one {
        return Err(Invalid);
    }
    round_trip(document).ok_or(Invalid)
}
