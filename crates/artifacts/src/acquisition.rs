// SPDX-License-Identifier: AGPL-3.0-only
//! Acquisition status and the acquisition store's current/history subset, ported from v0.1.4
//! f4e366e9 against the executed oracle (CPython 3.14.2):
//!
//! * [`validate_acquisition_status`]: `mycelium_swarm_artifacts.py:1119-1247` with its helpers
//!   (`_closed`, `_text`, `_identifier`, `_digest`, `_integer`, `_number`, :285-328) and the
//!   reason grammar `_REASON` (:44; a grammar, not a closed set of reasons). A refusal is
//!   `artifact_acquisition_status_invalid`, as the oracle raises it, except where the oracle's own
//!   path raises a builtin first: a list or dict `state` (:1123) or source `state` (:1222) is
//!   hashed by the frozenset membership test (`TypeError`), and an `int` rate or ETA past the
//!   double range overflows in `math.isfinite` (`OverflowError`, :324).
//! * [`ArtifactAcquisitionStore`]: `mycelium_live/artifact_provisioner.py` `__init__` (:215-228,
//!   with `_private_root` :126-147 and `_atomic_json` :112-123), `write_current`, `_read_history`
//!   and `finish` (:369-419), `ledger` (:441-466: it rereads `current.json`, refused as
//!   `artifact_state_corrupt`, and `history.json`, refused as `artifact_history_corrupt`),
//!   `public_ledger` (:468-474) and `_recover_interrupted` (:476-500), with
//!   `validate_acquisition_ledger` (mycelium_swarm_artifacts.py:1250-1278). Objects, partials,
//!   quarantine, grants, the writer lock, generations, member imports and the provisioner stay out.
//!
//! A file is decoded as `json.loads(path.read_text())` decodes it ([`loads`]): strict UTF-8,
//! `NaN`/`Infinity`/`-Infinity`, ints of any size up to 4300 digits, lone surrogates in strings,
//! and a repeated key keeps its last value. A stored file larger than [`MAX_DOCUMENT_BYTES`] or
//! nesting more than [`MAX_DOCUMENT_DEPTH`] containers is refused with its own code before it can
//! exhaust memory (the oracle's own limit, `RecursionError` near the 116178th nested container,
//! is a CPython stack property and not reproduced). A status is written as `_canonical` writes it (sorted
//! keys, `,`/`:` separators, raw non-ASCII, Python's float `repr`, a final newline); a text holding
//! a lone surrogate cannot be encoded (`UnicodeEncodeError`), as in the oracle.
//!
//! An exception the oracle does not catch keeps its class name as the code (`TypeError`,
//! `OverflowError`, `UnicodeEncodeError`, `ValueError`, `OSError` with the I/O kind); the two
//! bounds pass through the store the same way (`artifact_document_too_deep`,
//! `artifact_document_too_large`).
use crate::{Error, Result, sha_ref};
use budgets::{BudgetId as B, Entries};
use std::{
    collections::{HashMap, HashSet, hash_map::RandomState},
    ffi::OsStr,
    fs::{self, File, OpenOptions},
    hash::{BuildHasher, Hasher},
    io::{ErrorKind, Read, Write},
    os::unix::{
        ffi::{OsStrExt, OsStringExt},
        fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    },
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

/// `mycelium_swarm_artifacts.ACQUISITION_PROTOCOL` (:36).
pub const ACQUISITION_PROTOCOL: &str = "mycelium.swarm_artifact_acquisition.v1";
/// `mycelium_swarm_artifacts.LEDGER_PROTOCOL` (:37).
pub const LEDGER_PROTOCOL: &str = "mycelium.swarm_artifact_acquisition_ledger.v1";
/// `ArtifactAcquisitionStore(history_limit=64)` (artifact_provisioner.py:215).
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

impl PyInt {
    /// The value, when it fits an `i128` (every int of at most 38 digits does).
    fn exact(&self) -> Option<i128> {
        if self.digits.len() > 38 {
            return None;
        }
        let magnitude: i128 = self.digits.parse().ok()?;
        Some(if self.negative { -magnitude } else { magnitude })
    }

    /// `float(n)`, correctly rounded; infinite exactly where CPython raises `OverflowError`
    /// (`PyLong_AsDouble` rounds half-even to 53 bits, as decimal-to-double parsing does).
    fn to_f64(&self) -> f64 {
        let magnitude: f64 = self.digits.parse().unwrap_or(f64::INFINITY);
        if self.negative { -magnitude } else { magnitude }
    }

    fn write(&self, out: &mut Vec<u8>) {
        if self.negative {
            out.push(b'-');
        }
        out.extend_from_slice(self.digits.as_bytes());
    }
}

/// `sys.get_int_max_str_digits()` default: `int()` refuses a longer decimal literal with
/// `ValueError`, and json.loads converts every int literal through it.
const INT_MAX_STR_DIGITS: usize = 4300;
/// No reader here looks inside a container nested deeper than this (history list > status >
/// `sources` > source > field value), so a deeper container is decoded in full but kept only as
/// an empty container of its kind: nothing deep is built, and the decoder never recurses.
const KEPT_DEPTH: usize = 4;

/// The most containers a stored document may nest, counted from the top-level value as 1. A
/// valid `history.json` nests 4 (history list > status > `sources` > source) and a valid
/// `current.json` 3; entering a 33rd is `artifact_document_too_deep`, before anything inside it is
/// read, so neither a deep nor an unclosed document is scanned further. The decoder keeps open
/// containers on an explicit stack and never recurses, so the bound protects memory, not the
/// call stack.
pub const MAX_DOCUMENT_DEPTH: usize = 32;
/// The largest `current.json`/`history.json` the store reads: 16 MiB. A full history (the
/// ledger's maximum of 256 statuses) with every text field at its maximum length and every
/// character written as a `\u` escape stays well under 64 KiB a status, so 16 MiB never refuses
/// a document the store or the oracle writes. A longer file is `artifact_document_too_large`
/// after reading at most one byte past the bound (a link to an endless device included).
pub const MAX_DOCUMENT_BYTES: u64 = 16 * 1024 * 1024;
const DOCUMENT_TOO_DEEP: &str = "artifact_document_too_deep";
const DOCUMENT_TOO_LARGE: &str = "artifact_document_too_large";

fn syntax() -> Error {
    Error::new("JSONDecodeError")
}

/// `json.loads(text)` over `bytes` read as `Path.read_text()` reads them (strict UTF-8; the
/// universal-newline translation cannot change what JSON accepts). `UnicodeDecodeError`,
/// `JSONDecodeError` or `ValueError` (an int literal over 4300 digits), all `ValueError`s in
/// CPython, and `artifact_document_too_deep` on entering a container past [`MAX_DOCUMENT_DEPTH`].
/// A container nested deeper than four levels keeps only its kind, see [`KEPT_DEPTH`].
pub fn loads(bytes: &[u8]) -> Result<PyValue> {
    let text = std::str::from_utf8(bytes).map_err(|_| Error::new("UnicodeDecodeError"))?;
    let mut decoder = Decoder {
        s: text.as_bytes(),
        at: 0,
    };
    decoder.ws();
    let value = decoder.value()?;
    decoder.ws();
    if decoder.at != decoder.s.len() {
        return Err(syntax());
    }
    Ok(value)
}

struct Decoder<'a> {
    s: &'a [u8],
    at: usize,
}

