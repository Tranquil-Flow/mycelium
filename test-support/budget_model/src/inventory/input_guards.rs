// SPDX-License-Identifier: AGPL-3.0-only
//! Validated-input guards: a request-body field read after a dominating negated range check of the same
//! field in the same function derives from that check's reviewed upper-guard row. The dominance (an
//! `if ... or not <chain>: raise` statement that precedes the use's statement chain, with no rebinding or
//! item mutation of the container in between) is pinned in `python-input-guards.tsv` from the oracle AST;
//! this module re-checks everything the census can show.
use super::{Bound, Candidate};
use std::collections::BTreeMap;

pub const HEADER: &str = "key\tguard_key\trule\tif_line\tcontainer\tfield\treference";
pub const PREFIX: &str = "input-guard:";
pub const EDGES: &str = include_str!("../../data/python-input-guards.tsv");
pub const RULE: &str = "same-scope-raise-guard";
const ORACLE: &str = "@ f4e366e98a1f705dfaf182897044500d290dbd7f";

pub fn validate(row: &Bound, rows: &[Bound], candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with(row, rows, candidates, EDGES)
}

struct Edge<'a> {
    guard_key: &'a str,
    if_line: usize,
    container: &'a str,
    field: &'a str,
    reference: &'a str,
}

/// The single pinned edge of `key`; `None` for a missing, duplicated or malformed edge or header.
fn edge_of<'a>(text: &'a str, key: &str) -> Option<Edge<'a>> {
    let mut lines = text.lines();
    if lines.next() != Some(HEADER) {
        return None;
    }
    let mut found = None;
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        if f.len() != 7 || f.iter().any(|s| s.trim().is_empty()) {
            return None;
        }
        if f[0] == key {
            if found.is_some() || f[2] != RULE {
                return None;
            }
            found = Some(Edge {
                guard_key: f[1],
                if_line: f[3].parse().ok().filter(|n| *n > 0)?,
                container: f[4],
                field: f[5],
                reference: f[6],
            });
        }
    }
    found
}

/// `name['field']` with a plain identifier and a plain field word.
fn subscript(expr: &str) -> Option<(&str, &str)> {
    let (name, rest) = expr.split_once("['")?;
    let field = rest.strip_suffix("']")?;
    let word = |s: &str| {
        !s.is_empty()
            && !s.starts_with(|c: char| c.is_ascii_digit())
            && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
    };
    (word(name) && word(field)).then_some((name, field))
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
    if claim.split(';').next() != Some(RULE) {
        return false;
    }
    let Some(edge) = edge_of(edges_text, &row.key) else {
        return false;
    };
    let (Some(source), Some(guard)) = (candidates.get(&row.key), candidates.get(edge.guard_key))
    else {
        return false;
    };
    // The use: the exact census row, a keyword or dict field reading `container['field']`.
    let Some((container, field)) = subscript(&source.expression) else {
        return false;
    };
    if source.path != row.path
        || source.line != row.line
        || source.column != row.column
        || source.scope != row.scope
        || source.symbol != row.symbol
        || source.expression != row.expression
        || !matches!(source.kind.as_str(), "keyword" | "dict-field")
        || (container, field) != (edge.container, edge.field)
        || row.disposition != "derived"
        || !row.budget.is_empty()
        || row.value.is_some()
    {
        return false;
    }
    // The guard: a comparison of the same field in the same function, inside an `if` that precedes the use
    // (`if_line <= guard.line < use line`).
    let sub = format!("{container}['{field}']");
    let squeezed = |s: &str| -> String {
        s.chars()
            .filter(|c| !c.is_whitespace())
            .map(|c| if c == '"' { '\'' } else { c })
            .collect()
    };
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    let expression = guard.expression.replace('"', "'");
    let chain = expression
        .split_once(&format!(" <= {sub} <= "))
        .filter(|(lo, hi)| digits(lo) && digits(hi));
    let Some((lo, hi)) = chain else {
        return false;
    };
    // The pinned statement negates exactly this chain as written in the source line.
    if guard.kind != "comparison"
        || guard.path != source.path
        || guard.scope != source.scope
        || guard.line >= source.line
        || edge.if_line > guard.line
        || !squeezed(&guard.source).contains(&format!("not{lo}<={sub}<={hi}"))
        || !edge.reference.contains("exact-span-sha256=")
        || !edge.reference.contains(&format!("{}:", source.path))
        || !edge.reference.contains(ORACLE)
    {
        return false;
    }
    // The container is not rebound between the guard statement and the use (census-visible stores only).
    if candidates.values().any(|c| {
        c.path == source.path
            && c.scope == source.scope
            && c.kind == "assignment"
            && (c.symbol == container || c.symbol.starts_with(&format!("{container}[")))
            && c.line > edge.if_line
            && c.line <= source.line
    }) {
        return false;
    }
    // The guard row is a reviewed upper-guard budget of the row's unit; the row derives from exactly it.
    let Some(bound) = rows.iter().find(|b| b.key == guard.key) else {
        return false;
    };
    let unit_ok = bound.disposition.is_empty()
        && bound.budget.starts_with("PyUpperGuard")
        && BudgetId::from_name(&bound.budget).is_some_and(|id| {
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
        });
    unit_ok && bound.unit == row.unit && row.relation == bound.budget
}
