// SPDX-License-Identifier: AGPL-3.0-only
//! M16 runtime coordinator core: `M16AdmissionError` (mycelium_m16_runtime.py:75-81 @ f4e366e9),
//! `_digest` and `path_manifest_document_digest` (:116-138) over `execution_graph_to_dict`,
//! `path_manifest_to_dict` and `_edge_to_dict` (mycelium_router/serialization.py:87-166,
//! 214-254), and `M16RuntimeCoordinator` construction (:351-404), `admit` (:406-497),
//! `next_dispatch` (:499-526), `mark_phase` (:528-537), `complete` (:539-565),
//! `retire_dispatch_slot` (:567-579), `cancel` (:581-599), the accessors `phase`,
//! `terminal_state`, `request_context`, `route_identity` and `path_manifest` (:654-707) and
//! `_record_incident` (:1020-1041), composed over the merged [`ResourceLedger`],
//! [`RouteScorer`], [`RoutePolicy`], [`ProgressivePathBuilder`] and [`HopScheduler`]. The owner
//! admits one chat request (mycelium_live/router_port.py:776, mycelium_live/registry.py:381),
//! dispatches it, moves it through its phases, ends it with `complete` or `cancel`
//! (registry.py:406) and hands `route_identity` / `path_manifest` to the physical route. No
//! status projection (`status`, `_request_projection`, `attach_performance_budget`,
//! `validate_m16_runtime_status`), recovery successor (:709-988) or registry factory
//! (`build_live_m16_runtime`, :1163-1262) is here.
//!
//! Errors keep the oracle's exception kinds apart ([`CoordinatorError`]): the stable public
//! [`M16AdmissionError`] codes, the coordinator's own ValueError codes
//! (`m16_workload_profiles_invalid`, `invalid_max_concurrent_requests`,
//! `m16_phase_transition_invalid`, `m16_completion_invalid`), and the exceptions of the composed
//! parts and of the digest that the oracle lets propagate unchanged (the ledger's capacity
//! refusals at construction, the path builder's non-`RoutingError` refusals, the scheduler's
//! non-`BackpressureError` refusals and `json.dumps`'s ValueError). `admit` turns only a
//! `RoutingError` into `resource_unavailable` (with an incident) and only a `BackpressureError`
//! into `queue_full` (releasing the request's reservations, with an incident). What the oracle
//! does not undo stays: a digest or enqueue error after `lock` leaves the committed reservations
//! in place and records nothing.
//!
//! Clocks and ids: `clock` and `lease_clock` are separate objects unless `lease_clock` is
//! omitted, when it is the very same clock (:373); the ledger reads the lease clock; the ledger
//! and the path builder share the one id source (:383-393). Every `now()` in the oracle's
//! methods is a separate read here, in the same order: `advance` and `lock` each read the clock
//! and then the lease clock per call (:430-441), `next_dispatch` reads the clock for `pop_next`,
//! the lease clock once per live reservation until one has expired (:507-509, `any`
//! short-circuits), and the clock again for the terminal or dispatch time; an incident reads the
//! clock once (:1038).
//!
//! Locking: the oracle serialises every public method on one `RLock` (:83-89, :382). Here every
//! public method takes `&self` and holds one internal [`Mutex`] for the whole call, so the
//! coordinator is `Send + Sync` (the cancellation owner thread of :543-547 is a real caller); the
//! composed parts stay `&mut self` behind it. `next_dispatch`'s recursion (:520) is a loop with
//! the same effects. A Python exception releases the `RLock` and leaves the state as it is; a
//! poisoned mutex is entered the same way.
//!
//! Values at the seam are the ones the M16 producers deliver (factory :1163-1262, router_port.py
//! :742-790, the gateway's RequestContext mycelium_request_gateway/backend.py:204-239): the graph
//! is `execution_graph::parse`'s [`Graph`], the request a [`RequestContext`], the config a
//! [`RouterConfig`] of [`PyReal`]s, and the clocks return floats. Declared limits (RU-001, tagged
//! V03):
//! - `excluded_placements` is a set by type (`&[Value]`, its elements; Python-equal elements
//!   are one). A non-frozenset container, which the oracle refuses with the same
//!   `invalid_excluded_placements` (:413-416), is not representable; router_port.py:771-775
//!   raises that code itself before `admit`.
//! - `workload_profiles` is a `str -> str` mapping by type; a non-`str` profile value, which
//!   the oracle refuses with `m16_workload_profiles_invalid` (:364-368), is not representable
//!   (the factory's values are `"interactive"` / `"batch"`, :1240-1251).
//! - `next_dispatch` loops where the oracle recurses (:520), so a run of more consecutive
//!   expired queued requests than CPython's recursion limit allows (each level is two Python
//!   frames, `_synchronized`'s wrapper and the method) does not raise RecursionError here. A
//!   queued request holds a live reservation on every placement of its path, and the factory
//!   allows at most 64 per placement (:1231), so the factory's coordinator never queues that
//!   many.
//! - Strings are Rust `String`s and the graph's values `serde_json` values, so no string can
//!   hold a lone surrogate, which `.encode("utf-8")` would refuse with UnicodeEncodeError
//!   (:117-123): a graph carrying one is refused at JSON decode before a [`Graph`] exists, and
//!   the M16 request ids are the gateway's validated ids
//!   (mycelium_request_gateway/contracts.py:35-36, service.py:467-469).
//!
//! The canonical writer ([`canonical_json`]) is this module's own. Main's
//! `protocol::canonical` writes the same bytes for every key order, string, int, bool and null,
//! but its float repr takes Rust's shortest digits, which break an exact tie between two
//! shortest candidates upward where CPython's `float.__repr__` (dtoa.c mode 0) takes the even
//! last digit: `1059438285926254.25` is `1059438285926254.2` in Python and
//! `1059438285926254.3` there (49 of 226,434 sampled doubles differ, every one such a tie). A
//! reservation expiry is a clock reading plus the lease, so a tie reaches the path digest (a
//! Unix-aligned lease near 1.73e9 with a fraction that is an odd multiple of 2**-8, e.g.
//! `1730000000.00390625`), and the route identity would then differ from the oracle's. This
//! writer matches `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False,
//! allow_nan=False)` on every `serde_json` value: keys sorted by code point (UTF-8 byte order),
//! floats as `float.__repr__` (a literal with `.`, `e` or `E` is read as the double `json.loads`
//! makes, so `1e400` is inf and refused), ints of any size exact and the raw `-0` as `0`,
//! booleans as `true`/`false`, `"`, `\`, `\b`, `\f`, `\n`, `\r`, `\t` and the other C0
//! controls escaped, everything else raw.
use crate::m16_capacity::{PlacementCapacity, PyInt};
use crate::m16_hop_scheduler::{HopScheduler, SchedulerError};
use crate::m16_ledger::{
    IdSource, LeaseClock, LedgerError, PlacementStatus, Reservation, ReservationCommitResult,
    ReservationRequest, ReservationResult, ResourceLedger,
};
use crate::m16_path_builder::{
    CapacityPort, PathBuilderError, ProgressivePathBuilder, RoutePolicy,
};
use crate::m16_route_scorer::RouteScorer;
use crate::m16_router_contracts::{
    DeviceState, HopPayload, HopWorkItem, IdMap, IdSet, PathManifest, PyJson, PyReal,
    RequestContext, RouterConfig, RouterContractError,
};
use budgets::BudgetId;
use protocol::execution_graph::Graph;
use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