#[derive(Default)]
struct DictBuild {
    entries: Vec<(PyText, PyValue)>,
    index: HashMap<PyText, usize>,
}

impl DictBuild {
    /// `dict[key] = value`: a repeated key keeps its first position and its last value.
    fn insert(&mut self, key: PyText, value: PyValue) {
        match self.index.get(&key).and_then(|i| self.entries.get_mut(*i)) {
            Some(entry) => entry.1 = value,
            None => {
                self.index.insert(key.clone(), self.entries.len());
                self.entries.push((key, value));
            }
        }
    }
}

/// An open container: its contents (`None` past [`KEPT_DEPTH`]) and, for a dict, the key of
/// the value being read.
enum Frame {
    List(Option<Vec<PyValue>>),
    Dict(Option<DictBuild>, PyText),
}

impl Decoder<'_> {
    fn peek(&self) -> Option<u8> {
        self.s.get(self.at).copied()
    }

    fn ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn literal(&mut self, word: &[u8]) -> bool {
        let found = self
            .s
            .get(self.at..)
            .is_some_and(|rest| rest.starts_with(word));
        if found {
            self.at += word.len();
        }
        found
    }

    /// `"key"` `:` with the whitespace around the colon (`_parse_object_unicode`).
    fn key(&mut self) -> Result<PyText> {
        if self.peek() != Some(b'"') {
            return Err(syntax());
        }
        let key = self.string()?;
        self.ws();
        if self.peek() != Some(b':') {
            return Err(syntax());
        }
        self.at += 1;
        self.ws();
        Ok(key)
    }

    /// `scan_once` without recursion: open containers live on an explicit stack, at most
    /// [`MAX_DOCUMENT_DEPTH`] of them.
    fn value(&mut self) -> Result<PyValue> {
        let mut stack: Vec<Frame> = Vec::new();
        loop {
            let keep = stack.len() < KEPT_DEPTH;
            if matches!(self.peek(), Some(b'[' | b'{')) && stack.len() >= MAX_DOCUMENT_DEPTH {
                return Err(Error::new(DOCUMENT_TOO_DEEP));
            }
            let mut done = match self.peek() {
                Some(b'[') => {
                    self.at += 1;
                    self.ws();
                    if self.peek() == Some(b']') {
                        self.at += 1;
                        PyValue::List(Vec::new())
                    } else {
                        stack.push(Frame::List(keep.then(Vec::new)));
                        continue;
                    }
                }
                Some(b'{') => {
                    self.at += 1;
                    self.ws();
                    if self.peek() == Some(b'}') {
                        self.at += 1;
                        PyValue::Dict(Vec::new())
                    } else {
                        let key = self.key()?;
                        stack.push(Frame::Dict(keep.then(DictBuild::default), key));
                        continue;
                    }
                }
                _ => self.scalar()?,
            };
            loop {
                let Some(frame) = stack.last_mut() else {
                    return Ok(done);
                };
                let close = match frame {
                    Frame::List(items) => {
                        if let Some(items) = items {
                            items.push(done);
                        }
                        self.ws();
                        match self.peek() {
                            Some(b',') => false,
                            Some(b']') => true,
                            _ => return Err(syntax()),
                        }
                    }
                    Frame::Dict(build, key) => {
                        let key = std::mem::replace(key, PyText(Vec::new()));
                        if let Some(build) = build {
                            build.insert(key, done);
                        }
                        self.ws();
                        match self.peek() {
                            Some(b',') => false,
                            Some(b'}') => true,
                            _ => return Err(syntax()),
                        }
                    }
                };
                self.at += 1;
                if !close {
                    self.ws();
                    if let Some(Frame::Dict(_, key)) = stack.last_mut() {
                        *key = self.key()?;
                    }
                    break;
                }
                done = match stack.pop() {
                    Some(Frame::List(items)) => PyValue::List(items.unwrap_or_default()),
                    Some(Frame::Dict(build, _)) => {
                        PyValue::Dict(build.map(|b| b.entries).unwrap_or_default())
                    }
                    None => return Err(syntax()),
                };
            }
        }
    }

    fn scalar(&mut self) -> Result<PyValue> {
        match self.peek() {
            Some(b'"') => Ok(PyValue::Str(self.string()?)),
            Some(b'n') if self.literal(b"null") => Ok(PyValue::None),
            Some(b't') if self.literal(b"true") => Ok(PyValue::Bool(true)),
            Some(b'f') if self.literal(b"false") => Ok(PyValue::Bool(false)),
            Some(b'N') if self.literal(b"NaN") => Ok(PyValue::Float(f64::NAN)),
            Some(b'I') if self.literal(b"Infinity") => Ok(PyValue::Float(f64::INFINITY)),
            Some(b'-') if self.literal(b"-Infinity") => Ok(PyValue::Float(f64::NEG_INFINITY)),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(syntax()),
        }
    }

    fn digits(&mut self) {
        while self.peek().is_some_and(|b| b.is_ascii_digit()) {
            self.at += 1;
        }
    }

    /// `_match_number_unicode`: a fraction or exponent without digits is not consumed.
    fn number(&mut self) -> Result<PyValue> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        match self.peek() {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => self.digits(),
            _ => return Err(syntax()),
        }
        let mut float = false;
        if self.peek() == Some(b'.') && self.s.get(self.at + 1).is_some_and(u8::is_ascii_digit) {
            self.at += 1;
            self.digits();
            float = true;
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let mark = self.at;
            self.at += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.at += 1;
            }
            if self.peek().is_some_and(|b| b.is_ascii_digit()) {
                self.digits();
                float = true;
            } else {
                self.at = mark;
            }
        }
        let text = std::str::from_utf8(&self.s[start..self.at]).map_err(|_| syntax())?;
        if float {
            return text.parse().map(PyValue::Float).map_err(|_| syntax());
        }
        if text.trim_start_matches('-').len() > INT_MAX_STR_DIGITS {
            return Err(Error::new("ValueError"));
        }
        PyInt::parse(text).map(PyValue::Int).ok_or_else(syntax)
    }

    fn hex4(&mut self) -> Result<u32> {
        let digits = self.s.get(self.at..self.at + 4).ok_or_else(syntax)?;
        let mut value = 0;
        for digit in digits {
            value = value * 16 + char::from(*digit).to_digit(16).ok_or_else(syntax)?;
        }
        self.at += 4;
        Ok(value)
    }

    /// `scanstring_unicode` (strict): no raw control character; an escaped high surrogate
    /// directly followed by an escaped low one is one code point, any other surrogate escape
    /// stays a lone surrogate.
    fn string(&mut self) -> Result<PyText> {
        self.at += 1;
        let mut out = Vec::new();
        loop {
            let start = self.at;
            while self
                .peek()
                .is_some_and(|b| b != b'"' && b != b'\\' && b >= 0x20)
            {
                self.at += 1;
            }
            let run = std::str::from_utf8(&self.s[start..self.at]).map_err(|_| syntax())?;
            out.extend(run.chars().map(u32::from));
            match self.peek() {
                Some(b'"') => {
                    self.at += 1;
                    return Ok(PyText(out));
                }
                Some(b'\\') => {
                    self.at += 1;
                    let escape = self.peek().ok_or_else(syntax)?;
                    self.at += 1;
                    let point = match escape {
                        b'"' => 0x22,
                        b'\\' => 0x5c,
                        b'/' => 0x2f,
                        b'b' => 0x08,
                        b'f' => 0x0c,
                        b'n' => 0x0a,
                        b'r' => 0x0d,
                        b't' => 0x09,
                        b'u' => {
                            let high = self.hex4()?;
                            let paired = (0xd800..=0xdbff).contains(&high)
                                && self.s.get(self.at..).is_some_and(|r| r.starts_with(b"\\u"));
                            if paired {
                                let mark = self.at;
                                self.at += 2;
                                let low = self.hex4()?;
                                if (0xdc00..=0xdfff).contains(&low) {
                                    0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00)
                                } else {
                                    self.at = mark;
                                    high
                                }
                            } else {
                                high
                            }
                        }
                        _ => return Err(syntax()),
                    };
                    out.push(point);
                }
                _ => return Err(syntax()),
            }
        }
    }
}

