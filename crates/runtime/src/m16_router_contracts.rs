// SPDX-License-Identifier: AGPL-3.0-only
//! The router contracts the M16 route scorer and path builder read (mycelium_router/contracts.py
//! @ f4e366e9): `DeviceState` (:107-121), `RequestContext` (:124-136), `RouterConfig` (:139-182),
//! `ScoreBreakdown` (:211-219), and the path records `PathHop` (:84-90), `PathManifest` (:93-104,
//! with `PATH_MANIFEST_PROTOCOL`, :7), `BranchDecision` (:222-226) and `PathBuildState`
//! (:229-241), and the hop scheduler's records `RuntimeBatchKey` (:244-260), `BatchDecision`
//! (:285-295), `HopWorkItem` (:298-318) and `RuntimeBatch` (:321-333) with `hop_identity`
//! (mycelium_router/idempotency.py:98-111). The reservation records are the ledger's own
//! (`crate::m16_ledger`). No admission record is here.
//!
//! These are Python dataclasses that check no field, and the M16 coordinator passes them on as
//! they were built (mycelium_m16_runtime.py:348-433 and :1163-1262): the factory's DeviceStates
//! carry floats (:1204-1217), the gateway's RequestContext floats
//! (mycelium_request_gateway/backend.py:227-239), the graph its JSON numbers, and the factory's
//! RouterConfig numbers (:1260; a numeric-only RouterConfig is a declared V03 limit, see
//! [`RouterConfig`]). So every number field is a [`PyReal`] (a `bool`, an `int` of any size and
//! sign, or an IEEE double), and the scorer
//! computes on it as CPython does: ints are exact, an int meets a float through `float()`
//! (correctly rounded half-even, OverflowError past the double range), `int / int` is CPython's
//! correctly rounded true division, int-versus-float comparison is exact, and builtin `min`/`max`
//! keep their first argument unless a later one compares strictly below/above it (so a NaN
//! argument is kept only in first place). Node ids are the graph's JSON values used as dict keys
//! ([`IdMap`]): Python-equal ids are one key (`1 == 1.0 == True`, `"1" != 1`), and a list or dict
//! id is unhashable (TypeError).
use crate::m16_capacity::{PyInt, PyNumber};
use budgets::{BudgetId, Entries};
use protocol::execution_graph::Graph;
use serde_json::Value;
use std::cmp::Ordering;
use std::fmt;

/// A Python number as it reaches the scorer.
#[derive(Clone)]
pub enum PyReal {
    /// A `bool`: arithmetic treats it as the int 0 or 1, but it keeps its type.
    Bool(bool),
    /// An `int` of any size: its sign and magnitude (zero is never negative).
    Int { negative: bool, magnitude: PyInt },
    /// A `float`.
    Float(f64),
}

impl PyReal {
    /// The int `n`.
    pub fn int(n: i64) -> Self {
        Self::signed(n < 0, PyInt::from(n.unsigned_abs()))
    }

    /// The int with this sign and magnitude (a zero is not negative).
    pub fn signed(negative: bool, magnitude: PyInt) -> Self {
        let negative = negative && magnitude != PyInt::default();
        Self::Int {
            negative,
            magnitude,
        }
    }

    /// A JSON value as Python's json module reads it: a literal with `.`, `e` or `E` is a
    /// `float` (`1e400` is `inf`), any other number an `int`, `true`/`false` a `bool`; `None`
    /// for anything that is not a number.
    pub fn from_json(v: &Value) -> Option<Self> {
        match v {
            Value::Bool(b) => Some(Self::Bool(*b)),
            Value::Number(number) => {
                let text = number.to_string();
                if text.contains(['.', 'e', 'E']) {
                    return text.parse().ok().map(Self::Float);
                }
                let (negative, digits) = text
                    .strip_prefix('-')
                    .map_or((false, text.as_str()), |digits| (true, digits));
                digits
                    .parse()
                    .ok()
                    .map(|magnitude| Self::signed(negative, magnitude))
            }
            _ => None,
        }
    }
}

impl From<PyNumber> for PyReal {
    fn from(n: PyNumber) -> Self {
        match n {
            PyNumber::Int(magnitude) => Self::signed(false, magnitude),
            PyNumber::Float(x) => Self::Float(x),
        }
    }
}

/// Same Python type and same value; floats compare bit for bit (output identity, not `==`).
impl PartialEq for PyReal {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Bool(a), Self::Bool(b)) => a == b,
            (
                Self::Int {
                    negative: a,
                    magnitude: m,
                },
                Self::Int {
                    negative: b,
                    magnitude: n,
                },
            ) => m == n && (*a && *m != PyInt::default()) == (*b && *n != PyInt::default()),
            (Self::Float(a), Self::Float(b)) => a.to_bits() == b.to_bits(),
            _ => false,
        }
    }
}
impl Eq for PyReal {}

impl fmt::Debug for PyReal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Bool(true) => f.write_str("True"),
            Self::Bool(false) => f.write_str("False"),
            Self::Int {
                negative,
                magnitude,
            } => write!(f, "{}{magnitude}", if *negative { "-" } else { "" }),
            Self::Float(x) => write!(f, "{x:?}"),
        }
    }
}

/// A Python `str` as `json.loads` decodes one from a peer's frame (mycelium_router/wire.py:203,
/// the header's strict UTF-8 text): its code points in order, each at most U+10FFFF. CPython's
/// scanner (`scanstring_unicode`, Modules/_json.c @ v3.14.2) joins an escaped high surrogate
/// directly followed by an escaped low one into one code point (`"\ud83d\ude00"` is the literal
/// U+1F600) and keeps every other `\ud800`-`\udfff` escape as a lone surrogate code point
/// (`"\ud800"`, the reversed `"\udc00\ud800"`), which a Rust `String` cannot hold. `==` is
/// CPython's str equality (the same code points in the same order: `"\ud800" == "\uD800"`,
/// `"\ud800" != "\udfff"`, `"\ud83d" != "\ud83d\ude00"`, a lone surrogate never equals U+FFFD);
/// the order is its str comparison (code point by code point, a prefix first) and the hash runs
/// over the same code points, so the three agree.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct PyStr(Vec<u32>);

impl PyStr {
    /// The `str` of `text`'s characters.
    pub fn new(text: &str) -> Self {
        Self(text.chars().map(u32::from).collect())
    }

    /// `"".join(map(chr, points))`; `None` for a point past U+10FFFF (`chr` raises ValueError).
    pub fn from_code_points(points: impl IntoIterator<Item = u32>) -> Option<Self> {
        let points: Vec<u32> = points.into_iter().collect();
        points
            .iter()
            .all(|point| *point <= 0x10_FFFF)
            .then_some(Self(points))
    }

    /// The code points in order.
    pub fn code_points(&self) -> &[u32] {
        &self.0
    }

    /// The same text as a `String`; `None` when it holds a surrogate code point.
    pub fn to_text(&self) -> Option<String> {
        self.0.iter().map(|point| char::from_u32(*point)).collect()
    }
}

/// Quoted, a surrogate code point as `\u{d800}`.
impl fmt::Debug for PyStr {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("\"")?;
        for point in &self.0 {
            match char::from_u32(*point) {
                Some(c) => write!(f, "{}", c.escape_debug())?,
                None => write!(f, "\\u{{{point:x}}}")?,
            }
        }
        f.write_str("\"")
    }
}

/// A value as Python's `json` module decodes a peer's frame (`json.loads`, mycelium_router/
/// wire.py:203; CPython 3.14.2's json.decoder): `null` is `None`; `true`/`false`, every number
/// literal and the constants `NaN`, `Infinity` and `-Infinity` are Python numbers, a
/// [`PyReal`] (a literal with `.`, `e` or `E` is a `float`, so `1e400` is `inf`; any other an
/// `int` of any size); a string is a `str`, a [`PyStr`] (lone surrogates included), an array a
/// `list` and an object a `dict` with [`PyStr`] keys in insertion order. Its `==` is output
/// identity (the same Python types and values in the same order, floats bit for bit, strings
/// code point by code point); Python's `==` on two such values is [`py_json_eq`].
#[derive(Clone, PartialEq, Eq)]
pub enum PyJson {
    None,
    Num(PyReal),
    Str(PyStr),
    List(Vec<PyJson>),
    /// The keys are distinct, as a `dict`'s are.
    Dict(Vec<(PyStr, PyJson)>),
}