/// :75-81 `M16AdmissionError(code, retry_after_seconds=None)`, a RuntimeError subclass: the
/// stable public admission failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct M16AdmissionError {
    pub code: &'static str,
    /// `queue_full` carries the scheduler's `retry_after_seconds` (a Python number, as
    /// `max(0.001, backpressure_retry_after_seconds)` returns it); every other code `None`.
    pub retry_after_seconds: Option<PyReal>,
}

/// :413-416.
pub const INVALID_EXCLUDED_PLACEMENTS: &str = "invalid_excluded_placements";
/// :417-419.
pub const WORKLOAD_NOT_QUALIFIED: &str = "workload_not_qualified";
/// :420-421.
pub const DUPLICATE_REQUEST_ID: &str = "duplicate_request_id";
/// :442-449 a `RoutingError` while building or locking the path.
pub const RESOURCE_UNAVAILABLE: &str = "resource_unavailable";
/// :468-480 a `BackpressureError` at enqueue.
pub const QUEUE_FULL: &str = "queue_full";
/// :660-668, :683-685, :704-706.
pub const REQUEST_NOT_ADMITTED: &str = "request_not_admitted";
/// :364-368 (ValueError).
pub const WORKLOAD_PROFILES_INVALID: &str = "m16_workload_profiles_invalid";
/// :375-380 (ValueError).
pub const INVALID_MAX_CONCURRENT_REQUESTS: &str = "invalid_max_concurrent_requests";
/// :530-536 (ValueError).
pub const PHASE_TRANSITION_INVALID: &str = "m16_phase_transition_invalid";
/// :556-561 (ValueError).
pub const COMPLETION_INVALID: &str = "m16_completion_invalid";
/// :459 `manifest.ordered_hops[0]` of a path with no hop (IndexError; a locked path has one hop
/// per stage and a parsed graph at least one stage).
pub const EMPTY_PATH: &str = "m16_coordinator_empty_path";
/// :506 `self._requests[item.request_id]` for a queued item with no record (KeyError; every
/// queued item's request is recorded in the same `admit` call that queued it).
pub const UNKNOWN_QUEUED_REQUEST: &str = "m16_coordinator_unknown_queued_request";
/// A budget row the coordinator reads (:362, :378) is unavailable (the real table has them).
const POLICY_UNAVAILABLE: RouterContractError =
    RouterContractError("m16_coordinator_policy_unavailable");
/// A manifest hop placement id that is no Python value (a parsed graph's JSON number always
/// is one, so this never fires).
pub const UNREPRESENTABLE_PLACEMENT_ID: &str = "m16_coordinator_unrepresentable_placement_id";

/// `json.dumps(..., allow_nan=False)` refused a value (ValueError "Out of range float values are
/// not JSON compliant"): a float that is NaN or infinite, in the graph (a `1e400` literal) or as
/// a hop's reservation expiry.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DigestError {
    OutOfRangeFloat,
}

impl DigestError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::OutOfRangeFloat => "m16_digest_out_of_range_float",
        }
    }
}

/// A coordinator call's refusal, by the kind of exception the oracle raises.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CoordinatorError {
    /// `M16AdmissionError` (:75-81).
    Admission(M16AdmissionError),
    /// The coordinator's own ValueError codes.
    Value(&'static str),
    /// The ledger's exception, unchanged (its capacity checks at construction, :383-388).
    Ledger(LedgerError),
    /// A path builder exception other than `RoutingError`, unchanged (:424-441).
    PathBuilder(PathBuilderError),
    /// A scheduler exception other than `BackpressureError`, unchanged (:468, :506).
    Scheduler(SchedulerError),
    /// `json.dumps`'s ValueError while digesting the graph (:399) or the path (:450).
    Digest(DigestError),
    /// A default or bound unavailable from its budget row (`HopScheduler::new`'s retry floor,
    /// the concurrency default and bounds); the oracle's literals cannot fail, and the real
    /// budget table always has the rows.
    Config(RouterContractError),
    /// A Python IndexError or KeyError of these lines (see [`EMPTY_PATH`] and
    /// [`UNKNOWN_QUEUED_REQUEST`]).
    Python(&'static str),
}

impl CoordinatorError {
    /// The specific code.
    pub fn code(&self) -> &str {
        match self {
            Self::Admission(error) => error.code,
            Self::Value(code) | Self::Python(code) => code,
            Self::Ledger(error) => error.0,
            Self::PathBuilder(error) => error.code(),
            Self::Scheduler(error) => error.code(),
            Self::Digest(error) => error.code(),
            Self::Config(error) => error.0,
        }
    }

    /// The exception kind: `admission`, `value`, `ledger`, `path_builder`, `scheduler`,
    /// `digest`, `config` or `python`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Admission(_) => "admission",
            Self::Value(_) => "value",
            Self::Ledger(_) => "ledger",
            Self::PathBuilder(_) => "path_builder",
            Self::Scheduler(_) => "scheduler",
            Self::Digest(_) => "digest",
            Self::Config(_) => "config",
            Self::Python(_) => "python",
        }
    }
}

