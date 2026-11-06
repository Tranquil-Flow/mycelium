// SPDX-License-Identifier: AGPL-3.0-only
//! Acquisition status and the acquisition store's current/history subset (v0.1.4 f4e366e9:
//! `mycelium_swarm_artifacts.py:1119-1247` and `mycelium_live/artifact_provisioner.py:212-228,
//! 369-419,441-466,476-500`). Not yet ported: every entry point refuses with
//! `acquisition_status_unported`.
use crate::{Error, Result};
use std::path::{Path, PathBuf};

/// `mycelium_swarm_artifacts.ACQUISITION_PROTOCOL` (:36).
pub const ACQUISITION_PROTOCOL: &str = "mycelium.swarm_artifact_acquisition.v1";
/// `mycelium_swarm_artifacts.LEDGER_PROTOCOL` (:37).
pub const LEDGER_PROTOCOL: &str = "mycelium.swarm_artifact_acquisition_ledger.v1";
/// `ArtifactAcquisitionStore(history_limit=64)` (artifact_provisioner.py:214).
pub const DEFAULT_HISTORY_LIMIT: u64 = 64;

/// A Python `str`: its code points in order, lone surrogates included.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Debug)]
pub struct PyText(Vec<u32>);

impl PyText {
    pub fn new(text: &str) -> Self {
        Self(text.chars().map(u32::from).collect())
    }
    /// `None` for a point past U+10FFFF.
    pub fn from_code_points(points: impl IntoIterator<Item = u32>) -> Option<Self> {
        let points: Vec<u32> = points.into_iter().collect();
        points
            .iter()
            .all(|p| *p <= 0x10_FFFF)
            .then_some(Self(points))
    }
    pub fn code_points(&self) -> &[u32] {
        &self.0
    }
    /// `None` when the text holds a surrogate code point.
    pub fn to_text(&self) -> Option<String> {
        self.0.iter().map(|p| char::from_u32(*p)).collect()
    }
}

/// A Python `int` of any size: sign and decimal digits (no leading zero; zero is never negative).
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct PyInt {
    negative: bool,
    digits: String,
}

impl PyInt {
    /// JSON integer text `-?(0|[1-9][0-9]*)`.
    pub fn parse(text: &str) -> Option<Self> {
        let (negative, digits) = match text.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, text),
        };
        let bytes = digits.as_bytes();
        if bytes.is_empty()
            || !bytes.iter().all(u8::is_ascii_digit)
            || (bytes.len() > 1 && bytes[0] == b'0')
        {
            return None;
        }
        Some(Self {
            negative: negative && digits != "0",
            digits: digits.to_owned(),
        })
    }
}

impl From<u64> for PyInt {
    fn from(n: u64) -> Self {
        Self {
            negative: false,
            digits: n.to_string(),
        }
    }
}

impl From<i64> for PyInt {
    fn from(n: i64) -> Self {
        Self {
            negative: n < 0,
            digits: n.unsigned_abs().to_string(),
        }
    }
}

/// A value as CPython's `json.loads` decodes it.
#[derive(Clone, Debug)]
pub enum PyValue {
    None,
    Bool(bool),
    Int(PyInt),
    Float(f64),
    Str(PyText),
    List(Vec<PyValue>),
    /// Distinct keys in insertion order, as a `dict`'s.
    Dict(Vec<(PyText, PyValue)>),
}

impl PyValue {
    /// A standard JSON value as `json.loads` reads its text: a number literal with `.`, `e` or
    /// `E` is a `float`, any other an `int`.
    pub fn from_json(value: &serde_json::Value) -> Self {
        use serde_json::Value;
        match value {
            Value::Null => Self::None,
            Value::Bool(b) => Self::Bool(*b),
            Value::Number(n) => {
                let text = n.to_string();
                if text.contains(['.', 'e', 'E']) {
                    Self::Float(text.parse().unwrap_or(f64::NAN))
                } else {
                    PyInt::parse(&text).map_or(Self::Float(f64::NAN), Self::Int)
                }
            }
            Value::String(s) => Self::Str(PyText::new(s)),
            Value::Array(items) => Self::List(items.iter().map(Self::from_json).collect()),
            Value::Object(map) => Self::Dict(
                map.iter()
                    .map(|(k, v)| (PyText::new(k), Self::from_json(v)))
                    .collect(),
            ),
        }
    }
}

/// `json.loads(bytes.decode())`.
pub fn loads(_bytes: &[u8]) -> Result<PyValue> {
    Err(Error::new("acquisition_status_unported"))
}