impl PyJson {
    /// The `str` `text`.
    pub fn str(text: &str) -> Self {
        Self::Str(PyStr::new(text))
    }

    /// A standard JSON value as `json.loads` reads its text (numbers as [`PyReal::from_json`]
    /// reads them, an object's keys in the map's order); `None` for a number that is no Python
    /// literal.
    pub fn from_json(v: &Value) -> Option<Self> {
        Some(match v {
            Value::Null => Self::None,
            Value::Bool(_) | Value::Number(_) => Self::Num(PyReal::from_json(v)?),
            Value::String(text) => Self::str(text),
            Value::Array(items) => {
                Self::List(items.iter().map(Self::from_json).collect::<Option<_>>()?)
            }
            Value::Object(map) => Self::Dict(
                map.iter()
                    .map(|(key, item)| Some((PyStr::new(key), Self::from_json(item)?)))
                    .collect::<Option<_>>()?,
            ),
        })
    }
}

/// Python's `repr`.
impl fmt::Debug for PyJson {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::None => f.write_str("None"),
            Self::Num(n) => n.fmt(f),
            Self::Str(text) => text.fmt(f),
            Self::List(items) => f.debug_list().entries(items).finish(),
            Self::Dict(entries) => f
                .debug_map()
                .entries(entries.iter().map(|(key, item)| (key, item)))
                .finish(),
        }
    }
}

/// A Python exception raised by the seam arithmetic.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PyError {
    /// OverflowError "int too large to convert to float": `float(n)`, or an int meeting a float.
    IntTooLargeForFloat,
    /// OverflowError "integer division result too large for a float": `int / int`.
    IntDivisionTooLargeForFloat,
    /// ZeroDivisionError "division by zero".
    ZeroDivision,
}

/// A non-negative binary integer: little-endian base-2^32 limbs, no high zero limb.
#[derive(Clone, Default, PartialEq, Eq)]
struct Mag(Vec<u32>);

impl Mag {
    fn trimmed(mut limbs: Vec<u32>) -> Self {
        while limbs.last() == Some(&0) {
            limbs.pop();
        }
        Self(limbs)
    }

    fn small(n: u64) -> Self {
        Self::trimmed(vec![n as u32, (n >> 32) as u32])
    }

    fn is_zero(&self) -> bool {
        self.0.is_empty()
    }

    fn bits(&self) -> u64 {
        self.0.last().map_or(0, |top| {
            (self.0.len() as u64 - 1) * 32 + u64::from(u32::BITS - top.leading_zeros())
        })
    }

    /// `self = self * factor + addend`.
    fn mul_add_small(&mut self, factor: u32, addend: u32) {
        let mut carry = u64::from(addend);
        for limb in &mut self.0 {
            let value = u64::from(*limb) * u64::from(factor) + carry;
            *limb = value as u32;
            carry = value >> 32;
        }
        if carry > 0 {
            self.0.push(carry as u32);
        }
    }

    /// `(self // divisor, self % divisor)` for a positive divisor.
    fn div_small(&self, divisor: u32) -> (Self, u32) {
        let mut quotient = vec![0; self.0.len()];
        let mut remainder = 0u64;
        for (i, limb) in self.0.iter().enumerate().rev() {
            let current = (remainder << 32) | u64::from(*limb);
            quotient[i] = (current / u64::from(divisor)) as u32;
            remainder = current % u64::from(divisor);
        }
        (Self::trimmed(quotient), remainder as u32)
    }

    fn from_int(n: &PyInt) -> Self {
        let mut out = Self::default();
        for chunk in n.to_string().as_bytes().chunks(9) {
            let value = chunk
                .iter()
                .fold(0, |value, digit| value * 10 + u32::from(digit - b'0'));
            out.mul_add_small(10u32.pow(chunk.len() as u32), value);
        }
        out
    }

    fn to_int(&self) -> PyInt {
        let mut chunks = Vec::new();
        let mut rest = self.clone();
        while !rest.is_zero() {
            let (quotient, chunk) = rest.div_small(1_000_000_000);
            chunks.push(chunk);
            rest = quotient;
        }
        let mut text = chunks.last().map_or_else(|| "0".to_owned(), u32::to_string);
        for chunk in chunks.iter().rev().skip(1) {
            text.push_str(&format!("{chunk:09}"));
        }
        // ASCII digits, never empty: [`PyInt`]'s parser accepts every such text.
        text.parse().expect("decimal digits")
    }

    fn shl(&self, bits: u64) -> Self {
        if self.is_zero() {
            return Self::default();
        }
        let mut out = vec![0; (bits / 32) as usize];
        let shift = (bits % 32) as u32;
        if shift == 0 {
            out.extend_from_slice(&self.0);
        } else {
            let mut carry = 0;
            for limb in &self.0 {
                out.push((limb << shift) | carry);
                carry = limb >> (32 - shift);
            }
            out.push(carry);
        }
        Self::trimmed(out)
    }

    fn add(&self, other: &Self) -> Self {
        let mut out = Vec::with_capacity(self.0.len().max(other.0.len()) + 1);
        let mut carry = 0u64;
        for i in 0..self.0.len().max(other.0.len()) {
            let sum = u64::from(self.0.get(i).copied().unwrap_or(0))
                + u64::from(other.0.get(i).copied().unwrap_or(0))
                + carry;
            out.push(sum as u32);
            carry = sum >> 32;
        }
        out.push(carry as u32);
        Self::trimmed(out)
    }

    /// `self - other` for `self >= other`.
    fn sub(&self, other: &Self) -> Self {
        let mut out = Vec::with_capacity(self.0.len());
        let mut borrow = 0i64;
        for (i, limb) in self.0.iter().enumerate() {
            let mut value =
                i64::from(*limb) - i64::from(other.0.get(i).copied().unwrap_or(0)) - borrow;
            borrow = i64::from(value < 0);
            if value < 0 {
                value += 1 << 32;
            }
            out.push(value as u32);
        }
        Self::trimmed(out)
    }

    fn mul(&self, other: &Self) -> Self {
        if self.is_zero() || other.is_zero() {
            return Self::default();
        }
        let mut out = vec![0u32; self.0.len() + other.0.len()];
        for (i, a) in self.0.iter().enumerate() {
            let mut carry = 0u64;
            for (j, b) in other.0.iter().enumerate() {
                let value = u64::from(*a) * u64::from(*b) + u64::from(out[i + j]) + carry;
                out[i + j] = value as u32;
                carry = value >> 32;
            }
            out[i + other.0.len()] = carry as u32;
        }
        Self::trimmed(out)
    }
}