fn admission(code: &'static str) -> CoordinatorError {
    CoordinatorError::Admission(M16AdmissionError {
        code,
        retry_after_seconds: None,
    })
}

fn not_admitted() -> M16AdmissionError {
    M16AdmissionError {
        code: REQUEST_NOT_ADMITTED,
        retry_after_seconds: None,
    }
}

/// :117-123 `json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
/// allow_nan=False)` (see the module documentation).
pub fn canonical_json(document: &Value) -> Result<String, DigestError> {
    let mut out = String::new();
    write_json(&mut out, document)?;
    Ok(out)
}

/// :116-124 `_digest(document)`: `"sha256:" + sha256(canonical_json(document).encode("utf-8"))`.
pub fn digest_document(document: &Value) -> Result<String, DigestError> {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let text = canonical_json(document)?;
    let mut out = String::from("sha256:");
    for byte in Sha256::digest(text.as_bytes()) {
        out.push(char::from(HEX[usize::from(byte >> 4)]));
        out.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    Ok(out)
}

fn write_json(out: &mut String, value: &Value) -> Result<(), DigestError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => write_number(out, number)?,
        Value::String(text) => write_str(out, text),
        Value::Array(items) => {
            out.push('[');
            for (k, item) in items.iter().enumerate() {
                if k > 0 {
                    out.push(',');
                }
                write_json(out, item)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            // A str key's code-point order is its UTF-8 byte order.
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (k, key) in keys.into_iter().enumerate() {
                if k > 0 {
                    out.push(',');
                }
                write_str(out, key);
                out.push(':');
                if let Some(item) = map.get(key) {
                    write_json(out, item)?;
                }
            }
            out.push('}');
        }
    }
    Ok(())
}

/// A JSON number as `json.loads` reads it and `json.dumps` writes it back: a literal with `.`,
/// `e` or `E` is a float (`float.__repr__`; inf or NaN refused), any other an int, printed
/// exactly (the raw `-0` is the int 0).
fn write_number(out: &mut String, number: &Number) -> Result<(), DigestError> {
    let literal = number.to_string();
    if literal.contains(['.', 'e', 'E']) {
        // A JSON float literal always parses (an out-of-range one to an infinity).
        let value: f64 = literal.parse().map_err(|_| DigestError::OutOfRangeFloat)?;
        out.push_str(&float_repr(value)?);
    } else if literal == "-0" {
        out.push('0');
    } else {
        out.push_str(&literal);
    }
    Ok(())
}