/// `aggregate_bytes_per_second` / `eta_seconds`: an `int` or a `float`, kept as decoded.
#[derive(Clone, Copy, Debug)]
pub enum PyNumber {
    Int(u64),
    Float(f64),
}

impl PartialEq for PyNumber {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Int(a), Self::Int(b)) => a == b,
            (Self::Float(a), Self::Float(b)) => a.to_bits() == b.to_bits(),
            _ => false,
        }
    }
}

/// `_ACQUISITION_STATES` (mycelium_swarm_artifacts.py:57-71).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AcquisitionState {
    Pending,
    Reserving,
    DiscoveringSources,
    Transferring,
    VerifyingChunks,
    VerifyingPack,
    Promoting,
    Ready,
    Cancelling,
    Cancelled,
    Failed,
}

/// `_SOURCE_STATES` (mycelium_swarm_artifacts.py:56).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SourceState {
    Eligible,
    Active,
    Rotated,
    Lost,
    Origin,
}

/// One entry of `sources` (`_SOURCE_STATUS_FIELDS`, :269).
#[derive(Clone, Debug, PartialEq)]
pub struct SourceStatus {
    pub source_ref: String,
    pub state: SourceState,
    pub verified_bytes: u64,
}

/// A validated acquisition status (`_STATUS_FIELDS`, :225-268; `protocol` is always
/// [`ACQUISITION_PROTOCOL`]).
#[derive(Clone, Debug, PartialEq)]
pub struct AcquisitionStatus {
    pub generation: u64,
    pub acquisition_id: String,
    pub state: AcquisitionState,
    pub phase: Option<AcquisitionState>,
    pub model_id: PyText,
    pub model_revision: String,
    pub representation: PyText,
    pub assignment_id: String,
    pub placement_id: String,
    pub stage_id: String,
    pub layer_start: u64,
    pub layer_end_exclusive: u64,
    pub total_bytes: u64,
    pub cached_verified_bytes: u64,
    pub transferred_verified_bytes: u64,
    pub missing_bytes: u64,
    pub quarantined_bytes: u64,
    pub duplicate_bytes_prevented: u64,
    pub eligible_source_count: u64,
    pub active_source_count: u64,
    pub sources: Vec<SourceStatus>,
    pub origin_bytes: u64,
    pub aggregate_bytes_per_second: PyNumber,
    pub eta_seconds: Option<PyNumber>,
    pub chunk_count: u64,
    pub verified_chunk_count: u64,
    pub resumed_chunk_count: u64,
    pub source_rotation_count: u64,
    pub manifest_digest: String,
    pub assignment_digest: String,
    pub representation_digest: String,
    pub feasibility_digest: String,
    pub evidence_generation: u64,
    pub promotion_digest: Option<String>,
    pub reason_code: Option<String>,
    pub retryable: bool,
    pub started_at_unix_ms: u64,
    pub updated_at_unix_ms: u64,
    pub terminal_at_unix_ms: Option<u64>,
}

impl AcquisitionState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Reserving => "reserving",
            Self::DiscoveringSources => "discovering_sources",
            Self::Transferring => "transferring",
            Self::VerifyingChunks => "verifying_chunks",
            Self::VerifyingPack => "verifying_pack",
            Self::Promoting => "promoting",
            Self::Ready => "ready",
            Self::Cancelling => "cancelling",
            Self::Cancelled => "cancelled",
            Self::Failed => "failed",
        }
    }
}

impl SourceState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Eligible => "eligible",
            Self::Active => "active",
            Self::Rotated => "rotated",
            Self::Lost => "lost",
            Self::Origin => "origin",
        }
    }
}

fn number_value(n: PyNumber) -> PyValue {
    match n {
        PyNumber::Int(i) => PyValue::Int(i.into()),
        PyNumber::Float(x) => PyValue::Float(x),
    }
}

fn optional<T>(value: Option<T>, f: impl FnOnce(T) -> PyValue) -> PyValue {
    value.map_or(PyValue::None, f)
}