impl Ord for Mag {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0
            .len()
            .cmp(&other.0.len())
            .then_with(|| self.0.iter().rev().cmp(other.0.iter().rev()))
    }
}
impl PartialOrd for Mag {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// `2^e` for `-1022 <= e <= 1023`.
fn pow2(e: i64) -> f64 {
    f64::from_bits(((e + 1023) as u64) << 52)
}

/// `m * 2^e` for a result that is an exact double (`m <= 2^53`): scaled in exact steps.
fn scaled(m: u64, mut e: i64) -> f64 {
    let mut x = m as f64;
    while e != 0 {
        let step = e.clamp(-1000, 1000);
        x *= pow2(step);
        e -= step;
    }
    x
}

/// `a / b` for `b > 0`, correctly rounded half-even as CPython's `long_true_divide` and
/// `PyLong_AsDouble` round it, subnormals included; `None` where the rounded value is at least
/// `2^1024` (OverflowError).
fn divide(a: &Mag, b: &Mag) -> Option<f64> {
    if a.is_zero() {
        return Some(0.0);
    }
    // a/b lies in (2^(la-lb-1), 2^(la-lb+1)), so q = floor(a * 2^shift / b) lies in [2^55, 2^57).
    let shift = 56 + b.bits() as i64 - a.bits() as i64;
    let (mut rest, divisor) = if shift >= 0 {
        (a.shl(shift as u64), b.clone())
    } else {
        (a.clone(), b.shl(shift.unsigned_abs()))
    };
    let mut q = 0u64;
    for bit in (0..57).rev() {
        let part = divisor.shl(bit);
        if rest >= part {
            rest = rest.sub(&part);
            q |= 1 << bit;
        }
    }
    let sticky = !rest.is_zero();
    // a/b = (q + f) * 2^-shift with 0 <= f < 1 (f > 0 iff sticky). Keep 53 significant bits,
    // or fewer where the result falls below 2^-1022 (the subnormal grid is 2^-1074).
    let q_bits = i64::from(u64::BITS - q.leading_zeros());
    let drop = (q_bits - 53).max(shift - 1074);
    if drop > q_bits {
        return Some(0.0);
    }
    let drop = drop as u32;
    let mut m = q >> drop;
    let remainder = q & ((1 << drop) - 1);
    let half = 1 << (drop - 1);
    if remainder > half || (remainder == half && (sticky || m & 1 == 1)) {
        m += 1;
    }
    let exponent = i64::from(drop) - shift;
    if i64::from(u64::BITS - m.leading_zeros()) + exponent > 1024 {
        return None;
    }
    Some(scaled(m, exponent))
}

/// The arithmetic view of a [`PyReal`]: a `bool` is the int 0 or 1.
enum Num {
    Int(bool, Mag),
    Float(f64),
}

fn num(v: &PyReal) -> Num {
    match v {
        PyReal::Bool(b) => Num::Int(false, Mag::small(u64::from(*b))),
        PyReal::Int {
            negative,
            magnitude,
        } => {
            let magnitude = Mag::from_int(magnitude);
            Num::Int(*negative && !magnitude.is_zero(), magnitude)
        }
        PyReal::Float(x) => Num::Float(*x),
    }
}

fn int_result(negative: bool, magnitude: &Mag) -> PyReal {
    PyReal::signed(negative, magnitude.to_int())
}

/// `float(n)` for an int.
fn int_float(negative: bool, magnitude: &Mag) -> Result<f64, PyError> {
    let x = divide(magnitude, &Mag::small(1)).ok_or(PyError::IntTooLargeForFloat)?;
    Ok(if negative { -x } else { x })
}

fn as_float(n: Num) -> Result<f64, PyError> {
    match n {
        Num::Float(x) => Ok(x),
        Num::Int(negative, magnitude) => int_float(negative, &magnitude),
    }
}

/// Signed int addition.
fn int_add(a_negative: bool, a: &Mag, b_negative: bool, b: &Mag) -> PyReal {
    if a_negative == b_negative {
        int_result(a_negative, &a.add(b))
    } else if a >= b {
        int_result(a_negative, &a.sub(b))
    } else {
        int_result(b_negative, &b.sub(a))
    }
}

/// Python `float(v)`.
pub(crate) fn py_float(v: &PyReal) -> Result<f64, PyError> {
    as_float(num(v))
}

/// Python `a + b`.
pub(crate) fn py_add(a: &PyReal, b: &PyReal) -> Result<PyReal, PyError> {
    Ok(match (num(a), num(b)) {
        (Num::Int(an, am), Num::Int(bn, bm)) => int_add(an, &am, bn, &bm),
        (a, b) => PyReal::Float(as_float(a)? + as_float(b)?),
    })
}

/// Python `a - b`.
pub(crate) fn py_sub(a: &PyReal, b: &PyReal) -> Result<PyReal, PyError> {
    Ok(match (num(a), num(b)) {
        (Num::Int(an, am), Num::Int(bn, bm)) => int_add(an, &am, !bn, &bm),
        (a, b) => PyReal::Float(as_float(a)? - as_float(b)?),
    })
}

/// Python `a * b`.
pub(crate) fn py_mul(a: &PyReal, b: &PyReal) -> Result<PyReal, PyError> {
    Ok(match (num(a), num(b)) {
        (Num::Int(an, am), Num::Int(bn, bm)) => int_result(an != bn, &am.mul(&bm)),
        (a, b) => PyReal::Float(as_float(a)? * as_float(b)?),
    })
}

/// Python `a / b` (true division, always a float). A float operand converts both with
/// `float()` before the zero check (`float_div`); two ints divide exactly and round once
/// (`long_true_divide`).
pub(crate) fn py_truediv(a: &PyReal, b: &PyReal) -> Result<f64, PyError> {
    match (num(a), num(b)) {
        (Num::Int(an, am), Num::Int(bn, bm)) => {
            if bm.is_zero() {
                return Err(PyError::ZeroDivision);
            }
            let q = divide(&am, &bm).ok_or(PyError::IntDivisionTooLargeForFloat)?;
            Ok(if an != bn { -q } else { q })
        }
        (a, b) => {
            let (x, y) = (as_float(a)?, as_float(b)?);
            if y == 0.0 {
                return Err(PyError::ZeroDivision);
            }
            Ok(x / y)
        }
    }
}

/// Python's exact int-versus-float order; `None` for a NaN.
fn int_cmp_float(negative: bool, magnitude: &Mag, x: f64) -> Option<Ordering> {
    if x.is_nan() {
        return None;
    }
    if x.is_infinite() {
        return Some(if x > 0.0 {
            Ordering::Less
        } else {
            Ordering::Greater
        });
    }
    let sign = |zero: bool, below: bool| match (zero, below) {
        (true, _) => 0,
        (false, true) => -1,
        (false, false) => 1,
    };
    let (int_sign, float_sign) = (sign(magnitude.is_zero(), negative), sign(x == 0.0, x < 0.0));
    if int_sign != float_sign || int_sign == 0 {
        return Some(int_sign.cmp(&float_sign));
    }
    // |x| = mantissa * 2^exponent exactly.
    let raw = x.abs().to_bits();
    let biased = (raw >> 52) as i64;
    let fraction = raw & ((1 << 52) - 1);
    let (mantissa, exponent) = if biased == 0 {
        (fraction, -1074)
    } else {
        (fraction | (1 << 52), biased - 1075)
    };
    let order = if exponent >= 0 {
        magnitude.cmp(&Mag::small(mantissa).shl(exponent as u64))
    } else {
        magnitude
            .shl(exponent.unsigned_abs())
            .cmp(&Mag::small(mantissa))
    };
    Some(if negative { order.reverse() } else { order })
}

/// Python's order of `a` and `b`: exact across int and float; `None` when a NaN takes part.
pub(crate) fn py_cmp(a: &PyReal, b: &PyReal) -> Option<Ordering> {
    match (num(a), num(b)) {
        (Num::Float(x), Num::Float(y)) => x.partial_cmp(&y),
        // A zero is never negative here (`num`).
        (Num::Int(an, am), Num::Int(bn, bm)) => Some(match (an, bn) {
            (false, true) => Ordering::Greater,
            (true, false) => Ordering::Less,
            (false, false) => am.cmp(&bm),
            (true, true) => bm.cmp(&am),
        }),
        (Num::Int(n, m), Num::Float(y)) => int_cmp_float(n, &m, y),
        (Num::Float(x), Num::Int(n, m)) => int_cmp_float(n, &m, x).map(Ordering::reverse),
    }
}

/// Python `a < b`.
pub(crate) fn py_lt(a: &PyReal, b: &PyReal) -> bool {
    py_cmp(a, b) == Some(Ordering::Less)
}

/// Python `a <= b`.
pub(crate) fn py_le(a: &PyReal, b: &PyReal) -> bool {
    matches!(py_cmp(a, b), Some(Ordering::Less | Ordering::Equal))
}

/// Python `a == b` for two numbers: exact across int, float and bool (`True == 1 == 1.0`,
/// `-0.0 == 0`, `2**63 + 1 != float(2**63)`); a NaN equals nothing (`py_cmp` is `None`). The same
/// rule as `hash`, which CPython keeps equal for equal numbers (`hash(True) == hash(1) ==
/// hash(1.0)`), so a dict or set keyed by numbers merges exactly the `py_eq` ones.
pub(crate) fn py_eq(a: &PyReal, b: &PyReal) -> bool {
    py_cmp(a, b) == Some(Ordering::Equal)
}

/// Python `a == b` for two numbers a tuple, dict or set holds (`PyObject_RichCompareBool`, which
/// tuple comparison, dict lookup and set membership use, returns true for the same object before
/// calling `__eq__`, and CPython 3.10+ hashes a NaN by its identity). The NaN such a field holds is
/// a decoded frame's: `json.loads` (mycelium_router/wire.py:203) turns every `NaN` literal into
/// json.decoder's single `NaN` constant, and the relay copies that object (relay.py:1794-1834), so
/// two NaN fields are one object and equal here.
pub(crate) fn py_same_element(a: &PyReal, b: &PyReal) -> bool {
    py_eq(a, b) || (is_nan(a) && is_nan(b))
}

fn is_nan(v: &PyReal) -> bool {
    matches!(v, PyReal::Float(x) if x.is_nan())
}

/// Python `a == b` for two decoded values (`PyObject_RichCompare`, the dataclass `__eq__`'s
/// field comparison): two numbers with [`py_eq`], so a NaN equals nothing, not even the same
/// object (`float.__eq__` has no identity shortcut); `None` only `None`, strings code point by
/// code point ([`PyStr`]), and a list or dict as [`py_json_same`] compares them.
pub(crate) fn py_json_eq(a: &PyJson, b: &PyJson) -> bool {
    match (a, b) {
        (PyJson::Num(x), PyJson::Num(y)) => py_eq(x, y),
        _ => same_value(a, b),
    }
}

/// Python `a == b` for two values a list or dict holds, or two dict keys
/// (`PyObject_RichCompareBool`, true for the same object before `__eq__`): numbers with
/// [`py_same_element`], whose NaN is json's one `NaN` constant, the object decode_frame's
/// `json.loads` gives every `NaN` literal; nothing computes these values (serialization.py:20-84
/// and relay.py:2770-2781 copy them), so every NaN they hold is that object. Lists compare
/// element by element (`list_richcompare`) and dicts key by key (`dict_equal`), each element with
/// this comparison (Objects/listobject.c, Objects/dictobject.c @ v3.14.2).
pub(crate) fn py_json_same(a: &PyJson, b: &PyJson) -> bool {
    match (a, b) {
        (PyJson::Num(x), PyJson::Num(y)) => py_same_element(x, y),
        _ => same_value(a, b),
    }
}

fn same_value(a: &PyJson, b: &PyJson) -> bool {
    match (a, b) {
        (PyJson::None, PyJson::None) => true,
        (PyJson::Str(x), PyJson::Str(y)) => x == y,
        (PyJson::List(x), PyJson::List(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_json_same(p, q))
        }
        (PyJson::Dict(x), PyJson::Dict(y)) => {
            x.len() == y.len()
                && x.iter().all(|(key, p)| {
                    y.iter()
                        .find(|(other, _)| other == key)
                        .is_some_and(|(_, q)| py_json_same(p, q))
                })
        }
        _ => false,
    }
}

/// Builtin `min(first, second)`: `second` only if `second < first`.
pub(crate) fn py_min(first: &PyReal, second: &PyReal) -> PyReal {
    if py_lt(second, first) {
        second.clone()
    } else {
        first.clone()
    }
}

/// Builtin `max(first, second)`: `second` only if `second > first`.
pub(crate) fn py_max(first: &PyReal, second: &PyReal) -> PyReal {
    if py_lt(first, second) {
        second.clone()
    } else {
        first.clone()
    }
}

/// A list or dict id is unhashable (TypeError as a dict key or set element).
pub(crate) fn hashable(v: &Value) -> bool {
    !v.is_array() && !v.is_object()
}

/// Python `a == b` for JSON values: numbers and bools by exact value across int and float,
/// strings by text, `None` only to `None`, lists and dicts element by element.
pub(crate) fn python_equal(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| python_equal(p, q))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(key, p)| y.get(key).is_some_and(|q| python_equal(p, q)))
        }
        _ => match (PyReal::from_json(a), PyReal::from_json(b)) {
            (Some(x), Some(y)) => py_cmp(&x, &y) == Some(Ordering::Equal),
            (None, None) => a == b,
            _ => false,
        },
    }
}

