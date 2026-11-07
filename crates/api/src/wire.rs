// SPDX-License-Identifier: AGPL-3.0-only
//! Python-`json.dumps`-identical response bytes (integrator ruling (a)). v0.1.4 builds each
//! response as a `dict` (insertion order) and calls `json.dumps(v, separators=(',',':'))` with
//! `ensure_ascii` at its default (desktop app.py:1874, chat.py:613) or `False` (directory
//! server.py:136); the gateway sorts keys (asgi.py:58). `serde_json::Value` sorts keys, so
//! handlers that must emit the source's insertion order build a [`Json`] instead.
//! Lone surrogates cannot exist in a Rust `String` (the source can emit them only for input
//! it was given); every other code point is encoded exactly as `json.dumps` does.
use serde_json::{Number, Value};

/// A JSON value whose objects keep insertion order.
#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(Number),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

/// How non-ASCII text is written (`ensure_ascii`).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Text {
    /// `json.dumps` default: `\uXXXX` (surrogate pairs above the BMP), lowercase hex.
    Ascii,
    /// `ensure_ascii=False`: UTF-8 as is (control characters, `"` and `\` are still escaped).
    Utf8,
}

impl Json {
    pub fn object<K: Into<String>, const N: usize>(fields: [(K, Json); N]) -> Self {
        Self::Obj(fields.into_iter().map(|(k, v)| (k.into(), v)).collect())
    }
    pub fn str(value: impl Into<String>) -> Self {
        Self::Str(value.into())
    }
    pub fn int(value: i64) -> Self {
        Self::Num(value.into())
    }
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Self::Obj(fields) => fields.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    /// Append or replace like `dict[key] = value` (replacement keeps the key's position).
    pub fn set(&mut self, key: &str, value: Json) {
        if let Self::Obj(fields) = self {
            match fields.iter_mut().find(|(k, _)| k == key) {
                Some((_, slot)) => *slot = value,
                None => fields.push((key.to_owned(), value)),
            }
        }
    }
    pub fn keys(&self) -> Vec<&str> {
        match self {
            Self::Obj(fields) => fields.iter().map(|(k, _)| k.as_str()).collect(),
            _ => Vec::new(),
        }
    }
    /// Lossy view for code that only needs values (objects become key-sorted maps).
    pub fn to_value(&self) -> Value {
        match self {
            Self::Null => Value::Null,
            Self::Bool(b) => Value::Bool(*b),
            Self::Num(n) => Value::Number(n.clone()),
            Self::Str(s) => Value::String(s.clone()),
            Self::Arr(items) => Value::Array(items.iter().map(Json::to_value).collect()),
            Self::Obj(fields) => Value::Object(
                fields
                    .iter()
                    .map(|(k, v)| (k.clone(), v.to_value()))
                    .collect(),
            ),
        }
    }
    /// `json.dumps(self, separators=(',',':'))`.
    pub fn dumps(&self, text: Text) -> Vec<u8> {
        let mut out = String::new();
        self.write(&mut out, text);
        out.into_bytes()
    }
    fn write(&self, out: &mut String, text: Text) {
        match self {
            Self::Null => out.push_str("null"),
            Self::Bool(true) => out.push_str("true"),
            Self::Bool(false) => out.push_str("false"),
            Self::Num(n) => out.push_str(&number(n)),
            Self::Str(s) => string(s, out, text),
            Self::Arr(items) => {
                out.push('[');
                for (at, item) in items.iter().enumerate() {
                    if at > 0 {
                        out.push(',');
                    }
                    item.write(out, text);
                }
                out.push(']');
            }
            Self::Obj(fields) => {
                out.push('{');
                for (at, (key, value)) in fields.iter().enumerate() {
                    if at > 0 {
                        out.push(',');
                    }
                    string(key, out, text);
                    out.push(':');
                    value.write(out, text);
                }
                out.push('}');
            }
        }
    }
}
impl From<Value> for Json {
    /// Objects keep `serde_json`'s sorted order (the order a `Value` already has).
    fn from(value: Value) -> Self {
        match value {
            Value::Null => Self::Null,
            Value::Bool(b) => Self::Bool(b),
            Value::Number(n) => Self::Num(n),
            Value::String(s) => Self::Str(s),
            Value::Array(items) => Self::Arr(items.into_iter().map(Json::from).collect()),
            Value::Object(map) => {
                Self::Obj(map.into_iter().map(|(k, v)| (k, Json::from(v))).collect())
            }
        }
    }
}
impl From<&Value> for Json {
    fn from(value: &Value) -> Self {
        value.clone().into()
    }
}
impl From<bool> for Json {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}
impl From<&str> for Json {
    fn from(value: &str) -> Self {
        Self::Str(value.to_owned())
    }
}
impl From<String> for Json {
    fn from(value: String) -> Self {
        Self::Str(value)
    }
}
impl From<i64> for Json {
    fn from(value: i64) -> Self {
        Self::Num(value.into())
    }
}
impl From<u64> for Json {
    fn from(value: u64) -> Self {
        Self::Num(value.into())
    }
}

