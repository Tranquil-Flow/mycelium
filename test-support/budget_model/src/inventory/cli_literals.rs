// SPDX-License-Identifier: AGPL-3.0-only
//! CLI/env literal defaults: `r2-cli-byte-default-any-name` and `r2-cli-string-default`.
//!
//! * `byte-quantity`: a `type=int` argparse default whose option name carries `bytes` (the numeric-literal rule
//!   only admits names with a max/limit/reserve token). The literal is a configured byte quantity (workspace
//!   size, required memory/disk), not a ceiling and not a measurement.
//! * `decimal-string`: an exact decimal string default (`'15'`, `'180'`) of a seconds-named option or env var,
//!   consumed by `float()`; the consumer line is pinned by an exact-span hash.
//!
//! Membership is an exact pinned table (`python-cli-literals.tsv`). Each row maps to its own registry id with
//! `Source("path:line @ ef9ae61d")` provenance holding exactly the literal's conversion.
use super::{Bound, Candidate};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::BTreeMap;

pub const HEADER: &str = "key\trule\tunit\tvalue\tid\tconsumer";
pub const PREFIX: &str = "cli-literal:";
pub const TABLE: &str = include_str!("../../data/python-cli-literals.tsv");
const PIN: &str = "ef9ae61d";

/// Exact `a`, `a * b`, `a * b ** c` with decimal literals (underscores removed), checked arithmetic.
fn byte_literal(expression: &str) -> Option<u64> {
    let text = expression.replace('_', "");
    let mut product = 1_u64;
    for factor in text.split(" * ") {
        let (base, exponent) = factor.split_once("**").unwrap_or((factor, "1"));
        let (base, exponent) = (base.trim(), exponent.trim());
        if base.is_empty() || exponent.is_empty() {
            return None;
        }
        if !base
            .bytes()
            .chain(exponent.bytes())
            .all(|b| b.is_ascii_digit())
        {
            return None;
        }
        product = product.checked_mul(
            base.parse::<u64>()
                .ok()?
                .checked_pow(exponent.parse().ok()?)?,
        )?;
    }
    (product > 0).then_some(product)
}

/// `'15'` / `"180"`: a quoted, plain non-negative decimal; milliseconds, exact.
fn seconds_string(expression: &str) -> Option<u64> {
    let inner = expression
        .strip_prefix('\'')
        .and_then(|e| e.strip_suffix('\''))
        .or_else(|| {
            expression
                .strip_prefix('"')
                .and_then(|e| e.strip_suffix('"'))
        })?;
    if inner.is_empty() || !inner.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let value = inner.parse::<u64>().ok()?.checked_mul(1000)?;
    (value > 0).then_some(value)
}

pub fn validate(row: &Bound, candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with(row, candidates, TABLE)
}

pub fn validate_with(row: &Bound, candidates: &BTreeMap<String, Candidate>, table: &str) -> bool {
    if table.lines().next() != Some(HEADER) {
        return false;
    }
    let Some(claim) = row.review.strip_prefix(PREFIX) else {
        return false;
    };
    let rule = claim.split(';').next().unwrap_or("");
    let lines: Vec<_> = table
        .lines()
        .skip(1)
        .filter(|l| l.split('\t').next() == Some(row.key.as_str()))
        .collect();
    let [line] = lines.as_slice() else {
        return false;
    };
    let f: Vec<_> = line.split('\t').collect();
    if f.len() != 6 || f.iter().any(|s| s.trim().is_empty()) || f[1] != rule {
        return false;
    }
    let (unit, id) = (f[2], f[4]);
    let Ok(value) = f[3].parse::<u64>() else {
        return false;
    };
    let Some(c) = candidates.get(&row.key) else {
        return false;
    };
    if c.kind != "cli-default"
        || c.path != row.path
        || c.line != row.line
        || c.column != row.column
        || c.scope != row.scope
        || c.symbol != row.symbol
        || c.expression != row.expression
        || !f[5].contains("exact-span-sha256=")
        || !f[5].starts_with(&format!("{}:", c.path))
    {
        return false;
    }
    let literal_ok = match rule {
        "byte-quantity" => {
            unit == "bytes"
                && c.symbol.starts_with("--")
                && c.symbol.contains("bytes")
                && !c.symbol.contains("per")
                && c.source.contains("type=int")
                && byte_literal(&c.expression) == Some(value)
        }
        "decimal-string" => {
            let name = c.symbol.to_ascii_lowercase().replace('-', "_");
            unit == "milliseconds"
                && (name.ends_with("_seconds") || name.ends_with("_s"))
                && f[5].contains("float(")
                && seconds_string(&c.expression) == Some(value)
        }
        _ => false,
    };
    let registered = BudgetId::from_name(id).is_some_and(|b| {
        let entry = b.entry();
        let stored = match (entry.value, unit) {
            (Value::Storage(n), "bytes") => Some(n.get()),
            (Value::Duration(n), "milliseconds") => Some(n.millis()),
            _ => None,
        };
        stored == Some(value)
            && matches!(entry.provenance, Provenance::Source(text)
                if text == format!("{}:{} @ {PIN}", c.path, c.line))
    });
    literal_ok
        && registered
        && row.budget == id
        && row.unit == unit
        && row.value == Some(value)
        && row.disposition.is_empty()
}