/// A dict key that is a list or a dict (TypeError).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Unhashable;

/// A Python dict key as [`IdMap`] holds it: whether it is hashable (a list or dict is not:
/// TypeError) and the `==` a lookup uses once the hashes match (`PyObject_RichCompareBool`).
pub trait DictKey {
    fn hashable(&self) -> bool;
    fn same_key(&self, other: &Self) -> bool;
}

/// A graph id as the parsed graph holds it: [`python_equal`].
impl DictKey for Value {
    fn hashable(&self) -> bool {
        hashable(self)
    }

    fn same_key(&self, other: &Self) -> bool {
        python_equal(self, other)
    }
}

/// A decoded value: [`py_json_same`].
impl DictKey for PyJson {
    fn hashable(&self) -> bool {
        !matches!(self, Self::List(_) | Self::Dict(_))
    }

    fn same_key(&self, other: &Self) -> bool {
        py_json_same(self, other)
    }
}

/// A Python dict keyed by ids (the parsed graph's JSON values, or decoded [`PyJson`] values),
/// in insertion order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IdMap<V, K = Value> {
    entries: Vec<(K, V)>,
}

impl<V, K> Default for IdMap<V, K> {
    fn default() -> Self {
        Self {
            entries: Vec::new(),
        }
    }
}

impl<V, K: DictKey> IdMap<V, K> {
    pub fn new() -> Self {
        Self::default()
    }

    /// `d[key] = value`: a Python-equal key keeps its place and its first spelling.
    pub fn insert(&mut self, key: K, value: V) -> Result<(), Unhashable> {
        if !key.hashable() {
            return Err(Unhashable);
        }
        match self
            .entries
            .iter_mut()
            .find(|(held, _)| held.same_key(&key))
        {
            Some((_, held)) => *held = value,
            None => self.entries.push((key, value)),
        }
        Ok(())
    }

    /// `d.get(key)`.
    pub fn get(&self, key: &K) -> Result<Option<&V>, Unhashable> {
        if !key.hashable() {
            return Err(Unhashable);
        }
        Ok(self
            .entries
            .iter()
            .find(|(held, _)| held.same_key(key))
            .map(|(_, value)| value))
    }

    /// The entries in insertion order.
    pub fn entries(&self) -> &[(K, V)] {
        &self.entries
    }
}

/// A Python `frozenset` of JSON ids (an [`IdMap`] with no values): membership is Python `==`
/// (`1 == 1.0 == True`), and a list or dict id is unhashable (TypeError), as an element or as a
/// probe.
pub type IdSet = IdMap<()>;

impl IdMap<()> {
    /// `frozenset(ids)`: Python-equal ids are one element (the first spelling is kept).
    pub fn of(ids: impl IntoIterator<Item = Value>) -> Result<Self, Unhashable> {
        let mut set = Self::new();
        for id in ids {
            set.insert(id, ())?;
        }
        Ok(set)
    }

    /// `id in set`.
    pub fn contains(&self, id: &Value) -> Result<bool, Unhashable> {
        self.get(id).map(|held| held.is_some())
    }

    /// `set | frozenset({id})`.
    pub fn with(&self, id: &Value) -> Result<Self, Unhashable> {
        let mut set = self.clone();
        if !set.contains(id)? {
            set.insert(id.clone(), ())?;
        }
        Ok(set)
    }

    /// The elements in insertion order.
    pub fn ids(&self) -> impl Iterator<Item = &Value> {
        self.entries.iter().map(|(id, ())| id)
    }
}

/// A refusal with its specific code.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RouterContractError(pub &'static str);

/// A RouterConfig default could not be read from its budget row.
const POLICY_UNAVAILABLE: RouterContractError =
    RouterContractError("m16_router_config_policy_unavailable");

