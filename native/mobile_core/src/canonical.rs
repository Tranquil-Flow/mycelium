// SPDX-License-Identifier: AGPL-3.0-only
//! Canonical JSON byte-compatible with the repository's Python encoders.
//!
//! `json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
//! ensure_ascii=...)`: RouteQualificationV1 evidence uses `ensure_ascii=False`
//! (`mycelium_qualification/evidence.py:58-68`), the Router wire header uses the
//! default `ensure_ascii=True` (`mycelium_router/wire.py`). Floats are rendered with
//! Python's shortest-repr rules so signatures over documents carrying float
//! timestamps verify on both sides.

use serde_json::{Map, Number, Value};

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CanonicalError {
    #[error("noncanonical_json")]
    NonFinite,
}

/// Canonical bytes with `ensure_ascii=False` (evidence and signatures).
pub fn to_bytes(value: &Value) -> Result<Vec<u8>, CanonicalError> {
    let mut out = String::new();
    write_value(&mut out, value, false)?;
    Ok(out.into_bytes())
}

/// Canonical bytes with `ensure_ascii=True` (Router wire headers).
pub fn to_ascii_bytes(value: &Value) -> Result<Vec<u8>, CanonicalError> {
    let mut out = String::new();
    write_value(&mut out, value, true)?;
    Ok(out.into_bytes())
}

/// `"sha256:" + hex(sha256(canonical(value)))`.
pub fn digest(value: &Value) -> Result<String, CanonicalError> {
    Ok(sha256_prefixed(&to_bytes(value)?))
}

pub fn sha256_prefixed(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("sha256:{}", hex::encode(Sha256::digest(bytes)))
}

fn write_value(out: &mut String, value: &Value, ascii: bool) -> Result<(), CanonicalError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => write_number(out, n)?,
        Value::String(s) => write_string(out, s, ascii),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item, ascii)?;
            }
            out.push(']');
        }
        Value::Object(map) => write_object(out, map, ascii)?,
    }
    Ok(())
}

fn write_object(out: &mut String, map: &Map<String, Value>, ascii: bool) -> Result<(), CanonicalError> {
    // UTF-8 byte order equals code point order, which is Python's str ordering.
    let mut keys: Vec<&String> = map.keys().collect();
    keys.sort();
    out.push('{');
    for (i, key) in keys.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        write_string(out, key, ascii);
        out.push(':');
        write_value(out, &map[key.as_str()], ascii)?;
    }
    out.push('}');
    Ok(())
}

fn write_number(out: &mut String, n: &Number) -> Result<(), CanonicalError> {
    // `arbitrary_precision` keeps the literal, so integers of any size survive
    // exactly as Python ints do; floats are re-rendered with Python's repr.
    let literal = n.to_string();
    if literal.contains(['.', 'e', 'E']) {
        let f: f64 = literal.parse().map_err(|_| CanonicalError::NonFinite)?;
        out.push_str(&python_float_repr(f)?);
    } else if literal == "-0" {
        out.push('0');
    } else {
        out.push_str(&literal);
    }
    Ok(())
}

/// Python `repr(float)`: shortest round-trip digits, fixed notation for decimal
/// exponents in [-4, 16), otherwise `d.ddde+XX` with at least two exponent digits.
pub fn python_float_repr(f: f64) -> Result<String, CanonicalError> {
    if !f.is_finite() {
        return Err(CanonicalError::NonFinite);
    }
    if f == 0.0 {
        return Ok(if f.is_sign_negative() { "-0.0".into() } else { "0.0".into() });
    }
    let sci = format!("{:e}", f.abs()); // e.g. "1.790000000125e9", "1e16", "1.5e-5"
    let (mantissa, exp) = sci.split_once('e').expect("{:e} always has an exponent");
    let exp: i32 = exp.parse().expect("exponent is an integer");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let sign = if f.is_sign_negative() { "-" } else { "" };
    let body = if (-4..16).contains(&exp) {
        if exp >= 0 {
            let int_len = exp as usize + 1;
            if digits.len() <= int_len {
                format!("{}{}.0", digits, "0".repeat(int_len - digits.len()))
            } else {
                format!("{}.{}", &digits[..int_len], &digits[int_len..])
            }
        } else {
            format!("0.{}{}", "0".repeat((-exp - 1) as usize), digits)
        }
    } else {
        let mant = if digits.len() == 1 {
            digits.clone()
        } else {
            format!("{}.{}", &digits[..1], &digits[1..])
        };
        let esign = if exp < 0 { '-' } else { '+' };
        format!("{}e{}{:02}", mant, esign, exp.abs())
    };
    Ok(format!("{sign}{body}"))
}

fn write_string(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ascii && (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_float_repr_matches_cpython() {
        let cases = [
            (1790000000.0, "1790000000.0"),
            (1790000000.125, "1790000000.125"),
            (0.0001, "0.0001"),
            (1.5e-05, "1.5e-05"),
            (1e16, "1e+16"),
            (1234567890123456.0, "1234567890123456.0"),
            (2.5, "2.5"),
            (-0.5, "-0.5"),
            (0.1, "0.1"),
            (1e-310, "1e-310"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (0.0, "0.0"),
        ];
        for (value, expected) in cases {
            assert_eq!(python_float_repr(value).unwrap(), expected, "{value}");
        }
    }

    #[test]
    fn ascii_mode_escapes_non_ascii_like_python() {
        let v = serde_json::json!({"k": "é😀\u{7f}"});
        assert_eq!(
            String::from_utf8(to_ascii_bytes(&v).unwrap()).unwrap(),
            "{\"k\":\"\\u00e9\\ud83d\\ude00\\u007f\"}"
        );
        assert_eq!(String::from_utf8(to_bytes(&v).unwrap()).unwrap(), "{\"k\":\"é😀\u{7f}\"}");
    }
}
