// SPDX-License-Identifier: AGPL-3.0-only
//! Python-semantics helpers for the v0.1.4 ports in this crate.
//!
//! The pinned oracle distinguishes `ValueError` (a deliberate refusal whose text
//! is part of observable output, e.g. replan reasons) from `KeyError`/`TypeError`/
//! `AttributeError` raised by dynamic access to malformed documents. `Kind::Other`
//! is the second family: still a refusal, but its message is not an oracle contract.
use serde_json::{Number, Value};
use std::cmp::Ordering;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// Python `ValueError` with oracle-pinned text.
    Value,
    /// Any other Python exception class (KeyError, TypeError, AttributeError, …).
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    pub kind: Kind,
    pub message: String,
}

impl Failure {
    pub fn value(message: impl Into<String>) -> Self {
        Self {
            kind: Kind::Value,
            message: message.into(),
        }
    }
    pub fn other(message: impl Into<String>) -> Self {
        Self {
            kind: Kind::Other,
            message: message.into(),
        }
    }
}

impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for Failure {}

pub type PyResult<T> = Result<T, Failure>;

pub fn verr<T>(message: impl Into<String>) -> PyResult<T> {
    Err(Failure::value(message))
}

pub static NULL: Value = Value::Null;

/// `mapping[key]`: missing key or non-mapping is a non-`ValueError` exception.
pub fn req<'a>(v: &'a Value, key: &str) -> PyResult<&'a Value> {
    match v {
        Value::Object(m) => m
            .get(key)
            .ok_or_else(|| Failure::other(format!("KeyError: {key}"))),
        _ => Err(Failure::other(format!("TypeError: subscript {key}"))),
    }
}

/// `mapping.get(key)` (None when absent). A non-mapping is an AttributeError.
pub fn get<'a>(v: &'a Value, key: &str) -> PyResult<&'a Value> {
    match v {
        Value::Object(m) => Ok(m.get(key).unwrap_or(&NULL)),
        _ => Err(Failure::other(format!("AttributeError: get {key}"))),
    }
}

pub fn has(v: &Value, key: &str) -> bool {
    v.as_object().is_some_and(|m| m.contains_key(key))
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Num {
    Int(i128),
    Float(f64),
}

/// Python `int` or `float` (never `bool`).
pub fn num(v: &Value) -> Option<Num> {
    match v {
        Value::Number(n) => Some(number(n)),
        _ => None,
    }
}

pub fn number(n: &Number) -> Num {
    if let Some(i) = n.as_i64() {
        Num::Int(i128::from(i))
    } else if let Some(u) = n.as_u64() {
        Num::Int(i128::from(u))
    } else {
        Num::Float(n.as_f64().unwrap_or(f64::NAN))
    }
}

/// `isinstance(v, int) and not isinstance(v, bool)`.
pub fn int(v: &Value) -> Option<i128> {
    match num(v) {
        Some(Num::Int(i)) => Some(i),
        _ => None,
    }
}

/// `isinstance(v, (int, float)) and not isinstance(v, bool)`, as float.
pub fn float(v: &Value) -> Option<f64> {
    match num(v)? {
        Num::Int(i) => Some(i as f64),
        Num::Float(f) => Some(f),
    }
}

/// Python numeric comparison treats `bool` as `int`.
fn as_cmp_num(v: &Value) -> Option<Num> {
    match v {
        Value::Bool(b) => Some(Num::Int(i128::from(*b))),
        other => num(other),
    }
}

fn num_cmp(a: Num, b: Num) -> Option<Ordering> {
    match (a, b) {
        (Num::Int(x), Num::Int(y)) => Some(x.cmp(&y)),
        (Num::Float(x), Num::Float(y)) => x.partial_cmp(&y),
        (Num::Int(x), Num::Float(y)) => int_float_cmp(x, y),
        (Num::Float(x), Num::Int(y)) => int_float_cmp(y, x).map(Ordering::reverse),
    }
}

fn int_float_cmp(i: i128, f: f64) -> Option<Ordering> {
    if f.is_nan() {
        return None;
    }
    if f.is_infinite() {
        return Some(if f > 0.0 {
            Ordering::Less
        } else {
            Ordering::Greater
        });
    }
    let floor = f.floor();
    // i128 covers every JSON integer; floats beyond ±1.7e38 saturate consistently.
    let fi = floor as i128;
    match i.cmp(&fi) {
        Ordering::Equal if f > floor => Some(Ordering::Less),
        other => Some(other),
    }
}

/// Python `==` over JSON-shaped values (`1 == 1.0 == True`).
pub fn py_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| py_eq(v, w)))
        }
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(v, w)| py_eq(v, w))
        }
        (Value::String(x), Value::String(y)) => x == y,
        (Value::Null, Value::Null) => true,
        _ => match (as_cmp_num(a), as_cmp_num(b)) {
            (Some(x), Some(y)) => num_cmp(x, y) == Some(Ordering::Equal),
            _ => false,
        },
    }
}