/// mycelium_router/contracts.py:139-182. Every field is the dataclass field of that name with
/// its oracle default ([`RouterConfig::oracle_default`]); durations, counts and byte sizes that
/// have a budget row are read from it.
///
/// Declared limit (RU-001, tagged V03): every field is a Python number ([`PyReal`]), so a `None`,
/// a string or any other non-numeric field cannot be represented, so none is refused at use
/// time the way CPython would. The frozen dataclass checks no field, and
/// `build_live_m16_runtime(..., config=...)` (mycelium_m16_runtime.py:1168) hands a caller's
/// config to the coordinator unchanged (:1260), so a direct in-process Python call that breaks
/// the `RouterConfig | None` annotation can reach scoring with one: `stale_after_seconds=None`
/// (unused by the scorer, admitted) or `minimum_confidence=None` / `"0.25"` (TypeError at
/// scoring.py:94). Those are exactly the inputs this limit excludes. No v0.1.4 producer builds
/// them: every caller of the factory (mycelium_live/supervisor.py:1958-1962,
/// mycelium_live/registry.py:156-160, scripts/qualify_a5_replica_track.py:467-471,
/// scripts/build_a5_replica_pair_graph.py:432) passes no `config`, so the factory always uses
/// `RouterConfig(reservation_lease_seconds=3_600.0)` (:1260), whose fields are all numbers, and
/// no packaged-app, wire or persisted input produces a RouterConfig. Nor does any Rust path
/// decode one from external input (no serde, JSON, CLI or environment decode): a RouterConfig is
/// [`RouterConfig::oracle_default`] plus typed field assignment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RouterConfig {
    pub interactive_alpha: PyReal,
    pub batch_alpha: PyReal,
    pub stale_after_seconds: PyReal,
    pub confidence_half_life_seconds: PyReal,
    pub minimum_confidence: PyReal,
    pub conservative_compute_fraction: PyReal,
    pub conservative_queue_depth: PyReal,
    pub default_bandwidth_bytes_per_second: PyReal,
    pub default_rtt_ms: PyReal,
    pub interactive_base_priority: PyReal,
    pub batch_base_priority: PyReal,
    pub aging_priority_per_second: PyReal,
    pub batch_aging_multiplier: PyReal,
    pub maximum_deficit_boost: PyReal,
    pub maximum_recovery_attempts: PyReal,
    pub reservation_lease_seconds: PyReal,
    pub idempotency_retention_seconds: PyReal,
    pub maximum_idempotency_entries: PyReal,
    pub maximum_pending_hops: PyReal,
    pub maximum_pending_bytes: PyReal,
    pub backpressure_retry_after_seconds: PyReal,
    pub prefill_chunk_size_tokens: PyReal,
    pub maximum_runtime_batch_size: PyReal,
    pub decode_runtime_batch_size: PyReal,
    pub prefill_runtime_batch_size: PyReal,
    pub maximum_runtime_batch_bytes: PyReal,
    pub prefill_collection_window_seconds: PyReal,
    pub live_hop_collection_window_seconds: PyReal,
    pub live_hop_join_horizon_seconds: PyReal,
    pub batch_deadline_guard_seconds: PyReal,
    pub batch_bdp_multiplier: PyReal,
    pub batch_stats_stale_seconds: PyReal,
    pub batch_observation_ewma_alpha: PyReal,
    pub maximum_batch_decision_history: PyReal,
}

/// A float-seconds default from its millisecond budget row (`millis / 1000` rounds once, to the
/// double nearest the oracle's decimal literal).
pub(crate) fn seconds(id: BudgetId) -> Result<PyReal, RouterContractError> {
    id.span(Entries::new(0))
        .map(|span| PyReal::Float(span.millis() as f64 / 1000.0))
        .map_err(|_| POLICY_UNAVAILABLE)
}

fn count(id: BudgetId) -> Result<PyReal, RouterContractError> {
    id.count()
        .map(|n| PyReal::signed(false, PyInt::from(n.get())))
        .map_err(|_| POLICY_UNAVAILABLE)
}

fn bytes(id: BudgetId) -> Result<PyReal, RouterContractError> {
    id.bytes()
        .map(|n| PyReal::signed(false, PyInt::from(n.get())))
        .map_err(|_| POLICY_UNAVAILABLE)
}

impl RouterConfig {
    /// `RouterConfig()` (contracts.py:141-179). The live factory's 3600 s lease
    /// (mycelium_m16_runtime.py:1260) is its own override, not this default.
    pub fn oracle_default() -> Result<Self, RouterContractError> {
        use BudgetId::*;
        Ok(Self {
            interactive_alpha: PyReal::Float(0.35),
            batch_alpha: PyReal::Float(0.10),
            stale_after_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigStaleAfterSecondsL143,
            )?,
            confidence_half_life_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigConfidenceHalfLifeSecondsL144,
            )?,
            minimum_confidence: PyReal::Float(0.25),
            conservative_compute_fraction: PyReal::Float(0.10),
            conservative_queue_depth: PyReal::int(20),
            default_bandwidth_bytes_per_second: PyReal::Float(1_000_000.0),
            default_rtt_ms: PyReal::Float(100.0),
            interactive_base_priority: PyReal::Float(100.0),
            batch_base_priority: PyReal::Float(10.0),
            aging_priority_per_second: PyReal::Float(1.0),
            batch_aging_multiplier: PyReal::Float(4.0),
            maximum_deficit_boost: PyReal::Float(50.0),
            maximum_recovery_attempts: count(
                PyMyceliumRouterContractsRouterconfigMaximumRecoveryAttemptsL155,
            )?,
            reservation_lease_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigReservationLeaseSecondsL156,
            )?,
            idempotency_retention_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigIdempotencyRetentionSecondsL157,
            )?,
            maximum_idempotency_entries: count(
                PyMyceliumRouterContractsRouterconfigMaximumIdempotencyEntriesL158,
            )?,
            maximum_pending_hops: PyReal::int(1_024),
            maximum_pending_bytes: bytes(
                PyMyceliumRouterContractsRouterconfigMaximumPendingBytesL160,
            )?,
            backpressure_retry_after_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigBackpressureRetryAfterSecondsL161,
            )?,
            prefill_chunk_size_tokens: PyReal::int(0),
            maximum_runtime_batch_size: count(
                PyFinalMyceliumRouterContractsMaximumRuntimeBatchSizeL163C3,
            )?,
            decode_runtime_batch_size: PyReal::int(8),
            prefill_runtime_batch_size: PyReal::int(20),
            maximum_runtime_batch_bytes: bytes(
                PyMyceliumRouterContractsRouterconfigMaximumRuntimeBatchBytesL166,
            )?,
            prefill_collection_window_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigPrefillCollectionWindowSecondsL167,
            )?,
            live_hop_collection_window_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigLiveHopCollectionWindowSecondsL171,
            )?,
            live_hop_join_horizon_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigLiveHopJoinHorizonSecondsL174,
            )?,
            batch_deadline_guard_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigBatchDeadlineGuardSecondsL175,
            )?,
            batch_bdp_multiplier: PyReal::Float(2.0),
            batch_stats_stale_seconds: seconds(
                PyMyceliumRouterContractsRouterconfigBatchStatsStaleSecondsL177,
            )?,
            batch_observation_ewma_alpha: PyReal::Float(0.25),
            maximum_batch_decision_history: count(
                PyMyceliumRouterContractsRouterconfigMaximumBatchDecisionHistoryL179,
            )?,
        })
    }

    /// contracts.py:181-182: `batch_alpha` for the qos class `"batch"`, `interactive_alpha` for
    /// any other.
    pub fn alpha_for(&self, qos_class: &str) -> &PyReal {
        if qos_class == "batch" {
            &self.batch_alpha
        } else {
            &self.interactive_alpha
        }
    }
}

/// contracts.py:107-121.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeviceState {
    pub node_id: Value,
    pub state_seq: PyReal,
    pub last_updated: PyReal,
    pub availability: String,
    pub compute_units_per_second: PyReal,
    pub free_compute_fraction: PyReal,
    pub available_kv_bytes: PyReal,
    pub pending_hop_queue_depth: PyReal,
    pub neighbor_rtt_ms: IdMap<PyReal>,
    pub neighbor_bandwidth_bytes_per_second: IdMap<PyReal>,
}