/// Python's `repr(float)` for a finite value (`float_repr_style` "short"): the shortest
/// round-tripping digits, positional for `-4 < exponent + 1 <= 16`, else `d.ddde+XX`.
fn float_repr(x: f64) -> String {
    let sign = if x.is_sign_negative() { "-" } else { "" };
    if x == 0.0 {
        return format!("{sign}0.0");
    }
    let scientific = format!("{:e}", x.abs());
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let point = exponent.parse::<i64>().unwrap_or(0) + 1;
    let count = digits.len() as i64;
    let body = if point <= -4 || point > 16 {
        let exponent = point - 1;
        let fraction = if count > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        let mark = if exponent < 0 { '-' } else { '+' };
        format!("{}{fraction}e{mark}{:02}", &digits[..1], exponent.abs())
    } else if point <= 0 {
        format!("0.{}{digits}", "0".repeat(point.unsigned_abs() as usize))
    } else if point >= count {
        format!("{digits}{}.0", "0".repeat((point - count) as usize))
    } else {
        format!(
            "{}.{}",
            &digits[..point as usize],
            &digits[point as usize..]
        )
    };
    format!("{sign}{body}")
}

fn encode_text(text: &PyText, out: &mut Vec<u8>, surrogate: &mut bool) {
    out.push(b'"');
    for point in &text.0 {
        match *point {
            0x22 => out.extend_from_slice(b"\\\""),
            0x5c => out.extend_from_slice(b"\\\\"),
            0x0a => out.extend_from_slice(b"\\n"),
            0x0d => out.extend_from_slice(b"\\r"),
            0x09 => out.extend_from_slice(b"\\t"),
            0x08 => out.extend_from_slice(b"\\b"),
            0x0c => out.extend_from_slice(b"\\f"),
            p if p < 0x20 => out.extend_from_slice(format!("\\u{p:04x}").as_bytes()),
            p => match char::from_u32(p) {
                Some(c) => out.extend_from_slice(c.encode_utf8(&mut [0; 4]).as_bytes()),
                None => *surrogate = true,
            },
        }
    }
    out.push(b'"');
}

