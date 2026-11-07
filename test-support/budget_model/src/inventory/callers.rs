// SPDX-License-Identifier: AGPL-3.0-only
//! Forward-callers edges: a required parameter forwarded unchanged derives from every pinned call site.
use super::{Bound, Candidate};
use std::collections::{BTreeMap, BTreeSet};

pub const HEADER: &str = "key\ttarget_key\trule\tgroup_size\tcallee\treference";
pub const PREFIX: &str = "forward-callers:";
pub const EDGES: &str = include_str!("../../data/python-forward-callers.tsv");
pub const RULES: [&str; 2] = ["required-parameter", "instant-carrier"];

pub fn validate(row: &Bound, rows: &[Bound], candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with(row, rows, candidates, EDGES)
}

pub fn validate_with(
    row: &Bound,
    rows: &[Bound],
    candidates: &BTreeMap<String, Candidate>,
    edges_text: &str,
) -> bool {
    use budgets::{BudgetId, table::Value};
    let Some(claim) = row.review.strip_prefix(PREFIX) else {
        return false;
    };
    let rule = claim.split(';').next().unwrap_or("");
    let Some(edges) = edges_of(edges_text, &row.key) else {
        return false;
    };
    let n = edges.len();
    // At least one call site; every edge carries this rule, the declared size and the same callee; the
    // targets are strictly ascending (distinct) so the relation order is canonical.
    if !RULES.contains(&rule)
        || n == 0
        || edges
            .iter()
            .any(|e| e.rule != rule || e.group_size != n || e.callee != edges[0].callee)
        || edges.windows(2).any(|w| w[0].target_key >= w[1].target_key)
    {
        return false;
    }
    let Some(source) = candidates.get(&row.key) else {
        return false;
    };
    // The forwarded value is the parameter itself, or `float(parameter)` (the builtin, a value-preserving
    // conversion; the oracle shows the file never rebinds `float`).
    let param = source
        .expression
        .strip_prefix("float(")
        .and_then(|inner| inner.strip_suffix(')'))
        .unwrap_or(source.expression.as_str());
    let identifier = !param.is_empty()
        && !param.starts_with(|c: char| c.is_ascii_digit())
        && param
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_');
    // The forwarded value is a parameter name (optionally under float()) of the function that encloses the row, and the
    // pinned call graph names exactly that function.
    if source.path != row.path
        || source.line != row.line
        || source.column != row.column
        || source.scope != row.scope
        || source.symbol != row.symbol
        || source.expression != row.expression
        || !identifier
        || !matches!(
            source.kind.as_str(),
            "keyword" | "dict-field" | "assignment" | "positional-call" | "positional-timeout"
        )
        || edges[0].callee != format!("{}::{}", source.path, source.scope)
        || row.disposition != "derived"
        || !row.budget.is_empty()
        || row.value.is_some()
    {
        return false;
    }
    // A parameter with its own census row (a default) or any assignment to the name in the same
    // scope is a second root: refused here, the caller graph alone does not cover it.
    if candidates.values().any(|c| {
        c.path == source.path
            && c.scope == source.scope
            && c.symbol == param
            && matches!(c.kind.as_str(), "parameter" | "assignment")
    }) {
        return false;
    }
    let mut ids = Vec::with_capacity(n);
    for e in &edges {
        let Some(target) = candidates.get(e.target_key) else {
            return false;
        };
        let Some(bound) = rows.iter().find(|b| b.key == target.key) else {
            return false;
        };
        let id_text = if bound.budget.is_empty() && bound.disposition == "derived" {
            bound.relation.as_str()
        } else if bound.disposition.is_empty() {
            bound.budget.as_str()
        } else {
            return false;
        };
        // Every id resolves to a numeric (never Blocked) entry of the row's unit.
        let unit_ok = !id_text.is_empty()
            && id_text.split('+').all(|part| {
                BudgetId::from_name(part).is_some_and(|id| {
                    let unit = match id.entry().value {
                        Value::Duration(_) | Value::Derived(_) => "milliseconds",
                        Value::Storage(_) => "bytes",
                        Value::Count(_) => "entries",
                        Value::BytesPerSecond(_) => "bytes-per-second",
                        Value::CountPerSecond(_) => "entries-per-second",
                        Value::Ratio(_) => "permille",
                        Value::Blocked => "blocked",
                    };
                    unit == row.unit
                })
            });
        if target.key == source.key
            || target.kind != "keyword"
            || target.symbol != param
            || bound.unit != row.unit
            || !unit_ok
            || !e.reference.contains(&format!("{}:", target.path))
            || !e.reference.contains("exact-span-sha256=")
        {
            return false;
        }
        // A forwarded parameter is the callers' OWN value: a dotted name or constant arithmetic of numeric
        // literals, never an expression that modifies a variable (round 8, parent decision a). An absolute
        // instant carrier legitimately relates `clock + allowance` and is exempt.
        if rule == "required-parameter" && !is_own_value(&target.expression) {
            return false;
        }
        ids.push(id_text);
    }
    // An absolute instant is a typed instant: the carrier rule only ever relates millisecond rows.
    (rule != "instant-carrier" || row.unit == "milliseconds") && row.relation == ids.join("+")
}

/// A bare (dotted) name, or a numeric literal expression: digits, `_`, `.` joined by `*`, `+`, `-`, `/`
/// with every operand a literal. Calls, conditionals, unary signs and arithmetic over names are not "own".
fn is_own_value(expr: &str) -> bool {
    let e = expr.trim();
    let dotted = !e.is_empty()
        && e.split('.').all(|part| {
            !part.is_empty()
                && !part.starts_with(|c: char| c.is_ascii_digit())
                && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
        });
    let literal_arithmetic = !e.is_empty()
        && e.split(['*', '+', '-', '/']).map(str::trim).all(|operand| {
            !operand.is_empty()
                && operand.starts_with(|c: char| c.is_ascii_digit())
                && operand
                    .bytes()
                    .all(|b| b.is_ascii_digit() || b == b'_' || b == b'.')
        });
    dotted || literal_arithmetic
}

pub struct Edge<'a> {
    pub target_key: &'a str,
    pub rule: &'a str,
    pub group_size: usize,
    pub callee: &'a str,
    pub reference: &'a str,
}

pub fn edges_of<'a>(text: &'a str, key: &str) -> Option<Vec<Edge<'a>>> {
    let mut lines = text.lines();
    if lines.next() != Some(HEADER) {
        return None;
    }
    let mut out = Vec::new();
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        if f.len() != 6 || f.iter().any(|s| s.trim().is_empty()) {
            return None;
        }
        if f[0] == key {
            out.push(Edge {
                target_key: f[1],
                rule: f[2],
                group_size: f[3].parse().ok()?,
                callee: f[4],
                reference: f[5],
            });
        }
    }
    Some(out)
}

pub fn source_keys(text: &str) -> BTreeSet<&str> {
    text.lines()
        .skip(1)
        .filter_map(|l| l.split('\t').next())
        .collect()
}
