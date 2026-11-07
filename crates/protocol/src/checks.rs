// SPDX-License-Identifier: AGPL-3.0-only
use crate::ContractError;
use serde_json::Value;

pub(crate) fn require(ok: bool, code: &'static str) -> Result<(), ContractError> {
    if ok { Ok(()) } else { Err(ContractError(code)) }
}
pub(crate) fn exact(v: &Value, fields: &[&str], code: &'static str) -> Result<(), ContractError> {
    require(
        v.as_object()
            .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|f| m.contains_key(*f))),
        code,
    )
}
pub(crate) fn segment(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 128
            && s.as_bytes()[0].is_ascii_alphanumeric()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    })
}
pub(crate) fn digest(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 71
            && s.starts_with("sha256:")
            && s[7..]
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
/// Python integers are unbounded; do not truncate them to u64 at wire ingress.
pub(crate) fn integer(v: &Value, minimum: u64) -> bool {
    let Some(n) = v.as_number() else {
        return false;
    };
    let s = n.to_string();
    let min = minimum.to_string();
    !s.is_empty()
        && s.bytes().all(|b| b.is_ascii_digit())
        && (s.len() > min.len() || (s.len() == min.len() && s >= min))
}
pub(crate) fn number(v: &Value, code: &'static str) -> Result<f64, ContractError> {
    v.as_f64()
        .filter(|n| n.is_finite())
        .ok_or(ContractError(code))
}
pub(crate) fn text(v: &Value, max: usize) -> bool {
    v.as_str()
        .is_some_and(|s| !s.is_empty() && s.len() <= max && s.trim() == s)
}
pub(crate) fn one_of(v: &Value, choices: &[&str]) -> bool {
    v.as_str().is_some_and(|s| choices.contains(&s))
}
/// Python numeric equality without rounding arbitrary-size integers through f64.
/// Used only where the oracle compares JSON values, not for strict integer fields.
pub(crate) fn python_equal(a: &Value, b: &Value) -> bool {
    fn integer_text(v: &Value) -> Option<String> {
        if let Some(b) = v.as_bool() {
            return Some(u8::from(b).to_string());
        }
        // json.loads reads the integer -0, JSON's one signed integer zero, as int 0 (RU-001).
        let s = v.as_number().map(ToString::to_string)?;
        (!s.contains(['.', 'e', 'E'])).then(|| if s == "-0" { "0".to_owned() } else { s })
    }
    if (a.is_number() || a.is_boolean()) && (b.is_number() || b.is_boolean()) {
        return match (integer_text(a), integer_text(b)) {
            (Some(x), Some(y)) => x == y,
            (Some(x), None) => b.as_f64().is_some_and(|f| {
                f.is_finite()
                    && f.fract() == 0.0
                    && x == if f == 0.0 {
                        "0".to_owned()
                    } else {
                        format!("{f:.0}")
                    }
            }),
            (None, Some(y)) => a.as_f64().is_some_and(|f| {
                f.is_finite()
                    && f.fract() == 0.0
                    && y == if f == 0.0 {
                        "0".to_owned()
                    } else {
                        format!("{f:.0}")
                    }
            }),
            (None, None) => a.as_f64().zip(b.as_f64()).is_some_and(|(x, y)| x == y),
        };
    }
    match (a, b) {
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| python_equal(a, b))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(k, a)| b.get(k).is_some_and(|b| python_equal(a, b)))
        }
        _ => a == b,
    }
}

pub(crate) fn next_integer(previous: &Value, current: &Value) -> bool {
    if !integer(previous, 1) || !integer(current, 2) {
        return false;
    }
    let mut digits = previous.to_string().into_bytes();
    let mut carry = true;
    for digit in digits.iter_mut().rev() {
        if *digit < b'9' {
            *digit += 1;
            carry = false;
            break;
        }
        *digit = b'0';
    }
    if carry {
        digits.insert(0, b'1');
    }
    digits == current.to_string().as_bytes()
}