/// Only called on values built from validated statuses (at most four levels deep).
fn encode(value: &PyValue, out: &mut Vec<u8>, surrogate: &mut bool) -> Result<()> {
    match value {
        PyValue::None => out.extend_from_slice(b"null"),
        PyValue::Bool(true) => out.extend_from_slice(b"true"),
        PyValue::Bool(false) => out.extend_from_slice(b"false"),
        PyValue::Int(n) => n.write(out),
        PyValue::Float(x) if x.is_finite() => out.extend_from_slice(float_repr(*x).as_bytes()),
        PyValue::Float(_) => return Err(Error::new("ValueError")),
        PyValue::Str(text) => encode_text(text, out, surrogate),
        PyValue::List(items) => {
            out.push(b'[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                encode(item, out, surrogate)?;
            }
            out.push(b']');
        }
        PyValue::Dict(entries) => {
            let mut sorted: Vec<&(PyText, PyValue)> = entries.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            out.push(b'{');
            for (i, (key, item)) in sorted.into_iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                encode_text(key, out, surrogate);
                out.push(b':');
                encode(item, out, surrogate)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

/// `_canonical` (artifact_provisioner.py:90-101): `json.dumps(sort_keys=True, separators=(",",
/// ":"), ensure_ascii=False, allow_nan=False) + "\n"`, then `.encode()` (strict UTF-8).
fn canonical(value: &PyValue) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    let mut surrogate = false;
    encode(value, &mut out, &mut surrogate)?;
    if surrogate {
        return Err(Error::new("UnicodeEncodeError"));
    }
    out.push(b'\n');
    Ok(out)
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

const STATUS_INVALID: &str = "artifact_acquisition_status_invalid";
const LEDGER_INVALID: &str = "artifact_acquisition_ledger_invalid";
const STATE_CORRUPT: &str = "artifact_state_corrupt";
const HISTORY_CORRUPT: &str = "artifact_history_corrupt";
const TERMINAL_INVALID: &str = "artifact_terminal_status_invalid";
const ROOT_UNSAFE: &str = "artifact_state_root_unsafe";
/// `_text`'s default maximum (mycelium_swarm_artifacts.py:291).
const TEXT_MAX: usize = 256;
/// `_integer`'s default maximum, `2**63 - 1` (:312).
const INTEGER_MAX: i128 = i64::MAX as i128;
/// `_number`'s default maximum, `1e18` (:320), as the exact int it equals.
const NUMBER_MAX: f64 = 1e18;
const NUMBER_MAX_INT: i128 = 1_000_000_000_000_000_000;
/// `len(sources) > 64` (:1210).
const SOURCES_MAX: usize = 64;
/// `len(history_raw) > 256` (:1260).
const LEDGER_HISTORY_MAX: usize = 256;
/// `{0,127}` after one leading character in `_IDENTIFIER` and `_REASON` (:42, :44).
const PATTERN_MAX: usize = 128;

const STATUS_FIELDS: [&str; 40] = [
    "protocol",
    "generation",
    "acquisition_id",
    "state",
    "phase",
    "model_id",
    "model_revision",
    "representation",
    "assignment_id",
    "placement_id",
    "stage_id",
    "layer_start",
    "layer_end_exclusive",
    "total_bytes",
    "cached_verified_bytes",
    "transferred_verified_bytes",
    "missing_bytes",
    "quarantined_bytes",
    "duplicate_bytes_prevented",
    "eligible_source_count",
    "active_source_count",
    "sources",
    "origin_bytes",
    "aggregate_bytes_per_second",
    "eta_seconds",
    "chunk_count",
    "verified_chunk_count",
    "resumed_chunk_count",
    "source_rotation_count",
    "manifest_digest",
    "assignment_digest",
    "representation_digest",
    "feasibility_digest",
    "evidence_generation",
    "promotion_digest",
    "reason_code",
    "retryable",
    "started_at_unix_ms",
    "updated_at_unix_ms",
    "terminal_at_unix_ms",
];
const SOURCE_FIELDS: [&str; 3] = ["source_ref", "state", "verified_bytes"];

fn invalid() -> Error {
    Error::new(STATUS_INVALID)
}

/// A registered count limit of the oracle (crates/budgets).
fn limit(id: B) -> Result<u64> {
    id.count()
        .map(Entries::get)
        .map_err(|_| Error::new("artifact_policy_invalid"))
}

fn limit_usize(id: B) -> Result<usize> {
    usize::try_from(limit(id)?).map_err(|_| Error::new("artifact_policy_invalid"))
}

impl PyText {
    fn is(&self, text: &str) -> bool {
        self.0.iter().copied().eq(text.chars().map(u32::from))
    }

    /// The text when every code point is ASCII (every pattern here is ASCII-only).
    fn ascii(&self) -> Option<Vec<u8>> {
        self.0
            .iter()
            .map(|p| u8::try_from(*p).ok().filter(u8::is_ascii))
            .collect()
    }
}

impl AcquisitionState {
    fn parse(text: &str) -> Option<Self> {
        use AcquisitionState::*;
        [
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
        ]
        .into_iter()
        .find(|s| s.as_str() == text)
    }

    /// `_TERMINAL_STATES` (:72).
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Ready | Self::Cancelled | Self::Failed)
    }
}

impl SourceState {
    fn parse(text: &str) -> Option<Self> {
        use SourceState::*;
        [Eligible, Active, Rotated, Lost, Origin]
            .into_iter()
            .find(|s| s.as_str() == text)
    }
}

/// `_closed`: a mapping whose key set is exactly `fields`.
fn closed<'a>(value: &'a PyValue, fields: &[&str]) -> Result<&'a [(PyText, PyValue)]> {
    let PyValue::Dict(entries) = value else {
        return Err(invalid());
    };
    let exact = entries
        .iter()
        .all(|(key, _)| fields.iter().any(|f| key.is(f)))
        && fields
            .iter()
            .all(|f| entries.iter().any(|(key, _)| key.is(f)));
    if !exact {
        return Err(invalid());
    }
    Ok(entries)
}

/// `value.get(name)` of a closed mapping (the last entry, as a `dict` built from pairs keeps).
fn field<'a>(entries: &'a [(PyText, PyValue)], name: &str) -> &'a PyValue {
    const ABSENT: &PyValue = &PyValue::None;
    entries
        .iter()
        .rev()
        .find(|(key, _)| key.is(name))
        .map_or(ABSENT, |(_, value)| value)
}

/// `x in frozenset(...)`: a list or dict is unhashable (`TypeError`); any other non-member is
/// simply not in the set.
fn member<T>(value: &PyValue, parse: fn(&str) -> Option<T>) -> Result<Option<T>> {
    match value {
        PyValue::List(_) | PyValue::Dict(_) => Err(Error::new("TypeError")),
        PyValue::Str(text) => Ok(text.to_text().and_then(|t| parse(&t))),
        _ => Ok(None),
    }
}

/// `_integer`: exactly an `int` (never a `bool`), `minimum <= value <= 2**63 - 1`.
fn integer(value: &PyValue, minimum: u64) -> Result<u64> {
    match value {
        PyValue::Int(n) => match n.exact() {
            Some(v) if i128::from(minimum) <= v && v <= INTEGER_MAX => {
                u64::try_from(v).map_err(|_| invalid())
            }
            _ => Err(invalid()),
        },
        _ => Err(invalid()),
    }
}

/// `_number`: an `int` or `float` (never a `bool`), finite, `0.0 <= value <= 1e18`. The value
/// keeps its type. `math.isfinite` of an int past the double range raises `OverflowError`.
fn number(value: &PyValue) -> Result<PyNumber> {
    match value {
        PyValue::Int(n) => {
            if !n.to_f64().is_finite() {
                return Err(Error::new("OverflowError"));
            }
            match n.exact() {
                Some(v) if (0..=NUMBER_MAX_INT).contains(&v) => {
                    u64::try_from(v).map(PyNumber::Int).map_err(|_| invalid())
                }
                _ => Err(invalid()),
            }
        }
        PyValue::Float(x) if x.is_finite() && (0.0..=NUMBER_MAX).contains(x) => {
            Ok(PyNumber::Float(*x))
        }
        _ => Err(invalid()),
    }
}

