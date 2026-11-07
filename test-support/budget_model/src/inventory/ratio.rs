// SPDX-License-Identifier: AGPL-3.0-only
//! RU-024: dimensionless ratio literals. A fraction/ratio/margin literal such as `_DNS_RETRY_FRACTION = 0.5` is a
//! configured value whose unit is the exact decimal ratio in permille (`Value::Ratio`), never time, bytes or a
//! count. Only an exact decimal with at most three fraction digits and a value of at most 1 is admitted (no float
//! rounding), only for the pinned census symbols below, and only as a declaration site (assignment, keyword or
//! dict field). Comparisons against a ratio (`x >= 1.0`) and derived ratios are different rules and stay pending.
use super::{Bound, Candidate};
use std::collections::BTreeMap;

pub const PREFIX: &str = "ratio-category:";
pub const RULE: &str = "literal";
/// Lowercased census symbols that denote a dimensionless ratio at their declaration site.
pub const SYMBOLS: [&str; 5] = [
    "_dns_retry_fraction",
    "_native_response_transit_margin",
    "memory_reserve_fraction",
    "serving_traffic_reserve_ratio",
    "deadline_warm_p95_multiplier",
];
pub const UNIT: &str = "permille";

/// Exact canonical decimal `d` or `d.ddd` (1 to 3 fraction digits, no sign, no exponent, no leading zeros) with a
/// value of at most 1, as permille.
pub fn permille(expr: &str) -> Option<u64> {
    let (whole, fraction) = match expr.split_once('.') {
        Some((whole, fraction)) => (whole, fraction),
        None => (expr, ""),
    };
    let digits = |s: &str| s.bytes().all(|b| b.is_ascii_digit());
    if !digits(whole)
        || !digits(fraction)
        || fraction.len() > 3
        || (expr.contains('.') && fraction.is_empty())
        || (whole.len() > 1 && whole.starts_with('0'))
    {
        return None;
    }
    let whole: u64 = whole.parse().ok()?;
    let scaled: u64 = format!("{fraction:0<3}").parse().ok()?;
    let value = whole.checked_mul(1000)?.checked_add(scaled)?;
    (value <= 1000).then_some(value)
}

/// The permille value of a declaration-site census row, if it is a pinned ratio literal.
pub fn literal(row: &Candidate) -> Option<u64> {
    if !matches!(row.kind.as_str(), "assignment" | "keyword" | "dict-field") {
        return None;
    }
    let symbol = row.symbol.to_ascii_lowercase();
    SYMBOLS
        .contains(&symbol.as_str())
        .then(|| permille(&row.expression))?
}

pub fn validate(row: &Bound, candidates: &BTreeMap<String, Candidate>) -> bool {
    use budgets::BudgetId;
    validate_with(row, candidates, &|name| {
        BudgetId::from_name(name).map(BudgetId::entry)
    })
}

/// `entry_of` resolves a registry id name to its entry (production: the static table; tests: forged entries).
pub fn validate_with(
    row: &Bound,
    candidates: &BTreeMap<String, Candidate>,
    entry_of: &dyn Fn(&str) -> Option<budgets::table::BudgetEntry>,
) -> bool {
    use budgets::{Permille, table::Provenance, table::Value};
    let Some(claim) = row.review.strip_prefix(PREFIX) else {
        return false;
    };
    if claim.split(';').next() != Some(RULE) {
        return false;
    }
    let Some(c) = candidates.get(&row.key) else {
        return false;
    };
    let Some(value) = literal(c) else {
        return false;
    };
    c.path == row.path
        && c.line == row.line
        && c.column == row.column
        && c.scope == row.scope
        && c.symbol == row.symbol
        && c.expression == row.expression
        && row.disposition.is_empty()
        && row.unit == UNIT
        && row.value == Some(value)
        && entry_of(&row.budget).is_some_and(|entry| {
            entry.value == Value::Ratio(Permille::new(value))
                && matches!(entry.provenance, Provenance::Source(source)
                    if row.source_sha.get(..8).is_some_and(|sha|
                        source == format!("{}:{} @ {sha}", c.path, c.line)))
        })
}