/// `json.encoder.py_encode_basestring` / `c_encode_basestring` (ensure_ascii=False).
fn write_str(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if u32::from(c) < 0x20 => out.push_str(&format!("\\u{:04x}", u32::from(c))),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// The digits and decimal exponent of Rust's `{:e}` text of a positive double.
fn scientific(text: &str) -> (Vec<u8>, i32) {
    let (mantissa, exponent) = text.split_once('e').unwrap_or((text, ""));
    let digits = mantissa
        .bytes()
        .filter(u8::is_ascii_digit)
        .map(|b| b - b'0')
        .collect();
    let (negative, magnitude) = exponent
        .strip_prefix('-')
        .map_or((false, exponent), |rest| (true, rest));
    let value = magnitude
        .bytes()
        .filter(u8::is_ascii_digit)
        .fold(0i32, |acc, b| acc * 10 + i32::from(b - b'0'));
    (digits, if negative { -value } else { value })
}

/// When `digits` (the shortest digits of `magnitude`, Rust's choice) sits on an exact tie with
/// the other shortest candidate and has an odd last digit, the even candidate, provided it reads
/// back as the same double (dtoa.c mode 0 rounds a half-way last digit to even).
fn even_tie(magnitude: f64, digits: &[u8], exponent: i32) -> Option<Vec<u8>> {
    let n = digits.len();
    if digits.last().is_none_or(|last| last % 2 == 0) {
        return None;
    }
    // The exact decimal expansion: a double has at most 767 significant digits.
    let (exact, exact_exponent) = scientific(&format!("{magnitude:.800e}"));
    let significant = exact.iter().rposition(|d| *d != 0).map_or(0, |p| p + 1);
    if exact_exponent != exponent || significant != n + 1 || exact.get(n) != Some(&5) {
        return None;
    }
    let lower = exact[..n].to_vec();
    let mut upper = lower.clone();
    let mut carried = true;
    for d in upper.iter_mut().rev() {
        if *d == 9 {
            *d = 0;
        } else {
            *d += 1;
            carried = false;
            break;
        }
    }
    let other = if carried {
        return None;
    } else if digits == upper.as_slice() {
        lower
    } else if digits == lower.as_slice() {
        upper
    } else {
        return None;
    };
    let text: String = other.iter().map(|d| char::from(b'0' + d)).collect();
    let reread: Option<f64> = format!("0.{text}e{}", exponent + 1).parse().ok();
    (reread == Some(magnitude)).then_some(other)
}

/// `float.__repr__` (CPython 3.14, `float_repr_style == 'short'`): the shortest digits that read
/// back as the value, the nearest of them, an exact tie to the even last digit; fixed notation
/// when the decimal exponent is in `-4..16`, else `d.ddde+XX` with at least two exponent
/// digits; inf and NaN are refused (allow_nan=False).
fn float_repr(value: f64) -> Result<String, DigestError> {
    if !value.is_finite() {
        return Err(DigestError::OutOfRangeFloat);
    }
    let sign = if value.is_sign_negative() { "-" } else { "" };
    if value == 0.0 {
        return Ok(format!("{sign}0.0"));
    }
    let magnitude = value.abs();
    let (shortest, exponent) = scientific(&format!("{magnitude:e}"));
    let digits = even_tie(magnitude, &shortest, exponent).unwrap_or(shortest);
    let digits: String = digits.iter().map(|d| char::from(b'0' + d)).collect();
    let body = if (-4..16).contains(&exponent) {
        if exponent >= 0 {
            let whole = exponent.unsigned_abs() as usize + 1;
            if digits.len() <= whole {
                format!("{digits}{}.0", "0".repeat(whole - digits.len()))
            } else {
                format!("{}.{}", &digits[..whole], &digits[whole..])
            }
        } else {
            let zeros = (exponent.unsigned_abs() - 1) as usize;
            format!("0.{}{digits}", "0".repeat(zeros))
        }
    } else {
        let mantissa = if digits.len() == 1 {
            digits
        } else {
            format!("{}.{}", &digits[..1], &digits[1..])
        };
        let exponent_sign = if exponent < 0 { '-' } else { '+' };
        format!("{mantissa}e{exponent_sign}{:02}", exponent.unsigned_abs())
    };
    Ok(format!("{sign}{body}"))
}

/// serialization.py:87-148 `execution_graph_to_dict(graph)`. `validate_execution_graph` (:88)
/// holds for every parsed [`Graph`], and the parser keeps exactly the fields this dict emits
/// (execution_graph.rs `parse`: the fifteen graph fields, per stage its id, range, roles as
/// `tuple()` yields them, cost and placements with `lifecycle_state` defaulting to `"ACTIVE"`,
/// and each edge's four fields, `_edge_to_dict` :248-254), so the document is the graph's value.
pub fn execution_graph_to_dict(graph: &Graph) -> &Value {
    graph.as_value()
}

/// The graph digest the coordinator keeps (:399).
pub fn execution_graph_digest(graph: &Graph) -> Result<String, DigestError> {
    digest_document(execution_graph_to_dict(graph))
}

/// serialization.py:229-245 `path_manifest_to_dict(manifest)`. A hop's reservation expiry is a
/// float; a NaN or infinite one has no JSON number, so it is refused here with the ValueError
/// `json.dumps(allow_nan=False)` raises for it (the only uses are the digest and the recorded
/// manifests, whose digest succeeded).
pub fn path_manifest_to_dict(manifest: &PathManifest) -> Result<Value, DigestError> {
    let mut hops = Vec::with_capacity(manifest.ordered_hops.len());
    for hop in &manifest.ordered_hops {
        let expires_at =
            Number::from_f64(hop.reservation_expires_at).ok_or(DigestError::OutOfRangeFloat)?;
        let mut document = Map::new();
        document.insert("stage_id".to_owned(), hop.stage_id.clone());
        document.insert("placement_id".to_owned(), hop.placement_id.clone());
        document.insert(
            "reservation_id".to_owned(),
            Value::String(hop.reservation_id.clone()),
        );
        document.insert(
            "reservation_expires_at".to_owned(),
            Value::Number(expires_at),
        );
        document.insert(
            "reservation_epoch".to_owned(),
            hop.reservation_epoch.clone(),
        );
        hops.push(Value::Object(document));
    }
    let mut document = Map::new();
    document.insert(
        "protocol".to_owned(),
        Value::String(manifest.protocol.clone()),
    );
    document.insert(
        "path_id".to_owned(),
        Value::String(manifest.path_id.clone()),
    );
    document.insert(
        "path_attempt".to_owned(),
        Value::from(manifest.path_attempt),
    );
    document.insert(
        "request_id".to_owned(),
        Value::String(manifest.request_id.clone()),
    );
    document.insert("deployment_id".to_owned(), manifest.deployment_id.clone());
    document.insert(
        "deployment_epoch".to_owned(),
        manifest.deployment_epoch.clone(),
    );
    document.insert(
        "topology_version".to_owned(),
        manifest.topology_version.clone(),
    );
    document.insert(
        "manifest_digest".to_owned(),
        manifest.manifest_digest.clone(),
    );
    document.insert("ordered_hops".to_owned(), Value::Array(hops));
    document.insert(
        "loopback_edge_id".to_owned(),
        manifest.loopback_edge_id.clone(),
    );
    Ok(Value::Object(document))
}

/// :127-138 `path_manifest_document_digest(manifest)`.
pub fn path_manifest_document_digest(manifest: &PathManifest) -> Result<String, DigestError> {
    digest_document(&path_manifest_to_dict(manifest)?)
}

/// A clock the coordinator, the ledger and the builder's callers share (`clock.now()`).
pub type SharedClock = Arc<dyn LeaseClock + Send + Sync>;
/// The id source the ledger and the builder share (`id_source.new(prefix)`).
pub type SharedIdSource = Arc<dyn IdSource + Send + Sync>;

/// The one clock object, handed to the ledger.
#[derive(Clone)]
struct ClockHandle(SharedClock);

impl LeaseClock for ClockHandle {
    fn now(&self) -> f64 {
        self.0.now()
    }
}

/// The one id source object, handed to the ledger and the builder.
#[derive(Clone)]
struct IdHandle(SharedIdSource);

impl IdSource for IdHandle {
    fn new_id(&self, prefix: &str) -> String {
        self.0.new_id(prefix)
    }
}

type Ledger = ResourceLedger<ClockHandle, IdHandle>;

/// The coordinator's one ledger as the builder's capacity port. The builder is the oracle's
/// stateless composition `ProgressivePathBuilder(policy=RoutePolicy(RouteScorer(config)),
/// capacity=ledger, id_source=id_source)` with no lease provider (:389-393); it is assembled
/// over a borrow of the one ledger for each build, because its optional provider type is not
/// `Send`. It reads no clock and no id when assembled, and every reserve, release and commit
/// reaches the same ledger.
struct LedgerPort<'a>(&'a mut Ledger);