/// `_text`: a non-empty `str` of at most `maximum` code points.
fn text(value: &PyValue, maximum: usize) -> Result<&PyText> {
    match value {
        PyValue::Str(t) if !t.0.is_empty() && t.0.len() <= maximum => Ok(t),
        _ => Err(invalid()),
    }
}

/// An ASCII `str` (`isinstance(value, str)`) matched by `rule`.
fn pattern(value: &PyValue, rule: impl Fn(&[u8]) -> bool) -> Result<String> {
    match value {
        PyValue::Str(t) => t
            .ascii()
            .filter(|b| rule(b))
            .and_then(|b| String::from_utf8(b).ok())
            .ok_or_else(invalid),
        _ => Err(invalid()),
    }
}

fn lower_hex(bytes: &[u8]) -> bool {
    bytes
        .iter()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
}

/// `_identifier` (:297-301): `_text(maximum=128)`, then `_IDENTIFIER` (:42).
fn identifier(value: &PyValue, maximum: usize) -> Result<String> {
    text(value, maximum)?;
    pattern(value, |b| {
        b.len() <= PATTERN_MAX
            && b.first().is_some_and(u8::is_ascii_alphanumeric)
            && b[1..]
                .iter()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b':' | b'-'))
    })
}

/// `_digest` (:304-308): `_text(maximum=71)`, then `_DIGEST` (:40).
fn digest(value: &PyValue, maximum: usize) -> Result<String> {
    text(value, maximum)?;
    pattern(value, |b| std::str::from_utf8(b).is_ok_and(sha_ref))
}

/// `_REASON` (:44): `[a-z][a-z0-9_]{0,127}`.
fn reason_grammar(b: &[u8]) -> bool {
    b.len() <= PATTERN_MAX
        && b.first().is_some_and(u8::is_ascii_lowercase)
        && b[1..]
            .iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_')
}

/// `validate_acquisition_status` (mycelium_swarm_artifacts.py:1119-1247), check for check in the
/// oracle's order, so the first failing check decides between a refusal and a builtin error.
pub fn validate_acquisition_status(document: &PyValue) -> Result<AcquisitionStatus> {
    let entries = closed(document, &STATUS_FIELDS)?;
    let get = |name: &str| field(entries, name);
    let protocol = matches!(get("protocol"), PyValue::Str(t) if t.is(ACQUISITION_PROTOCOL));
    if !protocol {
        return Err(invalid());
    }
    let state = member(get("state"), AcquisitionState::parse)?.ok_or_else(invalid)?;
    let generation = integer(get("generation"), limit(B::PyKbSwarmLowerL1126C85)?)?;
    let identifier_max = limit_usize(B::PyKbSwarmUpperL298C38)?;
    let acquisition_id = identifier(get("acquisition_id"), identifier_max)?;
    let assignment_id = identifier(get("assignment_id"), identifier_max)?;
    let placement_id = identifier(get("placement_id"), identifier_max)?;
    let stage_id = identifier(get("stage_id"), identifier_max)?;
    let model_id = text(get("model_id"), TEXT_MAX)?.clone();
    let model_revision = pattern(get("model_revision"), |b| b.len() == 40 && lower_hex(b))?;
    let representation = text(
        get("representation"),
        limit_usize(B::PyKbSwarmUpperL1136C84)?,
    )?
    .clone();
    let layer_start = integer(get("layer_start"), 0)?;
    let layer_end_exclusive = integer(
        get("layer_end_exclusive"),
        limit(B::PyKbSwarmLowerL1142C16)?,
    )?;
    if layer_end_exclusive <= layer_start {
        return Err(invalid());
    }
    let total_bytes = integer(get("total_bytes"), 0)?;
    let cached_verified_bytes = integer(get("cached_verified_bytes"), 0)?;
    let transferred_verified_bytes = integer(get("transferred_verified_bytes"), 0)?;
    let missing_bytes = integer(get("missing_bytes"), 0)?;
    let quarantined_bytes = integer(get("quarantined_bytes"), 0)?;
    let duplicate_bytes_prevented = integer(get("duplicate_bytes_prevented"), 0)?;
    let origin_bytes = integer(get("origin_bytes"), 0)?;
    let eligible_source_count = integer(get("eligible_source_count"), 0)?;
    let active_source_count = integer(get("active_source_count"), 0)?;
    let chunk_count = integer(get("chunk_count"), 0)?;
    let verified_chunk_count = integer(get("verified_chunk_count"), 0)?;
    let resumed_chunk_count = integer(get("resumed_chunk_count"), 0)?;
    let source_rotation_count = integer(get("source_rotation_count"), 0)?;
    let evidence_generation = integer(get("evidence_generation"), 0)?;
    let aggregate_bytes_per_second = number(get("aggregate_bytes_per_second"))?;
    let eta_seconds = match get("eta_seconds") {
        PyValue::None => None,
        eta => Some(number(eta)?),
    };
    let digest_max = limit_usize(B::PyKbSwarmUpperL305C38)?;
    let manifest_digest = digest(get("manifest_digest"), digest_max)?;
    let assignment_digest = digest(get("assignment_digest"), digest_max)?;
    let representation_digest = digest(get("representation_digest"), digest_max)?;
    let feasibility_digest = digest(get("feasibility_digest"), digest_max)?;
    let promotion_digest = match get("promotion_digest") {
        PyValue::None => None,
        promotion => Some(digest(promotion, digest_max)?),
    };
    let reason_code = match get("reason_code") {
        PyValue::None => None,
        reason => Some(pattern(reason, reason_grammar)?),
    };
    let PyValue::Bool(retryable) = *get("retryable") else {
        return Err(invalid());
    };
    let started_minimum = B::PyKbSwarmLowerL1194C16
        .span(Entries::ZERO)
        .map_err(|_| Error::new("artifact_policy_invalid"))?
        .millis();
    let started_at_unix_ms = integer(get("started_at_unix_ms"), started_minimum)?;
    let updated_at_unix_ms = integer(get("updated_at_unix_ms"), started_at_unix_ms)?;
    let terminal_at_unix_ms = match get("terminal_at_unix_ms") {
        PyValue::None => None,
        terminal => Some(integer(terminal, updated_at_unix_ms)?),
    };
    let phase = match get("phase") {
        PyValue::None => None,
        PyValue::Str(t) => Some(
            t.to_text()
                .and_then(|t| AcquisitionState::parse(&t))
                .ok_or_else(invalid)?,
        ),
        _ => return Err(invalid()),
    };
    let PyValue::List(raw_sources) = get("sources") else {
        return Err(invalid());
    };
    if raw_sources.len() > SOURCES_MAX {
        return Err(invalid());
    }
    let mut sources = Vec::with_capacity(raw_sources.len());
    let mut source_bytes: u128 = 0;
    for raw in raw_sources {
        let source = closed(raw, &SOURCE_FIELDS)?;
        let source_ref = pattern(field(source, "source_ref"), |b| {
            b.len() == 19 && b.starts_with(b"source-") && lower_hex(&b[7..])
        })?;
        let state = member(field(source, "state"), SourceState::parse)?.ok_or_else(invalid)?;
        let verified_bytes = integer(field(source, "verified_bytes"), 0)?;
        source_bytes += u128::from(verified_bytes);
        sources.push(SourceStatus {
            source_ref,
            state,
            verified_bytes,
        });
    }
    if !sources
        .windows(2)
        .all(|w| w[0].source_ref < w[1].source_ref)
    {
        return Err(invalid());
    }
    let sum = |values: &[u64]| values.iter().map(|v| u128::from(*v)).sum::<u128>();
    if sum(&[
        cached_verified_bytes,
        transferred_verified_bytes,
        missing_bytes,
    ]) != u128::from(total_bytes)
        || verified_chunk_count > chunk_count
        || resumed_chunk_count > verified_chunk_count
        || active_source_count > eligible_source_count
        || active_source_count > sources.len() as u64
        || source_bytes + u128::from(origin_bytes) != u128::from(transferred_verified_bytes)
        || state.is_terminal() != terminal_at_unix_ms.is_some()
        || (state == AcquisitionState::Ready) != promotion_digest.is_some()
        || (state == AcquisitionState::Failed) != reason_code.is_some()
        || (state.is_terminal() && phase.is_some())
    {
        return Err(invalid());
    }
    Ok(AcquisitionStatus {
        generation,
        acquisition_id,
        state,
        phase,
        model_id,
        model_revision,
        representation,
        assignment_id,
        placement_id,
        stage_id,
        layer_start,
        layer_end_exclusive,
        total_bytes,
        cached_verified_bytes,
        transferred_verified_bytes,
        missing_bytes,
        quarantined_bytes,
        duplicate_bytes_prevented,
        eligible_source_count,
        active_source_count,
        sources,
        origin_bytes,
        aggregate_bytes_per_second,
        eta_seconds,
        chunk_count,
        verified_chunk_count,
        resumed_chunk_count,
        source_rotation_count,
        manifest_digest,
        assignment_digest,
        representation_digest,
        feasibility_digest,
        evidence_generation,
        promotion_digest,
        reason_code,
        retryable,
        started_at_unix_ms,
        updated_at_unix_ms,
        terminal_at_unix_ms,
    })
}