impl AcquisitionStatus {
    /// The status as the `dict` the oracle validates and writes (`protocol` included).
    pub fn to_value(&self) -> PyValue {
        let text = |s: &str| PyValue::Str(PyText::new(s));
        let int = |n: u64| PyValue::Int(n.into());
        let sources = self
            .sources
            .iter()
            .map(|s| {
                PyValue::Dict(vec![
                    (PyText::new("source_ref"), text(&s.source_ref)),
                    (PyText::new("state"), text(s.state.as_str())),
                    (PyText::new("verified_bytes"), int(s.verified_bytes)),
                ])
            })
            .collect();
        let fields: Vec<(&str, PyValue)> = vec![
            ("protocol", text(ACQUISITION_PROTOCOL)),
            ("generation", int(self.generation)),
            ("acquisition_id", text(&self.acquisition_id)),
            ("state", text(self.state.as_str())),
            ("phase", optional(self.phase, |p| text(p.as_str()))),
            ("model_id", PyValue::Str(self.model_id.clone())),
            ("model_revision", text(&self.model_revision)),
            ("representation", PyValue::Str(self.representation.clone())),
            ("assignment_id", text(&self.assignment_id)),
            ("placement_id", text(&self.placement_id)),
            ("stage_id", text(&self.stage_id)),
            ("layer_start", int(self.layer_start)),
            ("layer_end_exclusive", int(self.layer_end_exclusive)),
            ("total_bytes", int(self.total_bytes)),
            ("cached_verified_bytes", int(self.cached_verified_bytes)),
            (
                "transferred_verified_bytes",
                int(self.transferred_verified_bytes),
            ),
            ("missing_bytes", int(self.missing_bytes)),
            ("quarantined_bytes", int(self.quarantined_bytes)),
            (
                "duplicate_bytes_prevented",
                int(self.duplicate_bytes_prevented),
            ),
            ("eligible_source_count", int(self.eligible_source_count)),
            ("active_source_count", int(self.active_source_count)),
            ("sources", PyValue::List(sources)),
            ("origin_bytes", int(self.origin_bytes)),
            (
                "aggregate_bytes_per_second",
                number_value(self.aggregate_bytes_per_second),
            ),
            ("eta_seconds", optional(self.eta_seconds, number_value)),
            ("chunk_count", int(self.chunk_count)),
            ("verified_chunk_count", int(self.verified_chunk_count)),
            ("resumed_chunk_count", int(self.resumed_chunk_count)),
            ("source_rotation_count", int(self.source_rotation_count)),
            ("manifest_digest", text(&self.manifest_digest)),
            ("assignment_digest", text(&self.assignment_digest)),
            ("representation_digest", text(&self.representation_digest)),
            ("feasibility_digest", text(&self.feasibility_digest)),
            ("evidence_generation", int(self.evidence_generation)),
            (
                "promotion_digest",
                optional(self.promotion_digest.as_deref(), text),
            ),
            ("reason_code", optional(self.reason_code.as_deref(), text)),
            ("retryable", PyValue::Bool(self.retryable)),
            ("started_at_unix_ms", int(self.started_at_unix_ms)),
            ("updated_at_unix_ms", int(self.updated_at_unix_ms)),
            (
                "terminal_at_unix_ms",
                optional(self.terminal_at_unix_ms, int),
            ),
        ];
        PyValue::Dict(
            fields
                .into_iter()
                .map(|(k, v)| (PyText::new(k), v))
                .collect(),
        )
    }
}

/// `validate_acquisition_status` (mycelium_swarm_artifacts.py:1119-1247).
pub fn validate_acquisition_status(_document: &PyValue) -> Result<AcquisitionStatus> {
    Err(Error::new("acquisition_status_unported"))
}

/// A validated ledger (`validate_acquisition_ledger`, :1250-1278).
#[derive(Clone, Debug, PartialEq)]
pub struct AcquisitionLedger {
    pub generation: u64,
    pub current: Option<AcquisitionStatus>,
    pub history: Vec<AcquisitionStatus>,
}

/// `ArtifactAcquisitionStore`'s current/history subset.
#[derive(Debug)]
pub struct ArtifactAcquisitionStore {
    root: PathBuf,
}

impl ArtifactAcquisitionStore {
    /// `ArtifactAcquisitionStore.__init__` (artifact_provisioner.py:214-228).
    pub fn open(_root: &Path, _history_limit: u64) -> Result<Self> {
        Err(Error::new("acquisition_status_unported"))
    }
    pub fn root(&self) -> &Path {
        &self.root
    }
    /// `write_current` (:369-386).
    pub fn write_current(&mut self, _status: &PyValue) -> Result<()> {
        Err(Error::new("acquisition_status_unported"))
    }
    /// `finish` (:399-419).
    pub fn finish(&mut self, _status: &PyValue) -> Result<()> {
        Err(Error::new("acquisition_status_unported"))
    }
    /// `ledger` (:441-466).
    pub fn ledger(&self) -> Result<AcquisitionLedger> {
        Err(Error::new("acquisition_status_unported"))
    }
}