impl CapacityPort for LedgerPort<'_> {
    fn reserve(&mut self, request: &ReservationRequest) -> Result<ReservationResult, LedgerError> {
        self.0.reserve(request)
    }

    fn release(&mut self, reservation_ids: &[String]) {
        self.0.release(reservation_ids);
    }

    fn commit(
        &mut self,
        reservation_ids: &[String],
        deployment_epoch: &Value,
    ) -> ReservationCommitResult {
        self.0.commit(reservation_ids, deployment_epoch)
    }
}

/// `M16RuntimeCoordinator(...)`'s keyword arguments (:351-362).
pub struct CoordinatorInputs {
    pub graph: Graph,
    pub device_states: IdMap<DeviceState>,
    /// `placement_capacities` in the mapping's order, as the ledger takes it.
    pub placement_capacities: Vec<PlacementCapacity>,
    pub workload_profiles: BTreeMap<String, String>,
    pub clock: SharedClock,
    pub id_source: SharedIdSource,
    pub config: RouterConfig,
    /// `None` (the default) is the very same clock object.
    pub lease_clock: Option<SharedClock>,
    /// `None` (the default) is the int `1` (:362, its budget row).
    pub max_concurrent_requests: Option<PyReal>,
}

impl CoordinatorInputs {
    /// The required arguments, with `lease_clock=None` and `max_concurrent_requests=1`.
    pub fn new(
        graph: Graph,
        device_states: IdMap<DeviceState>,
        placement_capacities: Vec<PlacementCapacity>,
        workload_profiles: BTreeMap<String, String>,
        clock: SharedClock,
        id_source: SharedIdSource,
        config: RouterConfig,
    ) -> Self {
        Self {
            graph,
            device_states,
            placement_capacities,
            workload_profiles,
            clock,
            id_source,
            config,
            lease_clock: None,
            max_concurrent_requests: None,
        }
    }
}

/// One request record (:482-496). `recovery_successor` is `None` throughout these methods; the
/// recovery successor step carries it.
#[derive(Clone, Debug, PartialEq)]
pub struct RequestRecord {
    pub request: RequestContext,
    pub request_attempt: u64,
    pub workload_profile_id: String,
    pub manifest: PathManifest,
    pub path_manifest_digest: String,
    pub recovery_source_fenced: bool,
    pub recovery_attempts: u64,
    pub phase: String,
    pub queued_at: f64,
    pub dispatch_at: Option<f64>,
    pub terminal_at: Option<f64>,
    pub terminal_state: Option<String>,
}

/// One incident (:1031-1041).
#[derive(Clone, Debug, PartialEq)]
pub struct Incident {
    /// `m16-incident-<n>`, `n` counting from 1.
    pub incident_id: String,
    pub kind: &'static str,
    pub request_id: String,
    pub scope: &'static str,
    pub state: &'static str,
    /// `float(self._clock.now())`.
    pub observed_at_monotonic_s: f64,
    pub retry_after_seconds: Option<PyReal>,
}

/// :674-695 `route_identity(request_id)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RouteIdentity {
    pub request_id: String,
    pub request_attempt: u64,
    pub path_id: String,
    pub path_attempt: u64,
    pub path_manifest_digest: String,
    /// The graph's `deployment_id`.
    pub deployment_id: Value,
    /// The manifest's `deployment_epoch`.
    pub deployment_epoch: Value,
    /// The manifest's `topology_version`.
    pub topology_generation: Value,
}

struct Record {
    record: RequestRecord,
    /// `path_manifest_to_dict(record["manifest"])`, built once for the digest; the manifest is
    /// frozen, so every later call returns an equal dict (:697-707).
    document: Value,
}

struct Inner {
    graph: Graph,
    states: IdMap<DeviceState>,
    profiles: BTreeMap<String, String>,
    clock: SharedClock,
    lease_clock: SharedClock,
    id_source: IdHandle,
    max_concurrent_requests: u64,
    ledger: Ledger,
    policy: RoutePolicy,
    scheduler: HopScheduler,
    /// `self._requests` in insertion order, with an index by request id.
    requests: Vec<Record>,
    index: BTreeMap<String, usize>,
    active_request_ids: BTreeSet<String>,
    incidents: Vec<Incident>,
    incident_sequence: u64,
    graph_digest: String,
}

/// `M16RuntimeCoordinator` (:348-707, :1020-1041).
pub struct M16RuntimeCoordinator {
    inner: Mutex<Inner>,
}

/// A count from its budget row.
fn count(id: BudgetId) -> Result<u64, CoordinatorError> {
    id.count()
        .map(|n| n.get())
        .map_err(|_refusal| CoordinatorError::Config(POLICY_UNAVAILABLE))
}

/// :375-380 `isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 64`: the int in
/// `floor..=ceiling` (the budget rows of :378's bounds), else `None`.
fn concurrency(value: &PyReal, floor: u64, ceiling: u64) -> Option<u64> {
    let PyReal::Int {
        negative: false,
        magnitude,
    } = value
    else {
        return None;
    };
    (floor..=ceiling).find(|n| PyInt::from(*n) == *magnitude)
}

/// `min(floats)`: the first unless a later one compares strictly below it.
fn float_min(values: impl IntoIterator<Item = f64>) -> Option<f64> {
    values.into_iter().fold(None, |held, value| match held {
        Some(held) if value < held => Some(value),
        Some(held) => Some(held),
        None => Some(value),
    })
}

impl Inner {
    fn record(&self, request_id: &str) -> Option<&Record> {
        self.index
            .get(request_id)
            .and_then(|position| self.requests.get(*position))
    }

    fn record_mut(&mut self, request_id: &str) -> Option<&mut Record> {
        self.index
            .get(request_id)
            .and_then(|position| self.requests.get_mut(*position))
    }

