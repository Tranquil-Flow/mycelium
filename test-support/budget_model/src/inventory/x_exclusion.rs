// SPDX-License-Identifier: AGPL-3.0-only
//! RU-013 exclusion: an absolute instant (expiry, lease, valid-until, monotonic deadline) is a typed instant,
//! not a magnitude. Its enforceable allowance is the relative operand where the instant is produced, so the
//! carrier/validator/compare site is `not-a-bound` ONLY with a mechanically derived producer: a census row in the
//! same file whose expression does clock-plus-operand arithmetic, symbol-related to the instant. Membership,
//! class and producer come from the pinned closure forecast (`python-closure-forecast.tsv`, class X); this
//! validator re-checks the lexical producer invariants against the census, so an edited producer, a different
//! file, a producer without clock arithmetic, or a carrier that does clock arithmetic itself is refused.
use super::{Bound, Candidate};
use std::collections::BTreeMap;

pub const PREFIX: &str = "x-exclusion:";
pub const RULES: [&str; 3] = [
    "absolute-instant-carrier",
    "deadline-instant-carrier",
    "absolute-instant-compare",
];
pub const FORECAST: &str = include_str!("../../data/python-closure-forecast.tsv");
const CLOCK_TOKENS: [&str; 4] = ["monotonic(", "time.time(", "_clock(", " + "];

fn normalised(symbol: &str) -> String {
    symbol
        .replace("self._", "")
        .replace("self.", "")
        .trim_start_matches('_')
        .to_string()
}

pub fn validate(row: &Bound, candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with(row, candidates, FORECAST)
}

pub fn validate_with(
    row: &Bound,
    candidates: &BTreeMap<String, Candidate>,
    forecast: &str,
) -> bool {
    let Some(claim) = row.review.strip_prefix(PREFIX) else {
        return false;
    };
    let rule = claim.split(';').next().unwrap_or("");
    if !RULES.contains(&rule) {
        return false;
    }
    let mut matching = forecast
        .lines()
        .skip(1)
        .filter(|l| l.split('\t').next() == Some(row.key.as_str()));
    let (Some(line), None) = (matching.next(), matching.next()) else {
        return false;
    };
    let f: Vec<_> = line.split('\t').collect();
    if f.len() != 9 || f[2] != "X" || f[3] != format!("x-{rule}") || f[7].is_empty() {
        return false;
    }
    let producer = f[7];
    let (Some(c), Some(p)) = (candidates.get(&row.key), candidates.get(producer)) else {
        return false;
    };
    // The carrier must not itself do clock arithmetic (the compare family is a comparison against now).
    let carrier_clean = rule == "absolute-instant-compare"
        || !["monotonic(", "time.time(", " + ", " - "]
            .iter()
            .any(|t| c.expression.contains(t));
    let ps = normalised(&p.symbol);
    carrier_clean
        && p.path == c.path
        && p.key != c.key
        && CLOCK_TOKENS.iter().any(|t| p.expression.contains(t))
        && (normalised(&c.symbol) == ps || c.expression.contains(ps.as_str()))
        && c.path == row.path
        && c.line == row.line
        && c.column == row.column
        && c.scope == row.scope
        && c.symbol == row.symbol
        && c.expression == row.expression
        && row.disposition == "not-a-bound"
        && row.budget.is_empty()
        && row.value.is_none()
        && row.unit == "not-applicable"
        && row.relation == format!("absolute-instant-not-a-magnitude; producer={producer}")
        && row.review.contains(producer)
}