impl DeviceState {
    /// contracts.py:120-121: the same state with another availability.
    pub fn with_availability(&self, availability: &str) -> Self {
        Self {
            availability: availability.to_owned(),
            ..self.clone()
        }
    }
}

/// contracts.py:124-136.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestContext {
    pub request_id: String,
    pub prompt_token_ids: Vec<PyReal>,
    pub max_new_tokens: PyReal,
    pub expected_new_tokens: PyReal,
    pub qos_class: String,
    pub admitted_at: PyReal,
    pub target_ttft_ms: PyReal,
    pub target_tpot_ms: PyReal,
    pub target_tokens_per_second: PyReal,
    pub sampling_seed: PyReal,
    pub generation_config_digest: String,
}

/// contracts.py:211-219. Every number the scorer reports is a float.
#[derive(Clone, Debug)]
pub struct ScoreBreakdown {
    pub ttft_ms: f64,
    pub tpot_ms: f64,
    pub prefill_transfer_ms: f64,
    pub decode_transfer_ms: f64,
    pub total_score: f64,
    pub confidence: f64,
    /// The graph's node ids, sorted as Python sorts them.
    pub fallback_nodes: Vec<Value>,
}

/// Floats compare bit for bit (output identity, not `==`).
impl PartialEq for ScoreBreakdown {
    fn eq(&self, other: &Self) -> bool {
        let floats = |b: &Self| {
            [
                b.ttft_ms,
                b.tpot_ms,
                b.prefill_transfer_ms,
                b.decode_transfer_ms,
                b.total_score,
                b.confidence,
            ]
            .map(f64::to_bits)
        };
        floats(self) == floats(other) && self.fallback_nodes == other.fallback_nodes
    }
}
impl Eq for ScoreBreakdown {}

/// contracts.py:7.
pub const PATH_MANIFEST_PROTOCOL: &str = "mycelium.path_manifest.v1";

/// contracts.py:84-90. Ids are the graph's JSON values; the expiry is the ledger's float
/// (`ReservationResult.expires_at`, which the ledger keeps as `lease_base + float(...)`), and the
/// epoch the reservation's (the graph's deployment epoch value, or the refusal default `-1`).
#[derive(Clone, Debug)]
pub struct PathHop {
    pub stage_id: Value,
    pub placement_id: Value,
    pub reservation_id: String,
    pub reservation_expires_at: f64,
    pub reservation_epoch: Value,
}

impl PathHop {
    /// `PathHop(stage_id, placement_id, reservation_id)`: the defaults
    /// `reservation_expires_at = float("inf")` (:89) and `reservation_epoch = -1` (:90).
    pub fn new(stage_id: Value, placement_id: Value, reservation_id: &str) -> Self {
        Self {
            stage_id,
            placement_id,
            reservation_id: reservation_id.to_owned(),
            reservation_expires_at: f64::INFINITY,
            reservation_epoch: Value::from(-1),
        }
    }
}

/// Same values; the expiry compares bit for bit (output identity, not `==`).
impl PartialEq for PathHop {
    fn eq(&self, other: &Self) -> bool {
        self.stage_id == other.stage_id
            && self.placement_id == other.placement_id
            && self.reservation_id == other.reservation_id
            && self.reservation_expires_at.to_bits() == other.reservation_expires_at.to_bits()
            && self.reservation_epoch == other.reservation_epoch
    }
}
impl Eq for PathHop {}

/// contracts.py:93-104. The graph-derived fields keep the graph's JSON values.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PathManifest {
    pub path_id: String,
    pub path_attempt: u64,
    pub request_id: String,
    pub deployment_id: Value,
    pub deployment_epoch: Value,
    pub topology_version: Value,
    pub manifest_digest: Value,
    pub ordered_hops: Vec<PathHop>,
    pub loopback_edge_id: Value,
    /// Defaults to [`PATH_MANIFEST_PROTOCOL`] (:104).
    pub protocol: String,
}

/// contracts.py:222-226.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BranchDecision {
    pub placement_id: Value,
    pub complete_route: Vec<Value>,
    pub score: ScoreBreakdown,
}

/// contracts.py:229-241. The graph is the parsed one ([`Graph`] has no `==`, so neither has this).
#[derive(Clone, Debug)]
pub struct PathBuildState {
    pub request: RequestContext,
    pub graph: Graph,
    pub path_id: String,
    pub path_attempt: u64,
    pub ordered_hops: Vec<PathHop>,
    pub excluded_placements: IdSet,
    pub excluded_edges: IdSet,
    pub excluded_devices: IdSet,
}

impl PathBuildState {
    /// `PathBuildState(request, graph, path_id, path_attempt)`: no hops and empty exclusions
    /// (:235-238).
    pub fn new(request: RequestContext, graph: Graph, path_id: &str, path_attempt: u64) -> Self {
        Self {
            request,
            graph,
            path_id: path_id.to_owned(),
            path_attempt,
            ordered_hops: Vec::new(),
            excluded_placements: IdSet::new(),
            excluded_edges: IdSet::new(),
            excluded_devices: IdSet::new(),
        }
    }

    /// contracts.py:240-241: the same state with these excluded devices.
    pub fn with_excluded_devices(&self, devices: IdSet) -> Self {
        Self {
            excluded_devices: devices,
            ..self.clone()
        }
    }
}

/// contracts.py:244-260. The relay builds it from the registered graph and the hop's placement
/// (relay.py:2757-2785): `deployment_id`, `deployment_epoch`, `model_commit` (the graph's
/// `resolved_commit`), `manifest_digest`, `hidden_size` and `activation_bytes` are the graph's,
/// `placement_id`, `assignment_id`, `stage_signature`, `load_proof_digest` and `runtime_backend`
/// the placement's, each the value a peer's ManifestLocked carried as decode_frame's `json.loads`
/// read it (wire.py:203, 263-269, 339-355; serialization.py:20-84 copies it unchanged), so a
/// [`PyJson`]. A member registers that graph (transports/ iroh.py:4003-4009, router.py:148-177)
/// once the graph and manifest checks pass, each refusing where it is raised: a falsy id, an epoch
/// below 0 or a size not above 0 in the graph check (validation.py:41-134), surfaced by
/// decode_frame as `missing_wire_field` when the message says "missing" (a falsy graph id's
/// `missing_*` code) and `invalid_wire_field` otherwise (wire.py:281-287); a list or dict placement
/// id at validation.py:85 (unhashable); a NaN placement id by the manifest-versus-build hop
/// comparison (`invalid_manifest_locked`, wire.py:468-470); a top-level NaN the manifest repeats by
/// `validate_manifest` (validation.py:186, :195; wire.py:472-474); and a falsy HopHeader
/// destination at wire.py:496-499. Every other value reaches the key, NaN, `Infinity`, `1e400`,
/// `-0.0`, ints past 64 bits, lists, dicts and strings with lone surrogates (as a value, in a list
/// and as a dict key: a [`PyStr`]) included. `phase` and `speculative_role` are the relay's
/// strings. `token_span` is the relay's span: 1 for DECODE, else the peer frame's
/// `prefill_chunk_token_count`, which the wire decoder leaves any number (wire.py:245-278, 488-499)
/// and the relay keeps when it is not `<= 0` (relay.py:1822-1831, :2766): a bool, an int of any
/// size, a fractional, infinite or NaN float, so a [`PyReal`]. `speculative_width` is `token_span -
/// 1` only for VERIFY, whose span is an `int` in `2..=MAX_VERIFY_SPAN` (relay.py:938-946, :2784),
/// else `0`: an `i64`.
#[derive(Clone, Debug)]
pub struct RuntimeBatchKey {
    pub deployment_id: PyJson,
    pub deployment_epoch: PyJson,
    pub model_commit: PyJson,
    pub manifest_digest: PyJson,
    pub placement_id: PyJson,
    pub assignment_id: PyJson,
    pub stage_signature: PyJson,
    pub load_proof_digest: PyJson,
    pub runtime_backend: PyJson,
    pub phase: String,
    pub hidden_size: PyJson,
    pub activation_bytes: PyJson,
    pub token_span: PyReal,
    /// Defaults to `"NONE"` (:259).
    pub speculative_role: String,
    /// Defaults to `0` (:260).
    pub speculative_width: i64,
}