    /// :1020-1041.
    fn record_incident(
        &mut self,
        kind: &'static str,
        request_id: &str,
        scope: &'static str,
        state: &'static str,
        retry_after_seconds: Option<PyReal>,
    ) {
        self.incident_sequence += 1;
        let incident_id = format!("m16-incident-{}", self.incident_sequence);
        let observed_at_monotonic_s = self.clock.now();
        self.incidents.push(Incident {
            incident_id,
            kind,
            request_id: request_id.to_owned(),
            scope,
            state,
            observed_at_monotonic_s,
            retry_after_seconds,
        });
    }

    /// :423-441: start, advance until complete, lock.
    fn build_path(
        &mut self,
        request: &RequestContext,
        excluded_placements: IdSet,
    ) -> Result<PathManifest, PathBuilderError> {
        let mut builder = ProgressivePathBuilder::new(
            self.policy.clone(),
            LedgerPort(&mut self.ledger),
            self.id_source.clone(),
            None,
        );
        let mut build = builder.start(
            request,
            &self.graph,
            0,
            excluded_placements,
            IdSet::new(),
            IdSet::new(),
        );
        while !builder.is_complete(&build) {
            let now = self.clock.now();
            let lease_now = self.lease_clock.now();
            build = builder.advance(&build, &self.states, now, Some(lease_now))?;
        }
        let now = self.clock.now();
        let lease_now = self.lease_clock.now();
        builder.lock(&build, Some(now), Some(lease_now))
    }
}

impl M16RuntimeCoordinator {
    /// :351-400, in its order: the workload profiles (`m16_workload_profiles_invalid` when
    /// empty or a value is neither `"interactive"` nor `"batch"`), `max_concurrent_requests`
    /// (`invalid_max_concurrent_requests` unless an int, not a bool, in `1..=64`), the ledger
    /// over the lease clock and the id source (its capacity refusals propagate), the route
    /// policy and the hop scheduler over `config`, and last the graph digest (its ValueError
    /// propagates).
    pub fn new(inputs: CoordinatorInputs) -> Result<Self, CoordinatorError> {
        let CoordinatorInputs {
            graph,
            device_states,
            placement_capacities,
            workload_profiles,
            clock,
            id_source,
            config,
            lease_clock,
            max_concurrent_requests,
        } = inputs;
        if workload_profiles.is_empty()
            || workload_profiles
                .values()
                .any(|qos| qos != "interactive" && qos != "batch")
        {
            return Err(CoordinatorError::Value(WORKLOAD_PROFILES_INVALID));
        }
        let lease_clock = lease_clock.unwrap_or_else(|| Arc::clone(&clock));
        let max_concurrent_requests = match max_concurrent_requests {
            Some(value) => value,
            None => PyReal::signed(
                false,
                PyInt::from(count(
                    BudgetId::PyFinalMyceliumM16RuntimeMaxConcurrentRequestsL362C39,
                )?),
            ),
        };
        let floor = count(BudgetId::PyRangeFloorMyceliumM16RuntimeL378C19)?;
        let ceiling = count(BudgetId::PyUpperGuardMyceliumM16RuntimeL378C19)?;
        let max_concurrent_requests = concurrency(&max_concurrent_requests, floor, ceiling)
            .ok_or(CoordinatorError::Value(INVALID_MAX_CONCURRENT_REQUESTS))?;
        let id_source = IdHandle(id_source);
        let ledger = ResourceLedger::new(
            &graph,
            &placement_capacities,
            ClockHandle(Arc::clone(&lease_clock)),
            id_source.clone(),
        )
        .map_err(CoordinatorError::Ledger)?;
        let policy = RoutePolicy::new(RouteScorer::new(config.clone()));
        let scheduler = HopScheduler::new(config).map_err(CoordinatorError::Config)?;
        let graph_digest = execution_graph_digest(&graph).map_err(CoordinatorError::Digest)?;
        Ok(Self {
            inner: Mutex::new(Inner {
                graph,
                states: device_states,
                profiles: workload_profiles,
                clock,
                lease_clock,
                id_source,
                max_concurrent_requests,
                ledger,
                policy,
                scheduler,
                requests: Vec::new(),
                index: BTreeMap::new(),
                active_request_ids: BTreeSet::new(),
                incidents: Vec::new(),
                incident_sequence: 0,
                graph_digest,
            }),
        })
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// :402-404.
    pub fn maximum_concurrent_requests(&self) -> u64 {
        self.lock().max_concurrent_requests
    }

    /// :406-497, in its order: `invalid_excluded_placements` unless every element is a
    /// non-empty `str`; `workload_not_qualified` when the profile is unknown or its qos class is
    /// not the request's (Python `str` `!=`); `duplicate_request_id` for a recorded request;
    /// then build the path (a `RoutingError` records the `admission_rejected` incident and
    /// raises `resource_unavailable`; any other builder exception propagates), digest the locked
    /// path, read the queue time, enqueue the REQUEST item (a `BackpressureError` releases the
    /// request's reservations, records the `backpressure` incident and raises `queue_full` with
    /// the retry; any other scheduler exception propagates), record the request and return the
    /// manifest.
    pub fn admit(
        &self,
        request: &RequestContext,
        workload_profile_id: &str,
        excluded_placements: &[Value],
    ) -> Result<PathManifest, CoordinatorError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        if !excluded_placements
            .iter()
            .all(|item| item.as_str().is_some_and(|text| !text.is_empty()))
        {
            return Err(admission(INVALID_EXCLUDED_PLACEMENTS));
        }
        let qualified = inner
            .profiles
            .get(workload_profile_id)
            .is_some_and(|expected| request.qos_class == *expected);
        if !qualified {
            return Err(admission(WORKLOAD_NOT_QUALIFIED));
        }
        if inner.index.contains_key(&request.request_id) {
            return Err(admission(DUPLICATE_REQUEST_ID));
        }
        // Every element is a str, so hashable.
        let excluded = IdSet::of(excluded_placements.iter().cloned())
            .map_err(|_unhashable| admission(INVALID_EXCLUDED_PLACEMENTS))?;
        let manifest = match inner.build_path(request, excluded) {
            Ok(manifest) => manifest,
            Err(PathBuilderError::Routing { .. }) => {
                inner.record_incident(
                    "admission_rejected",
                    &request.request_id,
                    "deployment",
                    RESOURCE_UNAVAILABLE,
                    None,
                );
                return Err(admission(RESOURCE_UNAVAILABLE));
            }
            Err(error) => return Err(CoordinatorError::PathBuilder(error)),
        };
        let document = path_manifest_to_dict(&manifest).map_err(CoordinatorError::Digest)?;
        let path_digest = digest_document(&document).map_err(CoordinatorError::Digest)?;
        let queued_at = inner.clock.now();
        let first = manifest
            .ordered_hops
            .first()
            .ok_or(CoordinatorError::Python(EMPTY_PATH))?;
        let placement_id = PyJson::from_json(&first.placement_id)
            .ok_or(CoordinatorError::Python(UNREPRESENTABLE_PLACEMENT_ID))?;
        let lease_expires_at = float_min(
            manifest
                .ordered_hops
                .iter()
                .map(|hop| hop.reservation_expires_at),
        )
        .ok_or(CoordinatorError::Python(EMPTY_PATH))?;
        let mut item = HopWorkItem::new(
            &request.request_id,
            &manifest.path_id,
            PyReal::signed(false, PyInt::from(manifest.path_attempt)),
            "REQUEST",
            PyReal::int(-1),
            PyReal::int(0),
            placement_id,
            &request.qos_class,
            0.0,
            queued_at,
            &format!("m16:{}:{}", request.request_id, manifest.path_id),
            HopPayload::Tuple(request.prompt_token_ids.clone()),
        );
        item.lease_expires_at = PyReal::Float(lease_expires_at);
        match inner.scheduler.enqueue(item) {
            Ok(()) => {}
            Err(SchedulerError::Backpressure {
                reason,
                retry_after_seconds,
            }) => {
                inner.ledger.release_request(&request.request_id);
                inner.record_incident(
                    "backpressure",
                    &request.request_id,
                    "queue",
                    reason,
                    Some(retry_after_seconds.clone()),
                );
                return Err(CoordinatorError::Admission(M16AdmissionError {
                    code: QUEUE_FULL,
                    retry_after_seconds: Some(retry_after_seconds),
                }));
            }
            Err(error) => return Err(CoordinatorError::Scheduler(error)),
        }
        inner
            .index
            .insert(request.request_id.clone(), inner.requests.len());
        inner.requests.push(Record {
            record: RequestRecord {
                request: request.clone(),
                request_attempt: 1,
                workload_profile_id: workload_profile_id.to_owned(),
                manifest: manifest.clone(),
                path_manifest_digest: path_digest,
                recovery_source_fenced: false,
                recovery_attempts: 0,
                phase: "queued".to_owned(),
                queued_at,
                dispatch_at: None,
                terminal_at: None,
                terminal_state: None,
            },
            document,
        });
        Ok(manifest)
    }