/// `validate_acquisition_ledger` (mycelium_swarm_artifacts.py:1250-1278) over statuses that are
/// already validated (the store only builds ledgers from validated statuses).
fn checked_ledger(
    generation: u64,
    current: Option<AcquisitionStatus>,
    history: Vec<AcquisitionStatus>,
) -> Result<AcquisitionLedger> {
    if history.len() > LEDGER_HISTORY_MAX {
        return Err(Error::new(LEDGER_INVALID));
    }
    let all: Vec<&AcquisitionStatus> = history.iter().chain(current.iter()).collect();
    let ids: HashSet<&str> = all.iter().map(|s| s.acquisition_id.as_str()).collect();
    let newest = all.iter().map(|s| s.generation).max().unwrap_or(0);
    if current.as_ref().is_some_and(|c| c.state.is_terminal())
        || !history.iter().all(|s| s.state.is_terminal())
        || !history
            .windows(2)
            .all(|w| w[0].generation < w[1].generation)
        || ids.len() != all.len()
        || generation != newest
    {
        return Err(Error::new(LEDGER_INVALID));
    }
    Ok(AcquisitionLedger {
        generation,
        current,
        history,
    })
}

fn os_error(e: std::io::Error) -> Error {
    Error::io("OSError", e)
}

/// The codes the store's `except (OSError, UnicodeError, ValueError, json.JSONDecodeError)`
/// turns into `code`; `TypeError` and `OverflowError` are not caught there and pass through, as
/// do the port's two document bounds.
fn caught(code: &'static str) -> impl Fn(Error) -> Error {
    move |e| {
        if matches!(
            e.code,
            "TypeError" | "OverflowError" | DOCUMENT_TOO_DEEP | DOCUMENT_TOO_LARGE
        ) {
            e
        } else {
            Error {
                code,
                io: e.io,
                cleanup_io: e.cleanup_io,
            }
        }
    }
}

/// `path.read_text()` of a stored document, refused as `artifact_document_too_large` past
/// [`MAX_DOCUMENT_BYTES`] without reading more than one byte beyond it; an I/O error is `code`.
fn read_document(path: &Path, code: &'static str) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| file.take(MAX_DOCUMENT_BYTES + 1).read_to_end(&mut bytes))
        .map_err(|e| Error::io(code, e))?;
    if bytes.len() as u64 > MAX_DOCUMENT_BYTES {
        return Err(Error::new(DOCUMENT_TOO_LARGE));
    }
    Ok(bytes)
}

fn now_unix_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

static NEXT: AtomicU64 = AtomicU64::new(0);

/// 32 hex digits for a temporary name, as `uuid.uuid4().hex` gives (std's per-instance random
/// SipHash keys plus a process counter).
fn token() -> String {
    let state = RandomState::new();
    let mut words = [0u64; 2];
    for (i, word) in words.iter_mut().enumerate() {
        let mut hasher = state.build_hasher();
        hasher.write_u64(NEXT.fetch_add(1, Ordering::Relaxed));
        hasher.write_u32(std::process::id());
        hasher.write_usize(i);
        *word = hasher.finish();
    }
    format!("{:016x}{:016x}", words[0], words[1])
}

fn sync_directory(directory: &Path) -> Result<()> {
    File::open(directory)
        .and_then(|d| d.sync_all())
        .map_err(os_error)
}

