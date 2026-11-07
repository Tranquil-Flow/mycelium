// SPDX-License-Identifier: AGPL-3.0-only
//! Multi-target binding edges: one source slot fed by several production callers.
//!
//! The single-target binding table (`python-binding-edges.tsv`) allows exactly one
//! target per source. A constructor parameter with two production callers would
//! be under-stated by one target, so this additive table lists every caller as its
//! own edge with its own provenance. The validator refuses a missing target (the
//! set must equal the keyword-caller census and the declared group size) and an
//! extra target (it must be a caller of the same parameter and be listed).
use super::{Bound, Candidate};
use std::collections::{BTreeMap, BTreeSet};

pub const HEADER: &str = "key\ttarget_key\trule\tgroup_size\treference";
pub const PREFIX: &str = "multi-target-binding:";
pub const EDGES: &str = include_str!("../../data/python-multi-target-edges.tsv");

/// Rules: the store of a required constructor parameter (all callers), and a read
/// of the stored field (the same caller set, through the unique store).
pub const RULES: [&str; 2] = ["constructor-parameter-store", "constructor-field-read"];

pub struct Edge<'a> {
    pub target_key: &'a str,
    pub rule: &'a str,
    pub group_size: usize,
    pub reference: &'a str,
}

/// All edges of one source in table order; `None` on any malformed line.
pub fn edges_of<'a>(text: &'a str, key: &str) -> Option<Vec<Edge<'a>>> {
    let mut lines = text.lines();
    if lines.next() != Some(HEADER) {
        return None;
    }
    let mut out = Vec::new();
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        if f.len() != 5 || f.iter().any(|s| s.trim().is_empty()) {
            return None;
        }
        if f[0] == key {
            out.push(Edge {
                target_key: f[1],
                rule: f[2],
                group_size: f[3].parse().ok()?,
                reference: f[4],
            });
        }
    }
    Some(out)
}

/// Every source key that appears in the table.
pub fn source_keys(text: &str) -> BTreeSet<&str> {
    text.lines()
        .skip(1)
        .filter_map(|l| l.split('\t').next())
        .collect()
}

fn param_of(source: &Candidate) -> Option<&str> {
    source.symbol.strip_prefix("self.")
}

/// Exact keyword-caller census of a parameter name over the pinned candidates.
fn keyword_callers<'a>(
    candidates: &'a BTreeMap<String, Candidate>,
    param: &str,
) -> BTreeSet<&'a str> {
    candidates
        .values()
        .filter(|c| c.kind == "keyword" && c.symbol == param)
        .map(|c| c.key.as_str())
        .collect()
}

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
    // A group is at least two callers; the declared size, the rule and the strictly
    // ascending, distinct target order must agree for every edge of this source.
    if !RULES.contains(&rule)
        || n < 2
        || edges.iter().any(|e| e.rule != rule || e.group_size != n)
        || edges.windows(2).any(|w| w[0].target_key >= w[1].target_key)
    {
        return false;
    }
    let Some(source) = candidates.get(&row.key) else {
        return false;
    };
    if source.path != row.path
        || source.line != row.line
        || source.column != row.column
        || source.scope != row.scope
        || source.symbol != row.symbol
        || source.expression != row.expression
        || !row.disposition.eq("derived")
        || !row.budget.is_empty()
        || row.value.is_some()
    {
        return false;
    }
    // The parameter name and the class that owns the field.
    let (param, class) = match rule {
        "constructor-parameter-store" => {
            let Some(param) = param_of(source) else {
                return false;
            };
            let Some(class) = source.scope.strip_suffix(".__init__") else {
                return false;
            };
            if source.kind != "assignment" || source.expression != param || class.contains('.') {
                return false;
            }
            (param, class)
        }
        _ => {
            let Some(param) = source.expression.strip_prefix("self.") else {
                return false;
            };
            let Some((class, _)) = source.scope.split_once('.') else {
                return false;
            };
            if !matches!(
                source.kind.as_str(),
                "keyword" | "positional-call" | "positional-timeout"
            ) {
                return false;
            }
            (param, class)
        }
    };
    // The census of keyword callers is the authority for completeness: the table
    // must name exactly those callers, no fewer and no more.
    let census = keyword_callers(candidates, param);
    let listed: BTreeSet<_> = edges.iter().map(|e| e.target_key).collect();
    if listed != census || listed.len() != n {
        return false;
    }
    // A read needs the unique reviewed store of the same field in the same class,
    // whose edges (and relation) are exactly this row's.
    if rule == "constructor-field-read" {
        let field = format!("self.{param}");
        let init = format!("{class}.__init__");
        let stores: Vec<_> = candidates
            .values()
            .filter(|c| {
                c.kind == "assignment"
                    && c.path == source.path
                    && c.symbol == field
                    && c.scope == init
            })
            .collect();
        let [store] = stores.as_slice() else {
            return false;
        };
        let Some(store_row) = rows.iter().find(|r| r.key == store.key) else {
            return false;
        };
        let Some(store_edges) = edges_of(edges_text, &store.key) else {
            return false;
        };
        if !store_row
            .review
            .starts_with("multi-target-binding:constructor-parameter-store;")
            || store_row.relation != row.relation
            || store_edges.len() != n
            || store_edges
                .iter()
                .zip(&edges)
                .any(|(a, b)| a.target_key != b.target_key)
        {
            return false;
        }
    }
    // Each target is a reviewed, directly mapped literal row of the same unit, with
    // its own caller provenance (an exact source span) in the edge reference.
    let mut budgets_in_order = Vec::new();
    for e in &edges {
        let Some(target) = candidates.get(e.target_key) else {
            return false;
        };
        let Some(bound) = rows.iter().find(|b| b.key == target.key) else {
            return false;
        };
        let Some(id) = BudgetId::from_name(&bound.budget) else {
            return false;
        };
        let unit = match id.entry().value {
            Value::Duration(_) => "milliseconds",
            Value::Storage(_) => "bytes",
            Value::Count(_) => "entries",
            Value::BytesPerSecond(_) => "bytes-per-second",
            Value::CountPerSecond(_) => "entries-per-second",
            Value::Ratio(_) => "permille",
            Value::Derived(_) | Value::Blocked => return false,
        };
        if !bound.disposition.is_empty()
            || bound.value.is_none()
            || bound.unit != unit
            || unit != row.unit
            || target.kind != "keyword"
            || target.symbol != param
            || !e.reference.contains(&format!("{}:", target.path))
            || !e.reference.contains("exact-span-sha256=")
        {
            return false;
        }
        budgets_in_order.push(bound.budget.as_str());
    }
    row.relation == budgets_in_order.join("+")
}