/// `json.dumps(value, separators=(',',':'), sort_keys=True)` for a sorted `Value`.
pub fn dumps_sorted(value: &Value, text: Text) -> Vec<u8> {
    Json::from(value).dumps(text)
}

fn string(s: &str, out: &mut String, text: Text) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if c < ' ' => unit(c as u32, out),
            // ensure_ascii escapes everything outside ' '..='~', including DEL.
            c if text == Text::Ascii && !(' '..='~').contains(&c) => {
                let mut units = [0u16; 2];
                for u in c.encode_utf16(&mut units) {
                    unit(u32::from(*u), out);
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
fn unit(value: u32, out: &mut String) {
    out.push_str(&format!("\\u{value:04x}"));
}

/// An integer prints as its digits; a float as Python's `float.__repr__`.
fn number(n: &Number) -> String {
    let raw = n.to_string();
    if !raw.contains(['.', 'e', 'E']) {
        return raw;
    }
    match raw.parse::<f64>() {
        Ok(v) if v.is_finite() => float_repr(v),
        _ => raw,
    }
}
/// CPython `repr(float)`: the shortest round-trip digits, fixed notation for decimal exponents
/// in `-4..16`, otherwise `d.ddde+XX` with at least two exponent digits.
pub fn float_repr(v: f64) -> String {
    if v == 0.0 {
        return if v.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    let (digits, exponent) = shortest_digits(v.abs());
    let sign = if v.is_sign_negative() { "-" } else { "" };
    let body = if (-4..16).contains(&exponent) {
        if exponent >= 0 {
            let point = exponent as usize + 1;
            if digits.len() <= point {
                format!("{digits}{}.0", "0".repeat(point - digits.len()))
            } else {
                format!("{}.{}", &digits[..point], &digits[point..])
            }
        } else {
            format!("0.{}{digits}", "0".repeat((-exponent - 1) as usize))
        }
    } else {
        let (first, rest) = digits.split_at(1);
        let rest = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let marker = if exponent < 0 { '-' } else { '+' };
        format!("{first}{rest}e{marker}{:02}", exponent.abs())
    };
    format!("{sign}{body}")
}

/// The shortest decimal digit string that reads back as `v`, closest to `v` among equally short
/// ones (David Gay's mode 0, which `repr(float)` uses). Rust's own shortest formatting can pick a
/// different last digit when two candidates are equally short, so the n-digit correctly rounded
/// value is tried for n = 1..=17 (and its neighbours for the rare boundary case).
fn shortest_digits(v: f64) -> (String, i32) {
    for places in 0..17usize {
        let scientific = format!("{v:.places$e}");
        let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
        let exponent: i32 = exponent.parse().unwrap_or(0);
        let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
        if reads_back(&digits, exponent, v) {
            return (digits, exponent);
        }
        // Boundary case: the correctly rounded candidate misses but a neighbour reads back.
        if let Ok(n) = digits.parse::<u64>() {
            for neighbour in [n.checked_add(1), n.checked_sub(1)].into_iter().flatten() {
                let text = format!("{neighbour:0width$}", width = digits.len());
                if text.len() == digits.len() && reads_back(&text, exponent, v) {
                    return (text, exponent);
                }
            }
        }
    }
    let scientific = format!("{v:e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    (
        mantissa.chars().filter(char::is_ascii_digit).collect(),
        exponent.parse().unwrap_or(0),
    )
}
fn reads_back(digits: &str, exponent: i32, v: f64) -> bool {
    format!("{}e{}", digits, exponent - digits.len() as i32 + 1)
        .parse::<f64>()
        .is_ok_and(|parsed| parsed == v)
}

/// Why [`decode_json_bytes`] refused (Python raises `UnicodeDecodeError` or, for a leading
/// U+FEFF, `JSONDecodeError`; both are builtin `ValueError`s on the wire).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DecodeError {
    /// `UnicodeDecodeError`.
    Unicode,
    /// A leading U+FEFF left after decoding: `JSONDecodeError("Unexpected UTF-8 BOM")`.
    LeadingBom,
}

/// The text `json.loads(bytes)` parses: `json.detect_encoding` picks utf-8 / utf-8-sig /
/// utf-16(-le/-be) / utf-32(-le/-be) from the BOM or the NUL pattern of the first four bytes,
/// then the bytes are decoded with `errors='surrogatepass'`. A UTF-16/32 BOM is consumed by the
/// codec, a second BOM (or a BOM left by `utf-8-sig`) is a leading U+FEFF that `json.loads`
/// refuses. Surrogate code points (legal under `surrogatepass`) become U+FFFD: a Rust `String`
/// cannot hold them, and inside a JSON string that is observationally the same for every
/// caller here (they compare the string with fixed literals), while outside a string both
/// are syntax errors.
pub fn decode_json_bytes(raw: &[u8]) -> Result<String, DecodeError> {
    const BOM32_LE: &[u8] = b"\xff\xfe\x00\x00";
    const BOM32_BE: &[u8] = b"\x00\x00\xfe\xff";
    let text = if raw.starts_with(BOM32_LE) || raw.starts_with(BOM32_BE) {
        let big = raw.starts_with(BOM32_BE);
        utf32(&raw[4..], big)?
    } else if raw.starts_with(b"\xff\xfe") || raw.starts_with(b"\xfe\xff") {
        let big = raw.starts_with(b"\xfe\xff");
        utf16(&raw[2..], big)?
    } else if raw.starts_with(b"\xef\xbb\xbf") {
        utf8(&raw[3..])?
    } else if raw.len() >= 4 && raw[0] == 0 {
        // 00 00 -- -- utf-32-be; 00 XX -- -- utf-16-be.
        if raw[1] != 0 {
            utf16(raw, true)?
        } else {
            utf32(raw, true)?
        }
    } else if raw.len() >= 4 && raw[1] == 0 {
        // XX 00 00 00 utf-32-le; otherwise utf-16-le.
        if raw[2] != 0 || raw[3] != 0 {
            utf16(raw, false)?
        } else {
            utf32(raw, false)?
        }
    } else if raw.len() == 2 && raw[0] == 0 {
        utf16(raw, true)?
    } else if raw.len() == 2 && raw[1] == 0 {
        utf16(raw, false)?
    } else {
        utf8(raw)?
    };
    if text.starts_with('\u{feff}') {
        return Err(DecodeError::LeadingBom);
    }
    Ok(text)
}
fn utf8(raw: &[u8]) -> Result<String, DecodeError> {
    let mut out = String::with_capacity(raw.len());
    let mut rest = raw;
    loop {
        match std::str::from_utf8(rest) {
            Ok(valid) => {
                out.push_str(valid);
                return Ok(out);
            }
            Err(error) => {
                let (valid, tail) = rest.split_at(error.valid_up_to());
                out.push_str(std::str::from_utf8(valid).map_err(|_| DecodeError::Unicode)?);
                // surrogatepass: ED A0..BF 80..BF is a (lone) surrogate code point.
                match tail {
                    [0xed, 0xa0..=0xbf, 0x80..=0xbf, ..] => {
                        out.push('\u{fffd}');
                        rest = &tail[3..];
                    }
                    _ => return Err(DecodeError::Unicode),
                }
            }
        }
    }
}
fn utf16(raw: &[u8], big: bool) -> Result<String, DecodeError> {
    if !raw.len().is_multiple_of(2) {
        return Err(DecodeError::Unicode);
    }
    let units = raw.chunks_exact(2).map(|b| {
        if big {
            u16::from_be_bytes([b[0], b[1]])
        } else {
            u16::from_le_bytes([b[0], b[1]])
        }
    });
    Ok(char::decode_utf16(units)
        .map(|r| r.unwrap_or('\u{fffd}'))
        .collect())
}
fn utf32(raw: &[u8], big: bool) -> Result<String, DecodeError> {
    if !raw.len().is_multiple_of(4) {
        return Err(DecodeError::Unicode);
    }
    raw.chunks_exact(4)
        .map(|b| {
            let value = if big {
                u32::from_be_bytes([b[0], b[1], b[2], b[3]])
            } else {
                u32::from_le_bytes([b[0], b[1], b[2], b[3]])
            };
            match value {
                0xd800..=0xdfff => Ok('\u{fffd}'),
                v => char::from_u32(v).ok_or(DecodeError::Unicode),
            }
        })
        .collect()
}

/// Python's `json` accepts lone `\uD800`-`\uDFFF` escapes (it builds a lone surrogate); serde
/// does not. Rewrites each lone surrogate escape inside a string to `\ufffd` (see
/// [`decode_json_bytes`] for why that is equivalent for fixed-literal comparisons). Valid
/// high+low pairs are untouched.
pub fn neutralise_surrogate_escapes(text: &str) -> std::borrow::Cow<'_, str> {
    if !text.contains("\\u") {
        return text.into();
    }
    let bytes = text.as_bytes();
    let hex4 = |at: usize| -> Option<u32> {
        let digits = bytes.get(at..at + 4)?;
        u32::from_str_radix(std::str::from_utf8(digits).ok()?, 16).ok()
    };
    let mut out = String::with_capacity(text.len());
    let (mut at, mut in_string) = (0, false);
    while at < bytes.len() {
        let b = bytes[at];
        if !in_string {
            in_string = b == b'"';
        } else if b == b'"' {
            in_string = false;
        } else if b == b'\\' {
            if bytes.get(at + 1) == Some(&b'u')
                && let Some(unit) = hex4(at + 2)
                && (0xd800..=0xdfff).contains(&unit)
            {
                let pair = unit < 0xdc00
                    && bytes.get(at + 6) == Some(&b'\\')
                    && bytes.get(at + 7) == Some(&b'u')
                    && hex4(at + 8).is_some_and(|low| (0xdc00..=0xdfff).contains(&low));
                if pair {
                    out.push_str(&text[at..at + 12]);
                    at += 12;
                } else {
                    out.push_str("\\ufffd");
                    at += 6;
                }
                continue;
            }
            // Any other escape: copy the backslash and the escaped byte verbatim.
            out.push('\\');
            at += 1;
            if let Some(next) = text[at..].chars().next() {
                out.push(next);
                at += next.len_utf8();
            }
            continue;
        }
        let c = text[at..].chars().next().unwrap_or('\u{fffd}');
        out.push(c);
        at += c.len_utf8();
    }
    out.into()
}

impl From<u32> for Json {
    fn from(value: u32) -> Self {
        Self::Num(value.into())
    }
}
impl From<usize> for Json {
    fn from(value: usize) -> Self {
        Self::Num((value as u64).into())
    }
}
impl From<f64> for Json {
    fn from(value: f64) -> Self {
        Number::from_f64(value).map_or(Self::Null, Self::Num)
    }
}
impl<T: Into<Json>> From<Option<T>> for Json {
    fn from(value: Option<T>) -> Self {
        value.map_or(Self::Null, Into::into)
    }
}
impl<T: Into<Json>> From<Vec<T>> for Json {
    fn from(value: Vec<T>) -> Self {
        Self::Arr(value.into_iter().map(Into::into).collect())
    }
}
/// `jobj!{"key": value, ...}` builds an insertion-ordered [`Json`] object like a Python dict
/// literal; each value goes through `Json::from`.
#[macro_export]
macro_rules! jobj {
    ($($key:expr => $value:expr),* $(,)?) => {
        $crate::wire::Json::Obj(vec![$(($key.to_string(), $crate::wire::Json::from($value))),*])
    };
}

/// Python's `json.loads(text)` for a value this crate only inspects: the constants `NaN`,
/// `Infinity` and `-Infinity` parse in Python (the HTTP boundary refuses them with a
/// `parse_constant` hook, a stored/decoded document does not), serde refuses them, so each one
/// outside a string becomes `null` - never a finite number, which is all that matters to
/// every caller that compares or range-checks the value. `None` is a `JSONDecodeError`.
pub fn parse_python_json(text: &str) -> Option<Value> {
    if let Ok(value) = serde_json::from_str::<Value>(text) {
        return Some(value);
    }
    let mut cleaned = String::with_capacity(text.len());
    let (mut in_string, mut escaped, mut rest) = (false, false, text);
    while let Some(c) = rest.chars().next() {
        let tail = &rest[c.len_utf8()..];
        if in_string {
            in_string = c != '"' || escaped;
            escaped = c == '\\' && !escaped;
        } else if c == '"' {
            in_string = true;
        } else if let Some(after) = ["-Infinity", "Infinity", "NaN"]
            .iter()
            .find_map(|t| rest.strip_prefix(t))
        {
            cleaned.push_str("null");
            rest = after;
            continue;
        }
        cleaned.push(c);
        rest = tail;
    }
    serde_json::from_str::<Value>(&cleaned).ok()
}