/// `_atomic_json` (artifact_provisioner.py:112-123): a fresh `.name.<hex>.tmp` beside the
/// destination (created first, `open("xb")`), the canonical bytes, fsync, mode 0600, rename,
/// fsync of the directory; the temporary is removed whatever happens, and an error removing it
/// replaces the one in flight, as the `finally` does. Only a temporary this call created is
/// removed. Both syncs are `File::sync_all` (F_FULLFSYNC on macOS), the crate cache's practice,
/// where the oracle calls `os.fsync` (`rustix::fs::fsync` would be that call); the outcome is
/// the same on APFS.
fn atomic_json(destination: &Path, document: &PyValue) -> Result<()> {
    let mut name = std::ffi::OsString::from(".");
    name.push(destination.file_name().unwrap_or_default());
    name.push(format!(".{}.tmp", token()));
    let temporary = destination.with_file_name(name);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o666)
        .open(&temporary)
        .map_err(os_error)?;
    let result = (|| {
        let bytes = canonical(document)?;
        file.write_all(&bytes)
            .and_then(|()| file.flush())
            .and_then(|()| file.sync_all())
            .map_err(os_error)?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600)).map_err(os_error)?;
        fs::rename(&temporary, destination).map_err(os_error)?;
        sync_directory(destination.parent().unwrap_or(Path::new("/")))
    })();
    drop(file);
    match fs::remove_file(&temporary) {
        Ok(()) => result,
        Err(e) if e.kind() == ErrorKind::NotFound => result,
        Err(e) => Err(os_error(e)),
    }
}

/// `Path(root)` as pathlib spells it: empty and `.` parts dropped, `..` kept, and exactly two
/// leading slashes kept (POSIX leaves `//` implementation-defined; a resolved path never has it).
fn pure_path(root: &Path) -> PathBuf {
    let bytes = root.as_os_str().as_bytes();
    let anchor: &[u8] = if bytes.starts_with(b"//") && !bytes.starts_with(b"///") {
        b"//"
    } else if bytes.starts_with(b"/") {
        b"/"
    } else {
        b""
    };
    let parts: Vec<&[u8]> = bytes
        .split(|b| *b == b'/')
        .filter(|p| !p.is_empty() && *p != b".")
        .collect();
    let mut out = anchor.to_vec();
    out.extend_from_slice(&parts.join(&b'/'));
    PathBuf::from(std::ffi::OsString::from_vec(out))
}

fn raw_os_error(code: i32) -> Error {
    os_error(std::io::Error::from_raw_os_error(code))
}

/// `os.path.realpath(path, strict=True)` (CPython 3.14 posixpath): every component is
/// `lstat`ed and a link is followed by `readlink`, with the oracle's loop detection. Unlike
/// realpath(3) it never changes a name's case or normalisation.
fn realpath(path: &Path) -> Result<Vec<u8>> {
    enum Part {
        Name(Vec<u8>),
        Resolved(Vec<u8>),
    }
    fn split(text: &[u8]) -> Vec<Part> {
        text.split(|b| *b == b'/')
            .rev()
            .map(|p| Part::Name(p.to_vec()))
            .collect()
    }
    let mut rest = split(path.as_os_str().as_bytes());
    let mut pending = rest.len();
    let mut resolved = b"/".to_vec();
    let mut seen: HashMap<Vec<u8>, Option<Vec<u8>>> = HashMap::new();
    while pending > 0 {
        let Some(part) = rest.pop() else { break };
        let name = match part {
            Part::Resolved(link) => {
                seen.insert(link, Some(resolved.clone()));
                continue;
            }
            Part::Name(name) => name,
        };
        pending -= 1;
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            let cut = resolved.iter().rposition(|b| *b == b'/').unwrap_or(0);
            resolved.truncate(cut.max(1));
            continue;
        }
        let mut next = resolved.clone();
        if next != b"/" {
            next.push(b'/');
        }
        next.extend_from_slice(&name);
        let next_path = Path::new(OsStr::from_bytes(&next));
        let meta = fs::symlink_metadata(next_path).map_err(os_error)?;
        if !meta.file_type().is_symlink() {
            if pending > 0 && !meta.is_dir() {
                return Err(raw_os_error(libc::ENOTDIR));
            }
            resolved = next;
            continue;
        }
        match seen.get(&next) {
            Some(Some(target)) => {
                resolved = target.clone();
                continue;
            }
            Some(None) => return Err(raw_os_error(libc::ELOOP)),
            None => {}
        }
        let target = fs::read_link(next_path)
            .map_err(os_error)?
            .into_os_string()
            .into_vec();
        if target.starts_with(b"/") {
            resolved = b"/".to_vec();
        }
        seen.insert(next.clone(), None);
        rest.push(Part::Resolved(next));
        let parts = split(&target);
        pending += parts.len();
        rest.extend(parts);
    }
    Ok(resolved)
}

/// `Path.mkdir(mode, parents=parents, exist_ok=True)` (CPython 3.14 pathlib): a missing parent
/// is created with the default mode when `parents`; an existing directory (links followed) is
/// fine, anything else is the `OSError`.
fn make_directory(path: &Path, mode: u32, parents: bool) -> Result<()> {
    match fs::DirBuilder::new().mode(mode).create(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == ErrorKind::NotFound => match path.parent() {
            Some(parent) if parents && parent != path => {
                make_directory(parent, 0o777, true)?;
                make_directory(path, mode, false)
            }
            _ => Err(os_error(e)),
        },
        Err(e) if path.is_dir() => {
            drop(e);
            Ok(())
        }
        Err(e) => Err(os_error(e)),
    }
}

/// `_private_root` (artifact_provisioner.py:126-147).
fn private_root(root: &Path) -> Result<PathBuf> {
    let candidate = pure_path(root);
    if !candidate.is_absolute() {
        return Err(Error::new(ROOT_UNSAFE));
    }
    make_directory(&candidate, 0o700, true)?;
    let meta = fs::symlink_metadata(&candidate).map_err(os_error)?;
    if !meta.is_dir()
        || meta.file_type().is_symlink()
        || meta.uid() != rustix::process::geteuid().as_raw()
        || meta.mode() & 0o022 != 0
    {
        return Err(Error::new(ROOT_UNSAFE));
    }
    if realpath(&candidate)? != candidate.as_os_str().as_bytes() {
        return Err(Error::new(ROOT_UNSAFE));
    }
    for name in ["objects", "partials", "quarantine", "promoted", "state"] {
        make_directory(&candidate.join(name), 0o700, false)?;
    }
    Ok(candidate)
}

