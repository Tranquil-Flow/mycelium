// SPDX-License-Identifier: AGPL-3.0-only
//! CPython's integer-first / compensated-float sum for planner primary flags.
//! This is numeric wire compatibility, not an allocation or runtime budget.
use super::{C, number};
use crate::ContractError;
use serde_json::Value;
fn add(a: &str, b: &str) -> String {
    let an = a.starts_with('-');
    let bn = b.starts_with('-');
    let a = a.trim_start_matches('-');
    let b = b.trim_start_matches('-');
    let (large, small, negative) = if (a.len(), a) >= (b.len(), b) {
        (a, b, an)
    } else {
        (b, a, bn)
    };
    let mut out = Vec::new();
    let mut carry = 0i16;
    let small = small.as_bytes();
    for (i, x) in large.bytes().rev().enumerate() {
        let y = if i < small.len() {
            i16::from(small[small.len() - 1 - i] - b'0')
        } else {
            0
        };
        let n = i16::from(x - b'0') + carry + if an == bn { y } else { -y };
        out.push(u8::try_from(n.rem_euclid(10)).expect("decimal digit") + b'0');
        carry = n.div_euclid(10);
    }
    if carry > 0 {
        out.push(b'0' + u8::try_from(carry).expect("decimal carry"));
    }
    while out.len() > 1 && out.last() == Some(&b'0') {
        out.pop();
    }
    if negative && out != b"0" {
        out.push(b'-');
    }
    out.reverse();
    String::from_utf8(out).expect("decimal ASCII")
}
pub(super) fn sum(values: impl IntoIterator<Item = Value>) -> Result<Value, ContractError> {
    let mut integer = "0".to_owned();
    let mut floating = false;
    let mut generic = false;
    let (mut hi, mut lo) = (0.0f64, 0.0f64);
    for v in values {
        let text = if let Some(b) = v.as_bool() {
            u8::from(b).to_string()
        } else if v.is_number() {
            v.to_string()
        } else {
            return Err(ContractError(C));
        };
        if !floating && !text.contains(['.', 'e', 'E']) {
            integer = add(&integer, &text);
            generic |= text.parse::<i64>().is_err() || integer.parse::<i64>().is_err();
            continue;
        }
        if !floating {
            hi = integer.parse::<f64>().map_err(|_| ContractError(C))?;
            if !hi.is_finite() {
                return Err(ContractError(C));
            }
            floating = true;
        }
        if !text.contains(['.', 'e', 'E']) && text.parse::<i64>().is_err() {
            hi += lo;
            lo = 0.0;
            generic = true;
        }
        let x = number(&v)?;
        if generic {
            hi += x;
            continue;
        }
        let t = hi + x;
        lo += if hi.abs() >= x.abs() {
            (hi - t) + x
        } else {
            (x - t) + hi
        };
        hi = t;
    }
    if !floating {
        return serde_json::from_str(&integer).map_err(|_| ContractError(C));
    }
    let result = if lo != 0.0 && lo.is_finite() {
        hi + lo
    } else {
        hi
    };
    serde_json::Number::from_f64(result)
        .map(Value::Number)
        .ok_or(ContractError(C))
}