/// `value is True` / `value is False` identity checks.
pub fn is_bool(v: &Value, expected: bool) -> bool {
    v.as_bool() == Some(expected)
}

/// Python `str.format(layer=n)` for the only field the manifests use.
pub fn format_layer(template: &str, layer: u64) -> PyResult<String> {
    let mut out = String::new();
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                out.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                out.push('}');
            }
            '{' => {
                let mut field = String::new();
                loop {
                    match chars.next() {
                        Some('}') => break,
                        Some(x) => field.push(x),
                        None => return verr("Single '{' encountered in format string"),
                    }
                }
                if field != "layer" {
                    return Err(Failure::other(format!("unsupported format field {field}")));
                }
                out.push_str(&layer.to_string());
            }
            '}' => return verr("Single '}' encountered in format string"),
            other => out.push(other),
        }
    }
    Ok(out)
}

/// CPython `math.fsum` (Shewchuk partials with the final half-even correction).
pub fn fsum(values: impl IntoIterator<Item = f64>) -> f64 {
    let mut partials: Vec<f64> = Vec::new();
    let mut special = 0.0_f64;
    let mut inf_nan = false;
    for mut x in values {
        if !x.is_finite() {
            inf_nan = true;
            special += x;
            continue;
        }
        let mut i = 0;
        for j in 0..partials.len() {
            let mut y = partials[j];
            if x.abs() < y.abs() {
                std::mem::swap(&mut x, &mut y);
            }
            let hi = x + y;
            let lo = y - (hi - x);
            if lo != 0.0 {
                partials[i] = lo;
                i += 1;
            }
            x = hi;
        }
        partials.truncate(i);
        partials.push(x);
    }
    if inf_nan {
        return special;
    }
    let mut n = partials.len();
    let mut hi = 0.0;
    if n > 0 {
        n -= 1;
        hi = partials[n];
        let mut lo = 0.0;
        while n > 0 {
            let x = hi;
            n -= 1;
            let y = partials[n];
            hi = x + y;
            let yr = hi - x;
            lo = y - yr;
            if lo != 0.0 {
                break;
            }
        }
        if n > 0 && ((lo < 0.0 && partials[n - 1] < 0.0) || (lo > 0.0 && partials[n - 1] > 0.0)) {
            let y = lo * 2.0;
            let x = hi + y;
            let yr = x - hi;
            if y == yr {
                hi = x;
            }
        }
    }
    hi
}

/// Python `str(value)` of a JSON-shaped value (`None`, `True`, quoted containers).
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// Python `repr(value)` of a JSON-shaped value.
pub fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => match number(n) {
            Num::Int(i) => i.to_string(),
            Num::Float(f) => protocol::canonical::python_float_repr(f).unwrap_or_else(|_| {
                if f.is_nan() {
                    "nan".into()
                } else {
                    "inf".into()
                }
            }),
        },
        Value::String(s) => repr_str(s),
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => format!(
            "{{{}}}",
            m.iter()
                .map(|(k, v)| format!("{}: {}", repr_str(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fsum_is_exactly_rounded() {
        assert_eq!(fsum([0.1; 10]), 1.0);
        assert_eq!(fsum([1e100, 1.0, -1e100]), 1.0);
        assert_eq!(fsum([]), 0.0);
    }
}