/// contracts.py:259 the default speculative role.
pub const NO_SPECULATIVE_ROLE: &str = "NONE";

impl RuntimeBatchKey {
    /// `RuntimeBatchKey(...)` with the thirteen required fields, in the dataclass's order; the
    /// defaults `speculative_role = "NONE"` and `speculative_width = 0` (:259-260).
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        deployment_id: PyJson,
        deployment_epoch: PyJson,
        model_commit: PyJson,
        manifest_digest: PyJson,
        placement_id: PyJson,
        assignment_id: PyJson,
        stage_signature: PyJson,
        load_proof_digest: PyJson,
        runtime_backend: PyJson,
        phase: &str,
        hidden_size: PyJson,
        activation_bytes: PyJson,
        token_span: PyReal,
    ) -> Self {
        Self {
            deployment_id,
            deployment_epoch,
            model_commit,
            manifest_digest,
            placement_id,
            assignment_id,
            stage_signature,
            load_proof_digest,
            runtime_backend,
            phase: phase.to_owned(),
            hidden_size,
            activation_bytes,
            token_span,
            speculative_role: NO_SPECULATIVE_ROLE.to_owned(),
            speculative_width: 0,
        }
    }

    /// The eleven graph-derived values, in field order.
    fn graph_values(&self) -> [&PyJson; 11] {
        [
            &self.deployment_id,
            &self.deployment_epoch,
            &self.model_commit,
            &self.manifest_digest,
            &self.placement_id,
            &self.assignment_id,
            &self.stage_signature,
            &self.load_proof_digest,
            &self.runtime_backend,
            &self.hidden_size,
            &self.activation_bytes,
        ]
    }

    /// The same key as output: every field keeps its Python type and value (a `True` span or
    /// epoch is not the int `1`, a float compares bit for bit, a dict keeps its key order).
    pub fn same_output(&self, other: &Self) -> bool {
        self.graph_values() == other.graph_values()
            && self.phase == other.phase
            && self.token_span == other.token_span
            && self.speculative_role == other.speculative_role
            && self.speculative_width == other.speculative_width
    }
}

/// The dataclass `==` of two key objects. CPython 3.14.2's generated `__eq__` is `self is
/// other`, else every field's `==` in turn (dataclasses.py, `if eq:`): the graph-derived values
/// with [`py_json_eq`] (`1 == 1.0 == True`, `"1" != 1`, `[True] == [1]`, a NaN field equal to
/// nothing, a NaN inside a list or dict equal to itself) and `token_span` with [`py_eq`]. So a
/// key with a NaN field or span is equal only to itself, the identity the scheduler keeps for an
/// anchor's own key. Not `Eq`: a NaN makes it irreflexive.
impl PartialEq for RuntimeBatchKey {
    fn eq(&self, other: &Self) -> bool {
        self.graph_values()
            .iter()
            .zip(other.graph_values())
            .all(|(a, b)| py_json_eq(a, b))
            && self.phase == other.phase
            && py_eq(&self.token_span, &other.token_span)
            && self.speculative_role == other.speculative_role
            && self.speculative_width == other.speculative_width
    }
}

/// contracts.py:285-295. The scheduler only passes it through (scheduler.py:156, :205).
#[derive(Clone, Debug)]
pub struct BatchDecision {
    pub action: String,
    pub phase: String,
    pub batch_size: i64,
    pub target_items: i64,
    pub available_items: i64,
    pub predicted_payload_bytes: i64,
    pub predicted_transfer_ms: f64,
    pub ready_at: f64,
    pub reason: String,
}

/// Same values; the floats compare bit for bit (output identity, not `==`).
impl PartialEq for BatchDecision {
    fn eq(&self, other: &Self) -> bool {
        self.action == other.action
            && self.phase == other.phase
            && self.batch_size == other.batch_size
            && self.target_items == other.target_items
            && self.available_items == other.available_items
            && self.predicted_payload_bytes == other.predicted_payload_bytes
            && self.predicted_transfer_ms.to_bits() == other.predicted_transfer_ms.to_bits()
            && self.ready_at.to_bits() == other.ready_at.to_bits()
            && self.reason == other.reason
    }
}
impl Eq for BatchDecision {}

/// A hop's `payload` (contracts.py:311, an `object`) as the scheduler sizes it
/// (scheduler.py:265-275).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HopPayload {
    /// `None`: 0 bytes.
    None,
    /// `bytes` or `bytearray`: its length.
    Bytes(Vec<u8>),
    /// A `tuple` of Python numbers, as the gateway's `prompt_token_ids` (contracts.py:127,
    /// mycelium_request_gateway/backend.py:27): four bytes per element when every element is an
    /// `int` and none a `bool`.
    Tuple(Vec<PyReal>),
    /// A `list` of Python numbers, sized as a tuple.
    List(Vec<PyReal>),
    /// Any other object (a `str`, a dict, an array, a sequence holding a non-number), by its
    /// Python `repr`: it has no byte size (TypeError).
    Other(String),
}

/// contracts.py:298-318. Ids are the dispatcher's strings (a member relay copies a peer frame's
/// request and path ids, phase and qos class unchecked: another type or a lone surrogate there is
/// held with owner question Q7, neither carried nor refused); the placement id is the manifest
/// hop's (mycelium_m16_runtime.py:459, relay.py:1375, :1801), for a member a peer's ManifestLocked
/// value as decode_frame's `json.loads` read it, so a [`PyJson`]: the hop checks (wire.py:468,
/// validation.py:201-221, relay.py:1707-1718) refuse a list, dict or NaN id and pass `Infinity`,
/// `1e400`, ints past 64 bits and a `str` with lone surrogates. A falsy id (`None`, `False`, a
/// zero, the empty string) decodes in the manifest but is refused as the HopHeader destination
/// (wire.py:496-499) before the relay builds the item; every other hashable value passes. It is the
/// manifest hop's value and the batch key's `placement_id` the graph placement's (relay.py:1801,
/// :2774): Python `==` to each other, not necessarily one type or spelling (`1` and `1.0`), so each
/// is kept as decoded.
///
/// The fields the member relay copies from a peer's frame are [`PyReal`]s, the numbers the wire
/// decoder leaves unchecked (`json.loads`, wire.py:203, then `cls(**values)`, :245-278; :488-499
/// only requires `path_attempt` and `hop_index` not `< 0`): `token_index` and
/// `prefill_chunk_token_count` are the header's (relay.py:1794-1834: any number, NaN and infinities
/// from a frame's `NaN`/`Infinity` literals), `position` is computed from them (:927-935),
/// `hop_index` the header's, indexing the manifest (an int or a bool, :1704-1706), and
/// `path_attempt` and `lease_expires_at` the registered manifest's (a peer's ManifestLocked
/// registered as transports/iroh.py:3992-4011 does: `path_attempt` any number `==` the header's,
/// `reservation_expires_at` any value). The owner's M16 REQUEST item carries ints and floats there
/// (mycelium_m16_runtime.py:452-467). The times `enqueued_at` and `deadline_at` and the deficit
/// ratio stay floats: every producer passes a clock reading (relay.py:1378, :1804, :2172, :2892;
/// mycelium_m16_runtime.py:451), a float sum (`_hop_deadline`, relay.py:2744-2754) or the default
/// `inf`, and `deficit_ratio=0.0` (relay.py:1377, :1803, :2171, :2891; m16_runtime.py:461).
#[derive(Clone, Debug)]
pub struct HopWorkItem {
    pub request_id: String,
    pub path_id: String,
    pub path_attempt: PyReal,
    pub phase: String,
    pub token_index: PyReal,
    pub hop_index: PyReal,
    pub placement_id: PyJson,
    pub qos_class: String,
    pub deficit_ratio: f64,
    pub enqueued_at: f64,
    pub idempotency_key: String,
    pub payload: HopPayload,
    /// Defaults to `0` (:312).
    pub prefill_chunk_token_count: PyReal,
    /// Defaults to `None` (:313).
    pub batch_key: Option<RuntimeBatchKey>,
    /// Defaults to `float("inf")` (:314).
    pub deadline_at: f64,
    /// Defaults to `0` (:315).
    pub position: PyReal,
    /// Defaults to `False` (:316).
    pub emits_token: bool,
    /// Defaults to `False` (:317).
    pub terminal: bool,
    /// Defaults to `float("inf")` (:318).
    pub lease_expires_at: PyReal,
}