/// A validated ledger (`validate_acquisition_ledger`, :1250-1278).
#[derive(Clone, Debug, PartialEq)]
pub struct AcquisitionLedger {
    pub generation: u64,
    pub current: Option<AcquisitionStatus>,
    pub history: Vec<AcquisitionStatus>,
}

/// `ArtifactAcquisitionStore`'s current/history subset: `state/current.json` holds the one
/// acquisition in progress, `state/history.json` the last `history_limit` terminal ones.
#[derive(Debug)]
pub struct ArtifactAcquisitionStore {
    root: PathBuf,
    history_limit: usize,
    current_path: PathBuf,
    history_path: PathBuf,
    public: Option<AcquisitionLedger>,
}

impl ArtifactAcquisitionStore {
    /// `ArtifactAcquisitionStore.__init__` (artifact_provisioner.py:215-228): check the limit
    /// (`ValueError`), prepare the private root, recover an interrupted acquisition, then
    /// reread the whole ledger.
    pub fn open(root: &Path, history_limit: u64) -> Result<Self> {
        let floor = limit(B::PyRangeFloorMyceliumLiveArtifactProvisionerL216C49)?;
        let ceiling = limit(B::PyUpperGuardMyceliumLiveArtifactProvisionerL216C49)?;
        if !(floor..=ceiling).contains(&history_limit) {
            return Err(Error::new("ValueError"));
        }
        let root = private_root(root)?;
        let state = root.join("state");
        let mut store = Self {
            current_path: state.join("current.json"),
            history_path: state.join("history.json"),
            root,
            history_limit: usize::try_from(history_limit).map_err(|_| Error::new("ValueError"))?,
            public: None,
        };
        store.recover_interrupted()?;
        store.public = Some(store.ledger()?);
        Ok(store)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// `write_current` (:369-388): validate, write `current.json`, then validate the new
    /// in-memory ledger (a terminal or duplicate status is refused after the write, as there).
    pub fn write_current(&mut self, status: &PyValue) -> Result<()> {
        let validated = validate_acquisition_status(status)?;
        atomic_json(&self.current_path, &validated.to_value())?;
        let history = match &self.public {
            Some(ledger) => ledger.history.clone(),
            None => self.read_history()?,
        };
        let generation = history
            .iter()
            .map(|s| s.generation)
            .chain([validated.generation])
            .max()
            .unwrap_or(validated.generation);
        self.public = Some(checked_ledger(generation, Some(validated), history)?);
        Ok(())
    }

    /// `_read_history` (:390-399).
    fn read_history(&self) -> Result<Vec<AcquisitionStatus>> {
        if !self.history_path.exists() {
            return Ok(Vec::new());
        }
        let bytes = read_document(&self.history_path, HISTORY_CORRUPT)?;
        let PyValue::List(items) = loads(&bytes).map_err(caught(HISTORY_CORRUPT))? else {
            return Err(Error::new(HISTORY_CORRUPT));
        };
        items
            .iter()
            .map(|item| validate_acquisition_status(item).map_err(caught(HISTORY_CORRUPT)))
            .collect()
    }

    /// `finish` (:401-419): append a terminal status to history (keeping the last
    /// `history_limit`), remove `current.json`, then validate the new in-memory ledger.
    pub fn finish(&mut self, status: &PyValue) -> Result<()> {
        let validated = validate_acquisition_status(status)?;
        if validated.terminal_at_unix_ms.is_none() {
            return Err(Error::new(TERMINAL_INVALID));
        }
        let mut history = self.read_history()?;
        history.push(validated);
        let excess = history.len().saturating_sub(self.history_limit);
        history.drain(..excess);
        let document = PyValue::List(history.iter().map(AcquisitionStatus::to_value).collect());
        atomic_json(&self.history_path, &document)?;
        // `unlink(missing_ok=True)`: only an already-absent current.json is fine.
        if let Err(e) = fs::remove_file(&self.current_path)
            && e.kind() != ErrorKind::NotFound
        {
            return Err(os_error(e));
        }
        sync_directory(&self.root.join("state"))?;
        let generation = history.iter().map(|s| s.generation).max().unwrap_or(0);
        self.public = Some(checked_ledger(generation, None, history)?);
        Ok(())
    }

    fn read_current(&self) -> Result<AcquisitionStatus> {
        let bytes = read_document(&self.current_path, STATE_CORRUPT)?;
        let value = loads(&bytes).map_err(caught(STATE_CORRUPT))?;
        validate_acquisition_status(&value).map_err(caught(STATE_CORRUPT))
    }

    /// `ledger` (:441-466): reread and validate `current.json` (`artifact_state_corrupt`) and
    /// `history.json` (`artifact_history_corrupt`), then the ledger as a whole.
    pub fn ledger(&self) -> Result<AcquisitionLedger> {
        let current = if self.current_path.exists() {
            Some(self.read_current()?)
        } else {
            None
        };
        let history = self.read_history()?;
        let generation = history
            .iter()
            .chain(current.iter())
            .map(|s| s.generation)
            .max()
            .unwrap_or(0);
        checked_ledger(generation, current, history)
    }

    /// `public_ledger` (:468-474): the last validated snapshot, without rereading storage.
    pub fn public_ledger(&self) -> Result<AcquisitionLedger> {
        match &self.public {
            Some(ledger) => Ok(ledger.clone()),
            None => self.ledger(),
        }
    }

    /// `_recover_interrupted` (:476-500): a terminal `current.json` is finished as it is; any
    /// other becomes `failed`/`interrupted_transfer`, retryable, updated and terminal at
    /// `max(now, updated)`, and is finished.
    fn recover_interrupted(&mut self) -> Result<()> {
        if !self.current_path.exists() {
            return Ok(());
        }
        let mut current = self.read_current()?;
        if current.terminal_at_unix_ms.is_none() {
            let at = now_unix_ms().max(current.updated_at_unix_ms);
            current.state = AcquisitionState::Failed;
            current.phase = None;
            current.reason_code = Some("interrupted_transfer".to_owned());
            current.retryable = true;
            current.updated_at_unix_ms = at;
            current.terminal_at_unix_ms = Some(at);
        }
        self.finish(&current.to_value())
    }
}