    /// :499-526: `None` at the concurrency cap or on an empty queue; else pop the next item at
    /// `clock.now()`; when any of its request's live reservations has `expires_at <=
    /// lease_clock.now()`, release the request, fail it (`reservation_expired`, terminal time
    /// `clock.now()`), record the incident and try the next item; else mark it active in
    /// `prefill` at `clock.now()` and return its request id.
    pub fn next_dispatch(&self) -> Result<Option<String>, CoordinatorError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        loop {
            if inner.active_request_ids.len() as u64 >= inner.max_concurrent_requests
                || inner.scheduler.queue_depth() == 0
            {
                return Ok(None);
            }
            let now = inner.clock.now();
            let item = inner
                .scheduler
                .pop_next(now)
                .map_err(CoordinatorError::Scheduler)?;
            let request_id = item.request_id;
            if inner.record(&request_id).is_none() {
                return Err(CoordinatorError::Python(UNKNOWN_QUEUED_REQUEST));
            }
            let reservations = inner.ledger.request_reservations(&request_id);
            let expired = reservations
                .iter()
                .any(|reservation| reservation.expires_at <= inner.lease_clock.now());
            if expired {
                inner.ledger.release_request(&request_id);
                let terminal_at = inner.clock.now();
                if let Some(held) = inner.record_mut(&request_id) {
                    held.record.phase = "failed".to_owned();
                    held.record.terminal_state = Some("reservation_expired".to_owned());
                    held.record.terminal_at = Some(terminal_at);
                }
                inner.record_incident(
                    "reservation_expired",
                    &request_id,
                    "request",
                    "failed",
                    None,
                );
                continue;
            }
            inner.active_request_ids.insert(request_id.clone());
            let dispatch_at = inner.clock.now();
            if let Some(held) = inner.record_mut(&request_id) {
                held.record.phase = "prefill".to_owned();
                held.record.dispatch_at = Some(dispatch_at);
            }
            return Ok(Some(request_id));
        }
    }

    /// :528-537: `m16_phase_transition_invalid` unless the request is active and the phase is
    /// `prefill`, `first_token`, `decode` or `cleanup`.
    pub fn mark_phase(&self, request_id: &str, phase: &str) -> Result<(), CoordinatorError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        if !inner.active_request_ids.contains(request_id)
            || !matches!(phase, "prefill" | "first_token" | "decode" | "cleanup")
        {
            return Err(CoordinatorError::Value(PHASE_TRANSITION_INVALID));
        }
        if let Some(held) = inner.record_mut(request_id) {
            held.record.phase = phase.to_owned();
        }
        Ok(())
    }

    /// :539-565: `m16_completion_invalid` unless the request is active, or holds the late
    /// cleanup proof (not active, recorded, phase `cleanup`, no terminal state), and the state
    /// is `completed`, `failed` or `cancelled`; then release its reservations, set phase and
    /// terminal state, the terminal time `clock.now()`, and free its dispatch slot.
    pub fn complete(&self, request_id: &str, state: &str) -> Result<(), CoordinatorError> {
        let mut guard = self.lock();
        let inner = &mut *guard;
        let active = inner.active_request_ids.contains(request_id);
        let late_cleanup_proof = !active
            && inner.record(request_id).is_some_and(|held| {
                held.record.phase == "cleanup" && held.record.terminal_state.is_none()
            });
        if (!active && !late_cleanup_proof)
            || !matches!(state, "completed" | "failed" | "cancelled")
        {
            return Err(CoordinatorError::Value(COMPLETION_INVALID));
        }
        inner.ledger.release_request(request_id);
        let terminal_at = inner.clock.now();
        if let Some(held) = inner.record_mut(request_id) {
            held.record.phase = state.to_owned();
            held.record.terminal_state = Some(state.to_owned());
            held.record.terminal_at = Some(terminal_at);
        }
        inner.active_request_ids.remove(request_id);
        Ok(())
    }

    /// :567-579: free the dispatch slot only; the record and the reservations stay.
    pub fn retire_dispatch_slot(&self, request_id: &str) {
        self.lock().active_request_ids.remove(request_id);
    }

    /// :581-599: `false` for an unknown or terminal request; else drop its queued path, release
    /// its reservations, cancel it at `clock.now()`, free its slot, record the
    /// `cancellation_cleanup` incident and return `true`.
    pub fn cancel(&self, request_id: &str) -> bool {
        let mut guard = self.lock();
        let inner = &mut *guard;
        let Some(path_id) = inner
            .record(request_id)
            .filter(|held| held.record.terminal_state.is_none())
            .map(|held| held.record.manifest.path_id.clone())
        else {
            return false;
        };
        inner.scheduler.release_path(&path_id);
        inner.ledger.release_request(request_id);
        let terminal_at = inner.clock.now();
        if let Some(held) = inner.record_mut(request_id) {
            held.record.phase = "cancelled".to_owned();
            held.record.terminal_state = Some("cancelled".to_owned());
            held.record.terminal_at = Some(terminal_at);
        }
        inner.active_request_ids.remove(request_id);
        inner.record_incident(
            "cancellation_cleanup",
            request_id,
            "request",
            "resources_released",
            None,
        );
        true
    }

    /// :654-657.
    pub fn phase(&self, request_id: &str) -> Option<String> {
        self.lock()
            .record(request_id)
            .map(|held| held.record.phase.clone())
    }

    /// :659-664.
    pub fn terminal_state(&self, request_id: &str) -> Option<String> {
        self.lock()
            .record(request_id)
            .and_then(|held| held.record.terminal_state.clone())
    }

    /// :666-673: the owner-held request context; `request_not_admitted` for an unknown id.
    pub fn request_context(&self, request_id: &str) -> Result<RequestContext, M16AdmissionError> {
        self.lock()
            .record(request_id)
            .map(|held| held.record.request.clone())
            .ok_or_else(not_admitted)
    }

    /// :675-695.
    pub fn route_identity(&self, request_id: &str) -> Result<RouteIdentity, M16AdmissionError> {
        let guard = self.lock();
        let held = guard.record(request_id).ok_or_else(not_admitted)?;
        let record = &held.record;
        Ok(RouteIdentity {
            request_id: record.request.request_id.clone(),
            request_attempt: record.request_attempt,
            path_id: record.manifest.path_id.clone(),
            path_attempt: record.manifest.path_attempt,
            path_manifest_digest: record.path_manifest_digest.clone(),
            deployment_id: guard.graph.as_value()["deployment_id"].clone(),
            deployment_epoch: record.manifest.deployment_epoch.clone(),
            topology_generation: record.manifest.topology_version.clone(),
        })
    }

    /// :697-707 `path_manifest_to_dict(record["manifest"])`.
    pub fn path_manifest(&self, request_id: &str) -> Result<Value, M16AdmissionError> {
        self.lock()
            .record(request_id)
            .map(|held| held.document.clone())
            .ok_or_else(not_admitted)
    }

    /// `self._incidents`, in order (read-only, for the status step).
    pub fn incidents(&self) -> Vec<Incident> {
        self.lock().incidents.clone()
    }

    /// `self._requests[request_id]`.
    pub fn request_record(&self, request_id: &str) -> Option<RequestRecord> {
        self.lock()
            .record(request_id)
            .map(|held| held.record.clone())
    }

    /// `self._requests.values()`, in insertion order.
    pub fn request_records(&self) -> Vec<RequestRecord> {
        self.lock()
            .requests
            .iter()
            .map(|held| held.record.clone())
            .collect()
    }

    /// `sorted(self._active_request_ids)`.
    pub fn active_request_ids(&self) -> Vec<String> {
        self.lock().active_request_ids.iter().cloned().collect()
    }

    /// `self._graph_digest` (:399).
    pub fn graph_digest(&self) -> String {
        self.lock().graph_digest.clone()
    }

    /// `self._scheduler.queue_depth()`.
    pub fn queue_depth(&self) -> usize {
        self.lock().scheduler.queue_depth()
    }

    /// `self._scheduler.queued_payload_bytes()`.
    pub fn queued_payload_bytes(&self) -> u64 {
        self.lock().scheduler.queued_payload_bytes()
    }

    /// The scheduler's queued items, in arrival order.
    pub fn queued_items(&self) -> Vec<HopWorkItem> {
        self.lock().scheduler.queued_items().cloned().collect()
    }

    /// `self._ledger.request_reservations(request_id)`: its live reservations.
    pub fn request_reservations(&self, request_id: &str) -> Vec<Reservation> {
        self.lock().ledger.request_reservations(request_id)
    }

    /// `self._ledger.placement_status()`.
    pub fn placement_status(&self) -> Result<Vec<PlacementStatus>, LedgerError> {
        self.lock().ledger.placement_status()
    }
}