impl HopWorkItem {
    /// `HopWorkItem(...)` with the twelve required fields, in the dataclass's order; every
    /// other field takes its default (:312-318).
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        request_id: &str,
        path_id: &str,
        path_attempt: PyReal,
        phase: &str,
        token_index: PyReal,
        hop_index: PyReal,
        placement_id: PyJson,
        qos_class: &str,
        deficit_ratio: f64,
        enqueued_at: f64,
        idempotency_key: &str,
        payload: HopPayload,
    ) -> Self {
        Self {
            request_id: request_id.to_owned(),
            path_id: path_id.to_owned(),
            path_attempt,
            phase: phase.to_owned(),
            token_index,
            hop_index,
            placement_id,
            qos_class: qos_class.to_owned(),
            deficit_ratio,
            enqueued_at,
            idempotency_key: idempotency_key.to_owned(),
            payload,
            prefill_chunk_token_count: PyReal::int(0),
            batch_key: None,
            deadline_at: f64::INFINITY,
            position: PyReal::int(0),
            emits_token: false,
            terminal: false,
            lease_expires_at: PyReal::Float(f64::INFINITY),
        }
    }
}

/// Same values: every field keeps its Python type and value and the floats compare bit for bit
/// (output identity, not `==`: a `True` token index is not the int `1`, an `Infinity` placement
/// id is a float).
impl PartialEq for HopWorkItem {
    fn eq(&self, other: &Self) -> bool {
        let floats = |i: &Self| [i.deficit_ratio, i.enqueued_at, i.deadline_at].map(f64::to_bits);
        let same_key = match (&self.batch_key, &other.batch_key) {
            (Some(a), Some(b)) => a.same_output(b),
            (a, b) => a.is_none() && b.is_none(),
        };
        hop_identity(self) == hop_identity(other)
            && self.placement_id == other.placement_id
            && self.qos_class == other.qos_class
            && self.idempotency_key == other.idempotency_key
            && self.payload == other.payload
            && self.prefill_chunk_token_count == other.prefill_chunk_token_count
            && same_key
            && self.position == other.position
            && self.lease_expires_at == other.lease_expires_at
            && self.emits_token == other.emits_token
            && self.terminal == other.terminal
            && floats(self) == floats(other)
    }
}
impl Eq for HopWorkItem {}

/// idempotency.py:98-111: `(request_id, path_id, path_attempt, phase, token_index,
/// hop_index)`, the structured identity a hop's idempotency key joins with `:` (ids may contain
/// `:`, so two hops can share the joined key but never this tuple). Its `==` here is output
/// identity (each number's type and value); the scheduler compares identities as Python's tuple
/// `==` and `<` do ([`same_hop`], [`hop_lt`]).
pub type HopIdentity = (String, String, PyReal, String, PyReal, PyReal);

/// Python `a == b` for two hop identities, as a dict key, set element or tuple: the strings by
/// text, the numbers with [`py_same_element`] (`True == 1 == 1.0`, `-0.0 == 0`, the decoder's NaN
/// equal to itself).
pub(crate) fn same_hop(a: &HopIdentity, b: &HopIdentity) -> bool {
    a.0 == b.0
        && a.1 == b.1
        && py_same_element(&a.2, &b.2)
        && a.3 == b.3
        && py_same_element(&a.4, &b.4)
        && py_same_element(&a.5, &b.5)
}

/// Python's tuple `a < b` for two hop identities (`tuplerichcompare`): the first element that is
/// not equal ([`py_same_element`]) decides with `<` (strings by code point, numbers with
/// [`py_lt`], so a NaN is below nothing and nothing is below it); all equal is not less.
pub(crate) fn hop_lt(a: &HopIdentity, b: &HopIdentity) -> bool {
    if a.0 != b.0 {
        return a.0 < b.0;
    }
    if a.1 != b.1 {
        return a.1 < b.1;
    }
    if !py_same_element(&a.2, &b.2) {
        return py_lt(&a.2, &b.2);
    }
    if a.3 != b.3 {
        return a.3 < b.3;
    }
    if !py_same_element(&a.4, &b.4) {
        return py_lt(&a.4, &b.4);
    }
    !py_same_element(&a.5, &b.5) && py_lt(&a.5, &b.5)
}

/// idempotency.py:98-111.
pub fn hop_identity(item: &HopWorkItem) -> HopIdentity {
    (
        item.request_id.clone(),
        item.path_id.clone(),
        item.path_attempt.clone(),
        item.phase.clone(),
        item.token_index.clone(),
        item.hop_index.clone(),
    )
}

/// contracts.py:321-333. Built only through [`RuntimeBatch::new`], which refuses as
/// `__post_init__` does (:327-333).
#[derive(Clone, Debug)]
pub struct RuntimeBatch {
    compatibility_key: Option<RuntimeBatchKey>,
    items: Vec<HopWorkItem>,
    decision: Option<BatchDecision>,
}

impl RuntimeBatch {
    /// `RuntimeBatch(compatibility_key, items, decision)`: ValueError `empty_runtime_batch` for
    /// no items (:328-329); `incompatible_runtime_batch` when an item's batch key is not the
    /// batch's (Python `==`, :330-331) or a batch without a key holds more than one item
    /// (:332-333).
    pub fn new(
        compatibility_key: Option<RuntimeBatchKey>,
        items: Vec<HopWorkItem>,
        decision: Option<BatchDecision>,
    ) -> Result<Self, RouterContractError> {
        Self::assembled(compatibility_key, items, decision, |_| false)
    }

    /// [`RuntimeBatch::new`] where `holds_batch_key(k)` says item `k`'s key is the batch's own
    /// key object, which the dataclass `!=` finds equal by identity before comparing fields
    /// (the scheduler's anchor, scheduler.py:201-205); every other item's key is compared with
    /// Python `==`.
    pub(crate) fn assembled(
        compatibility_key: Option<RuntimeBatchKey>,
        items: Vec<HopWorkItem>,
        decision: Option<BatchDecision>,
        holds_batch_key: impl Fn(usize) -> bool,
    ) -> Result<Self, RouterContractError> {
        if items.is_empty() {
            return Err(RouterContractError("empty_runtime_batch"));
        }
        if items
            .iter()
            .enumerate()
            .any(|(k, item)| !holds_batch_key(k) && item.batch_key != compatibility_key)
            || (compatibility_key.is_none() && items.len() != 1)
        {
            return Err(RouterContractError("incompatible_runtime_batch"));
        }
        Ok(Self {
            compatibility_key,
            items,
            decision,
        })
    }

    pub fn compatibility_key(&self) -> Option<&RuntimeBatchKey> {
        self.compatibility_key.as_ref()
    }

    pub fn items(&self) -> &[HopWorkItem] {
        &self.items
    }

    pub fn decision(&self) -> Option<&BatchDecision> {
        self.decision.as_ref()
    }

    pub fn into_items(self) -> Vec<HopWorkItem> {
        self.items
    }
}

/// The same batch as output: the key with [`RuntimeBatchKey::same_output`], the items and the
/// decision as their own `==`.
impl PartialEq for RuntimeBatch {
    fn eq(&self, other: &Self) -> bool {
        let same_key = match (&self.compatibility_key, &other.compatibility_key) {
            (Some(a), Some(b)) => a.same_output(b),
            (a, b) => a.is_none() && b.is_none(),
        };
        same_key && self.items == other.items && self.decision == other.decision
    }
}
impl Eq for RuntimeBatch {}
